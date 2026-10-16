"""Filas, watchdog e afinidade de threads usando o mesmo Facade da GUI."""

import asyncio
import heapq
import threading
import time
import unittest
from queue import Queue
from types import SimpleNamespace

import av
import numpy as np

from foto_webrtc.application import Application
from foto_webrtc.channels import CommandMailbox, get_latest, put_latest
from foto_webrtc.commands import LocomotionWorker
from foto_webrtc.config import Config
from foto_webrtc.models import Command, InputSnapshot, RobotState


class FakeRoot:
    def __init__(self):
        self.callbacks = []
        self.sequence = 0
        self.running = True

    def after(self, ms, callback):
        self.sequence += 1
        heapq.heappush(self.callbacks, (time.monotonic() + ms / 1000, self.sequence, callback))

    def mainloop(self):
        while self.running:
            if self.callbacks and self.callbacks[0][0] <= time.monotonic():
                _, _, callback = heapq.heappop(self.callbacks)
                callback()
            else:
                time.sleep(0.001)

    def quit(self):
        self.running = False


class FakeWindow:
    def __init__(self):
        self.root = FakeRoot()
        self.pressionadas = set()
        self.status = SimpleNamespace(set=lambda value: None)
        self.preview = SimpleNamespace(winfo_width=lambda: 80, winfo_height=lambda: 60)
        self.preview_threads = []

    def mostrar(self, preview):
        self.preview_threads.append(threading.get_ident())


class FakeTrack:
    async def recv(self):
        await asyncio.sleep(0.005)
        return av.VideoFrame.from_ndarray(np.zeros((60, 80, 3), dtype=np.uint8), format="bgr24")


class FakeTransport:
    instances = []
    connect_delay = 0.01
    rejected_move = False

    def __init__(self, config, tracks, failure):
        self.calls = []
        self.tracks, self.failure = tracks, failure
        self.disconnected = False
        self.instances.append(self)

    async def connect(self):
        await asyncio.sleep(self.connect_delay)
        put_latest(self.tracks, FakeTrack())

    def set_motion_mode(self, mode):
        self.motion_mode = mode

    def send(self, command, wait_response=False):
        self.calls.append((command, threading.current_thread().name))
        if command.name == "Move" and any(command.parameter.values()) and self.rejected_move:
            self.failure(command, "rejected")
        if wait_response:
            future = asyncio.get_running_loop().create_future()
            response = {"data": {"data": {"name": "normal"}, "header": {"status": {"code": 0}}}}
            asyncio.get_running_loop().call_later(0.01, lambda: None if future.done() else future.set_result(response))
            return future

    def tick(self):
        pass

    async def disconnect(self):
        self.disconnected = True


