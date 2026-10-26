"""Records global mouse and keyboard input into a list of events (the format storage.py saves).

    recorder = Recorder(on_auto_stop=handle_auto_stop)
    recorder.start()              # begins listening
    ...                           # recorder.event_count / recorder.elapsed are safe to poll
    events = recorder.stop()      # list of event dicts, ready for storage.make_recording()

What it does on top of plain listening:

* Every event is stamped from ONE clock, under one lock, so timestamps never go backwards even
  though the mouse and keyboard listeners run in different threads (storage rejects files where
  time goes backwards).
* The recording starts with a mouse_move at t=0 (where the cursor was), so playback starts from
  the same spot.
* Mouse moves are throttled to one per MOVE_MIN_INTERVAL.
* Hotkeys are dropped: a key press is left out when it, together with the modifiers held at that
  moment, is one of the user's hotkeys (so a plain ` is still recorded, Ctrl+` isn't). When
  recording is stopped with a hotkey like Ctrl+`, stop(drop_modifiers={"ctrl"}) also removes the
  Ctrl press that was part of it, so Ctrl doesn't end up held at the end of the recording.
* Recording options (start(options=...)): keyboard / clicks / moves can be left out, and with
  "delay" off the events are spaced NO_DELAY_GAP apart instead of at their real times.
* Holding a key only records the first press; the OS auto-repeat presses are dropped.
* Releases without a recorded press are dropped (e.g. a key that was already held when recording
  began).
* Keys that can't be saved are skipped and counted (skipped_keys).
* When recording stops, anything still held down gets a release event, so playback never leaves a
  key or a mouse button stuck.
* Recording stops itself at MAX_EVENTS / MAX_RECORDING_SECONDS and calls on_auto_stop.

Threading: handlers run on pynput's listener threads. on_auto_stop is called from one of them, so
the GUI must hop to its own thread (self.after(0, ...)) before touching widgets or calling stop().
start(), stop() and the read-only properties are meant for the GUI thread.

pynput is imported lazily (in start), so importing this module never needs a display.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from typing import Callable, Optional

from config import (
    MAX_COORD,
    MAX_EVENTS,
    MAX_RECORDING_SECONDS,
    MAX_SCROLL,
    MOVE_MIN_INTERVAL,
    NO_DELAY_GAP,
    TAIL_TRIM_SECONDS,
)
from hotkey_spec import DEFAULT_BINDINGS, MODIFIER_KEY_NAMES, key_to_vk, sanitize_bindings
from storage import BUTTON_NAMES, RecordingError, key_to_json

log = logging.getLogger(__name__)

# The recorder stops accepting events this far below MAX_EVENTS, which leaves room for the
# release events added when recording stops (one per key / button still held down).
_RESERVE_EVENTS = 100

_TIME_DECIMALS = 4  # timestamps are rounded to 0.1 ms, which keeps the files small

DEFAULT_OPTIONS = {"keyboard": True, "clicks": True, "moves": True, "delay": True}

# recorded special-key name -> modifier name ("ctrl_l" -> "ctrl")
_MODIFIER_OF_KEY = {name: mod for mod, names in MODIFIER_KEY_NAMES.items() for name in names}


# ---- Small helpers ----------------------------------------------------------------------

def _to_int(value, limit: int) -> Optional[int]:
    """Round to a whole number within +-limit. None if it isn't a usable number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return max(-limit, min(limit, int(round(value))))


def key_identity(data: dict):
    """What makes two serialized keys 'the same physical key' (so repeats and releases match up).

    The virtual-key code is used when there is one, because holding Shift changes the character
    ('a' is pressed, 'A' is released) but never the code.
    """
    kind = data["kind"]
    if kind == "special":
        return ("special", data["value"])
    if kind == "vk":
        return ("vk", data["value"])
    if data.get("vk") is not None:
        return ("vk", data["vk"])
    return ("char", data["value"].lower())


