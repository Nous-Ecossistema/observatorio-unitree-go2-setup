"""Testes reais de Tk quando há display; são ignorados em CI sem Tk/display."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("DISPLAY"):
            raise unittest.SkipTest("Teste de Tk requer DISPLAY (ex.: xvfb-run)")
        try:
            import tkinter as tk
            from foto_webrtc.gui import JanelaCamera
        except ImportError as error:
            raise unittest.SkipTest(f"Tkinter indisponível: {error}")
        try:
            probe = tk.Tk()
            probe.destroy()
        except tk.TclError as error:
            raise unittest.SkipTest(f"Display indisponível: {error}")
        cls.Window = JanelaCamera

    def setUp(self):
        self.window = self.Window()
        self.addCleanup(self.window.destruir)

    def test_autorepeat_does_not_toggle_video_repeatedly(self):
        action = Mock()
        self.window.on_action = action
        event = SimpleNamespace(keysym="v")
        self.window._pressionar(event)
        self.window._soltar(event)
        self.window._pressionar(event)
        action.assert_called_once_with("Video")

    def test_focus_loss_clears_keys_and_requests_stop(self):
        self.window.on_action = Mock()
        self.window._pressionar(SimpleNamespace(keysym="w"))
        self.window._perder_foco(None)
        self.assertFalse(self.window.pressionadas)
        self.window.on_action.assert_called_once_with("Parar")

    def test_close_is_idempotent(self):
        self.window.on_close = Mock()
        self.window.fechar()
        self.window.fechar()
        self.window.on_close.assert_called_once_with()

    def test_real_tk_with_workers_photo_and_video(self):
        from foto_webrtc.application import Application
        from foto_webrtc.config import Config
        from test_concurrency import FakeTransport

        FakeTransport.instances = []
        FakeTransport.connect_delay = 0.01
        FakeTransport.rejected_move = False
        with tempfile.TemporaryDirectory() as temp:
            config = Config(output_dir=Path(temp), posture_delays=(("StandUp", 0.01),))
            app = Application(config, self.window, FakeTransport)
            heartbeats = []
            for ms in (100, 200, 300, 400, 500):
                self.window.root.after(ms, lambda: heartbeats.append(True))
            self.window.root.after(100, lambda: app.action("StandUp"))
            self.window.root.after(200, lambda: app.action("Foto"))
            self.window.root.after(250, lambda: app.action("Video"))
            self.window.root.after(550, self.window.fechar)
            app.run()
            self.assertEqual(len(heartbeats), 5)
            self.assertTrue(list(Path(temp).glob("fotos/*.jpg")))
            self.assertTrue(list(Path(temp).glob("videos/*.mp4")))
            self.assertIsNotNone(self.window.imagem)
            self.assertFalse(any(worker.is_alive() for worker in app.workers))
