"""State + Adapter: estado de postura e I/O serializado na thread RTC."""

import asyncio
import time
from dataclasses import replace
from threading import Event

from loguru import logger

from .channels import put_latest
from .models import Command, RobotState
from .transport import WebRTCTransport
from .workers import Worker


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
        self.log = logger.bind(component="robot")

    def _state(self, **changes):
        self.state = replace(self.state, **changes)
        put_latest(self.ui_states, self.state)
        put_latest(self.command_states, self.state)
        self.log.bind(event="robot_state", connected=self.state.connected,
                      busy=self.state.busy, allowed=self.state.allowed, mode=self.state.mode
                      ).info("{}", self.state.message)

    def _failure(self, command, status):
        if command.name == "Move" and any(command.parameter.values()) and \
                command.generation == self.mailbox.generation:
            self._state(allowed=False, message=f"Move {status}. M: diagnosticar; U/R: retomar")
            self.mailbox.halt(f"Move {status}")

    def work(self):
        asyncio.run(self._run())

    def receive_frame(self, track):
        """Ponte thread-safe; recv() continua no loop que criou o track."""
        async def receive():
            return await track.recv()

        if not self.ready.is_set() or self.loop is None or self.loop.is_closed():
            raise ConnectionError("Loop RTC indisponível")
        return asyncio.run_coroutine_threadsafe(receive(), self.loop)

    async def _request(self, command):
        future = self.transport.send(command, wait_response=True)
        # Esta Future pertence ao adapter; cancelar não afeta o resolver do SDK.
        return await asyncio.wait_for(future, self.config.request_timeout + 0.2)

    async def _mode(self, command=None, parent_id=None):
        import json

        response = await self._request(command or Command("Consultar", parent_command_id=parent_id))
        if response is None:
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
        self._state(mode=mode)
        return mode

    async def _posture(self, command):
        allowed_before = self.state.allowed
        self._state(busy=True, allowed=False, message=f"{command.name} em andamento")
        try:
            if command.name == "Consultar":
                mode = await self._mode(command)
                self._state(allowed=allowed_before and mode == "normal")
                return
            if self.state.mode != "normal":
                mode = await self._mode(parent_id=command.command_id)
                if mode is None:
                    return
                if mode != "normal":
                    if await self._request(Command("ModoNormal", (("name", "normal"),),
                                                  parent_command_id=command.command_id)) is None:
                        return
                    await asyncio.sleep(self.config.mode_settle)
                    if await self._mode(parent_id=command.command_id) != "normal":
                        return
            response = await self._request(command)
            if response is None:
                return
            await asyncio.sleep(dict(self.config.posture_delays)[command.name])
            if command.name in ("StandUp", "RecoveryStand"):
                self._state(allowed=True)
                self.log.bind(event="posture_accepted").info("API aceitou a postura; confira a estabilidade antes de andar")
            elif command.name == "Hello":
                self._state(allowed=allowed_before)
        except asyncio.CancelledError:
            self._state(allowed=False)
            raise
        except Exception:
            self._state(allowed=False, mode=None)
            self.log.bind(event="posture_failed", command_id=command.command_id,
                          command=command.name).exception("Falha de postura; locomoção suspensa")
        finally:
            self._state(busy=False, message="Locomoção liberada" if self.state.allowed else
                        "Locomoção suspensa. M: consultar; U: levantar; R: recuperar")

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.transport = self.transport_factory(self.config, self.tracks, self._failure)
        connection = asyncio.create_task(self.transport.connect())
        posture = None
        try:
            while not connection.done() and not self.stop_event.is_set():
                await asyncio.sleep(0.01)
            if self.stop_event.is_set():
                return
            await connection
            self.ready.set()
            self._state(connected=True, message="Conectado. U: levantar; M: consultar modo")
            posture = asyncio.create_task(self._posture(Command("Consultar")))
            while not self.stop_event.is_set():
                self.transport.tick()
                command = self.mailbox.take()
                if command is not None:
                    if command.name == "Move":
                        nonzero = any(command.parameter.values())
                        valid = command.generation == self.mailbox.generation and (
                            command.valid_until is None or time.monotonic() < command.valid_until)
                        if not nonzero or (self.state.allowed and not self.state.busy and valid):
                            try:
                                self.mailbox.dispatch_move(command, self.transport.send)
                            except Exception:
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
            self.ready.clear()
            if not connection.done():
                connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
            if posture is not None:
                posture.cancel()
                await asyncio.gather(posture, return_exceptions=True)
            # Último escritor do driver envia zero antes de desconectar.
            if self.state.connected:
                try:
                    self.transport.send(Command.move())
                except Exception:
                    self.log.bind(event="shutdown_stop_failed").exception("Não foi possível enviar a parada final")
            try:
                await self.transport.disconnect()
            finally:
                self._state(connected=False, allowed=False, busy=False, message="Desconectado")
