"""Portable defaults and per-user configuration, independent of the checkout."""

import os
import sys
import tomllib
from pathlib import Path


def user_directory(kind):
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return root / "AgentUsage"
    fallback = Path.home() / (".config" if kind == "config" else ".local/state")
    variable = "XDG_CONFIG_HOME" if kind == "config" else "XDG_STATE_HOME"
    return Path(os.environ.get(variable) or fallback) / "agent-usage"


def load_config(path=None):
    config = tomllib.loads(Path(__file__).with_name("defaults.toml").read_text(encoding="utf-8"))
    state = user_directory("state")
    config["history"] = {"database": str(state / "history.sqlite3")}
    config["subscriptions"]["claude_file"] = str(state / "claude.json")
    if path is not None and not Path(path).is_file():
        raise FileNotFoundError(path)
    path = Path(path) if path else user_directory("config") / "config.toml"
    if path.is_file():
        for section, values in tomllib.loads(path.read_text(encoding="utf-8")).items():
            config.setdefault(section, {}).update(values)
    subscriptions = config["subscriptions"]
    if any(type(value) is not bool for value in config["integrations"].values()):
        raise ValueError("Integration settings must be booleans")
    if not 1 <= config["collection"]["refresh_seconds"] <= 300:
        raise ValueError("Invalid collection interval")
    if not 10 <= subscriptions["refresh_seconds"] <= subscriptions["stale_seconds"]:
        raise ValueError("Invalid subscription refresh interval")
    if not 1 <= subscriptions["timeout_seconds"] <= 30:
        raise ValueError("Invalid subscription timeout")
    for agent in ("codex", "claude"):
        command = subscriptions[agent + "_command"]
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg for arg in command)
        ):
            raise ValueError("Agent command must be a nonempty argument list")
    widget = config["widget"]
    if not 280 <= widget["width"] <= 800 or not 280 <= widget["compact_width"] <= 800:
        raise ValueError("Invalid widget size")
    return config
