"""The contract every agent implements, and the native helpers they share."""

import asyncio
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable
from urllib.parse import urlsplit

from ..records import number


class AgentUnavailable(RuntimeError):
    """A known account condition, without native error text or credentials."""


@dataclass
class Login:
    """A browser sign-in requested by the user; the widget opens validated URLs."""

    open: Callable[[str], None]
    cancelled: Event


class Agent:
    """One coding agent: where its history lives, how to read it, and its account limits.

    Subclasses set the class attributes and implement the methods that raise
    NotImplementedError. Everything else in the app works from this interface.
    """

    id = ""
    """Stable key in settings, history records and the top bar."""
    name = ""
    """Display name."""
    namespace = ""
    """Hashed into history record identities; never change it once released."""
    colors = ("", "")
    """Accent color on light and dark themes."""
    monochrome = False
    """A single-color icon, inverted on dark themes."""
    executable = ""
    """Command name searched on PATH."""
    extension = ""
    """Editor extension directory prefix that bundles the executable, if any."""
    login = "terminal"
    """How the user signs in: "terminal" runs the agent interactively, "browser" opens a URL."""
    login_hosts = ()
    """Hosts a browser sign-in may open."""
    demo_limits = ((None, 10080, 30, 259200),)
    """Fictional limits for the demo: (bucket, minutes, used percent, seconds until reset)."""
    window_minutes = 300
    """Short rolling usage window duration in minutes, used by the window scheduler."""

    def install_directories(self):
        """Native installation directories searched when the command is not on PATH."""
        return [Path.home() / ".local/bin"]

    def extension_binary(self, target, name):
        """The executable's path inside an editor extension, for a platform target."""
        raise NotImplementedError

    def default_history(self):
        """The native session directory, honoring the agent's own environment variables."""
        raise NotImplementedError

    def history_files(self, root):
        """Session files below a history directory."""
        return root.rglob("*.jsonl")

    def parse(self, event, state):
        """Turn one native event into prompt and call records; state persists per file."""
        raise NotImplementedError

    async def read_limits(self, command, timeout, login=None):
        """Query account limits through the native client, as normalized windows."""
        raise NotImplementedError

    async def activate_window(self, command, timeout):
        """Start a usage window with a minimal turn, without persisting a session."""
        raise NotImplementedError

    def demo_session(self, session, title, start, seconds, tokens):
        """Fictional native events for one prompt, used by the demo profile."""
        raise NotImplementedError

    def command(self, configured=None):
        """The configured argument list, or this agent's native installation."""
        if configured:
            return list(configured)
        windows = sys.platform == "win32"
        name = self.executable + (".exe" if windows else "")
        found = shutil.which(name)
        # Do not auto-discover a program in the current project or Downloads directory.
        if found and Path(found).is_absolute() and Path(found).parent != Path.cwd():
            return [found]
        candidates = [
            Path(directory).expanduser() / name for directory in self.install_directories()
        ]
        if self.extension:
            machine = platform.machine().lower()
            architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
            target = ("windows" if windows else "linux") + "-" + architecture
            candidates += [
                extension / self.extension_binary(target, name)
                for extension in editor_extensions(self.extension)
            ]
        for candidate in candidates:
            if candidate.is_file() and (windows or os.access(candidate, os.X_OK)):
                return [str(candidate)]
        raise FileNotFoundError("Agent executable unavailable")


def editor_extensions(prefix):
    """Installed editor extensions with a prefix, newest version first in each editor."""
    for editor in (".vscode", ".vscode-insiders", ".cursor"):
        yield from sorted(
            (Path.home() / editor / "extensions").glob(prefix + "-*"),
            key=lambda path: tuple(map(int, re.findall(r"\d+", path.name))),
            reverse=True,
        )


def window(bucket, minutes, used, resets):
    """One limit window; incomplete or invalid observations are dropped, never zeroed."""
    minutes, used, resets = map(number, (minutes, used, resets))
    if None in (minutes, used, resets) or minutes <= 0 or used > 100 or resets <= 0:
        return None
    return dict(bucket=bucket, minutes=minutes, used_percent=used, resets_at=resets)


def validate_login_url(value, hosts):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or url.hostname not in hosts
        or url.username is not None
        or url.password is not None
        or url.port not in (None, 443)
    ):
        raise ValueError("Invalid native login URL")
    return value


async def start(command, *args, cwd=None):
    return await asyncio.create_subprocess_exec(
        *[str(Path(arg).expanduser()) if arg.startswith("~") else arg for arg in command],
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=1024 * 1024,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


async def stop(process):
    if process.returncode is None:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        process.kill()
        await process.wait()
