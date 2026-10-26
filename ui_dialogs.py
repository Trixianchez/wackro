"""Wackro's dark dialog boxes (CustomTkinter), plus the small parsers they use.

    SpeedDialog(parent, 1.0).run()                  -> 2.0, or None if cancelled
    RepeatDialog(parent, 3, False, 0.5).run()       -> {"repeat": 3, "infinite": False, "delay": 0.5}
    RecordOptionsDialog(parent, options).run()      -> {"keyboard": True, ...}
    HotkeysDialog(parent, bindings).run()           -> {"record": {...}, "play": {...}, "stop": {...}}
    CloseDialog(parent, tray=True).run()            -> ("exit" | "tray" | "cancel", remember)
    InfoDialog(parent, "About", text).run()

run() is modal: it grabs input and waits until the box is closed. The tests don't call run();
they build a dialog, poke its widgets / methods and read .result, so every box works without a
person clicking.

Enter = OK and Esc = Cancel everywhere, except while the hotkeys box is waiting for a key: then
every key (Esc included: it cancels the capture) goes to the capture.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Optional

import customtkinter as ctk

import winutil
from config import APP_NAME, MAX_REPEAT, MAX_REPEAT_DELAY, MAX_SPEED, MIN_SPEED
from hotkey_spec import (
    ACTION_LABELS,
    ACTIONS,
    DEFAULT_BINDINGS,
    MODIFIER_LABELS,
    MODIFIERS,
    binding_problem,
    find_clash,
    keysym_to_vk,
    label,
    make_binding,
    same_keys,
    tk_state_mods,
)

WINDOWS = sys.platform.startswith("win")

# ---- Theme ------------------------------------------------------------------------------

BG = "#17191d"            # window
PANEL = "#20242a"         # menu bar, dialog panels
PANEL_HOVER = "#2c3139"
BORDER = "#323842"
TEXT = "#e8eaed"
MUTED = "#9aa0a8"
FAINT = "#6b717a"
ERROR = "#ff7b72"
ACCENT = "#3d8bfd"
ACCENT_HOVER = "#2f74d8"

# ---- Parsers (no Tk needed; also used by app.py) ----------------------------------------

REPEAT_ERROR = (
    f"Repeat must be a whole number from 1 to {MAX_REPEAT:,}. "
    "Tick Infinite to play until you press Stop."
)
DELAY_ERROR = f"Delay must be a number of seconds from 0 to {MAX_REPEAT_DELAY:,.0f}."
SPEED_ERROR = f"Speed must be a number from {MIN_SPEED:g} to {MAX_SPEED:g}."
SPEED_PRESETS = (0.5, 1.0, 2.0, 5.0)


def parse_repeat(text: str) -> int:
    """'5' -> 5, '1,000' -> 1000. Raises ValueError(REPEAT_ERROR) for anything else."""
    cleaned = text.strip().replace(",", "")
    if not (cleaned.isascii() and cleaned.isdigit()):
        raise ValueError(REPEAT_ERROR)
    value = int(cleaned)
    if not 1 <= value <= MAX_REPEAT:
        raise ValueError(REPEAT_ERROR)
    return value


def parse_delay(text: str) -> float:
    """'' -> 0.0, '1.5' or '1,5' -> 1.5. Raises ValueError(DELAY_ERROR) for anything else."""
    cleaned = text.strip().replace(",", ".")
    if not cleaned:
        return 0.0
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(DELAY_ERROR) from None
    if not math.isfinite(value) or not 0 <= value <= MAX_REPEAT_DELAY:
        raise ValueError(DELAY_ERROR)
    return value


def parse_minutes(text: str, low: float, high: float, what: str) -> float:
    """'5' / '2.5' / '2,5' minutes. Raises ValueError with a message the box can show."""
    message = f"{what} must be a number of minutes from {low:g} to {high:,.0f}."
    cleaned = text.strip().replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(message) from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(message)
    return value


def format_minutes(value: float) -> str:
    return f"{value:g}"


def parse_repeat_settings(repeat_text: str, infinite: bool, delay_text: str) -> tuple:
    """The repeat box -> (repeat, delay) for Player.start. repeat is 0 for "until Stop".

    With Infinite ticked the repeat box is ignored, but the delay is still checked. Raises
    ValueError with a message that can be shown as-is.
    """
    repeat = 0 if infinite else parse_repeat(repeat_text)
    return repeat, parse_delay(delay_text)


def parse_speed(text: str) -> float:
    """'2' / '2x' / '1,5' -> a speed factor. Raises ValueError(SPEED_ERROR)."""
    cleaned = text.strip().lower()
    if cleaned.endswith(("x", "×")):
        cleaned = cleaned[:-1].strip()
    cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(SPEED_ERROR) from None
    if not math.isfinite(value) or not MIN_SPEED <= value <= MAX_SPEED:
        raise ValueError(SPEED_ERROR)
    return value


def format_speed(speed: float) -> str:
    return f"{speed:g}x"


# ---- Where a box goes when the main window is hidden ------------------------------------

# ---- CustomTkinter housekeeping ---------------------------------------------------------------

def _scaling_tracker():
    try:
        from customtkinter.windows.widgets.scaling import ScalingTracker
        return ScalingTracker
    except Exception:
        return None


def forget_window(window) -> None:
    """After a window (menu, box) is destroyed: take it out of CustomTkinter's screen-scaling
    list. CustomTkinter never does, and it checks EVERY window in that list 10 times a second,
    so without this Wackro got slower and used more memory the longer it was open."""
    tracker = _scaling_tracker()
    if tracker is None:
        return
    for name in ("window_widgets_dict", "window_dpi_scaling_dict"):
        try:
            getattr(tracker, name).pop(window, None)
        except Exception:
            pass


def sweep_dead_windows() -> int:
    """Remove every window that no longer exists from CustomTkinter's lists (a catch-all for
    windows created inside CustomTkinter itself). Returns how many were removed."""
    tracker = _scaling_tracker()
    if tracker is None:
        return 0
    removed = 0
    for name in ("window_widgets_dict", "window_dpi_scaling_dict"):
        table = getattr(tracker, name, None)
        if not isinstance(table, dict):
            continue
        for window in list(table):
            try:
                alive = bool(window.winfo_exists())
            except Exception:
                alive = False
            if not alive:
                table.pop(window, None)
                removed += 1
    return removed


def open_boxes(root) -> list:
    """Wackro's own dialog boxes that are open right now (children of root)."""
    found = []
    try:
        for child in root.winfo_children():
            if isinstance(child, BaseDialog) and child.winfo_exists():
                found.append(child)
    except Exception:
        pass
    return found


def bring_boxes_forward(root) -> bool:
    """Bring the open box(es) back to the front with the keyboard focus. True if there was one."""
    boxes = open_boxes(root)
    for box in boxes:
        try:
            box.deiconify()
            box.lift()
            box.attributes("-topmost", True)
            previous = box._win.force_foreground(winutil.hwnd_of(box))
            if box._previous_foreground is None and box.standalone:
                box._previous_foreground = previous
            box.focus_force()
        except Exception:
            pass
    return bool(boxes)


def parent_hidden(parent) -> bool:
    try:
        return parent.state() in ("withdrawn", "iconic")
    except Exception:
        return False


def pointer_monitor(widget, win=None):
    """The monitor the mouse is on (the user is looking there), else the main one."""
    win = win if win is not None else winutil.get_win32(
        lambda: (widget.winfo_screenwidth(), widget.winfo_screenheight()))
    monitors = win.monitors()
    try:
        px, py = widget.winfo_pointerxy()
        for m in monitors:
            if m.left <= px < m.right and m.top <= py < m.bottom:
                return m
    except Exception:
        pass
    return next((m for m in monitors if m.primary), monitors[0])


