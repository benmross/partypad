#!/usr/bin/env python3
"""
Configure Dolphin so Wii Remotes 1-4 are automatically bound to partypad's
DSU bridge (players 1-4), with no manual mapping.

It:
  1. adds a DSU server entry  partypad:127.0.0.1:26760  to DSUClient.ini
  2. rewrites [Wiimote1..4] in WiimoteNew.ini to point at
     DSUClient/0..3/partypad with the full button + D-pad + IR + motion map.

Backs up both files first. Dolphin MUST be closed while this runs, because it
rewrites these files on exit and would clobber our changes.

Run:  uv run python setup_dolphin.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dolphin-emu"
WIIMOTE_INI = CONFIG_DIR / "WiimoteNew.ini"
DSU_INI = CONFIG_DIR / "DSUClient.ini"

SERVER_NAME = "partypad"
SERVER_ENTRY = f"{SERVER_NAME}:127.0.0.1:26760"


# One emulated Wii Remote bound to DSU pad `index`, sideways-friendly.
# Button field names on the right match what partypad's web controller sends.
def wiimote_block(index: int) -> str:
    return f"""Device = DSUClient/{index}/{SERVER_NAME}
Source = 1
Buttons/A = `Cross`
Buttons/B = `Square`
Buttons/1 = `Triangle`
Buttons/2 = `Circle`
Buttons/- = `Share`
Buttons/+ = `Options`
Buttons/Home = `PS`
D-Pad/Up = `Pad N`
D-Pad/Down = `Pad S`
D-Pad/Left = `Pad W`
D-Pad/Right = `Pad E`
IR/Up = `Right Y+`
IR/Down = `Right Y-`
IR/Left = `Right X-`
IR/Right = `Right X+`
IR/Center = 0.00 0.00
IMUIR/Enabled = False
IMUIR/Accelerometer Influence = 0.
IMUAccelerometer/Up = Accel Up
IMUAccelerometer/Down = Accel Down
IMUAccelerometer/Left = Accel Left
IMUAccelerometer/Right = Accel Right
IMUAccelerometer/Forward = Accel Forward
IMUAccelerometer/Backward = Accel Backward
IMUGyroscope/Pitch Up = Gyro Pitch Up
IMUGyroscope/Pitch Down = Gyro Pitch Down
IMUGyroscope/Roll Left = Gyro Roll Left
IMUGyroscope/Roll Right = Gyro Roll Right
IMUGyroscope/Yaw Left = Gyro Yaw Left
IMUGyroscope/Yaw Right = Gyro Yaw Right
"""


def parse_sections(text: str):
    """Return list of (header_or_None, body_lines) preserving order."""
    sections = []
    header = None
    body = []
    for line in text.splitlines():
        if line.startswith("[") and line.rstrip().endswith("]"):
            sections.append((header, body))
            header = line.rstrip()
            body = []
        else:
            body.append(line)
    sections.append((header, body))
    return sections


def backup(path: Path):
    if not path.exists():
        return
    bak = path.with_suffix(path.suffix + ".partypad-bak")
    if not bak.exists():
        shutil.copy2(path, bak)
        print(f"  backed up {path.name} -> {bak.name}")
    else:
        print(f"  backup {bak.name} already exists (keeping the original)")


def patch_wiimote_ini():
    existing = WIIMOTE_INI.read_text() if WIIMOTE_INI.exists() else ""
    sections = parse_sections(existing)

    wanted = {f"[Wiimote{i}]": wiimote_block(i - 1) for i in range(1, 5)}
    seen = set()
    out = []

    # preamble before first header (usually empty)
    if sections and sections[0][0] is None:
        pre = "\n".join(sections[0][1]).strip()
        if pre:
            out.append(pre)

    for header, body in sections:
        if header is None:
            continue
        if header in wanted:
            out.append(header + "\n" + wanted[header].rstrip())
            seen.add(header)
        else:
            out.append(header + "\n" + "\n".join(body).rstrip())

    # add any Wiimote sections that were missing entirely
    for header in ("[Wiimote1]", "[Wiimote2]", "[Wiimote3]", "[Wiimote4]"):
        if header not in seen:
            out.append(header + "\n" + wanted[header].rstrip())

    WIIMOTE_INI.write_text("\n".join(out).rstrip() + "\n")
    print(f"  wrote {WIIMOTE_INI.name}: Wiimote1-4 -> DSUClient/0-3/{SERVER_NAME}")


def patch_dsu_ini():
    lines = (
        DSU_INI.read_text().splitlines()
        if DSU_INI.exists()
        else ["[Server]", "Enabled = True", "Entries = "]
    )
    out = []
    has_server = any(line.strip() == "[Server]" for line in lines)
    entries_done = False
    enabled_done = False
    in_server = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_server = stripped == "[Server]"
        if in_server and stripped.lower().startswith("enabled"):
            out.append("Enabled = True")
            enabled_done = True
            continue
        if in_server and stripped.lower().startswith("entries"):
            _, _, val = line.partition("=")
            entries = [e for e in val.strip().split(";") if e]
            if not any(e.split(":")[0] == SERVER_NAME for e in entries):
                entries.append(SERVER_ENTRY)
            out.append("Entries = " + ";".join(entries) + ";")
            entries_done = True
            continue
        out.append(line)

    if not has_server:
        out = ["[Server]", "Enabled = True", f"Entries = {SERVER_ENTRY};"]
    else:
        if not enabled_done:
            out.insert(out.index("[Server]") + 1, "Enabled = True")
        if not entries_done:
            out.insert(out.index("[Server]") + 2, f"Entries = {SERVER_ENTRY};")

    DSU_INI.write_text("\n".join(out).rstrip() + "\n")
    print(f"  wrote {DSU_INI.name}: server entry '{SERVER_ENTRY}' present, enabled")


def main():
    if not CONFIG_DIR.is_dir():
        sys.exit(f"Dolphin config dir not found: {CONFIG_DIR}")

    running = subprocess.run(["pgrep", "-i", "dolphin"], capture_output=True).returncode == 0
    if running and "--force" not in sys.argv:
        sys.exit(
            "Dolphin is running — close it first (it rewrites these files on exit), "
            "then re-run. Use --force to override."
        )

    print(f"Configuring Dolphin in {CONFIG_DIR}")
    backup(WIIMOTE_INI)
    backup(DSU_INI)
    patch_dsu_ini()
    patch_wiimote_ini()
    print(
        "\nDone. Start partypad (uv run python server.py), then launch Dolphin — "
        "Wii Remotes 1-4 are already mapped to players 1-4."
    )
    print("Revert anytime: restore the *.partypad-bak files.")


if __name__ == "__main__":
    main()
