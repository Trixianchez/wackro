"""Global hotkeys with your own keys (set in Settings > Hotkeys, see hotkey_spec.py).

    hotkeys = Hotkeys(
        bindings={"record": {...}, "play": {...}, "stop": {...}},
        on_action=app.on_hotkey,              # called with "record", "play" or "stop"
        dispatch=lambda fn: root.after(0, fn),  # hop to the GUI thread
    )
    hotkeys.start()
    hotkeys.set_bindings(new_bindings)        # takes effect immediately
    hotkeys.set_enabled(False)                # e.g. while the "press a key" box is open
    hotkeys.stop()

Rules:

* On Windows the hotkeys are SWALLOWED (when `swallow` is True, the default): a low-level keyboard
  hook sees the key first and stops it, so NumLock doesn't toggle the NumLock light and Ctrl+`
  doesn't also reach the focused program. Only the hotkey's main key is swallowed, never Ctrl /
  Alt / Shift / Win themselves. On other systems the keys are only listened to.
* Keys sent by a program (our own player, for example) are ignored, so playing back a recording
  that contains a hotkey never triggers it.
* What an action means (toggle record, toggle play, stop) is up to the app. This class only says
  which binding was pressed.
* on_action is NOT run on the hook thread. It goes through `dispatch` (the GUI's thread hop).
  The hook thread must answer Windows quickly, so nothing slow happens there.
* A handler that raises is logged and swallowed; pynput would otherwise kill the listener and
  every hotkey would silently stop working.

pynput is imported lazily (in start), so importing this module never needs a display.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

from hotkey_spec import DEFAULT_BINDINGS, HotkeyMatcher, key_to_vk

log = logging.getLogger(__name__)

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105
_LLKHF_INJECTED = 0x10
_LLKHF_LOWER_IL_INJECTED = 0x02

# GetAsyncKeyState codes of the modifiers (generic Ctrl / Shift / Alt cover both sides).
_ASYNC_MODIFIERS = (("ctrl", 0x11), ("shift", 0x10), ("alt", 0x12), ("win", 0x5B), ("win", 0x5C))


def windows_modifier_reader():
    """A function returning the modifiers physically held right now (Windows only)."""
    import ctypes

    get_async_key_state = ctypes.windll.user32.GetAsyncKeyState
    get_async_key_state.restype = ctypes.c_short
    get_async_key_state.argtypes = [ctypes.c_int]

    def read() -> set:
        return {name for name, vk in _ASYNC_MODIFIERS if get_async_key_state(vk) & 0x8000}

    return read


class Hotkeys:
    def __init__(
        self,
        on_action: Callable[[str], None],
        bindings: Optional[dict] = None,
        dispatch: Optional[Callable[[Callable[[], None]], None]] = None,
        swallow: bool = True,
        windows: Optional[bool] = None,
        listener_factory=None,
        mods_reader=None,
    ):
        """windows / listener_factory / mods_reader exist for the tests (fake platform, fake pynput,
        fake modifier state). On Windows mods_reader defaults to GetAsyncKeyState."""
        self._on_action = on_action
        self._dispatch = dispatch
        self._swallow = swallow
        self._windows = sys.platform.startswith("win") if windows is None else windows
        self._listener_factory = listener_factory
        if mods_reader is None and self._windows:
            try:
                mods_reader = windows_modifier_reader()
            except Exception:
                log.warning("Can't read the modifier keys; tracking them instead", exc_info=True)
        self._matcher = HotkeyMatcher(bindings if bindings is not None else DEFAULT_BINDINGS, mods_reader)
        self._match_lock = threading.Lock()   # the hook thread and the GUI both touch the matcher
        self._listener = None
        self._hook_listener = None            # the listener whose hook the filter runs in
        self._lock = threading.Lock()         # start() / stop()

    # ---- Settings -----------------------------------------------------------------------

    @property
    def bindings(self) -> dict:
        with self._match_lock:
            return self._matcher.bindings

    def set_bindings(self, bindings: dict) -> None:
        with self._match_lock:
            self._matcher.set_bindings(bindings)

    def set_extra(self, action: str, binding) -> bool:
        """An optional extra hotkey (hotkey_spec.EXTRA_ACTIONS), None to unbind."""
        with self._match_lock:
            return self._matcher.set_extra(action, binding)

    @property
    def extras(self) -> dict:
        with self._match_lock:
            return self._matcher.extras

    @property
    def enabled(self) -> bool:
        return self._matcher.enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._match_lock:
            self._matcher.enabled = bool(enabled)

    # ---- Lifecycle ----------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        listener = self._listener
        return listener is not None and bool(getattr(listener, "running", False))

    @property
    def swallowing(self) -> bool:
        """True if hotkeys are really kept away from other programs (Windows only)."""
        return self._swallow and self._windows

    def start(self) -> None:
        """Start listening. Does nothing if already running.

        Raises RuntimeError with a GUI-ready message if the listener can't be started.
        """
        with self._lock:
            if self.is_running:
                return
            try:
                listener = self._make_listener()
                self._hook_listener = listener      # the filter may run before start() returns
                listener.daemon = True
                listener.start()
                listener.wait()  # block until the hook is really installed (or has died)
            except Exception as exc:  # pynput raises many things (ImportError, X errors, ...)
                log.exception("Could not start global hotkeys")
                raise RuntimeError(f"Could not start the global hotkeys: {exc}") from exc
            if not getattr(listener, "running", False):
                raise RuntimeError("Could not start the global hotkeys.")
            self._listener = listener

    def stop(self) -> None:
        """Stop listening. Safe to call any number of times, and before start()."""
        with self._lock:
            listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:
            log.exception("Error while stopping global hotkeys")

    def _make_listener(self):
        factory = self._listener_factory
        if factory is None:
            from pynput import keyboard

            factory = keyboard.Listener
        if self._windows:
            # Everything happens in the filter: it runs on the hook thread before Windows passes
            # the key on, which is the only place a key can be swallowed.
            return factory(win32_event_filter=self._win32_filter)
        return factory(on_press=self._on_press, on_release=self._on_release)

    # ---- Windows: the hook filter (runs on the hook thread) ----------------------------

    def _win32_filter(self, msg, data):
        """pynput's win32_event_filter. Returns False (no need for pynput's own key decoding),
        or calls listener.suppress_event(), which raises to swallow the key."""
        swallow = False
        try:
            vk = int(data.vkCode)
            injected = bool(int(data.flags) & (_LLKHF_INJECTED | _LLKHF_LOWER_IL_INJECTED))
            with self._match_lock:
                if msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                    action, swallow = self._matcher.key_down(vk, injected)
                elif msg in (_WM_KEYUP, _WM_SYSKEYUP):
                    action, swallow = None, self._matcher.key_up(vk, injected)
                else:
                    action = None
            if action is not None:
                self._fire(action)
        except Exception:
            log.exception("Hotkey filter failed")
            return False
        if swallow and self._swallow:
            listener = self._hook_listener
            if listener is not None:
                listener.suppress_event()   # raises; pynput turns that into "swallow this key"
        return False

    # ---- Other systems: plain listening (runs on pynput's listener thread) -------------

    def _on_press(self, key, injected=False):
        try:
            vk = key_to_vk(key, windows=False)
            if vk is None:
                return
            with self._match_lock:
                action, _ = self._matcher.key_down(vk, bool(injected))
            if action is not None:
                self._fire(action)
        except Exception:
            log.exception("Hotkey press handler failed")

    def _on_release(self, key, injected=False):
        try:
            vk = key_to_vk(key, windows=False)
            if vk is None:
                return
            with self._match_lock:
                self._matcher.key_up(vk, bool(injected))
        except Exception:
            log.exception("Hotkey release handler failed")

    # ---- Firing -------------------------------------------------------------------------

    def _fire(self, action: str) -> None:
        """Hand an action to the app (through dispatch). Never raises."""
        try:
            if self._dispatch is None:
                self._call(action)
            else:
                self._dispatch(lambda: self._call(action))
        except Exception:
            log.exception("Could not dispatch the %r hotkey", action)

    def _call(self, action: str) -> None:
        try:
            self._on_action(action)
        except Exception:
            log.exception("Hotkey %r handler failed", action)