# ---- The base box -----------------------------------------------------------------------

class BaseDialog(ctk.CTkToplevel):
    """A small modal box with a title, a body and OK / Cancel buttons."""

    ok_text = "OK"
    urgent = False      # always on top and takes the focus, even with the main window open

    def __init__(self, parent, title: str, win32=None):
        super().__init__(parent)
        self._parent = parent
        self._win = win32 if win32 is not None else winutil.get_win32(
            lambda: (parent.winfo_screenwidth(), parent.winfo_screenheight()))
        # Main window hidden (in the tray): this box shows up on its own, on top of everything,
        # and takes the keyboard focus. Attached to a hidden window it would stay hidden too.
        self.standalone = parent_hidden(parent)
        self._previous_foreground = None
        self.result = None
        self.title(title)
        self.configure(fg_color=BG)
        self.resizable(False, False)
        if self.standalone or self.urgent:
            try:
                self.attributes("-topmost", True)
            except Exception:
                pass
        if not self.standalone:
            try:
                self.transient(parent)       # stays with the open main window
            except Exception:
                pass
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", self._on_escape)
        self.bind("<Return>", self._on_return)

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=18, pady=(16, 6))
        self.error_label = ctk.CTkLabel(self, text="", text_color=ERROR, wraplength=320,
                                        justify="left", anchor="w")   # packed only while it has text
        self.buttons = ctk.CTkFrame(self, fg_color="transparent", height=36)
        self.buttons.pack(fill="x", padx=18, pady=(10, 16))
        self.cancel_button = ctk.CTkButton(
            self.buttons, text="Cancel", width=90, fg_color=PANEL, hover_color=PANEL_HOVER,
            border_width=1, border_color=BORDER, text_color=TEXT, command=self.cancel,
        )
        self.cancel_button.pack(side="right")
        self.ok_button = ctk.CTkButton(
            self.buttons, text=self.ok_text, width=90, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=self.ok,
        )
        self.ok_button.pack(side="right", padx=(0, 8))

    # -- running --

    def run(self):
        """Show the box modally and return .result once it is closed."""
        self.update_idletasks()
        self._center()
        try:
            self.grab_set()
        except Exception:
            pass
        self._bring_forward()
        self.focus_force()
        if self.urgent:
            try:
                self._win.alert(winutil.hwnd_of(self))
            except Exception:
                pass
        # On Windows CTkToplevel gives the focus back to the main window ~10 ms after it appears
        # (after colouring its title bar, which also hides and re-shows it). Take it back
        # afterwards, or Enter / Esc and the hotkey capture would go elsewhere.
        for delay in (60, 250):
            self.after(delay, self._refocus)
        self.wait_window(self)
        return self.result

    def _bring_forward(self) -> None:
        """Standalone / urgent: to the front of every program, with the keyboard focus."""
        if not (self.standalone or self.urgent):
            return
        try:
            self.lift()
            self.attributes("-topmost", True)
            previous = self._win.force_foreground(winutil.hwnd_of(self))
            if self._previous_foreground is None:
                self._previous_foreground = previous
        except Exception:
            pass

    def _refocus(self) -> None:
        try:
            if self.winfo_exists():
                self._bring_forward()
                self.focus_force()
        except Exception:
            pass

    def _center(self) -> None:
        try:
            parent = self._parent
            if self.standalone or parent_hidden(parent):
                m = pointer_monitor(self, self._win)
                x = m.left + (m.width - self.winfo_reqwidth()) // 2
                y = m.top + (m.height - self.winfo_reqheight()) // 3
                self.geometry(f"+{x}+{y}")
                return
            else:
                x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
                y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

    # -- buttons --

    def show_error(self, text: str) -> None:
        self.error_label.configure(text=text)
        if text:
            self.error_label.pack(fill="x", padx=18, before=self.buttons)
        else:
            self.error_label.pack_forget()

    def validate(self):
        """Return the result, or raise ValueError(message) to keep the box open."""
        return True

    def ok(self) -> None:
        try:
            result = self.validate()
        except ValueError as error:
            self.show_error(str(error))
            return
        self.result = result
        self._close()

    def cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        forget_window(self)
        if self.standalone:
            # Back to the program the user was in (e.g. the one about to be recorded).
            self._win.restore_foreground(self._previous_foreground)

    def _on_escape(self, _event=None):
        self.cancel()
        return "break"

    def _on_return(self, _event=None):
        self.ok()
        return "break"

    # -- small widget helpers --

    def heading(self, parent, text: str):
        widget = ctk.CTkLabel(parent, text=text, text_color=TEXT, font=ctk.CTkFont(size=14, weight="bold"),
                              anchor="w")
        widget.pack(fill="x", pady=(0, 8))
        return widget

    def note(self, parent, text: str):
        widget = ctk.CTkLabel(parent, text=text, text_color=MUTED, font=ctk.CTkFont(size=12),
                              anchor="w", justify="left", wraplength=320)
        widget.pack(fill="x", pady=(6, 0))
        return widget


def entry(parent, value: str, width: int = 80):
    widget = ctk.CTkEntry(parent, width=width, justify="center", fg_color=PANEL, border_color=BORDER,
                          text_color=TEXT)
    widget.insert(0, value)
    return widget


def set_entry(widget, value: str) -> None:
    state = widget.cget("state")
    widget.configure(state="normal")
    widget.delete(0, "end")
    widget.insert(0, value)
    widget.configure(state=state)


# ---- Playback > Speed -------------------------------------------------------------------

class SpeedDialog(BaseDialog):
    def __init__(self, parent, speed: float):
        super().__init__(parent, "Playback speed")
        self.heading(self.body, "Playback speed")
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="Speed", text_color=TEXT).pack(side="left", padx=(0, 8))
        self.speed_entry = entry(row, format_speed(speed), width=70)
        self.speed_entry.pack(side="left")
        self.speed_entry.bind("<KeyRelease>", lambda _e: self._highlight(), add="+")
        presets = ctk.CTkFrame(self.body, fg_color="transparent")
        presets.pack(fill="x", pady=(10, 0))
        self.preset_buttons = []
        for value in SPEED_PRESETS:
            button = ctk.CTkButton(
                presets, text=format_speed(value), width=56, fg_color=PANEL, hover_color=PANEL_HOVER,
                border_width=1, border_color=BORDER, text_color=TEXT,
                command=lambda v=value: self.pick(v),
            )
            button.pack(side="left", padx=(0, 8))
            self.preset_buttons.append(button)
        self.note(self.body, f"1x = as recorded, 2x = twice as fast, 0.5x = half speed "
                             f"({MIN_SPEED:g}x to {MAX_SPEED:g}x).")
        self._highlight()

    def pick(self, value: float) -> None:
        set_entry(self.speed_entry, format_speed(value))
        self.show_error("")
        self._highlight()

    def _highlight(self) -> None:
        """The preset that matches the box gets the accent border."""
        try:
            current = parse_speed(self.speed_entry.get())
        except ValueError:
            current = None
        for value, button in zip(SPEED_PRESETS, self.preset_buttons):
            button.configure(border_color=ACCENT if current == value else BORDER)

    def validate(self):
        return parse_speed(self.speed_entry.get())


# ---- Playback > Repeat ------------------------------------------------------------------

