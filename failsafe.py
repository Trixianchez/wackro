"""Playback failsafes: stop a playback that has "gone off road" before it does damage.

Two parts, both switched in Settings > Safety… (all on by default):

InputWatch: you take over.
    Any key you press, any click or scroll, moving the mouse yourself (more than a few pixels in
    one go: the sensitivity), or flinging the pointer into a screen corner stops playback. Only REAL input counts: Windows marks what the player
    types and clicks as "injected", and those events are ignored. Moves are also checked against
    where the player last put the pointer, so the player's own moves never trip it.

        watch = InputWatch(on_trip=lambda reason: ...)   # called ONCE, on a listener thread
        watch.start(options, hotkey_vks={0x90, 0x79})    # the hotkeys keep doing their own job
        watch.note_cursor(x, y)                          # player.start(on_cursor=...)
        watch.stop()

WindowGuard: the right program is still there (and the PC isn't locked).
    Recordings (version 2) note which program each click, scroll and key press went to
    (program file + window class, never the title). Right before playing one, the guard checks:
      * a popup (a standard Windows message box, like "VAT rate value should be 10 or 12 only")
        is in the way, or has disabled the program the click was meant for;
      * a different program is there / in front (the program crashed, closed, or Alt+Tab landed
        somewhere else);
      * the program stopped responding, or Windows' crash reporter (WerFault) is up.
          * the PC is locked, or a UAC / Ctrl+Alt+Del screen is up (input can't get through).
    A mismatch gets SETTLE_SECONDS to sort itself out (a window that is still opening), then
    playback stops with a sentence saying why. Old recordings without the notes still get the
    "not responding" / crash check.

        player.start(events, guard=WindowGuard(win32, options), on_cursor=watch.note_cursor)

missing_programs(events, running) is the pre-flight check: programs the recording used that
aren't running right now.

Nothing in here touches Tk. Anything that fails is logged and treated as "no problem": a broken
failsafe must never stop a good playback, and the F10 / Play hotkeys always work regardless.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from typing import Callable, Optional

from hotkey_spec import key_to_vk

log = logging.getLogger(__name__)

# Settings keys -> what they switch on (the defaults live in settings.py).
SENSITIVITY_PIXELS = {"low": 120, "medium": 40, "high": 15}
SENSITIVITY_LABELS = {"low": "Low (big moves only)", "medium": "Medium", "high": "High (tiny nudges)"}

GRACE_SECONDS = 0.6         # after start: the key / click that started playback is still settling
MOVE_IDLE_RESET = 1.5       # mouse still this long: your movement count starts over
MAX_STEP = 150              # px between two of your move events; more means the pointer jumped
PLAYER_MOVE_SLACK = 3       # px: a real-looking move this close to where the player put it is the player's
PLAYER_MOVE_MEMORY = 1.0    # s: how long the player's positions are remembered
CORNER_PIXELS = 2           # how close to a corner counts as "in the corner"
SETTLE_SECONDS = 0.5        # a window mismatch gets this long to sort itself out
SETTLE_STEP = 0.05
POPUP_CLASS = "#32770"      # the class of every standard Windows message box / dialog
CRASH_REPORTERS = ("werfault.exe", "werfaultsecure.exe")

DEFAULT_OPTIONS = {
    "fs_keys": True, "fs_clicks": True, "fs_move": True, "fs_move_sensitivity": "medium",
    "fs_corner": True, "fs_window": True, "fs_preflight": True, "fs_crash": True,
    "fs_locked": True, "fs_screen": True, "fs_sleep": True,
}
TAKE_OVER_KEYS = ("fs_keys", "fs_clicks", "fs_move", "fs_corner")
WAKE_GRACE = 2.0            # after the blackout is woken: your input doesn't trip anything yet
LAG_LIMIT = 5.0             # s behind schedule = the PC slept / froze (see Player max_lag)
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
MODIFIER_NAME_VKS = {"ctrl": (0x11, 0xA2, 0xA3), "alt": (0x12, 0xA4, 0xA5),
                     "shift": (0x10, 0xA0, 0xA1), "win": (0x5B, 0x5C)}


def hotkey_vks(bindings: dict, actions) -> tuple:
    """(main key vks, modifier vks) of the given hotkey bindings, for InputWatch.start."""
    mains, mods = set(), set()
    for action in actions:
        binding = bindings.get(action) if isinstance(bindings, dict) else None
        if not isinstance(binding, dict) or not isinstance(binding.get("vk"), int):
            continue
        mains.add(binding["vk"])
        for name in binding.get("mods", []):
            mods.update(MODIFIER_NAME_VKS.get(name, ()))
    return mains, mods


def options_from(settings: dict) -> dict:
    """The failsafe part of the settings, with defaults for anything missing."""
    result = {}
    for key, default in DEFAULT_OPTIONS.items():
        value = settings.get(key, default)
        if key == "fs_move_sensitivity":
            result[key] = value if value in SENSITIVITY_PIXELS else default
        else:
            result[key] = bool(value)
    return result


def _same_program(a: str, b: str) -> bool:
    return str(a).casefold() == str(b).casefold()


def missing_programs(events: list, running: Optional[set]) -> list:
    """Programs the recording clicked / typed into that aren't running now (sorted, as recorded).
    [] when that can't be told (running is None) or the recording has no notes."""
    if running is None:
        return []
    running = {name.casefold() for name in running}
    missing = {}
    for event in events:
        context = event.get("w") if isinstance(event, dict) else None
        if isinstance(context, dict) and context.get("exe"):
            exe = str(context["exe"])
            if exe.casefold() not in running:
                missing.setdefault(exe.casefold(), exe)
    return sorted(missing.values(), key=str.casefold)


