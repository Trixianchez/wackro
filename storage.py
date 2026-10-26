"""Saving, loading and validating Wackro recordings.

File format (version 1): plain JSON, one event per line so it is easy to read, diff and hand-edit.

    {
      "version": 1,
      "screen": {"width": 1920, "height": 1080},
      "events": [
        {"t": 0.0, "type": "mouse_move", "x": 100, "y": 200},
        {"t": 0.25, "type": "mouse_click", "x": 100, "y": 200, "button": "left", "pressed": true},
        {"t": 0.4, "type": "mouse_scroll", "x": 100, "y": 200, "dx": 0, "dy": -1},
        {"t": 0.9, "type": "key_press", "key": {"kind": "char", "value": "a"}},
        {"t": 1.0, "type": "key_release", "key": {"kind": "special", "value": "enter"}}
      ]
    }

Version 2 (Wackro 1.5+) adds one optional field to mouse_click, mouse_scroll and key_press
events: the window that was in front / under the mouse, used by the failsafe to stop playback
when something else is in the way:

    {"t": 0.9, "type": "key_press", "key": {...}, "w": {"exe": "notepad.exe", "cls": "Notepad"}}

"exe" is the program file name and "cls" the window type (class). Never window titles or
contents. A recording without any "w" is still saved as version 1, so older versions open it.

"t" is seconds since the recording started. Keys come in three flavours, because pynput has
different key objects for them:

    {"kind": "special", "value": "enter"}            keyboard.Key.enter
    {"kind": "char", "value": "a"}                   a character (optionally with "vk": 65)
    {"kind": "vk", "value": 65}                      a key we only know the virtual-key code of

Everything that can go wrong with a recording raises RecordingError. Its message is written for
humans, so the GUI can show str(error) as-is.

PRIVACY: a recording contains every key that was pressed (passwords included) as plain text.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from config import (
    MAX_COORD,
    MAX_EVENTS,
    MAX_FILE_BYTES,
    MAX_RECORDING_SECONDS,
    MAX_SCREEN_DIM,
    MAX_SCROLL,
    MAX_VK,
    RECORDING_VERSION,
)

# Union of the Key names across pynput's Windows / macOS / Linux backends (pynput 1.8.2).
# Windows is the only one with f21-f24 and media_stop, macOS the only one with media_eject.
# tests/test_storage.py checks the real Key list of whatever machine it runs on against this.
SPECIAL_KEY_NAMES = frozenset("""
    alt alt_gr alt_l alt_r backspace caps_lock cmd cmd_l cmd_r ctrl ctrl_l ctrl_r delete down end
    enter esc f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12 f13 f14 f15 f16 f17 f18 f19 f20 f21 f22 f23
    f24 home insert left media_eject media_next media_play_pause media_previous media_stop
    media_volume_down media_volume_mute media_volume_up menu num_lock page_down page_up pause
    print_screen right scroll_lock shift shift_l shift_r space tab up
""".split())

BUTTON_NAMES = ("left", "right", "middle", "x1", "x2")

# Fields each event type must have (besides "t" and "type").
EVENT_FIELDS = {
    "mouse_move": ("x", "y"),
    "mouse_click": ("x", "y", "button", "pressed"),
    "mouse_scroll": ("x", "y", "dx", "dy"),
    "key_press": ("key",),
    "key_release": ("key",),
}

_TOP_LEVEL_FIELDS = ("version", "screen", "events")
_CONTEXT_EVENTS = ("mouse_click", "mouse_scroll", "key_press")
_CONTEXT_MAX = 260


class RecordingError(Exception):
    """Something is wrong with a recording or its file. str(error) is safe to show to the user."""


# ---- Small helpers ----------------------------------------------------------------------

def _short(value, limit: int = 40) -> str:
    """repr() of a value, cut down so a hostile file can't make a giant error message."""
    try:
        text = repr(value)
    except Exception:  # e.g. an int with thousands of digits, or an object with a broken __repr__
        return "<unprintable value>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _is_int(value) -> bool:
    return type(value) is int  # bool is a subclass of int, and must not count


def _is_number(value) -> bool:
    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value)


def _int_problem(name: str, value, low: int, high: int):
    if not _is_int(value):
        return f"'{name}' must be a whole number"
    if value < low or value > high:
        return f"'{name}' is out of range"
    return None


def _size_text(num_bytes: int) -> str:
    mib = 1024 * 1024
    return f"{num_bytes // mib} MB" if num_bytes >= mib else f"{num_bytes} bytes"


def _os_reason(error: OSError) -> str:
    return error.strerror or str(error)


# ---- Keys -------------------------------------------------------------------------------

def _keyboard():
    # Imported lazily so loading / saving / validating never needs pynput (or a display).
    from pynput import keyboard

    return keyboard