def _held_modifier_presses(events: list, modifiers) -> set:
    """Indexes of the key_press events of `modifiers` ("ctrl", ...) still held at the end."""
    held = {}
    for index, event in enumerate(events):
        if event["type"] not in ("key_press", "key_release"):
            continue
        identity = key_identity(event["key"])
        if event["type"] == "key_press":
            held[identity] = index
        else:
            held.pop(identity, None)
    result = set()
    for identity, index in held.items():
        key = events[index]["key"]
        if key["kind"] == "special" and _MODIFIER_OF_KEY.get(key["value"]) in modifiers:
            result.add(index)
    return result


def finish_events(events: list, end_t: float, trim_tail: bool = False, drop_modifiers=()) -> list:
    """Turn the raw event list into the final one. Pure function, no pynput needed.

    * trim_tail drops everything in the last TAIL_TRIM_SECONDS (the move and click that pressed
      the Stop button).
    * drop_modifiers ({"ctrl"}, ...) removes those modifiers' presses that are still held at the
      end: they were part of the hotkey that stopped the recording (Ctrl of Ctrl+`).
    * Any key or mouse button still held down gets a release event at the end.
    """
    cutoff = max(0.0, end_t - TAIL_TRIM_SECONDS) if trim_tail else end_t
    kept = []
    for event in events:
        if event["t"] > cutoff:
            break  # events are in time order
        kept.append(event)
    if drop_modifiers:
        dropped = _held_modifier_presses(kept, set(drop_modifiers))
        kept = [event for index, event in enumerate(kept) if index not in dropped]

    last_t = kept[-1]["t"] if kept else 0.0
    release_t = round(max(last_t, cutoff), _TIME_DECIMALS)

    held_keys: dict = {}
    held_buttons: dict = {}
    last_pos = None
    for event in kept:
        kind = event["type"]
        if "x" in event:
            last_pos = (event["x"], event["y"])
        if kind == "key_press":
            held_keys[key_identity(event["key"])] = event["key"]
        elif kind == "key_release":
            held_keys.pop(key_identity(event["key"]), None)
        elif kind == "mouse_click":
            if event["pressed"]:
                held_buttons[event["button"]] = True
            else:
                held_buttons.pop(event["button"], None)

    releases = []
    if last_pos is not None:
        for button in held_buttons:
            releases.append({"t": release_t, "type": "mouse_click", "x": last_pos[0],
                             "y": last_pos[1], "button": button, "pressed": False})
    for key in reversed(list(held_keys.values())):  # last pressed, first released
        releases.append({"t": release_t, "type": "key_release", "key": key})

    return kept + releases[: max(0, MAX_EVENTS - len(kept))]


def retime(events: list, gap: float = NO_DELAY_GAP) -> list:
    """Same events, spaced `gap` seconds apart (Recording > Options > Delay off)."""
    return [dict(event, t=round(index * gap, _TIME_DECIMALS)) for index, event in enumerate(events)]


# ---- The recorder -----------------------------------------------------------------------

