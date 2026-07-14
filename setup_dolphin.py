#!/usr/bin/env python3
"""Discover, preview, configure, and revert PartyPad's Dolphin DSU mapping."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SERVER_NAME = "partypad"
SERVER_ENTRY = f"{SERVER_NAME}:127.0.0.1:26760"
CONFIG_FILENAMES = ("WiimoteNew.ini", "DSUClient.ini")
BACKUP_SUFFIX = ".partypad-bak"
MANIFEST_NAME = ".partypad-setup.json"


@dataclass(frozen=True)
class DolphinCandidate:
    config_dir: Path
    source: str

    @property
    def populated(self) -> bool:
        return any((self.config_dir / name).exists() for name in (*CONFIG_FILENAMES, "Dolphin.ini"))


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


def parse_sections(text: str) -> list[tuple[str | None, list[str]]]:
    """Return `(header, body)` sections while preserving their order."""
    sections: list[tuple[str | None, list[str]]] = []
    header: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("[") and line.rstrip().endswith("]"):
            sections.append((header, body))
            header = line.rstrip()
            body = []
        else:
            body.append(line)
    sections.append((header, body))
    return sections


def patched_wiimote_ini(existing: str) -> str:
    sections = parse_sections(existing)
    wanted = {f"[Wiimote{i}]": wiimote_block(i - 1) for i in range(1, 5)}
    seen: set[str] = set()
    output: list[str] = []

    if sections and sections[0][0] is None:
        preamble = "\n".join(sections[0][1]).strip()
        if preamble:
            output.append(preamble)
    for header, body in sections:
        if header is None:
            continue
        if header in wanted:
            output.append(header + "\n" + wanted[header].rstrip())
            seen.add(header)
        else:
            output.append(header + "\n" + "\n".join(body).rstrip())
    for header in ("[Wiimote1]", "[Wiimote2]", "[Wiimote3]", "[Wiimote4]"):
        if header not in seen:
            output.append(header + "\n" + wanted[header].rstrip())
    return "\n".join(output).rstrip() + "\n"


def patched_dsu_ini(existing: str) -> str:
    lines = existing.splitlines() if existing else ["[Server]", "Enabled = True", "Entries = "]
    output: list[str] = []
    has_server = any(line.strip() == "[Server]" for line in lines)
    entries_done = False
    enabled_done = False
    in_server = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_server = stripped == "[Server]"
        if in_server and stripped.lower().startswith("enabled"):
            output.append("Enabled = True")
            enabled_done = True
            continue
        if in_server and stripped.lower().startswith("entries"):
            _, _, value = line.partition("=")
            entries = [entry for entry in value.strip().split(";") if entry]
            entries = [entry for entry in entries if entry.split(":", 1)[0] != SERVER_NAME]
            entries.append(SERVER_ENTRY)
            output.append("Entries = " + ";".join(entries) + ";")
            entries_done = True
            continue
        output.append(line)

    if not has_server:
        output = ["[Server]", "Enabled = True", f"Entries = {SERVER_ENTRY};", *output]
    else:
        server_index = output.index("[Server]")
        if not enabled_done:
            output.insert(server_index + 1, "Enabled = True")
        if not entries_done:
            output.insert(server_index + 2, f"Entries = {SERVER_ENTRY};")
    return "\n".join(output).rstrip() + "\n"


def _config_dir(user_dir: Path, *, xdg=False) -> Path:
    """Map a Dolphin user directory (or XDG config root) to its INI directory."""
    if xdg or any((user_dir / name).exists() for name in (*CONFIG_FILENAMES, "Dolphin.ini")):
        return user_dir
    return user_dir / "Config"


def preference_path(platform: str, home: Path, env: dict[str, str]) -> Path:
    if platform == "win32":
        root = Path(env.get("APPDATA", home / "AppData" / "Roaming")) / "PartyPad"
    elif platform == "darwin":
        root = home / "Library" / "Application Support" / "PartyPad"
    else:
        root = Path(env.get("XDG_CONFIG_HOME", home / ".config")) / "partypad"
    return root / "dolphin_user_dir"


def discover_dolphin_dirs(
    *,
    platform: str | None = None,
    home: Path | None = None,
    env: dict[str, str] | None = None,
    explicit: Path | None = None,
    portable_dir: Path | None = None,
    include_saved=True,
) -> list[DolphinCandidate]:
    """Return de-duplicated existing or explicitly requested config candidates."""
    platform = platform or sys.platform
    home = home or Path.home()
    env = dict(os.environ if env is None else env)
    raw: list[DolphinCandidate] = []

    if explicit is not None:
        raw.append(DolphinCandidate(_config_dir(explicit.expanduser()), "--dolphin-user-dir"))
    if portable_dir is not None:
        raw.append(DolphinCandidate(portable_dir.expanduser() / "User" / "Config", "portable build"))
    user_path = env.get("DOLPHIN_EMU_USERPATH")
    if user_path:
        raw.append(DolphinCandidate(_config_dir(Path(user_path).expanduser()), "DOLPHIN_EMU_USERPATH"))

    if include_saved:
        saved = preference_path(platform, home, env)
        try:
            saved_path = Path(saved.read_text().strip()).expanduser()
        except OSError:
            pass
        else:
            if str(saved_path):
                raw.append(DolphinCandidate(saved_path, "saved PartyPad choice"))

    if platform == "win32":
        appdata = Path(env.get("APPDATA", home / "AppData" / "Roaming"))
        documents = Path(env.get("USERPROFILE", home)) / "Documents"
        raw.extend(
            (
                DolphinCandidate(appdata / "Dolphin Emulator" / "Config", "Windows AppData"),
                DolphinCandidate(documents / "Dolphin Emulator" / "Config", "Windows Documents"),
            )
        )
    elif platform == "darwin":
        raw.append(
            DolphinCandidate(
                home / "Library" / "Application Support" / "Dolphin" / "Config",
                "macOS Application Support",
            )
        )
    else:
        xdg = Path(env.get("XDG_CONFIG_HOME", home / ".config"))
        raw.extend(
            (
                DolphinCandidate(xdg / "dolphin-emu", "Linux XDG"),
                DolphinCandidate(home / ".dolphin-emu" / "Config", "legacy Linux"),
                DolphinCandidate(
                    home
                    / ".var"
                    / "app"
                    / "org.DolphinEmu.dolphin-emu"
                    / "config"
                    / "dolphin-emu",
                    "Dolphin Flatpak",
                ),
            )
        )

    keep_missing = {candidate.config_dir for candidate in raw[: int(explicit is not None) + int(portable_dir is not None)]}
    result: list[DolphinCandidate] = []
    seen: set[Path] = set()
    for candidate in raw:
        path = candidate.config_dir.resolve(strict=False)
        if path in seen or (not path.is_dir() and path not in keep_missing):
            continue
        seen.add(path)
        result.append(DolphinCandidate(path, candidate.source))
    return result


def select_dolphin_dir(candidates: list[DolphinCandidate]) -> Path:
    if not candidates:
        raise ValueError(
            "Dolphin configuration was not found; launch Dolphin once or pass --dolphin-user-dir"
        )
    populated = [candidate for candidate in candidates if candidate.populated]
    if len(populated) == 1:
        return populated[0].config_dir
    choices = populated or candidates
    if len(choices) == 1:
        return choices[0].config_dir
    detail = "\n".join(f"  {item.config_dir} ({item.source})" for item in choices)
    raise ValueError(
        "multiple Dolphin configurations were found; choose one with --dolphin-user-dir:\n"
        + detail
    )


def dolphin_is_running(platform: str | None = None) -> bool:
    """Best-effort cross-platform process check without an optional dependency."""
    platform = platform or sys.platform
    try:
        if platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Dolphin.exe", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
            )
            names = {
                Path(line.strip().strip('"').split('\",\"', 1)[0]).name.lower()
                for line in result.stdout.splitlines()
            }
        else:
            result = subprocess.run(
                ["ps", "-A", "-o", "comm="], check=False, capture_output=True, text=True
            )
            names = {Path(line.strip()).name.lower() for line in result.stdout.splitlines()}
    except OSError:
        return False
    return any(
        name in {"dolphin.exe", "dolphin-emu", "dolphinqt", "dolphin"}
        or name.startswith("dolphin-emu-")
        for name in names
    )


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def planned_changes(config_dir: Path) -> dict[Path, tuple[str, str]]:
    wiimote = config_dir / "WiimoteNew.ini"
    dsu = config_dir / "DSUClient.ini"
    before_wiimote = wiimote.read_text() if wiimote.exists() else ""
    before_dsu = dsu.read_text() if dsu.exists() else ""
    return {
        wiimote: (before_wiimote, patched_wiimote_ini(before_wiimote)),
        dsu: (before_dsu, patched_dsu_ini(before_dsu)),
    }


def preview(config_dir: Path) -> str:
    diffs: list[str] = []
    for path, (before, after) in planned_changes(config_dir).items():
        diffs.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path) + " (PartyPad)",
            )
        )
    return "".join(diffs) or "No changes are needed.\n"


def install(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = config_dir / MANIFEST_NAME
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {"version": 1, "files": {}}

    for path, (before, after) in planned_changes(config_dir).items():
        record = manifest["files"].get(path.name)
        if record is None:
            existed = path.exists()
            manifest["files"][path.name] = {"existed": existed}
            if existed:
                backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
                if not backup.exists():
                    shutil.copy2(path, backup)
        if before != after:
            atomic_write(path, after)
    atomic_write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def revert(config_dir: Path) -> None:
    manifest_path = config_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise ValueError(f"no PartyPad setup manifest found in {config_dir}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid PartyPad setup manifest: {manifest_path}") from exc
    for name, record in manifest.get("files", {}).items():
        if name not in CONFIG_FILENAMES or not isinstance(record, dict):
            raise ValueError(f"unsafe entry in PartyPad setup manifest: {name!r}")
        path = config_dir / name
        backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        if record.get("existed"):
            if not backup.exists():
                raise ValueError(f"cannot restore {path.name}: backup is missing")
            os.replace(backup, path)
        else:
            path.unlink(missing_ok=True)
    manifest_path.unlink()


def save_choice(config_dir: Path, *, platform: str | None = None) -> None:
    destination = preference_path(platform or sys.platform, Path.home(), dict(os.environ))
    atomic_write(destination, str(config_dir) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="configure Dolphin controllers for PartyPad")
    parser.add_argument(
        "--dolphin-user-dir",
        type=Path,
        help="Dolphin user directory selected in Dolphin with -u (contains Config)",
    )
    parser.add_argument(
        "--portable-dir",
        type=Path,
        help="directory containing a portable Dolphin executable and portable.txt",
    )
    parser.add_argument("--preview", action="store_true", help="show changes without writing")
    parser.add_argument("--revert", action="store_true", help="restore the original configuration")
    parser.add_argument("--force", action="store_true", help="write even if Dolphin appears open")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dolphin_user_dir and args.portable_dir:
        parser.error("choose --dolphin-user-dir or --portable-dir, not both")
    try:
        candidates = discover_dolphin_dirs(
            explicit=args.dolphin_user_dir,
            portable_dir=args.portable_dir,
        )
        config_dir = select_dolphin_dir(candidates)
    except ValueError as exc:
        parser.error(str(exc))

    if args.preview:
        print(preview(config_dir), end="")
        return
    if dolphin_is_running() and not args.force:
        parser.error(
            "Dolphin appears to be running and may overwrite these files on exit; "
            "close it first or use --force"
        )
    try:
        if args.revert:
            revert(config_dir)
            print(f"Restored the Dolphin configuration in {config_dir}")
        else:
            print(preview(config_dir), end="")
            install(config_dir)
            save_choice(config_dir)
            print(f"Configured Dolphin in {config_dir}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
