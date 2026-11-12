"""State + Adapter: estado de postura e I/O serializado na thread RTC."""

import asyncio
import time
from dataclasses import replace
from threading import Event, Lock

from loguru import logger

from .channels import get_latest, put_latest
from .models import Command, ConnectionState, RobotState
from .transport import WebRTCTransport, is_recoverable_connection_error
from .workers import Worker


SUPPORTED_MOTION_MODES = frozenset(("normal", "mcf"))


class RobotWorker(Worker):
    def __init__(self, config, stop_event, mailbox, tracks, ui_states, command_states,
                 transport_factory=WebRTCTransport):
        super().__init__("go2-rtc", stop_event)
        self.config, self.mailbox, self.tracks = config, mailbox, tracks
        self.ui_states, self.command_states = ui_states, command_states
        self.transport_factory = transport_factory
        self.state = RobotState()
        self.loop = None
        self.ready = Event()
        self.reconnect_requested = Event()
        self.reconnect_requested.set()  # uma tentativa inicial, sem retry automático
        self._reconnect_lock = Lock()
        self._attempt_active = False
        self.connection_generation = 0
        self._receive_tasks = set()
        self.log = logger.bind(component="robot")

    def _state(self, **changes):
        self.state = replace(self.state, **changes)
        if not self.state.connected:
            self.state = replace(self.state, allowed=False, faults=())
        elif self.state.blocking_faults:
            codes = "; ".join(fault.label for fault in self.state.blocking_faults)
            self.state = replace(self.state, allowed=False,
                                 message=f"Falhas de motor/proteção ativas: {codes}. Verifique o Unitree Go")
        put_latest(self.ui_states, self.state)
        put_latest(self.command_states, self.state)
        self.log.bind(event="robot_state", connected=self.state.connected,
                      busy=self.state.busy, allowed=self.state.allowed, mode=self.state.mode,
                      connection_state=self.state.connection_state.value,
                      faults=[fault.label for fault in self.state.faults]
                      ).info("{}", self.state.message)

    def _failure(self, command, status):
        if command.name == "Move" and any(command.parameter.values()) and \
                command.generation == self.mailbox.generation:
            self._state(allowed=False, message=f"Move {status}. M: diagnosticar; U/R: retomar")
            self.mailbox.halt(f"Move {status}")

    def work(self):
        asyncio.run(self._run())

    def request_reconnect(self):
        """Chamável pela GUI: sinaliza o worker existente sem criar uma thread."""
        with self._reconnect_lock:
            if (self.stop_event.is_set() or self._attempt_active or self.ready.is_set()
                    or self.reconnect_requested.is_set()):
                return False
            self.reconnect_requested.set()
        self.log.bind(event="reconnect_requested").info("Reconexão manual solicitada")
        return True

    def receive_frame(self, track):
        """Ponte thread-safe; recv() continua no loop que criou o track."""
        generation = self.connection_generation

        async def receive():
            if not self.ready.is_set() or generation != self.connection_generation:
                raise ConnectionError("Sessão RTC da câmera encerrada")
            task = asyncio.current_task()
            self._receive_tasks.add(task)
            try:
                return await track.recv()
            finally:
                self._receive_tasks.discard(task)

        if not self.ready.is_set() or self.loop is None or self.loop.is_closed():
            raise ConnectionError("Loop RTC indisponível")
        return asyncio.run_coroutine_threadsafe(receive(), self.loop)

    async def _request(self, command):
        self._sync_faults()
        if self.state.blocking_faults and command.name != "Consultar":
            self.log.bind(event="fault_command_blocked", command=command.name).warning(
                "Comando bloqueado por falha ativa no robô: {}", command.name)
            return None
        future = self.transport.send(command, wait_response=True)
        # Esta Future pertence ao adapter; cancelar não afeta o resolver do SDK.
        return await asyncio.wait_for(future, self.config.request_timeout + 0.2)

    def _sync_faults(self):
        faults = getattr(self.transport, "faults", ())
        if faults == self.state.faults:
            return
        had_blocking_fault = bool(self.state.blocking_faults)
        blocking = any(fault.blocks_motion for fault in faults)
        if blocking:
            self.mailbox.halt("falha ativa no robô")
            self._state(faults=faults, allowed=False)
        elif had_blocking_fault:
            self._state(faults=faults, allowed=False,
                        message="Falhas de motor/proteção removidas. U/R para retomar")
        else:
            self._state(faults=faults)

    async def _mode(self, command=None, parent_id=None):
        import json

        response = await self._request(command or Command("Consultar", parent_command_id=parent_id))
        if response is None:
            self.transport.set_motion_mode(None)
            self._state(mode=None)
            return None
        try:
            data = response["data"]["data"]
            mode = (json.loads(data) if isinstance(data, str) else data)["name"]
            if not isinstance(mode, str):
                raise ValueError("modo inválido")
        except (TypeError, ValueError, KeyError):
            self.log.bind(event="invalid_mode", response=response).warning("Resposta de modo inválida")
            mode = None
        self.transport.set_motion_mode(mode)
        self._state(mode=mode)
        return mode

    async def _posture(self, command):
        allowed_before = self.state.allowed
        failure_message = "Locomoção suspensa. M: consultar; U: levantar; R: recuperar"
        self._state(busy=True, allowed=False, message=f"{command.name} em andamento")
        try:
            if command.name == "Consultar":
                mode = await self._mode(command)
                self._state(allowed=allowed_before and mode in SUPPORTED_MOTION_MODES)
                return
            if self.state.mode not in SUPPORTED_MOTION_MODES:
                mode = await self._mode(parent_id=command.command_id)
                if mode is None:
                    return
                if mode not in SUPPORTED_MOTION_MODES:
                    if await self._request(Command("ModoNormal", (("name", "normal"),),
                                                  parent_command_id=command.command_id)) is None:
                        return
                    await asyncio.sleep(self.config.mode_settle)
                    if await self._mode(parent_id=command.command_id) != "normal":
                        return
            response = await self._request(command)
            if response is None:
                failure_message = f"{command.name} sem confirmação. Verifique código/timeout no terminal; M: consultar"
                return
            await asyncio.sleep(dict(self.config.posture_delays)[command.name])
            if command.name in ("StandUp", "RecoveryStand"):
                if self.state.mode == "mcf":
                    self._state(message="Preparando equilíbrio (BalanceStand); aguarde")
                    balance = Command("BalanceStand", parent_command_id=command.command_id)
                    if await self._request(balance) is None:
                        failure_message = "Equilíbrio sem confirmação. Locomoção suspensa; B: equilibrar; M: consultar"
                        return
                    await asyncio.sleep(dict(self.config.posture_delays).get("BalanceStand", 1.0))
                self._state(allowed=True)
                self.log.bind(event="posture_accepted").info("API aceitou a postura; confira a estabilidade antes de andar")
            elif command.name == "BalanceStand":
                self._state(allowed=True)
            elif command.name == "Hello":
                self._state(allowed=allowed_before)
        except asyncio.CancelledError:
            self._state(allowed=False)
            raise
        except BufferError:
            self._state(allowed=False, mode=None)
            self.log.bind(event="posture_failed", command_id=command.command_id,
                          command=command.name).exception("Falha de postura; locomoção suspensa")
        finally:
            self._state(busy=False, message="Locomoção liberada" if self.state.allowed else
                        failure_message)

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        try:
            while not self.stop_event.is_set():
                if not self.reconnect_requested.is_set():
                    await asyncio.sleep(0.02)
                    continue
                with self._reconnect_lock:
                    self.reconnect_requested.clear()
                    self._attempt_active = True
                self.connection_generation += 1
                self.mailbox.reset("nova tentativa de conexão")
                self._state(connected=False, allowed=False, busy=False, mode=None,
                            connection_state=ConnectionState.CONNECTING,
                            message="Conectando ao Go2...")
                self._was_connected = False
                try:
                    self.transport = self.transport_factory(self.config, self.tracks, self._failure)
                    await self._session()
                except Exception as error:
                    if not is_recoverable_connection_error(error):
                        raise
                    status = ConnectionState.DISCONNECTED if self._was_connected else ConnectionState.ERROR
                    self._state(connected=False, allowed=False, busy=False, mode=None,
                                connection_state=status,
                                message=f"{status.value}: {error}. Modo offline; reconecte manualmente")
                    self.log.bind(event="connection_recoverable", error_type=type(error).__name__,
                                  attempt=self.connection_generation).warning(
                                      "Comunicação indisponível; interface mantida em modo offline: {}", error)
                finally:
                    with self._reconnect_lock:
                        self._attempt_active = False
        finally:
            self.ready.clear()
            self.reconnect_requested.clear()
            self._state(connected=False, allowed=False, busy=False, mode=None,
                        connection_state=ConnectionState.DISCONNECTED, message="Desconectado")

    async def _session(self):
        connection = asyncio.create_task(self.transport.connect())
        posture = None
        try:
            while not connection.done() and not self.stop_event.is_set():
                await asyncio.sleep(0.01)
            if self.stop_event.is_set():
                return
            await connection
            self.ready.set()
            self._was_connected = True
            self._state(connected=True, allowed=False, connection_state=ConnectionState.CONNECTED,
                        message="Conectado. U: levantar; M: consultar modo")
            self._sync_faults()
            posture = asyncio.create_task(self._posture(Command("Consultar")))
            while not self.stop_event.is_set():
                self.transport.tick()
                self._sync_faults()
                if posture is not None and posture.done():
                    posture.result()  # erros críticos/da comunicação não são silenciados
                    posture = None
                command = self.mailbox.take()
                if command is not None:
                    if command.name == "Move":
                        nonzero = any(command.parameter.values())
                        valid = command.generation == self.mailbox.generation and (
                            command.valid_until is None or time.monotonic() < command.valid_until)
                        if not nonzero or (self.state.allowed and not self.state.busy and valid):
                            try:
                                self.mailbox.dispatch_move(command, self.transport.send)
                            except BufferError:
                                self.log.bind(event="movement_send_failed", command_id=command.command_id,
                                              parameters=command.parameter).exception("Falha ao enviar Move")
                                self._state(allowed=False, message="Falha no envio. Verifique conexão e modo")
                                if nonzero:
                                    self.mailbox.halt("falha no envio")
                                else:
                                    raise
                        else:
                            self.log.bind(event="stale_command", command_id=command.command_id
                                          ).debug("Move antigo ou inibido descartado")
                    elif posture is not None and not posture.done():
                        self.log.bind(event="action_busy", command_id=command.command_id,
                                      command=command.name).warning("Ação em andamento; novo pedido rejeitado")
                    else:
                        posture = asyncio.create_task(self._posture(command))
                await asyncio.sleep(0.005)
        finally:
            was_connected = self.state.connected
            self.ready.clear()
            self._state(connected=False, allowed=False, busy=False, mode=None,
                        connection_state=ConnectionState.DISCONNECTED,
                        message="Desconectado. Fechando sessão WebRTC")
            self.mailbox.reset("conexão indisponível")
            receives = list(self._receive_tasks)
            for task in receives:
                task.cancel()
            if receives:
                await asyncio.gather(*receives, return_exceptions=True)
            if not connection.done():
                connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
            if posture is not None:
                posture.cancel()
                await asyncio.gather(posture, return_exceptions=True)
            # Último escritor do driver envia zero antes de desconectar.
            if was_connected:
                try:
                    self.transport.send(Command.move())
                except Exception:
                    self.log.bind(event="shutdown_stop_failed").exception("Não foi possível enviar a parada final")
            try:
                await self.transport.disconnect()
            except Exception as error:
                if not is_recoverable_connection_error(error):
                    raise
                self.log.bind(event="disconnect_recoverable", error_type=type(error).__name__
                              ).warning("Falha recuperável no fechamento da comunicação: {}", error)
            finally:
                get_latest(self.tracks)
                self.mailbox.reset("sessão WebRTC encerrada")
                self._state(connected=False, allowed=False, busy=False, mode=None,
                            connection_state=ConnectionState.DISCONNECTED,
                            message="Desconectado. Modo offline; reconecte manualmente")
