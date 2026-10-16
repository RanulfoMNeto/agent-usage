import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from usage.integration import agent_command
from usage.subscriptions import (
    AgentUnavailable,
    Subscriptions,
    claude_snapshot,
    codex_snapshot,
    read_claude,
    read_codex,
)


class SubscriptionTests(unittest.TestCase):
    def test_disconnected_agents_never_query_accounts(self):
        source = Subscriptions({"refresh_seconds": 60})
        with patch.object(source, "query") as query:
            providers = source.snapshot({"codex": False, "claude": False})["providers"]
            query.assert_not_called()
            self.assertTrue(
                all(
                    p["issue"] == "disconnected" and not p["connected"] and not p["windows"]
                    for p in providers
                )
            )

    def test_login_uses_native_flow_validates_urls_and_can_be_cancelled(self):
        processes = []
        create = asyncio.create_subprocess_exec

        async def track(*args, **kwargs):
            process = await create(*args, **kwargs)
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "login.py"
            script.write_text("""import json, sys, time
def request(method, result):
    query = json.loads(sys.stdin.readline())
    assert query['method'] == method, query
    print(json.dumps({'id': query['id'], 'result': result}), flush=True)
request('initialize', {})
assert json.loads(sys.stdin.readline())['method'] == 'initialized'
request('account/read', {'account': None, 'requiresOpenaiAuth': True})
url = 'https://auth.openai.com/oauth/authorize?state=PRIVATE'
if sys.argv[1] == 'bad-url': url = 'https://example.com/steal'
request('account/login/start', {'loginId': 'expected', 'authUrl': url})
print(json.dumps({'method':'account/login/completed','params':{'loginId':'other','success':False}}), flush=True)
if sys.argv[1] == 'cancel':
    assert json.loads(sys.stdin.readline())['method'] == 'account/login/cancel'
else:
    print(json.dumps({'method':'account/login/completed','params':{'loginId':'expected','success':sys.argv[1] != 'reject'}}), flush=True)
    request('account/read', {'account': {'type':'chatgpt','email':'PRIVATE'}})
    request('account/rateLimits/read', {'rateLimits': {'primary': {'usedPercent': 12, 'windowDurationMins': 300, 'resetsAt': 2000000000}}})
time.sleep(30)
""")
            with patch("usage.subscriptions.asyncio.create_subprocess_exec", side_effect=track):
                opened = []
                result = asyncio.run(
                    read_codex([sys.executable, str(script), "ok"], 5, login=opened.append)
                )
                self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 12)
                self.assertEqual(len(opened), 1)
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertIsNotNone(processes[-1].returncode)
                for mode, error, message in (
                    ("bad-url", ValueError, "Invalid native login URL"),
                    ("reject", AgentUnavailable, "login_cancelled"),
                    ("cancel", AgentUnavailable, "login_cancelled"),
                ):
                    with self.subTest(mode=mode):
                        cancelled = threading.Event()
                        opened.clear()

                        def open_browser(url):
                            opened.append(url)
                            if mode == "cancel":
                                cancelled.set()

                        with self.assertRaisesRegex(error, message):
                            asyncio.run(
                                read_codex(
                                    [sys.executable, str(script), mode],
                                    5,
                                    login=open_browser,
                                    cancelled=cancelled,
                                )
                            )
                        self.assertEqual(len(opened), 0 if mode == "bad-url" else 1)
                        self.assertIsNotNone(processes[-1].returncode)

    def test_default_command_finds_native_installations_without_changing_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with (
                patch("usage.integration.Path.home", return_value=home),
                patch("usage.integration.shutil.which", return_value=None) as which,
                patch("usage.integration.platform.machine", return_value="AMD64"),
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
                            agent_command("codex", ["codex"])
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
                            self.assertEqual(agent_command("codex", ["codex"]), [str(versions[-1])])
                            versions[-1].unlink()
                            self.assertEqual(agent_command("codex", ["codex"]), [str(versions[0])])
                            versions[0].unlink()
                        native = installed / name
                        native.parent.mkdir(parents=True, exist_ok=True)
                        native.touch()
                        native.chmod(0o700)
                        self.assertEqual(agent_command("codex", ["codex"]), [str(native)])
                        custom = home / "custom install" / name
                        custom.parent.mkdir(exist_ok=True)
                        custom.touch()
                        custom.chmod(0o700)
                        with patch.dict(os.environ, {"CODEX_INSTALL_DIR": str(custom.parent)}):
                            self.assertEqual(agent_command("codex", ["codex"]), [str(custom)])
                        custom.unlink()
                        which.return_value = str(home / "on-path" / name)
                        self.assertEqual(agent_command("codex", ["codex"]), [which.return_value])
                        which.return_value = str(Path.cwd() / name)
                        self.assertEqual(agent_command("codex", ["codex"]), [str(native)])
                        which.return_value = None
                        native.unlink()
                override = ["/custom/codex", "-c", "model_provider=custom"]
                which.reset_mock()
                self.assertEqual(agent_command("codex", override), override)
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
                    {
                        "rate_limits": {
                            "five_hour": {"utilization": value, "resets_at": "2099-01-01T00:00:00Z"}
                        }
                    },
                    1000,
                )
                self.assertEqual(snapshot["windows"], [])
        self.assertEqual(claude_snapshot({}, 1000)["windows"], [])

    def test_both_accounts_refresh_independently_of_terminal_activity(self):
        source = Subscriptions(
            dict(
                codex_command=["codex"],
                claude_command=["claude"],
                refresh_seconds=60,
                timeout_seconds=2,
                stale_seconds=300,
            )
        )
        payload = {
            "rate_limits_available": True,
            "rate_limits": {
                "five_hour": {"utilization": 0, "resets_at": "2099-01-01T00:00:00Z"},
                "seven_day": {"utilization": 3, "resets_at": "2099-01-07T00:00:00Z"},
            },
            "session": "PRIVATE",
            "subscription_type": "pro",
        }
        with (
            patch(
                "usage.subscriptions.read_codex",
                new_callable=AsyncMock,
                return_value={"rateLimits": None},
            ) as codex,
            patch(
                "usage.subscriptions.read_claude", new_callable=AsyncMock, return_value=payload
            ) as claude,
        ):
            first = source.snapshot()["providers"][1]
            self.assertEqual(first["status"], "available")
            self.assertEqual([w["used_percent"] for w in first["windows"]], [0, 3])
            self.assertNotIn("PRIVATE", json.dumps(first))
            source.snapshot()
            self.assertEqual(codex.await_count, 1)
            self.assertEqual(claude.await_count, 1)
            source.checked["claude"] -= 61
            payload["rate_limits"]["seven_day"]["utilization"] = 7
            self.assertEqual(source.snapshot()["providers"][1]["windows"][1]["used_percent"], 7)
            self.assertEqual(codex.await_count, 1)
            self.assertEqual(claude.await_count, 2)
            for error, issue in (
                (RuntimeError("SECRET"), "query_failed"),
                (FileNotFoundError("PRIVATE"), "not_found"),
                (AgentUnavailable("login_required"), "login_required"),
                (AgentUnavailable("unsupported_version"), "unsupported_version"),
                (TimeoutError("SECRET"), "timeout"),
            ):
                source.checked.clear()
                claude.side_effect = error
                failed = source.snapshot()["providers"][1]
                self.assertEqual(failed["status"], "unavailable")
                self.assertEqual(failed["windows"], [])
                self.assertEqual(failed["issue"], issue)
                self.assertNotIn("SECRET", json.dumps(failed))

    def test_claude_control_queries_never_send_prompts_and_stop_on_failures(self):
        processes = []
        create = asyncio.create_subprocess_exec

        async def track(*args, **kwargs):
            self.assertIn("--safe-mode", args)
            self.assertIn("--no-session-persistence", args)
            self.assertEqual(list(Path(kwargs["cwd"]).iterdir()), [])
            process = await create(*args, **kwargs)
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "native.py"
            script.write_text(
                "import json, sys, time\nmode=sys.argv[1]\nfor line in sys.stdin:\n    msg=json.loads(line)\n    assert msg['type']=='control_request'\n    kind=msg['request']['subtype']\n    assert kind in ('initialize', 'get_usage')\n    response={'subtype':'success','request_id':msg['request_id']}\n    if kind=='initialize':\n        response['response']={'account':{'tokenSource':'none'} if mode=='signed-out' else {'email':'PRIVATE'}}\n    else:\n        assert msg['request']['skip_behaviors'] is True\n        if mode=='hang': time.sleep(30)\n        if mode=='unsupported': response.update(subtype='error',error='Unsupported control request subtype: get_usage')\n        elif mode=='permission':\n            print(json.dumps({'type':'control_request','request':{'subtype':'can_use_tool'}}),flush=True)\n            time.sleep(30)\n        else: response['response']={'rate_limits_available':mode!='api-key','rate_limits':{'five_hour':{'utilization':0,'resets_at':'2099-01-01T00:00:00Z'}}}\n    print(json.dumps({'type':'control_response','response':response}),flush=True)\n"
            )
            with patch("usage.subscriptions.asyncio.create_subprocess_exec", side_effect=track):
                payload = asyncio.run(read_claude([sys.executable, str(script), "ok"], 5))
                self.assertEqual(
                    claude_snapshot(payload, time.time())["windows"][0]["used_percent"], 0
                )
                self.assertIsNotNone(processes[-1].returncode)
                self.assertNotIn("PRIVATE", json.dumps(payload))
                for mode, error, message in (
                    ("signed-out", AgentUnavailable, "login_required"),
                    ("api-key", AgentUnavailable, "unsupported_account"),
                    ("unsupported", AgentUnavailable, "unsupported_version"),
                    ("permission", RuntimeError, "Unexpected native permission request"),
                    ("hang", TimeoutError, ""),
                ):
                    with self.assertRaisesRegex(error, message):
                        asyncio.run(read_claude([sys.executable, str(script), mode], 0.3))
                    self.assertIsNotNone(processes[-1].returncode)

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
                    with self.assertRaisesRegex(AgentUnavailable, issue):
                        asyncio.run(read_codex([sys.executable, str(script), mode], 5))
                    self.assertIsNotNone(processes[-1].returncode)
                with self.assertRaises(TimeoutError):
                    asyncio.run(read_codex([sys.executable, str(script), "hang"], 0.2))
                self.assertIsNotNone(processes[-1].returncode)
