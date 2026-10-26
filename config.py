"""Every tunable constant for Wackro lives here.

Keep this file free of pynput / tkinter imports so it is safe to import from anywhere,
including the tests.
"""

import os
import sys
from pathlib import Path

# ---- App info ---------------------------------------------------------------------------
APP_NAME = "Wackro"
APP_VERSION = "1.7.5"

# Window icon, relative to the project root (and bundled into the .exe by Wackro.spec).
ICON_RELATIVE_PATH = Path("assets") / "Wackro.ico"
# System tray icons (32x32 PNGs): the dot shows what the app is doing.
TRAY_ICON_PATHS = {
    "idle": Path("assets") / "tray_idle.png",
    "recording": Path("assets") / "tray_recording.png",
    "playing": Path("assets") / "tray_playing.png",
}


def resource_path(relative) -> Path:
    """Where a bundled file lives: next to this file when running from source, inside PyInstaller's
    temporary folder (sys._MEIPASS) when running as the .exe."""
    bundle = getattr(sys, "_MEIPASS", None)
    root = Path(bundle) if bundle else Path(__file__).resolve().parent
    return root / relative


# ---- Global hotkeys ---------------------------------------------------------------------
# The keys themselves are set by the user (Settings > Hotkeys) and stored in settings.json.
# Defaults and the key-name tables live in hotkey_spec.py.

# ---- Recording file format --------------------------------------------------------------
RECORDING_VERSION = 2               # 2 adds the optional window context ("w")
RECORDING_EXTENSION = ".json"
RECORDING_FILE_TYPES = [("Wackro recording", "*.json"), ("All files", "*.*")]

# ---- Recorder (step 2) ------------------------------------------------------------------
MOVE_MIN_INTERVAL = 0.010          # seconds between recorded mouse moves (throttle)
MAX_EVENTS = 200_000               # recorder stops itself here; loader rejects anything bigger
MAX_RECORDING_SECONDS = 24 * 60 * 60
TAIL_TRIM_SECONDS = 0.5            # trimmed off the end when recording is stopped with the GUI button
NO_DELAY_GAP = 0.02                # seconds between events when Recording > Options > Delay is off

# ---- Delays (steps 2-4) -----------------------------------------------------------------
WINDOW_MINIMIZE_DELAY = 0.4        # let the window finish minimizing before recording / playing
PLAYBACK_START_DELAY = 0.3         # small pause between pressing Play and the first event

# ---- Playback repeat (step 8) -----------------------------------------------------------
DEFAULT_REPEAT = 1                 # number of runs; 0 means "until Stop"
DEFAULT_REPEAT_DELAY = 0.0         # seconds between runs
MAX_REPEAT = 1_000_000
MAX_REPEAT_DELAY = 24 * 60 * 60.0
MIN_PASS_SECONDS = 0.05            # a run never takes less than this, so an endless loop of a
                                   # zero-length recording can't spin the CPU
MIN_SPEED = 0.1
MAX_SPEED = 10.0
DEFAULT_SPEED = 1.0

# ---- Sanity limits used when validating a recording file --------------------------------
MAX_FILE_BYTES = 64 * 1024 * 1024  # 200k events is roughly 20 MB, so this has plenty of room
MAX_COORD = 100_000                # |x| and |y| (negative is fine on multi-monitor setups)
MAX_SCROLL = 10_000                # |dx| and |dy|
MAX_SCREEN_DIM = 100_000           # screen width / height
MAX_VK = 0xFFFF                    # virtual-key codes

# ---- Logging ----------------------------------------------------------------------------
_local_appdata = os.environ.get("LOCALAPPDATA")
if _local_appdata:
    LOG_DIR = Path(_local_appdata) / APP_NAME
else:
    LOG_DIR = Path.home() / ("." + APP_NAME.lower())
LOG_PATH = LOG_DIR / "wackro.log"
SETTINGS_PATH = LOG_DIR / "settings.json"
# Before 1.7.0 the app was called Wackylloop; its settings are carried over once (see main.py).
OLD_DATA_DIRS = (LOG_DIR.parent / ("Wackylloop" if _local_appdata else ".wackylloop"),)
CARRIED_OVER_FILES = ("settings.json", "brightness_restore.json")


def carry_over_old_data(new_dir: Path = None, old_dirs=None) -> list:
    """Copy settings (and a pending brightness restore) from the old Wackylloop folder, if this
    is the first start as Wackro. Never overwrites anything; returns the files copied."""
    import shutil

    new_dir = Path(new_dir) if new_dir is not None else LOG_DIR
    copied = []
    for old in (old_dirs if old_dirs is not None else OLD_DATA_DIRS):
        old = Path(old)
        if old == new_dir or not old.is_dir():
            continue
        for name in CARRIED_OVER_FILES:
            source, target = old / name, new_dir / name
            if source.is_file() and not target.exists():
                try:
                    new_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied.append(name)
                except OSError:
                    pass
    return copied
