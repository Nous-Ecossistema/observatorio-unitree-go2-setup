"""Testes reais de Tk quando há display; são ignorados em CI sem Tk/display."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


class KeyboardTests(unittest.TestCase):
    """Timers Tcl reais, sem criar janela nem exigir servidor gráfico."""

    def setUp(self):
        import tkinter as tk
        from foto_webrtc.gui import JanelaCamera

        self.window = JanelaCamera.__new__(JanelaCamera)
        self.window.root = tk.Tcl()
        self.window.pressionadas = set()
        self.window.solturas = {}
        self.window.fechada = False
        self.window.drive_enabled = False
        self.window.on_action = Mock()
        self.addCleanup(self.cleanup_timers)

    def cleanup_timers(self):
        for timer in self.window.solturas.values():
            self.window.root.after_cancel(timer)

    def test_idle_between_repeat_release_and_press_does_not_repeat_posture(self):
        event = SimpleNamespace(keysym="h")
        self.window._pressionar(event)
        for _ in range(5):
            self.window._soltar(event)
            self.window.root.update_idletasks()
            self.window._pressionar(event)
        self.window.on_action.assert_called_once_with("Hello")
        self.assertFalse(self.window.solturas)

    def test_arrow_release_waits_for_repeat_and_preserves_other_held_key(self):
        for key in ("Up", "w"):
            self.window._pressionar(SimpleNamespace(keysym=key))
        self.window._soltar(SimpleNamespace(keysym="Up"))
        self.window.root.update_idletasks()
        self.assertEqual(self.window.pressionadas, {"up", "w"})
        time.sleep(0.06)
        self.window.root.update()
        self.assertEqual(self.window.pressionadas, {"w"})

    def test_physical_release_allows_next_explicit_posture(self):
        event = SimpleNamespace(keysym="u")
        self.window._pressionar(event)
        self.window._soltar(event)
        time.sleep(0.06)
        self.window.root.update()
        self.window._pressionar(event)
        self.assertEqual(self.window.on_action.call_count, 2)

    def test_state_indicator_and_buttons_follow_locomotion_gate(self):
        import tkinter as tk
        from foto_webrtc.models import RobotState, VideoState

        for field in ("connection", "status", "media_status", "movement"):
            setattr(self.window, field, tk.StringVar(master=self.window.root))
        self.window.movement_label = Mock()
        self.window.robot_buttons = [Mock()]
        self.window.turn_buttons = [Mock(), Mock()]
        for connected, busy, allowed, text in ((False, False, False, "SEM CONEXÃO"),
                (True, False, False, "LEVANTAR"), (True, True, False, "AÇÃO EM ANDAMENTO"),
                (True, False, True, "PRONTO PARA ANDAR")):
            self.window.atualizar_estado(RobotState(connected=connected, busy=busy, allowed=allowed), "OK", VideoState())
            self.assertIn(text, self.window.movement.get())
            self.window.robot_buttons[0].configure.assert_called_with(
                state="normal" if connected and not busy else "disabled")
            for button in self.window.turn_buttons:
                button.configure.assert_called_with(state="normal" if connected and allowed and not busy else "disabled")

    def test_mouse_turn_is_blocked_until_ready_and_stops_on_release_or_leave(self):
        self.window._iniciar_giro("turn_left")
        self.assertFalse(self.window.pressionadas)
        self.window.drive_enabled = True
        self.window._iniciar_giro("turn_left")
        self.assertEqual(self.window.pressionadas, {"turn_left"})
        self.window._parar_giro("turn_left")
        self.assertFalse(self.window.pressionadas)
        self.window._iniciar_giro("turn_right")
        self.window._perder_foco(None)
        self.assertFalse(self.window.pressionadas)
        self.window.on_action.assert_called_once_with("Parar")

    def test_releasing_mouse_turn_preserves_held_keyboard_and_stop_clears_both(self):
        self.window.drive_enabled = True
        self.window._pressionar(SimpleNamespace(keysym="Up"))
        self.window._iniciar_giro("turn_right")
        self.window._parar_giro("turn_right")
        self.assertEqual(self.window.pressionadas, {"up"})
        self.window._iniciar_giro("turn_right")
        self.window._acionar("Parar")
        self.assertFalse(self.window.pressionadas)
        self.window.on_action.assert_called_once_with("Parar")

    def test_turn_gain_rejects_invalid_values(self):
        from foto_webrtc.models import InputSnapshot

        for gain in (0, -0.1, 1.1, float("nan"), float("inf")):
            with self.subTest(gain=gain), self.assertRaises(ValueError):
                InputSnapshot(angular_gain=gain)

    def test_motor_fault_indicator_overrides_ready_and_disables_posture_buttons(self):
        import tkinter as tk
        from foto_webrtc.models import RobotState, VideoState
        from foto_webrtc.faults import RobotFault

        for field in ("connection", "status", "media_status", "movement"):
            setattr(self.window, field, tk.StringVar(master=self.window.root))
        self.window.movement_label = Mock()
        self.window.mode_button = Mock()
        self.window.robot_buttons = [Mock(), self.window.mode_button]
        self.window.turn_buttons = [Mock()]
        self.window.pressionadas.add("turn_left")
        state = RobotState(connected=True, allowed=True, faults=(RobotFault(123, 309, 4),))
        self.window.atualizar_estado(state, "OK", VideoState())
        self.assertEqual(self.window.movement.get(), "FALHA ATIVA NO ROBÔ")
        self.assertFalse(self.window.drive_enabled)
        self.assertFalse(self.window.pressionadas)
        self.window.robot_buttons[0].configure.assert_called_with(state="disabled")
        self.window.mode_button.configure.assert_called_with(state="normal")


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

    def test_arrow_and_letter_are_held_and_released_independently(self):
        for keysym in ("Up", "w"):
            self.window._pressionar(SimpleNamespace(keysym=keysym))
        self.assertEqual(self.window.pressionadas, {"up", "w"})
        self.window._soltar(SimpleNamespace(keysym="Up"))
        self.window.root.after(60, self.window.root.quit)
        self.window.root.mainloop()
        self.assertEqual(self.window.pressionadas, {"w"})

    def test_posture_clears_all_movement_keys_including_arrows(self):
        self.window.on_action = Mock()
        for keysym in ("Up", "Down", "Left", "Right", "w", "q"):
            self.window._pressionar(SimpleNamespace(keysym=keysym))
        self.window._pressionar(SimpleNamespace(keysym="u"))
        self.assertEqual(self.window.pressionadas, {"u"})
        self.window.on_action.assert_called_once_with("StandUp")

    def test_close_is_idempotent(self):
        self.window.on_close = Mock()
        self.window.fechar()
        self.window.fechar()
        self.window.on_close.assert_called_once_with()

    def test_reconnect_button_is_only_enabled_offline(self):
        from foto_webrtc.models import ConnectionState

        for state in ConnectionState:
            self.window.set_connection_state(state)
            expected = "normal" if state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR) else "disabled"
            self.assertEqual(str(self.window.reconnect_button["state"]), expected)

    def test_f5_requests_reconnect_once_during_autorepeat(self):
        self.window.on_action = Mock()
        event = SimpleNamespace(keysym="F5")
        self.window._pressionar(event)
        self.window._soltar(event)
        self.window._pressionar(event)
        self.window.on_action.assert_called_once_with("Reconectar")

    def test_camera_viewport_does_not_push_controls_when_image_changes(self):
        from foto_webrtc.models import Preview

        self.window.root.update()
        initial = self.window.preview.winfo_width(), self.window.preview.winfo_height()
        ppm = b"P6\n1920 1080\n255\n" + bytes([100, 110, 120]) * (1920 * 1080)
        self.window.mostrar(Preview(ppm, time.monotonic()))
        self.window.root.update()
        self.assertEqual((self.window.preview.winfo_width(), self.window.preview.winfo_height()), initial)
        self.assertTrue(self.window.reconnect_button.winfo_viewable())
        self.assertEqual(self.window.preview.coords(self.window.preview_image),
                         [initial[0] / 2, initial[1] / 2])

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
            self.assertTrue(list(Path(temp).glob("fotos/originais/*.jpg")))
            self.assertTrue(list(Path(temp).glob("videos/*.mp4")))
            self.assertIsNotNone(self.window.imagem)
            self.assertFalse(any(worker.is_alive() for worker in app.workers))

    def test_captured_photo_opens_updates_and_reopens_without_replacing_live_preview(self):
        from foto_webrtc.models import PhotoResult

        ppm = b"P6\n80 60\n255\n" + bytes([100, 110, 120]) * (80 * 60)
        photo = PhotoResult(ppm, Path("foto_corrigida.jpg"), Path("original.png"))
        self.window.mostrar_foto(photo)
        self.window.root.update()
        popup = self.window.photo_window
        self.assertEqual(popup.title(), "Go2 - Foto corrigida")
        self.assertIsNone(self.window.imagem)
        self.assertIn("original.png", self.window.photo_path["text"])
        self.window.mostrar_foto(PhotoResult(ppm, Path("segunda.jpg")))
        self.assertIs(self.window.photo_window, popup)
        self.assertIn("segunda.jpg", self.window.photo_path["text"])
        popup.destroy()
        self.window.mostrar_foto(photo)
        self.assertTrue(self.window.photo_window.winfo_exists())