class RepeatDialog(BaseDialog):
    def __init__(self, parent, repeat: int, infinite: bool, delay: float):
        super().__init__(parent, "Repeat")
        self.heading(self.body, "Repeat playback")
        grid = ctk.CTkFrame(self.body, fg_color="transparent")
        grid.pack(fill="x")
        ctk.CTkLabel(grid, text="Play", text_color=TEXT).grid(row=0, column=0, sticky="w", pady=4)
        self.repeat_entry = entry(grid, str(repeat))
        self.repeat_entry.grid(row=0, column=1, padx=8, pady=4)
        ctk.CTkLabel(grid, text="times", text_color=TEXT).grid(row=0, column=2, sticky="w", pady=4)
        self.infinite_var = ctk.BooleanVar(value=bool(infinite))
        self.infinite_check = ctk.CTkCheckBox(
            grid, text="Infinite (until Stop)", variable=self.infinite_var, command=self._sync,
            checkbox_width=20, checkbox_height=20, text_color=TEXT,
        )
        self.infinite_check.grid(row=1, column=0, columnspan=3, sticky="w", pady=4)
        ctk.CTkLabel(grid, text="Wait", text_color=TEXT).grid(row=2, column=0, sticky="w", pady=4)
        self.delay_entry = entry(grid, f"{delay:g}")
        self.delay_entry.grid(row=2, column=1, padx=8, pady=4)
        ctk.CTkLabel(grid, text="seconds between runs", text_color=TEXT).grid(row=2, column=2, sticky="w", pady=4)
        self.note(self.body, "Decimals are fine (0.5). The wait is skipped after the last run.")
        self._sync()

    def _sync(self) -> None:
        self.repeat_entry.configure(state="disabled" if self.infinite_var.get() else "normal")

    def validate(self):
        infinite = bool(self.infinite_var.get())
        repeat_text = self.repeat_entry.get()
        _, delay = parse_repeat_settings(repeat_text, infinite, self.delay_entry.get())
        if infinite:
            try:
                repeat = parse_repeat(repeat_text)
            except ValueError:
                repeat = 1          # greyed out and nonsense: keep a harmless number
        else:
            repeat = parse_repeat(repeat_text)
        return {"repeat": repeat, "infinite": infinite, "delay": delay}


# ---- Recording > Options ----------------------------------------------------------------

RECORD_OPTION_LABELS = (
    ("keyboard", "Keyboard"),
    ("clicks", "Mouse clicks and scrolling"),
    ("moves", "Mouse movement"),
    ("delay", "Delay (real timing between actions)"),
)


class RecordOptionsDialog(BaseDialog):
    def __init__(self, parent, options: dict):
        super().__init__(parent, "Recording options")
        self.heading(self.body, "What to record")
        self.vars = {}
        self.checks = {}
        for name, text in RECORD_OPTION_LABELS:
            var = ctk.BooleanVar(value=bool(options.get(name, True)))
            check = ctk.CTkCheckBox(self.body, text=text, variable=var, checkbox_width=20,
                                    checkbox_height=20, text_color=TEXT)
            check.pack(anchor="w", pady=3)
            self.vars[name], self.checks[name] = var, check
        self.note(self.body, "Without Delay, every action follows the one before almost at once.")

    def validate(self):
        result = {name: bool(var.get()) for name, var in self.vars.items()}
        if not (result["keyboard"] or result["clicks"] or result["moves"]):
            raise ValueError("Pick at least one of keyboard, clicks or movement, or nothing gets recorded.")
        return result


# ---- Settings > Safety --------------------------------------------------------------------

SAFETY_TAKE_OVER = (
    ("fs_keys", "I press any key"),
    ("fs_clicks", "I click or scroll"),
    ("fs_move", "I move the mouse"),
    ("fs_corner", "I fling the mouse into a screen corner"),
)
SAFETY_GOES_WRONG = (
    ("fs_window", "A popup or another program is in the way"),
    ("fs_crash", "A program crashes or stops responding"),
    ("fs_locked", "The PC gets locked (or a Windows security prompt pops up)"),
    ("fs_screen", "The screen resolution or the monitors change"),
    ("fs_sleep", "The PC falls asleep or freezes"),
    ("fs_preflight", "Warn me before playing if a program it used isn't open"),
)
TIME_LIMIT_RANGE = (1.0, 10080.0)
BLACKOUT_AFTER_RANGE = (0.5, 1440.0)
SENSITIVITY_CHOICES = (("low", "Low"), ("medium", "Medium"), ("high", "High"))


class SafetyDialog(BaseDialog):
    """Settings > Safety…: the playback failsafes (failsafe.py), each one on / off."""

    def __init__(self, parent, options: dict, stop_keys: str = "F10"):
        super().__init__(parent, "Safety")
        self.vars = {}
        self.checks = {}
        self.heading(self.body, "Stop playback when…")
        for name, text in SAFETY_TAKE_OVER:
            self._check(name, text, options)
            if name == "fs_move":
                self._sensitivity_row(options)
        self.heading(self.body, "…or when something goes wrong")
        for name, text in SAFETY_GOES_WRONG:
            self._check(name, text, options)
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(anchor="w", pady=(6, 0))
        self.limit_var = ctk.BooleanVar(value=bool(options.get("time_limit_on", False)))
        self.limit_check = ctk.CTkCheckBox(row, text="Stop playing after", variable=self.limit_var,
                                           checkbox_width=20, checkbox_height=20, text_color=TEXT)
        self.limit_check.pack(side="left")
        self.limit_entry = entry(row, format_minutes(float(options.get("time_limit", 60.0))), width=60)
        self.limit_entry.pack(side="left", padx=(6, 6))
        ctk.CTkLabel(row, text="minutes", text_color=TEXT).pack(side="left")
        self.note(self.body, f"{stop_keys} always stops playback, whatever is switched on here. "
                             "Popups are caught in recordings made with version 1.5 or newer.")
        self._update_sensitivity()

    def _check(self, name: str, text: str, options: dict) -> None:
        var = ctk.BooleanVar(value=bool(options.get(name, True)))
        check = ctk.CTkCheckBox(self.body, text=text, variable=var, checkbox_width=20, checkbox_height=20,
                                text_color=TEXT, command=self._update_sensitivity)
        check.pack(anchor="w", pady=3)
        self.vars[name], self.checks[name] = var, check

    def _sensitivity_row(self, options: dict) -> None:
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(anchor="w", padx=(28, 0), pady=(0, 4))
        ctk.CTkLabel(row, text="Sensitivity", text_color=MUTED).pack(side="left", padx=(0, 8))
        names = dict(SENSITIVITY_CHOICES)
        current = options.get("fs_move_sensitivity", "medium")
        self.sensitivity = ctk.CTkSegmentedButton(row, values=[label for _, label in SENSITIVITY_CHOICES])
        self.sensitivity.set(names.get(current, "Medium"))
        self.sensitivity.pack(side="left")

    def _update_sensitivity(self) -> None:
        try:
            on = bool(self.vars["fs_move"].get())
            self.sensitivity.configure(state="normal" if on else "disabled")
        except (KeyError, AttributeError):
            pass

    def validate(self):
        result = {name: bool(var.get()) for name, var in self.vars.items()}
        by_label = {label: name for name, label in SENSITIVITY_CHOICES}
        result["fs_move_sensitivity"] = by_label.get(self.sensitivity.get(), "medium")
        result["time_limit_on"] = bool(self.limit_var.get())
        text = self.limit_entry.get()
        if result["time_limit_on"]:
            result["time_limit"] = parse_minutes(text, *TIME_LIMIT_RANGE, what="The time limit")
        else:
            try:
                result["time_limit"] = parse_minutes(text, *TIME_LIMIT_RANGE, what="The time limit")
            except ValueError:
                result["time_limit"] = None       # off: keep the old value
        return result


# ---- Settings > Blackout & power ------------------------------------------------------------

