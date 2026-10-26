"""The blackout: every monitor covered in black while a long playback keeps working underneath.

    cover = Blackout(root)
    cover.prepare()                       # once, at start-up (built off-screen)
    cover.show("▶ Run 3/10 · 12:04")      # fades in; text "" = no status line
    cover.set_text("▶ Run 3/10 · 12:05")
    cover.hide()
    cover.destroy()

Why a cover and not "monitor off": Windows switches a sleeping monitor back on at ANY input,
including the clicks and keys the player sends, so the screen would wake up within a second.
The cover is like the recording indicator (overlay.py): click-through (the player's clicks land
on the programs underneath), never activated, not in Alt+Tab, always on top, hidden from
screenshots / screen recorders. It's built once and only moved, so it never steals the focus.
Brightness 0 on top of it is brightness.Dimmer's job.

Anything that fails is logged and the cover just isn't shown.
"""

from __future__ import annotations

import logging
import tkinter
from typing import Optional

import winutil

log = logging.getLogger(__name__)

_PARK = -10000
FADE_STEPS = (60, 120, 180, 230, 255)   # opacity steps of the fade-in
FADE_MS = 90                            # between steps
TEXT_COLOR = "#3a3f47"                  # just readable on black, without lighting the room
TEXT_FONT = ("Segoe UI", 12)


class Blackout:
    def __init__(self, root, win32=None):
        self._root = root
        self._win = win32 if win32 is not None else winutil.get_win32(
            lambda: (root.winfo_screenwidth(), root.winfo_screenheight()))
        self._covers: list = []
        self._hwnds: dict = {}
        self._label: Optional[tkinter.Label] = None
        self._fade_job = None
        self.shown = False
        self.text = ""
        self.ok = True

    # ---- building ----

    def prepare(self) -> None:
        try:
            self._ensure(len(self._win.monitors()))
        except Exception:
            log.warning("The blackout is unavailable", exc_info=True)
            self.ok = False

    def _ensure(self, monitors: int) -> None:
        while len(self._covers) < max(1, monitors):
            window = tkinter.Toplevel(self._root, background="black", highlightthickness=0, borderwidth=0)
            window.overrideredirect(True)
            window.geometry(f"1x1+{_PARK}+{_PARK}")
            try:
                window.attributes("-topmost", True)
            except tkinter.TclError:
                pass
            if not self._covers:
                # the status line lives on the first cover (the main monitor's)
                self._label = tkinter.Label(window, text="", font=TEXT_FONT, fg=TEXT_COLOR, bg="black")
                self._label.place(relx=0.5, rely=0.94, anchor="center")
            window.update_idletasks()
            hwnd = winutil.hwnd_of(window)
            self._hwnds[window] = hwnd
            self._win.make_overlay(hwnd, 255)
            self._win.exclude_from_capture(hwnd)
            self._covers.append(window)

    # ---- showing ----

    def show(self, text: str = "") -> None:
        """Cover every monitor (fading in). text: the status line, "" for none."""
        self.text = text
        self.shown = True
        if not self.ok:
            return
        try:
            monitors = self._win.monitors()
            self._ensure(len(monitors))
            main = next((m for m in monitors if m.primary), monitors[0])
            ordered = [main] + [m for m in monitors if m is not main]
            for window, m in zip(self._covers, ordered):
                self._win.set_alpha(self._hwnds.get(window), FADE_STEPS[0])
                self._put(window, m.left, m.top, m.width, m.height)
            for window in self._covers[len(ordered):]:
                self._put(window, _PARK, _PARK, 1, 1)
            self._set_label(text)
            self._fade(0)
        except Exception:
            log.warning("Could not show the blackout", exc_info=True)

    def set_text(self, text: str) -> None:
        if text == self.text or not self.shown:
            return
        self.text = text
        self._set_label(text)

    def hide(self) -> None:
        self.shown = False
        self._cancel_fade()
        for window in self._covers:
            try:
                self._put(window, _PARK, _PARK, 1, 1)
            except Exception:
                pass

    def destroy(self) -> None:
        self._cancel_fade()
        for window in self._covers:
            try:
                window.destroy()
            except Exception:
                pass
        self._covers, self._hwnds, self._label = [], {}, None
        self.shown = False

    # ---- internals ----

    def _set_label(self, text: str) -> None:
        if self._label is not None:
            try:
                self._label.configure(text=text)
            except Exception:
                pass

    def _put(self, window, x, y, width, height) -> None:
        window.geometry(f"{max(1, width)}x{max(1, height)}+{x}+{y}")
        try:
            self._root.update_idletasks()
        except Exception:
            pass
        self._win.move(self._hwnds.get(window), x, y, max(1, width), max(1, height))

    def _fade(self, step: int) -> None:
        self._fade_job = None
        if not self.shown or step >= len(FADE_STEPS):
            return
        for window in self._covers:
            self._win.set_alpha(self._hwnds.get(window), FADE_STEPS[step])
        try:
            self._fade_job = self._root.after(FADE_MS, lambda: self._fade(step + 1))
        except Exception:
            pass

    def _cancel_fade(self) -> None:
        if self._fade_job is not None:
            try:
                self._root.after_cancel(self._fade_job)
            except Exception:
                pass
            self._fade_job = None

    def placed(self) -> list:
        """(x, y, width, height) of every cover on screen (tests / diagnostics)."""
        result = []
        for window in self._covers:
            try:
                window.update_idletasks()
                x, y = window.winfo_x(), window.winfo_y()
                if x > _PARK + 1000 or y > _PARK + 1000:
                    result.append((x, y, window.winfo_width(), window.winfo_height()))
            except Exception:
                pass
        return result
