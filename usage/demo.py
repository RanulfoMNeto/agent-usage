"""Fictional native sessions in a disposable profile; no agent process or login."""

import json
import os
import random
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .agents import AGENTS
from .config import load_config
from .limits import window_id

TITLES = (
    "Add keyboard navigation",
    "Review the settings panel",
    "Fix empty-day selection",
    "Explain the session parser",
    "Add regression tests for imports",
    "Profile the history refresh",
    "Tidy the settings dialog",
    "Check the release workflow",
    "Improve chart accessibility",
    "Review the dependency update",
    "Handle interrupted sessions",
    "Prepare the release notes",
)


class DemoLimits:
    def __init__(self, agents=AGENTS):
        self.agents = agents
        self.started_at = time.time()
        self.activated = {}

    def _windows_for(self, agent, now):
        items = [
            dict(bucket=b, minutes=m, used_percent=u, resets_at=self.started_at + r)
            for b, m, u, r in agent.demo_limits
        ]
        extra = self.activated.get(agent.id)
        if extra and extra["resets_at"] > now and not any(w["minutes"] < 10080 for w in items):
            items.insert(0, dict(extra))
        return [dict(item, id=window_id(agent, item)) for item in items if item["resets_at"] > now]

    def activate(self, agent, now=None):
        now = time.time() if now is None else float(now)
        windows = self._windows_for(agent, now)
        if any(w["used_percent"] >= 100 for w in windows):
            return dict(activated=False, reason="weekly_exhausted", windows=windows)
        if any(w["minutes"] < 10080 and w["used_percent"] > 0 for w in windows):
            return dict(activated=False, reason="already_active", windows=windows)
        bucket = agent.demo_limits[0][0] if agent.demo_limits else None
        minutes = getattr(agent, "window_minutes", 300)
        self.activated[agent.id] = dict(
            bucket=bucket, minutes=minutes, used_percent=1, resets_at=now + minutes * 60
        )
        return dict(activated=True, reason="activated", windows=self._windows_for(agent, now))

    def snapshot(self, connections=None):
        now = time.time()
        return {
            "agents": [
                dict(
                    id=agent.id,
                    status="available",
                    updated_at=now,
                    connected=True,
                    windows=self._windows_for(agent, now),
                )
                for agent in self.agents.values()
            ]
        }


@contextmanager
def profile(agents=AGENTS):
    """Keep sessions, settings, history and credentials outside the user's profile."""
    with tempfile.TemporaryDirectory(prefix="agent-usage-demo-") as folder:
        root = Path(folder)
        overrides = {"XDG_CONFIG_HOME": folder, "XDG_STATE_HOME": folder, "LOCALAPPDATA": folder}
        original = {key: os.environ.get(key) for key in overrides}
        os.environ.update(overrides)
        try:
            config = load_config()
            config["widget"]["demo"] = True
            members = list(agents.values())
            for agent in members:
                (root / agent.id).mkdir()
                config["agents"][agent.id] = {"history": str(root / agent.id)}
            now = datetime.now().astimezone()
            rng = random.Random(42)
            for day in range(180):
                date = now - timedelta(days=day)
                if day and rng.randrange(7) == 0:
                    continue
                prompts = 24 if day == 0 else rng.randint(3, 9 if date.weekday() >= 5 else 30)
                intensity = rng.uniform(0.25, 2.2)
                end = now - timedelta(minutes=8) if day == 0 else date.replace(hour=18, minute=0)
                for index in range(prompts):
                    start = end - timedelta(minutes=(prompts - index) * 480 / prompts)
                    session = f"demo-{day}-{index}"
                    inp = int(rng.triangular(6000, 160000, 32000) * intensity)
                    tokens = dict(
                        input=inp,
                        output=rng.randint(400, 6500),
                        cached=int(inp * rng.uniform(0.35, 0.85)),
                    )
                    agent = members[index % len(members)]
                    events = agent.demo_session(
                        session,
                        TITLES[(index + day) % len(TITLES)],
                        start,
                        rng.randint(18, 360),
                        tokens,
                    )
                    (root / agent.id / f"{session}.jsonl").write_text(
                        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
                    )
            yield config, DemoLimits(agents)
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