class BlackoutDialog(BaseDialog):
    """Settings > Blackout & power…: the black screen while playing, its hotkey, keep awake."""

    def __init__(self, parent, options: dict, main_bindings: dict, windows: bool = WINDOWS):
        super().__init__(parent, "Blackout & power")
        self._windows = windows
        self._main = main_bindings
        self.hotkey = sanitize_hotkey(options.get("blackout_hotkey"))
        self.capturing = False
        self.vars = {}
        self.checks = {}
        self.heading(self.body, "Black screen while playing")
        self._check("blackout", "Black out the screen during long playbacks", options, False)
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(anchor="w", padx=(28, 0), pady=(0, 4))
        ctk.CTkLabel(row, text="after", text_color=MUTED).pack(side="left", padx=(0, 6))
        self.after_entry = entry(row, format_minutes(float(options.get("blackout_after", 5.0))), width=60)
        self.after_entry.pack(side="left")
        ctk.CTkLabel(row, text="minutes of playing", text_color=MUTED).pack(side="left", padx=(6, 0))
        self._check("blackout_dim", "Also turn the brightness down to 0 (laptop screen)", options, True)
        self._check("blackout_text", "Show a small status line on the black screen", options, True)
        keyrow = ctk.CTkFrame(self.body, fg_color="transparent")
        keyrow.pack(anchor="w", pady=(6, 0))
        ctk.CTkLabel(keyrow, text="Black out now hotkey", text_color=TEXT).pack(side="left", padx=(0, 8))
        self.key_button = ctk.CTkButton(
            keyrow, text=self._key_text(), width=120, height=30, fg_color=PANEL, hover_color=PANEL_HOVER,
            border_width=1, border_color=BORDER, text_color=TEXT, command=self.begin_capture)
        self.key_button.pack(side="left")
        self.clear_button = ctk.CTkButton(
            keyrow, text="Clear", width=10, fg_color="transparent", hover_color=PANEL_HOVER, text_color=MUTED,
            command=self.clear_hotkey)
        self.clear_button.pack(side="left", padx=(6, 0))
        self.heading(self.body, "Power")
        self._check("keep_awake", "Keep the PC awake while playing", options, True)
        self.note(self.body, "Playback keeps working under the black screen. Your first key, click or "
                             "mouse move only wakes it up (it isn't typed or clicked anywhere), then "
                             "everything works as normal again.")
        self.bind("<KeyPress>", self.on_key, add="+")

    def _check(self, name, text, options, default):
        var = ctk.BooleanVar(value=bool(options.get(name, default)))
        check = ctk.CTkCheckBox(self.body, text=text, variable=var, checkbox_width=20, checkbox_height=20,
                                text_color=TEXT)
        check.pack(anchor="w", pady=3)
        self.vars[name], self.checks[name] = var, check

    def _key_text(self) -> str:
        return label(self.hotkey) if self.hotkey else "Not set"

    def begin_capture(self) -> None:
        self.capturing = True
        self.key_button.configure(text="Press keys…", border_color=ACCENT)
        self.show_error("")
        self.focus_force()

    def end_capture(self) -> None:
        self.capturing = False
        self.key_button.configure(text=self._key_text(), border_color=BORDER)

    def clear_hotkey(self) -> None:
        self.hotkey = None
        self.end_capture()

    def on_key(self, event):
        if not self.capturing:
            return None
        vk = keysym_to_vk(event.keysym, event.keycode, self._windows)
        if event.keysym == "Escape" or vk == 0x1B:
            self.end_capture()
            return "break"
        if vk is None:
            self.show_error("That key can't be used as a hotkey here. Try another one.")
            return "break"
        return self.capture(vk, tk_state_mods(event.state, self._windows))

    def capture(self, vk: int, mods) -> str:
        from hotkey_spec import MODIFIER_VKS
        if vk in MODIFIER_VKS:
            held = [MODIFIER_LABELS[m] for m in MODIFIERS if m in set(mods) | {MODIFIER_VKS[vk]}]
            self.key_button.configure(text="+".join(held) + "+…")
            return "break"
        problem = binding_problem(vk, mods)
        if problem:
            self.show_error(problem)
            return "break"
        candidate = make_binding(vk, mods)
        for action, binding in self._main.items():
            if same_keys(candidate, binding):
                self.show_error(f"{label(candidate)} is already {ACTION_LABELS.get(action, action)}.")
                return "break"
        self.hotkey = candidate
        self.end_capture()
        self.show_error("")
        return "break"

    def _on_escape(self, event=None):
        if self.capturing:
            return "break"
        return super()._on_escape(event)

    def _on_return(self, event=None):
        if self.capturing:
            return "break"
        return super()._on_return(event)

    def validate(self):
        if self.capturing:
            raise ValueError("Press the keys for the hotkey first (or Esc).")
        result = {name: bool(var.get()) for name, var in self.vars.items()}
        result["blackout_after"] = parse_minutes(self.after_entry.get(), *BLACKOUT_AFTER_RANGE,
                                                 what="The blackout delay")
        result["blackout_hotkey"] = dict(self.hotkey, mods=list(self.hotkey["mods"])) if self.hotkey else None
        return result


def sanitize_hotkey(binding):
    from hotkey_spec import sanitize_binding
    return sanitize_binding(binding) if binding is not None else None


# ---- Settings > Hotkeys -----------------------------------------------------------------

class HotkeysDialog(BaseDialog):
    """Click a box, press the keys. Esc cancels that capture; the same keys can't be used twice."""

    def __init__(self, parent, bindings: dict, windows: bool = WINDOWS):
        super().__init__(parent, "Hotkeys")
        self._windows = windows
        self.bindings = {a: make_binding(bindings[a]["vk"], bindings[a]["mods"]) for a in ACTIONS}
        self.capturing: Optional[str] = None
        self.heading(self.body, "Hotkeys")
        grid = ctk.CTkFrame(self.body, fg_color="transparent")
        grid.pack(fill="x")
        self.key_buttons = {}
        for row, action in enumerate(ACTIONS):
            ctk.CTkLabel(grid, text=ACTION_LABELS[action], text_color=TEXT, anchor="w", width=110) \
                .grid(row=row, column=0, sticky="w", pady=4)
            button = ctk.CTkButton(
                grid, text=label(self.bindings[action]), width=170, height=32,
                font=ctk.CTkFont(size=14, weight="bold"), fg_color=PANEL, hover_color=PANEL_HOVER,
                border_width=1, border_color=BORDER, text_color=TEXT,
                command=lambda a=action: self.begin_capture(a),
            )
            button.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=4)
            self.key_buttons[action] = button
        self.reset_button = ctk.CTkButton(
            self.body, text="Reset to defaults", width=10, anchor="w", fg_color="transparent", hover_color=PANEL_HOVER,
            text_color=MUTED, command=self.reset,
        )
        self.reset_button.pack(anchor="w", pady=(8, 0))
        self.note(self.body, "Click a box, then press the keys (for example F9 or Ctrl+Shift+R). "
                             "Esc cancels. Hotkeys work everywhere, even when this window is hidden.")
        self.bind("<KeyPress>", self.on_key, add="+")

    def begin_capture(self, action: str) -> None:
        self.end_capture()
        self.capturing = action
        self.key_buttons[action].configure(text="Press keys…", border_color=ACCENT)
        self.show_error("")
        self.focus_force()
        self.after(30, self._refocus)   # in case something takes the focus right back

    def end_capture(self) -> None:
        if self.capturing is not None:
            action, self.capturing = self.capturing, None
            self.key_buttons[action].configure(text=label(self.bindings[action]), border_color=BORDER)

    def on_key(self, event):
        """<KeyPress> anywhere in the box. Returns "break" while capturing so Enter, Tab, Esc etc.
        don't also do their usual thing."""
        if self.capturing is None:
            return None
        vk = keysym_to_vk(event.keysym, event.keycode, self._windows)
        if event.keysym == "Escape" or vk == 0x1B:
            self.end_capture()
            return "break"
        if vk is None:
            self.show_error("That key can't be used as a hotkey here. Try another one.")
            return "break"
        mods = tk_state_mods(event.state, self._windows)
        return self.capture(vk, mods)

    def capture(self, vk: int, mods) -> str:
        """Use (vk, mods) for the action being captured. Modifier-only presses keep waiting."""
        action = self.capturing
        if action is None:
            return "break"
        from hotkey_spec import MODIFIER_VKS
        if vk in MODIFIER_VKS:
            held = [MODIFIER_LABELS[m] for m in MODIFIERS if m in set(mods) | {MODIFIER_VKS[vk]}]
            self.key_buttons[action].configure(text="+".join(held) + "+…")
            return "break"
        problem = binding_problem(vk, mods)
        if problem:
            self.show_error(problem)
            return "break"
        candidate = dict(self.bindings, **{action: make_binding(vk, mods)})
        clash = find_clash(candidate)
        if clash:
            other = clash[0] if clash[1] == action else clash[1]
            self.show_error(f"{label(candidate[action])} is already {ACTION_LABELS[other]}.")
            return "break"
        self.bindings = candidate
        self.end_capture()
        self.show_error("")
        return "break"

    def reset(self) -> None:
        self.end_capture()
        self.bindings = {a: make_binding(b["vk"], b["mods"]) for a, b in DEFAULT_BINDINGS.items()}
        for action in ACTIONS:
            self.key_buttons[action].configure(text=label(self.bindings[action]))
        self.show_error("")

    def _on_escape(self, event=None):
        if self.capturing is not None:   # handled by on_key
            return "break"
        return super()._on_escape(event)

    def _on_return(self, event=None):
        if self.capturing is not None:
            return "break"
        return super()._on_return(event)

    def validate(self):
        if self.capturing is not None:
            raise ValueError("Press the keys for the highlighted box first (or Esc).")
        if find_clash(self.bindings):
            raise ValueError("Two actions have the same keys.")
        return {a: dict(b, mods=list(b["mods"])) for a, b in self.bindings.items()}