# ---- You take over --------------------------------------------------------------------------

class InputWatch:
    """See the module docstring. Windows only by default (elsewhere the player's own input can't
    be told apart from yours); a listener_factory makes it run anywhere (tests)."""

    def __init__(self, on_trip: Callable[[str], None], clock: Callable[[], float] = time.monotonic,
                 listener_factory=None, screen_corners: Optional[Callable[[], list]] = None):
        self._on_trip = on_trip
        self._clock = clock
        self._listener_factory = listener_factory
        self._screen_corners = screen_corners    # () -> [(x, y), ...] of every monitor's corners
        self._lock = threading.Lock()
        self._listeners: tuple = ()
        self._active = False
        self._tripped = False
        self._options = dict(DEFAULT_OPTIONS)
        self._hotkey_vks: frozenset = frozenset()
        self._t0 = 0.0
        self._last_pos: Optional[tuple] = None    # where the pointer last was (yours or the player's)
        self._travel = 0.0                         # how far YOU moved it in the current burst
        self._pending: Optional[str] = None
        self._last_user_move = -1e9
        self._player_positions: list = []        # [(t, x, y)], newest last
        self._corners: list = []
        self._mod_vks: frozenset = frozenset()
        self._on_wake: Optional[Callable[[], None]] = None
        self._blacked_out = False
        self._wake_until = -1e9                  # your input is let through but trips nothing until then
        self._last_activity = -1e9               # last real input of yours (any kind)
        self._wake_pending = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def last_activity(self) -> float:
        """clock() time of your last real key / click / move (-1e9 if none)."""
        return self._last_activity

    def wanted(self, options: dict) -> bool:
        o = options_from(options)
        return any(o[k] for k in TAKE_OVER_KEYS)

    def set_blacked_out(self, on: bool) -> None:
        """While the blackout is up, your first real input only wakes the screen: it's swallowed
        (never reaches the program being played into) and on_wake is called; the take-over
        checks then wait WAKE_GRACE seconds."""
        with self._lock:
            self._blacked_out = bool(on)
            if not on:
                self._wake_until = max(self._wake_until, self._clock() + WAKE_GRACE)

    @property
    def blacked_out(self) -> bool:
        return self._blacked_out

    def start(self, options: dict, hotkey_vks=(), track: bool = False, on_wake=None, mod_vks=()) -> bool:
        """Begin watching. Returns False (and watches nothing) when every take-over check is off
        and track is False, or the listeners can't start / this isn't Windows.

        hotkey_vks: main keys of your hotkeys (left to the hotkeys, never trip / never swallowed).
        mod_vks: modifier vks those hotkeys use (same).
        track: watch anyway, to know when you last used the PC and to wake the blackout.
        on_wake(): called (on a listener thread) when your input wakes the blackout."""
        self.stop()
        options = options_from(options)
        if not self.wanted(options) and not track:
            return False
        if self._listener_factory is None and not sys.platform.startswith("win"):
            return False
        with self._lock:
            self._options = options
            self._hotkey_vks = frozenset(int(vk) for vk in hotkey_vks)
            self._mod_vks = frozenset(int(vk) for vk in mod_vks)
            self._on_wake = on_wake
            self._blacked_out = False
            self._wake_until = -1e9
            self._last_activity = -1e9
            self._wake_pending = False
            self._t0 = self._clock()
            self._last_pos = None
            self._travel = 0.0
            self._pending = None
            self._last_user_move = -1e9
            self._player_positions = []
            self._tripped = False
            self._corners = self._read_corners() if options["fs_corner"] else []
            self._active = True
        started = []
        try:
            for listener in self._make_listeners(options):
                listener.start()
                started.append(listener)
        except Exception:
            log.warning("The take-over failsafe could not start", exc_info=True)
            for listener in started:
                self._stop_listener(listener)
            with self._lock:
                self._active = False
            return False
        with self._lock:
            self._listeners = tuple(started)
        return True

    def stop(self) -> None:
        with self._lock:
            self._active = False
            self._blacked_out = False
            listeners, self._listeners = self._listeners, ()
        for listener in listeners:
            self._stop_listener(listener)

    def note_cursor(self, x, y) -> None:
        """The player is about to put the pointer at (x, y). Called on the player's thread."""
        with self._lock:
            now = self._clock()
            self._player_positions.append((now, x, y))
            if len(self._player_positions) > 64:
                del self._player_positions[:32]
            self._last_pos = (x, y)

    # ---- internals ----

    def _read_corners(self) -> list:
        try:
            return list(self._screen_corners()) if self._screen_corners else []
        except Exception:
            log.debug("Could not read the screen corners", exc_info=True)
            return []

    def _make_listeners(self, options: dict) -> list:
        if self._listener_factory is not None:
            return list(self._listener_factory(self))
        from pynput import keyboard, mouse

        # Both are always watched: any input can wake the blackout and counts as "you're here".
        # The filters run first, inside the Windows hook, and swallow input while blacked out.
        holder = {}

        def keyboard_filter(msg, data):
            vk = getattr(data, "vkCode", None)
            injected = bool(getattr(data, "flags", 0) & LLKHF_INJECTED)
            down = msg in (0x0100, 0x0104)      # WM_KEYDOWN / WM_SYSKEYDOWN
            if self._filter("key", vk, injected, down) == "suppress":
                holder["keyboard"].suppress_event()
            return True

        def mouse_filter(msg, data):
            injected = bool(getattr(data, "flags", 0) & LLMHF_INJECTED)
            if self._filter("mouse", None, injected, msg != 0x0200) == "suppress":   # 0x200 = move
                holder["mouse"].suppress_event()
            return True

        holder["keyboard"] = keyboard.Listener(on_press=self._on_press, on_release=self._on_release,
                                               win32_event_filter=keyboard_filter)
        holder["mouse"] = mouse.Listener(on_move=self._on_move, on_click=self._on_click,
                                         on_scroll=self._on_scroll, win32_event_filter=mouse_filter)
        return [holder["keyboard"], holder["mouse"]]

    def _filter(self, kind: str, vk, injected: bool, press: bool) -> str:
        """Inside the Windows hook: "pass" or "suppress". While blacked out (and for the wake
        grace right after), your real input is swallowed; the first press / click / move wakes
        the blackout. Your hotkeys always pass (the play and panic-stop keys still stop playback)."""
        if injected:
            return "pass"
        fire = False
        with self._lock:
            if not self._active:
                return "pass"
            now = self._clock()
            self._last_activity = now
            if kind == "key" and vk is not None and (vk in self._hotkey_vks or vk in self._mod_vks):
                return "pass"
            if self._blacked_out:
                self._blacked_out = False
                self._wake_until = now + WAKE_GRACE
                self._wake_pending = True
                fire = True
                result = "suppress"
            elif now < self._wake_until - WAKE_GRACE + 0.3:
                result = "suppress"             # the rest of the wiggle / key that woke it
            else:
                result = "pass"
        if fire:
            self._fire_wake()
        return result

    def _fire_wake(self) -> None:
        with self._lock:
            pending, self._wake_pending = self._wake_pending, False
            callback = self._on_wake
        if pending and callback is not None:
            try:
                callback()
            except Exception:
                log.exception("The wake callback failed")

    def _on_release(self, key, injected=False):
        return None

    @staticmethod
    def _stop_listener(listener) -> None:
        try:
            listener.stop()
        except Exception:
            log.debug("Problem while stopping a failsafe listener", exc_info=True)

    def _ready(self, injected) -> bool:
        """Called with the lock held: should a real input event be looked at now?"""
        if not self._active or injected:
            return False
        now = self._clock()
        self._last_activity = now
        return (not self._tripped and not self._blacked_out and now >= self._wake_until
                and now - self._t0 >= GRACE_SECONDS)

    def _trip(self, reason: str) -> None:
        """Called with the lock held. The callback runs after the lock is let go (see _fire)."""
        self._tripped = True
        self._pending = reason

    def _fire(self) -> None:
        with self._lock:
            reason, self._pending = self._pending, None
        if reason is None:
            return
        log.warning("Failsafe: %s", reason)
        try:
            self._on_trip(reason)
        except Exception:
            log.exception("The failsafe callback failed")

    def _on_press(self, key, injected=False):
        try:
            with self._lock:
                if not self._ready(injected) or not self._options["fs_keys"]:
                    return
                vk = key_to_vk(key, True)
                if vk is not None and (vk in self._hotkey_vks or vk in self._mod_vks):
                    return      # your hotkeys (and their Ctrl / Alt...) do their own job
                self._trip("you pressed a key")
        except Exception:
            log.debug("Failsafe key check failed", exc_info=True)
        self._fire()

    def _on_click(self, x, y, button, pressed, injected=False):
        try:
            with self._lock:
                if self._ready(injected) and pressed and self._options["fs_clicks"]:
                    self._trip("you clicked the mouse")
        except Exception:
            log.debug("Failsafe click check failed", exc_info=True)
        self._fire()

    def _on_scroll(self, x, y, dx, dy, injected=False):
        try:
            with self._lock:
                if self._ready(injected) and self._options["fs_clicks"]:
                    self._trip("you scrolled the mouse wheel")
        except Exception:
            log.debug("Failsafe scroll check failed", exc_info=True)
        self._fire()

    def _on_move(self, x, y, injected=False):
        try:
            with self._lock:
                self._check_move(x, y, injected)
        except Exception:
            log.debug("Failsafe move check failed", exc_info=True)
        self._fire()

    def _check_move(self, x, y, injected) -> None:
        """Called with the lock held."""
        if injected or not self._active:
            return
        now = self._clock()
        if self._is_player_move(now, x, y):
            self._last_pos = (x, y)
            return
        if not self._ready(injected):
            self._last_pos = (x, y)
            return
        if self._options["fs_corner"] and self._in_corner(x, y):
            self._trip("you moved the mouse into a screen corner")
            return
        if not self._options["fs_move"]:
            self._last_pos = (x, y)
            return
        if now - self._last_user_move > MOVE_IDLE_RESET:
            self._travel = 0.0      # still for a while: a bump of the desk doesn't add up
        self._last_user_move = now
        if self._last_pos is not None:
            step = math.hypot(x - self._last_pos[0], y - self._last_pos[1])
            if step <= MAX_STEP:    # bigger: the pointer jumped (the player), not your hand
                self._travel += step
        self._last_pos = (x, y)
        limit = SENSITIVITY_PIXELS[self._options["fs_move_sensitivity"]]
        if self._travel > limit:
            self._trip("you moved the mouse")

    def _is_player_move(self, now: float, x, y) -> bool:
        for t, px, py in reversed(self._player_positions):
            if now - t > PLAYER_MOVE_MEMORY:
                break
            if abs(x - px) <= PLAYER_MOVE_SLACK and abs(y - py) <= PLAYER_MOVE_SLACK:
                return True
        return False

    def _in_corner(self, x, y) -> bool:
        return any(abs(x - cx) <= CORNER_PIXELS and abs(y - cy) <= CORNER_PIXELS for cx, cy in self._corners)


