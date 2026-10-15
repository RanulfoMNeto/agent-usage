import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from usage.subscriptions import (
    CodexUnavailable,
    Subscriptions,
    capture_claude,
    claude_snapshot,
    codex_command,
    codex_snapshot,
    read_codex,
)


class SubscriptionTests(unittest.TestCase):
    def test_default_command_finds_native_installations_without_changing_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with (
                patch("usage.subscriptions.Path.home", return_value=home),
                patch("usage.subscriptions.shutil.which", return_value=None) as which,
                patch("usage.subscriptions.platform.machine", return_value="AMD64"),
                patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": str(home / "AppData/Local"), "CODEX_INSTALL_DIR": ""},
                ),
            ):
                for system, target, name, installed in (
                    (
                        "win32",
                        "windows-x86_64",
                        "codex.exe",
                        home / "AppData/Local/Programs/OpenAI/Codex/bin",
                    ),
                    ("linux", "linux-x86_64", "codex", home / ".local/bin"),
                ):
                    with self.subTest(system=system), patch("sys.platform", system):
                        with self.assertRaises(FileNotFoundError):
                            codex_command(["codex"])
                        for editor in (".vscode", ".vscode-insiders", ".cursor"):
                            versions = []
                            for version in ("1.9.0", "1.10.0"):
                                path = (
                                    home
                                    / editor
                                    / "extensions"
                                    / ("openai.chatgpt-" + version)
                                    / "bin"
                                    / target
                                    / name
                                )
                                path.parent.mkdir(parents=True, exist_ok=True)
                                path.touch()
                                path.chmod(0o700)
                                versions.append(path)
                            self.assertEqual(codex_command(["codex"]), [str(versions[-1])])
                            versions[-1].unlink()
                            self.assertEqual(codex_command(["codex"]), [str(versions[0])])
                            versions[0].unlink()
                        native = installed / name
                        native.parent.mkdir(parents=True, exist_ok=True)
                        native.touch()
                        native.chmod(0o700)
                        self.assertEqual(codex_command(["codex"]), [str(native)])
                        custom = home / "custom install" / name
                        custom.parent.mkdir(exist_ok=True)
                        custom.touch()
                        custom.chmod(0o700)
                        with patch.dict(os.environ, {"CODEX_INSTALL_DIR": str(custom.parent)}):
                            self.assertEqual(codex_command(["codex"]), [str(custom)])
                        custom.unlink()
                        which.return_value = str(home / "on-path" / name)
                        self.assertEqual(codex_command(["codex"]), [which.return_value])
                        which.return_value = str(Path.cwd() / name)
                        self.assertEqual(codex_command(["codex"]), [str(native)])
                        which.return_value = None
                        native.unlink()
                override = ["/custom/codex", "-c", "model_provider=custom"]
                which.reset_mock()
                self.assertEqual(codex_command(override), override)
                which.assert_not_called()

    def test_codex_preserves_native_windows_and_zero_without_private_fields(self):
        payload = {
            "accountId": "SECRET",
            "rateLimits": {"primary": {"usedPercent": 99}},
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 2000},
                    "secondary": None,
                    "credits": {"id": "SECRET"},
                }
            },
        }
        snapshot = codex_snapshot(payload, 1000)
        self.assertEqual(
            snapshot["windows"], [dict(name="codex", minutes=10080, used_percent=0, resets_at=2000)]
        )
        self.assertNotIn("SECRET", json.dumps(snapshot))
        self.assertEqual(codex_snapshot({"rateLimits": None}, 1000)["windows"], [])

    def test_missing_or_invalid_native_values_never_become_zero(self):
        for value in (None, True, -1, 101, float("nan"), "invalid"):
            with self.subTest(value=value):
                snapshot = claude_snapshot(
                    {"rate_limits": {"five_hour": {"used_percentage": value, "resets_at": 2000}}},
                    1000,
                )
                self.assertEqual(snapshot["windows"], [])
        self.assertEqual(claude_snapshot({}, 1000)["windows"], [])

    def test_claude_capture_is_private_atomic_and_whitelisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "claude.json"
            capture_claude(
                path,
                {
                    "cwd": "SECRET",
                    "transcript_path": "PRIVATE",
                    "rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": 2000}},
                },
            )
            self.assertEqual(json.loads(path.read_text())["windows"][0]["used_percent"], 12)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("SECRET", path.read_text())
            self.assertNotIn("PRIVATE", path.read_text())
            capture_claude(path, {})
            self.assertEqual(json.loads(path.read_text())["windows"], [])
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_refresh_failure_expiry_and_staleness_are_honest(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.json"
            path.write_text(
                json.dumps(
                    dict(
                        updated_at=now - 600,
                        windows=[
                            dict(minutes=300, used_percent=20, resets_at=now + 3600),
                            dict(minutes=10080, used_percent=90, resets_at=now - 10),
                        ],
                    )
                )
            )
            source = Subscriptions(
                dict(
                    codex_command=["codex"],
                    claude_file=str(path),
                    refresh_seconds=60,
                    timeout_seconds=2,
                    stale_seconds=300,
                )
            )
            payload = {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 5,
                        "windowDurationMins": 10080,
                        "resetsAt": now + 3600,
                    }
                }
            }
            with patch(
                "usage.subscriptions.read_codex", new_callable=AsyncMock, return_value=payload
            ) as read:
                first = source.snapshot()["providers"]
                source.snapshot()
                self.assertEqual(read.await_count, 1)
            self.assertEqual(first[0]["status"], "available")
            self.assertEqual(first[1]["status"], "stale")
            self.assertEqual(len(first[1]["windows"]), 1)
            capture_claude(path, {})
            self.assertEqual(source.snapshot()["providers"][1]["issue"], "claude_no_limits")
            path.unlink()
            self.assertEqual(source.snapshot()["providers"][1]["issue"], "claude_waiting")
            for error, issue in (
                (RuntimeError("SECRET"), "query_failed"),
                (FileNotFoundError("PRIVATE"), "not_found"),
                (CodexUnavailable("login_required"), "login_required"),
                (CodexUnavailable("unsupported_account"), "unsupported_account"),
                (TimeoutError("SECRET"), "timeout"),
            ):
                source.checked = None
                with patch(
                    "usage.subscriptions.read_codex", new_callable=AsyncMock, side_effect=error
                ):
                    failed = source.snapshot()["providers"][0]
                self.assertEqual(failed["status"], "unavailable")
                self.assertEqual(failed["windows"], [])
                self.assertEqual(failed["issue"], issue)
                self.assertNotIn("SECRET", json.dumps(failed))
                self.assertNotIn("PRIVATE", json.dumps(failed))

    def test_native_protocol_handshake_and_timeout_cleanup(self):
        processes = []
        create = asyncio.create_subprocess_exec

        async def track(*args, **kwargs):
            process = await create(*args, **kwargs)
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "native.py"
            script.write_text("""import json, sys, time
init=json.loads(sys.stdin.readline())
assert init['method']=='initialize'
print(json.dumps({'id':init['id'],'result':{}}),flush=True)
assert json.loads(sys.stdin.readline())['method']=='initialized'
query=json.loads(sys.stdin.readline())
assert query['method']=='account/read'
account={'type':'chatgpt','email':'PRIVATE','planType':'pro'}
if sys.argv[1]=='signed-out': account=None
if sys.argv[1]=='api-key': account={'type':'apiKey'}
print(json.dumps({'id':query['id'],'result':{'account':account,'requiresOpenaiAuth':True}}),flush=True)
query=json.loads(sys.stdin.readline())
assert query['method']=='account/rateLimits/read'
if sys.argv[1]=='hang': time.sleep(30)
print(json.dumps({'method':'notification'}),flush=True)
print(json.dumps({'id':query['id'],'result':{'rateLimits':None}}),flush=True)
time.sleep(30)
""")
            with patch("usage.subscriptions.asyncio.create_subprocess_exec", side_effect=track):
                result = asyncio.run(read_codex([sys.executable, str(script), "ok"], 5))
                self.assertEqual(result, {"rateLimits": None})
                self.assertIsNotNone(processes[-1].returncode)
                for mode, issue in (
                    ("signed-out", "login_required"),
                    ("api-key", "unsupported_account"),
                ):
                    with self.assertRaisesRegex(CodexUnavailable, issue):
                        asyncio.run(read_codex([sys.executable, str(script), mode], 5))
                    self.assertIsNotNone(processes[-1].returncode)
                with self.assertRaises(TimeoutError):
                    asyncio.run(read_codex([sys.executable, str(script), "hang"], 0.2))
                self.assertIsNotNone(processes[-1].returncode)
