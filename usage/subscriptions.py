"""Subscription snapshots from native interfaces; no credential handling."""

import asyncio
import json
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .integration import agent_command
from .readers import number


class AgentUnavailable(RuntimeError):
    """A known account condition, without native error text or credentials."""


def validate_login_url(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or url.hostname not in ("auth.openai.com", "chatgpt.com")
        or url.username is not None
        or url.password is not None
        or url.port not in (None, 443)
    ):
        raise ValueError("Invalid native login URL")
    return value


def window(name, minutes, used, resets):
    """Keep incomplete and invalid observations out of quota indicators."""
    minutes, used, resets = map(number, (minutes, used, resets))
    if None in (minutes, used, resets) or minutes <= 0 or used > 100 or resets <= 0:
        return None
    return dict(name=name, minutes=minutes, used_percent=used, resets_at=resets)


def codex_snapshot(payload, now):
    buckets = payload.get("rateLimitsByLimitId")
    if buckets is None:
        buckets = {"codex": payload.get("rateLimits") or {}}
    windows = []
    for key, bucket in buckets.items():
        for kind in ("primary", "secondary"):
            source = bucket.get(kind) or {}
            item = window(
                bucket.get("limitName") or key,
                source.get("windowDurationMins"),
                source.get("usedPercent"),
                source.get("resetsAt"),
            )
            if item:
                windows.append(item)
    return dict(agent="Codex", source="Native account limits", updated_at=now, windows=windows)


def claude_snapshot(payload, now):
    windows = []
    limits = payload.get("rate_limits") or {}
    for key, minutes in (("five_hour", 300), ("seven_day", 10080)):
        source = limits.get(key) or {}
        try:
            resets = datetime.fromisoformat(source["resets_at"].replace("Z", "+00:00"))
            if resets.tzinfo is None:
                continue
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        item = window("Claude Code", minutes, source.get("utilization"), resets.timestamp())
        if item:
            windows.append(item)
    return dict(
        agent="Claude Code", source="Native account limits", updated_at=now, windows=windows
    )


async def start_native(agent, command, *args, cwd=None):
    return await asyncio.create_subprocess_exec(
        *[
            str(Path(arg).expanduser()) if arg.startswith("~") else arg
            for arg in agent_command(agent, command)
        ],
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=1024 * 1024,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


async def stop_native(process):
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        process.kill()
        await process.wait()


async def read_codex(command, timeout, *, login=None, cancelled=None):
    """Read native limits; start browser login only when explicitly requested."""
    process = await start_native("codex", command, "app-server")

    async def send(message):
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def request(identifier, method, params):
        await send(dict(id=identifier, method=method, params=params))
        while True:
            message = await receive()
            if message.get("id") == identifier:
                if "error" in message:
                    raise RuntimeError("Native account query failed")
                return message["result"]

    async def receive():
        while True:
            if cancelled is not None and cancelled.is_set():
                raise AgentUnavailable("login_cancelled")
            try:
                line = await asyncio.wait_for(process.stdout.readline(), 0.25)
            except TimeoutError:
                continue
            if not line:
                raise RuntimeError("Native account connection closed")
            return json.loads(line)

    login_id = None
    try:
        async with asyncio.timeout(timeout) as deadline:
            await request(
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "usage",
                        "title": "Agent Usage",
                        "version": "0.1.3",
                    },
                },
            )
            await send({"method": "initialized"})
            account = await request(2, "account/read", {"refreshToken": login is not None})
            if login is not None and (account.get("account") or {}).get("type") != "chatgpt":
                started = await request(3, "account/login/start", {"type": "chatgpt"})
                login_id = started["loginId"]
                login(validate_login_url(started["authUrl"]))
                deadline.reschedule(asyncio.get_running_loop().time() + 300)
                while True:
                    message = await receive()
                    params = message.get("params") or {}
                    if (
                        message.get("method") == "account/login/completed"
                        and params.get("loginId") == login_id
                    ):
                        if params.get("success") is not True:
                            raise AgentUnavailable("login_cancelled")
                        login_id = None
                        break
                deadline.reschedule(asyncio.get_running_loop().time() + timeout)
                account = await request(4, "account/read", {"refreshToken": False})
            if account.get("account") is None and account.get("requiresOpenaiAuth"):
                raise AgentUnavailable("login_required")
            if (account.get("account") or {}).get("type") in ("apiKey", "amazonBedrock"):
                raise AgentUnavailable("unsupported_account")
            return await request(5, "account/rateLimits/read", {})
    finally:
        if login_id is not None and process.returncode is None:
            try:
                await send(dict(id=6, method="account/login/cancel", params={"loginId": login_id}))
            except (OSError, RuntimeError):
                pass
        await stop_native(process)


