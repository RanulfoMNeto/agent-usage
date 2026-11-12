import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from usage import VERSION
from usage.__main__ import main
from usage.agents import AGENTS, Agent
from usage.config import load_config
from usage.demo import profile
from usage.history import Usage
from usage.integration import launch_command


class CLITests(unittest.TestCase):
    def test_demo_uses_every_agent_reader_without_touching_the_user_profile(self):
        original = os.environ.copy()
        with patch.object(Agent, "read_limits", new_callable=AsyncMock) as live:
            with profile() as (config, limits):
                root = Path(config["agents"][next(iter(AGENTS))]["history"]).parent
                store = Usage(config)
                try:
                    store.sync()
                    rows = store.snapshot()["rows"]
                    self.assertGreater(len(rows), 1500)
                    self.assertGreater(
                        max(r["start"] for r in rows) - min(r["start"] for r in rows),
                        170 * 86400,
                    )
                    self.assertEqual({r["agent"] for r in rows}, set(AGENTS))
                    self.assertTrue(all(r["tokens"] > 0 for r in rows))
                    self.assertTrue(any(r["cost"] is not None for r in rows))
                    self.assertTrue(
                        all(p.is_relative_to(root) for p in store.source_roots().values())
                    )
                    snapshot = limits.snapshot()["agents"]
                    self.assertEqual([a["id"] for a in snapshot], list(AGENTS))
                    self.assertTrue(all(a["windows"] for a in snapshot))
                    self.assertTrue(config["widget"]["demo"])
                finally:
                    store.close()
            self.assertFalse(root.exists())
        live.assert_not_awaited()
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
                patch("sys.platform", "win32"),
                patch.object(sys, "argv", ["agent-usage"]),
            ):
                main()
                shortcut.assert_called_once_with(None)
                self.assertIs(run.call_args.kwargs["limits"].agents, AGENTS)
                shortcut.reset_mock()
                with patch.object(sys, "argv", ["agent-usage", "--demo"]):
                    main()
                shortcut.assert_not_called()
                config["integrations"] = {"windows_shortcut": False}
                main()
                shortcut.assert_not_called()

    def test_setup_failure_does_not_prevent_widget_startup(self):
        with profile() as (config, _):
            with (
                patch("usage.__main__.load_config", return_value=config),
                patch("usage.desktop.run") as run,
                patch("usage.integration.install_windows_shortcut", side_effect=OSError),
                patch("sys.platform", "win32"),
                patch.object(sys, "argv", ["agent-usage"]),
                self.assertLogs(level="WARNING") as logs,
            ):
                main()
                run.assert_called_once()
                self.assertEqual(len(logs.output), 1)

    def test_configuration_is_per_agent_and_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text(
                '[agents.codex]\ncommand = ["/opt/codex"]\nhistory = "/data/codex"\n'
                '[subscriptions]\ncodex_command = ["/old"]\n[widget]\nwidth = 350\n',
                encoding="utf-8",
            )
            with self.assertLogs(level="WARNING") as logs:
                config = load_config(path)
            self.assertIn("subscriptions", logs.output[0])
            self.assertEqual(
                config["agents"]["codex"], {"command": ["/opt/codex"], "history": "/data/codex"}
            )
            self.assertNotIn("subscriptions", config)
            self.assertEqual(config["widget"]["width"], 350)
            for body in (
                '[agents.unknown]\ncommand = ["x"]\n',
                '[agents.codex]\ncommand = "codex"\n',
                '[agents.codex]\nhistory = ""\n',
                '[agents.codex]\npath = "/x"\n',
                "[widget]\nwidth = 0\n",
                "[limits]\nrefresh_seconds = 600\n",
                "[scheduler]\nrefresh_seconds = 0\n",
                "[scheduler]\nrefresh_seconds = 600\n",
                "[scheduler]\nunknown = 1\n",
            ):
                with self.subTest(body=body):
                    path.write_text(body, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_config(path)

    def test_package_version_matches_the_project(self):
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], VERSION)
