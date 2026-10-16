"""Native desktop launchers and optional GNOME integration."""

import ast
import base64
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BUNDLE = Path(__file__).with_name("gnome")
UUID = "agent-usage@agent-usage.local"


class ConnectionUnavailable(RuntimeError):
    """An actionable connection failure without command output or private paths."""


def claude_terminal_command(command):
    """Prepare an argument list; never ask a shell to interpret user paths."""
    try:
        args = agent_command("claude", command)
    except FileNotFoundError:
        raise ConnectionUnavailable(
            "Install Claude Code or its official VS Code extension."
        ) from None
    args = [str(Path(arg).expanduser()) if arg.startswith("~") else arg for arg in args]
    if sys.platform == "win32":
        return args
    for terminal, separator in (
        ("xdg-terminal-exec", []),
        ("gnome-terminal", ["--"]),
        ("konsole", ["-e"]),
        ("x-terminal-emulator", ["-e"]),
    ):
        executable = shutil.which(terminal)
        if executable:
            return [executable, *separator, *args]
    raise ConnectionUnavailable("No supported terminal found. Install a desktop terminal.")


def open_claude_terminal(args):
    """Keep the interactive session separate from the widget's lifetime."""
    try:
        if sys.platform == "win32":
            return subprocess.Popen(
                args, cwd=Path.home(), creationflags=subprocess.CREATE_NEW_CONSOLE
            )
        from PySide6.QtCore import QProcess

        started, _ = QProcess.startDetached(args[0], args[1:], str(Path.home()))
        if started:
            return None
    except OSError:
        pass
    raise ConnectionUnavailable("Could not open the Claude terminal. Try connecting again.")


def agent_command(agent, command):
    """Reuse a native installation when the default command is not on PATH."""
    if agent not in ("codex", "claude"):
        raise ValueError("Unknown agent")
    if command != [agent]:
        return command
    windows = sys.platform == "win32"
    name = agent + (".exe" if windows else "")
    found = shutil.which(name)
    # Do not auto-discover a program in the current project or Downloads directory.
    if found and Path(found).is_absolute() and Path(found).parent != Path.cwd():
        return [found]
    home = Path.home()
    installed = (
        Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local") / "Programs/OpenAI/Codex/bin"
        if windows and agent == "codex"
        else home / ".local/bin"
    )
    directory = os.environ.get("CODEX_INSTALL_DIR") if agent == "codex" else None
    candidates = [Path(directory or installed).expanduser() / name]
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    target = ("windows" if windows else "linux") + "-" + architecture
    prefix = "openai.chatgpt" if agent == "codex" else "anthropic.claude-code"
    for editor in (".vscode", ".vscode-insiders", ".cursor"):
        extensions = (home / editor / "extensions").glob(prefix + "-*")
        for extension in sorted(
            extensions,
            key=lambda path: tuple(map(int, re.findall(r"\d+", path.name))),
            reverse=True,
        ):
            candidates.append(
                extension / "bin" / target / name
                if agent == "codex"
                else extension / "resources/native-binary" / name
            )
    for candidate in candidates:
        if candidate.is_file() and (windows or os.access(candidate, os.X_OK)):
            return [str(candidate)]
    raise FileNotFoundError("Agent executable unavailable")


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15).stdout


def launch_command(config=None):
    executable = Path(sys.executable)
    # A Start-menu launch uses pythonw; status-line capture still needs standard I/O.
    if executable.name.lower() == "pythonw.exe" and not getattr(sys, "frozen", False):
        executable = executable.with_name("python.exe")
    args = [str(executable)]
    if not getattr(sys, "frozen", False):
        args.extend(["-I", "-m", "usage"])
    if config:
        args.extend(["--config", str(Path(config).resolve())])
    return args


