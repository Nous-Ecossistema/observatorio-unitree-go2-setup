"""Filas limitadas: frames/velocidades recentes e ações discretas FIFO."""

import time
from queue import Empty, Full, Queue
from threading import RLock

from loguru import logger

from .models import Command


def put_latest(queue, item):
    """Substitui itens antigos sem bloquear; apropriado para um produtor."""
    while True:
        try:
            queue.put_nowait(item)
            return
        except Full:
            try:
                queue.get_nowait()
            except Empty:
                pass


def get_latest(queue, default=None):
    item = default
    while True:
        try:
            item = queue.get_nowait()
        except Empty:
            return item


class CommandMailbox:
    """Parada prioritária e geração para invalidar velocidades anteriores.

    Todos os escritores passam pelo mesmo lock. A conexão é o único leitor;
    nenhuma fila permite acumular velocidades para reproduzir depois.
    """

    def __init__(self):
        self.actions = Queue(maxsize=8)
        self.moves = Queue(maxsize=1)
        self.stops = Queue(maxsize=1)
        self._generation = 0
        self._lock = RLock()

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def halt(self, reason="Parar"):
        with self._lock:
            self._generation += 1
            get_latest(self.moves)
            command = Command.move(generation=self._generation)
            put_latest(self.stops, command)
            logger.bind(component="commands", event="stop_requested",
                        command_id=command.command_id, reason=reason,
                        generation=self._generation).info("Parada solicitada")

    def action(self, name):
        with self._lock:
            self._generation += 1
            get_latest(self.moves)
            command = Command(name, generation=self._generation)
            put_latest(self.stops, Command.move(generation=self._generation,
                                               parent_command_id=command.command_id))
            try:
                self.actions.put_nowait(command)
            except Full:
                logger.bind(component="commands", event="queue_full", command_id=command.command_id,
                            command=name).warning("Fila de ações cheia; ação rejeitada")
                return False
            logger.bind(component="commands", event="command_queued", command_id=command.command_id,
                        command=name).info("Ação enfileirada")
            return True

    def move(self, command):
        with self._lock:
            if command.generation != self._generation:
                return False
            put_latest(self.moves, command)
            return True

    def take(self):
        with self._lock:
            for queue in (self.stops, self.actions, self.moves):
                try:
                    command = queue.get_nowait()
                except Empty:
                    continue
                if queue is self.moves and (command.generation != self._generation or
                        command.valid_until is not None and time.monotonic() > command.valid_until):
                    return Command.move(generation=self._generation)
                return command
        return None

    def dispatch_move(self, command, callback):
        """Validação e envio atômicos em relação a halt()/action()."""
        with self._lock:
            nonzero = any(command.parameter.values())
            if nonzero and (command.generation != self._generation or
                    command.valid_until is not None and time.monotonic() > command.valid_until):
                return False
            callback(command)
            return True
