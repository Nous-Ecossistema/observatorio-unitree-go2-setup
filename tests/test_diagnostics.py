"""O diagnóstico consulta modo/estado sem publicar postura ou movimento."""

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from foto_webrtc.diagnostico_api import query_diagnostics, query_lowstate


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def mode_reply(self, name="mcf", code=0):
        return {"data": {"header": {"status": {"code": code}}, "data": {"name": name}}}

    async def test_only_mode_and_state_queries_are_sent(self):
        pub_sub = SimpleNamespace(publish_request_new=AsyncMock(side_effect=[
            self.mode_reply(), {"data": {"data": '{"state":0,"bodyHeight":0.1}'}}]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await query_diagnostics(pub_sub)
        calls = pub_sub.publish_request_new.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args, ("rt/api/motion_switcher/request", {"api_id": 1001}))
        self.assertEqual(calls[1].args, ("rt/api/sport/request",
            {"api_id": 1034, "parameter": ["state", "bodyHeight", "speedLevel", "gait"]}))
        self.assertIn("Estado informado pelo robô", output.getvalue())

    async def test_unknown_or_rejected_mode_does_not_query_sport(self):
        for response in (self.mode_reply("ai"), self.mode_reply(code=-1), {}):
            with self.subTest(response=response):
                pub_sub = SimpleNamespace(publish_request_new=AsyncMock(return_value=response))
                with contextlib.redirect_stdout(io.StringIO()):
                    await query_diagnostics(pub_sub)
                self.assertEqual(pub_sub.publish_request_new.call_count, 1)

    async def test_empty_state_payload_is_not_reported_as_confirmed_posture(self):
        pub_sub = SimpleNamespace(publish_request_new=AsyncMock(side_effect=[
            self.mode_reply(), {"data": {"data": ""}}]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await query_diagnostics(pub_sub)
        self.assertIn("postura física não foi confirmada", output.getvalue())

    async def test_motor_temperature_subscription_is_read_only_and_always_removed(self):
        pub_sub = SimpleNamespace(subscribe=Mock(side_effect=lambda topic, receive: receive({"data": {
            "motor_state": [{"temperature": 78, "lost": 0, "reserve": [16]}], "bms_state": {"soc": 70}}})),
            unsubscribe=Mock())
        with contextlib.redirect_stdout(io.StringIO()):
            report = await query_lowstate(pub_sub)
        self.assertEqual(report["motores"][0]["temperatura"], 78)
        self.assertEqual(report["bateria_percentual"], 70)
        pub_sub.unsubscribe.assert_called_once_with("rt/lf/lowstate")

    async def test_absent_motor_telemetry_is_explicit_and_unsubscribed(self):
        pub_sub = SimpleNamespace(subscribe=Mock(), unsubscribe=Mock())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            report = await query_lowstate(pub_sub, timeout=0.001)
        self.assertIsNone(report)
        self.assertIn("temperaturas não confirmadas", output.getvalue())
        pub_sub.unsubscribe.assert_called_once()
