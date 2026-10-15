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
    Subscriptions,
    capture_claude,
    claude_snapshot,
    codex_snapshot,
    read_codex,
)


class SubscriptionTests(unittest.TestCase):
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
            source.checked = None
            with patch(
                "usage.subscriptions.read_codex",
                new_callable=AsyncMock,
                side_effect=RuntimeError("SECRET"),
            ):
                failed = source.snapshot()["providers"][0]
            self.assertEqual(failed["status"], "unavailable")
            self.assertEqual(failed["windows"], [])
            self.assertNotIn("SECRET", json.dumps(failed))

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
                with self.assertRaises(TimeoutError):
                    asyncio.run(read_codex([sys.executable, str(script), "hang"], 0.2))
                self.assertIsNotNone(processes[-1].returncode)
