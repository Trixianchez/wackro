"""Where recordings and sequences are saved by default. Pure Python, no tkinter.

Before 1.7.5 the Save / Open boxes had no starting folder, so Windows opened them wherever the
exe was started from: run Wackro.exe from Downloads and the recordings landed in Downloads too.
Now every Save / Open box starts in the recordings folder:

* settings["recordings_dir"] if the user picked one (Settings > Recordings folder…), else
* the folder Setup was told to use (install.json next to the exe, written by the installer), else
* Documents\\Wackro. Documents is asked from Windows, so a OneDrive-redirected Documents works.

The folder is created when a box needs it. If it can't be created (a removed USB stick, no
rights), the box starts in Documents or the home folder instead: saving must never fail just
because the preferred folder is gone.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

FOLDER_NAME = "Wackro"
MAX_PATH_CHARS = 1024
INSTALL_INFO_NAME = "install.json"   # written by the installer next to Wackro.exe


def valid_folder_text(value) -> bool:
    """True if `value` can be stored as a folder: an absolute path, not absurdly long."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_PATH_CHARS or "\0" in value:
        return False
    return os.path.isabs(value.strip())


def documents_dir(documents_from_windows: Optional[Callable[[], Optional[str]]] = None) -> Path:
    """The user's Documents folder (Windows' answer first, then ~/Documents, then ~)."""
    if documents_from_windows is not None:
        try:
            found = documents_from_windows()
        except Exception:
            log.warning("Could not ask Windows for the Documents folder", exc_info=True)
            found = None
        if found and valid_folder_text(found):
            return Path(found)
    home = Path.home()
    documents = home / "Documents"
    return documents if documents.is_dir() else home


def install_dir() -> Path:
    """The folder the running program lives in (the exe's folder when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def installer_choice(folder: Optional[Path] = None) -> Optional[str]:
    """The recordings folder picked in Setup, if any (install.json: {"recordings_dir": "..."})."""
    path = (Path(folder) if folder is not None else install_dir()) / INSTALL_INFO_NAME
    try:
        if path.stat().st_size > 64 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError):
        log.warning("Ignoring %s: not readable", path, exc_info=True)
        return None
    value = data.get("recordings_dir") if isinstance(data, dict) else None
    return value.strip() if valid_folder_text(value) else None


def default_recordings_dir(documents_from_windows=None, install_folder: Optional[Path] = None) -> Path:
    """What "Reset to default" means: Setup's choice, else Documents\\Wackro."""
    chosen = installer_choice(install_folder)
    if chosen:
        return Path(chosen)
    return documents_dir(documents_from_windows) / FOLDER_NAME


def recordings_dir(settings: dict, documents_from_windows=None, install_folder: Optional[Path] = None) -> Path:
    """The folder the user wants (their own pick, else the default)."""
    picked = settings.get("recordings_dir") if isinstance(settings, dict) else None
    if valid_folder_text(picked):
        return Path(picked.strip())
    return default_recordings_dir(documents_from_windows, install_folder)


def ensure_dir(path: Path) -> bool:
    """Create the folder (and its parents) if needed. True if it exists afterwards."""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return Path(path).is_dir()
    except OSError:
        log.warning("Could not create the folder %s", path, exc_info=True)
        return False


def dialog_start_dir(settings: dict, documents_from_windows=None, install_folder: Optional[Path] = None) -> str:
    """Where a Save / Open box should start: the recordings folder (created if needed), or
    Documents / home if that can't be used."""
    wanted = recordings_dir(settings, documents_from_windows, install_folder)
    if ensure_dir(wanted):
        return str(wanted)
    fallback = documents_dir(documents_from_windows)
    return str(fallback if fallback.is_dir() else Path.home())
