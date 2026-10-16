import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from usage.integration import (
    UUID,
    ConnectionUnavailable,
    agent_command,
    claude_terminal_command,
    install_gnome,
    install_windows_shortcut,
    launch_command,
    open_claude_terminal,
    owns_claude_command,
    remove_legacy_claude_statusline,
)


class IntegrationTests(unittest.TestCase):
    def test_retired_capture_migration_preserves_custom_settings_and_backups(self):
        from PySide6.QtCore import QSaveFile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            legacy = "'/old install/agent-usage' --capture-claude"
            custom = {"type": "command", "command": "custom-status"}
            backup = path.with_name(path.name + ".agent-usage.bak")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": folder}):
                remove_legacy_claude_statusline()
                self.assertFalse(path.exists())
                original = json.dumps({"theme": "dark", "statusLine": custom}).encode()
                path.write_bytes(original)
                remove_legacy_claude_statusline()
                self.assertEqual(path.read_bytes(), original)
                backup.write_bytes(original)
                path.write_text(
                    json.dumps(
                        {"theme": "light", "statusLine": {"type": "command", "command": legacy}}
                    )
                )
                before = path.read_bytes()
                with patch("PySide6.QtCore.QSaveFile.commit", return_value=False):
                    with self.assertRaises(OSError):
                        remove_legacy_claude_statusline()
                self.assertEqual(path.read_bytes(), before)
                # Concurrent edits must never be overwritten during migration.
                open_file = QSaveFile.open

                def change(output, mode):
                    path.write_bytes(original)
                    return open_file(output, mode)

                with patch("PySide6.QtCore.QSaveFile.open", new=change):
                    with self.assertRaises(OSError):
                        remove_legacy_claude_statusline()
                self.assertEqual(path.read_bytes(), original)
                path.write_bytes(before)
                remove_legacy_claude_statusline()
                self.assertEqual(
                    json.loads(path.read_text()), {"theme": "light", "statusLine": custom}
                )
                self.assertEqual(backup.read_bytes(), original)
                saved = path.read_bytes()
                remove_legacy_claude_statusline()
                self.assertEqual(path.read_bytes(), saved)
                backup.unlink()
                path.write_bytes(before)
                remove_legacy_claude_statusline()
                self.assertEqual(json.loads(path.read_text()), {"theme": "light"})
            for command in (legacy, "python -m usage --capture-claude"):
                self.assertTrue(owns_claude_command(command))
            for command in ("echo --capture-claude", legacy + " | custom", "custom-status"):
                self.assertFalse(owns_claude_command(command))

    def test_claude_discovery_and_terminal_arguments_are_portable(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with (
                patch("usage.integration.Path.home", return_value=home),
                patch("usage.integration.shutil.which", return_value=None) as which,
            ):
                for system, name in (("linux", "claude"), ("win32", "claude.exe")):
                    with self.subTest(system=system), patch("sys.platform", system):
                        binary = (
                            home
                            / ".vscode/extensions/anthropic.claude-code-2.1.301/resources/native-binary"
                            / name
                        )
                        binary.parent.mkdir(parents=True, exist_ok=True)
                        binary.touch()
                        binary.chmod(0o700)
                        self.assertEqual(agent_command("claude", ["claude"]), [str(binary)])
                        if system == "win32":
                            self.assertEqual(claude_terminal_command(["claude"]), [str(binary)])
                            with (
                                patch("usage.integration.subprocess.Popen") as launch,
                                patch(
                                    "usage.integration.subprocess.CREATE_NEW_CONSOLE",
                                    16,
                                    create=True,
                                ),
                            ):
                                open_claude_terminal([str(binary)])
                                launch.assert_called_once_with(
                                    [str(binary)], cwd=home, creationflags=16
                                )
                        else:
                            with self.assertRaises(ConnectionUnavailable):
                                claude_terminal_command(["claude"])
                            which.side_effect = lambda name: (
                                "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None
                            )
                            override = [str(home / "Claude $data; & binary"), "a b"]
                            args = claude_terminal_command(override)
                            self.assertEqual(args, ["/usr/bin/gnome-terminal", "--", *override])
                            with patch(
                                "PySide6.QtCore.QProcess.startDetached", return_value=(True, 123)
                            ) as launch:
                                open_claude_terminal(args)
                                launch.assert_called_once_with(args[0], args[1:], str(home))
                            which.side_effect = None
                        binary.unlink()
                        with self.assertRaises(ConnectionUnavailable):
                            claude_terminal_command(["claude"])

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
                windowed = Path(args[0]).with_name("pythonw.exe")
                self.assertEqual(
                    Path(actual["TargetPath"]), windowed if windowed.is_file() else Path(args[0])
                )
                self.assertEqual(actual["Arguments"], subprocess.list2cmdline(args[1:]))
                self.assertEqual(Path(actual["WorkingDirectory"]), Path(args[0]).parent)
                moved = root / "new folder" / "agent-usage.exe"
                with (
                    patch("usage.integration.launch_command", return_value=[str(moved)]),
                    patch.object(sys, "frozen", True, create=True),
                ):
                    install_windows_shortcut()
                actual = read_shortcut()
                self.assertEqual(Path(actual["TargetPath"]), moved)
                self.assertEqual(actual["Arguments"], "")

    def test_windowed_python_uses_console_python_for_capture(self):
        with patch("sys.executable", str(Path("venv") / "pythonw.exe")):
            self.assertEqual(Path(launch_command()[0]).name, "python.exe")

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
