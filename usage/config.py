"""Portable defaults and per-user configuration, independent of the checkout."""

import logging
import os
import sys
import tomllib
from pathlib import Path

from .agents import AGENTS

RETIRED = ("subscriptions", "transcripts")


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
    if path is not None and not Path(path).is_file():
        raise FileNotFoundError(path)
    path = Path(path) if path else user_directory("config") / "config.toml"
    if path.is_file():
        for section, values in tomllib.loads(path.read_text(encoding="utf-8")).items():
            if section in RETIRED:
                logging.warning(
                    "Ignoring retired [%s] settings; see [agents] and [limits].", section
                )
                continue
            config.setdefault(section, {}).update(values)
    if any(type(value) is not bool for value in config["integrations"].values()):
        raise ValueError("Integration settings must be booleans")
    if not 1 <= config["collection"]["refresh_seconds"] <= 300:
        raise ValueError("Invalid collection interval")
    limits = config["limits"]
    if not 10 <= limits["refresh_seconds"] <= limits["stale_seconds"]:
        raise ValueError("Invalid limit refresh interval")
    if not 1 <= limits["timeout_seconds"] <= 30:
        raise ValueError("Invalid limit query timeout")
    for agent, values in config["agents"].items():
        if (
            agent not in AGENTS
            or not isinstance(values, dict)
            or set(values) - {"command", "history"}
        ):
            raise ValueError("Unknown agent setting")
        command = values.get("command")
        if command is not None and (
            not isinstance(command, list)
            or not command
            or not all(isinstance(arg, str) and arg for arg in command)
        ):
            raise ValueError("Agent command must be a nonempty argument list")
        if "history" in values and (
            not isinstance(values["history"], str) or not values["history"]
        ):
            raise ValueError("Agent history must be a directory path")
    if not 280 <= config["widget"]["width"] <= 800:
        raise ValueError("Invalid widget size")
    return config
