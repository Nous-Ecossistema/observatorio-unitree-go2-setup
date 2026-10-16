"""Facade: conecta os workers à View e coordena o encerramento."""

import time
from queue import Full, Queue
from threading import Event, Thread

from loguru import logger

from .channels import CommandMailbox, get_latest, put_latest
from .commands import MOVEMENT_KEYS, LocomotionWorker
from .models import Command, InputSnapshot, RobotState, VideoState
from .robot import RobotWorker
from .video import CaptureWorker, VideoProcessor


class Application:
    def __init__(self, config, window, transport_factory=None):
        self.config, self.window = config, window
        self.stop_event = Event()
        self.finished = Event()
        self.slow_shutdown = Event()
        self.mailbox = CommandMailbox()
        self.inputs, self.previews, self.sizes = (Queue(maxsize=1) for _ in range(3))
        self.ui_states, self.command_states, self.video_states = (Queue(maxsize=1) for _ in range(3))
        self.tracks, self.frames = Queue(maxsize=1), Queue(maxsize=2)
        self.video_actions = Queue(maxsize=16)
        kwargs = {"transport_factory": transport_factory} if transport_factory is not None else {}
        self.robot = RobotWorker(config, self.stop_event, self.mailbox, self.tracks,
                                 self.ui_states, self.command_states, **kwargs)
        self.workers = [
            VideoProcessor(config, self.stop_event, self.frames, self.previews,
                           self.video_actions, self.video_states, self.sizes),
            CaptureWorker(self.stop_event, self.robot, self.tracks, self.frames),
            LocomotionWorker(config, self.stop_event, self.inputs, self.command_states, self.mailbox),
            self.robot,
        ]
        self.started = []
        self.closer = None
        self.robot_state, self.video_state = RobotState(), VideoState()
        self.frame_ts = 0.0
        self.gui_failure = None
        self.window.on_action = self.action
        self.window.on_close = self.request_shutdown
        self.window.root.report_callback_exception = self._gui_exception

    def _gui_exception(self, kind, error, traceback):
        self.gui_failure = error
        logger.bind(event="gui_callback_failed", component="gui").opt(
            exception=(kind, error, traceback)).error("Falha em callback da interface")
        self.request_shutdown()
        self.window.root.after(30, self._tick)

    def start(self):
        for worker in self.workers:
            worker.start()
            self.started.append(worker)

    def action(self, name):
        if self.stop_event.is_set():
            return
        if name == "Reconectar":
            return self.robot.request_reconnect()
        elif name == "Parar":
            self.mailbox.halt("teclado/foco")
        elif name in ("Foto", "Video"):
            command = Command(name)
            try:
                self.video_actions.put_nowait(command)
            except Full:
                logger.bind(event="queue_full", component="video", command=name,
                            command_id=command.command_id).warning("Fila de captura cheia; ação rejeitada")
        else:
            if not self.robot.ready.is_set() or not self.robot.state.connected:
                logger.bind(event="offline_command_blocked", component="commands", command=name
                            ).warning("Comando bloqueado em modo offline: {}", name)
                return False
            if self.robot.state.busy or not self.mailbox.actions.empty():
                logger.bind(event="action_busy", component="commands", command=name).warning(
                    "Ação rejeitada: aguarde a ação atual terminar")
                return False
            return self.mailbox.action(name)

    def _tick(self):
        if self.stop_event.is_set():
            self.request_shutdown()
            if self.finished.is_set():
                self.window.root.quit()
                return
            self.window.status.set("Encerrando: aguardando threads e finalizando MP4..." if not
                                   self.slow_shutdown.is_set() else "Encerramento lento; aguardando recursos (veja logs)")
        else:
            previous = self.robot_state
            self.robot_state = get_latest(self.ui_states, self.robot_state)
            if not self.robot_state.connected or not previous.connected:
                self.window.pressionadas.difference_update(MOVEMENT_KEYS)
            if hasattr(self.window, "set_connection_state"):
                self.window.set_connection_state(self.robot_state.connection_state)
            put_latest(self.inputs, InputSnapshot(frozenset(self.window.pressionadas),
                                                 generation=self.mailbox.generation))
            put_latest(self.sizes, (self.window.preview.winfo_width(), self.window.preview.winfo_height()))
            preview = get_latest(self.previews)
            if preview is not None:
                self.window.mostrar(preview)
                self.frame_ts = preview.received_at
            self.video_state = get_latest(self.video_states, self.video_state)
            camera = "OK" if self.frame_ts and time.monotonic() - self.frame_ts < self.config.frame_timeout else "sem imagens recentes"
            if not self.robot_state.connected:
                camera = "offline"
            if hasattr(self.window, "atualizar_estado"):
                self.window.atualizar_estado(self.robot_state, camera, self.video_state)
            else:
                self.window.status.set(f"{self.robot_state.connection_state.value} | Modo: {self.robot_state.mode or 'desconhecido'} | {self.robot_state.message} | Câmera: {camera} | {self.video_state.message}")
        self.window.root.after(30, self._tick)

    def request_shutdown(self):
        if self.closer is not None:
            return
        self.window.pressionadas.clear()
        self.mailbox.halt("encerramento")
        self.stop_event.set()
        logger.bind(event="shutdown_started").info("Encerrando e finalizando arquivos")
        # join() não acontece em callbacks Tk: a janela continua respondendo.
        self.closer = Thread(target=self._join, name="go2-shutdown", daemon=False)
        self.closer.start()

    def _join(self):
        deadline = time.monotonic() + self.config.shutdown_timeout
        while True:
            alive = [worker for worker in self.started if worker.is_alive()]
            if not alive:
                break
            for worker in alive:
                worker.join(timeout=0.05)
            if time.monotonic() > deadline and not self.slow_shutdown.is_set():
                self.slow_shutdown.set()
                logger.bind(event="shutdown_slow", threads=[worker.name for worker in alive]
                            ).error("Threads demorando para encerrar; aguardando fechamento seguro")
        self.finished.set()

    def run(self):
        try:
            self.start()
            self.window.root.after(0, self._tick)
            self.window.root.mainloop()
        finally:
            self.request_shutdown()
            # Também trata mainloop interrompido pelo terminal ou por erro Tk.
            self.closer.join()
        failures = [worker for worker in self.started if worker.failure is not None]
        if failures:
            raise RuntimeError("Falha nas threads: " + ", ".join(worker.name for worker in failures))
        if self.gui_failure is not None:
            raise RuntimeError("Falha na interface") from self.gui_failure
