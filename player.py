"""Plays a recording back by driving the real mouse and keyboard.

    player = Player(on_finished=handle_finished)
    player.start(events)      # returns at once; playback runs on a worker thread
    player.start(events, repeat=5, delay=1.0)   # five runs, one second apart
    player.start(events, repeat=0)              # run again and again until stop()
    ...                       # player.is_playing / progress / elapsed are safe to poll
    player.stop()             # instant; anything still held down is released

How it behaves:

* Every event fires at an absolute moment (start + its "t"), never "sleep(gap) after the last
  event", so slow events can't make the whole playback drift. If playback falls behind, it
  catches up by firing the late events straight away.
* Stop is instant: the worker sleeps on a threading.Event, which stop() sets. That includes the
  pause between runs.
* With repeat != 1 the recording is played several times (repeat=0 means until stop()). Every
  run starts its own absolute clock, so runs never drift into each other. Keys and buttons held
  at the end of a run are released before the next one starts. `delay` seconds are waited between
  runs (after the last event of a run), and a run never takes less than MIN_PASS_SECONDS in
  total, so a zero-length recording on endless repeat can't spin the CPU. `speed` scales every
  event time (2.0 = twice as fast).
* Keys and mouse buttons still held down when playback ends, is stopped or fails are released,
  so Stop never leaves Shift or a mouse button stuck.
* Events that can't be played here are skipped and counted (skipped_events): keys or buttons
  this system doesn't have. An event that fails while playing is counted in failed_events and
  playback goes on. 25 failures in a row abort it.
* A recording may contain keys that are also hotkeys (from a hand-edited file, or one recorded
  before the hotkeys were changed). They are played like any other key: the hotkey listener
  ignores keys sent by a program (hotkeys.py), so they can't stop or restart playback.
* Everything is resolved and validated BEFORE the worker starts, so mistakes show up as a
  RecordingError from start() with a GUI-ready message, not as a crash on a background thread.

Threading: on_finished(reason) is called from the worker thread, after is_playing has become
False. reason is "done", "stopped" or "error". The GUI must hop to its own thread
(self.after(0, ...)) before touching widgets. is_playing is also what hotkeys.py uses as the
"playing" guard. start(), stop() and the read-only properties are meant for the GUI thread.

pynput is imported lazily (in start), so importing this module never needs a display.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Optional

from config import (
    MAX_REPEAT,
    MAX_REPEAT_DELAY,
    MAX_SPEED,
    MIN_PASS_SECONDS,
    MIN_SPEED,
    PLAYBACK_START_DELAY,
    RECORDING_VERSION,
)
from recorder import key_identity
from storage import RecordingError, key_from_json, validate_recording

log = logging.getLogger(__name__)

_MAX_CONSECUTIVE_FAILURES = 25
_JOIN_TIMEOUT = 2.0


def _wait_on_event(event: threading.Event, seconds: float) -> bool:
    """Sleep up to `seconds`, waking early if `event` is set. Returns True if it was set."""
    return event.wait(seconds)


class Player:
    """Plays events back. See the module docstring."""

    def __init__(
        self,
        on_finished: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.perf_counter,
        waiter: Callable[[threading.Event, float], bool] = _wait_on_event,
        mouse_controller=None,
        keyboard_controller=None,
    ):
        """on_finished(reason) is called from the worker thread when playback ends. clock, waiter
        and the two controllers exist so tests can fake time and input."""
        self._on_finished = on_finished
        self._clock = clock
        self._waiter = waiter
        self._mouse_controller = mouse_controller
        self._keyboard_controller = keyboard_controller

        self._playing = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._mouse = None
        self._keyboard = None
        self._held_keys: dict = {}
        self._held_buttons: dict = {}
        self._t0: Optional[float] = None        # when the first run started
        self._pass_t0: Optional[float] = None   # when the current run started
        self._t_end: Optional[float] = None
        self._total = 0
        self._done = 0                           # events played in the current run
        self._runs_total = 1                     # 0 = until stopped
        self._run_index = 0                      # 1-based number of the current run (0 = not started)
        self._runs_done = 0                      # runs that played to the end
        self._delay = 0.0
        self._speed = 1.0
        self._consecutive_failures = 0
        self._reason: Optional[str] = None
        self._guard = None
        self._on_cursor = None
        self._max_lag = None
        self._next_round = None
        self._mouse_module = None
        self._last_action_t = None
        self.stop_text: Optional[str] = None   # why playback was stopped (failsafe), if it says
        self.skipped_events = 0   # events that can't be played on this system (set by start)
        self.failed_events = 0    # events that raised while playing

    # ---- Read-only state for the GUI ----

    @property
    def is_playing(self) -> bool:
        """True from start() until the worker has finished (and released everything)."""
        return self._playing

    @property
    def progress(self) -> tuple:
        """(events played so far in the current run, events per run)."""
        return (self._done, self._total)

    @property
    def total_runs(self) -> int:
        """How many runs were asked for; 0 means "until stopped"."""
        return self._runs_total

    @property
    def current_run(self) -> int:
        """1-based number of the run in progress (0 until the first run begins)."""
        return self._run_index

    @property
    def completed_runs(self) -> int:
        """Runs that played all the way to the end."""
        return self._runs_done

    @property
    def elapsed(self) -> float:
        """Seconds since the first run began (0 during the start delay), counting the pauses between
        runs; frozen once playback ends."""
        if self._t0 is None:
            return 0.0
        end = self._t_end if self._t_end is not None else self._clock()
        return max(0.0, end - self._t0)

    @property
    def run_position(self) -> float:
        """Seconds into the current run, in recording time (speed taken out). Used to tell which
        step of a sequence is playing. Once playback has ended it's the time of the last event it
        went for (the one a failsafe stopped, say), not the clock: a check that waited a moment
        before stopping must not shift the blame to a later step."""
        if not self._playing and self._last_action_t is not None:
            return self._last_action_t
        if self._pass_t0 is None:
            return 0.0
        end = self._t_end if self._t_end is not None else self._clock()
        return max(0.0, (end - self._pass_t0) * self._speed)

    @property
    def finish_reason(self) -> Optional[str]:
        """None while playing, else "done", "stopped" or "error"."""
        return self._reason

    @property
    def finish_message(self) -> str:
        """A GUI-ready sentence about how the last playback ended ("" before the first one)."""
        if self._reason == "error":
            return "Playback stopped because something went wrong. Check the log file."
        if self._reason == "stopped":
            if self.stop_text:
                return f"Playback stopped: {self.stop_text}"
            if self._runs_total == 1 or self._run_index < 1:
                return "Playback stopped."
            if self._runs_total == 0:
                return f"Playback stopped during run {self._run_index:,}."
            return f"Playback stopped during run {self._run_index:,} of {self._runs_total:,}."
        if self._reason == "done":
            runs = "" if self._runs_total == 1 else f" ({self._runs_done:,} runs)"
            problems = self.skipped_events + self.failed_events
            if problems:
                noun = "event" if problems == 1 else "events"
                return f"Playback finished{runs}, but {problems} {noun} couldn't be played."
            return f"Playback finished{runs}."
        return ""

    # ---- Start / stop ----

    def start(self, events: list, repeat: int = 1, delay: float = 0.0, speed: float = 1.0,
              guard=None, on_cursor=None, max_lag: Optional[float] = None, next_round=None) -> None:
        """Begin playing `events` (a list in the storage format). Returns at once.

        repeat: number of runs, 1 to MAX_REPEAT, or 0 to keep going until stop().
        delay:  seconds to wait between runs (0 to MAX_REPEAT_DELAY). Ignored for a single run.
        speed:  playback speed factor, MIN_SPEED to MAX_SPEED (2.0 = twice as fast).
        guard:  optional failsafe check, called on the worker thread right before every click,
                scroll and key press as guard(operation, x, y, context, stop_event) with the
                recorded window context (or None). Returning a sentence stops playback with it.
        on_cursor: optional on_cursor(x, y), called just before the player moves the mouse, so
                the failsafe can tell the player's own moves from the user's.
        next_round: optional next_round(run_number) -> events, called (on the worker thread) before
                every run after the first, so each run can be different (sequences: a new shuffle,
                new random waits). Raising RecordingError stops playback with its message.
        max_lag: optional seconds. If playback ever falls this far behind schedule (the PC slept,
                or froze), it stops instead of firing everything it missed at once.

        Raises RecordingError if there's nothing to play, an argument is out of range, the events
        are invalid, or input can't be controlled. Raises RuntimeError if already playing.
        """
        if self._playing:
            raise RuntimeError("Already playing.")
        if not isinstance(events, list) or not events:
            raise RecordingError("There's nothing to play yet. Record something or load a recording first.")
        # Same checks as loading a file, so a bad event can't crash the worker thread later.
        validate_recording({"version": RECORDING_VERSION, "screen": {"width": 1, "height": 1}, "events": events})
        self._check_options(repeat, delay, speed)

        try:
            from pynput import keyboard, mouse

            self._mouse = self._mouse_controller if self._mouse_controller is not None else mouse.Controller()
            self._keyboard = (
                self._keyboard_controller if self._keyboard_controller is not None else keyboard.Controller()
            )
        except Exception as error:
            log.exception("Could not create the input controllers")
            raise RecordingError(f"Couldn't take control of the mouse and keyboard: {error}") from error

        self._mouse_module = mouse
        actions, skipped = self._resolve(events, mouse)
        if not actions:
            raise RecordingError("None of the events in this recording can be played on this system.")

        self.skipped_events = skipped
        self.failed_events = 0
        self._held_keys = {}
        self._held_buttons = {}
        self._t0 = None
        self._pass_t0 = None
        self._t_end = None
        self._total = len(actions)
        self._done = 0
        self._runs_total = repeat
        self._run_index = 0
        self._runs_done = 0
        self._delay = float(delay)
        self._speed = float(speed)
        self._guard = guard
        self._on_cursor = on_cursor
        self._max_lag = max_lag
        self._next_round = next_round
        self._last_action_t = None
        self.stop_text = None
        self._consecutive_failures = 0
        self._reason = None
        self._stop_event = threading.Event()
        self._playing = True  # set before the thread runs, so the hotkey guard is up immediately

        self._thread = threading.Thread(target=self._run, args=(actions,), name="Wackro-player", daemon=True)
        try:
            self._thread.start()
        except Exception as error:
            self._playing = False
            raise RecordingError(f"Couldn't start playback: {error}") from error

    def stop(self, reason: Optional[str] = None) -> None:
        """Stop playback now. Held keys and buttons are released. Does nothing if not playing.

        reason: why (a failsafe), shown in finish_message. Safe to call from on_finished too (it
        just doesn't wait for the worker in that case).
        """
        self.halt(reason)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(_JOIN_TIMEOUT)

    def halt(self, reason: Optional[str] = None) -> None:
        """Like stop() but never waits: safe from any thread, even inside a Windows input hook
        (the failsafe). The worker stops before its next event and on_finished follows."""
        if reason and self._playing and not self.stop_text:
            self.stop_text = reason
        self._stop_event.set()

    # ---- Internals: preparing the actions ----

    @staticmethod
    def _check_options(repeat, delay, speed) -> None:
        """Reject out-of-range repeat / delay / speed with a message the GUI can show as-is."""
        if isinstance(repeat, bool) or not isinstance(repeat, int) or not 0 <= repeat <= MAX_REPEAT:
            raise RecordingError(f"Repeat must be a whole number from 1 to {MAX_REPEAT:,} (0 means forever).")
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) \
                or not 0 <= delay <= MAX_REPEAT_DELAY:
            raise RecordingError(f"The delay between runs must be from 0 to {MAX_REPEAT_DELAY:,.0f} seconds.")
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) \
                or not MIN_SPEED <= speed <= MAX_SPEED:
            raise RecordingError(f"Speed must be from {MIN_SPEED:g}x to {MAX_SPEED:g}x.")

    @staticmethod
    def _resolve(events: list, mouse) -> tuple:
        """Turn events into (t, operation, a, b, c) tuples with pynput objects ready to use.

        Returns (actions, number_of_skipped_events).
        """
        actions = []
        skipped = 0
        for event in events:
            kind = event["type"]
            t = event["t"]
            if kind == "mouse_move":
                actions.append((t, "move", event["x"], event["y"], None))
            elif kind == "mouse_scroll":
                actions.append((t, "scroll", event["x"], event["y"], (event["dx"], event["dy"]), event.get("w")))
            elif kind == "mouse_click":
                button = getattr(mouse.Button, event["button"], None)  # x1 / x2 don't exist everywhere
                if button is None:
                    skipped += 1
                    continue
                actions.append((t, "click", event["x"], event["y"], (button, event["button"], event["pressed"]),
                                event.get("w")))
            else:  # key_press / key_release
                data = event["key"]
                try:
                    key = key_from_json(data)
                except RecordingError:
                    skipped += 1
                    continue
                operation = "press" if kind == "key_press" else "release"
                actions.append((t, operation, key, key_identity(data), None, event.get("w")))
        return actions, skipped

    # ---- Internals: the worker ----

    def _run(self, actions: list) -> None:
        reason = "done"
        try:
            if self._waiter(self._stop_event, PLAYBACK_START_DELAY):
                reason = "stopped"
            else:
                self._t0 = self._clock()
                reason = self._play_all(actions)
        except Exception:
            log.exception("Playback crashed")
            reason = "error"
        finally:
            self._release_everything()
            if self._t0 is not None:
                self._t_end = self._clock()
            self._reason = reason
            self._playing = False
        if self._on_finished is not None:
            try:
                self._on_finished(reason)
            except Exception:
                log.exception("on_finished callback failed")

    def _play_all(self, actions: list) -> str:
        """Every run, with the pauses between them. Returns "done", "stopped" or "error"."""
        run = 0
        while True:
            run += 1
            if run > 1 and self._next_round is not None:
                try:
                    events = self._next_round(run)
                    validate_recording({"version": RECORDING_VERSION, "screen": {"width": 1, "height": 1},
                                        "events": events})
                    actions, skipped = self._resolve(events, self._mouse_module)
                    if not actions:
                        raise RecordingError("nothing in this round can be played")
                except RecordingError as error:
                    self.stop_text = str(error).rstrip(".")
                    log.warning("Next round failed: %s", self.stop_text)
                    return "stopped"
                self.skipped_events += skipped
                self._total = len(actions)
            self._run_index = run
            self._done = 0
            self._pass_t0 = self._clock()
            result = self._play(actions)
            if result != "done":
                return result
            self._runs_done = run
            self._release_everything()   # nothing held down may leak into the next run
            if self._runs_total and run >= self._runs_total:
                return "done"
            pause = max(self._delay, MIN_PASS_SECONDS - (self._clock() - self._pass_t0))
            if pause > 0 and self._waiter(self._stop_event, pause):
                return "stopped"
            if self._stop_event.is_set():
                return "stopped"

    def _play(self, actions: list) -> str:
        """One run through `actions`, timed from self._pass_t0."""
        for index, action in enumerate(actions):
            remaining = action[0] / self._speed - (self._clock() - self._pass_t0)
            if remaining > 0 and self._waiter(self._stop_event, remaining):
                return "stopped"
            if self._stop_event.is_set():
                return "stopped"
            self._last_action_t = action[0]
            if self._max_lag is not None:
                late = (self._clock() - self._pass_t0) - action[0] / self._speed
                if late > self._max_lag:
                    self.stop_text = (f"playback fell {late:.0f} s behind schedule "
                                      "(the PC was asleep or froze)")
                    log.warning("Failsafe stopped playback: %s", self.stop_text)
                    return "stopped"
            if self._guard is not None and self._guarded(action):
                problem = self._check_guard(action)
                if problem:
                    log.warning("Failsafe stopped playback: %s", problem)
                    self.stop_text = problem
                    return "stopped"
                if self._stop_event.is_set():
                    return "stopped"
            try:
                self._perform(action)
                self._consecutive_failures = 0
            except Exception:
                self.failed_events += 1
                self._consecutive_failures += 1
                log.warning("Could not play event %d (%s)", index + 1, action[1], exc_info=True)
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    log.error("Giving up after %d failed events in a row", self._consecutive_failures)
                    return "error"
            self._done = index + 1
        return "done"

    @staticmethod
    def _guarded(action: tuple) -> bool:
        """Clicks (presses), scrolls and key presses are checked; moves and releases aren't
        (they can't do harm in the wrong window, and releases must always go out)."""
        operation = action[1]
        if operation == "click":
            return bool(action[4][2])
        return operation in ("scroll", "press")

    def _check_guard(self, action: tuple):
        operation = action[1]
        x, y = (action[2], action[3]) if operation in ("click", "scroll") else (None, None)
        context = action[5] if len(action) > 5 else None
        try:
            return self._guard(operation, x, y, context, self._stop_event)
        except Exception:
            log.warning("Failsafe check failed; carrying on", exc_info=True)
            return None

    def _perform(self, action: tuple) -> None:
        _, operation, a, b, c = action[:5]
        if operation in ("move", "scroll", "click") and self._on_cursor is not None:
            try:
                self._on_cursor(a, b)
            except Exception:
                log.debug("on_cursor failed", exc_info=True)
        if operation == "move":
            self._mouse.position = (a, b)
        elif operation == "scroll":
            self._mouse.position = (a, b)
            self._mouse.scroll(*c)
        elif operation == "click":
            button, name, pressed = c
            self._mouse.position = (a, b)
            if pressed:
                self._mouse.press(button)
                self._held_buttons[name] = button
            else:
                self._mouse.release(button)
                self._held_buttons.pop(name, None)
        elif operation == "press":
            self._keyboard.press(a)
            self._held_keys[b] = a
        else:  # release
            self._keyboard.release(a)
            self._held_keys.pop(b, None)

    def _release_everything(self) -> None:
        """Let go of whatever is still held down: mouse buttons first, then keys, last pressed first."""
        for name, button in list(self._held_buttons.items()):
            try:
                self._mouse.release(button)
            except Exception:
                log.warning("Could not release mouse button %s", name, exc_info=True)
        for identity, key in reversed(list(self._held_keys.items())):
            try:
                self._keyboard.release(key)
            except Exception:
                log.warning("Could not release key %s", identity, exc_info=True)
        self._held_buttons = {}
        self._held_keys = {}
