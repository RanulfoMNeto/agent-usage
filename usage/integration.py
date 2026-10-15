"""Native desktop launchers and optional GNOME integration."""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BUNDLE = Path(__file__).with_name("gnome")
UUID = "agent-usage@agent-usage.local"


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15).stdout


def launch_command(config=None):
    args = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "usage"]
    if config:
        args.extend(["--config", str(Path(config).resolve())])
    return args


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
