import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from usage.integration import UUID, install_gnome


class IntegrationTests(unittest.TestCase):
    def test_install_reinstall_remove_preserve_other_extensions_and_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            history = root / "history.sqlite3"
            history.write_bytes(b"retained")
            settings = {"enabled-extensions": ["existing@example"], "disabled-extensions": [UUID]}

            def command(*args):
                if args[0] == "gnome-shell":
                    return "GNOME Shell 50.1\n"
                if args[:2] == ("gsettings", "get"):
                    return repr(settings[args[3]])
                if args[:2] == ("gsettings", "set"):
                    settings[args[3]] = ast.literal_eval(args[4])
                return ""

            with (
                patch("sys.platform", "linux"),
                patch.dict(os.environ, {"XDG_DATA_HOME": str(root), "XDG_CONFIG_HOME": str(root)}),
                patch("usage.integration.command", side_effect=command),
            ):
                install_gnome()
                install_gnome()
                self.assertEqual(settings["enabled-extensions"], ["existing@example", UUID])
                self.assertEqual(settings["disabled-extensions"], [])
                extension = root / "gnome-shell/extensions" / UUID
                self.assertTrue((extension / "extension.js").is_file())
                self.assertIn("-m", (root / "autostart/agent-usage.desktop").read_text())
                install_gnome(remove=True)
                self.assertEqual(settings["enabled-extensions"], ["existing@example"])
                self.assertFalse(extension.exists())
                self.assertFalse((root / "autostart/agent-usage.desktop").exists())
                self.assertEqual(history.read_bytes(), b"retained")

    def test_unsupported_platform_or_shell_does_not_install(self):
        with patch("sys.platform", "win32"):
            with self.assertRaises(RuntimeError):
                install_gnome()
        with (
            patch("sys.platform", "linux"),
            patch("usage.integration.command", return_value="GNOME Shell 49.0") as command,
        ):
            with self.assertRaises(RuntimeError):
                install_gnome()
            command.assert_called_once_with("gnome-shell", "--version")
