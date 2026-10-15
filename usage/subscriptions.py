"""Subscription snapshots from native interfaces; no credential handling."""

import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .readers import number


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


async def read_codex(command, timeout):
    """Use the documented stdio handshake, query limits, and close the process."""
    process = await asyncio.create_subprocess_exec(
        *[str(Path(arg).expanduser()) if arg.startswith("~") else arg for arg in command],
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
        while line := await process.stdout.readline():
            message = json.loads(line)
            if message.get("id") == identifier:
                if "error" in message:
                    raise RuntimeError("Native account query failed")
                return message["result"]
        raise RuntimeError("Native account connection closed")

    try:
        async with asyncio.timeout(timeout):
            await request(
                1,
                "initialize",
                {
                    "clientInfo": {
                        "name": "usage",
                        "title": "Agent Usage",
                        "version": "0.1.0",
                    },
                },
            )
            await send({"method": "initialized"})
            return await request(2, "account/rateLimits/read", {})
    finally:
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
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.checked = None
        self.codex = None

    def snapshot(self):
        with self.lock:
            if (
                self.checked is None
                or time.monotonic() - self.checked >= self.config["refresh_seconds"]
            ):
                try:
                    payload = asyncio.run(
                        read_codex(self.config["codex_command"], self.config["timeout_seconds"])
                    )
                    self.codex = codex_snapshot(payload, time.time())
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    TimeoutError,
                    KeyError,
                    TypeError,
                    AttributeError,
                ):
                    self.codex = dict(
                        agent="Codex", source="Native account limits", updated_at=None, windows=[]
                    )
                self.checked = time.monotonic()
            try:
                raw = json.loads(
                    Path(self.config["claude_file"]).expanduser().read_text(encoding="utf-8")
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
            providers = []
            for provider in (self.codex, claude):
                result = dict(provider)
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
                providers.append(result)
            return dict(providers=providers, refresh_seconds=self.config["refresh_seconds"])