class QueueTests(unittest.TestCase):
    def test_stop_precedes_action_and_invalidates_old_movement(self):
        bus = CommandMailbox()
        old = Command.move(0.3, generation=bus.generation)
        bus.move(old)
        bus.action("StandDown")
        self.assertEqual(bus.take().parameter, {"x": 0.0, "y": 0.0, "z": 0.0})
        self.assertEqual(bus.take().name, "StandDown")
        self.assertIsNone(bus.take())
        self.assertFalse(bus.move(old))
        self.assertFalse(bus.dispatch_move(old, lambda command: self.fail("movimento antigo enviado")))

    def test_latest_queue_has_bounded_memory(self):
        queue = Queue(1)
        for i in range(10000):
            put_latest(queue, i)
        self.assertEqual(queue.qsize(), 1)
        self.assertEqual(get_latest(queue), 9999)

    def test_input_watchdog_stops_without_new_gui_heartbeat(self):
        config = Config(publish_hz=100, input_timeout=0.04)
        stop, inputs, states, bus = threading.Event(), Queue(1), Queue(1), CommandMailbox()
        put_latest(inputs, InputSnapshot(frozenset("waq")))
        put_latest(states, RobotState(connected=True, allowed=True))
        worker = LocomotionWorker(config, stop, inputs, states, bus)
        worker.start()
        try:
            deadline = time.monotonic() + 1
            moving = None
            while time.monotonic() < deadline:
                command = bus.take()
                if command and any(command.parameter.values()):
                    moving = command
                    break
                time.sleep(0.002)
            self.assertIsNotNone(moving)
            self.assertEqual(moving.parameter, {"x": 0.3, "y": 0.3, "z": 0.5})
            time.sleep(0.07)
            self.assertEqual(bus.take().parameter, {"x": 0.0, "y": 0.0, "z": 0.0})
        finally:
            stop.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_arrow_keys_move_and_aliases_do_not_double_speed(self):
        config = Config(publish_hz=100)
        for keys, expected in [
            ({"up", "w"}, (0.3, 0, 0)), ({"down"}, (-0.3, 0, 0)),
            ({"left", "a"}, (0, 0.3, 0)), ({"right"}, (0, -0.3, 0)),
            ({"up", "right", "q"}, (0.3, -0.3, 0.5)),
        ]:
            with self.subTest(keys=keys):
                stop, inputs, states, bus = threading.Event(), Queue(1), Queue(1), CommandMailbox()
                put_latest(inputs, InputSnapshot(frozenset(keys)))
                put_latest(states, RobotState(connected=True, allowed=True))
                worker = LocomotionWorker(config, stop, inputs, states, bus)
                worker.start()
                try:
                    deadline = time.monotonic() + 1
                    command = None
                    while time.monotonic() < deadline:
                        command = bus.take()
                        if command is not None:
                            break
                        time.sleep(0.002)
                    self.assertIsNotNone(command)
                    self.assertEqual(command.parameter, dict(zip(("x", "y", "z"), expected)))
                    put_latest(inputs, InputSnapshot())
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        command = bus.take()
                        if command is not None and not any(command.parameter.values()):
                            break
                        time.sleep(0.002)
                    self.assertIsNotNone(command)
                    self.assertFalse(any(command.parameter.values()))
                finally:
                    stop.set()
                    worker.join(1)
                self.assertFalse(worker.is_alive())


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        FakeTransport.instances = []
        FakeTransport.connect_delay = 0.01
        FakeTransport.rejected_move = False
        self.window = FakeWindow()
        self.config = Config(request_timeout=0.1, mode_settle=0,
                             posture_delays=(("StandUp", 0.01),))

    def test_gui_keeps_running_while_connecting_and_all_workers_join(self):
        FakeTransport.connect_delay = 0.2
        app = Application(self.config, self.window, FakeTransport)
        ticks = []
        for ms in (20, 40, 60, 80):
            self.window.root.after(ms, lambda: ticks.append(threading.get_ident()))
        self.window.root.after(100, self.window.on_close)
        app.run()
        self.assertEqual(len(ticks), 4)
        self.assertEqual(set(ticks), {threading.get_ident()})
        self.assertTrue(FakeTransport.instances[0].disconnected)
        self.assertTrue(app.finished.is_set())
        self.assertFalse(any(worker.is_alive() for worker in app.workers))

    def test_io_is_owned_by_rtc_thread_and_preview_by_main_thread(self):
        app = Application(self.config, self.window, FakeTransport)
        self.window.root.after(60, lambda: app.action("StandUp"))
        self.window.root.after(130, lambda: self.window.pressionadas.add("w"))
        self.window.root.after(230, self.window.on_close)
        app.run()
        transport = FakeTransport.instances[0]
        self.assertTrue(any(c.name == "Move" and c.parameter.get("x") == 0.3 for c, _ in transport.calls))
        self.assertEqual({name for _, name in transport.calls}, {"go2-rtc"})
        self.assertFalse(any(transport.calls[-1][0].parameter.values()))
        self.assertTrue(self.window.preview_threads)
        self.assertEqual(set(self.window.preview_threads), {threading.get_ident()})
        self.assertFalse(any(worker.is_alive() for worker in app.workers))

    def test_move_rejection_suspends_and_prioritizes_zero(self):
        FakeTransport.rejected_move = True
        app = Application(self.config, self.window, FakeTransport)
        self.window.root.after(60, lambda: app.action("StandUp"))
        self.window.root.after(130, lambda: self.window.pressionadas.add("w"))
        self.window.root.after(230, self.window.on_close)
        app.run()
        calls = [command for command, _ in FakeTransport.instances[0].calls]
        nonzero = [i for i, c in enumerate(calls) if c.name == "Move" and any(c.parameter.values())]
        self.assertEqual(len(nonzero), 1)
        self.assertFalse(any(calls[nonzero[0] + 1].parameter.values()))

    def test_worker_exception_stops_application_and_closes_resources(self):
        class BrokenTransport(FakeTransport):
            async def connect(self):
                raise RuntimeError("Erro crítico no transporte")

        app = Application(self.config, self.window, BrokenTransport)
        self.window.root.after(1000, self.window.on_close)
        with self.assertRaisesRegex(RuntimeError, "go2-rtc"):
            app.run()
        self.assertTrue(app.finished.is_set())
        self.assertTrue(BrokenTransport.instances[-1].disconnected)
        self.assertFalse(any(worker.is_alive() for worker in app.workers))
