"""Claude Code: project transcripts and limits from its usage control request."""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from ..records import call, number, prompt, question, timestamp
from .base import Agent, AgentUnavailable, start, stop, window


class Claude(Agent):
    id = "claude"
    name = "Claude Code"
    namespace = "Claude Code"
    colors = ("#d97757", "#d97757")
    executable = "claude"
    extension = "anthropic.claude-code"
    demo_limits = ((None, 300, 14, 7200), (None, 10080, 42, 345600))

    def extension_binary(self, target, name):
        return Path("resources/native-binary", name)

    def default_history(self):
        home = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(home) / "projects" if home else Path("~/.claude/projects").expanduser()

    def parse(self, event, state):
        kind, message = event.get("type"), event.get("message") or {}
        stamp = timestamp(event.get("timestamp"))
        if not stamp or not isinstance(message, dict):
            return []
        state["session"] = event.get("sessionId") or state.get("session")
        if not state.get("session"):
            return []
        parents = state.setdefault("parents", {})
        active = state.get("active")
        parent = parents.get(event.get("parentUuid")) or (active or {}).get("turn")
        if kind == "user" and not event.get("isMeta"):
            content = message.get("content")
            tool_result = isinstance(content, list) and any(
                c.get("type") == "tool_result" for c in content if isinstance(c, dict)
            )
            if not tool_result and event.get("uuid"):
                active = prompt(self, state["session"], event["uuid"], stamp)
                active.update(title=question(content))
                state["active"] = active
                parents[event["uuid"]] = active["turn"]
                return [active.copy()]
        if event.get("uuid") and parent:
            parents[event["uuid"]] = parent
        if kind == "assistant" and parent and not event.get("isApiErrorMessage"):
            usage = message.get("usage")
            if not isinstance(usage, dict) or not message.get("id"):
                return []
            state["model"] = message.get("model")
            values = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cached_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_write_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "reasoning_output_tokens": (usage.get("output_tokens_details") or {}).get(
                    "thinking_tokens", 0
                ),
            }
            # Native input excludes cache reads and writes; records count them within input.
            components = [
                number(values[k])
                for k in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens")
            ]
            values["input_tokens"] = sum(components) if None not in components else None
            record = call(self, state, message["id"], "unused", stamp, values)
            record["turn"] = parent
            records = [record]
            if active and active["turn"] == parent and message.get("stop_reason") == "end_turn":
                active.update(end=stamp, updated=stamp, status="completed")
                records.append(active.copy())
            return records
        if kind == "system" and event.get("subtype") == "turn_duration" and active:
            duration = number(event.get("durationMs"))
            active.update(
                end=stamp,
                updated=stamp,
                status="completed",
                duration_ms=duration,
                duration_exact=duration is not None,
            )
            return [active.copy()]
        return []

    @staticmethod
    def windows(payload):
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
            item = window(None, minutes, source.get("utilization"), resets.timestamp())
            if item:
                windows.append(item)
        return windows

    async def read_limits(self, command, timeout, login=None):
        """Use the experimental get_usage control request, without a model turn."""
        # An empty working directory and safe mode prevent loading project tools or hooks.
        with tempfile.TemporaryDirectory(prefix="agent-usage-") as directory:
            process = await start(
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
                    if (
                        message.get("type") == "control_response"
                        and response.get("request_id") == kind
                    ):
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
                    return self.windows(payload)
            finally:
                await stop(process)

    def demo_session(self, session, title, start, seconds, tokens):
        return [
            dict(
                type="user",
                timestamp=start.isoformat(),
                uuid=session,
                sessionId=session,
                message=dict(content=title),
            ),
            dict(
                type="assistant",
                timestamp=(start + timedelta(seconds=seconds)).isoformat(),
                uuid=session + "-answer",
                parentUuid=session,
                sessionId=session,
                message=dict(
                    id=session + "-message",
                    model="demo-model",
                    stop_reason="end_turn",
                    usage=dict(
                        input_tokens=tokens["input"] - tokens["cached"],
                        cache_read_input_tokens=tokens["cached"],
                        output_tokens=tokens["output"],
                    ),
                ),
            ),
        ]
