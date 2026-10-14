"""Preserva a diferença entre ações, consulta e permissão de locomoção."""

import asyncio
import unittest
from queue import Queue
from threading import Event

from foto_webrtc.channels import CommandMailbox
from foto_webrtc.config import Config
from foto_webrtc.models import Command, RobotState
from foto_webrtc.robot import RobotWorker


class PostureTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, responses):
        class Transport:
            def __init__(self):
                self.commands = []

            def send(self, command, wait_response=False):
                self.commands.append(command)
                future = asyncio.get_running_loop().create_future()
                future.set_result(responses.pop(0))
                return future

        config = Config(mode_settle=0, posture_delays=tuple((name, 0) for name in
                        ("StandUp", "StandDown", "RecoveryStand", "Hello", "Damp")))
        worker = RobotWorker(config, Event(), CommandMailbox(), Queue(1), Queue(1), Queue(1))
        worker.transport = Transport()
        worker.state = RobotState(connected=True, allowed=True, mode="normal")
        return worker

    async def test_stand_down_and_damp_suspend_without_followup_mode_switch(self):
        for action in ("StandDown", "Damp"):
            with self.subTest(action=action):
                worker = self.worker([{}])
                await worker._posture(Command(action))
                self.assertFalse(worker.state.allowed)
                self.assertFalse(worker.state.busy)
                self.assertEqual([c.name for c in worker.transport.commands], [action])

    async def test_recovery_is_explicit_and_distinct_from_stand_up(self):
        for action in ("StandUp", "RecoveryStand"):
            with self.subTest(action=action):
                worker = self.worker([{}])
                worker.state = RobotState(connected=True, allowed=False, mode="normal")
                await worker._posture(Command(action))
                self.assertTrue(worker.state.allowed)
                self.assertEqual([c.name for c in worker.transport.commands], [action])

    async def test_hello_preserves_previous_permission(self):
        for allowed in (True, False):
            worker = self.worker([{}])
            worker.state = RobotState(connected=True, allowed=allowed, mode="normal")
            await worker._posture(Command("Hello"))
            self.assertEqual(worker.state.allowed, allowed)

    async def test_uncertain_posture_is_not_retried_or_released(self):
        worker = self.worker([None])
        await worker._posture(Command("StandUp"))
        self.assertFalse(worker.state.allowed)
        self.assertEqual(len(worker.transport.commands), 1)

    async def test_mode_query_keeps_command_identity_and_never_lifts_robot(self):
        worker = self.worker([{"data": {"data": '{"name":"normal"}'}}])
        worker.state = RobotState(connected=True, allowed=False, mode=None)
        command = Command("Consultar")
        await worker._posture(command)
        self.assertEqual(worker.transport.commands, [command])
        self.assertEqual(worker.state.mode, "normal")
        self.assertFalse(worker.state.allowed)

    async def test_mode_change_is_confirmed_before_posture_and_links_auxiliary_commands(self):
        worker = self.worker([{"data": {"data": {"name": "ai"}}}, {},
                              {"data": {"data": {"name": "normal"}}}, {}])
        worker.state = RobotState(connected=True, allowed=False, mode=None)
        command = Command("StandUp")
        await worker._posture(command)
        calls = worker.transport.commands
        self.assertEqual([c.name for c in calls], ["Consultar", "ModoNormal", "Consultar", "StandUp"])
        self.assertEqual({c.parent_command_id for c in calls[:-1]}, {command.command_id})
        self.assertTrue(worker.state.allowed)

    async def test_unconfirmed_mode_does_not_send_posture(self):
        worker = self.worker([None])
        worker.state = RobotState(connected=True, allowed=False, mode=None)
        await worker._posture(Command("StandUp"))
        self.assertEqual([c.name for c in worker.transport.commands], ["Consultar"])
        self.assertFalse(worker.state.allowed)
