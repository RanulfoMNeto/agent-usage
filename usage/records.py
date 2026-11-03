"""History records shared by all agents, without retaining conversation bodies."""

import hashlib
import json
import math
import re
from datetime import datetime
from textwrap import shorten

TOKEN_KEYS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cached": "cached_input_tokens",
    "write": "cache_write_input_tokens",
    "reasoning": "reasoning_output_tokens",
}


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


def question(content):
    """A short title for a prompt; the full text is never stored."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    else:
        text = ""
    # Native clients wrap annotations before the actual question.
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
    """Standard API price for a call: a comparison, not subscription billing."""
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


def identity(agent, kind, key):
    return hashlib.sha256(f"{agent.namespace}:{kind}:{key}".encode()).hexdigest()


def counter_key(state, turn, total):
    return json.dumps(
        [state["session"], turn, [number(total.get(k, 0)) for k in TOKEN_KEYS.values()]]
    )


def prompt(agent, session, turn, stamp):
    key = identity(agent, "prompt", turn)
    return dict(
        id=key,
        kind="prompt",
        agent=agent.id,
        session=session,
        turn=key,
        updated=stamp,
        start=stamp,
        end=None,
        title=None,
        status="observed",
        duration_ms=None,
        duration_exact=False,
    )


def call(agent, state, key, turn, stamp, usage):
    """A model call; usage uses TOKEN_KEYS' native names, cached and written within input."""
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
        agent=agent.id,
        session=state["session"],
        turn=identity(agent, "prompt", turn),
        updated=stamp,
        model=state.get("model"),
        native_cost=None,
        counter=None,
        **values,
    )