# ---- Boxes answered with the keyboard ------------------------------------------------------

FOCUS_RING = "#ffffff"
URGENT = "#e3b341"


class ChoiceKeys:
    """Keyboard answers for a box with a few choice buttons:
    arrow keys / Tab move the highlight, Enter or Space picks the highlighted one, and each choice
    can have a letter (Y / N / ...). Esc stays Cancel (BaseDialog)."""

    def setup_choices(self, choices: list, selected: int = 0, letters: Optional[dict] = None) -> None:
        """choices: [(value, button)] in on-screen order. letters: {"y": value, ...}."""
        self.choices = choices
        self.letters = {k.lower(): v for k, v in (letters or {}).items()}
        self.selected = max(0, min(selected, len(choices) - 1))
        for sequence, step in (("<Left>", -1), ("<Up>", -1), ("<Right>", 1), ("<Down>", 1),
                               ("<Tab>", 1), ("<Shift-Tab>", -1), ("<ISO_Left_Tab>", -1)):
            try:
                self.bind(sequence, lambda _e, s=step: self.move_choice(s), add="+")
            except Exception:
                pass
        self.bind("<space>", lambda _e: self._pick_selected(), add="+")
        self.bind("<KP_Enter>", lambda _e: self._pick_selected(), add="+")
        self.bind("<KeyPress>", self._on_letter, add="+")
        self._paint_choices()

    def move_choice(self, step: int) -> str:
        self.selected = (self.selected + step) % len(self.choices)
        self._paint_choices()
        return "break"

    def _paint_choices(self) -> None:
        for index, (_value, button) in enumerate(self.choices):
            chosen = index == self.selected
            try:
                button.configure(border_width=2 if chosen else (1 if button.cget("fg_color") == PANEL else 0),
                                 border_color=FOCUS_RING if chosen else BORDER)
            except Exception:
                pass

    def selected_value(self):
        return self.choices[self.selected][0]

    def _pick_selected(self) -> str:
        self.pick(self.selected_value())
        return "break"

    def _on_letter(self, event):
        value = self.letters.get((getattr(event, "keysym", "") or "").lower())
        if value is None:
            return None
        self.pick(value)
        return "break"


# ---- Closing ----------------------------------------------------------------------------

class CloseDialog(ChoiceKeys, BaseDialog):
    """Exit / Hide to system tray / Cancel, with "Don't ask again".
    Keyboard: up / down (or Tab) and Enter, or E / H / Esc."""

    def __init__(self, parent, tray: bool = True):
        super().__init__(parent, APP_NAME)
        self.choice = "cancel"
        self.buttons.pack_forget()   # it has its own three buttons
        self.heading(self.body, f"Close {APP_NAME}?")
        self.exit_button = ctk.CTkButton(self.body, text="Exit", height=34, fg_color="#b3261e",
                                         hover_color="#8c1d18", command=lambda: self.pick("exit"))
        self.exit_button.pack(fill="x", pady=3)
        self.tray_button = ctk.CTkButton(
            self.body, text="Hide to system tray", height=34, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=lambda: self.pick("tray"),
        )
        if tray:
            self.tray_button.pack(fill="x", pady=3)
        self.cancel_choice = ctk.CTkButton(
            self.body, text="Cancel", height=34, fg_color=PANEL, hover_color=PANEL_HOVER, border_width=1,
            border_color=BORDER, text_color=TEXT, command=lambda: self.pick("cancel"),
        )
        self.cancel_choice.pack(fill="x", pady=3)
        self.remember_var = ctk.BooleanVar(value=False)
        self.remember_check = ctk.CTkCheckBox(
            self.body, text="Don't ask again (change it in Settings)", variable=self.remember_var,
            checkbox_width=18, checkbox_height=18, text_color=MUTED, font=ctk.CTkFont(size=12),
        )
        self.remember_check.pack(anchor="w", pady=(8, 0))
        self.tray = tray
        self.result = ("cancel", False)
        choices = [("exit", self.exit_button)] + ([("tray", self.tray_button)] if tray else []) + \
                  [("cancel", self.cancel_choice)]
        letters = {"e": "exit", "c": "cancel"}
        if tray:
            letters["h"] = "tray"
        self.setup_choices(choices, selected=1 if tray else 0, letters=letters)
        self.hint = ctk.CTkLabel(self.body, text="↑ ↓ to choose · Enter to confirm · Esc to cancel",
                                 text_color=FAINT, font=ctk.CTkFont(size=11), anchor="w")
        self.hint.pack(anchor="w", pady=(6, 0))

    def pick(self, choice: str) -> None:
        self.choice = choice
        remember = bool(self.remember_var.get()) and choice != "cancel"
        self.result = (choice, remember)
        self._close()

    def cancel(self) -> None:
        self.result = ("cancel", False)
        self._close()

    def ok(self) -> None:          # Enter: the highlighted choice
        self.pick(self.selected_value())


# ---- Unsaved recording ------------------------------------------------------------------

