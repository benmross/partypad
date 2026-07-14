# PyInstaller specification for the unsigned native alpha CLI.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_submodules

root = Path.cwd()
hidden = collect_submodules("aiortc") + ["dashboard"]
if sys.platform.startswith("linux"):
    hidden += ["uinput_backend", "hotspot", "ap_helper"]

analysis = Analysis(
    [str(root / "partypad.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "static"), "static")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="partypad",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