def corners_of(monitors) -> list:
    """Every corner pixel of every monitor (winutil.Monitor list)."""
    result = []
    for m in monitors:
        result += [(m.left, m.top), (m.right - 1, m.top), (m.left, m.bottom - 1), (m.right - 1, m.bottom - 1)]
    return result


# ---- The right program is still there -------------------------------------------------------

class WindowGuard:
    """player.start(guard=...). See the module docstring. Called on the player's thread."""

    def __init__(self, win32, options: dict, clock: Callable[[], float] = time.monotonic):
        self._win = win32
        self._options = options_from(options)
        self._clock = clock

    @property
    def wanted(self) -> bool:
        return self._options["fs_window"] or self._options["fs_crash"] or self._options["fs_locked"]

    def __call__(self, operation: str, x, y, context, stop_event) -> Optional[str]:
        deadline = self._clock() + SETTLE_SECONDS
        while True:
            problem = self.problem(operation, x, y, context)
            if problem is None:
                return None
            if self._clock() >= deadline:
                return problem
            if stop_event is not None and stop_event.wait(SETTLE_STEP):
                return None         # stopped anyway; the player handles that
            if stop_event is None:
                time.sleep(SETTLE_STEP)

    def problem(self, operation: str, x, y, context) -> Optional[str]:
        """One look: a sentence describing what's wrong, or None."""
        win = self._win
        if self._options["fs_locked"] and win.input_desktop_blocked():
            return "the PC got locked (or a Windows security prompt is up)"
        is_key = x is None or y is None
        target = win.foreground() if is_key else win.window_at(x, y)
        expected = context if isinstance(context, dict) and context.get("exe") else None

        if self._options["fs_crash"]:
            front = win.foreground_context()
            if front and str(front.get("exe", "")).casefold() in CRASH_REPORTERS:
                return "a program crashed (the Windows error report is open)"
            if target and win.is_hung(target):
                actual = win.context_of(target)
                name = (actual or expected or {}).get("exe", "the program")
                return f"{name} stopped responding"

        if not self._options["fs_window"] or expected is None or not target:
            return None

        # A message box disables the window it belongs to; the click would go nowhere.
        if not is_key and not win.is_enabled(target):
            front_hwnd = win.foreground()
            front = win.context_of(front_hwnd)
            if front and front["cls"] == POPUP_CLASS:
                return self._popup_text(front_hwnd, front)
            return f"{expected['exe']} is blocked by another window"

        actual = win.context_of(target)
        if actual is None:
            return None             # can't tell (or it's one of our own windows): don't stop
        popup = actual["cls"] == POPUP_CLASS and expected["cls"] != POPUP_CLASS
        if popup:
            return self._popup_text(target, actual)
        if not _same_program(actual["exe"], expected["exe"]):
            if is_key:
                return f"keys were going to {expected['exe']}, but {actual['exe']} is in front"
            return f"that click was meant for {expected['exe']}, but {actual['exe']} is there"
        return None

    def _popup_text(self, hwnd, context: dict) -> str:
        try:
            text = self._win.dialog_text(hwnd)
        except Exception:
            text = ""
        text = " ".join(str(text).split())
        if len(text) > 120:
            text = text[:117] + "…"
        where = f"a popup appeared in {context['exe']}"
        return f'{where}: "{text}"' if text else where
