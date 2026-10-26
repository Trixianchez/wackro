"""The on-screen "it's live" sign: a thin coloured border around every monitor and a small pill at
the top of the main monitor (● REC 0:12 while recording, ▶ Run 2/5 · 0:07 while playing).

    overlay = Overlay(root)
    overlay.prepare()                       # once, at start-up
    overlay.show("recording", "● REC 0:00", style="both")    # or "border" / "pill" / "off"
    overlay.set_text("● REC 0:12")          # every tick
    overlay.hide()
    overlay.destroy()

How it stays out of the way (Windows):
* Every piece is click-through (clicks land on whatever is underneath, so nothing extra gets
  into a recording), never becomes the active window, isn't in Alt+Tab or the taskbar, and is
  always on top. See winutil.Win32.make_overlay.
* It is hidden from screenshots and screen recorders (winutil.exclude_from_capture; Windows 10
  2004+, silently skipped on older versions).
* The windows are created ONCE, at start-up, and parked off-screen. Showing / hiding only moves
  them. Creating or un-hiding a window can make Windows activate it, which would steal the
  keyboard focus from the program being recorded; moving one never does.

Anything that goes wrong is logged and the overlay just isn't shown: it's a convenience and must
never get in the way of recording or playing.
"""

from __future__ import annotations

import logging
import tkinter
from typing import Optional

import winutil

log = logging.getLogger(__name__)

STYLES = ("both", "border", "pill", "off")
STYLE_LABELS = {"both": "Border + REC pill", "border": "Border only", "pill": "REC pill only", "off": "Off"}
COLORS = {"recording": "#e5383b", "playing": "#2eb85c"}
THICKNESS = 3                    # border width in pixels
PILL_FONT = ("Segoe UI", 11, "bold")
PILL_ALPHA = 235                 # 0-255
# Off-screen position for hidden pieces. NOT -32000: that's where Windows puts minimized
# windows, and a window parked there is treated as minimized (Tk then never moves it back on
# screen, so the indicator never appeared on Windows). Nothing special happens at -10000.
_PARK = -10000