def _key_problem(data):
    """Return a short description of what's wrong with a serialized key, or None if it's fine."""
    if not isinstance(data, dict):
        return "key must be an object"
    kind = data.get("kind")
    if kind == "special":
        allowed = ("kind", "value")
    elif kind == "char":
        allowed = ("kind", "value", "vk")
    elif kind == "vk":
        allowed = ("kind", "value")
    else:
        return f"unknown key kind {_short(kind)}"

    if "value" not in data:
        return "key has no 'value'"
    for name in data:
        if name not in allowed:
            return f"unexpected field {_short(name)} in key"

    value = data["value"]
    if kind == "special":
        if not isinstance(value, str) or value not in SPECIAL_KEY_NAMES:
            return f"unknown special key {_short(value)}"
    elif kind == "char":
        if not isinstance(value, str) or len(value) != 1:
            return "a 'char' key must be exactly one character"
        if value == "\x00" or 0xD800 <= ord(value) <= 0xDFFF:
            return "that character can't be recorded"
        if "vk" in data:
            return _int_problem("vk", data["vk"], 1, MAX_VK)
    else:
        return _int_problem("value", value, 1, MAX_VK)
    return None


def key_to_json(key) -> dict:
    """Turn a pynput Key / KeyCode into the JSON form. Raises RecordingError if it can't be saved."""
    keyboard = _keyboard()
    if isinstance(key, keyboard.Key):
        data = {"kind": "special", "value": key.name}
    elif isinstance(key, keyboard.KeyCode):
        if key.char is not None:
            data = {"kind": "char", "value": key.char}
            if key.vk is not None:
                data["vk"] = key.vk
        elif key.vk is not None:
            data = {"kind": "vk", "value": key.vk}
        else:
            raise RecordingError("Can't record a key that has neither a character nor a key code.")
    else:
        raise RecordingError(f"Can't record {_short(key)} as a key.")

    problem = _key_problem(data)  # so everything we write is guaranteed to load again
    if problem:
        raise RecordingError(f"Can't record this key: {problem}.")
    return data


def key_from_json(data):
    """Turn the JSON form back into a pynput Key / KeyCode. Raises RecordingError if it's invalid."""
    problem = _key_problem(data)
    if problem:
        raise RecordingError(f"Invalid key: {problem}.")
    keyboard = _keyboard()
    kind = data["kind"]
    if kind == "special":
        try:
            return keyboard.Key[data["value"]]
        except KeyError:
            raise RecordingError(f"The key '{data['value']}' isn't available on this system.") from None
    if kind == "char":
        return keyboard.KeyCode(vk=data.get("vk"), char=data["value"])
    return keyboard.KeyCode.from_vk(data["value"])


# ---- Validation -------------------------------------------------------------------------

def _time_problem(t, previous_t):
    if not _is_number(t):
        return "'t' must be a finite number"
    if t < 0:
        return "'t' can't be negative"
    if t > MAX_RECORDING_SECONDS:
        return "'t' is too large"
    if t < previous_t:
        return f"time goes backwards ({t} after {previous_t})"
    return None


def _context_problem(context):
    if not isinstance(context, dict) or set(context) != {"exe", "cls"}:
        return "'w' must be an object with 'exe' and 'cls'"
    for name in ("exe", "cls"):
        value = context[name]
        if not isinstance(value, str) or not 1 <= len(value) <= _CONTEXT_MAX:
            return f"'w.{name}' must be a short text"
    return None


def _event_problem(event, previous_t, version=1):
    """Return a short description of what's wrong with one event, or None if it's fine."""
    if not isinstance(event, dict):
        return "must be a JSON object"
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return "missing or invalid 'type'"
    fields = EVENT_FIELDS.get(event_type)
    if fields is None:
        return f"unknown event type {_short(event_type)}"

    for name in ("t",) + fields:
        if name not in event:
            return f"missing field '{name}'"
    allowed = ("t", "type") + fields
    if version >= 2 and event_type in _CONTEXT_EVENTS:
        allowed += ("w",)
    for name in event:
        if name not in allowed:
            return f"unexpected field {_short(name)}"

    problem = _time_problem(event["t"], previous_t)
    if problem:
        return problem

    if event_type in ("mouse_move", "mouse_click", "mouse_scroll"):
        for name in ("x", "y"):
            problem = _int_problem(name, event[name], -MAX_COORD, MAX_COORD)
            if problem:
                return problem

    if event_type == "mouse_click":
        button = event["button"]
        if not (isinstance(button, str) and button in BUTTON_NAMES):
            return f"unknown mouse button {_short(button)}"
        if type(event["pressed"]) is not bool:
            return "'pressed' must be true or false"
    elif event_type == "mouse_scroll":
        for name in ("dx", "dy"):
            problem = _int_problem(name, event[name], -MAX_SCROLL, MAX_SCROLL)
            if problem:
                return problem
    elif event_type in ("key_press", "key_release"):
        problem = _key_problem(event["key"])
        if problem:
            return f"invalid key ({problem})"
    if "w" in event:
        problem = _context_problem(event["w"])
        if problem:
            return problem
    return None


