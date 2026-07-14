#!/usr/bin/env python3
"""Unified source and packaged command-line entry point for PartyPad."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"dashboard", "bridge", "serve", "authorize", "setup-dolphin"}
    command = argv.pop(0) if argv and argv[0] in commands else "dashboard"
    if command == "authorize":
        from setup_online import main as authorize

        authorize(argv)
    elif command == "setup-dolphin":
        from setup_dolphin import main as setup_dolphin

        setup_dolphin(argv)
    elif command in ("serve", "bridge"):
        from server import main as serve

        serve(argv)
    else:
        from dashboard import main as dashboard

        dashboard(argv)


if __name__ == "__main__":
    main()
