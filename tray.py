"""The system tray icon (the little picture by the clock), using pystray.

    tray = Tray(on_show=..., on_record=..., on_play=..., on_stop=..., on_exit=...,
                dispatch=lambda fn: root.after(0, fn))
    if not tray.start():          # False: no tray on this system; the app minimizes instead
        ...
    tray.set_state("recording")   # "idle" / "recording" / "playing": the dot changes colour
    tray.set_labels({"record": "Ctrl+`", ...})   # hotkeys shown in the menu
    tray.stop()

Why: while recording or playing, the window is hidden completely (withdraw), so it isn't in
Alt+Tab or the taskbar and can't get in the way. The tray icon is how you get it back: click it,
or right-click for Show / Record / Play / Stop / Exit.

Threading: pystray runs its own loop on a daemon thread (it can never keep the app alive).
Menu clicks arrive on that thread, so every callback goes through `dispatch` (the GUI's thread
hop). Nothing in here touches Tk.

pystray is imported lazily (in start): on Linux its import needs a display, and a missing or
broken tray must never stop the app from starting. start() just returns False then.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from config import APP_NAME, TRAY_ICON_PATHS, resource_path

log = logging.getLogger(__name__)

STATES = ("idle", "recording", "playing")
_TITLES = {
    "idle": APP_NAME,
    "recording": f"{APP_NAME}: recording",
    "playing": f"{APP_NAME}: playing",
}
_READY_TIMEOUT = 3.0
_DOT_FALLBACK = {"idle": (128, 134, 144), "recording": (230, 40, 46), "playing": (46, 184, 92)}


def load_images(loader=None) -> dict:
    """{state: PIL image}. A missing / broken PNG becomes a plain coloured dot, so the tray
    still works (just less pretty)."""
    from PIL import Image, ImageDraw

    images = {}
    for state in STATES:
        try:
            if loader is not None:
                image = loader(state)
            else:
                with Image.open(resource_path(TRAY_ICON_PATHS[state])) as raw:
                    image = raw.convert("RGBA")
        except Exception:
            log.warning("Tray icon for %s is missing; using a plain dot", state, exc_info=True)
            image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            ImageDraw.Draw(image).ellipse((4, 4, 28, 28), fill=_DOT_FALLBACK[state] + (255,))
        images[state] = image
    return images


class Tray:
    def __init__(
        self,
        on_show: Callable[[], None],
        on_record: Callable[[], None],
        on_play: Callable[[], None],
        on_stop: Callable[[], None],
        on_exit: Callable[[], None],
        dispatch: Optional[Callable[[Callable[[], None]], None]] = None,
        pystray_module=None,
        image_loader=None,
    ):
        """pystray_module / image_loader exist for the tests."""
        self._callbacks = {
            "show": on_show, "record": on_record, "play": on_play, "stop": on_stop, "exit": on_exit,
        }
        self._dispatch = dispatch
        self._pystray = pystray_module
        self._image_loader = image_loader
        self._icon = None
        self._thread: Optional[threading.Thread] = None
        self._images: dict = {}
        self._state = "idle"
        self._labels = {"record": "", "play": "", "stop": ""}
        self._lock = threading.Lock()

    # ---- Lifecycle ----------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True while the tray icon is up."""
        return self._icon is not None

    @property
    def state(self) -> str:
        return self._state

    def start(self) -> bool:
        """Show the tray icon. Returns False (and logs why) if there's no tray to be had."""
        with self._lock:
            if self._icon is not None:
                return True
            try:
                pystray = self._pystray
                if pystray is None:
                    import pystray  # noqa: F811  (lazy on purpose, see the module docstring)
                self._images = load_images(self._image_loader)
                icon = pystray.Icon(
                    APP_NAME, self._images[self._state], _TITLES[self._state], self._menu(pystray)
                )
            except Exception:
                log.warning("No system tray icon (pystray unavailable)", exc_info=True)
                return False

            ready = threading.Event()

            def setup(started_icon):
                try:
                    started_icon.visible = True
                finally:
                    ready.set()

            def run():
                try:
                    icon.run(setup)
                except Exception:
                    log.warning("System tray icon stopped with an error", exc_info=True)
                finally:
                    ready.set()

            thread = threading.Thread(target=run, name=f"{APP_NAME}-tray", daemon=True)
            try:
                thread.start()
            except Exception:
                log.warning("Could not start the tray thread", exc_info=True)
                return False
            if not ready.wait(_READY_TIMEOUT) or not thread.is_alive():
                log.warning("System tray icon didn't come up")
                self._stop_icon(icon)
                return False
            self._icon, self._thread = icon, thread
            return True

    def stop(self) -> None:
        """Remove the tray icon. Safe to call any number of times, and before start()."""
        with self._lock:
            icon, self._icon = self._icon, None
            self._thread = None
        if icon is not None:
            self._stop_icon(icon)

    @staticmethod
    def _stop_icon(icon) -> None:
        try:
            icon.visible = False
        except Exception:
            log.debug("Could not hide the tray icon", exc_info=True)
        try:
            icon.stop()
        except Exception:
            log.debug("Could not stop the tray icon", exc_info=True)

    # ---- State --------------------------------------------------------------------------

    def set_state(self, state: str) -> None:
        """"idle", "recording" or "playing": changes the dot colour and the hover text."""
        if state not in STATES:
            raise ValueError(f"unknown tray state {state!r}")
        self._state = state
        icon = self._icon
        if icon is None:
            return
        try:
            icon.icon = self._images[state]
            icon.title = _TITLES[state]
        except Exception:
            log.debug("Could not update the tray icon", exc_info=True)

    def notify(self, message: str, title: str = APP_NAME) -> None:
        """A Windows notification from the tray icon (e.g. why the failsafe stopped playback).
        Silently skipped where the tray can't show one."""
        icon = self._icon
        if icon is None:
            return
        try:
            if getattr(icon, "HAS_NOTIFICATION", True):
                icon.notify(str(message)[:250], str(title)[:60])
        except Exception:
            log.debug("Could not show a tray notification", exc_info=True)

    def set_labels(self, labels: dict) -> None:
        """Hotkey names shown next to Record / Play / Stop in the tray menu."""
        for action in self._labels:
            if action in labels:
                self._labels[action] = str(labels[action])
        icon = self._icon
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                log.debug("Could not refresh the tray menu", exc_info=True)

    # ---- Menu ---------------------------------------------------------------------------

    def _text(self, title: str, action: str):
        def text(_item):
            hotkey = self._labels.get(action)
            return f"{title}\t{hotkey}" if hotkey else title
        return text

    def _menu(self, pystray):
        item = pystray.MenuItem
        return pystray.Menu(
            item(f"Show {APP_NAME}", self._handler("show"), default=True),
            pystray.Menu.SEPARATOR,
            item(self._text("Record / Stop", "record"), self._handler("record")),
            item(self._text("Play / Stop", "play"), self._handler("play")),
            item(self._text("Stop", "stop"), self._handler("stop")),
            pystray.Menu.SEPARATOR,
            item("Exit", self._handler("exit")),
        )

    def _handler(self, name: str):
        def handle(*_args):   # pystray passes (icon, item)
            self.fire(name)
        return handle

    def fire(self, name: str) -> None:
        """Run a menu action on the GUI thread. Never raises (it runs on pystray's thread)."""
        callback = self._callbacks[name]

        def call():
            try:
                callback()
            except Exception:
                log.exception("Tray action %r failed", name)

        try:
            if self._dispatch is None:
                call()
            else:
                self._dispatch(call)
        except Exception:
            log.exception("Could not dispatch the tray action %r", name)