def validate_recording(data):
    """Check a recording (as loaded from JSON). Returns it unchanged, or raises RecordingError."""
    if not isinstance(data, dict):
        raise RecordingError("This isn't a Wackro recording (the file must contain a JSON object).")

    # version first, so a file from a newer app gets the friendly message instead of "unexpected field"
    if "version" not in data:
        raise RecordingError("Missing 'version' - this doesn't look like a Wackro recording.")
    version = data["version"]
    if not _is_int(version):
        raise RecordingError("'version' must be a whole number.")
    if version > RECORDING_VERSION:
        raise RecordingError(
            "This recording was made by a newer version of Wackro. Update the app to open it."
        )
    if version < 1:
        raise RecordingError("Unsupported 'version' in this recording.")

    for name in data:
        if name not in _TOP_LEVEL_FIELDS:
            raise RecordingError(f"Unexpected field {_short(name)} at the top level of the recording.")

    if "screen" not in data:
        raise RecordingError("Missing 'screen'.")
    screen = data["screen"]
    if not isinstance(screen, dict):
        raise RecordingError("'screen' must be an object with 'width' and 'height'.")
    for name in ("width", "height"):
        if name not in screen:
            raise RecordingError(f"'screen' is missing '{name}'.")
    for name in screen:
        if name not in ("width", "height"):
            raise RecordingError(f"'screen' has an unexpected field {_short(name)}.")
    for name in ("width", "height"):
        problem = _int_problem(f"screen.{name}", screen[name], 1, MAX_SCREEN_DIM)
        if problem:
            raise RecordingError(f"{problem}.")

    if "events" not in data:
        raise RecordingError("Missing 'events'.")
    events = data["events"]
    if not isinstance(events, list):
        raise RecordingError("'events' must be a list.")
    if len(events) > MAX_EVENTS:
        raise RecordingError(f"Too many events ({len(events):,}); the limit is {MAX_EVENTS:,}.")

    previous_t = 0
    for number, event in enumerate(events, start=1):
        problem = _event_problem(event, previous_t, version)
        if problem:
            raise RecordingError(f"Event #{number}: {problem}.")
        previous_t = event["t"]
    return data


def make_recording(events: list, screen_width: int, screen_height: int) -> dict:
    """Wrap a list of events into a complete recording dict (validated)."""
    with_context = any(isinstance(e, dict) and "w" in e for e in events)
    recording = {
        "version": RECORDING_VERSION if with_context else 1,
        "screen": {"width": screen_width, "height": screen_height},
        "events": events,
    }
    return validate_recording(recording)


# ---- Save / load ------------------------------------------------------------------------

def _to_text(recording: dict) -> str:
    """Pretty JSON with exactly one event per line (and plain ASCII, so encodings can't bite)."""
    events = recording["events"]
    lines = [
        "{",
        f'  "version": {recording["version"]},',
        f'  "screen": {json.dumps(recording["screen"])},',
        '  "events": [',
    ]
    last = len(events) - 1
    for index, event in enumerate(events):
        lines.append("    " + json.dumps(event, allow_nan=False) + ("," if index < last else ""))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def save_recording(path, recording: dict) -> None:
    """Validate and save a recording. The write is atomic: a failed save never damages an existing file."""
    validate_recording(recording)
    write_text_atomic(path, _to_text(recording))


def write_text_atomic(path, text: str) -> None:
    """Write text so that a failed save never damages an existing file (temp file + rename).
    Raises RecordingError with a friendly message."""
    path = Path(path)

    temp_path = None
    try:
        fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    except OSError as error:
        raise RecordingError(f"Couldn't save the file: {_os_reason(error)}.") from error
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _reject_constant(name: str):
    raise ValueError(f"the file contains '{name}', which isn't a valid number")


def _parse_json(text: str):
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except RecursionError:
        raise RecordingError("This file is nested too deeply to be a recording.") from None
    except json.JSONDecodeError as error:
        raise RecordingError(
            f"This isn't valid JSON (line {error.lineno}, column {error.colno}): {error.msg}."
        ) from None
    except ValueError as error:  # NaN / Infinity, or a number with an absurd number of digits
        raise RecordingError(f"This file can't be read as a recording: {error}.") from None


def load_recording(path) -> dict:
    """Load and validate a recording. Raises RecordingError with a friendly message on any problem."""
    return validate_recording(read_json_file(path))


def read_json_file(path, max_bytes=None, what: str = "a recording"):
    """Read and parse a JSON file with the same size / encoding / JSON checks as recordings.
    Returns the parsed data (not validated). Raises RecordingError with a friendly message."""
    max_bytes = MAX_FILE_BYTES if max_bytes is None else max_bytes
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except FileNotFoundError:
        raise RecordingError(f"Couldn't find the file '{path.name}'.") from None
    except OSError as error:
        raise RecordingError(f"Couldn't open the file: {_os_reason(error)}.") from error

    if len(raw) > max_bytes:
        raise RecordingError(
            f"That file is too big to be {what} (the limit is {_size_text(max_bytes)})."
        )
    try:
        text = raw.decode("utf-8-sig")  # utf-8-sig also copes with the BOM Windows editors like to add
    except UnicodeDecodeError:
        raise RecordingError("That isn't a text file, so it can't be a Wackro recording.") from None
    if not text.strip():
        raise RecordingError("The file is empty.")
    return _parse_json(text)
