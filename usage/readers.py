"""Normalize native usage records without retaining conversation bodies."""

import hashlib
import json
import math
import ntpath
import posixpath
import re
from datetime import datetime
from textwrap import shorten


def timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def project_path(value):
    """Normalize recorded working directories without accessing the filesystem."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if any(ord(char) < 32 for char in value):
        return None
    drive, _ = ntpath.splitdrive(value)
    if drive and ntpath.isabs(value):
        return ntpath.normcase(ntpath.normpath(value)).replace("\\", "/")
    return posixpath.normpath(value) if value.startswith("/") else None


def question(content):
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    else:
        text = ""
    # The native client wraps annotations before the actual question.
    if "## My request:" in text:
        text = text.rsplit("## My request:", 1)[1]
    text = re.sub(r"<[^>]+>", " ", text)
    try:
        reply = json.loads(text)
        if (
            isinstance(reply, list)
            and reply
            and all(
                isinstance(item, dict) and "questionItemId" in item and "answer" in item
                for item in reply
            )
        ):
            text = " ".join(str(item["answer"]) for item in reply)
    except ValueError:
        pass
    text = re.sub(r"^s?elected prompt\b[\s:#]*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s*[#>*]+\s*", "", text)
    text = " ".join(text.split())
    return shorten(text, width=80, placeholder="…") or "Image or attachment prompt"


def estimate(call, prices):
    if call.get("native_cost") is not None:
        return call["native_cost"]
    price = prices.get(call.get("model"))
    values = [call.get(k) for k in ("input", "output", "cached", "write")]
    if not price or any(v is None for v in values):
        return None
    inp, out, cached, write = values
    long = inp > price.get("long_context_above", math.inf)
    im = price.get("long_input_multiplier", 1) if long else 1
    om = price.get("long_output_multiplier", 1) if long else 1
    return (
        (inp - cached - write) * price["input"] * im
        + cached * price["cached"] * im
        + write * price.get("write", price["input"]) * im
        + out * price["output"] * om
    ) / 1e6


TOKEN_KEYS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cached": "cached_input_tokens",
    "write": "cache_write_input_tokens",
    "reasoning": "reasoning_output_tokens",
}


def identity(agent, kind, key):
    return hashlib.sha256(f"{agent}:{kind}:{key}".encode()).hexdigest()


def counter_key(state, turn, total):
    return json.dumps(
        [state["session"], turn, [number(total.get(k, 0)) for k in TOKEN_KEYS.values()]]
    )


def prompt(agent, session, turn, stamp, project=None):
    key = identity(agent, "prompt", turn)
    return dict(
        id=key,
        kind="prompt",
        agent=agent,
        session=session,
        turn=key,
        updated=stamp,
        start=stamp,
        end=None,
        title=None,
        status="observed",
        duration_ms=None,
        duration_exact=False,
        project=project,
    )


def call(agent, state, key, turn, stamp, usage):
    values = {
        field: number(usage.get(native, 0 if field not in ("input", "output") else None))
        for field, native in TOKEN_KEYS.items()
    }
    if values["input"] is not None and values["cached"] is not None and values["write"] is not None:
        if values["cached"] + values["write"] > values["input"]:
            values["input"] = None
    return dict(
        id=identity(agent, "call", key),
        kind="call",
        agent=agent,
        session=state["session"],
        turn=identity(agent, "prompt", turn),
        updated=stamp,
        model=state.get("model"),
        native_cost=None,
        counter=None,
        **values,
    )


def codex(event, state):
    """Prefer response IDs; cumulative snapshots cover older native formats."""
    p = event.get("payload") or {}
    kind = p.get("type") if event.get("type") == "event_msg" else event.get("type")
    stamp = timestamp(event.get("timestamp"))
    if kind == "session_meta":
        state["session"] = p["id"]
        state["created"] = timestamp(p.get("timestamp"))
        state["fork"] = bool(p.get("forked_from_id"))
        state["project"] = project_path(p.get("cwd"))
        return []
    if not stamp or not state.get("session"):
        return []
    inherited = state.get("fork") and stamp < (state.get("created") or 0)
    if kind == "turn_context":
        state["model"] = p.get("model")
        if "cwd" in p:
            state["project"] = project_path(p["cwd"])
            if state.get("active") and state.get("own"):
                state["active"].update(project=state["project"], updated=stamp)
                return [state["active"].copy()]
    elif kind == "task_started":
        native = p.get("turn_id")
        if native:
            state["turn"] = p.get("root_turn_id") or native
            state["own"] = state["turn"] == native and not inherited
            state["active"] = prompt("Codex", state["session"], native, stamp, state.get("project"))
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
            record = call("Codex", state, p["response_id"], turn, stamp, p["usage"])
            if p.get("thread_token_usage"):
                record["counter"] = identity(
                    "Codex", "call", counter_key(state, turn, p["thread_token_usage"])
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
        record = call("Codex", state, key, state["turn"], stamp, usage)
        record["counter"] = record["id"]
        return [record]
    return []


def claude(event, state):
    kind, message = event.get("type"), event.get("message") or {}
    stamp = timestamp(event.get("timestamp"))
    if not stamp or not isinstance(message, dict):
        return []
    state["session"] = event.get("sessionId") or state.get("session")
    if not state.get("session"):
        return []
    if "cwd" in event:
        state["project"] = project_path(event["cwd"])
    parents = state.setdefault("parents", {})
    active = state.get("active")
    parent = parents.get(event.get("parentUuid")) or (active or {}).get("turn")
    if kind == "user" and not event.get("isMeta"):
        content = message.get("content")
        tool_result = isinstance(content, list) and any(
            c.get("type") == "tool_result" for c in content if isinstance(c, dict)
        )
        if not tool_result and event.get("uuid"):
            active = prompt(
                "Claude Code", state["session"], event["uuid"], stamp, state.get("project")
            )
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
        components = [
            number(values[k])
            for k in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens")
        ]
        values["input_tokens"] = sum(components) if None not in components else None
        record = call("Claude Code", state, message["id"], "unused", stamp, values)
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
