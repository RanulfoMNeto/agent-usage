"""Fictional native sessions in a disposable profile; no agent process or login."""

import json
import os
import random
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .config import load_config


class DemoLimits:
    def __init__(self):
        self.started_at = time.time()

    def snapshot(self, connections=None):
        now = time.time()
        return {
            "providers": [
                dict(
                    agent=agent,
                    source="Demo",
                    status="available",
                    updated_at=now,
                    windows=[
                        dict(
                            name=agent,
                            minutes=minutes,
                            used_percent=used,
                            resets_at=self.started_at + remaining,
                        )
                        for minutes, used, remaining in limits
                    ],
                )
                for agent, limits in (
                    ("Codex", [(10080, 28, 172800)]),
                    ("Claude Code", [(300, 14, 7200), (10080, 42, 345600)]),
                )
            ]
        }


@contextmanager
def profile():
    """Keep sessions, settings, history and credentials outside the user's profile."""
    with tempfile.TemporaryDirectory(prefix="agent-usage-demo-") as folder:
        root = Path(folder)
        overrides = {
            "XDG_CONFIG_HOME": folder,
            "XDG_STATE_HOME": folder,
            "LOCALAPPDATA": folder,
            "CODEX_HOME": str(root / "codex"),
            "CLAUDE_CONFIG_DIR": str(root / "claude"),
        }
        original = {key: os.environ.get(key) for key in overrides}
        os.environ.update(overrides)
        try:
            config = load_config()
            config["widget"]["demo"] = True
            codex = root / "codex/sessions"
            claude = root / "claude/projects/demo"
            codex.mkdir(parents=True)
            claude.mkdir(parents=True)
            now = datetime.now().astimezone()
            rng = random.Random(42)
            titles = (
                "Add keyboard navigation",
                "Review the settings panel",
                "Fix empty-day selection",
                "Explain the session parser",
                "Add regression tests for imports",
                "Profile the history refresh",
                "Simplify the project filter",
                "Check the release workflow",
                "Improve chart accessibility",
                "Review the dependency update",
                "Handle interrupted sessions",
                "Prepare the release notes",
            )
            for day in range(180):
                date = now - timedelta(days=day)
                if day and rng.randrange(7) == 0:
                    continue
                prompts = 24 if day == 0 else rng.randint(3, 9 if date.weekday() >= 5 else 30)
                intensity = rng.uniform(0.25, 2.2)
                end = now - timedelta(minutes=8) if day == 0 else date.replace(hour=18, minute=0)
                for index in range(prompts):
                    title = titles[(index + day) % len(titles)]
                    start = end - timedelta(minutes=(prompts - index) * 480 / prompts)
                    sid = f"demo-{day}-{index}"
                    inp = int(rng.triangular(6000, 160000, 32000) * intensity)
                    out = rng.randint(400, 6500)
                    cached = int(inp * rng.uniform(0.35, 0.85))
                    seconds = rng.randint(18, 360)
                    is_claude = index % 4 in (1, 2)
                    timestamp = start.isoformat()
                    usage = dict(input_tokens=inp, output_tokens=out, cached_input_tokens=cached)
                    events = [
                        dict(type="session_meta", payload=dict(id=sid, cwd="/demo/project")),
                        dict(
                            type="turn_context",
                            timestamp=timestamp,
                            payload=dict(model="gpt-6-astra"),
                        ),
                        dict(
                            type="event_msg",
                            timestamp=timestamp,
                            payload=dict(type="task_started", turn_id=sid),
                        ),
                        dict(
                            type="event_msg",
                            timestamp=timestamp,
                            payload=dict(type="user_message", message=title),
                        ),
                        dict(
                            type="event_msg",
                            timestamp=timestamp,
                            payload=dict(
                                type="token_count",
                                info=dict(total_token_usage=usage, last_token_usage=usage),
                            ),
                        ),
                        dict(
                            type="event_msg",
                            timestamp=(start + timedelta(seconds=seconds)).isoformat(),
                            payload=dict(
                                type="task_complete",
                                turn_id=sid,
                                duration_ms=seconds * 1000,
                            ),
                        ),
                    ]
                    if is_claude:
                        events = [
                            dict(
                                type="user",
                                timestamp=timestamp,
                                uuid=sid,
                                sessionId=sid,
                                cwd="/demo/project",
                                message=dict(content=title),
                            ),
                            dict(
                                type="assistant",
                                timestamp=(start + timedelta(seconds=seconds)).isoformat(),
                                uuid=sid + "-answer",
                                parentUuid=sid,
                                sessionId=sid,
                                message=dict(
                                    id=sid + "-message",
                                    model="demo-model",
                                    stop_reason="end_turn",
                                    usage=dict(
                                        input_tokens=inp - cached,
                                        cache_read_input_tokens=cached,
                                        output_tokens=out,
                                    ),
                                ),
                            ),
                        ]
                    target = claude if is_claude else codex
                    (target / (sid + ".jsonl")).write_text(
                        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
                    )
            yield config, DemoLimits()
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
