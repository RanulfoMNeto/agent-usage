"""Account limits from each agent's native client; no credential handling."""

import asyncio
import threading
import time

from .agents import AGENTS, AgentUnavailable, Login


def window_id(agent, item):
    """Stable across refreshes, so the top bar can remember a chosen limit."""
    return f"{agent.id}:{item['bucket'] or ''}:{item['minutes']:g}"


class Limits:
    def __init__(self, config, agents=AGENTS):
        self.settings = config["limits"]
        self.overrides = config.get("agents", {})
        self.agents = agents
        self.lock = threading.Lock()
        self.login_lock = threading.Lock()
        self.cancelled = threading.Event()
        self.closed = False
        self.checked = {}
        self.results = {}

    def command(self, agent):
        return agent.command(self.overrides.get(agent.id, {}).get("command"))

    def query(self, agent, login=None):
        try:
            windows = asyncio.run(
                agent.read_limits(self.command(agent), self.settings["timeout_seconds"], login)
            )
            return dict(updated_at=time.time(), windows=windows)
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
            return dict(updated_at=None, windows=[], issue=issue)

    def connect(self, agent, open_url):
        """Sign in through the browser for agents whose native client offers it."""
        if not self.login_lock.acquire(blocking=False):
            raise RuntimeError("Connection already in progress")
        try:
            self.cancelled.clear()
            if self.closed:
                self.cancelled.set()
            result = self.query(agent, Login(open_url, self.cancelled))
            with self.lock:
                self.results[agent.id] = result
                self.checked[agent.id] = time.monotonic()
        finally:
            self.login_lock.release()
        return result

    def cancel_login(self, *, close=False):
        self.closed = self.closed or close
        self.cancelled.set()

    def invalidate(self, agent):
        with self.lock:
            self.checked.pop(agent.id, None)

    def snapshot(self, connections=None):
        connections = connections or {}
        with self.lock:
            results = []
            for agent in self.agents.values():
                if connections.get(agent.id) is False:
                    results.append(
                        dict(
                            id=agent.id,
                            windows=[],
                            updated_at=None,
                            status="unavailable",
                            issue="disconnected",
                            connected=False,
                        )
                    )
                    continue
                checked = self.checked.get(agent.id)
                if (
                    checked is None
                    or time.monotonic() - checked >= self.settings["refresh_seconds"]
                ):
                    self.results[agent.id] = self.query(agent)
                    self.checked[agent.id] = time.monotonic()
                result = dict(self.results[agent.id], id=agent.id)
                now = time.time()
                result["windows"] = [
                    dict(item, id=window_id(agent, item))
                    for item in result["windows"]
                    if item["resets_at"] > now
                ]
                age = now - result["updated_at"] if result["updated_at"] is not None else None
                result["status"] = (
                    "unavailable"
                    if not result["windows"]
                    else "stale"
                    if age is None or age < -5 or age > self.settings["stale_seconds"]
                    else "available"
                )
                result["connected"] = connections.get(agent.id, bool(result["windows"]))
                results.append(result)
            return dict(agents=results)
