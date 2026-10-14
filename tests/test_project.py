"""Contratos de empacotamento/CLI; não conectam ao robô nem abrem uma GUI."""

import contextlib
import importlib
import io
import sys
import unittest
from unittest.mock import patch


class ProjectTests(unittest.TestCase):
    def test_import_does_not_open_gui_or_connect(self):
        with patch("tkinter.Tk", side_effect=AssertionError("GUI durante import")):
            module = importlib.import_module("foto_webrtc.teste_video")
        self.assertTrue(callable(module.cli))

    def test_help_does_not_open_gui(self):
        from foto_webrtc.teste_video import cli

        output = io.StringIO()
        with patch.object(sys, "argv", ["go2-env", "--help"]), patch(
            "tkinter.Tk", side_effect=AssertionError("GUI durante --help")
        ), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            cli()
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--testar-janela", output.getvalue())

    def test_installed_entrypoint_delegates_to_existing_cli(self):
        from go2_env import main

        with patch("foto_webrtc.teste_video.cli") as cli:
            main()
        cli.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
