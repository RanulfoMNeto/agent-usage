"""Agent Usage: a persistent local desktop widget for coding agents."""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from usage.config import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Override per-user configuration")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--demo", action="store_true", help="Try fictional data in a temporary profile"
    )
    modes.add_argument(
        "--install-gnome",
        action="store_true",
        help="Install the GNOME top bar and desktop integration, starting at login (GNOME 50)",
    )
    modes.add_argument(
        "--uninstall-gnome",
        action="store_true",
        help="Remove GNOME integration, retaining usage history",
    )
    args = parser.parse_args()
    try:
        if args.demo:
            from usage.demo import profile
            from usage.desktop import run

            with profile() as (config, limits):
                run(config, limits=limits)
            return
        config = load_config(args.config)
        if args.install_gnome or args.uninstall_gnome:
            from usage.integration import install_gnome

            print(install_gnome(args.config, remove=args.uninstall_gnome))
            return
        if sys.platform == "win32" and config["integrations"]["windows_shortcut"]:
            from usage.integration import install_windows_shortcut

            try:
                install_windows_shortcut(args.config)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                logging.warning("Could not create the Start menu shortcut.")
        from usage.desktop import run
        from usage.limits import Limits

        run(config, limits=Limits(config))
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
