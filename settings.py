"""What Wackro remembers between runs, as a small JSON file (config.SETTINGS_PATH).

    settings = load_settings()      # always returns a complete, valid dict
    save_settings(settings)         # returns True/False, never raises

    {
      "repeat": 5, "infinite": false, "delay": 1.5,          Playback > Repeat
      "speed": 1.0,                                          Playback > Speed
      "record_keyboard": true, "record_clicks": true,        Recording > Options
      "record_moves": true, "record_delay": true,
      "hotkeys": {"record": {"mods": ["ctrl"], "vk": 192},   Settings > Hotkeys
                  "play": {"mods": [], "vk": 144},
                  "stop": {"mods": [], "vk": 121}},
      "close_action": "ask",                                 the window's X: "ask", "tray" or "exit"
      "indicator": "both",                                   on-screen sign: both / border / pill / off
      "indicator_playback": true,                            also show it (green) while playing
      "fs_keys": true, "fs_clicks": true,                    Settings > Safety (failsafe.py): stop
      "fs_move": true, "fs_move_sensitivity": "medium",      playback when you take over, a popup /
      "fs_corner": true, "fs_window": true,                  another program gets in the way, or a
      "fs_preflight": true, "fs_crash": true,                program crashed / stopped responding,
      "fs_locked": true, "fs_screen": true, "fs_sleep": true,  the PC got locked / screen changed / slept
      "time_limit_on": false, "time_limit": 60.0,            Safety: stop playing after N minutes
      "keep_awake": true,                                    Settings > Blackout & power
      "blackout": false, "blackout_after": 5.0,              black screen after N minutes of playing
      "blackout_dim": true, "blackout_text": true,           brightness 0 / small status line on it
      "blackout_hotkey": null,                               optional hotkey, e.g. {"mods": [], "vk": 123}
      "recordings_dir": null                                 Settings > Recordings folder; null = default
                                                             (Setup's choice, else Documents\\Wackro)
    }

repeat    number of runs, 1 to MAX_REPEAT (ignored while "infinite" is true)
infinite  keep playing until Stop
delay     seconds between runs, 0 to MAX_REPEAT_DELAY
speed     playback speed, MIN_SPEED to MAX_SPEED

A missing, unreadable or hand-broken file is never an error: every field that is wrong falls back
to its default on its own, so one bad value doesn't throw away the others. Settings are a
convenience, so failing to save one is logged and otherwise ignored.

This is NOT part of the recording file (storage.py): a recording stays a plain list of events that
plays the same way everywhere, and these settings belong to the person using the app.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Optional

from config import (
    DEFAULT_REPEAT,
    DEFAULT_REPEAT_DELAY,
    DEFAULT_SPEED,
    MAX_REPEAT,
    MAX_REPEAT_DELAY,
    MAX_SPEED,
    MIN_SPEED,
    SETTINGS_PATH,
)
from failsafe import DEFAULT_OPTIONS as FAILSAFE_DEFAULTS
from failsafe import SENSITIVITY_PIXELS
from folders import valid_folder_text
from hotkey_spec import DEFAULT_BINDINGS, sanitize_binding, sanitize_bindings, same_keys

log = logging.getLogger(__name__)

_MAX_SETTINGS_BYTES = 64 * 1024   # a settings file is a few dozen bytes; anything huge isn't ours


RECORD_OPTIONS = ("record_keyboard", "record_clicks", "record_moves", "record_delay")
CLOSE_ACTIONS = ("ask", "tray", "exit")
INDICATOR_STYLES = ("both", "border", "pill", "off")
BLACKOUT_AFTER_RANGE = (0.5, 1440.0)      # minutes
TIME_LIMIT_RANGE = (1.0, 10080.0)         # minutes (a week)
_BOOL_KEYS = ("time_limit_on", "keep_awake", "blackout", "blackout_dim", "blackout_text")


def default_settings() -> dict:
    result = {
        "repeat": DEFAULT_REPEAT,
        "infinite": False,
        "delay": DEFAULT_REPEAT_DELAY,
        "speed": DEFAULT_SPEED,
    }
    for name in RECORD_OPTIONS:
        result[name] = True
    result["hotkeys"] = {action: dict(b, mods=list(b["mods"])) for action, b in DEFAULT_BINDINGS.items()}
    result["close_action"] = "ask"
    result["indicator"] = "both"
    result["indicator_playback"] = True
    result.update(FAILSAFE_DEFAULTS)
    result.update(time_limit_on=False, time_limit=60.0, keep_awake=True, blackout=False,
                  blackout_after=5.0, blackout_dim=True, blackout_text=True, blackout_hotkey=None)
    result["recordings_dir"] = None
    return result


def _number_in(value, low, high) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and low <= value <= high
    )


def sanitize(data) -> dict:
    """Turn whatever was read from disk into a complete, valid settings dict."""
    result = default_settings()
    if not isinstance(data, dict):
        return result

    repeat = data.get("repeat")
    if isinstance(repeat, int) and not isinstance(repeat, bool) and 1 <= repeat <= MAX_REPEAT:
        result["repeat"] = repeat

    infinite = data.get("infinite")
    if isinstance(infinite, bool):
        result["infinite"] = infinite

    delay = data.get("delay")
    if _number_in(delay, 0, MAX_REPEAT_DELAY):
        result["delay"] = float(delay)

    speed = data.get("speed")
    if _number_in(speed, MIN_SPEED, MAX_SPEED):
        result["speed"] = float(speed)

    for name in RECORD_OPTIONS:
        if isinstance(data.get(name), bool):
            result[name] = data[name]

    if "hotkeys" in data:
        result["hotkeys"] = sanitize_bindings(data["hotkeys"])

    if data.get("close_action") in CLOSE_ACTIONS:
        result["close_action"] = data["close_action"]

    if data.get("indicator") in INDICATOR_STYLES:
        result["indicator"] = data["indicator"]
    if isinstance(data.get("indicator_playback"), bool):
        result["indicator_playback"] = data["indicator_playback"]

    for name in FAILSAFE_DEFAULTS:
        value = data.get(name)
        if name == "fs_move_sensitivity":
            if value in SENSITIVITY_PIXELS:
                result[name] = value
        elif isinstance(value, bool):
            result[name] = value

    for name in _BOOL_KEYS:
        if isinstance(data.get(name), bool):
            result[name] = data[name]
    if _number_in(data.get("blackout_after"), *BLACKOUT_AFTER_RANGE):
        result["blackout_after"] = float(data["blackout_after"])
    if _number_in(data.get("time_limit"), *TIME_LIMIT_RANGE):
        result["time_limit"] = float(data["time_limit"])
    hotkey = sanitize_binding(data.get("blackout_hotkey")) if data.get("blackout_hotkey") is not None else None
    if hotkey is not None and not any(same_keys(hotkey, b) for b in result["hotkeys"].values()):
        result["blackout_hotkey"] = hotkey
    if valid_folder_text(data.get("recordings_dir")):
        result["recordings_dir"] = data["recordings_dir"].strip()
    return result


def load_settings(path: Optional[Path] = None) -> dict:
    path = Path(path) if path is not None else SETTINGS_PATH
    try:
        if path.stat().st_size > _MAX_SETTINGS_BYTES:
            log.warning("Ignoring %s: too large to be a settings file", path)
            return default_settings()
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_settings()
    except (OSError, UnicodeDecodeError):
        log.warning("Could not read settings from %s", path, exc_info=True)
        return default_settings()
    try:
        data = json.loads(text)
    except ValueError:
        log.warning("Settings file %s is not valid JSON; using defaults", path)
        return default_settings()
    return sanitize(data)


def save_settings(settings: dict, path: Optional[Path] = None) -> bool:
    """Write `settings` (sanitized first). Atomic: a crash mid-write can't leave a half file."""
    path = Path(path) if path is not None else SETTINGS_PATH
    text = json.dumps(sanitize(settings), indent=2) + "\n"
    temp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
        temp_name = None
        return True
    except OSError:
        log.warning("Could not save settings to %s", path, exc_info=True)
        return False
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
