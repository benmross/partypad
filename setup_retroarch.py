#!/usr/bin/env python3
"""Install or remove PartyPad's RetroArch udev autoconfiguration profile."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


HERE = Path(__file__).parent
SOURCE = HERE / "retroarch" / "PartyPad Controller.cfg"


def config_dir() -> Path:
    override = os.environ.get("RETROARCH_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "retroarch"


def install(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_suffix(destination.suffix + ".partypad-bak")
    if destination.exists() and not backup.exists():
        shutil.copy2(destination, backup)
        print(f"Backed up existing profile to {backup}")
    shutil.copy2(SOURCE, destination)
    print(f"Installed {destination}")


def revert(destination: Path) -> None:
    backup = destination.with_suffix(destination.suffix + ".partypad-bak")
    if backup.exists():
        shutil.copy2(backup, destination)
        backup.unlink()
        print(f"Restored original profile at {destination}")
    elif destination.exists() and destination.read_bytes() == SOURCE.read_bytes():
        destination.unlink()
        print(f"Removed {destination}")
    else:
        print(f"Nothing to revert at {destination}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revert", action="store_true", help="restore/remove PartyPad's profile")
    args = parser.parse_args()
    destination = config_dir() / "autoconfig" / "udev" / SOURCE.name
    revert(destination) if args.revert else install(destination)


if __name__ == "__main__":
    main()
