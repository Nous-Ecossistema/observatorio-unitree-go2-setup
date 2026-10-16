"""Offline, reconexão e ciclo de vida com a GUI simulada, sem acessar um Go2."""

import asyncio
import threading
import time
import unittest

from loguru import logger
from unitree_webrtc_connect.unitree_auth import LocalSignalingPortError

from foto_webrtc.application import Application
from foto_webrtc.config import Config
from foto_webrtc.models import Command, ConnectionState
from foto_webrtc.transport import is_recoverable_connection_error
from test_concurrency import FakeTransport, FakeWindow


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        sink = logger.add(lambda message: self.records.append(message.record), level="DEBUG")
        self.addCleanup(logger.remove, sink)
        self.window = FakeWindow()
        self.statuses = []
        self.window.set_connection_state = self.statuses.append
        self.config = Config(request_timeout=0.05, connect_timeout=0.2, mode_settle=0,
                             disconnect_timeout=0.1, posture_delays=(("StandUp", 0.005),))

    def transport(self, outcomes, delay=0.01):
        class Transport(FakeTransport):
            instances = []
            rejected_move = False
            connect_delay = delay

            def __init__(self, *args):
                super().__init__(*args)
                self.outcome = outcomes[len(self.instances) - 1]
                self.drop = False
                self.owners = []

            async def connect(self):
                self.owners.append(threading.get_ident())
                if self.outcome == "blocked":
                    try:
                        await asyncio.Future()
                    finally:
                        self.cancelled = True
                elif isinstance(self.outcome, Exception):
                    await asyncio.sleep(delay)
                    raise self.outcome
                else:
                    await super().connect()

            def tick(self):
                if self.drop:
                    raise ConnectionError("Canal WebRTC fechado")

            async def disconnect(self):
                self.owners.append(threading.get_ident())
                await super().disconnect()

        return Transport

    def when(self, condition, callback, timeout=1.5):
        deadline = time.monotonic() + timeout

        def poll():
            if condition():
                callback()
            elif time.monotonic() > deadline:
                self.fail("A condição do worker não ocorreu no prazo")
            else:
                self.window.root.after(10, poll)

        self.window.root.after(10, poll)

    def run_app(self, app):
        self.window.root.after(2500, self.window.on_close)  # proteção contra deadlock no teste
        app.run()
        self.assertTrue(app.finished.is_set())
        self.assertFalse(any(worker.is_alive() for worker in app.workers))
        self.assertTrue(all(worker.failure is None for worker in app.workers))

    def test_signaling_failure_keeps_gui_and_threads_alive_and_blocks_commands(self):
        transport = self.transport([LocalSignalingPortError("192.168.12.1")])
        app = Application(self.config, self.window, transport)
        heartbeats = []

        def offline():
            self.assertFalse(app.stop_event.is_set())
            self.assertTrue(all(worker.is_alive() for worker in app.workers))
            self.assertFalse(app.robot.ready.is_set())
            self.assertFalse(app.action("StandUp"))
            self.window.pressionadas.add("w")
            self.window.root.after(70, verify)

        def verify():
            self.assertEqual(len(transport.instances), 1)  # sem reconexão automática
            self.assertFalse(transport.instances[0].calls)
            self.assertTrue(transport.instances[0].disconnected)
            self.assertFalse(app.robot.state.allowed)
            self.assertFalse(app.stop_event.is_set())
            self.window.on_close()

        for ms in (20, 40, 60):
            self.window.root.after(ms, lambda: heartbeats.append(threading.get_ident()))
        self.when(lambda: app.robot.state.connection_state == ConnectionState.ERROR, offline)
        self.run_app(app)
        self.assertEqual(len(heartbeats), 3)
        self.assertEqual(set(heartbeats), {threading.get_ident()})
        errors = [r["extra"] for r in self.records if r["extra"].get("event") == "connection_recoverable"]
        self.assertEqual(errors[0]["error_type"], "LocalSignalingPortError")
        self.assertIn(ConnectionState.ERROR, self.statuses)

    def test_manual_reconnect_uses_same_workers_and_loop_and_discards_old_actions(self):
        transport = self.transport([ConnectionError("Sem Wi-Fi"), None], delay=0.06)
        app = Application(self.config, self.window, transport)
        identities = []
        loops = []

        def reconnect():
            identities.extend(id(worker) for worker in app.workers)
            loops.append(app.robot.loop)
            app.mailbox.action("RecoveryStand")  # até um produtor que ignora o gate é limpo
            app.mailbox.move(Command.move(0.3, generation=app.mailbox.generation))
            self.assertTrue(app.action("Reconectar"))
            for _ in range(30):
                self.assertFalse(app.action("Reconectar"))
            self.when(lambda: app.robot.ready.is_set() and not app.robot.state.busy, connected)

        def connected():
            self.assertEqual(identities, [id(worker) for worker in app.workers])
            self.assertIs(loops[0], app.robot.loop)
            self.assertEqual(len(transport.instances), 2)
            self.assertFalse(app.robot.state.allowed)
            self.assertFalse(app.action("Reconectar"))
            self.assertEqual([command.name for command, _ in transport.instances[1].calls], ["Consultar"])
            self.assertEqual(len([t for t in threading.enumerate() if t.name == "go2-rtc"]), 1)
            self.window.on_close()

        self.when(lambda: app.robot.state.connection_state == ConnectionState.ERROR, reconnect)
        self.run_app(app)
        self.assertTrue(all(t.disconnected for t in transport.instances))
        self.assertEqual(len({owner for t in transport.instances for owner in t.owners}), 1)
        self.assertIn(ConnectionState.CONNECTING, self.statuses)

    def test_disconnect_during_movement_goes_offline_then_requires_new_posture(self):
        transport = self.transport([None, None])
        app = Application(self.config, self.window, transport)

        def stand():
            app.action("StandUp")
            self.when(lambda: app.robot.state.allowed, drive)

        def drive():
            self.window.pressionadas.add("w")
            self.when(lambda: any(c.name == "Move" and any(c.parameter.values())
                                 for c, _ in transport.instances[0].calls), drop)

        def drop():
            transport.instances[0].drop = True
            self.when(lambda: app.robot.state.connection_state == ConnectionState.DISCONNECTED
                      and not app.robot._attempt_active, offline)

        def offline():
            self.assertFalse(app.stop_event.is_set())
            self.assertFalse(app.robot.state.allowed)
            self.assertFalse(app.robot.ready.is_set())
            self.assertFalse(app.action("RecoveryStand"))
            self.assertTrue(app.action("Reconectar"))
            self.when(lambda: len(transport.instances) == 2 and app.robot.ready.is_set()
                      and not app.robot.state.busy, reconnected)

        def reconnected():
            self.assertFalse(app.robot.state.allowed)
            self.assertFalse(any(c.name == "Move" and any(c.parameter.values())
                                 for c, _ in transport.instances[1].calls))
            self.assertTrue(app.action("StandUp"))
            self.when(lambda: app.robot.state.allowed, self.window.on_close)

        self.when(lambda: app.robot.ready.is_set() and not app.robot.state.busy, stand)
        self.run_app(app)
        self.assertTrue(all(t.disconnected for t in transport.instances))

    def test_shutdown_cancels_blocked_reconnect_and_pending_video_read(self):
        transport = self.transport([ConnectionError("Offline"), "blocked"])
        app = Application(self.config, self.window, transport)

        def retry():
            self.assertTrue(app.action("Reconectar"))
            self.when(lambda: len(transport.instances) == 2, self.window.on_close)

        self.when(lambda: app.robot.state.connection_state == ConnectionState.ERROR, retry)
        self.run_app(app)
        self.assertTrue(transport.instances[1].cancelled)
        self.assertTrue(all(t.disconnected for t in transport.instances))
        self.assertFalse(app.robot.request_reconnect())
        self.assertFalse(app.robot._receive_tasks)

    def test_blocked_video_read_is_cancelled_on_disconnect_without_killing_capture(self):
        from foto_webrtc.channels import put_latest

        transport = self.transport([None])
        receiving = threading.Event()
        cancelled = threading.Event()

        class Track:
            async def recv(self):
                receiving.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

        async def connect(instance):
            put_latest(instance.tracks, Track())

        transport.connect = connect
        app = Application(self.config, self.window, transport)

        def drop():
            transport.instances[0].drop = True
            self.when(lambda: app.robot.state.connection_state == ConnectionState.DISCONNECTED
                      and cancelled.is_set(), offline)

        def offline():
            self.assertTrue(app.workers[1].is_alive())
            self.assertFalse(app.stop_event.is_set())
            self.assertFalse(app.robot._receive_tasks)
            self.window.on_close()

        self.when(receiving.is_set, drop)
        self.run_app(app)

    def test_repeated_failed_manual_reconnects_do_not_accumulate_workers(self):
        transport = self.transport([TimeoutError("Sem resposta")] * 3)
        app = Application(self.config, self.window, transport)

        def offline():
            self.assertEqual(len([t for t in threading.enumerate() if t.name == "go2-rtc"]), 1)
            if len(transport.instances) == 3:
                self.window.on_close()
                return
            self.assertTrue(app.action("Reconectar"))
            count = len(transport.instances)
            self.when(lambda: len(transport.instances) > count
                      and app.robot.state.connection_state == ConnectionState.ERROR, offline)

        self.when(lambda: app.robot.state.connection_state == ConnectionState.ERROR, offline)
        self.run_app(app)
        self.assertTrue(all(t.disconnected for t in transport.instances))

    def test_recoverable_disconnect_error_still_allows_offline_shutdown(self):
        transport = self.transport([LocalSignalingPortError("192.168.12.1")])

        async def disconnect(instance):
            instance.disconnected = True
            raise TimeoutError("Fechamento expirou")

        transport.disconnect = disconnect
        app = Application(self.config, self.window, transport)
        self.when(lambda: app.robot.state.connection_state == ConnectionState.ERROR, self.window.on_close)
        self.run_app(app)

    def test_classifier_does_not_hide_critical_errors(self):
        for error in (LocalSignalingPortError("192.168.12.1"), ConnectionError(), TimeoutError(), OSError()):
            self.assertTrue(is_recoverable_connection_error(error))
        for error in (RuntimeError("bug"), ValueError(), ImportError(), FileNotFoundError(), PermissionError()):
            self.assertFalse(is_recoverable_connection_error(error))

    def test_recoverable_send_failure_goes_offline_without_stopping_gui(self):
        transport = self.transport([None])

        def send(instance, command, wait_response=False):
            result = FakeTransport.send(instance, command, wait_response)
            if command.name == "Move" and any(command.parameter.values()):
                raise OSError("Falha SCTP")
            return result

        transport.send = send
        app = Application(self.config, self.window, transport)

        def stand():
            app.action("StandUp")
            self.when(lambda: app.robot.state.allowed, drive)

        def drive():
            self.window.pressionadas.add("w")
            self.when(lambda: app.robot.state.connection_state == ConnectionState.DISCONNECTED
                      and not app.robot._attempt_active, self.window.on_close)

        self.when(lambda: app.robot.ready.is_set() and not app.robot.state.busy, stand)
        self.run_app(app)
        self.assertTrue(transport.instances[0].disconnected)

    def test_critical_posture_error_still_stops_and_closes_application(self):
        transport = self.transport([None])

        def send(instance, command, wait_response=False):
            if command.name == "StandUp":
                raise RuntimeError("Bug na implementação do comando")
            return FakeTransport.send(instance, command, wait_response)

        transport.send = send
        app = Application(self.config, self.window, transport)
        self.when(lambda: app.robot.ready.is_set() and not app.robot.state.busy,
                  lambda: app.action("StandUp"))
        self.window.root.after(2500, self.window.on_close)
        with self.assertRaisesRegex(RuntimeError, "go2-rtc"):
            app.run()
        self.assertIsInstance(app.robot.failure, RuntimeError)
        self.assertTrue(transport.instances[0].disconnected)
        self.assertFalse(any(worker.is_alive() for worker in app.workers))

    def test_video_track_negotiated_before_connection_ready_is_not_lost(self):
        from foto_webrtc.channels import put_latest
        from test_concurrency import FakeTrack

        transport = self.transport([None])

        async def connect(instance):
            put_latest(instance.tracks, FakeTrack())
            await asyncio.sleep(0.15)  # maior que o wait() da captura

        transport.connect = connect
        app = Application(self.config, self.window, transport)
        self.when(lambda: bool(self.window.preview_threads), self.window.on_close)
        self.run_app(app)