class SaveChangesDialog(ChoiceKeys, BaseDialog):
    """"Save changes to the current recording?"  Yes / No / Cancel (like ReMouse).

    .result is "yes" (save first), "no" (throw it away) or "cancel" (don't do it).
    Urgent: a gold warning bar, always on top with the keyboard focus, the Windows warning sound
    and a taskbar flash. Keyboard: left / right (or Tab) + Enter, or Y / N / Esc.
    """

    urgent = True

    def __init__(self, parent, detail: str = "", what: str = "recording"):
        super().__init__(parent, APP_NAME)
        self.ok_button.pack_forget()
        self.cancel_button.pack_forget()
        self.result = "cancel"
        self.warning_bar = ctk.CTkFrame(self, height=8, fg_color=URGENT, corner_radius=0)
        self.warning_bar.pack(fill="x", side="top", before=self.body)
        self.heading(self.body, f"⚠  Save changes to the current {what}?").configure(text_color=URGENT)
        if detail:
            self.note(self.body, detail)
        self.hint = ctk.CTkLabel(self.body, text="← → to choose · Enter to confirm · or press Y / N / Esc",
                                 text_color=FAINT, font=ctk.CTkFont(size=11), anchor="w")
        self.hint.pack(anchor="w", pady=(8, 0))
        self.cancel_choice = ctk.CTkButton(
            self.buttons, text="Cancel", width=90, fg_color=PANEL, hover_color=PANEL_HOVER,
            border_width=1, border_color=BORDER, text_color=TEXT, command=lambda: self.pick("cancel"),
        )
        self.cancel_choice.pack(side="right")
        self.no_button = ctk.CTkButton(
            self.buttons, text="No", width=90, fg_color=PANEL, hover_color=PANEL_HOVER,
            border_width=1, border_color=BORDER, text_color=TEXT, command=lambda: self.pick("no"),
        )
        self.no_button.pack(side="right", padx=(0, 8))
        self.yes_button = ctk.CTkButton(
            self.buttons, text="Yes", width=90, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            command=lambda: self.pick("yes"),
        )
        self.yes_button.pack(side="right", padx=(0, 8))
        self.setup_choices([("yes", self.yes_button), ("no", self.no_button), ("cancel", self.cancel_choice)],
                           selected=0, letters={"y": "yes", "n": "no", "c": "cancel"})

    def pick(self, choice: str) -> None:
        self.result = choice
        self._close()

    def ok(self) -> None:          # Enter: the highlighted choice (Yes unless you moved)
        self.pick(self.selected_value())

    def cancel(self) -> None:      # Esc / X
        self.pick("cancel")


# ---- Info (About) -----------------------------------------------------------------------

class InfoDialog(BaseDialog):
    def __init__(self, parent, title: str, heading: str, text: str):
        super().__init__(parent, title)
        self.cancel_button.pack_forget()
        self.heading(self.body, heading)
        self.text_label = ctk.CTkLabel(self.body, text=text, text_color=MUTED, justify="left", anchor="w",
                                       wraplength=340)
        self.text_label.pack(fill="x")


# ---- Hover tips ------------------------------------------------------------------------------

