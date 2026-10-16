"""Falhas de protocolo e correlação sem conectar a um Go2."""

import asyncio
import json
import unittest
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from loguru import logger
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD, SPORT_CMD_MCF

from foto_webrtc.config import Config
from foto_webrtc.models import Command
from foto_webrtc.transport import WebRTCTransport


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.records = []
        self.sink = logger.add(lambda message: self.records.append(message.record), level="DEBUG")
        self.failure = Mock()
        self.transport = WebRTCTransport(Config(request_timeout=0.05), Queue(1), self.failure)
        self.transport.topics = RTC_TOPIC
        self.transport.apis = SPORT_CMD
        self.transport.mcf_apis = SPORT_CMD_MCF
        self.channel = SimpleNamespace(readyState="open", bufferedAmount=0)
        self.transport.channel = self.channel
        self.transport.pub_sub = SimpleNamespace(publish_without_callback=Mock())

    async def asyncTearDown(self):
        logger.remove(self.sink)

    def reply(self, request_id, code=0, api=1008, topic="rt/api/sport/response", data=None):
        return json.dumps({"type": "res", "topic": topic, "data": {
            "header": {"identity": {"id": request_id, "api_id": api}, "status": {"code": code}},
            "data": data}})

    def last_id(self):
        return self.transport.pub_sub.publish_without_callback.call_args.args[1]["header"]["identity"]["id"]

    async def test_move_payload_and_correlated_success(self):
        command = Command.move(0.3, -0.3, 0.5)
        self.transport.send(command)
        request_id = self.last_id()
        payload = self.transport.pub_sub.publish_without_callback.call_args.args[1]
        self.assertEqual(self.transport.pub_sub.publish_without_callback.call_args.args[0],
                         "rt/api/sport/request")
        self.assertEqual(json.loads(payload["parameter"]), {"x": 0.3, "y": -0.3, "z": 0.5})
        self.transport._on_message(self.reply(request_id))
        self.assertFalse(self.transport.pending)
        result = next(r["extra"] for r in self.records if r["extra"].get("event") == "command_result")
        self.assertEqual(result["command_id"], command.command_id)
        self.assertEqual(result["request_id"], request_id)
        self.assertEqual(result["status"], "accepted")
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.failure.assert_not_called()

    async def test_mode_query_accepts_robot_response_topic_and_json_string(self):
        future = self.transport.send(Command("Consultar"), wait_response=True)
        request_id = self.last_id()
        self.assertEqual(self.transport.pub_sub.publish_without_callback.call_args.args[0],
                         "rt/api/motion_switcher/request")
        self.transport._on_message(self.reply(request_id, api=1001,
            topic="rt/api/motion_switcher/response", data='{"form":"0","name":"mcf"}'))
        response = await asyncio.wait_for(future, 0.1)
        self.assertEqual(json.loads(response["data"]["data"])["name"], "mcf")
        self.assertFalse(self.transport.pending)
        self.failure.assert_not_called()

    async def test_mcf_routes_supported_controls_using_driver_table(self):
        self.transport.set_motion_mode("mcf")
        for name in ("Move", "StandUp", "StandDown", "RecoveryStand", "Hello", "Damp"):
            self.transport.send(Command(name))
            args = self.transport.pub_sub.publish_without_callback.call_args.args
            self.assertEqual(args[0], "rt/api/sport/request")
            self.assertEqual(args[1]["header"]["identity"]["api_id"], SPORT_CMD_MCF[name])

    async def test_timeout_then_late_response_without_cancelled_sdk_future(self):
        command = Command("StandUp")
        future = self.transport.send(command, wait_response=True)
        request_id = self.last_id()
        self.transport.pending[request_id].started -= 0.1
        self.transport.tick()
        self.assertIsNone(await future)
        self.assertFalse(future.cancelled())
        self.transport._on_message(self.reply(request_id, api=1004))
        self.failure.assert_called_once_with(command, "timeout")
        self.assertTrue(any(r["extra"].get("event") == "late_response" for r in self.records))

    async def test_rejection_and_malformed_response_inhibit_movement(self):
        for code, api, expected in [(3102, 1008, "rejected"), (None, 1008, "malformed"), (0, 999, "malformed")]:
            command = Command.move(0.3)
            self.transport.send(command)
            self.transport._on_message(self.reply(self.last_id(), code=code, api=api))
            self.failure.assert_called_with(command, expected)

    async def test_unrelated_topic_cannot_complete_request(self):
        self.transport.send(Command.move(0.3))
        request_id = self.last_id()
        for topic in ("other", "rt/api/motion_switcher/response", "rt/api/sport/request"):
            self.transport._on_message(self.reply(request_id, topic=topic))
            self.assertIn(request_id, self.transport.pending)

    async def test_late_response_with_wrong_topic_does_not_consume_expired_request(self):
        self.transport.send(Command("StandUp"))
        request_id = self.last_id()
        self.transport.pending[request_id].started -= 0.1
        self.transport.tick()
        self.transport._on_message(self.reply(request_id, api=1004, topic="other"))
        self.assertIn(request_id, self.transport.expired)
        self.transport._on_message(self.reply(request_id, api=1004))
        self.assertNotIn(request_id, self.transport.expired)

    async def test_closed_channel_and_network_backpressure(self):
        self.channel.readyState = "closed"
        with self.assertRaises(ConnectionError):
            self.transport.send(Command.move(0.3))
        self.transport.pub_sub.publish_without_callback.assert_not_called()
        self.channel.readyState = "open"
        self.channel.bufferedAmount = 300 * 1024
        with self.assertRaises(BufferError):
            self.transport.send(Command.move(0.3))
        self.transport.send(Command.move())  # zero ainda pode ser enviado

    async def test_pending_registry_is_bounded_and_stop_still_sends(self):
        self.transport.MAX_PENDING = 8
        for _ in range(8):
            self.transport.send(Command.move(0.3))
        with self.assertRaises(BufferError):
            self.transport.send(Command.move(0.3))
        self.transport.send(Command.move())
        self.assertEqual(len(self.transport.pending), 8)

    async def test_send_exception_is_logged_and_pending_entry_is_removed(self):
        self.transport.pub_sub.publish_without_callback.side_effect = OSError("SCTP failed")
        with self.assertRaises(OSError):
            self.transport.send(Command.move(0.3))
        self.assertFalse(self.transport.pending)
        self.assertTrue(any(r["extra"].get("event") == "send_failed" and r["exception"] for r in self.records))

    async def test_cancelled_adapter_future_accepts_late_result(self):
        future = self.transport.send(Command("StandUp"), wait_response=True)
        request_id = self.last_id()
        future.cancel()
        self.transport._on_message(self.reply(request_id, api=1004))
        self.assertFalse(self.transport.pending)

    async def test_robot_errors_are_preserved_in_structured_logs(self):
        message = {"type": "add_error", "data": [1710000000, 100, 12]}
        self.transport._on_message(json.dumps(message))
        record = self.records[-1]
        self.assertEqual(record["extra"]["event"], "robot_error")
        self.assertEqual(record["extra"]["response"], message)
        self.assertEqual(record["level"].name, "WARNING")

    async def test_video_disable_failure_does_not_skip_disconnect_or_pending_cleanup(self):
        self.transport.connection = SimpleNamespace(video=SimpleNamespace(
            switchVideoChannel=Mock(side_effect=ConnectionError("Canal fechado"))), disconnect=AsyncMock())
        self.channel.remove_listener = Mock()
        future = self.transport.send(Command("StandUp"), wait_response=True)
        await self.transport.disconnect()
        self.transport.connection.disconnect.assert_awaited_once()
        self.assertFalse(self.transport.pending)
        self.assertIsNone(await future)


if __name__ == "__main__":
    unittest.main()
