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
    claude_command,
    claude_status,
    claude_terminal_command,
    install_claude_statusline,
    install_gnome,
    install_windows_shortcut,
    launch_command,
    open_claude_terminal,
    owns_claude_command,
)


class IntegrationTests(unittest.TestCase):
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

    def test_legacy_capture_is_repaired_only_on_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            legacy = 'cd "/old widget" && /old/widget/python -m usage --capture-claude'
            path.write_text(json.dumps({"statusLine": {"type": "command", "command": legacy}}))
            original = path.read_bytes()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": folder}):
                self.assertTrue(owns_claude_command(legacy))
                self.assertFalse(owns_claude_command(legacy + " | custom"))
                self.assertEqual(claude_status(), "claude_setup_required")
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(install_claude_statusline(), "claude_waiting")
                self.assertEqual(claude_status(), "claude_waiting")
                self.assertEqual(
                    path.with_name(path.name + ".agent-usage.bak").read_bytes(), original
                )

    def test_custom_replacement_preserves_current_settings_and_previous_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            backup = path.with_name(path.name + ".agent-usage.bak")
            backup.write_bytes(b"previous backup")
            original = b'{"theme":"dark","statusLine":{"type":"command","command":"custom"}}'
            path.write_bytes(original)
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": folder}):
                self.assertEqual(install_claude_statusline(), "claude_custom_statusline")
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(install_claude_statusline(replace=True), "claude_waiting")
                self.assertEqual(json.loads(path.read_text())["theme"], "dark")
                self.assertEqual(backup.read_bytes(), b"previous backup")
                self.assertIn(original, [file.read_bytes() for file in path.parent.glob("*.bak")])

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

    def test_claude_setup_preserves_settings_and_updates_only_its_own_command(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            original = b'{"permissions": {"deny": ["Read(.env)"]}, "theme": "dark"}\n'
            path.write_bytes(original)
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": folder}):
                self.assertEqual(install_claude_statusline(), "claude_waiting")
                settings = json.loads(path.read_text())
                self.assertEqual(settings["permissions"], {"deny": ["Read(.env)"]})
                self.assertEqual(settings["theme"], "dark")
                self.assertEqual(settings["statusLine"]["command"], claude_command())
                self.assertEqual(
                    path.with_name(path.name + ".agent-usage.bak").read_bytes(), original
                )
                modified = path.stat().st_mtime_ns
                install_claude_statusline()
                self.assertEqual(path.stat().st_mtime_ns, modified)
                override = Path(folder) / "custom config.toml"
                install_claude_statusline(override)
                self.assertEqual(
                    json.loads(path.read_text())["statusLine"]["command"], claude_command(override)
                )
                self.assertEqual(
                    path.with_name(path.name + ".agent-usage.bak").read_bytes(), original
                )

    def test_claude_setup_leaves_custom_invalid_and_missing_profiles_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(path.parent / "missing")}):
                self.assertEqual(claude_status(), "claude_not_found")
                self.assertFalse(path.parent.joinpath("missing").exists())
                self.assertEqual(install_claude_statusline(), "claude_waiting")
                self.assertTrue(path.parent.joinpath("missing/settings.json").is_file())
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": folder}):
                for original in (
                    b'{"statusLine":{"type":"command","command":"my-status --verbose"}}',
                    b'{"statusLine":false}',
                    b'{"statusLine":{"type":"command","command":""}}',
                ):
                    path.write_bytes(original)
                    self.assertEqual(install_claude_statusline(), "claude_custom_statusline")
                    self.assertEqual(path.read_bytes(), original)
                for original in (b'{"statusLine":', b"[]", b"\xff"):
                    path.write_bytes(original)
                    with self.assertRaises(ValueError):
                        install_claude_statusline()
                    self.assertEqual(path.read_bytes(), original)
                self.assertFalse(path.with_name(path.name + ".agent-usage.bak").exists())
                path.unlink()
                self.assertEqual(install_claude_statusline(), "claude_waiting")
                self.assertEqual(
                    json.loads(path.read_text())["statusLine"]["command"], claude_command()
                )

    def test_claude_setup_failure_and_concurrent_edit_preserve_original(self):
        from PySide6.QtCore import QLockFile, QSaveFile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            original = b'{"theme":"dark"}\n'
            path.write_bytes(original)
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": folder}):
                lock = QLockFile(str(path) + ".agent-usage.lock")
                self.assertTrue(lock.tryLock(0))
                try:
                    with self.assertRaises(OSError):
                        install_claude_statusline()
                finally:
                    lock.unlock()
                self.assertEqual(path.read_bytes(), original)
                with patch("PySide6.QtCore.QSaveFile.commit", return_value=False):
                    with self.assertRaises(OSError):
                        install_claude_statusline()
                self.assertEqual(path.read_bytes(), original)
                open_file = QSaveFile.open
                changed = b'{"theme":"light"}\n'

                def edit_before_save(output, mode):
                    path.write_bytes(changed)
                    return open_file(output, mode)

                with patch("PySide6.QtCore.QSaveFile.open", new=edit_before_save):
                    with self.assertRaises(OSError):
                        install_claude_statusline()
                self.assertEqual(path.read_bytes(), changed)

    @unittest.skipUnless(sys.platform == "win32", "Exercise native Windows command shells")
    def test_windows_capture_preserves_shell_metacharacters_and_standard_input(self):
        from usage.demo import profile

        with profile() as (config, _):
            root = Path(os.environ["CLAUDE_CONFIG_DIR"])
            override = root / "O'Brien $profile & config.toml"
            override.write_text("", encoding="utf-8")
            capture = Path(config["subscriptions"]["claude_file"])
            shells = [["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]]
            import shutil

            bash = shutil.which("bash")
            if bash:
                shells.append([bash, "-c"])
            for shell in shells:
                with self.subTest(shell=shell):
                    result = subprocess.run(
                        [*shell, claude_command(override)],
                        input=json.dumps(
                            {
                                "rate_limits": {
                                    "seven_day": {"used_percentage": 42, "resets_at": 2000000000}
                                }
                            }
                        ),
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                    self.assertIn("42%", result.stdout)
                    self.assertEqual(
                        json.loads(capture.read_text())["windows"][0]["used_percent"], 42
                    )
                    capture.unlink()

    def test_generated_and_legacy_commands_are_recognized_without_matching_custom_scripts(self):
        for platform in ("linux", "win32"):
            with patch("sys.platform", platform):
                self.assertTrue(owns_claude_command(claude_command()))
        self.assertTrue(owns_claude_command("C:/Python/python.exe -m usage --capture-claude"))
        self.assertTrue(
            owns_claude_command(
                "'/old install/agent-usage' --config '/old path/config' --capture-claude"
            )
        )
        for command in (
            None,
            "",
            "echo --capture-claude",
            "agent-usage --capture-claude | my-status",
            "python -m usage --capture-claude; other",
            "'unterminated",
            "powershell.exe -NoProfile -NonInteractive -EncodedCommand invalid",
        ):
            self.assertFalse(owns_claude_command(command))

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
