import os
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

    def test_packaged_launcher_does_not_invoke_python_module(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(launch_command(), [sys.executable])

    def test_launch_registers_shortcut_but_never_configures_agents(self):
        with profile() as (config, _):
            with (
                patch("usage.__main__.load_config", return_value=config),
                patch("usage.desktop.run") as run,
                patch("usage.integration.install_windows_shortcut") as shortcut,
                patch(
                    "usage.integration.remove_legacy_claude_statusline", return_value=None
                ) as claude,
                patch("sys.platform", "win32"),
                patch.object(sys, "frozen", False, create=True),
                patch.object(sys, "argv", ["agent-usage"]),
            ):
                main()
                shortcut.assert_called_once_with(None)
                claude.assert_called_once_with()
                self.assertEqual(
                    run.call_args.kwargs["subscriptions"].config, config["subscriptions"]
                )
                shortcut.reset_mock()
                claude.reset_mock()
                with patch.object(sys, "argv", ["agent-usage", "--demo"]):
                    main()
                shortcut.assert_not_called()
                claude.assert_not_called()
                config["integrations"] = {"windows_shortcut": False}
                main()
                shortcut.assert_not_called()
                claude.assert_called_once_with()

    def test_setup_failure_does_not_prevent_widget_startup(self):
        with profile() as (config, _):
            with (
                patch("usage.__main__.load_config", return_value=config),
                patch("usage.desktop.run") as run,
                patch("usage.integration.install_windows_shortcut", side_effect=OSError),
                patch("usage.integration.remove_legacy_claude_statusline", side_effect=OSError),
                patch("sys.platform", "win32"),
                patch.object(sys, "argv", ["agent-usage"]),
                self.assertLogs(level="WARNING") as logs,
            ):
                main()
                run.assert_called_once()
                self.assertEqual(len(logs.output), 2)
