"""System registry for controller modes and emulator backends.

Adding a system here makes it a known PartyPad target. Mark it supported only
after its controller mode, backend mapping, documentation, and tests exist.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SystemSpec:
    id: str
    label: str
    backend: str
    controller_mode: str
    supported: bool = False
    detail: str = "Planned RetroArch system"


_PLANNED = (
    ("amiga", "Amiga"),
    ("amstradcpc", "Amstrad CPC"),
    ("arcade", "Arcade"),
    ("atari2600", "Atari 2600"),
    ("atari5200", "Atari 5200"),
    ("atari7800", "Atari 7800"),
    ("atarilynx", "Atari Lynx"),
    ("bbcmicro", "BBC Micro"),
    ("c64", "Commodore 64"),
    ("coleco", "ColecoVision"),
    ("cps", "Capcom Play System"),
    ("daphne", "Daphne"),
    ("doom", "Doom"),
    ("dosbox", "DOSBox"),
    ("fba", "FinalBurn Alpha"),
    ("fds", "Famicom Disk System"),
    ("gamegear", "Game Gear"),
    ("gb", "Game Boy"),
    ("gba", "Game Boy Advance"),
    ("gbc", "Game Boy Color"),
    ("gw", "Game & Watch"),
    ("intelli", "Intellivision"),
    ("mastersystem", "Master System"),
    ("megadrive", "Mega Drive / Genesis"),
    ("msx", "MSX"),
    ("neogeo", "Neo Geo"),
    ("ngp", "Neo Geo Pocket"),
    ("pcecd", "PC Engine CD"),
    ("pcengine", "PC Engine"),
    ("pico8", "PICO-8"),
    ("pokemini", "Pokémon Mini"),
    ("psx", "PlayStation"),
    ("quake", "Quake"),
    ("scummvm", "ScummVM"),
    ("sega32x", "Sega 32X"),
    ("segacd", "Sega CD"),
    ("sg-1000", "SG-1000"),
    ("snes", "Super Nintendo"),
    ("supervision", "Watara Supervision"),
    ("test", "Generic test controller"),
    ("tic80", "TIC-80"),
    ("vb", "Virtual Boy"),
    ("wsc", "WonderSwan Color"),
    ("zx", "ZX Spectrum"),
)


SYSTEMS = {
    "nes": SystemSpec(
        "nes",
        "Nintendo Entertainment System",
        "retroarch",
        "nes",
        True,
        "RetroArch via uinput; full-screen NES controller",
    ),
    "wii": SystemSpec(
        "wii",
        "Nintendo Wii",
        "dolphin",
        "wii",
        True,
        "Dolphin via DSU; Wii Remote controller",
    ),
    **{
        system_id: SystemSpec(system_id, label, "retroarch", system_id)
        for system_id, label in _PLANNED
    },
}

SUPPORTED_SYSTEMS = tuple(spec for spec in SYSTEMS.values() if spec.supported)


def get_system(system_id: str) -> SystemSpec:
    return SYSTEMS[system_id]
