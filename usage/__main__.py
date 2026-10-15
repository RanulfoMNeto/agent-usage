"""Agent Usage: a persistent local desktop widget."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .config import load_config
from .subscriptions import capture_claude


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Override per-user configuration")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--capture-claude", action="store_true", help="Capture native status-line JSON"
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
        config = load_config(args.config)
        if args.install_gnome or args.uninstall_gnome:
            from .integration import install_gnome

            print(install_gnome(args.config, remove=args.uninstall_gnome))
            return
        if args.capture_claude:
            snapshot = capture_claude(
                config["subscriptions"]["claude_file"], json.loads(sys.stdin.read(65537))
            )
            print(
                " · ".join(
                    f"{w['minutes'] / 60:g}h: {w['used_percent']:g}%" for w in snapshot["windows"]
                )
            )
            return
        from .desktop import run

        run(config)
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
