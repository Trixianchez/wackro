"""The Wackro window (CustomTkinter, dark): a menu bar, big Play / Record / Stop buttons with
your hotkeys under them, a status line, live progress and a summary of what Play will do.

    App().mainloop()

State machine (self._state): "idle" -> "recording" -> "idle", or "idle" -> "playing" -> "idle".
It is set the moment Record / Play is pressed, BEFORE the window has finished hiding, so the
buttons and hotkeys are right straight away. The recorder / player really start
WINDOW_MINIMIZE_DELAY seconds later (self._pending holds that timer), so the hide animation and
the click on the button don't end up in the recording.

Hiding: while recording or playing, the window is withdrawn (gone from the taskbar AND Alt+Tab)
and the tray icon shows a red / green dot; it comes back afterwards if it was open before. If the
system has no tray, the window is minimized instead (it must always be reachable).

Hotkeys (Settings > Hotkeys, default F9 / F11 / F10):
    record  idle -> record, recording -> stop      (ignored while playing)
    play    idle -> play,   playing -> stop        (ignored while recording)
    stop    stops whatever is running
Stopping a recording with a hotkey like Ctrl+` also drops that Ctrl press from the recording.

Threads: recorder.on_auto_stop, player.on_finished, the hotkeys and the tray menu all fire on
worker threads. Every one of them goes through self._hop, which only puts the work in a
thread-safe inbox; the Tk thread empties it every _INBOX_MS. Worker threads never call into
tkinter at all: a tkinter call from another thread BLOCKS until the Tk thread is free, and the
hotkeys run inside a Windows keyboard hook that must answer at once (a hook that stalls makes all
typing lag and can be removed by Windows, killing every hotkey). It could also deadlock for a
moment: Stop waits for the player, the player's key waits for the hook, the hook waits for Tk.

Failsafes (failsafe.py, Settings > Safety): while playing, InputWatch stops playback the moment
you take over (a key, a click, moving the mouse, a screen corner) and WindowGuard checks before
every click / key that the program it was recorded in is still the one there (popups, crashes,
"not responding"). Before playing, programs the recording used that aren't open are pointed out.
Why it stopped goes in the status line and a tray notification. Also checked while playing: the
PC getting locked, the screen / monitors changing, the PC sleeping (playback falling far behind),
and an optional time limit.

Blackout (Settings > Blackout & power, off by default): after N minutes of playing with nobody
touching the PC, every monitor is covered in black (blackout.py) and the laptop brightness goes to
0 (brightness.py), while playback goes on. Your first key / click / move only wakes it up
(InputWatch swallows it), or press the optional "black out now" hotkey. The PC is kept awake while
playing. All of it is undone the moment playback ends, however it ends.

Everything the window depends on can be injected (recorder_factory, player_factory,
hotkeys_factory, tray_factory, overlay_factory, watch_factory, dialogs, delay, settings_path),
which is how the tests run it with fakes.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import subprocess
import sys
import time
from pathlib import Path
import tkinter
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

import failsafe
import folders
import ui_dialogs as ui
from blackout import Blackout
from brightness import Dimmer
import winutil
from config import (
    APP_NAME,
    APP_VERSION,
    ICON_RELATIVE_PATH,
    LOG_DIR,
    RECORDING_EXTENSION,
    WINDOW_MINIMIZE_DELAY,
    resource_path,
)
from hotkey_spec import ACTION_LABELS, ACTIONS, label as hotkey_label
from hotkeys import Hotkeys
from overlay import STYLE_LABELS as INDICATOR_LABELS
from overlay import Overlay, pill_text
from player import Player
from recorder import Recorder
from settings import load_settings, save_settings
import sequence as seq
from storage import RecordingError, load_recording, make_recording, save_recording
from tray import Tray

# Re-exported: the parsers used to live here.
from ui_dialogs import (  # noqa: F401
    DELAY_ERROR,
    REPEAT_ERROR,
    format_speed,
    parse_delay,
    parse_repeat,
    parse_repeat_settings,
)

log = logging.getLogger(__name__)

IDLE = "idle"
RECORDING = "recording"
PLAYING = "playing"

_TICK_MS = 100       # how often the counter / timer refresh while recording / playing
_IDLE_TICK_MS = 250  # ... and while idle (nothing is counting then)
_INBOX_MS = 15       # how often work handed over by other threads is run
_SWEEP_SECONDS = 30  # how often closed windows are cleared out of CustomTkinter's lists
DOUBLE_PRESS = 0.25  # s: the same Record / Play hotkey again this soon is the same press

PRIVACY_NOTICE = "Recordings keep every key you type (passwords too) as plain text."
_SAVE_KEYS_WARNING = (
    "This recording contains keystrokes. Anything you typed while recording, including "
    "passwords, is saved in the file as plain text.\n\nSave it anyway?"
)
TAGLINE = "Record it once, loop it forever."
CREDIT = "Made by Trixian Wackyll Granado"

_RECORD_COLOR = ("#c0392b", "#c0392b")
_RECORD_HOVER = ("#a93226", "#a93226")
_STOP_COLOR = ("#4a5260", "#4a5260")
_STOP_HOVER = ("#3d444f", "#3d444f")
_PLAY_COLOR = ("#2e8b57", "#2e8b57")
_PLAY_HOVER = ("#27774b", "#27774b")

_DISABLED_COLOR = ("#2a2f36", "#2a2f36")   # a big button that can't be pressed right now

_RECORD_OPTION_KEYS = {"keyboard": "record_keyboard", "clicks": "record_clicks",
                       "moves": "record_moves", "delay": "record_delay"}


# ---- Small pure helpers (no Tk needed to test them) --------------------------------------

def format_duration(seconds: float) -> str:
    """0 -> '0:00.0', 75.25 -> '1:15.2'"""
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    return f"{minutes}:{seconds - minutes * 60:04.1f}"


def has_key_events(events: list) -> bool:
    return any(event.get("type") in ("key_press", "key_release") for event in events)


def duration_of(events: list) -> float:
    return float(events[-1]["t"]) if events else 0.0


def plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" + ("" if count == 1 else "s")


def playing_status(repeat: int, stop_keys: str = "F10") -> str:
    if repeat == 1:
        return f"Playing… press {stop_keys} to stop."
    if repeat == 0:
        return f"Playing on repeat… press {stop_keys} to stop."
    return f"Playing {repeat:,} runs… press {stop_keys} to stop."


def playback_summary(settings: dict) -> str:
    """'Repeat 3× · wait 0.5 s · 2x speed' for the line under the buttons."""
    parts = ["Repeat ∞" if settings["infinite"] else f"Repeat {settings['repeat']:,}×"]
    if (settings["infinite"] or settings["repeat"] != 1) and settings["delay"]:
        parts.append(f"wait {settings['delay']:g} s")
    parts.append(f"{format_speed(settings['speed'])} speed")
    return " · ".join(parts)


OPEN_FILE_TYPES = [("Wackro recordings and sequences", "*.json *.wseq"), ("Recordings", "*.json"),
                   ("Sequences", "*.wseq"), ("All files", "*.*")]
RECORDING_FILE_TYPES = [("Wackro recording", "*.json"), ("All files", "*.*")]
SEQUENCE_FILE_TYPES = [("Wackro sequence", "*.wseq"), ("All files", "*.*")]


def sequence_has_keys(sequence: dict) -> bool:
    return any(has_key_events(step["recording"]["events"]) for step in sequence["steps"])


def recording_summary(settings: dict) -> str:
    """'' when everything is recorded, else e.g. 'Recording: keys, clicks · no delay'."""
    names = [("record_keyboard", "keys"), ("record_clicks", "clicks"), ("record_moves", "moves")]
    kept = [word for key, word in names if settings[key]]
    parts = []
    if len(kept) != len(names):
        parts.append("Recording: " + (", ".join(kept) if kept else "nothing"))
    if not settings["record_delay"]:
        parts.append("no delay")
    return " · ".join(parts)


# ---- Dialogs (replaced in tests) ---------------------------------------------------------

class Dialogs:
    """The dialog boxes. A separate class so tests can swap in fakes.

    When the main window is hidden in the tray, the native boxes (Save As, Open, the keystroke
    warning, errors) get a tiny invisible always-on-top window as their owner, brought to the
    front with the focus. Owned by the hidden main window they would open hidden or behind the
    program being used. The dark boxes (ui_dialogs) do the same thing by themselves.
    """

    def __init__(self, parent, win32=None):
        self._parent = parent
        self._win = win32
        self.start_dir = None   # callable -> folder the Save / Open boxes start in (the App sets it)

    def _start(self) -> dict:
        """initialdir for a file box: the recordings folder (folders.py), if known."""
        try:
            folder = self.start_dir() if self.start_dir is not None else None
        except Exception:
            log.warning("Could not work out the recordings folder", exc_info=True)
            folder = None
        return {"initialdir": folder} if folder else {}

    def _win32(self):
        if self._win is None:
            self._win = winutil.get_win32(
                lambda: (self._parent.winfo_screenwidth(), self._parent.winfo_screenheight()))
        return self._win

    @contextlib.contextmanager
    def _owner(self):
        """The window native boxes belong to (see the class docstring)."""
        if not ui.parent_hidden(self._parent):
            yield self._parent
            return
        win = self._win32()
        anchor = tkinter.Toplevel(self._parent)
        previous = None
        try:
            anchor.overrideredirect(True)
            try:
                anchor.attributes("-alpha", 0.0)
                anchor.attributes("-topmost", True)
            except tkinter.TclError:
                pass
            m = ui.pointer_monitor(anchor, win)
            anchor.geometry(f"1x1+{m.left + m.width // 2}+{m.top + m.height // 3}")
            anchor.update_idletasks()
            previous = win.force_foreground(winutil.hwnd_of(anchor))
            anchor.focus_force()
            yield anchor
        finally:
            try:
                anchor.destroy()
            except Exception:
                pass
            win.restore_foreground(previous)

    def save_path(self) -> Optional[str]:
        with self._owner() as owner:
            return filedialog.asksaveasfilename(
                parent=owner,
                title="Save recording",
                defaultextension=RECORDING_EXTENSION,
                filetypes=RECORDING_FILE_TYPES,
                **self._start(),
            ) or None

    def open_path(self) -> Optional[str]:
        with self._owner() as owner:
            return filedialog.askopenfilename(
                parent=owner, title="Open a recording or sequence", filetypes=OPEN_FILE_TYPES,
                **self._start(),
            ) or None

    def save_sequence_path(self) -> Optional[str]:
        with self._owner() as owner:
            return filedialog.asksaveasfilename(
                parent=owner, title="Save sequence", defaultextension=seq.SEQUENCE_EXTENSION,
                filetypes=SEQUENCE_FILE_TYPES, **self._start(),
            ) or None

    def recording_paths(self, parent) -> list:
        """Sequence window > Add recordings…: several recording files at once."""
        return list(filedialog.askopenfilenames(
            parent=parent, title="Add recordings to the sequence", filetypes=RECORDING_FILE_TYPES,
            **self._start()) or [])

    def ask_sequence(self, sequence: dict, current=None) -> Optional[dict]:
        return ui.SequenceDialog(self._parent, sequence, add_files=self.recording_paths, current=current).run()

    def confirm(self, title: str, message: str) -> bool:
        with self._owner() as owner:
            return bool(messagebox.askokcancel(title, message, icon="warning", parent=owner))

    def error(self, title: str, message: str) -> None:
        with self._owner() as owner:
            messagebox.showerror(title, message, parent=owner)

    def info(self, title: str, heading: str, text: str) -> None:
        ui.InfoDialog(self._parent, title, heading, text).run()

    def ask_save_changes(self, detail: str = "", what: str = "recording") -> str:
        return ui.SaveChangesDialog(self._parent, detail, what).run()

    def close_choice(self, tray: bool) -> tuple:
        return ui.CloseDialog(self._parent, tray=tray).run()

    def ask_speed(self, speed: float) -> Optional[float]:
        return ui.SpeedDialog(self._parent, speed).run()

    def ask_repeat(self, repeat: int, infinite: bool, delay: float) -> Optional[dict]:
        return ui.RepeatDialog(self._parent, repeat, infinite, delay).run()

    def ask_record_options(self, options: dict) -> Optional[dict]:
        return ui.RecordOptionsDialog(self._parent, options).run()

    def ask_hotkeys(self, bindings: dict) -> Optional[dict]:
        return ui.HotkeysDialog(self._parent, bindings).run()

    def ask_safety(self, options: dict, stop_keys: str) -> Optional[dict]:
        return ui.SafetyDialog(self._parent, options, stop_keys).run()

    def ask_blackout(self, options: dict, main_bindings: dict) -> Optional[dict]:
        return ui.BlackoutDialog(self._parent, options, main_bindings).run()

    def ask_recordings_folder(self, current: str, default: str) -> Optional[dict]:
        return ui.RecordingsFolderDialog(self._parent, current, default).run()


class _GuardedDialogs:
    """Wraps the dialogs so the app knows when a box is open (Record / Play hotkeys then do
    nothing: they would otherwise start behind a modal box)."""

    def __init__(self, app, dialogs):
        self._app = app
        self._dialogs = dialogs

    def __getattr__(self, name):
        target = getattr(self._dialogs, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            self._app._boxes_open += 1
            try:
                return target(*args, **kwargs)
            finally:
                self._app._boxes_open -= 1

        return call


# ---- The dark drop-down menus -------------------------------------------------------------

class DropdownMenu(tkinter.Toplevel):
    """A borderless dark list of menu items under a menu-bar button.

    items: (text, command) / (text, command, enabled) tuples, or None for a separator.

    It is a plain tkinter Toplevel on purpose, not a CTkToplevel: on Windows, CTkToplevel hides
    and re-shows itself to colour its title bar and then hands the keyboard focus back to the
    main window 10 ms later. A menu that closed on losing focus therefore vanished right after
    opening on Windows. This one never takes the focus; the app closes it (see App._watch_menu):
    a click on an item or anywhere else, Esc, or the app losing focus to another program.
    """

    def __init__(self, app, anchor, items):
        super().__init__(app, background=ui.BORDER)
        self.app = app
        self.withdraw()
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        frame = ctk.CTkFrame(self, fg_color=ui.PANEL, corner_radius=0)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        self.item_buttons = {}
        for item in items:
            if item is None:
                ctk.CTkFrame(frame, height=1, fg_color=ui.BORDER).pack(fill="x", padx=8, pady=4)
                continue
            text, command = item[0], item[1]
            enabled = item[2] if len(item) > 2 else True
            button = ctk.CTkButton(
                frame, text=text, anchor="w", height=28, width=210, corner_radius=4,
                fg_color="transparent", hover_color=ui.PANEL_HOVER, text_color=ui.TEXT,
                text_color_disabled=ui.FAINT, state="normal" if enabled else "disabled",
                command=lambda c=command: self.choose(c),
            )
            button.pack(fill="x", padx=4, pady=1)
            self.item_buttons[text] = button
        self.update_idletasks()
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height()
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.lift()

    def contains(self, widget) -> bool:
        """True if `widget` (a Tk widget or its path name) is part of this menu."""
        return str(widget).startswith(str(self))

    def choose(self, command) -> None:
        self.close()
        self.app.after(1, command)   # after the menu is gone, so dialogs open on a clean window

    def close(self) -> None:
        if self.app._menu is self:
            self.app._menu = None
        try:
            self.destroy()
        except Exception:
            pass
        ui.forget_window(self)


# ---- The window --------------------------------------------------------------------------

class App(ctk.CTk):
    def __init__(
        self,
        recorder_factory=Recorder,
        player_factory=Player,
        hotkeys_factory=Hotkeys,
        tray_factory=Tray,
        overlay_factory=Overlay,
        watch_factory=failsafe.InputWatch,
        blackout_factory=Blackout,
        dimmer=None,
        dialogs=None,
        delay: float = WINDOW_MINIMIZE_DELAY,
        settings_path=None,
    ):
        ctk.set_appearance_mode("dark")
        super().__init__()
        self.title(APP_NAME)
        self.configure(fg_color=ui.BG)
        self.geometry("440x285")
        self.resizable(False, False)

        self._delay_ms = max(0, int(delay * 1000))
        self._raw_dialogs = dialogs if dialogs is not None else Dialogs(self)
        self._dialogs = _GuardedDialogs(self, self._raw_dialogs)

        self._state = IDLE
        self._pending = None          # after() id of the delayed recorder / player start
        self._events: list = []       # the current recording (recorded or loaded)
        self._path: Optional[str] = None   # where the current recording was saved / loaded
        self._dirty = False           # True while the current recording hasn't been saved
        self._sequence: Optional[dict] = None   # the sequence (Sequence menu), if any
        self._seq_path: Optional[str] = None
        self._seq_dirty = False
        self._mode = "recording"      # what Play / Save use: "recording" or "sequence"
        self._runner = None           # sequence.Runner of the running playback
        self._status = "Ready."
        self._closed = False
        self._tick_job = None
        self._menu: Optional[DropdownMenu] = None
        self._restore_after = True    # show the window again when recording / playing ends
        self._stop_modifiers: set = set()
        self._tray_state = None
        self._inbox: "queue.SimpleQueue" = queue.SimpleQueue()   # work from other threads (see _hop)
        self._boxes_open = 0          # dialog boxes on screen: Record / Play hotkeys wait for them
        self._inbox_job = None
        self._settings_path = settings_path       # None = the default file (config.SETTINGS_PATH)
        self._settings = load_settings(settings_path)
        self._raw_dialogs.start_dir = self.recordings_folder
        self._play_options = {"repeat": 1, "delay": 0.0, "speed": 1.0}   # the running / pending playback
        self._blacked = False          # the blackout is up
        self._play_began = 0.0         # self._clock() when the player really started
        self._last_wake = -1e9
        self._screen_sig = None        # the monitors when playback started
        self._next_screen_check = 0.0
        self._awake = False            # keep-awake is switched on
        self._clock = time.monotonic   # tests replace it
        self._last_press: dict = {}    # hotkey action -> when it was last pressed (double taps)
        self._double_press = DOUBLE_PRESS
        self._next_sweep = 0.0
        self._shown_texts: dict = {}   # widget -> the text it shows (only changes are drawn)

        self._build_widgets()
        self._set_icon()

        self._recorder = recorder_factory(on_auto_stop=self._on_auto_stop)
        self._player = player_factory(on_finished=self._on_play_finished)
        self._hotkeys = hotkeys_factory(
            on_action=self.on_hotkey, bindings=self._settings["hotkeys"], dispatch=self._hop,
        )
        self._apply_blackout_hotkey()
        try:
            self._hotkeys.start()
        except RuntimeError as error:
            self._status = f"{error} The buttons still work."

        self._tray = tray_factory(
            on_show=self.show_window, on_record=lambda: self.on_hotkey("record"),
            on_play=lambda: self.on_hotkey("play"), on_stop=lambda: self.on_hotkey("stop"),
            on_exit=self.exit_app, dispatch=self._hop,
        )
        try:
            self._tray_ok = bool(self._tray.start())
        except Exception:
            log.warning("Tray icon failed to start", exc_info=True)
            self._tray_ok = False
        self._update_hotkey_labels()

        # Failsafe: you take over -> playback stops (see failsafe.py). Starts with each playback.
        self._watch = watch_factory(
            on_trip=self._on_failsafe_trip,
            screen_corners=lambda: failsafe.corners_of(self._win32().monitors()),
        )

        # Blackout: the black cover (built now, off-screen) and the brightness. A brightness left
        # at 0 by a crash is put back right away.
        self._dimmer = dimmer if dimmer is not None else Dimmer()
        try:
            self._dimmer.recover()
        except Exception:
            log.debug("Brightness recovery failed", exc_info=True)
        self._blackout = blackout_factory(self)
        try:
            self._blackout.prepare()
        except Exception:
            log.warning("Blackout unavailable", exc_info=True)

        # The on-screen "it's live" sign. Built now, off-screen: showing it later only moves it,
        # so it can never take the keyboard focus from the program being recorded.
        self._overlay_shown = None      # (state, style) currently on screen
        self._overlay = overlay_factory(self)
        try:
            self._overlay.prepare()
        except Exception:
            log.warning("Recording indicator unavailable", exc_info=True)

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind_all("<Button-1>", self._close_menu_on_click, add="+")   # clicks in any of our windows
        self.bind("<Escape>", lambda _e: self._close_menu(), add="+")
        self._refresh()
        self._tick()
        self._drain_inbox()

    def _win32(self):
        return winutil.get_win32(lambda: (self.winfo_screenwidth(), self.winfo_screenheight()))

    # ---- Widgets ------------------------------------------------------------------------

    def _build_widgets(self) -> None:
        self.grid_columnconfigure(0, weight=1)

        # Menu bar
        bar = ctk.CTkFrame(self, fg_color=ui.PANEL, corner_radius=0, height=30)
        bar.grid(row=0, column=0, sticky="ew")
        self._menu_buttons = {}
        for name in ("File", "Playback", "Recording", "Sequence", "Settings", "Help"):
            button = ctk.CTkButton(
                bar, text=name, width=10, height=26, corner_radius=4, fg_color="transparent",
                hover_color=ui.PANEL_HOVER, text_color=ui.TEXT, font=ctk.CTkFont(size=13),
                command=lambda n=name: self.toggle_menu(n),
            )
            button.pack(side="left", padx=(6 if name == "File" else 0, 2), pady=2)
            self._menu_buttons[name] = button

        # The three big buttons, with their hotkeys underneath
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=1, column=0, sticky="ew", padx=14, pady=(14, 0))
        main.grid_columnconfigure((0, 1, 2), weight=1, uniform="buttons")
        big = ctk.CTkFont(size=17, weight="bold")
        small = ctk.CTkFont(size=12)
        self._play_btn = ctk.CTkButton(main, text="▶  Play", font=big, height=58, fg_color=_PLAY_COLOR,
                                       hover_color=_PLAY_HOVER, command=self.start_playback)
        self._record_btn = ctk.CTkButton(main, text="●  Record", font=big, height=58, fg_color=_RECORD_COLOR,
                                         hover_color=_RECORD_HOVER, command=self.start_recording)
        self._stop_btn = ctk.CTkButton(main, text="■  Stop", font=big, height=58, fg_color=_STOP_COLOR,
                                       hover_color=_STOP_HOVER, command=lambda: self.stop(trim_tail=True))
        self._key_labels = {}
        for column, (button, action) in enumerate(
            ((self._play_btn, "play"), (self._record_btn, "record"), (self._stop_btn, "stop"))
        ):
            button.grid(row=0, column=column, padx=4, sticky="ew")
            caption = ctk.CTkLabel(main, text="", font=small, text_color=ui.MUTED)
            caption.grid(row=1, column=column, pady=(3, 0))
            self._key_labels[action] = caption

        # Status, progress and the summary of what Play will do
        self._status_label = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=14), text_color=ui.TEXT,
                                          wraplength=405, justify="left", anchor="w")
        self._status_label.grid(row=2, column=0, padx=18, pady=(12, 0), sticky="ew")
        self._info_label = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=13), text_color=ui.MUTED,
                                        anchor="w")
        self._info_label.grid(row=3, column=0, padx=18, sticky="ew")
        self._summary_label = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=12), text_color=ui.MUTED,
                                           anchor="w", justify="left")
        self._summary_label.grid(row=4, column=0, padx=18, pady=(6, 0), sticky="ew")
        self._notice = ctk.CTkLabel(self, text="⚠ " + PRIVACY_NOTICE, font=ctk.CTkFont(size=11),
                                    text_color=ui.FAINT, anchor="w")
        self._notice.grid(row=5, column=0, padx=18, pady=(4, 10), sticky="ew")

    def _set_icon(self) -> None:
        """Window / taskbar icon from assets/Wackro.ico. Does nothing if the file isn't there.

        CustomTkinter sets its own icon shortly after the window appears on Windows, so ours is
        applied a moment later to win.
        """
        path = resource_path(ICON_RELATIVE_PATH)
        if not path.is_file():
            return

        def apply() -> None:
            if self._closed:
                return
            try:
                self.iconbitmap(str(path))
            except Exception:
                log.debug("Could not set the window icon", exc_info=True)

        try:
            self.after(200, apply)
        except Exception:
            log.debug("Could not schedule the window icon", exc_info=True)

    # ---- Menus --------------------------------------------------------------------------

    def menu_items(self, name: str) -> list:
        """What each menu holds: (text, command[, enabled]) or None (separator). Built fresh every
        time a menu opens, so check marks and greyed-out items are always current."""
        idle = self._state == IDLE
        have = self._have_playable()
        close_action = self._settings["close_action"]

        def check(on: bool, text: str) -> str:
            return ("✓  " if on else "     ") + text

        if name == "File":
            return [
                ("New", self.new_recording, idle),
                ("Open…", self.load, idle),
                ("Save", self.save_quick, idle and have),
                ("Save As…", self.save, idle and have),
                None,
                ("Hide to tray", self.hide_to_tray),
                ("Exit", self.exit_app),
            ]
        if name == "Playback":
            return [
                (f"Speed…   ({format_speed(self._settings['speed'])})", self.open_speed, idle),
                ("Repeat…", self.open_repeat, idle and self._mode == "recording"),
                None,
                (f"Play / Stop   {self._binding_label('play')}", lambda: self.on_hotkey("play")),
            ]
        if name == "Recording":
            return [
                ("Options…", self.open_record_options, idle),
                None,
                (f"Record / Stop   {self._binding_label('record')}", lambda: self.on_hotkey("record")),
            ]
        if name == "Sequence":
            return [
                ("New sequence…", self.new_sequence, idle),
                ("Edit sequence…", self.edit_sequence, idle and self._sequence is not None),
                ("Open…", self.load, idle),
                None,
                (check(self._mode == "recording", "Play: current recording"), lambda: self.set_mode("recording"),
                 idle and bool(self._events)),
                (check(self._mode == "sequence", "Play: sequence"), lambda: self.set_mode("sequence"),
                 idle and self._sequence is not None),
            ]
        if name == "Settings":
            indicator = self._settings["indicator"]
            return [
                ("Hotkeys…", self.open_hotkeys, idle),
                ("Safety…", self.open_safety, idle),
                ("Blackout & power…", self.open_blackout, idle),
                ("Recordings folder…", self.open_recordings_folder, idle),
                None,
                *[(check(indicator == style, f"Indicator: {INDICATOR_LABELS[style]}"),
                   lambda s=style: self.set_indicator(s)) for style in ("both", "border", "pill", "off")],
                (check(self._settings["indicator_playback"], "Indicator while playing"),
                 self.toggle_indicator_playback),
                None,
                (check(close_action == "ask", "Close button asks"), lambda: self.set_close_action("ask")),
                (check(close_action == "tray", "Close button hides to tray"),
                 lambda: self.set_close_action("tray"), self._tray_ok),
                (check(close_action == "exit", "Close button exits"), lambda: self.set_close_action("exit")),
            ]
        if name == "Help":
            return [
                (f"About {APP_NAME}", self.show_about),
                ("Open log folder", self.open_log_folder),
            ]
        raise KeyError(name)

    def toggle_menu(self, name: str) -> None:
        """Menu-bar button: open that menu, or close it if it is already open."""
        current = self._menu
        self._close_menu()
        if current is not None and getattr(current, "menu_name", None) == name:
            return
        try:
            menu = DropdownMenu(self, self._menu_buttons[name], self.menu_items(name))
        except Exception:
            log.exception("Could not open the %s menu", name)
            return
        menu.menu_name = name
        self._menu = menu
        self._menu_seen_focus = False   # only "lost focus" after we've seen the app have it

    def _close_menu(self) -> None:
        menu, self._menu = self._menu, None
        if menu is not None:
            menu.close()

    def _close_menu_on_click(self, event) -> None:
        """A click anywhere: close the open menu, unless the click is on the menu itself (its
        items close it) or on a menu-bar button (that button's command toggles it)."""
        menu = self._menu
        if menu is None:
            return
        widget = str(event.widget)
        if menu.contains(widget):
            return
        if any(widget.startswith(str(b)) for b in self._menu_buttons.values()):
            return
        self._close_menu()

    def _watch_menu(self) -> None:
        """Every tick: close the menu once the app has lost the focus to another program."""
        if self._menu is None:
            return
        try:
            has_focus = self.focus_displayof() is not None
        except Exception:
            has_focus = True
        if has_focus:
            self._menu_seen_focus = True
        elif getattr(self, "_menu_seen_focus", False):
            self._close_menu()

    # ---- Settings -----------------------------------------------------------------------

    def _save_settings(self) -> None:
        """Remember the settings. A convenience: if this fails it is logged and ignored, and never
        keeps the window from playing or closing."""
        try:
            save_settings(self._settings, self._settings_path)
        except Exception:
            log.warning("Could not save the settings", exc_info=True)

    def _binding_label(self, action: str) -> str:
        return hotkey_label(self._settings["hotkeys"][action])

    def _update_hotkey_labels(self) -> None:
        for action in ACTIONS:
            self._key_labels[action].configure(text=self._binding_label(action))
        try:
            self._tray.set_labels({a: self._binding_label(a) for a in ACTIONS})
        except Exception:
            log.debug("Could not update the tray menu labels", exc_info=True)

    def open_speed(self) -> None:
        if self._state != IDLE:
            return
        speed = self._dialogs.ask_speed(self._settings["speed"])
        if speed is not None:
            self._settings["speed"] = float(speed)
            self._save_settings()
            self._set_status(f"Playback speed: {format_speed(speed)}.")

    def open_repeat(self) -> None:
        if self._state != IDLE:
            return
        s = self._settings
        result = self._dialogs.ask_repeat(s["repeat"], s["infinite"], s["delay"])
        if result is not None:
            s.update(repeat=int(result["repeat"]), infinite=bool(result["infinite"]), delay=float(result["delay"]))
            self._save_settings()
            self._set_status(playback_summary(s) + ".")

    def open_record_options(self) -> None:
        if self._state != IDLE:
            return
        current = {name: self._settings[key] for name, key in _RECORD_OPTION_KEYS.items()}
        result = self._dialogs.ask_record_options(current)
        if result is not None:
            for name, key in _RECORD_OPTION_KEYS.items():
                self._settings[key] = bool(result.get(name, True))
            self._save_settings()
            self._set_status("Recording options saved.")

    def open_hotkeys(self) -> None:
        """Settings > Hotkeys. The hotkeys are switched off while the box is open, so pressing
        them there only sets them."""
        if self._state != IDLE:
            return
        self._hotkeys.set_enabled(False)
        try:
            result = self._dialogs.ask_hotkeys(self._settings["hotkeys"])
        finally:
            self._hotkeys.set_enabled(True)
        if result is None:
            return
        self._settings["hotkeys"] = result
        self._hotkeys.set_bindings(result)
        self._settings["hotkeys"] = self._hotkeys.bindings   # the sanitized form
        try:
            self._settings["blackout_hotkey"] = self._hotkeys.extras.get("blackout")
        except Exception:
            pass
        self._save_settings()
        self._update_hotkey_labels()
        self._set_status("Hotkeys: " + ", ".join(
            f"{ACTION_LABELS[a]} {self._binding_label(a)}" for a in ACTIONS) + ".")

    def _documents_from_windows(self):
        try:
            return winutil.get_win32().documents_folder()
        except Exception:
            return None

    def recordings_folder(self) -> str:
        """Where Save / Open boxes start (created if needed). See folders.py."""
        return folders.dialog_start_dir(self._settings, self._documents_from_windows)

    def open_recordings_folder(self) -> None:
        """Settings > Recordings folder…"""
        if self._state != IDLE:
            return
        current = folders.recordings_dir(self._settings, self._documents_from_windows)
        default = folders.default_recordings_dir(self._documents_from_windows)
        result = self._dialogs.ask_recordings_folder(str(current), str(default))
        if result is None:
            return
        self._settings["recordings_dir"] = result.get("recordings_dir")
        self._save_settings()
        shown = folders.recordings_dir(self._settings, self._documents_from_windows)
        self._set_status(f"Recordings are saved in {shown}.")

    def _stop_keys(self) -> str:
        """'F11 or F10': what stops playback."""
        keys = self._binding_label("play")
        if self._binding_label("stop") != keys:
            keys += f" or {self._binding_label('stop')}"
        return keys

    def open_safety(self) -> None:
        """Settings > Safety…: the playback failsafes."""
        if self._state != IDLE:
            return
        current = dict(failsafe.options_from(self._settings), time_limit_on=self._settings["time_limit_on"],
                       time_limit=self._settings["time_limit"])
        result = self._dialogs.ask_safety(current, self._stop_keys())
        if result is None:
            return
        self._settings.update(failsafe.options_from(result))
        self._settings["time_limit_on"] = bool(result.get("time_limit_on", False))
        if result.get("time_limit") is not None:
            self._settings["time_limit"] = float(result["time_limit"])
        self._save_settings()
        switches = [k for k in failsafe.DEFAULT_OPTIONS if k != "fs_move_sensitivity"]
        on = [k for k in switches if self._settings[k]]
        text = f"Safety: {len(on)} of {len(switches)} failsafes on"
        if self._settings["time_limit_on"]:
            text += f", stop after {ui.format_minutes(self._settings['time_limit'])} min"
        self._set_status(text + ".")

    def open_blackout(self) -> None:
        """Settings > Blackout & power…"""
        if self._state != IDLE:
            return
        keys = ("blackout", "blackout_after", "blackout_dim", "blackout_text", "keep_awake", "blackout_hotkey")
        current = {k: self._settings[k] for k in keys}
        self._hotkeys.set_enabled(False)     # so pressing the keys in the box only sets them
        try:
            result = self._dialogs.ask_blackout(current, self._settings["hotkeys"])
        finally:
            self._hotkeys.set_enabled(True)
        if result is None:
            return
        for key in keys:
            if key in result:
                self._settings[key] = result[key]
        self._apply_blackout_hotkey()
        self._save_settings()
        if self._settings["blackout"]:
            text = f"Blackout after {ui.format_minutes(self._settings['blackout_after'])} min of playing"
        else:
            text = "Blackout off"
        if self._settings["blackout_hotkey"]:
            text += f" · black out now: {hotkey_label(self._settings['blackout_hotkey'])}"
        self._set_status(text + ".")

    def _apply_blackout_hotkey(self) -> None:
        """Give the optional hotkey to the hotkey listener; one it refuses (clash) is dropped."""
        binding = self._settings.get("blackout_hotkey")
        try:
            if not self._hotkeys.set_extra("blackout", binding):
                self._settings["blackout_hotkey"] = None
        except Exception:
            log.debug("Could not set the blackout hotkey", exc_info=True)

    def set_indicator(self, style: str) -> None:
        """Settings > Indicator: both / border / pill / off."""
        if style not in INDICATOR_LABELS:
            return
        self._settings["indicator"] = style
        self._save_settings()
        self._overlay_shown = None      # redraw in the new style if something is running
        self._refresh()

    def toggle_indicator_playback(self) -> None:
        self._settings["indicator_playback"] = not self._settings["indicator_playback"]
        self._save_settings()
        self._overlay_shown = None
        self._refresh()

    def set_close_action(self, action: str) -> None:
        if action == "tray" and not self._tray_ok:
            return
        self._settings["close_action"] = action
        self._save_settings()

    # ---- Thread hop ---------------------------------------------------------------------

    def _hop(self, fn) -> None:
        """Run fn on the Tk thread soon. Safe from any thread, never blocks, and does nothing once
        the window is gone (see the module docstring for why it doesn't call tkinter)."""
        if self._closed:
            return
        self._inbox.put(fn)

    def _drain_inbox(self) -> None:
        """Tk thread: run everything other threads handed over, then check again shortly.

        The next check is scheduled FIRST: a job below may open a box, which waits (inside
        this call) until it's answered. Scheduled afterwards, nothing handed over (hotkeys,
        F10, playback finishing) ran until the box closed; then all of it ran at once, e.g. a
        second Record press cancelled the recording the box had just let start."""
        self._inbox_job = None
        if not self._closed:
            try:
                self._inbox_job = self.after(_INBOX_MS, self._drain_inbox)
            except Exception:
                log.debug("Could not schedule the inbox", exc_info=True)
        while not self._closed:
            try:
                fn = self._inbox.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.exception("Error in work handed over from another thread")

    # ---- Hotkeys ------------------------------------------------------------------------

    def on_hotkey(self, action: str) -> None:
        """A hotkey (or the same entry in the tray / menus). See the module docstring."""
        if self._closed:
            return
        if self._boxes_open and action != "stop":
            # A box is waiting for an answer: show it again (it may have ended up behind
            # something) instead of starting anything. The panic stop still works.
            ui.bring_boxes_forward(self)
            return
        if action in ("record", "play"):
            now = self._clock()
            if now - self._last_press.get(action, -1e9) < self._double_press:
                return   # a double tap / bouncing key: the same press
            self._last_press[action] = now
        mods = set(self._settings["hotkeys"].get(action, {}).get("mods", []))
        if action == "record":
            if self._state == IDLE:
                self.start_recording()
            elif self._state == RECORDING:
                self.stop(trim_tail=False, drop_modifiers=mods)
        elif action == "play":
            if self._state == IDLE:
                self.start_playback()
            elif self._state == PLAYING:
                self.stop()
        elif action == "stop":
            self.stop(trim_tail=False, drop_modifiers=mods)
        elif action == "blackout":
            self.toggle_blackout()

    def _hotkey_guard(self) -> bool:
        """True from the moment Play is pressed (also while waiting for the player to start)."""
        return self._state == PLAYING

    # ---- Window helpers -----------------------------------------------------------------

    def _window_visible(self) -> bool:
        try:
            return self.state() not in ("withdrawn", "iconic")
        except Exception:
            return False

    def _hide(self) -> None:
        """Out of the way while recording / playing: gone from the taskbar and Alt+Tab when there
        is a tray icon to get it back from, minimized otherwise."""
        self._close_menu()
        try:
            if self._tray_ok:
                self.withdraw()
            else:
                self.iconify()
        except Exception:
            log.debug("Could not hide the window", exc_info=True)

    def _restore(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            log.debug("Could not restore the window", exc_info=True)

    def _finish_restore(self) -> None:
        """At the end of recording / playing: back on screen if it was open when it started."""
        if self._restore_after:
            self._restore()

    def show_window(self) -> None:
        """Tray: Show Wackro."""
        if not self._closed:
            self._restore()

    def hide_to_tray(self) -> None:
        """File > Hide to tray, or the close button. The hotkeys keep working."""
        self._close_menu()
        try:
            if self._tray_ok:
                self.withdraw()
            else:
                self.iconify()
        except Exception:
            log.debug("Could not hide the window", exc_info=True)

    def _cancel_pending(self) -> None:
        if self._pending is not None:
            try:
                self.after_cancel(self._pending)
            except Exception:
                pass
            self._pending = None

    def _set_status(self, text: str) -> None:
        self._status = text
        self._refresh()

    # ---- Record -------------------------------------------------------------------------

    def start_recording(self) -> None:
        """Button / hotkey. Ignored unless idle. With an unsaved recording it first asks
        Save changes? Yes / No / Cancel (and shows the window for that if it was hidden)."""
        if self._state != IDLE:
            return
        was_visible = self._window_visible()
        if self._has_unsaved():
            # Hidden in the tray: only the question pops up (on top, focused), not the window.
            if not self._settle_unsaved("Starting a new recording replaces it."):
                return
            if self._state != IDLE:     # something else started while the box was open
                return
        self._restore_after = was_visible
        self._state = RECORDING
        self._status = f"Recording… press {self._binding_label('record')} to stop."
        self._hide()
        self._pending = self.after(self._delay_ms, self._begin_recorder)
        self._refresh()

    def _begin_recorder(self) -> None:
        self._pending = None
        if self._state != RECORDING:
            return
        options = {name: self._settings[key] for name, key in _RECORD_OPTION_KEYS.items()}
        try:
            self._recorder.start(hotkeys=self._settings["hotkeys"], options=options)
        except (RecordingError, RuntimeError) as error:
            log.exception("Recording failed to start")
            self._state = IDLE
            self._restore()
            self._set_status(str(error))

    def _on_auto_stop(self, reason: str) -> None:  # worker thread
        self._hop(self._handle_auto_stop)

    def _handle_auto_stop(self) -> None:
        if self._state != RECORDING or self._pending is not None:
            return
        message = self._recorder.stop_message
        self._finish_recording(self._recorder.stop(), message)

    def _finish_recording(self, events: list, extra: str = "") -> None:
        self._events = events
        self._mode = "recording"
        self._path = None
        self._dirty = bool(events)
        self._state = IDLE
        self._finish_restore()
        text = f"Recorded {plural(len(events), 'event')} ({format_duration(duration_of(events))})."
        skipped = getattr(self._recorder, "skipped_keys", 0) + getattr(self._recorder, "skipped_buttons", 0)
        if skipped:
            text += f" {plural(skipped, 'input')} couldn't be recorded."
        if extra:
            text = f"{extra} {text}"
        self._set_status(text)

    # ---- Play ---------------------------------------------------------------------------

    def start_playback(self) -> None:
        """Button / hotkey. Ignored unless idle."""
        if self._state != IDLE:
            return
        if not self._have_playable():
            self._set_status("There's nothing to play yet. Record something or open a recording first.")
            return
        if not self._preflight():
            return
        if self._state != IDLE:     # something else started while the box was open
            return
        s = self._settings
        if self._mode == "sequence":
            repeat = self._sequence["rounds"]
            self._play_options = {"repeat": repeat, "delay": 0.0, "speed": s["speed"]}
            status = f"Playing {self._sequence['name']}… press {self._stop_keys()} to stop."
        else:
            repeat = 0 if s["infinite"] else s["repeat"]
            self._play_options = {"repeat": repeat, "delay": s["delay"], "speed": s["speed"]}
            status = playing_status(repeat, self._stop_keys())
        self._restore_after = self._window_visible()
        self._state = PLAYING
        self._status = status
        self._hide()
        self._pending = self.after(self._delay_ms, self._begin_player)
        self._refresh()

    def _preflight(self) -> bool:
        """Settings > Safety: before playing, point out programs the recording used that aren't
        open now. True = go ahead."""
        if not self._settings["fs_preflight"]:
            return True
        try:
            events = seq.programs_used(self._sequence) if self._mode == "sequence" else self._events
            missing = failsafe.missing_programs(events, self._win32().running_programs())
        except Exception:
            log.warning("Pre-flight check failed; playing anyway", exc_info=True)
            return True
        if not missing:
            return True
        names = ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
        verb = "isn't" if len(missing) == 1 else "aren't"
        if self._dialogs.confirm("Program not open",
                                 f"This recording used {names}, which {verb} open right now. "
                                 "Clicks and keys meant for it would land somewhere else.\n\nPlay anyway?"):
            return True
        self._set_status(f"Not played: open {names} first.")
        return False

    def _start_watch(self) -> Optional[object]:
        """Start the take-over failsafe; returns the on_cursor callback for the player (or None)."""
        options = failsafe.options_from(self._settings)
        bindings = dict(self._settings["hotkeys"], blackout=self._settings.get("blackout_hotkey"))
        vks, mod_vks = failsafe.hotkey_vks(bindings, ("play", "stop", "blackout"))
        try:
            if self._watch.start(options, hotkey_vks=vks, track=bool(self._settings["blackout"]),
                                 on_wake=self._on_wake_hook, mod_vks=mod_vks):
                return self._watch.note_cursor
        except Exception:
            log.warning("The take-over failsafe could not start", exc_info=True)
        return None

    def _stop_watch(self) -> None:
        try:
            self._watch.stop()
        except Exception:
            log.debug("Could not stop the take-over failsafe", exc_info=True)

    def _begin_player(self) -> None:
        self._pending = None
        if self._state != PLAYING:
            return
        options = failsafe.options_from(self._settings)
        guard = failsafe.WindowGuard(self._win32(), options)
        try:
            events, next_round = self._playback_events()
        except RecordingError as error:
            self._state = IDLE
            self._restore()
            self._set_status(str(error))
            return
        on_cursor = self._start_watch()
        self._begin_playback_extras()
        try:
            self._player.start(events, **self._play_options,
                               guard=guard if guard.wanted else None, on_cursor=on_cursor,
                               max_lag=failsafe.LAG_LIMIT if options["fs_sleep"] else None,
                               **({"next_round": next_round} if next_round else {}))
        except (RecordingError, RuntimeError) as error:
            log.exception("Playback failed to start")
            self._end_playback_extras()
            self._state = IDLE
            self._restore()
            self._set_status(str(error))

    def _playback_events(self) -> tuple:
        """(first run's events, next_round or None) for what Play plays."""
        if self._mode != "sequence":
            self._runner = None
            return self._events, None
        self._runner = seq.Runner(self._sequence)
        return self._runner.round(1), self._runner.round

    def _current_step(self) -> Optional[tuple]:
        """(number, count, name) of the sequence step playing now, or None."""
        if self._runner is None:
            return None
        try:
            return self._runner.step_at(max(1, self._player.current_run), self._player.run_position)
        except Exception:
            return None

    def _step_text(self) -> str:
        step = self._current_step()
        if step is None:
            return ""
        number, count, name = step
        return f"{number}/{count} {name}"

    # ---- While playing: blackout, keep awake, time limit, screen changes ---------------

    def _begin_playback_extras(self) -> None:
        self._play_began = self._clock()
        self._last_wake = -1e9
        self._blacked = False
        self._screen_sig = self._monitor_signature()
        self._next_screen_check = self._play_began + 1.0
        if self._settings["keep_awake"]:
            try:
                self._win32().keep_awake(True)
            except Exception:
                log.debug("Could not switch keep-awake on", exc_info=True)
            self._awake = True

    def _end_playback_extras(self) -> None:
        """Undo everything playback switched on. Safe to call more than once."""
        self._stop_watch()
        self._hide_blackout()
        if self._awake:
            self._awake = False
            try:
                self._win32().keep_awake(False)
            except Exception:
                log.debug("Could not switch keep-awake off", exc_info=True)

    def _monitor_signature(self):
        try:
            return tuple(sorted(tuple(m) for m in self._win32().monitors()))
        except Exception:
            return None

    def _watch_playback(self) -> None:
        """Every tick while playing: time limit, screen changes, and when to black out."""
        if self._state != PLAYING or self._pending is not None or not self._player.is_playing:
            return
        s = self._settings
        if s["time_limit_on"] and self._player.elapsed >= s["time_limit"] * 60:
            self._failsafe_stop(f"it reached the time limit of {ui.format_minutes(s['time_limit'])} min")
            return
        now = self._clock()
        if s["fs_screen"] and now >= self._next_screen_check:
            self._next_screen_check = now + 1.0
            signature = self._monitor_signature()
            if signature is not None and self._screen_sig is not None and signature != self._screen_sig:
                self._failsafe_stop("the screen resolution or the monitors changed")
                return
        if self._blacked:
            self._blackout_text_update()
        elif s["blackout"] and getattr(self._watch, "active", False):
            idle_since = max(self._play_began, self._last_wake, getattr(self._watch, "last_activity", -1e9))
            if now - idle_since >= s["blackout_after"] * 60:
                self.blackout_now()

    def _blackout_text(self) -> str:
        if not self._settings["blackout_text"]:
            return ""
        return pill_text("playing", self._player.elapsed, run=self._player.current_run,
                         runs=self._player.total_runs, step=self._step_text())

    def _blackout_text_update(self) -> None:
        try:
            self._blackout.set_text(self._blackout_text())
        except Exception:
            log.debug("Could not update the blackout text", exc_info=True)

    def blackout_now(self) -> None:
        """Black out (only while really playing, and only when a touch can wake it again)."""
        if self._blacked or self._state != PLAYING or self._pending is not None:
            return
        if not getattr(self._watch, "active", False):
            return      # nothing would notice you touching the PC: never cover the screen then
        self._blacked = True
        try:
            self._blackout.show(self._blackout_text())
        except Exception:
            log.warning("Could not show the blackout", exc_info=True)
        self._watch.set_blacked_out(True)
        if self._settings["blackout_dim"]:
            self._dimmer.dim()
        self._update_overlay()

    def _hide_blackout(self) -> None:
        if not self._blacked:
            return
        self._blacked = False
        try:
            self._blackout.hide()
        except Exception:
            log.debug("Could not hide the blackout", exc_info=True)
        try:
            self._watch.set_blacked_out(False)
        except Exception:
            pass
        self._dimmer.restore()

    def wake_blackout(self) -> None:
        if self._blacked:
            self._last_wake = self._clock()
            self._hide_blackout()
            self._refresh()

    def toggle_blackout(self) -> None:
        """The optional "black out now" hotkey: black out, or wake up again."""
        if self._blacked:
            self.wake_blackout()
        else:
            self.blackout_now()

    def _on_wake_hook(self) -> None:   # a listener thread (inside the Windows hook): never waits
        self._hop(self.wake_blackout)

    def _on_failsafe_trip(self, reason: str) -> None:  # a listener thread (inside a Windows hook)
        """You took over: halt the player right here (never waits), tidy up on the Tk thread."""
        try:
            halt = getattr(self._player, "halt", None)
            if halt is not None:
                halt(reason)
        except Exception:
            log.exception("Could not halt playback")
        self._hop(lambda: self._failsafe_stop(reason))

    def _failsafe_stop(self, reason: str) -> None:
        if self._state != PLAYING:
            return
        if self._pending is not None:
            self.stop()
            return
        self._player.stop(reason)
        if not self._player.is_playing:
            self._handle_play_finished()

    def _on_play_finished(self, reason: str) -> None:  # worker thread
        self._hop(self._handle_play_finished)

    def _handle_play_finished(self) -> None:
        # is_playing is already False when on_finished fires; if it's True now, this is a stale
        # notification from an earlier playback and a newer one is running.
        if self._state != PLAYING or self._pending is not None or self._player.is_playing:
            return
        self._end_playback_extras()
        self._state = IDLE
        self._finish_restore()
        message = self._player.finish_message
        step = self._current_step()
        stop_text = getattr(self._player, "stop_text", None)
        if step is not None and stop_text:
            message = f'Playback stopped in step "{step[2]}": {stop_text}'
        self._runner = None
        self._set_status(message)
        if getattr(self._player, "stop_text", None):
            try:
                self._tray.notify(message)
            except Exception:
                log.debug("Could not show the failsafe notification", exc_info=True)

    # ---- Stop ---------------------------------------------------------------------------

    def stop(self, trim_tail: bool = False, drop_modifiers=()) -> None:
        """Stop button (trim_tail=True) / hotkeys. Ignored when idle."""
        if self._state == IDLE:
            return
        if self._pending is not None:      # Record / Play hadn't actually started yet
            self._cancel_pending()
            self._state = IDLE
            self._finish_restore()
            self._set_status("Cancelled.")
            return
        if self._state == RECORDING:
            self._finish_recording(self._recorder.stop(trim_tail=trim_tail, drop_modifiers=set(drop_modifiers)))
        elif self._state == PLAYING:
            self._player.stop()            # on_finished then brings the window back
            if not self._player.is_playing:
                self._handle_play_finished()

    # ---- File ---------------------------------------------------------------------------

    def _has_unsaved(self) -> bool:
        return self._dirty and bool(self._events)

    def _have_playable(self) -> bool:
        if self._mode == "sequence":
            return self._sequence is not None
        return bool(self._events)

    def set_mode(self, mode: str) -> None:
        """Sequence menu: what Play plays (and Save saves)."""
        if self._state != IDLE:
            return
        if mode == "sequence" and self._sequence is not None:
            self._mode = "sequence"
            self._set_status(f"Play: sequence {self._sequence['name']}.")
        elif mode == "recording" and self._events:
            self._mode = "recording"
            self._set_status("Play: the current recording.")

    def _has_unsaved_sequence(self) -> bool:
        return self._seq_dirty and self._sequence is not None

    def _settle_unsaved_sequence(self, detail: str = "") -> bool:
        if not self._has_unsaved_sequence():
            return True
        choice = self._dialogs.ask_save_changes(detail, what="sequence")
        if choice == "no":
            return True
        if choice == "yes":
            return self._save_sequence(as_new=self._seq_path is None, force=True)
        return False

    def new_sequence(self) -> None:
        """Sequence > New sequence…"""
        if self._state != IDLE:
            return
        if not self._settle_unsaved_sequence("A new sequence replaces it."):
            return
        self._edit(seq.new_sequence(), new=True)

    def edit_sequence(self) -> None:
        """Sequence > Edit sequence…"""
        if self._state != IDLE or self._sequence is None:
            return
        self._edit(self._sequence, new=False)

    def _edit(self, sequence: dict, new: bool) -> None:
        current = None
        if self._events:
            name = seq.name_from_path(self._path) if self._path else "Recording"
            try:
                current = (name, make_recording(self._events, self.winfo_screenwidth(), self.winfo_screenheight()))
            except RecordingError:
                current = None
        result = self._dialogs.ask_sequence(sequence, current)
        if result is None:
            return
        if new:
            self._seq_path = None
            self._seq_dirty = True
        elif result != self._sequence:
            self._seq_dirty = True
        self._sequence = result
        self._mode = "sequence"
        self._set_status(f"Sequence {result['name']}: {seq.summary(result)}. Press Play.")

    def _save_sequence(self, as_new: bool, force: bool = False) -> bool:
        if self._sequence is None or (self._state != IDLE and not (force and self._state == PLAYING)):
            return False
        path = self._seq_path
        if as_new or path is None:
            if sequence_has_keys(self._sequence) and not self._dialogs.confirm(
                    "Keystrokes in this sequence", _SAVE_KEYS_WARNING):
                return False
            path = self._dialogs.save_sequence_path()
            if not path:
                return False
        try:
            seq.save_sequence(path, self._sequence)
        except RecordingError as error:
            self._dialogs.error("Couldn't save", str(error))
            self._set_status(str(error))
            return False
        self._seq_path = path
        self._seq_dirty = False
        self._set_status(f"Saved to {Path(path).name}.")
        return True

    def _settle_unsaved(self, detail: str = "") -> bool:
        """Before something replaces an unsaved recording: Save changes? Yes / No / Cancel.
        True = go ahead (it was saved, or the user said No). False = stop (Cancel, or the save
        was cancelled / failed, so nothing is lost)."""
        if not self._has_unsaved():
            return True
        choice = self._dialogs.ask_save_changes(detail)
        if choice == "no":
            return True
        if choice == "yes":
            return self.save_quick(force=True)
        return False

    def new_recording(self) -> None:
        if self._state != IDLE:
            return
        if not self._settle_unsaved("File > New clears it."):
            return
        self._events, self._path, self._dirty = [], None, False
        if self._mode == "recording" and self._sequence is not None:
            self._mode = "sequence"
        self._set_status("Ready.")

    def save(self, force: bool = False) -> bool:
        """File > Save As…  Returns True if it was saved. force: also while playing (quitting)."""
        if self._mode == "sequence":
            return self._save_sequence(as_new=True, force=force)
        if (self._state != IDLE and not (force and self._state == PLAYING)) or not self._events:
            return False
        if has_key_events(self._events) and not self._dialogs.confirm("Keystrokes in this recording", _SAVE_KEYS_WARNING):
            return False
        path = self._dialogs.save_path()
        if not path:
            return False
        return self._write(path)

    def save_quick(self, force: bool = False) -> bool:
        """File > Save: straight to the file it came from, or Save As… the first time."""
        if self._mode == "sequence":
            return self._save_sequence(as_new=False, force=force)
        if (self._state != IDLE and not (force and self._state == PLAYING)) or not self._events:
            return False
        if self._path is None:
            return self.save(force=force)
        return self._write(self._path)

    def _write(self, path: str) -> bool:
        try:
            recording = make_recording(self._events, self.winfo_screenwidth(), self.winfo_screenheight())
            save_recording(path, recording)
        except RecordingError as error:
            self._dialogs.error("Couldn't save", str(error))
            self._set_status(str(error))
            return False
        self._path = path
        self._dirty = False
        self._set_status(f"Saved to {Path(path).name}.")
        return True

    def load(self) -> None:
        """File > Open… (a recording or a sequence)"""
        if self._state != IDLE:
            return
        path = self._dialogs.open_path()
        if not path:
            return
        try:
            kind, data = seq.load_any(path)
        except RecordingError as error:
            self._dialogs.error("Couldn't open", str(error))
            self._set_status(str(error))
            return
        if kind == "sequence":
            if not self._settle_unsaved_sequence("Opening another one replaces it."):
                return
            self._sequence, self._seq_path, self._seq_dirty = data, path, False
            self._mode = "sequence"
            text = f"Loaded sequence {data['name']} ({seq.summary(data)})."
            here = (self.winfo_screenwidth(), self.winfo_screenheight())
            others = {(s["recording"]["screen"]["width"], s["recording"]["screen"]["height"]) for s in data["steps"]}
            if others - {here}:
                text += " Some steps were recorded on a different screen size: clicks may land in the wrong place."
            self._set_status(text)
            return
        if not self._settle_unsaved("Opening another one replaces it."):
            return
        recording = data
        self._mode = "recording"
        self._events = recording["events"]
        self._path = path
        self._dirty = False
        text = f"Loaded {plural(len(self._events), 'event')} from {Path(path).name}."
        screen = recording["screen"]
        here = (self.winfo_screenwidth(), self.winfo_screenheight())
        if (screen["width"], screen["height"]) != here:
            text += (
                f" Recorded on a {screen['width']}×{screen['height']} screen, this one is "
                f"{here[0]}×{here[1]}: clicks may land in the wrong place."
            )
        self._set_status(text)

    # ---- Help ---------------------------------------------------------------------------

    def show_about(self) -> None:
        keys = "\n".join(f"{ACTION_LABELS[a]}: {self._binding_label(a)}" for a in ACTIONS)
        text = (f"Version {APP_VERSION}\n{TAGLINE}\n{CREDIT}\n\n{keys}\n\n{PRIVACY_NOTICE} "
                "Don't record while typing passwords, and don't share such files.")
        self._dialogs.info(f"About {APP_NAME}", APP_NAME, text)

    def open_log_folder(self) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(LOG_DIR))  # noqa: S606 (our own folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(LOG_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(LOG_DIR)])
        except Exception:
            log.warning("Could not open the log folder", exc_info=True)
            self._set_status(f"The log folder is {LOG_DIR}")

    # ---- Refresh ------------------------------------------------------------------------

    def _refresh(self) -> None:
        """Buttons and labels from the current state. Cheap; called often."""
        if self._closed:
            return
        idle = self._state == IDLE
        have = self._have_playable()
        wanted = (
            (self._record_btn, idle, _RECORD_COLOR),
            (self._play_btn, idle and have, _PLAY_COLOR),
            (self._stop_btn, not idle, _STOP_COLOR),
        )
        for button, enabled, color in wanted:
            state = "normal" if enabled else "disabled"
            if button.cget("state") != state:      # only touch it when it changes (no flicker)
                button.configure(state=state, fg_color=color if enabled else _DISABLED_COLOR)
        self._show_text(self._status_label, self._status)
        self._show_text(self._info_label, self._info_text())
        if self._mode == "sequence" and self._sequence is not None:
            summary = f"{seq.summary(self._sequence)} · {format_speed(self._settings['speed'])} speed"
        else:
            summary = playback_summary(self._settings)
        extra = recording_summary(self._settings)
        self._show_text(self._summary_label, summary + (f"\n{extra}" if extra else ""))
        if self._tray_state != self._state:
            self._tray_state = self._state
            try:
                self._tray.set_state(self._state)
            except Exception:
                log.debug("Could not update the tray icon", exc_info=True)
        self._update_overlay()

    def _show_text(self, widget, text: str) -> None:
        """Change a label only when its text really changed (redrawing costs on Windows)."""
        if self._shown_texts.get(widget) != text:
            self._shown_texts[widget] = text
            widget.configure(text=text)

    def _overlay_text(self) -> str:
        pending = self._pending is not None
        if self._state == RECORDING:
            return pill_text("recording", 0 if pending else self._recorder.elapsed, pending=pending)
        if pending:
            return pill_text("playing", 0, pending=True)
        return pill_text("playing", self._player.elapsed, run=self._player.current_run,
                         runs=self._player.total_runs, step=self._step_text())

    def _update_overlay(self) -> None:
        """Show / hide / update the on-screen sign for the current state and settings."""
        if getattr(self, "_overlay", None) is None:
            return                      # still starting up
        style = self._settings["indicator"]
        wanted = None
        if self._state == RECORDING:
            wanted = ("recording", style)
        elif self._state == PLAYING and self._settings["indicator_playback"]:
            wanted = ("playing", style)
        if wanted is not None and (style == "off" or self._blacked):
            wanted = None
        try:
            if wanted != self._overlay_shown:
                self._overlay_shown = wanted
                if wanted is None:
                    self._overlay.hide()
                else:
                    self._overlay.show(wanted[0], self._overlay_text(), style=wanted[1])
            elif wanted is not None:
                self._overlay.set_text(self._overlay_text())
        except Exception:
            log.debug("Could not update the recording indicator", exc_info=True)

    def _info_text(self) -> str:
        if self._state == RECORDING:
            if self._pending is not None:
                return "Starting…"
            return f"● {plural(self._recorder.event_count, 'event')}   {format_duration(self._recorder.elapsed)}"
        if self._state == PLAYING:
            if self._pending is not None:
                return "Starting…"
            done, total = self._player.progress
            runs = self._player.total_runs
            run_text = ""
            step = self._step_text()
            if runs != 1:
                of = "∞" if runs == 0 else f"{runs:,}"
                run_text = f"{'Round' if step else 'Run'} {max(1, self._player.current_run):,} / {of} · "
            if step:
                return f"▶ {run_text}Step {step}   {format_duration(self._player.elapsed)}"
            return f"▶ {run_text}{done:,} / {total:,} events   {format_duration(self._player.elapsed)}"
        if self._mode == "sequence" and self._sequence is not None:
            text = f"Sequence: {self._sequence['name']}"
            if self._seq_path:
                text += f"  ·  {Path(self._seq_path).name}"
            return text + ("  (not saved)" if self._seq_dirty else "")
        if not self._events:
            return "No recording yet."
        text = f"{plural(len(self._events), 'event')}, {format_duration(duration_of(self._events))}"
        if self._path:
            text += f"  ·  {Path(self._path).name}"
        return text + ("  (not saved)" if self._dirty else "")

    def _tick(self) -> None:
        if self._closed:
            return
        self._refresh()
        self._watch_menu()
        try:
            self._watch_playback()
        except Exception:
            log.exception("Playback watch failed")
        now = self._clock()
        if now >= self._next_sweep:
            self._next_sweep = now + _SWEEP_SECONDS
            try:
                ui.sweep_dead_windows()
            except Exception:
                log.debug("Could not sweep closed windows", exc_info=True)
        busy = self._state != IDLE or self._menu is not None
        self._tick_job = self.after(_TICK_MS if busy else _IDLE_TICK_MS, self._tick)

    # ---- Closing ------------------------------------------------------------------------

    def close(self) -> None:
        """The window's X: ask (Exit / Hide to tray / Cancel), or do what was chosen before."""
        if self._closed:
            return
        action = self._settings["close_action"]
        if action == "tray" and not self._tray_ok:
            action = "ask"
        if action == "ask":
            choice, remember = self._dialogs.close_choice(tray=self._tray_ok)
            if choice not in ("exit", "tray"):
                return
            if remember:
                self._settings["close_action"] = choice
                self._save_settings()
            action = choice
        if action == "tray":
            self.hide_to_tray()
            return
        self.exit_app()

    def exit_app(self) -> None:
        """Really quit (File > Exit, tray > Exit, or the X set to exit). Asks first if that would
        throw away a recording."""
        if self._closed:
            return
        if self._boxes_open:
            ui.bring_boxes_forward(self)   # answer the open box first (no second box on top)
            return
        if self._state == RECORDING:
            if not self._dialogs.confirm("Quit?", "A recording is still running and will be lost. Quit anyway?"):
                return
        elif self._has_unsaved():
            if not self._settle_unsaved(f"Quitting {APP_NAME} throws it away otherwise."):
                return
        if self._state != RECORDING and self._has_unsaved_sequence():
            if not self._settle_unsaved_sequence(f"Quitting {APP_NAME} throws it away otherwise."):
                return
        self._save_settings()
        self._closed = True
        self._close_menu()
        self._cancel_pending()
        for job in (self._tick_job, self._inbox_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._tick_job = self._inbox_job = None
        self._hotkeys.stop()
        self._end_playback_extras()
        try:
            self._blackout.destroy()
        except Exception:
            log.debug("Could not remove the blackout", exc_info=True)
        try:
            self._dimmer.close(3.0)
        except Exception:
            log.debug("Could not restore the brightness", exc_info=True)
        try:
            self._tray.stop()
        except Exception:
            log.debug("Could not remove the tray icon", exc_info=True)
        try:
            self._overlay.destroy()
        except Exception:
            log.debug("Could not remove the recording indicator", exc_info=True)
        try:
            self.tk.eval("proc bgerror {args} {}")  # CTk's own timers may fire once more while we close
            if self._state == RECORDING:
                self._recorder.stop()
            elif self._state == PLAYING:
                self._player.stop()
        except Exception:
            log.exception("Error while shutting down")
        self.destroy()
