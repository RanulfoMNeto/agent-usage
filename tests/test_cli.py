import json
import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from usage.__main__ import main
from usage.demo import profile
from usage.history import Usage
from usage.integration import launch_command


class CLITests(unittest.TestCase):
    def test_demo_uses_real_readers_without_touching_the_user_profile(self):
        original = os.environ.copy()
        with patch("usage.subscriptions.read_codex", side_effect=AssertionError("Live account")):
            with profile() as (config, limits):
                root = Path(os.environ["CODEX_HOME"]).parent
                store = Usage(config)
                try:
                    store.sync()
                    rows = store.snapshot()["rows"]
                    self.assertGreater(len(rows), 1500)
                    self.assertGreater(
                        max(r["start"] for r in rows) - min(r["start"] for r in rows),
                        170 * 86400,
                    )
                    self.assertEqual({r["agent"] for r in rows}, {"Codex", "Claude Code"})
                    self.assertTrue(all(r["tokens"] > 0 for r in rows))
                    self.assertTrue(
                        all(r["cost"] is not None for r in rows if r["agent"] == "Codex")
                    )
                    self.assertTrue(
                        all(p.is_relative_to(root) for p in store.source_roots().values())
                    )
                    self.assertEqual(len(limits.snapshot()["providers"]), 2)
                    self.assertTrue(config["widget"]["demo"])
                finally:
                    store.close()
            self.assertFalse(root.exists())
        self.assertEqual(dict(os.environ), original)

    def test_generated_claude_config_captures_only_quota_fields(self):
        with profile() as (config, _):
            root = Path(os.environ["CODEX_HOME"]).parent
            override = root / "custom config.toml"
            (root / "usage.py").write_text("raise RuntimeError('Project module must not run')\n")
            custom_capture = root / "custom limits.json"
            override.write_text(
                "[subscriptions]\nclaude_file = " + json.dumps(custom_capture.as_posix()) + "\n"
            )
            for options, capture in (
                ([], Path(config["subscriptions"]["claude_file"])),
                (["--config", str(override)], custom_capture),
            ):
                with self.subTest(options=options):
                    process = subprocess.run(
                        [sys.executable, "-m", "usage", "--claude-config", *options],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    command = shlex.split(json.loads(process.stdout)["statusLine"]["command"])
                    result = subprocess.run(
                        command,
                        cwd=root,
                        input=json.dumps(
                            {
                                "cwd": "PRIVATE",
                                "rate_limits": {
                                    "seven_day": {"used_percentage": 42, "resets_at": 2000000000}
                                },
                            }
                        ),
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    self.assertIn("42%", result.stdout)
                    captured = capture.read_text()
                    self.assertNotIn("PRIVATE", captured)
                    self.assertEqual(json.loads(captured)["windows"][0]["used_percent"], 42)

    def test_packaged_launcher_does_not_invoke_python_module(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(launch_command(), [sys.executable])

    def test_normal_launch_installs_integrations_and_optional_modes_do_not(self):
        with profile() as (config, _):
            with (
                patch("usage.__main__.load_config", return_value=config),
                patch("usage.desktop.run") as run,
                patch("usage.integration.install_windows_shortcut") as shortcut,
                patch(
                    "usage.integration.install_claude_statusline", return_value="claude_waiting"
                ) as claude,
                patch("sys.platform", "win32"),
                patch.object(sys, "frozen", False, create=True),
                patch.object(sys, "argv", ["agent-usage"]),
            ):
                main()
                shortcut.assert_called_once_with(None)
                claude.assert_called_once_with(None)
                self.assertEqual(
                    run.call_args.kwargs["subscriptions"].claude_issue, "claude_waiting"
                )
                shortcut.reset_mock()
                claude.reset_mock()
                with patch.object(sys, "argv", ["agent-usage", "--demo"]):
                    main()
                shortcut.assert_not_called()
                claude.assert_not_called()
                config["integrations"] = {"windows_shortcut": False, "claude_statusline": False}
                main()
                shortcut.assert_not_called()
                claude.assert_not_called()
                self.assertEqual(
                    run.call_args.kwargs["subscriptions"].claude_issue, "claude_setup_disabled"
                )

    def test_setup_failure_does_not_prevent_widget_startup(self):
        with profile() as (config, _):
            with (
                patch("usage.__main__.load_config", return_value=config),
                patch("usage.desktop.run") as run,
                patch("usage.integration.install_windows_shortcut", side_effect=OSError),
                patch("usage.integration.install_claude_statusline", side_effect=OSError),
                patch("sys.platform", "win32"),
                patch.object(sys, "argv", ["agent-usage"]),
                self.assertLogs(level="WARNING") as logs,
            ):
                main()
                self.assertEqual(
                    run.call_args.kwargs["subscriptions"].claude_issue, "claude_setup_failed"
                )
                self.assertEqual(len(logs.output), 2)
