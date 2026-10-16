"""Subscription snapshots from native interfaces; no credential handling."""

import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .integration import agent_command, claude_status
from .readers import number


class CodexUnavailable(RuntimeError):
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
        item = window(
            "Claude Code", minutes, source.get("used_percentage"), source.get("resets_at")
        )
        if item:
            windows.append(item)
    return dict(
        agent="Claude Code", source="Native terminal status line", updated_at=now, windows=windows
    )


async def read_codex(command, timeout, *, login=None, cancelled=None):
    """Read native limits; start browser login only when explicitly requested."""
    process = await asyncio.create_subprocess_exec(
        *[
            str(Path(arg).expanduser()) if arg.startswith("~") else arg
            for arg in agent_command("codex", command)
        ],
        "app-server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=1024 * 1024,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

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
                raise CodexUnavailable("login_cancelled")
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
                        "version": "0.1.2",
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
                            raise CodexUnavailable("login_cancelled")
                        login_id = None
                        break
                deadline.reschedule(asyncio.get_running_loop().time() + timeout)
                account = await request(4, "account/read", {"refreshToken": False})
            if account.get("account") is None and account.get("requiresOpenaiAuth"):
                raise CodexUnavailable("login_required")
            if (account.get("account") or {}).get("type") in ("apiKey", "amazonBedrock"):
                raise CodexUnavailable("unsupported_account")
            return await request(5, "account/rateLimits/read", {})
    finally:
        if login_id is not None and process.returncode is None:
            try:
                await send(dict(id=6, method="account/login/cancel", params={"loginId": login_id}))
            except (OSError, RuntimeError):
                pass
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            process.kill()
            await process.wait()


def capture_claude(path, payload):
    """Store only quota fields, atomically; never persist the full status payload."""
    snapshot = claude_snapshot(payload, time.time())
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            json.dump(snapshot, stream, allow_nan=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return snapshot


class Subscriptions:
    def __init__(self, config, *, config_path=None):
        self.config = config
        self.config_path = config_path
        self.lock = threading.Lock()
        self.login_lock = threading.Lock()
        self.cancelled = threading.Event()
        self.closed = False
        self.checked = None
        self.codex = None

    def query_codex(self, **options):
        try:
            payload = asyncio.run(
                read_codex(self.config["codex_command"], self.config["timeout_seconds"], **options)
            )
            return codex_snapshot(payload, time.time())
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
                if isinstance(exc, CodexUnavailable)
                else "not_found"
                if isinstance(exc, FileNotFoundError)
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "query_failed"
            )
            return dict(
                agent="Codex",
                source="Native account limits",
                updated_at=None,
                windows=[],
                issue=issue,
            )

    def connect_codex(self, open_browser):
        if not self.login_lock.acquire(blocking=False):
            raise RuntimeError("Connection already in progress")
        try:
            self.cancelled.clear()
            if self.closed:
                self.cancelled.set()
            result = self.query_codex(login=open_browser, cancelled=self.cancelled)
            with self.lock:
                self.codex = result
                self.checked = time.monotonic()
        finally:
            self.login_lock.release()
        return result

    def cancel_login(self, *, close=False):
        self.closed = self.closed or close
        self.cancelled.set()

    def snapshot(self, connections=None):
        connections = connections or {}
        with self.lock:
            if connections.get("codex") is not False and (
                self.checked is None
                or time.monotonic() - self.checked >= self.config["refresh_seconds"]
            ):
                self.codex = self.query_codex()
                self.checked = time.monotonic()
            try:
                raw = (
                    {}
                    if connections.get("claude") is False
                    else json.loads(
                        Path(self.config["claude_file"]).expanduser().read_text(encoding="utf-8")
                    )
                )
                # Revalidate the local snapshot; serve no arbitrary captured fields.
                claude = dict(
                    agent="Claude Code",
                    source="Native terminal status line",
                    updated_at=number(raw.get("updated_at")),
                    windows=[],
                )
                for source in raw.get("windows", []):
                    item = window(
                        "Claude Code",
                        source.get("minutes"),
                        source.get("used_percent"),
                        source.get("resets_at"),
                    )
                    if item:
                        claude["windows"].append(item)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                claude = dict(
                    agent="Claude Code",
                    source="Native terminal status line",
                    updated_at=None,
                    windows=[],
                )
            now = time.time()
            issue = (
                "disconnected"
                if connections.get("claude") is False
                else claude_status(self.config_path)
            )
            claude["issue"] = (
                "claude_no_limits"
                if issue == "claude_waiting"
                and claude["updated_at"] is not None
                and not claude["windows"]
                else issue
            )
            providers = []
            codex = self.codex or dict(agent="Codex", windows=[], updated_at=None)
            for provider in (codex, claude):
                result = dict(provider)
                key = "codex" if provider["agent"] == "Codex" else "claude"
                if connections.get(key) is False:
                    result.update(
                        windows=[],
                        updated_at=None,
                        status="unavailable",
                        issue="disconnected",
                        connected=False,
                    )
                    providers.append(result)
                    continue
                result["windows"] = [w for w in provider["windows"] if w["resets_at"] > now]
                age = now - provider["updated_at"] if provider["updated_at"] is not None else None
                result["status"] = (
                    "unavailable"
                    if not result["windows"]
                    else (
                        "stale"
                        if age is None or age < -5 or age > self.config["stale_seconds"]
                        else "available"
                    )
                )
                result["connected"] = connections.get(key, bool(result["windows"]))
                providers.append(result)
            return dict(providers=providers, refresh_seconds=self.config["refresh_seconds"])