class Tooltip:
    """A small explanation that appears when the mouse rests on a widget for a moment."""

    def __init__(self, widget, text: str, delay: int = 450):
        self.widget, self.text, self.delay = widget, text, delay
        self._job = None
        self._tip = None
        for sequence, handler in (("<Enter>", self._schedule), ("<Leave>", self.hide), ("<ButtonPress>", self.hide)):
            try:
                widget.bind(sequence, handler, add="+")
            except Exception:
                pass

    def _schedule(self, _event=None):
        self._cancel()
        try:
            self._job = self.widget.after(self.delay, self.show)
        except Exception:
            self._job = None

    def _cancel(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def show(self):
        self._job = None
        if self._tip is not None:
            return
        try:
            import tkinter
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 18
            tip = tkinter.Toplevel(self.widget)
            tip.overrideredirect(True)
            try:
                tip.attributes("-topmost", True)
            except Exception:
                pass
            tkinter.Label(tip, text=self.text, justify="left", wraplength=300, bg=PANEL, fg=TEXT,
                          relief="solid", borderwidth=1, padx=8, pady=6,
                          font=("Segoe UI", 10)).pack()
            tip.update_idletasks()
            # keep the whole tip on screen (near the right / bottom edge it goes left / up)
            x = max(0, min(x, tip.winfo_screenwidth() - tip.winfo_reqwidth() - 8))
            if y + tip.winfo_reqheight() > tip.winfo_screenheight() - 8:
                y = max(0, self.widget.winfo_pointery() - tip.winfo_reqheight() - 12)
            tip.geometry(f"+{x}+{y}")
            self._tip = tip
        except Exception:
            self._tip = None

    def hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


# ---- Sequence > New / Edit sequence ---------------------------------------------------------

SEQUENCE_TIPS = {
    "play": "Untick to skip this step without removing it.",
    "name": "Just a label. It shows while playing, and in messages like \"stopped in step ...\".",
    "pin": "Anywhere: this step follows the order you pick below.\n\n"
           "Always first / Always last: this step plays at the start / end of EVERY round, "
           "whatever the order.",
    "times": "How many times in a row this step plays. 1 = once.",
    "pause": "Seconds to wait after this step.\n2 = always 2 seconds.\n1-5 = a random 1 to 5 seconds "
             "each time.\nEmpty = no pause.",
    "up": "Move this step up.",
    "down": "Move this step down.",
    "remove": "Remove this step from the sequence (the recording file itself isn't touched).",
    "add": "Add saved recordings (.json files). You can pick several at once.",
    "add_current": "Add the recording that's in the main window right now.",
    "in_order": "The steps play from top to bottom, every round.",
    "shuffle": "Every step plays once per round, in a random order.",
    "same_shuffle": "Shuffle once, then keep that same order for every round.",
    "random_picks": "Each round plays this many steps picked at random. The same step can come up "
                    "more than once. Pinned steps always play on top of these.",
    "rounds": "How many times the whole sequence plays.",
    "forever": "Keep going until you press your stop key.",
    "round_pause": "Seconds to wait before each new round. 2 = always 2 seconds, 1-5 = random.",
    "reroll": "Show another random example of one round.",
}
_COLUMNS = (("play", "Play", 46), ("name", "Step name", 174), ("pin", "Pin", 128), ("times", "Times", 56),
            ("pause", "Pause after", 96))
WARN = "#e3b341"


class SequenceDialog(BaseDialog):
    """The Sequence window: the steps (recordings) with Play / name / Pin / Times / Pause after,
    move up / down / remove; how the steps play (in order, shuffle, pick at random); rounds and
    the pause between rounds; and, at the bottom, a plain-English sentence of what will happen
    plus an example round that update as you change things.

    add_files(parent) -> list of file paths (the app's Open box); current: (name, recording) of
    the recording in the main window, or None. OK returns the finished sequence (version 2)."""

    ok_text = "Use sequence"

    def __init__(self, parent, sequence: dict, add_files=None, current=None):
        import random
        import tkinter
        import sequence as seq
        super().__init__(parent, "Sequence")
        self._seq = seq
        self._add_files = add_files
        self._current = current
        self._seed = random.randrange(1 << 30)
        self.rows: list = []
        self.tips: dict = {}
        self.sequence = seq.upgrade(json.loads(json.dumps(sequence)))   # edited copy, version 2

        top = ctk.CTkFrame(self.body, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text="Name", text_color=TEXT).pack(side="left", padx=(0, 8))
        self.name_entry = entry(top, self.sequence["name"], width=260)
        self.name_entry.pack(side="left")
        self.name_entry.bind("<KeyRelease>", lambda _e: self._changed(), add="+")

        header = ctk.CTkFrame(self.body, fg_color="transparent")
        header.pack(fill="x", pady=(10, 2), padx=(8, 0))
        self.header_labels = {}
        for key, text, width in _COLUMNS:
            label_widget = ctk.CTkLabel(header, text=text + " ⓘ", text_color=MUTED, width=width, anchor="w",
                                        font=ctk.CTkFont(size=12))
            label_widget.pack(side="left", padx=(0, 4))
            self.header_labels[key] = label_widget
            self.tips[key] = Tooltip(label_widget, SEQUENCE_TIPS[key])

        self.list_frame = ctk.CTkScrollableFrame(self.body, fg_color=PANEL, width=690, height=170,
                                                 corner_radius=6)
        self.list_frame.pack(fill="x")
        self.empty_label = ctk.CTkLabel(self.list_frame, text="No steps yet. Click \"+ Add recordings…\" below.",
                                        text_color=FAINT)

        adds = ctk.CTkFrame(self.body, fg_color="transparent")
        adds.pack(fill="x", pady=(8, 0))
        self.add_button = ctk.CTkButton(adds, text="+ Add recordings…", width=150, fg_color=PANEL,
                                        hover_color=PANEL_HOVER, border_width=1, border_color=BORDER,
                                        text_color=TEXT, command=self.add_recordings,
                                        state="normal" if add_files else "disabled")
        self.add_button.pack(side="left")
        Tooltip(self.add_button, SEQUENCE_TIPS["add"])
        self.add_current_button = ctk.CTkButton(
            adds, text="+ Add current recording", width=180, fg_color=PANEL, hover_color=PANEL_HOVER,
            border_width=1, border_color=BORDER, text_color=TEXT, command=self.add_current,
            state="normal" if current else "disabled")
        self.add_current_button.pack(side="left", padx=(8, 0))
        Tooltip(self.add_current_button, SEQUENCE_TIPS["add_current"])

        self.heading(self.body, "How should the steps play?").pack_configure(pady=(14, 4))
        self.order_var = tkinter.StringVar(value=self.sequence["order"])
        self.order_buttons = {}

        def order_row(key, title, description):
            row = ctk.CTkFrame(self.body, fg_color="transparent")
            row.pack(fill="x", pady=2)
            button = ctk.CTkRadioButton(row, text=title, variable=self.order_var, value=key, width=130,
                                        radiobutton_width=18, radiobutton_height=18, text_color=TEXT,
                                        command=self._changed)
            button.pack(side="left")
            self.order_buttons[key] = button
            self.tips[key] = Tooltip(button, SEQUENCE_TIPS[key])
            if description:
                ctk.CTkLabel(row, text=description, text_color=MUTED).pack(side="left", padx=(4, 10))
            return row

        order_row("in_order", "Play in order", "top to bottom, every round")
        shuffle_row = order_row("shuffle", "Shuffle", "every step once, in a random order")
        self.same_var = ctk.BooleanVar(value=self.sequence["same_shuffle"])
        self.same_check = ctk.CTkCheckBox(shuffle_row, text="keep the same order every round", variable=self.same_var,
                                          checkbox_width=18, checkbox_height=18, text_color=TEXT,
                                          command=self._changed)
        self.same_check.pack(side="left")
        self.tips["same_shuffle"] = Tooltip(self.same_check, SEQUENCE_TIPS["same_shuffle"])
        picks_row = order_row("random_picks", "Pick at random", "")
        self.picks_entry = entry(picks_row, str(self.sequence["picks"]), width=50)
        self.picks_entry.pack(side="left", padx=(4, 6))
        self.picks_entry.bind("<KeyRelease>", lambda _e: self._changed(), add="+")
        ctk.CTkLabel(picks_row, text="random steps per round (the same one can come up again)",
                     text_color=MUTED).pack(side="left")

        rounds = ctk.CTkFrame(self.body, fg_color="transparent")
        rounds.pack(fill="x", pady=(10, 0))
        rounds_label = ctk.CTkLabel(rounds, text="Rounds ⓘ", text_color=TEXT)
        rounds_label.pack(side="left", padx=(0, 8))
        self.tips["rounds"] = Tooltip(rounds_label, SEQUENCE_TIPS["rounds"])
        self.rounds_entry = entry(rounds, str(self.sequence["rounds"] or 1), width=60)
        self.rounds_entry.pack(side="left")
        self.rounds_entry.bind("<KeyRelease>", lambda _e: self._changed(), add="+")
        self.forever_var = ctk.BooleanVar(value=self.sequence["rounds"] == 0)
        self.forever_check = ctk.CTkCheckBox(rounds, text="until I stop it", variable=self.forever_var,
                                             checkbox_width=18, checkbox_height=18, text_color=TEXT,
                                             command=self._changed)
        self.forever_check.pack(side="left", padx=(10, 24))
        self.tips["forever"] = Tooltip(self.forever_check, SEQUENCE_TIPS["forever"])
        pause_label = ctk.CTkLabel(rounds, text="Pause between rounds ⓘ", text_color=TEXT)
        pause_label.pack(side="left", padx=(0, 8))
        self.tips["round_pause"] = Tooltip(pause_label, SEQUENCE_TIPS["round_pause"])
        self.round_wait_entry = self._pause_entry(rounds, seq.format_wait(self.sequence["round_wait"]))
        self.round_wait_entry.pack(side="left")
        ctk.CTkLabel(rounds, text="sec", text_color=MUTED).pack(side="left", padx=(6, 0))

        box = ctk.CTkFrame(self.body, fg_color=PANEL, corner_radius=6)
        box.pack(fill="x", pady=(12, 0))
        self.explain_label = ctk.CTkLabel(box, text="", text_color=TEXT, anchor="w", justify="left",
                                          wraplength=670)
        self.explain_label.pack(fill="x", padx=12, pady=(10, 2))
        self.warning_label = ctk.CTkLabel(box, text="", text_color=WARN, anchor="w", justify="left",
                                          wraplength=670)
        example = ctk.CTkFrame(box, fg_color="transparent")
        example.pack(fill="x", padx=12, pady=(2, 10))
        self.preview_label = ctk.CTkLabel(example, text="", text_color=MUTED, anchor="w", justify="left",
                                          wraplength=540)
        self.preview_label.pack(side="left", fill="x", expand=True)
        self.reroll_button = ctk.CTkButton(example, text="↻ Another example", width=130, height=26,
                                           fg_color="transparent", hover_color=PANEL_HOVER, border_width=1,
                                           border_color=BORDER, text_color=TEXT, command=self.reroll)
        self.reroll_button.pack(side="right")
        self.tips["reroll"] = Tooltip(self.reroll_button, SEQUENCE_TIPS["reroll"])
        self._warning_shown = False
        self.error_label.configure(wraplength=690)

        for step in self.sequence["steps"]:
            self._add_row(step)
        self._render()
        self._changed()

    # -- widgets --

    def _pause_entry(self, parent, value: str, width: int = 90):
        widget = ctk.CTkEntry(parent, width=width, placeholder_text="no pause", justify="center",
                              placeholder_text_color=FAINT)
        if value:
            widget.insert(0, value)
        widget.bind("<KeyRelease>", lambda _e: self._changed(), add="+")
        return widget

    # -- rows --

    def _add_row(self, step: dict) -> None:
        import tkinter
        self.rows.append({
            "step": step,
            "enabled": ctk.BooleanVar(value=step["enabled"]),
            "pin": tkinter.StringVar(value=self._seq.PIN_LABELS[step["pin"]]),
            "name": step["name"],
            "repeat": str(step["repeat"]),
            "wait": self._seq.format_wait(step["wait"]),
            "widgets": None,
        })

    def _sync(self) -> None:
        """Copy what's typed in the rows back into the row state (before moving / re-drawing)."""
        for row in self.rows:
            widgets = row["widgets"]
            if widgets:
                row["name"] = widgets["name"].get()
                row["repeat"] = widgets["repeat"].get()
                row["wait"] = widgets["wait"].get()

    def _render(self) -> None:
        for row in self.rows:
            if row["widgets"]:
                row["widgets"]["frame"].destroy()
                row["widgets"] = None
        self.empty_label.pack_forget()
        if not self.rows:
            self.empty_label.pack(pady=30)
        last = len(self.rows) - 1
        for index, row in enumerate(self.rows):
            frame = ctk.CTkFrame(self.list_frame, fg_color="transparent")
            frame.pack(fill="x", pady=2)
            on = ctk.CTkCheckBox(frame, text="", variable=row["enabled"], width=46, checkbox_width=20,
                                 checkbox_height=20, command=self._changed)
            on.pack(side="left", padx=(0, 4))
            name = entry(frame, row["name"], width=174)
            name.pack(side="left", padx=(0, 4))
            name.bind("<KeyRelease>", lambda _e: self._changed(), add="+")
            pin = ctk.CTkOptionMenu(frame, values=[self._seq.PIN_LABELS[p] for p in self._seq.PINS],
                                    variable=row["pin"], width=124, fg_color=BG, button_color=PANEL_HOVER,
                                    button_hover_color=BORDER, command=lambda _v: self._changed())
            pin.pack(side="left", padx=(0, 4))
            repeat = entry(frame, row["repeat"], width=52)
            repeat.pack(side="left", padx=(0, 4))
            repeat.bind("<KeyRelease>", lambda _e: self._changed(), add="+")
            wait = self._pause_entry(frame, row["wait"], width=92)
            wait.pack(side="left", padx=(0, 8))
            buttons = {}
            for key, text, enabled in (("up", "↑", index > 0), ("down", "↓", index < last), ("remove", "✕", True)):
                button = ctk.CTkButton(frame, text=text, width=28, height=26, fg_color="transparent",
                                       hover_color=PANEL_HOVER, text_color=TEXT if enabled else FAINT,
                                       state="normal" if enabled else "disabled",
                                       command=lambda k=key, i=index: self._row_action(k, i))
                button.pack(side="left", padx=1)
                Tooltip(button, SEQUENCE_TIPS[key])
                buttons[key] = button
            events = row["step"]["recording"]["events"]
            seconds = events[-1]["t"] if events else 0
            info = f"{len(events):,} actions · {seconds:.0f} s"
            ctk.CTkLabel(frame, text=info, text_color=FAINT, font=ctk.CTkFont(size=11)).pack(side="left", padx=(6, 0))
            row["widgets"] = {"frame": frame, "on": on, "name": name, "pin": pin, "repeat": repeat,
                              "wait": wait, **buttons}

    def _row_action(self, action: str, index: int) -> None:
        self._sync()
        if action == "up" and index > 0:
            self.rows[index - 1], self.rows[index] = self.rows[index], self.rows[index - 1]
        elif action == "down" and index < len(self.rows) - 1:
            self.rows[index + 1], self.rows[index] = self.rows[index], self.rows[index + 1]
        elif action == "remove":
            self.rows.pop(index)
        self._render()
        self.show_error("")
        self._changed()

    def move(self, index: int, action: str) -> None:      # for tests / keyboard use
        self._row_action(action, index)

    # -- adding --

    def add_recordings(self) -> None:
        if not self._add_files:
            return
        paths = self._add_files(self) or []
        problems = []
        self._sync()
        for path in paths:
            if len(self.rows) >= self._seq.MAX_STEPS:
                problems.append(f"A sequence can have up to {self._seq.MAX_STEPS} steps.")
                break
            try:
                kind, data = self._seq.load_any(path)
                if kind != "recording":
                    raise ValueError("that's a sequence, not a recording")
                self._add_row(self._seq.make_step(self._seq.name_from_path(path), data))
            except Exception as error:   # RecordingError / ValueError: say which file, keep going
                problems.append(f"{Path(path).name}: {error}")
        self._render()
        self.show_error(" ".join(problems)[:400])
        self._changed()

    def add_current(self) -> None:
        if not self._current:
            return
        if len(self.rows) >= self._seq.MAX_STEPS:
            self.show_error(f"A sequence can have up to {self._seq.MAX_STEPS} steps.")
            return
        self._sync()
        name, recording = self._current
        self._add_row(self._seq.make_step(name, recording))
        self._render()
        self._changed()

    # -- reading what's on screen --

    def _order(self) -> str:
        value = self.order_var.get()
        return value if value in self._seq.ORDERS else "in_order"

    def _build(self) -> dict:
        """The sequence as it stands in the window. Raises ValueError with a friendly message."""
        seq = self._seq
        self._sync()
        if not self.rows:
            raise ValueError("Add at least one recording to get started.")
        pins = {label: key for key, label in seq.PIN_LABELS.items()}
        steps = []
        for number, row in enumerate(self.rows, start=1):
            name = seq.clean_name(row["name"])
            where = f"Step {number} ({name})"
            try:
                repeat = int(row["repeat"].strip().replace(",", ""))
            except ValueError:
                repeat = 0
            if not 1 <= repeat <= seq.MAX_STEP_REPEAT:
                raise ValueError(f"{where}: Times must be a whole number from 1 to {seq.MAX_STEP_REPEAT:,}.")
            try:
                wait = seq.parse_wait(row["wait"])
            except ValueError as error:
                raise ValueError(f"{where}: {error}") from None
            steps.append(dict(row["step"], name=name, enabled=bool(row["enabled"].get()),
                              pin=pins.get(row["pin"].get(), "none"), repeat=repeat, wait=wait))
        order = self._order()
        picks = self.sequence["picks"]
        if order == "random_picks":
            try:
                picks = int(self.picks_entry.get().strip())
            except ValueError:
                picks = 0
            if not 1 <= picks <= seq.MAX_PICKS:
                raise ValueError(f"Pick at random: the number of steps must be from 1 to {seq.MAX_PICKS:,}.")
        if self.forever_var.get():
            rounds = 0
        else:
            try:
                rounds = int(self.rounds_entry.get().strip().replace(",", ""))
            except ValueError:
                rounds = 0
            if not 1 <= rounds <= MAX_REPEAT:
                raise ValueError(f"Rounds must be a whole number from 1 to {MAX_REPEAT:,} "
                                 "(or tick \"until I stop it\").")
        try:
            round_wait = seq.parse_wait(self.round_wait_entry.get())
        except ValueError as error:
            raise ValueError(f"Pause between rounds: {error}") from None
        name = " ".join(self.name_entry.get().split())[:seq.MAX_NAME] or "Sequence"
        return dict(self.sequence, name=name, order=order, same_shuffle=bool(self.same_var.get()),
                    picks=picks, rounds=rounds, round_wait=round_wait, steps=steps)

    # -- the live explanation --

    def _changed(self) -> None:
        """Something changed: refresh what depends on it (enabled boxes, the sentence, the example)."""
        order = self._order()
        try:
            self.same_check.configure(state="normal" if order == "shuffle" else "disabled")
            self.picks_entry.configure(state="normal" if order == "random_picks" else "disabled")
            self.rounds_entry.configure(state="disabled" if self.forever_var.get() else "normal")
        except Exception:
            pass
        try:
            draft = self._build()
        except ValueError as error:
            self._explain(str(error), [], "")
            return
        import random
        self._explain(self._seq.describe(draft), self._seq.warnings(draft),
                      "Example round:  " + self._seq.preview(draft, random.Random(self._seed)))

    def _explain(self, sentence: str, warnings: list, example: str) -> None:
        self.explain_label.configure(text=sentence)
        if warnings:
            self.warning_label.configure(text="⚠ " + " ".join(warnings))
            if not self._warning_shown:
                self.warning_label.pack(fill="x", padx=12, pady=(0, 2), after=self.explain_label)
                self._warning_shown = True
        elif self._warning_shown:
            self.warning_label.pack_forget()
            self._warning_shown = False
        self.preview_label.configure(text=example or "Example round:  -")

    def reroll(self) -> None:
        import random
        self._seed = random.randrange(1 << 30)
        self._changed()

    # -- closing --

    def _on_return(self, event=None):
        return "break"          # Enter in a box never closes the window by accident

    def validate(self):
        result = self._build()
        if not self._seq.enabled_steps(result):
            raise ValueError("Every step is switched off. Tick Play on at least one step.")
        try:
            return self._seq.validate_sequence(result)
        except Exception as error:
            raise ValueError(str(error)) from None
