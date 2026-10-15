import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from usage.integration import UUID, install_gnome, install_windows_shortcut, launch_command


class IntegrationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows shortcuts require the native shell")
    def test_start_menu_shortcut_preserves_arguments_and_updates_after_moving(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "custom settings $(example).toml"
            with patch("PySide6.QtCore.QStandardPaths.writableLocation", return_value=str(root)):
                shortcut = install_windows_shortcut(config)
                install_windows_shortcut(config)
                self.assertEqual(list(root.glob("*.lnk")), [shortcut])
                args = launch_command(config)
                powershell = (
                    Path(os.environ["SystemRoot"])
                    / "System32/WindowsPowerShell/v1.0/powershell.exe"
                )

                def read_shortcut():
                    result = subprocess.run(
                        [
                            str(powershell),
                            "-NoProfile",
                            "-NonInteractive",
                            "-Command",
                            "$link=(New-Object -ComObject WScript.Shell).CreateShortcut($env:AGENT_USAGE_TEST_LINK);"
                            "$link | Select-Object TargetPath,Arguments,WorkingDirectory | ConvertTo-Json",
                        ],
                        env={**os.environ, "AGENT_USAGE_TEST_LINK": str(shortcut)},
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    return json.loads(result.stdout)

                actual = read_shortcut()
                self.assertEqual(Path(actual["TargetPath"]), Path(args[0]))
                self.assertEqual(actual["Arguments"], subprocess.list2cmdline(args[1:]))
                self.assertEqual(Path(actual["WorkingDirectory"]), Path(args[0]).parent)
                moved = root / "new folder" / "agent-usage.exe"
                with patch("usage.integration.launch_command", return_value=[str(moved)]):
                    install_windows_shortcut()
                actual = read_shortcut()
                self.assertEqual(Path(actual["TargetPath"]), moved)
                self.assertEqual(actual["Arguments"], "")

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