class Recorder:
    """Records global input. See the module docstring."""

    def __init__(
        self,
        on_auto_stop: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.perf_counter,
        position_func: Optional[Callable[[], tuple]] = None,
        windows_info=None,
    ):
        """on_auto_stop(reason) is called (from a listener thread) when the recorder stopped taking
        events by itself; reason is "limit" (too many events), "time" (too long) or "error". The
        GUI should then call stop(). clock and position_func exist so tests can fake them.

        windows_info: something with foreground_context() and context_at(x, y) (winutil.Win32),
        used to note which program each click / scroll / key press went to, so playback can check
        it's still the same one (failsafe.WindowGuard). Default: the real one on Windows, none
        elsewhere."""
        self._on_auto_stop = on_auto_stop
        self._clock = clock
        self._position_func = position_func

        self._lock = threading.Lock()
        self._active = False        # between start() and stop()
        self._accepting = False     # handlers only record while this is True
        self._listeners: tuple = ()
        self._events: list = []
        self._result: list = []
        self._t0 = 0.0
        self._last_t = 0.0
        self._last_move_t = 0.0
        self._duration = 0.0
        self._down_keys: dict = {}
        self._down_buttons: set = set()
        self._pending_notice: Optional[str] = None
        self._stop_reason: Optional[str] = None
        self._options = dict(DEFAULT_OPTIONS)
        self._hotkeys: tuple = ()     # ((vk, frozenset(mods)), ...) left out of the recording
        self._windows = sys.platform.startswith("win")
        self._windows_info = windows_info
        if windows_info is None and self._windows:
            try:
                import winutil

                self._windows_info = winutil.get_win32()
            except Exception:
                log.warning("Window context unavailable; recordings won't note programs", exc_info=True)

        self.skipped_keys = 0       # keys that couldn't be saved (written by the keyboard thread)
        self.skipped_buttons = 0    # mouse buttons we have no name for (written by the mouse thread)

    # ---- Read-only state for the GUI ----

    @property
    def is_recording(self) -> bool:
        return self._active

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def elapsed(self) -> float:
        """Seconds since recording started; frozen once it has stopped."""
        if self._active:
            return max(0.0, self._clock() - self._t0)
        return self._duration

    @property
    def stop_reason(self) -> Optional[str]:
        """None, or "limit" / "time" / "error" when the recorder stopped taking events by itself."""
        return self._stop_reason

    @property
    def stop_message(self) -> str:
        """A GUI-ready sentence for stop_reason ("" if there is none)."""
        if self._stop_reason == "limit":
            return f"Recording stopped: it reached the limit of {MAX_EVENTS:,} events."
        if self._stop_reason == "time":
            hours = MAX_RECORDING_SECONDS / 3600
            return f"Recording stopped: it reached the limit of {hours:g} hours."
        if self._stop_reason == "error":
            return "Recording stopped because something went wrong. Check the log file."
        return ""

    @property
    def last_events(self) -> list:
        """The events of the most recent finished recording (empty before the first one)."""
        return list(self._result)

    # ---- Start / stop ----

    def start(self, hotkeys: Optional[dict] = None, options: Optional[dict] = None) -> None:
        """Begin recording. Raises RecordingError if input can't be listened to.

        hotkeys: the user's bindings (hotkey_spec format); those key presses are left out.
        options: {"keyboard", "clicks", "moves", "delay"} -> bool, see DEFAULT_OPTIONS.
        """
        bindings = sanitize_bindings(hotkeys if hotkeys is not None else DEFAULT_BINDINGS)
        merged = dict(DEFAULT_OPTIONS)
        for name, value in (options or {}).items():
            if name in merged:
                merged[name] = bool(value)
        with self._lock:
            if self._active:
                raise RuntimeError("Already recording.")
            self._options = merged
            self._hotkeys = tuple((b["vk"], frozenset(b["mods"])) for b in bindings.values())
            self._events = []
            self._result = []
            self._down_keys = {}
            self._down_buttons = set()
            self._pending_notice = None
            self._stop_reason = None
            self._duration = 0.0
            self._last_t = 0.0
            self._last_move_t = 0.0
            self.skipped_keys = 0
            self.skipped_buttons = 0
            self._t0 = self._clock()

            position = self._start_position()
            if position is not None:
                self._events.append({"t": 0.0, "type": "mouse_move", "x": position[0], "y": position[1]})

            self._active = True
            self._accepting = True

        started = []
        try:
            listeners = self._make_listeners()
            for listener in listeners:
                listener.start()
                started.append(listener)
        except Exception as error:  # ImportError (no display), permission problems, ...
            log.exception("Could not start the input listeners")
            with self._lock:
                self._active = False
                self._accepting = False
            for listener in started:
                self._stop_listener(listener)
            raise RecordingError(f"Couldn't start recording: {error}") from error
        with self._lock:
            self._listeners = tuple(started)

    def stop(self, trim_tail: bool = False, drop_modifiers=()) -> list:
        """Stop recording and return the events. Safe to call twice (you get the same events back).

        trim_tail=True cuts the last TAIL_TRIM_SECONDS, for when recording was ended by clicking
        the Stop button with the mouse. drop_modifiers: the modifiers of the hotkey that stopped
        it (see finish_events). Call this from the GUI thread, not from on_auto_stop.
        """
        with self._lock:
            if not self._active:
                return list(self._result)
            self._active = False
            self._accepting = False
            end_t = max(0.0, self._clock() - self._t0)
            listeners, self._listeners = self._listeners, ()

        for listener in listeners:
            self._stop_listener(listener, join=True)

        with self._lock:  # any handler that was mid-way has finished by now
            self._duration = end_t
            result = finish_events(self._events, round(end_t, _TIME_DECIMALS), trim_tail, drop_modifiers)
            if not self._options["delay"]:
                result = retime(result)
            self._result = result
            return list(self._result)

    # ---- Internals: setup ----

    def _start_position(self):
        """The mouse position at the start as (x, y), or None if we can't tell."""
        try:
            if self._position_func is not None:
                raw = self._position_func()
            else:
                from pynput import mouse

                raw = mouse.Controller().position
            x = _to_int(raw[0], MAX_COORD)
            y = _to_int(raw[1], MAX_COORD)
        except Exception:
            log.warning("Could not read the starting mouse position", exc_info=True)
            return None
        return None if x is None or y is None else (x, y)

    def _make_listeners(self) -> tuple:
        from pynput import keyboard, mouse

        mouse_listener = mouse.Listener(
            on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll
        )
        keyboard_listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        return (mouse_listener, keyboard_listener)

    @staticmethod
    def _stop_listener(listener, join: bool = False) -> None:
        try:
            listener.stop()
            if join:
                listener.join(1.0)
        except Exception:  # a dead listener or joining from its own thread: nothing more to do
            log.debug("Problem while stopping a listener", exc_info=True)

    # ---- Internals: recording (all called with the lock held unless noted) ----

    def _now(self) -> float:
        # max() keeps time from going backwards, whatever the clock or rounding does.
        t = max(self._last_t, round(self._clock() - self._t0, _TIME_DECIMALS))
        return t

    def _append(self, event: dict) -> bool:
        if event["t"] > MAX_RECORDING_SECONDS:
            self._stop_taking_events("time")
            return False
        if len(self._events) >= MAX_EVENTS - _RESERVE_EVENTS:
            self._stop_taking_events("limit")
            return False
        self._events.append(event)
        self._last_t = event["t"]
        return True

    def _stop_taking_events(self, reason: str) -> None:
        if self._accepting:
            self._accepting = False
            self._stop_reason = reason
            self._pending_notice = reason

    def _notify(self) -> None:
        """Call on_auto_stop if a handler asked for it. Called WITHOUT the lock held."""
        with self._lock:
            reason, self._pending_notice = self._pending_notice, None
        if reason and self._on_auto_stop is not None:
            try:
                self._on_auto_stop(reason)
            except Exception:
                log.exception("on_auto_stop callback failed")

    def _fail(self) -> None:
        """A handler crashed: log it, stop taking events and tell the GUI."""
        log.exception("Error while recording input")
        with self._lock:
            self._stop_taking_events("error")

    def _key_data(self, key) -> Optional[dict]:
        """Serialized key, or None if it must not be recorded (unsaveable, unknown)."""
        if key is None:
            self.skipped_keys += 1
            return None
        try:
            return key_to_json(key)
        except RecordingError:
            self.skipped_keys += 1
            return None

    def _is_hotkey(self, key) -> bool:
        """True if pressing `key` right now (with the modifiers held) is one of the user's hotkeys.
        Called with the lock held."""
        if not self._hotkeys:
            return False
        vk = key_to_vk(key, self._windows)
        if vk is None:
            return False
        mods = set()
        for identity in self._down_keys:
            if identity[0] == "special" and identity[1] in _MODIFIER_OF_KEY:
                mods.add(_MODIFIER_OF_KEY[identity[1]])
        return any(vk == hk_vk and mods == hk_mods for hk_vk, hk_mods in self._hotkeys)

    def _context(self, x=None, y=None) -> Optional[dict]:
        """The program a click / scroll at (x, y), or a key press (no x, y), goes to. Called
        WITHOUT the lock held (it asks Windows). None if unknown: nothing is noted then."""
        info = self._windows_info
        if info is None or not self._accepting:
            return None
        try:
            context = info.foreground_context() if x is None else info.context_at(x, y)
        except Exception:
            log.debug("Could not read the window context", exc_info=True)
            return None
        if not isinstance(context, dict) or not context.get("exe") or not context.get("cls"):
            return None
        return {"exe": str(context["exe"])[:260], "cls": str(context["cls"])[:260]}

    # ---- Handlers (run on pynput's listener threads) ----

    def _on_move(self, x, y, injected=False):
        try:
            px, py = _to_int(x, MAX_COORD), _to_int(y, MAX_COORD)
            if px is None or py is None:
                return
            with self._lock:
                if not self._accepting or not self._options["moves"]:
                    return
                t = self._now()
                if t - self._last_move_t < MOVE_MIN_INTERVAL:
                    return
                if self._append({"t": t, "type": "mouse_move", "x": px, "y": py}):
                    self._last_move_t = t
        except Exception:
            self._fail()
        self._notify()

    def _on_click(self, x, y, button, pressed, injected=False):
        try:
            name = getattr(button, "name", None)
            px, py = _to_int(x, MAX_COORD), _to_int(y, MAX_COORD)
            if name not in BUTTON_NAMES:
                self.skipped_buttons += 1
                return
            if px is None or py is None:
                return
            pressed = bool(pressed)
            context = self._context(px, py) if pressed and self._options["clicks"] else None
            with self._lock:
                if not self._accepting or not self._options["clicks"]:
                    return
                if not pressed and name not in self._down_buttons:
                    return  # release of a button we never saw go down
                t = self._now()
                event = {"t": t, "type": "mouse_click", "x": px, "y": py, "button": name, "pressed": pressed}
                if context:
                    event["w"] = context
                if self._append(event):
                    if pressed:
                        self._down_buttons.add(name)
                    else:
                        self._down_buttons.discard(name)
        except Exception:
            self._fail()
        self._notify()

    def _on_scroll(self, x, y, dx, dy, injected=False):
        try:
            px, py = _to_int(x, MAX_COORD), _to_int(y, MAX_COORD)
            sx, sy = _to_int(dx, MAX_SCROLL), _to_int(dy, MAX_SCROLL)
            if None in (px, py, sx, sy) or (sx == 0 and sy == 0):
                return
            context = self._context(px, py) if self._options["clicks"] else None
            with self._lock:
                if not self._accepting or not self._options["clicks"]:
                    return
                event = {"t": self._now(), "type": "mouse_scroll", "x": px, "y": py, "dx": sx, "dy": sy}
                if context:
                    event["w"] = context
                self._append(event)
        except Exception:
            self._fail()
        self._notify()

    def _on_press(self, key, injected=False):
        try:
            data = self._key_data(key)
            if data is None:
                return
            identity = key_identity(data)
            # Auto-repeat of a held key is dropped below anyway; don't ask Windows for it.
            repeat = identity in self._down_keys
            context = self._context() if self._options["keyboard"] and not repeat else None
            with self._lock:
                if not self._accepting or not self._options["keyboard"]:
                    return
                if self._is_hotkey(key):
                    return  # the user's hotkey: it controls the app, it isn't part of the recording
                if identity in self._down_keys:
                    return  # auto-repeat of a key that is still held down
                event = {"t": self._now(), "type": "key_press", "key": data}
                if context:
                    event["w"] = context
                if self._append(event):
                    self._down_keys[identity] = data
        except Exception:
            self._fail()
        self._notify()

    def _on_release(self, key, injected=False):
        try:
            data = self._key_data(key)
            if data is None:
                return
            identity = key_identity(data)
            with self._lock:
                if not self._accepting or not self._options["keyboard"]:
                    return
                if identity not in self._down_keys:
                    return  # release of a key we never saw go down
                # Record the key as it was pressed, so press and release match exactly.
                if self._append({"t": self._now(), "type": "key_release", "key": self._down_keys[identity]}):
                    del self._down_keys[identity]
        except Exception:
            self._fail()
        self._notify()
