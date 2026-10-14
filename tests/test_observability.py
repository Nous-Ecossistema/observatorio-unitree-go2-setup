"""Verifica flush e integridade dos registros produzidos em várias threads."""

import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path

from loguru import logger

from foto_webrtc.config import Config
from foto_webrtc.observability import LoggingSession


class LoggingTests(unittest.TestCase):
    def test_jsonl_flush_correlates_threads_and_captures_library_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            with LoggingSession(Config(log_dir=Path(temp), log_level="ERROR")) as session:
                def emit(worker):
                    for sequence in range(20):
                        logger.bind(component="commands", event="command_sent",
                                    command_id=f"{worker}-{sequence}", parameters={"x": 0.3}).debug("Move")

                threads = [threading.Thread(target=emit, args=(worker,)) for worker in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                logging.getLogger("aiortc").warning("Falha de conexão simulada")
            records = [json.loads(line)["record"] for path in Path(temp).glob("*.jsonl")
                       for line in path.read_text().splitlines()]
            commands = [record for record in records if record["extra"]["event"] == "command_sent"]
            self.assertEqual(len(commands), 80)
            self.assertEqual(len({record["extra"]["command_id"] for record in commands}), 80)
            self.assertEqual({record["extra"]["session_id"] for record in records}, {session.session_id})
            self.assertTrue(any(record["extra"]["component"] == "aiortc" for record in records))
            self.assertEqual(records[-1]["extra"]["event"], "session_finished")
