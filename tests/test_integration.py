import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from usage.agents import AGENTS
from usage.desktop import top_bar_limits
from usage.integration import (
    UUID,
    ConnectionUnavailable,
    install_gnome,
    install_windows_shortcut,
    launch_command,
    open_terminal,
    terminal_command,
)


class IntegrationTests(unittest.TestCase):
    def test_sign_in_terminal_arguments_are_never_interpreted_by_a_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            command = [str(home / "Agent $data; & binary"), "a b"]
            with (
                patch("usage.integration.Path.home", return_value=home),
                patch("usage.integration.shutil.which", return_value=None) as which,
            ):
                with patch("sys.platform", "win32"):
                    self.assertEqual(terminal_command(command), command)
                    with (
                        patch("usage.integration.subprocess.Popen") as launch,
                        patch("usage.integration.subprocess.CREATE_NEW_CONSOLE", 16, create=True),
                    ):
                        open_terminal(command)
                        launch.assert_called_once_with(command, cwd=home, creationflags=16)
                with patch("sys.platform", "linux"):
                    with self.assertRaises(ConnectionUnavailable):
                        terminal_command(command)
                    which.side_effect = lambda name: (
                        "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None
                    )
                    args = terminal_command(["~/bin/agent", "--login"])
                    expanded = str(Path("~/bin/agent").expanduser())
                    self.assertEqual(args, ["/usr/bin/gnome-terminal", "--", expanded, "--login"])
                    with patch(
                        "PySide6.QtCore.QProcess.startDetached", return_value=(True, 123)
                    ) as launch:
                        open_terminal(args)
                        launch.assert_called_once_with(args[0], args[1:], str(home))
                    with (
                        patch("PySide6.QtCore.QProcess.startDetached", return_value=(False, 0)),
                        self.assertRaises(ConnectionUnavailable),
                    ):
                        open_terminal(args)

    @unittest.skipUnless(shutil.which("node"), "Node runs the top bar module outside the shell")
    def test_top_bar_accepts_the_payload_the_app_publishes(self):
        now = time.time()
        snapshot = {
            "agents": [
                dict(
                    id=agent,
                    status="available",
                    updated_at=now,
                    connected=True,
                    windows=[
                        dict(
                            id=f"{agent}::300",
                            bucket=None,
                            minutes=300,
                            used_percent=20 + index,
                            resets_at=now + 3600,
                        )
                    ],
                )
                for index, agent in enumerate(AGENTS)
            ]
        }
        payload = json.dumps(top_bar_limits(snapshot, AGENTS, 80, True))
        module = (Path(__file__).parents[1] / "usage/gnome/limits.js").as_uri()
        script = (
            f"import {{accepts, summarize}} from {json.dumps(module)};"
            "const payload = JSON.parse(process.argv[1]);"
            "const {agents, shown} = summarize(payload, Date.now() / 1000, process.argv[2].split(','));"
            "console.log(JSON.stringify([accepts(payload), agents.map(a => a.name),"
            " shown.map(l => l.id)]));"
        )
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, payload, f"{list(AGENTS)[-1]}::300"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        accepted, names, shown = json.loads(result.stdout)
        self.assertTrue(accepted)
        self.assertEqual(sorted(names), sorted(agent.name for agent in AGENTS.values()))
        self.assertEqual(shown, [f"{list(AGENTS)[-1]}::300"])

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
                stale = root / "gnome-shell/extensions" / UUID / "icons/retired-symbolic.svg"
                stale.touch()
                install_gnome()
                self.assertFalse(stale.exists())
                self.assertEqual(settings["enabled-extensions"], ["existing@example", UUID])
                self.assertEqual(settings["disabled-extensions"], [])
                extension = root / "gnome-shell/extensions" / UUID
                for name in ("extension.js", "limits.js", "stylesheet.css"):
                    self.assertTrue((extension / name).is_file())
                # Each registered agent's widget icon becomes its recolorable top bar icon.
                self.assertEqual(
                    sorted(path.name for path in (extension / "icons").iterdir()),
                    sorted(
                        ["agent-usage-symbolic.svg", "clock-symbolic.svg"]
                        + [f"{agent}-symbolic.svg" for agent in AGENTS]
                    ),
                )
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
