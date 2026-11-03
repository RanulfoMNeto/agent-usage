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

from usage.agents import AGENTS, AgentUnavailable, Login
from usage.agents.claude import Claude
from usage.agents.codex import Codex
from usage.limits import Limits

SETTINGS = dict(refresh_seconds=60, timeout_seconds=2, stale_seconds=300)


def tracked(test):
    """Record native processes so each test can check they were stopped."""
    processes = []
    create = asyncio.create_subprocess_exec

    async def track(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    return processes, patch("usage.agents.base.asyncio.create_subprocess_exec", side_effect=track)


class LimitTests(unittest.TestCase):
    def test_every_agent_implements_the_contract(self):
        for agent in AGENTS.values():
            with self.subTest(agent=agent.id):
                self.assertRegex(agent.id, r"^[a-z0-9][a-z0-9_-]*$")
                self.assertTrue(agent.name and agent.namespace and agent.executable)
                self.assertEqual(len(agent.colors), 2)
                self.assertIn(agent.login, ("terminal", "browser"))
                self.assertEqual(agent.login == "browser", bool(agent.login_hosts))
                icon = Path("usage/static/assets/agents") / f"{agent.id}.svg"
                self.assertTrue(icon.is_file(), icon)
                self.assertTrue(agent.default_history().is_absolute())
        self.assertEqual(len({agent.namespace for agent in AGENTS.values()}), len(AGENTS))

    def test_disconnected_agents_never_query_accounts(self):
        source = Limits({"limits": SETTINGS})
        with patch.object(source, "query") as query:
            agents = source.snapshot(dict.fromkeys(AGENTS, False))["agents"]
            query.assert_not_called()
        self.assertEqual([agent["id"] for agent in agents], list(AGENTS))
        self.assertTrue(
            all(
                a["issue"] == "disconnected" and not a["connected"] and not a["windows"]
                for a in agents
            )
        )

    def test_browser_login_validates_urls_and_can_be_cancelled(self):
        processes, tracking = tracked(self)
        with tempfile.TemporaryDirectory() as directory, tracking:
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
            opened = []
            windows = asyncio.run(
                Codex().read_limits(
                    [sys.executable, str(script), "ok"], 5, Login(opened.append, threading.Event())
                )
            )
            self.assertEqual(
                windows, [dict(bucket="codex", minutes=300, used_percent=12, resets_at=2000000000)]
            )
            self.assertEqual(len(opened), 1)
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
                            Codex().read_limits(
                                [sys.executable, str(script), mode],
                                5,
                                Login(open_browser, cancelled),
                            )
                        )
                    self.assertEqual(len(opened), 0 if mode == "bad-url" else 1)
                    self.assertIsNotNone(processes[-1].returncode)

    def test_default_commands_find_native_installations_without_changing_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layouts = (
                (Codex(), "win32", "codex.exe", "openai.chatgpt", "bin/windows-x86_64",
                 home / "AppData/Local/Programs/OpenAI/Codex/bin"),
                (Codex(), "linux", "codex", "openai.chatgpt", "bin/linux-x86_64", home / ".local/bin"),
                (Claude(), "win32", "claude.exe", "anthropic.claude-code", "resources/native-binary",
                 home / ".local/bin"),
                (Claude(), "linux", "claude", "anthropic.claude-code", "resources/native-binary",
                 home / ".local/bin"),
            )  # fmt: skip
            with (
                patch("usage.agents.base.Path.home", return_value=home),
                patch("usage.agents.base.shutil.which", return_value=None) as which,
                patch("usage.agents.base.platform.machine", return_value="AMD64"),
                patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": str(home / "AppData/Local"), "CODEX_INSTALL_DIR": ""},
                ),
            ):
                for agent, system, name, extension, inner, installed in layouts:
                    with self.subTest(agent=agent.id, system=system), patch("sys.platform", system):
                        with self.assertRaises(FileNotFoundError):
                            agent.command()
                        for editor in (".vscode", ".vscode-insiders", ".cursor"):
                            versions = []
                            for version in ("1.9.0", "1.10.0"):
                                path = home / editor / "extensions" / f"{extension}-{version}"
                                path = path / inner / name
                                path.parent.mkdir(parents=True, exist_ok=True)
                                path.touch()
                                path.chmod(0o700)
                                versions.append(path)
                            self.assertEqual(agent.command(), [str(versions[-1])])
                            versions[-1].unlink()
                            self.assertEqual(agent.command(), [str(versions[0])])
                            versions[0].unlink()
                        native = installed / name
                        native.parent.mkdir(parents=True, exist_ok=True)
                        native.touch()
                        native.chmod(0o700)
                        self.assertEqual(agent.command(), [str(native)])
                        which.return_value = str(home / "on-path" / name)
                        self.assertEqual(agent.command(), [which.return_value])
                        # A program in the current directory is never picked up.
                        which.return_value = str(Path.cwd() / name)
                        self.assertEqual(agent.command(), [str(native)])
                        which.return_value = None
                        native.unlink()
                # Outside the simulated platforms, the executable name follows the real one.
                custom = (
                    home / "custom install" / ("codex.exe" if sys.platform == "win32" else "codex")
                )
                custom.parent.mkdir()
                custom.touch()
                custom.chmod(0o700)
                with patch.dict(os.environ, {"CODEX_INSTALL_DIR": str(custom.parent)}):
                    self.assertEqual(Codex().command(), [str(custom)])
                override = ["/custom/agent", "-c", "option=value"]
                which.reset_mock()
                self.assertEqual(Codex().command(override), override)
                which.assert_not_called()

    def test_windows_are_normalized_without_private_fields(self):
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
        windows = Codex.windows(payload)
        self.assertEqual(
            windows, [dict(bucket="codex", minutes=10080, used_percent=0, resets_at=2000)]
        )
        self.assertNotIn("SECRET", json.dumps(windows))
        self.assertEqual(Codex.windows({"rateLimits": None}), [])
        for value in (None, True, -1, 101, float("nan"), "invalid"):
            with self.subTest(value=value):
                limits = {"five_hour": {"utilization": value, "resets_at": "2099-01-01T00:00:00Z"}}
                self.assertEqual(Claude.windows({"rate_limits": limits}), [])
        self.assertEqual(Claude.windows({}), [])

    def test_agents_refresh_independently_and_report_generic_issues(self):
        source = Limits({"limits": SETTINGS})
        windows = [
            dict(bucket=None, minutes=300, used_percent=0, resets_at=4102444800),
            dict(bucket=None, minutes=10080, used_percent=3, resets_at=4102963200),
        ]
        with (
            patch.object(Codex, "read_limits", new_callable=AsyncMock, return_value=[]) as codex,
            patch.object(
                Claude, "read_limits", new_callable=AsyncMock, return_value=windows
            ) as claude,
            patch.object(Codex, "command", return_value=["codex"]),
            patch.object(Claude, "command", return_value=["claude"]),
        ):
            first = {a["id"]: a for a in source.snapshot()["agents"]}
            self.assertEqual(first["claude"]["status"], "available")
            self.assertEqual(
                [w["id"] for w in first["claude"]["windows"]], ["claude::300", "claude::10080"]
            )
            self.assertEqual(first["codex"]["status"], "unavailable")
            source.snapshot()
            self.assertEqual((codex.await_count, claude.await_count), (1, 1))
            source.checked["claude"] -= 61
            windows[1]["used_percent"] = 7
            refreshed = {a["id"]: a for a in source.snapshot()["agents"]}
            self.assertEqual(refreshed["claude"]["windows"][1]["used_percent"], 7)
            self.assertEqual((codex.await_count, claude.await_count), (1, 2))
            for error, issue in (
                (RuntimeError("SECRET"), "query_failed"),
                (FileNotFoundError("PRIVATE"), "not_found"),
                (AgentUnavailable("login_required"), "login_required"),
                (AgentUnavailable("unsupported_version"), "unsupported_version"),
                (TimeoutError("SECRET"), "timeout"),
            ):
                source.invalidate(AGENTS["claude"])
                claude.side_effect = error
                failed = {a["id"]: a for a in source.snapshot()["agents"]}["claude"]
                self.assertEqual((failed["status"], failed["windows"]), ("unavailable", []))
                self.assertEqual(failed["issue"], issue)
                self.assertNotIn("SECRET", json.dumps(failed))
            # A stale reading stays visible but is marked; expired windows disappear.
            claude.side_effect = None
            source.invalidate(AGENTS["claude"])
            source.snapshot()
            source.results["claude"]["updated_at"] -= SETTINGS["stale_seconds"] + 1
            self.assertEqual(source.snapshot()["agents"][0]["status"], "stale")
            source.results["claude"]["windows"][0]["resets_at"] = time.time() - 1
            self.assertEqual(len(source.snapshot()["agents"][0]["windows"]), 1)

    def test_terminal_login_queries_never_send_prompts_and_stop_on_failures(self):
        processes, tracking = tracked(self)
        with tempfile.TemporaryDirectory() as directory, tracking:
            script = Path(directory) / "native.py"
            script.write_text(
                "import json, os, sys, time\nassert not os.listdir('.')\nmode=sys.argv[1]\nassert '--safe-mode' in sys.argv and '--no-session-persistence' in sys.argv\nfor line in sys.stdin:\n    msg=json.loads(line)\n    assert msg['type']=='control_request'\n    kind=msg['request']['subtype']\n    assert kind in ('initialize', 'get_usage')\n    response={'subtype':'success','request_id':msg['request_id']}\n    if kind=='initialize':\n        response['response']={'account':{'tokenSource':'none'} if mode=='signed-out' else {'email':'PRIVATE'}}\n    else:\n        assert msg['request']['skip_behaviors'] is True\n        if mode=='hang': time.sleep(30)\n        if mode=='unsupported': response.update(subtype='error',error='Unsupported control request subtype: get_usage')\n        elif mode=='permission':\n            print(json.dumps({'type':'control_request','request':{'subtype':'can_use_tool'}}),flush=True)\n            time.sleep(30)\n        else: response['response']={'rate_limits_available':mode!='api-key','rate_limits':{'five_hour':{'utilization':0,'resets_at':'2099-01-01T00:00:00Z'}}}\n    print(json.dumps({'type':'control_response','response':response}),flush=True)\n"
            )
            windows = asyncio.run(Claude().read_limits([sys.executable, str(script), "ok"], 5))
            self.assertEqual([w["used_percent"] for w in windows], [0])
            self.assertIsNotNone(processes[-1].returncode)
            for mode, error, message in (
                ("signed-out", AgentUnavailable, "login_required"),
                ("api-key", AgentUnavailable, "unsupported_account"),
                ("unsupported", AgentUnavailable, "unsupported_version"),
                ("permission", RuntimeError, "Unexpected native permission request"),
                ("hang", TimeoutError, ""),
            ):
                with self.assertRaisesRegex(error, message):
                    asyncio.run(Claude().read_limits([sys.executable, str(script), mode], 0.3))
                self.assertIsNotNone(processes[-1].returncode)

    def test_browser_login_agent_handshake_and_timeout_cleanup(self):
        processes, tracking = tracked(self)
        with tempfile.TemporaryDirectory() as directory, tracking:
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
            self.assertEqual(
                asyncio.run(Codex().read_limits([sys.executable, str(script), "ok"], 5)), []
            )
            self.assertIsNotNone(processes[-1].returncode)
            for mode, issue in (
                ("signed-out", "login_required"),
                ("api-key", "unsupported_account"),
            ):
                with self.assertRaisesRegex(AgentUnavailable, issue):
                    asyncio.run(Codex().read_limits([sys.executable, str(script), mode], 5))
                self.assertIsNotNone(processes[-1].returncode)
            with self.assertRaises(TimeoutError):
                asyncio.run(Codex().read_limits([sys.executable, str(script), "hang"], 0.2))
            self.assertIsNotNone(processes[-1].returncode)
