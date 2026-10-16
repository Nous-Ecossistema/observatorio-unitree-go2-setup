"""Snapshot de boot, deltas e bloqueio de posturas por falhas do robô."""

import unittest

from foto_webrtc.faults import FaultTracker, RobotFault
from foto_webrtc.models import Command, RobotState
import test_posture


class FaultTrackerTests(unittest.TestCase):
    def test_active_motor_codes_match_log_hexadecimal_format(self):
        tracker = FaultTracker()
        tracker.update({"type": "errors", "data": [[123, 306, 16], [123, 309, 4]]})
        self.assertEqual([fault.label for fault in tracker.faults], ["306-10 (0x10)", "309-4 (0x4)"])
        self.assertTrue(all(fault.blocks_motion for fault in tracker.faults))

    def test_snapshots_replace_and_deltas_add_remove_without_duplicates(self):
        tracker = FaultTracker()
        tracker.update({"type": "errors", "data": [[123, 306, 16]]})
        tracker.update({"type": "add_error", "data": [124, 306, 16]})
        self.assertEqual(tracker.faults, (RobotFault(123, 306, 16),))
        tracker.update({"type": "add_error", "data": [124, 309, 4]})
        tracker.update({"type": "rm_error", "data": [125, 306, 16]})
        self.assertEqual(tracker.faults, (RobotFault(124, 309, 4),))
        tracker.update({"type": "errors", "data": []})
        self.assertFalse(tracker.faults)
        self.assertTrue(tracker.seen)

    def test_malformed_snapshot_cannot_clear_active_faults(self):
        tracker = FaultTracker()
        tracker.update({"type": "errors", "data": [[123, 306, 16]]})
        for data in (None, [[123, 309]], [[123, 309, "4"]], [[123, 309, 0]]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                tracker.update({"type": "errors", "data": data})
            self.assertEqual(len(tracker.faults), 1)


class FaultPostureTests(unittest.IsolatedAsyncioTestCase):
    worker = test_posture.PostureTests.worker

    async def test_fault_blocks_recovery_and_even_success_cannot_enable_movement(self):
        worker = self.worker([])
        worker.transport.faults = (RobotFault(123, 306, 16),)
        await worker._posture(Command("RecoveryStand"))
        self.assertFalse(worker.transport.commands)
        self.assertFalse(worker.state.allowed)
        self.assertIn("306-10", worker.state.message)
        worker._state(allowed=True)
        self.assertFalse(worker.state.allowed)

    async def test_faults_clear_without_automatically_resuming_locomotion(self):
        worker = self.worker([])
        worker.transport.faults = (RobotFault(123, 309, 4),)
        worker._sync_faults()
        worker.transport.faults = ()
        worker._sync_faults()
        self.assertFalse(worker.state.allowed)
        self.assertIn("U/R", worker.state.message)

    async def test_read_only_mode_query_remains_available_with_motor_fault(self):
        worker = self.worker([{"data": {"data": '{"name":"mcf"}'}}])
        worker.transport.faults = (RobotFault(123, 309, 4),)
        await worker._posture(Command("Consultar"))
        self.assertEqual([command.name for command in worker.transport.commands], ["Consultar"])
        self.assertEqual(worker.state.mode, "mcf")
        self.assertFalse(worker.state.allowed)
