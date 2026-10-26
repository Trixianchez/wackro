# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Wackro. Build with build.ps1 (or by hand, see README.md).
#
#   Wackro.exe        normal build (default)
#   Wackro-Admin.exe  same app with a UAC manifest, i.e. it always asks for administrator
#                        rights. Set WACKRO_ADMIN=1 before running pyinstaller to get this one.
#
# Both are onefile, windowed (no console) builds (WACKRO_ONEDIR=1: the installer's folder build). The window icon / exe icon comes from
# assets/Wackro.ico; without that file the build still works, just with the default icon.
# The tray icons (assets/tray_*.png) are bundled too; without them the tray shows plain dots.

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)  # noqa: F821  (SPECPATH is provided by PyInstaller)
ADMIN = os.environ.get("WACKRO_ADMIN") == "1"
# WACKRO_ONEDIR=1: a normal folder (Wackro.exe + _internal\) for the installer. It starts faster
# than the onefile exe (nothing to unpack each time) and antivirus flags it less. Admin rights come
# from the "--admin" shortcut (main.py) instead of a second exe.
ONEDIR = os.environ.get("WACKRO_ONEDIR") == "1"
if ONEDIR and ADMIN:
    raise SystemExit("WACKRO_ONEDIR and WACKRO_ADMIN can't be combined")
NAME = "Wackro-Admin" if ADMIN else "Wackro"
ICON = ROOT / "assets" / "Wackro.ico"
TRAY_ICONS = [ROOT / "assets" / f"tray_{state}.png" for state in ("idle", "recording", "playing")]

# customtkinter ships theme .json files and fonts that PyInstaller can't discover by itself.
datas, binaries, hiddenimports = collect_all("customtkinter")

# pynput and pystray pick their backend at runtime with importlib, which PyInstaller can't see.
if sys.platform == "win32":
    hiddenimports += ["pynput.keyboard._win32", "pynput.mouse._win32", "pynput._util.win32",
                      "pystray._win32"]
elif sys.platform == "darwin":
    hiddenimports += ["pynput.keyboard._darwin", "pynput.mouse._darwin", "pynput._util.darwin",
                      "pystray._darwin"]
else:
    hiddenimports += ["pynput.keyboard._xorg", "pynput.mouse._xorg", "pynput._util.xorg",
                      "pystray._xorg"]

icon_path = None
if ICON.is_file():
    icon_path = str(ICON)
    datas += [(icon_path, "assets")]  # config.resource_path("assets/Wackro.ico") finds it here
else:
    print(f"WARNING: {ICON} not found, building without an icon.")

for tray_icon in TRAY_ICONS:
    if tray_icon.is_file():
        datas += [(str(tray_icon), "assets")]   # config.TRAY_ICON_PATHS
    else:
        print(f"WARNING: {tray_icon} not found, the tray will show a plain dot instead.")

a = Analysis(  # noqa: F821
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

if ONEDIR:
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        icon=icon_path,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)  # noqa: F821
else:
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,          # UPX-packed exes get flagged by antivirus far more often
        console=False,      # no console window; main.py logs to a file instead
        icon=icon_path,
        uac_admin=ADMIN,
    )
