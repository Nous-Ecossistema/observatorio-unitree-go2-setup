"""Ciclo de vida comum dos workers; falhas provocam parada coordenada."""

from threading import Thread

from loguru import logger


class Worker(Thread):
    def __init__(self, name, stop_event):
        # Workers não daemon: os recursos devem ser fechados antes de sair.
        super().__init__(name=name, daemon=False)
        self.stop_event = stop_event
        self.failure = None

    def run(self):
        log = logger.bind(component=self.name)
        log.bind(event="worker_started").info("Thread iniciada")
        try:
            self.work()
        except Exception as error:
            self.failure = error
            log.bind(event="worker_failed").exception("Thread falhou")
            self.stop_event.set()
        finally:
            log.bind(event="worker_stopped").info("Thread encerrada")

    def work(self):
        raise NotImplementedError