def claude_command(config=None):
    """Quote once for Claude's shell; Windows supports both Git Bash and PowerShell."""
    args = [Path(arg).as_posix() for arg in launch_command(config)] + ["--capture-claude"]
    if sys.platform == "win32":
        quoted = " ".join("'" + arg.replace("'", "''") + "'" for arg in args)
        script = (
            "# Agent Usage status line\n"
            "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "[Console]::In.ReadToEnd() | & " + quoted + "; exit $LASTEXITCODE"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded
    return shlex.join(args)


def owns_claude_command(command):
    """Recognize our generated commands, including the previous manual installation."""
    if not isinstance(command, str):
        return False
    try:
        args = shlex.split(command)
        # Older installations wrapped the capture in a working-directory change.
        if len(args) > 3 and args[0] == "cd" and args[2] == "&&":
            args = args[3:]
        if args[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]:
            return len(args) == 5 and base64.b64decode(args[4], validate=True).decode(
                "utf-16-le"
            ).startswith("# Agent Usage status line\n")
        executable = Path(args.pop(0)).name.lower()
        if args[:1] == ["-I"]:
            args = args[1:]
        if args[:2] == ["-m", "usage"] and executable.startswith("python"):
            args = args[2:]
        elif executable not in ("agent-usage", "agent-usage.exe"):
            return False
        if args[:1] == ["--config"] and len(args) >= 3:
            args = args[2:]
        return args == ["--capture-claude"]
    except (ValueError, IndexError, UnicodeError):
        return False


def claude_status(config=None):
    """Inspect the connection without changing the agent's settings."""
    directory = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    if not directory.is_dir():
        return "claude_not_found"
    path = directory / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        previous = settings.get("statusLine") or {}
        return (
            "claude_waiting"
            if previous.get("type") == "command"
            and previous.get("command") == claude_command(config)
            else "claude_setup_required"
        )
    except (OSError, ValueError, AttributeError):
        return "claude_setup_failed"


def install_claude_statusline(config=None, *, replace=False):
    """Set up capture on request; replacing another status line requires consent."""
    from PySide6.QtCore import QIODevice, QLockFile, QSaveFile

    directory = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "settings.json"
    lock = QLockFile(str(path) + ".agent-usage.lock")
    if not lock.tryLock(0):
        raise OSError("Claude settings are busy")
    try:
        original = path.read_bytes() if path.exists() else None
        settings = json.loads(original.decode("utf-8-sig")) if original is not None else {}
        if not isinstance(settings, dict):
            raise ValueError("Invalid Claude settings")
        previous = settings.get("statusLine")
        custom = previous is not None and (
            not isinstance(previous, dict)
            or previous.get("type") != "command"
            or not owns_claude_command(previous.get("command"))
        )
        if custom and not replace:
            return "claude_custom_statusline"
        statusline = {
            **(previous if isinstance(previous, dict) else {}),
            "type": "command",
            "command": claude_command(config),
        }
        if previous == statusline:
            return "claude_waiting"
        settings["statusLine"] = statusline
        body = (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        backup = path.with_name("settings.json.agent-usage.bak")
        if original is not None and (custom or not backup.exists()):
            # Never overwrite an earlier backup when replacing a custom command.
            stream = (
                tempfile.NamedTemporaryFile(
                    prefix="settings.json.agent-usage-", suffix=".bak", dir=directory, delete=False
                )
                if backup.exists()
                else backup.open("xb")
            )
            with stream:
                if os.name != "nt":
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(original)
        output = QSaveFile(str(path))
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError("Claude settings are not writable")
        current = path.read_bytes() if path.exists() else None
        if current != original or output.write(body) != len(body):
            output.cancelWriting()
            raise OSError("Claude settings changed during setup")
        if not output.commit():
            raise OSError("Could not save Claude settings")
        return "claude_waiting"
    finally:
        lock.unlock()


def install_windows_shortcut(config=None):
    """Register the current executable in the user's Start menu, without elevation."""
    if sys.platform != "win32":
        return None
    from PySide6.QtCore import QStandardPaths

    directory = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.ApplicationsLocation
    )
    if not directory:
        raise OSError("Start menu unavailable")
    shortcut = Path(directory) / "Agent Usage.lnk"
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    args = launch_command(config)
    if not getattr(sys, "frozen", False):
        windowed = Path(args[0]).with_name("pythonw.exe")
        if windowed.is_file():
            args[0] = str(windowed)
    entry = dict(path=str(shortcut), target=args[0], arguments=subprocess.list2cmdline(args[1:]))
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            """
$ErrorActionPreference = 'Stop'
$entry = $env:AGENT_USAGE_SHORTCUT | ConvertFrom-Json
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($entry.path)
$link.TargetPath = $entry.target
$link.Arguments = $entry.arguments
$link.WorkingDirectory = Split-Path -LiteralPath $entry.target
$link.Description = 'Agent Usage'
$link.IconLocation = $entry.target + ',0'
$link.Save()
""",
        ],
        env={**os.environ, "AGENT_USAGE_SHORTCUT": json.dumps(entry)},
        check=True,
        capture_output=True,
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return shortcut


