"""Loguru: console legível e JSONL correlacionado, com rotação e flush."""

import logging
import sys
from uuid import uuid4

from loguru import logger


class InterceptHandler(logging.Handler):
    """Encaminha falhas das bibliotecas WebRTC/aiortc para os mesmos sinks."""

    def emit(self, record):
        logger.bind(component=record.name, event="library_log").opt(
            exception=record.exc_info
        ).log(record.levelname, record.getMessage())


class LoggingSession:
    def __init__(self, config):
        self.config = config
        self.session_id = uuid4().hex
        self.sinks = []
        self.handler = InterceptHandler()

    def __enter__(self):
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        logger.remove()
        logger.configure(extra={"session_id": self.session_id, "component": "application", "event": "message"})
        common = dict(enqueue=True, backtrace=True, diagnose=False)
        self.sinks.append(logger.add(sys.stderr, level=self.config.log_level,
            format="{time:HH:mm:ss.SSS} | {level: <8} | {thread.name} | {extra[component]} | {message}",
            **common))
        self.sinks.append(logger.add(self.config.log_dir / "go2_{time:YYYY-MM-DD}.jsonl",
            level="DEBUG", serialize=True, rotation="10 MB", retention="14 days",
            encoding="utf-8", **common))
        root = logging.getLogger()
        self.previous_level = root.level
        root.addHandler(self.handler)
        root.setLevel(logging.WARNING)
        logger.bind(event="session_started").info("Sessão iniciada: {}", self.session_id)
        return self

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self.handler)
        logging.getLogger().setLevel(self.previous_level)
        logger.bind(event="session_finished").info("Sessão encerrada")
        logger.complete()
        for sink in self.sinks:
            logger.remove(sink)
