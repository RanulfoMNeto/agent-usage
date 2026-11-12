"""Codex: session logs under CODEX_HOME and limits from its app server."""

import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from .. import VERSION
from ..records import TOKEN_KEYS, call, counter_key, identity, number, prompt, question, timestamp
from .base import Agent, AgentUnavailable, start, stop, validate_login_url, window


class Codex(Agent):
    id = "codex"
    name = "Codex"
    namespace = "Codex"
    colors = ("#367b78", "#8bc8bf")
    monochrome = True
    executable = "codex"
    extension = "openai.chatgpt"
    login = "browser"
    login_hosts = ("auth.openai.com", "chatgpt.com")
    demo_limits = (("codex", 10080, 28, 172800),)

    def install_directories(self):
        if os.environ.get("CODEX_INSTALL_DIR"):
            return [Path(os.environ["CODEX_INSTALL_DIR"])]
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local"
            return [Path(local) / "Programs/OpenAI/Codex/bin"]
        return super().install_directories()

    def extension_binary(self, target, name):
        return Path("bin", target, name)

    def default_history(self):
        return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()

    def history_files(self, root):
        roots = [root / "sessions", root / "archived_sessions"]
        for directory in roots if any(path.is_dir() for path in roots) else [root]:
            yield from directory.rglob("*.jsonl")

    def parse(self, event, state):
        """Prefer response IDs; cumulative snapshots cover older native formats."""
        p = event.get("payload") or {}
        kind = p.get("type") if event.get("type") == "event_msg" else event.get("type")
        stamp = timestamp(event.get("timestamp"))
        if kind == "session_meta":
            state["session"] = p["id"]
            state["created"] = timestamp(p.get("timestamp"))
            state["fork"] = bool(p.get("forked_from_id"))
            return []
        if not stamp or not state.get("session"):
            return []
        inherited = state.get("fork") and stamp < (state.get("created") or 0)
        if kind == "turn_context":
            state["model"] = p.get("model")
        elif kind == "task_started":
            native = p.get("turn_id")
            if native:
                state["turn"] = p.get("root_turn_id") or native
                state["own"] = state["turn"] == native and not inherited
                state["active"] = prompt(self, state["session"], native, stamp)
                if state["own"]:
                    return [state["active"].copy()]
        elif kind in ("user_message", "item_completed") and state.get("active"):
            item = p.get("item", {})
            if state.get("own") and (kind == "user_message" or item.get("type") == "UserMessage"):
                active = state["active"]
                if active["title"] is None:
                    active.update(
                        title=question(
                            p.get("message") if kind == "user_message" else item.get("content")
                        ),
                        updated=stamp,
                    )
                    return [active.copy()]
        elif kind in ("task_complete", "turn_aborted") and state.get("active"):
            active = state.pop("active")
            if state.get("own"):
                duration = number(p.get("duration_ms"))
                active.update(
                    end=stamp,
                    updated=stamp,
                    duration_ms=duration,
                    duration_exact=duration is not None,
                    status="completed" if kind == "task_complete" else "interrupted",
                )
                return [active]
        elif kind == "token_usage_record":
            # Newer clients emit complete per-response records. Their cumulative
            # counters can include compaction usage absent from token_count; do not
            # combine the two streams after this format becomes available.
            if p.get("thread_id") and p.get("turn_token_usage") is not None:
                state["modern"] = True
            turn = p.get("root_turn_id") or p.get("turn_id") or state.get("turn")
            if not inherited and turn and p.get("response_id") and isinstance(p.get("usage"), dict):
                record = call(self, state, p["response_id"], turn, stamp, p["usage"])
                if p.get("thread_token_usage"):
                    record["counter"] = identity(
                        self, "call", counter_key(state, turn, p["thread_token_usage"])
                    )
                return [record]
        elif kind == "token_count":
            info = p.get("info") or {}
            current = info.get("total_token_usage")
            if not isinstance(current, dict):
                return []
            previous = state.get("total")
            state["total"] = current
            if inherited or state.get("modern") or not state.get("turn") or current == previous:
                return []
            if previous and all(
                number(current.get(k)) is not None
                and number(previous.get(k)) is not None
                and current[k] >= previous[k]
                for k in ("input_tokens", "output_tokens")
            ):
                usage = {k: current.get(k, 0) - previous.get(k, 0) for k in TOKEN_KEYS.values()}
            else:
                usage = info.get("last_token_usage") or {}
            if not any(usage.get(k, 0) for k in ("input_tokens", "output_tokens")):
                return []
            key = counter_key(state, state["turn"], current)
            record = call(self, state, key, state["turn"], stamp, usage)
            record["counter"] = record["id"]
            return [record]
        return []

    @staticmethod
    def windows(payload):
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
        return windows

    async def read_limits(self, command, timeout, login=None):
        """Read limits through the app server; sign in only when explicitly requested."""
        process = await start(command, "app-server")

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
                if login is not None and login.cancelled.is_set():
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
                    {"clientInfo": {"name": "usage", "title": "Agent Usage", "version": VERSION}},
                )
                await send({"method": "initialized"})
                account = await request(2, "account/read", {"refreshToken": login is not None})
                if login is not None and (account.get("account") or {}).get("type") != "chatgpt":
                    started = await request(3, "account/login/start", {"type": "chatgpt"})
                    login_id = started["loginId"]
                    login.open(validate_login_url(started["authUrl"], self.login_hosts))
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
                return self.windows(await request(5, "account/rateLimits/read", {}))
        finally:
            if login_id is not None and process.returncode is None:
                try:
                    await send(
                        dict(id=6, method="account/login/cancel", params={"loginId": login_id})
                    )
                except (OSError, RuntimeError):
                    pass
            await stop(process)

    async def activate_window(self, command, timeout):
        """Start a usage window through an ephemeral read-only turn on the app server."""
        with tempfile.TemporaryDirectory(prefix="agent-usage-") as directory:
            process = await start(command, "app-server", cwd=directory)

            async def send(message):
                process.stdin.write((json.dumps(message) + "\n").encode())
                await process.stdin.drain()

            async def receive():
                line = await process.stdout.readline()
                if not line:
                    raise RuntimeError("Native account connection closed")
                return json.loads(line)

            async def request(identifier, method, params):
                await send(dict(id=identifier, method=method, params=params))
                while True:
                    message = await receive()
                    if message.get("id") == identifier:
                        if "error" in message:
                            raise RuntimeError("Native account query failed")
                        return message["result"]

            try:
                async with asyncio.timeout(timeout):
                    await request(
                        1,
                        "initialize",
                        {
                            "clientInfo": {
                                "name": "usage",
                                "title": "Agent Usage",
                                "version": VERSION,
                            }
                        },
                    )
                    await send({"method": "initialized"})
                    account = await request(2, "account/read", {"refreshToken": False})
                    if account.get("account") is None and account.get("requiresOpenaiAuth"):
                        raise AgentUnavailable("login_required")
                    if (account.get("account") or {}).get("type") in ("apiKey", "amazonBedrock"):
                        raise AgentUnavailable("unsupported_account")
                    windows = self.windows(await request(3, "account/rateLimits/read", {}))
                    if any(w["used_percent"] >= 100 for w in windows):
                        return dict(activated=False, reason="weekly_exhausted", windows=windows)
                    if any(w["minutes"] < 10080 and w["used_percent"] > 0 for w in windows):
                        return dict(activated=False, reason="already_active", windows=windows)
                    thread = await request(
                        4,
                        "thread/start",
                        {
                            "ephemeral": True,
                            "approvalPolicy": "never",
                            "sandbox": "read-only",
                            "baseInstructions": "Reply with ok.",
                        },
                    )
                    thread_id = (
                        (thread.get("thread") or {}).get("id")
                        or thread.get("threadId")
                        or "ephemeral"
                    )
                    await request(
                        5,
                        "turn/start",
                        {"threadId": thread_id, "input": [{"type": "text", "text": "."}]},
                    )
                    while True:
                        message = await receive()
                        method = message.get("method")
                        params = message.get("params") or {}
                        if method in (
                            "turn/completed",
                            "codex/event/task_complete",
                            "task_complete",
                        ):
                            turn = params.get("turn") or params
                            if turn.get("status") in ("failed", "error"):
                                raise RuntimeError("Native activation failed")
                            break
                    try:
                        after = await request(6, "account/rateLimits/read", {})
                        windows = self.windows(after) or windows
                    except (RuntimeError, OSError):
                        pass
                    return dict(activated=True, reason="activated", windows=windows)
            finally:
                await stop(process)

    def demo_session(self, session, title, start, seconds, tokens):
        stamp = start.isoformat()
        usage = dict(
            input_tokens=tokens["input"],
            output_tokens=tokens["output"],
            cached_input_tokens=tokens["cached"],
        )
        return [
            dict(type="session_meta", payload=dict(id=session)),
            dict(type="turn_context", timestamp=stamp, payload=dict(model="gpt-6-astra")),
            dict(
                type="event_msg",
                timestamp=stamp,
                payload=dict(type="task_started", turn_id=session),
            ),
            dict(
                type="event_msg", timestamp=stamp, payload=dict(type="user_message", message=title)
            ),
            dict(
                type="event_msg",
                timestamp=stamp,
                payload=dict(
                    type="token_count",
                    info=dict(total_token_usage=usage, last_token_usage=usage),
                ),
            ),
            dict(
                type="event_msg",
                timestamp=(start + timedelta(seconds=seconds)).isoformat(),
                payload=dict(type="task_complete", turn_id=session, duration_ms=seconds * 1000),
            ),
        ]