async def read_claude(command, timeout):
    """Use the extension's experimental get_usage control request, without a model turn."""
    # An empty working directory and safe mode prevent loading project tools or hooks.
    with tempfile.TemporaryDirectory(prefix="agent-usage-") as directory:
        process = await start_native(
            "claude",
            command,
            "--safe-mode",
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            cwd=directory,
        )

        async def request(kind, **params):
            message = dict(
                type="control_request", request_id=kind, request=dict(subtype=kind, **params)
            )
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise AgentUnavailable("unsupported_version")
                message = json.loads(line)
                if message.get("type") == "control_request":
                    raise RuntimeError("Unexpected native permission request")
                response = message.get("response") or {}
                if message.get("type") == "control_response" and response.get("request_id") == kind:
                    if response.get("subtype") != "success":
                        if "Unsupported control request" in str(response.get("error")):
                            raise AgentUnavailable("unsupported_version")
                        raise RuntimeError("Native account query failed")
                    return response["response"]

        try:
            async with asyncio.timeout(timeout):
                initialized = await request("initialize")
                if (initialized.get("account") or {}).get("tokenSource") == "none":
                    raise AgentUnavailable("login_required")
                payload = await request("get_usage", skip_behaviors=True)
                if payload.get("rate_limits_available") is not True:
                    raise AgentUnavailable("unsupported_account")
                return payload
        finally:
            await stop_native(process)


class Subscriptions:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.login_lock = threading.Lock()
        self.cancelled = threading.Event()
        self.closed = False
        self.checked = {}
        self.providers = {}

    def query(self, agent, **options):
        name, reader, normalize = {
            "codex": ("Codex", read_codex, codex_snapshot),
            "claude": ("Claude Code", read_claude, claude_snapshot),
        }[agent]
        try:
            payload = asyncio.run(
                reader(self.config[agent + "_command"], self.config["timeout_seconds"], **options)
            )
            return normalize(payload, time.time())
        except (
            OSError,
            ValueError,
            RuntimeError,
            TimeoutError,
            KeyError,
            TypeError,
            AttributeError,
        ) as exc:
            issue = (
                str(exc)
                if isinstance(exc, AgentUnavailable)
                else "not_found"
                if isinstance(exc, FileNotFoundError)
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "query_failed"
            )
            return dict(
                agent=name, source="Native account limits", updated_at=None, windows=[], issue=issue
            )

    def connect_codex(self, open_browser):
        if not self.login_lock.acquire(blocking=False):
            raise RuntimeError("Connection already in progress")
        try:
            self.cancelled.clear()
            if self.closed:
                self.cancelled.set()
            result = self.query("codex", login=open_browser, cancelled=self.cancelled)
            with self.lock:
                self.providers["codex"] = result
                self.checked["codex"] = time.monotonic()
        finally:
            self.login_lock.release()
        return result

    def cancel_login(self, *, close=False):
        self.closed = self.closed or close
        self.cancelled.set()

    def snapshot(self, connections=None):
        connections = connections or {}
        with self.lock:
            providers = []
            for agent, name in (("codex", "Codex"), ("claude", "Claude Code")):
                if connections.get(agent) is False:
                    providers.append(
                        dict(
                            agent=name,
                            windows=[],
                            updated_at=None,
                            status="unavailable",
                            issue="disconnected",
                            connected=False,
                        )
                    )
                    continue
                checked = self.checked.get(agent)
                if checked is None or time.monotonic() - checked >= self.config["refresh_seconds"]:
                    self.providers[agent] = self.query(agent)
                    self.checked[agent] = time.monotonic()
                result = dict(self.providers[agent])
                now = time.time()
                result["windows"] = [w for w in result["windows"] if w["resets_at"] > now]
                age = now - result["updated_at"] if result["updated_at"] is not None else None
                result["status"] = (
                    "unavailable"
                    if not result["windows"]
                    else "stale"
                    if age is None or age < -5 or age > self.config["stale_seconds"]
                    else "available"
                )
                result["connected"] = connections.get(agent, bool(result["windows"]))
                providers.append(result)
            return dict(providers=providers)
