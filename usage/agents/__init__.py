"""Supported agents. Adding one means a module implementing Agent and an entry here."""

from .base import Agent, AgentUnavailable, Login
from .claude import Claude
from .codex import Codex

AGENTS = {agent.id: agent for agent in (Claude(), Codex())}

__all__ = ["AGENTS", "Agent", "AgentUnavailable", "Login"]