def desktop_entry(config=None):
    args = launch_command(config)
    quoted = []
    for arg in args:
        if any(ord(c) < 32 for c in arg):
            raise ValueError("Invalid launcher path")
        escaped = "".join("\\" + c if c in '\\"`$' else c for c in arg)
        quoted.append('"' + escaped.replace("\\", "\\\\").replace("%", "%%") + '"')
    icon = Path(__file__).with_name("static") / "assets/widget.svg"
    return (
        "[Desktop Entry]\nType=Application\nName=Agent Usage\n"
        f"Exec={' '.join(quoted)}\nIcon={icon}\nTerminal=false\n"
        "Categories=Utility;\nX-GNOME-Autostart-enabled=true\n"
    )


def install_gnome(config=None, *, remove=False):
    if sys.platform != "linux":
        raise RuntimeError("GNOME integration requires Linux")
    metadata = json.loads((BUNDLE / "metadata.json").read_text())
    version = command("gnome-shell", "--version").strip().split()[-1].split(".")[0]
    if not remove and version not in metadata["shell-version"]:
        raise RuntimeError("Unsupported GNOME version")
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    target = data / "gnome-shell/extensions" / UUID
    entries = [
        data / "applications/agent-usage.desktop",
        config_home / "autostart/agent-usage.desktop",
    ]
    enabled = ast.literal_eval(
        command("gsettings", "get", "org.gnome.shell", "enabled-extensions")
        .removeprefix("@as ")
        .strip()
    )
    if remove:
        command(
            "gsettings",
            "set",
            "org.gnome.shell",
            "enabled-extensions",
            repr([item for item in enabled if item != UUID]),
        )
        if target.exists():
            shutil.rmtree(target)
        for entry in entries:
            entry.unlink(missing_ok=True)
        return "GNOME integration removed. Usage history is unchanged."
    with tempfile.TemporaryDirectory(prefix="agent-usage-extension-") as folder:
        staged = Path(folder) / UUID
        shutil.copytree(BUNDLE, staged)
        command("glib-compile-schemas", "--strict", str(staged / "schemas"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged, target, dirs_exist_ok=True)
    for entry in entries:
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(desktop_entry(config), encoding="utf-8")
    if UUID not in enabled:
        command("gsettings", "set", "org.gnome.shell", "enabled-extensions", repr([*enabled, UUID]))
    disabled = ast.literal_eval(
        command("gsettings", "get", "org.gnome.shell", "disabled-extensions")
        .removeprefix("@as ")
        .strip()
    )
    if UUID in disabled:
        command(
            "gsettings",
            "set",
            "org.gnome.shell",
            "disabled-extensions",
            repr([item for item in disabled if item != UUID]),
        )
    return "GNOME integration installed. Sign out and back in to load it and start the widget."


def gnome_call(method, *args):
    if sys.platform != "linux" or "GNOME" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
        return False
    from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage

    interface = QDBusInterface(
        "org.gnome.Shell",
        "/org/agentusage/Desktop",
        "org.agentusage.Desktop",
        QDBusConnection.sessionBus(),
    )
    interface.setTimeout(200)
    if not interface.isValid():
        return False
    response = interface.call(method, os.getpid(), *args)
    return response.type() == QDBusMessage.MessageType.ReplyMessage and response.arguments() == [
        True
    ]