class Overlay:
    def __init__(self, root, win32=None):
        self._root = root
        self._win = win32 if win32 is not None else winutil.get_win32(
            lambda: (root.winfo_screenwidth(), root.winfo_screenheight()))
        self._strips: list = []          # [Toplevel] four per monitor
        self._hwnds: dict = {}           # Toplevel -> Windows handle
        self._pill: Optional[tkinter.Toplevel] = None
        self._pill_label: Optional[tkinter.Label] = None
        self.state: Optional[str] = None  # "recording" / "playing" while shown
        self.style = "off"
        self.text = ""
        self.ok = True

    # ---- building ----

    def prepare(self) -> None:
        """Build the pieces off-screen. Call once, while the app's own window is coming up."""
        try:
            self._ensure_strips(len(self._win.monitors()))
            self._ensure_pill()
        except Exception:
            log.warning("The recording indicator is unavailable", exc_info=True)
            self.ok = False

    def _new_window(self, color: str, alpha: int = 255) -> tkinter.Toplevel:
        window = tkinter.Toplevel(self._root, background=color, highlightthickness=0, borderwidth=0)
        window.overrideredirect(True)
        window.geometry(f"1x1+{_PARK}+{_PARK}")
        try:
            window.attributes("-topmost", True)
        except tkinter.TclError:
            pass
        window.update_idletasks()
        hwnd = winutil.hwnd_of(window)
        self._hwnds[window] = hwnd
        self._win.make_overlay(hwnd, alpha)
        self._win.exclude_from_capture(hwnd)
        return window

    def _put(self, window, x: int, y: int, width: int, height: int) -> None:
        """Move / resize one piece. Tk first (keeps Tk's idea in sync, and is all there is off
        Windows), then Windows directly: on Windows Tk ignores a new position for these borderless
        windows (seen on a real PC: the size changed, the position stayed off-screen)."""
        window.geometry(f"{max(1, width)}x{max(1, height)}+{x}+{y}")
        self._flush()
        self._win.move(self._hwnds.get(window), x, y, max(1, width), max(1, height))

    def _ensure_strips(self, monitors: int) -> None:
        while len(self._strips) < 4 * monitors:
            self._strips.append(self._new_window(COLORS["recording"]))

    def _ensure_pill(self) -> None:
        if self._pill is not None:
            return
        self._pill = self._new_window(COLORS["recording"], PILL_ALPHA)
        self._pill_label = tkinter.Label(self._pill, text="", font=PILL_FONT, fg="white",
                                         bg=COLORS["recording"], padx=12, pady=3)
        self._pill_label.pack()
        # Let the size change from packing the label settle NOW, while parked. Left pending, it
        # can land after a later move on screen and drag the pill back to the parking spot.
        self._pill.update_idletasks()
        self._root.update_idletasks()

    # ---- showing ----

    def show(self, state: str, text: str = "", style: str = "both") -> None:
        """Show the sign for "recording" or "playing" in `style` (see STYLES)."""
        if state not in COLORS:
            raise ValueError(f"unknown overlay state {state!r}")
        if style not in STYLES:
            style = "both"
        self.state, self.style, self.text = state, style, text
        if not self.ok or style == "off":
            self._park_all()
            return
        try:
            monitors = self._win.monitors()
            self._ensure_strips(len(monitors))
            self._ensure_pill()
            color = COLORS[state]
            if style in ("both", "border"):
                self._place_border(monitors, color)
            else:
                self._park(self._strips)
            if style in ("both", "pill"):
                self._place_pill(monitors, color, text)
            else:
                self._park([self._pill])
            self._flush()
        except Exception:
            log.warning("Could not show the recording indicator", exc_info=True)

    def set_text(self, text: str) -> None:
        """Update the pill (cheap; only touches the label when the text changed)."""
        if text == self.text or self.state is None:
            return
        self.text = text
        if self._pill_label is None or self.style not in ("both", "pill"):
            return
        try:
            self._pill_label.configure(text=text)
            self._center_pill(self._win.monitors())
        except Exception:
            log.debug("Could not update the indicator text", exc_info=True)

    def hide(self) -> None:
        self.state = None
        self._park_all()

    def destroy(self) -> None:
        for window in self._strips + ([self._pill] if self._pill is not None else []):
            try:
                window.destroy()
            except Exception:
                pass
        self._strips, self._pill, self._pill_label = [], None, None
        self._hwnds = {}
        self.state = None

    # ---- layout ----

    def _place_border(self, monitors, color: str) -> None:
        t = THICKNESS
        used = 0
        for m in monitors:
            rects = (
                (m.left, m.top, m.width, t),                  # top
                (m.left, m.bottom - t, m.width, t),           # bottom
                (m.left, m.top, t, m.height),                 # left
                (m.right - t, m.top, t, m.height),            # right
            )
            for (x, y, w, h), strip in zip(rects, self._strips[used:used + 4]):
                strip.configure(background=color)
                self._put(strip, x, y, w, h)
            used += 4
        self._park(self._strips[used:])

    def _main_monitor(self, monitors):
        return next((m for m in monitors if m.primary), monitors[0])

    def _place_pill(self, monitors, color: str, text: str) -> None:
        self._pill.configure(background=color)
        self._pill_label.configure(text=text, bg=color)
        self._center_pill(monitors)

    def _center_pill(self, monitors) -> None:
        self._flush()
        m = self._main_monitor(monitors)
        width = self._pill_label.winfo_reqwidth()
        height = self._pill_label.winfo_reqheight()
        x = m.left + (m.width - width) // 2
        y = m.top + THICKNESS + 8
        self._put(self._pill, x, y, width, height)

    def _park(self, windows) -> None:
        for window in windows:
            if window is None:
                continue
            try:
                self._put(window, _PARK, _PARK, 1, 1)
            except Exception:
                pass

    def _flush(self) -> None:
        """Apply every pending size / position change right away, so nothing stale is left
        waiting to be applied after the next move."""
        try:
            self._root.update_idletasks()
        except Exception:
            pass

    def _park_all(self) -> None:
        self._park(self._strips + [self._pill])

    # ---- for tests / diagnostics ----

    def placed(self) -> list:
        """(x, y, width, height) of every piece currently on screen."""
        result = []
        for window in self._strips + ([self._pill] if self._pill is not None else []):
            try:
                window.update_idletasks()
                x, y = window.winfo_x(), window.winfo_y()
                if x > _PARK + 1000 or y > _PARK + 1000:
                    result.append((x, y, window.winfo_width(), window.winfo_height()))
            except Exception:
                pass
        return result


def pill_text(state: str, elapsed: float, run: int = 0, runs: int = 1, pending: bool = False,
              step: str = "") -> str:
    """'● REC 0:12' / '▶ Run 2/5 · 0:07' / '▶ 0:07' / '● REC' (while starting).
    With step (sequences): '▶ Round 2/∞ · 3/5 Export · 0:07'."""
    minutes, seconds = divmod(int(max(0.0, elapsed)), 60)
    clock = f"{minutes}:{seconds:02d}"
    if state == "recording":
        return "● REC" if pending else f"● REC {clock}"
    if pending:
        return "▶ PLAY"
    parts = []
    if runs != 1:
        of = "∞" if runs == 0 else f"{runs:,}"
        parts.append(f"{'Round' if step else 'Run'} {max(1, run):,}/{of}")
    if step:
        parts.append(step)
    parts.append(clock)
    return "▶ " + " · ".join(parts)
