"""Agent Usage: a persistent local desktop widget."""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from usage.config import load_config
from usage.subscriptions import capture_claude


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Override per-user configuration")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--demo", action="store_true", help="Try fictional data in a temporary profile"
    )
    modes.add_argument(
        "--capture-claude", action="store_true", help="Capture native status-line JSON"
    )
    modes.add_argument(
        "--claude-config",
        action="store_true",
        help="Print Claude status-line settings; no files are changed",
    )
    modes.add_argument(
        "--install-gnome",
        action="store_true",
        help="Install GNOME desktop integration and start at login (GNOME 50)",
    )
    modes.add_argument(
        "--uninstall-gnome",
        action="store_true",
        help="Remove GNOME integration, retaining usage history",
    )
    args = parser.parse_args()
    try:
        if args.claude_config:
            from usage.integration import claude_command

            command = claude_command(args.config)
            print(json.dumps({"statusLine": {"type": "command", "command": command}}, indent=2))
            return
        if args.demo:
            from usage.demo import profile
            from usage.desktop import run

            with profile() as (config, subscriptions):
                run(config, subscriptions=subscriptions)
            return
        config = load_config(args.config)
        if args.install_gnome or args.uninstall_gnome:
            from usage.integration import install_gnome

            print(install_gnome(args.config, remove=args.uninstall_gnome))
            return
        if args.capture_claude:
            snapshot = capture_claude(
                config["subscriptions"]["claude_file"], json.loads(sys.stdin.buffer.read(65537))
            )
            print(
                " · ".join(
                    f"{w['minutes'] / 60:g}h: {w['used_percent']:g}%" for w in snapshot["windows"]
                )
            )
            return
        from usage.desktop import run

        if sys.platform == "win32" and config["integrations"]["windows_shortcut"]:
            from usage.integration import install_windows_shortcut

            try:
                install_windows_shortcut(args.config)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                logging.warning("Could not create the Start menu shortcut.")
        from usage.integration import install_claude_statusline
        from usage.subscriptions import Subscriptions

        claude_issue = "claude_setup_disabled"
        if config["integrations"]["claude_statusline"]:
            try:
                claude_issue = install_claude_statusline(args.config)
            except (OSError, ValueError):
                claude_issue = "claude_setup_failed"
                logging.warning("Could not configure Claude's status line. Settings are unchanged.")
        run(config, subscriptions=Subscriptions(config["subscriptions"], claude_issue=claude_issue))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        parser.exit(
            1, f"Agent Usage: {type(exc).__name__}. Check your configuration and desktop session.\n"
        )


if __name__ == "__main__":
    main()
