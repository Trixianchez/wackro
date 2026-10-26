"""Tests for app.py.

The real window runs (CustomTkinter), but the recorder, player, hotkeys and dialogs are fakes, so
no real input is read or sent. The whole file is skipped when tkinter / customtkinter or a
display isn't available (on Linux CI use `xvfb-run -a`). The window is briefly created and
destroyed once per test.

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import inspect
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("PYNPUT_BACKEND", "dummy")

try:
    import tkinter
    import customtkinter  # noqa: F401
    import app as app_module
    import ui_dialogs
    from app import (
        App,
        duration_of,
        format_duration,
        has_key_events,
        parse_delay,
        parse_repeat,
        parse_repeat_settings,
        playback_summary,
        playing_status,
        plural,
        recording_summary,
    )
    _IMPORT_ERROR = None
    _IMPORT_TRACEBACK = ""
except Exception as error:  # ImportError, or pynput refusing to load without a display
    import traceback as _traceback
    _IMPORT_ERROR = error
    _IMPORT_TRACEBACK = _traceback.format_exc()

import config  # noqa: E402
import hotkey_spec  # noqa: E402
import settings as settings_module  # noqa: E402
import storage  # noqa: E402
from storage import RecordingError  # noqa: E402

import logging  # noqa: E402

logging.getLogger("settings").addHandler(logging.NullHandler())   # tests feed it corrupt files on purpose
logging.getLogger("app").addHandler(logging.NullHandler())

MOUSE_EVENTS = [
    {"t": 0.0, "type": "mouse_move", "x": 10, "y": 10},
    {"t": 0.5, "type": "mouse_click", "x": 10, "y": 10, "button": "left", "pressed": True},
    {"t": 0.6, "type": "mouse_click", "x": 10, "y": 10, "button": "left", "pressed": False},
]
KEY_EVENTS = MOUSE_EVENTS + [
    {"t": 1.0, "type": "key_press", "key": {"kind": "char", "value": "a"}},
    {"t": 1.1, "type": "key_release", "key": {"kind": "char", "value": "a"}},
]


# ---- Fakes -------------------------------------------------------------------------------

class FakeRecorder:
    def __init__(self, on_auto_stop=None):
        self.on_auto_stop = on_auto_stop
        self.start_error = None
        self.events_to_return = list(MOUSE_EVENTS)
        self.start_calls = 0
        self.start_kwargs = []        # hotkeys / options of each start()
        self.stop_calls = []          # trim_tail value of each stop()
        self.stop_modifiers = []      # drop_modifiers of each stop()
        self.event_count = 7
        self.elapsed = 1.5
        self.stop_message = ""
        self.skipped_keys = 0
        self.skipped_buttons = 0

    def start(self, hotkeys=None, options=None):
        self.start_calls += 1
        self.start_kwargs.append({"hotkeys": hotkeys, "options": options})
        if self.start_error is not None:
            raise self.start_error

    def stop(self, trim_tail=False, drop_modifiers=()):
        self.stop_calls.append(trim_tail)
        self.stop_modifiers.append(set(drop_modifiers))
        return list(self.events_to_return)


class FakePlayer:
    def __init__(self, on_finished=None):
        self.on_finished = on_finished
        self.start_error = None
        self.started_with = []
        self.start_options = []
        self.stop_calls = 0
        self.is_playing = False
        self.progress = (3, 10)
        self.elapsed = 2.0
        self.total_runs = 1
        self.current_run = 1
        self.finish_message = "Playback finished."
        self.stop_text = None
        self.halted = None
        self.guard = self.on_cursor = None
        self.next_round = None
        self.run_position = 0.0

    def start(self, events, repeat=1, delay=0.0, speed=1.0, guard=None, on_cursor=None, max_lag=None,
              next_round=None):
        if self.start_error is not None:
            raise self.start_error
        self.started_with.append(events)
        self.start_options.append({"repeat": repeat, "delay": delay, "speed": speed})
        self.guard, self.on_cursor, self.max_lag = guard, on_cursor, max_lag
        self.next_round = next_round
        self.total_runs = repeat
        self.stop_text = None
        self.is_playing = True

    def halt(self, reason=None):
        self.halted = reason
        if reason and self.is_playing and not self.stop_text:
            self.stop_text = reason

    def stop(self, reason=None):
        self.halt(reason)
        self.stop_calls += 1
        self.is_playing = False
        self.finish_message = (f"Playback stopped: {self.stop_text}" if self.stop_text
                               else "Playback stopped.")


class FakeHotkeys:
    def __init__(self, **kwargs):
        self.kw = kwargs
        self.start_error = None
        self.started = 0
        self.stopped = 0
        self.enabled_calls = []
        self.bindings = hotkey_spec.sanitize_bindings(kwargs.get("bindings"))

    def start(self):
        self.started += 1
        if self.start_error is not None:
            raise self.start_error

    def stop(self):
        self.stopped += 1

    def set_enabled(self, enabled):
        self.enabled_calls.append(enabled)

    def set_bindings(self, bindings):
        self.bindings = hotkey_spec.sanitize_bindings(bindings)
        self.extras = {a: b for a, b in getattr(self, "extras", {}).items()
                       if not any(hotkey_spec.same_keys(b, m) for m in self.bindings.values())}

    def set_extra(self, action, binding):
        self.extras = getattr(self, "extras", {})
        if binding is None:
            self.extras.pop(action, None)
            return True
        if any(hotkey_spec.same_keys(binding, m) for m in self.bindings.values()):
            self.extras.pop(action, None)
            return False
        self.extras[action] = dict(binding)
        return True

    def press(self, action):
        """What the real class does when a hotkey fires: dispatch on_action(action)."""
        self.kw["dispatch"](lambda: self.kw["on_action"](action))


class FakeTray:
    works = True

    def __init__(self, **kwargs):
        self.kw = kwargs
        self.started = 0
        self.stopped = 0
        self.states = []
        self.labels = {}

    def start(self):
        self.started += 1
        return FakeTray.works

    def stop(self):
        self.stopped += 1

    def set_state(self, state):
        self.states.append(state)

    def set_labels(self, labels):
        self.labels = dict(labels)

    def notify(self, message, title="Wackro"):
        self.notes = getattr(self, "notes", []) + [message]


class FakeWatch:
    """Stands in for failsafe.InputWatch (no real input hooks in tests)."""

    def __init__(self, on_trip, screen_corners=None):
        self.on_trip = on_trip
        self.screen_corners = screen_corners
        self.starts = []
        self.start_kwargs = []
        self.stops = 0
        self.works = True
        self.active = False
        self.blacked_out = False
        self.last_activity = -1e9
        self.on_wake = None

    def start(self, options, hotkey_vks=(), track=False, on_wake=None, mod_vks=()):
        self.starts.append((dict(options), set(hotkey_vks)))
        self.start_kwargs.append({"track": track, "mod_vks": set(mod_vks)})
        self.on_wake = on_wake
        self.active = self.works
        return self.works

    def stop(self):
        self.stops += 1
        self.active = False
        self.blacked_out = False

    def set_blacked_out(self, on):
        self.blacked_out = on

    def note_cursor(self, x, y):
        pass


class FakeBlackout:
    def __init__(self, root):
        self.prepared = 0
        self.shown = False
        self.texts = []
        self.shows = 0
        self.destroyed = 0

    def prepare(self):
        self.prepared += 1

    def show(self, text=""):
        self.shows += 1
        self.shown = True
        self.texts.append(text)

    def set_text(self, text):
        self.texts.append(text)

    def hide(self):
        self.shown = False

    def destroy(self):
        self.destroyed += 1


class FakeDimmer:
    def __init__(self):
        self.calls = []
        self.dimmed = False

    def recover(self):
        self.calls.append("recover")

    def dim(self):
        self.calls.append("dim")
        self.dimmed = True

    def restore(self):
        if self.dimmed:
            self.calls.append("restore")
        self.dimmed = False

    def close(self, timeout=3.0):
        self.restore()
        self.calls.append("close")

    def note_cursor(self, x, y):
        pass


class FakeOverlay:
    def __init__(self, root):
        self.root = root
        self.prepared = 0
        self.shown = []        # (state, text, style) of each show()
        self.texts = []
        self.hidden = 0
        self.destroyed = 0
        self.state = None

    def prepare(self):
        self.prepared += 1

    def show(self, state, text="", style="both"):
        self.shown.append((state, text, style))
        self.state = state

    def set_text(self, text):
        self.texts.append(text)

    def hide(self):
        self.hidden += 1
        self.state = None

    def destroy(self):
        self.destroyed += 1


class FakeDialogs:
    def __init__(self):
        self.save_to = None
        self.open_from = None
        self.confirm_answer = True
        self.confirms = []
        self.errors = []
        self.save_asked = 0
        self.open_asked = 0
        self.asked = []
        self.answers = {}
        self.close_answer = ("exit", False)
        self.save_changes_answer = "no"       # "Save changes?" -> No (throw the old one away)

    def save_path(self):
        self.save_asked += 1
        return self.save_to

    def open_path(self):
        self.open_asked += 1
        return self.open_from

    def confirm(self, title, message):
        self.confirms.append((title, message))
        return self.confirm_answer

    def error(self, title, message):
        self.errors.append((title, message))

    # the new boxes: each answers with whatever the test put in self.answers
    def _answer(self, name, *args):
        self.asked.append((name,) + args)
        return self.answers.get(name)

    def info(self, title, heading, text):
        self.asked.append(("info", title, heading, text))

    def ask_save_changes(self, detail="", what="recording"):
        self.asked.append(("save_changes", detail) if what == "recording" else ("save_changes", detail, what))
        return self.save_changes_answer

    def close_choice(self, tray):
        self.asked.append(("close_choice", tray))
        return self.close_answer

    def ask_speed(self, speed):
        return self._answer("speed", speed)

    def ask_repeat(self, repeat, infinite, delay):
        return self._answer("repeat", repeat, infinite, delay)

    def ask_record_options(self, options):
        return self._answer("record_options", options)

    def ask_hotkeys(self, bindings):
        return self._answer("hotkeys", bindings)

    def ask_safety(self, options, stop_keys):
        return self._answer("safety", options, stop_keys)

    def ask_blackout(self, options, main_bindings):
        return self._answer("blackout", options, main_bindings)

    def ask_sequence(self, sequence, current=None):
        self.asked.append(("sequence", sequence, current))
        answer = self.answers.get("sequence")
        return answer(sequence) if callable(answer) else answer

    def save_sequence_path(self):
        self.save_asked += 1
        return self.seq_save_to if hasattr(self, "seq_save_to") else None

    def recording_paths(self, parent):
        return []


# ---- Pure helpers (these need the module to import, but no window) ------------------------

@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI libraries unavailable: {_IMPORT_ERROR}")
class TestHelpers(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(0), "0:00.0")
        self.assertEqual(format_duration(75.25), "1:15.2")
        self.assertEqual(format_duration(9.96), "0:10.0")
        self.assertEqual(format_duration(3600), "60:00.0")
        self.assertEqual(format_duration(-5), "0:00.0")

    def test_plural(self):
        self.assertEqual(plural(1, "event"), "1 event")
        self.assertEqual(plural(0, "event"), "0 events")
        self.assertEqual(plural(12345, "event"), "12,345 events")

    def test_has_key_events(self):
        self.assertFalse(has_key_events(MOUSE_EVENTS))
        self.assertTrue(has_key_events(KEY_EVENTS))
        self.assertFalse(has_key_events([]))
        self.assertTrue(has_key_events([{"t": 0.0, "type": "key_release", "key": {"kind": "char", "value": "a"}}]))

    def test_duration_of(self):
        self.assertEqual(duration_of([]), 0.0)
        self.assertEqual(duration_of(MOUSE_EVENTS), 0.6)


# ---- The window --------------------------------------------------------------------------

@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI libraries unavailable: {_IMPORT_ERROR}")
def temp_settings_path(testcase) -> Path:
    """A settings file location that is thrown away after the test (never the real one)."""
    folder = tempfile.TemporaryDirectory()
    testcase.addCleanup(folder.cleanup)
    return Path(folder.name) / "settings.json"


class AppImportsTests(unittest.TestCase):
    """On Windows there is always a screen, so the app failing to import is a real bug: fail loudly
    with the real error instead of quietly skipping every window test."""

    @unittest.skipUnless(sys.platform.startswith("win"), "only Windows always has a display")
    def test_the_app_imports_on_windows(self):
        if _IMPORT_ERROR is not None:
            self.fail("app.py failed to import:\n" + _IMPORT_TRACEBACK)


class NoRealSideEffectsTests(unittest.TestCase):
    """Every App built in these tests must use the fake tray and hotkeys: a real tray icon or a
    real keyboard hook would show up on the machine running the tests (e.g. during build.ps1)."""

    def test_every_app_in_this_file_passes_fakes(self):
        source = Path(__file__).read_text(encoding="utf-8")
        calls = source.split("App(")[1:]
        for call in calls:
            head = call[:400]
            if not head.lstrip().startswith("recorder_factory="):
                continue   # not a constructor call
            with self.subTest(call=head[:80]):
                self.assertIn("tray_factory=", head)
                self.assertIn("hotkeys_factory=", head)
                self.assertIn("overlay_factory=", head)
                self.assertIn("watch_factory=", head)
                self.assertIn("blackout_factory=", head)
                self.assertIn("dimmer=", head)


class AppCase(unittest.TestCase):
    DELAY = 0.03

    def setUp(self):
        import winutil
        patcher = mock.patch.object(winutil, "get_win32", lambda screen_size=None: winutil.NullWin32(screen_size))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dialogs = FakeDialogs()
        self.settings_path = temp_settings_path(self)
        try:
            self.app = App(
                recorder_factory=FakeRecorder,
                player_factory=FakePlayer,
                hotkeys_factory=FakeHotkeys,
                tray_factory=FakeTray, overlay_factory=FakeOverlay, watch_factory=FakeWatch, blackout_factory=FakeBlackout, dimmer=FakeDimmer(),
                dialogs=self.dialogs,
                delay=self.DELAY,
                settings_path=self.settings_path,
            )
        except tkinter.TclError as error:
            self.skipTest(f"no display: {error}")
        self.app.withdraw()
        self.app._double_press = 0      # tests press keys milliseconds apart on purpose
        self.addCleanup(self._destroy)
        # An error inside a Tk callback must never vanish silently in these tests.
        self.callback_errors = []

        def report(exc_type, exc, tb):
            import traceback
            self.callback_errors.append(exc)
            sys.stderr.write("\n[Tk callback error in %s]\n" % self.id())
            traceback.print_exception(exc_type, exc, tb)
        self.app.report_callback_exception = report
        self.recorder = self.app._recorder
        self.player = self.app._player
        self.hotkeys = self.app._hotkeys
        self.tray = self.app._tray
        # With a tray, hiding means withdraw (gone from Alt+Tab); the window counts as "open" so
        # it is brought back afterwards.
        self.hide = mock.patch.object(self.app, "withdraw").start()
        self.iconify = mock.patch.object(self.app, "iconify").start()
        self.deiconify = mock.patch.object(self.app, "deiconify").start()
        mock.patch.object(self.app, "_window_visible", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def _destroy(self):
        if not self.app._closed:
            self.app.tk.eval("proc bgerror {args} {}")   # CTk leaves timers behind; keep the output clean
            try:
                for job in self.app.tk.splitlist(self.app.tk.call("after", "info")):
                    try:
                        self.app.after_cancel(job)
                    except Exception:
                        pass
            except Exception:
                pass
            self.app._closed = True
            self.app._cancel_pending()
            for job in (self.app._tick_job, self.app._inbox_job):
                if job is not None:
                    self.app.after_cancel(job)
            self.app.destroy()

    def pump(self, condition=lambda: False, timeout=2.0):
        """Run the app's Tk events until `condition()` is true (or `timeout`); returns the condition.

        A plain loop with a hard time limit, so it can never hang. (It used to be mainloop() ended
        by a timer; an error raised inside that timer, e.g. a ResourceWarning from the garbage
        collector, killed the timer and the loop then ran forever. Seen on Windows.) Other threads
        never call tkinter (they hand work to the app's inbox), so mainloop() isn't needed.
        """
        end = time.monotonic() + timeout
        dont_wait = tkinter._tkinter.DONT_WAIT
        while True:
            if condition():
                return True
            if time.monotonic() >= end:
                return bool(condition())
            try:
                # A bounded batch, then look at the condition again: on Windows there is nearly
                # always another event waiting, so "until nothing is left" could mean "until the
                # timeout" (that made every wait take its full time).
                for _ in range(50):
                    if not self.app.tk.dooneevent(dont_wait):
                        break
            except tkinter.TclError:
                pass                      # the window is gone
            time.sleep(0.002)

    def settle(self):
        """Let the minimize-delay timer fire."""
        self.pump(lambda: self.app._pending is None)

    def run_in_thread(self, fn):
        """Call fn on a worker thread once the main loop is running (call pump() afterwards)."""
        self.app.after(1, lambda: threading.Thread(target=fn, daemon=True).start())

    def menu_enabled(self, menu, prefix):
        for item in self.app.menu_items(menu):
            if item is not None and item[0].strip(" ✓").startswith(prefix):
                return item[2] if len(item) > 2 else True
        raise KeyError(prefix)

    def menu_command(self, menu, prefix):
        for item in self.app.menu_items(menu):
            if item is not None and item[0].strip(" ✓").startswith(prefix):
                return item[1]
        raise KeyError(prefix)

    def states(self):
        a = self.app
        return {
            "record": a._record_btn.cget("state"),
            "stop": a._stop_btn.cget("state"),
            "play": a._play_btn.cget("state"),
            "save": "normal" if self.menu_enabled("File", "Save As") else "disabled",
            "load": "normal" if self.menu_enabled("File", "Open") else "disabled",
        }

    def record_something(self, events=None):
        if events is not None:
            self.recorder.events_to_return = list(events)
        self.app.start_recording()
        self.settle()
        self.app.stop(trim_tail=False)

    @property
    def status(self):
        return self.app._status_label.cget("text")

    @property
    def info(self):
        return self.app._info_label.cget("text")


class TestStartup(AppCase):
    def test_initial_state(self):
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.states(), {
            "record": "normal", "stop": "disabled", "play": "disabled", "save": "disabled", "load": "normal",
        })
        self.assertEqual(self.status, "Ready.")
        self.assertEqual(self.info, "No recording yet.")

    def test_buttons_show_their_hotkeys(self):
        self.assertEqual(self.app._key_labels["record"].cget("text"), "F9")
        self.assertEqual(self.app._key_labels["play"].cget("text"), "F11")
        self.assertEqual(self.app._key_labels["stop"].cget("text"), "F10")

    def test_the_tray_is_started_and_gets_the_hotkey_names(self):
        self.assertEqual(self.tray.started, 1)
        self.assertTrue(self.app._tray_ok)
        self.assertEqual(self.tray.labels, {"record": "F9", "play": "F11", "stop": "F10"})
        self.assertEqual(self.tray.states[-1], "idle")

    def test_dark_mode(self):
        self.assertEqual(customtkinter.get_appearance_mode(), "Dark")

    def test_summary_line(self):
        self.assertEqual(self.app._summary_label.cget("text"), "Repeat 1× · 1x speed")

    def test_privacy_notice_is_visible(self):
        self.assertIn("passwords", self.app._notice.cget("text"))

    def test_hotkeys_are_started_and_wired(self):
        self.assertEqual(self.hotkeys.started, 1)
        self.assertEqual(self.hotkeys.kw["on_action"], self.app.on_hotkey)
        self.assertEqual(self.hotkeys.kw["bindings"], hotkey_spec.DEFAULT_BINDINGS)

    def test_recorder_and_player_get_callbacks(self):
        self.assertIsNotNone(self.recorder.on_auto_stop)
        self.assertIsNotNone(self.player.on_finished)

    def test_hotkey_start_failure_is_shown_and_buttons_still_work(self):
        self.app.close()
        dialogs = FakeDialogs()

        class BadHotkeys(FakeHotkeys):
            def start(self):
                raise RuntimeError("Could not start the global hotkeys: nope")

        app = App(recorder_factory=FakeRecorder, player_factory=FakePlayer,
                  hotkeys_factory=BadHotkeys, tray_factory=FakeTray, overlay_factory=FakeOverlay, watch_factory=FakeWatch, blackout_factory=FakeBlackout, dimmer=FakeDimmer(), dialogs=dialogs, delay=0.0,
                  settings_path=temp_settings_path(self))
        try:
            app.withdraw()
            self.assertIn("nope", app._status)
            self.assertEqual(app._record_btn.cget("state"), "normal")
        finally:
            app.close()


class TestThreadHop(AppCase):
    """Everything that comes from a worker thread must be handed to the Tk thread, not run inline."""

    def test_auto_stop_callback_only_hops(self):
        with mock.patch.object(self.app, "_hop") as hop:
            self.recorder.on_auto_stop("limit")
        hop.assert_called_once_with(self.app._handle_auto_stop)

    def test_play_finished_callback_only_hops(self):
        with mock.patch.object(self.app, "_hop") as hop:
            self.player.on_finished("done")
        hop.assert_called_once_with(self.app._handle_play_finished)

    def test_hotkeys_dispatch_through_the_hop(self):
        self.assertEqual(self.hotkeys.kw["dispatch"], self.app._hop)

    def test_tray_dispatches_through_the_hop(self):
        self.assertEqual(self.tray.kw["dispatch"], self.app._hop)

    def test_hop_never_calls_tkinter_from_the_calling_thread(self):
        # A blocked Tk thread must never block a worker (the Windows keyboard hook especially).
        with mock.patch.object(self.app, "after", side_effect=AssertionError("tkinter called")):
            done = threading.Event()
            threading.Thread(target=lambda: (self.app._hop(lambda: None), done.set()), daemon=True).start()
            self.assertTrue(done.wait(1))

    def test_hopped_work_runs_on_the_tk_thread_in_order(self):
        seen = []
        for i in range(3):
            self.run_in_thread(lambda i=i: self.app._hop(lambda: seen.append((i, threading.current_thread()))))
        self.assertTrue(self.pump(lambda: len(seen) == 3))
        self.assertEqual(sorted(i for i, _ in seen), [0, 1, 2])
        self.assertTrue(all(thread is threading.main_thread() for _, thread in seen))

    def test_a_crash_in_hopped_work_doesnt_stop_the_inbox(self):
        seen = []
        self.app._hop(lambda: 1 / 0)
        self.app._hop(lambda: seen.append("after"))
        self.assertTrue(self.pump(lambda: seen == ["after"]))


class TestMinimizeDelay(AppCase):
    def test_recorder_waits_for_the_configured_delay(self):
        self.app._delay_ms = 400
        self.app.start_recording()
        self.pump(timeout=0.1)
        self.assertEqual(self.recorder.start_calls, 0)
        self.pump(lambda: self.recorder.start_calls == 1)
        self.assertEqual(self.recorder.start_calls, 1)

    def test_player_waits_for_the_configured_delay(self):
        self.record_something()
        self.app._delay_ms = 400
        self.app.start_playback()
        self.pump(timeout=0.1)
        self.assertEqual(self.player.started_with, [])
        self.pump(lambda: bool(self.player.started_with))
        self.assertEqual(len(self.player.started_with), 1)

    def test_default_delay_comes_from_config(self):
        default = inspect.signature(App.__init__).parameters["delay"].default
        self.assertEqual(default, config.WINDOW_MINIMIZE_DELAY)


class TestRecording(AppCase):
    def test_record_goes_straight_to_recording_but_starts_after_the_delay(self):
        self.app.start_recording()
        self.assertEqual(self.app._state, "recording")
        self.assertEqual(self.recorder.start_calls, 0)
        self.assertEqual(self.hide.call_count, 1)
        self.assertEqual(self.states()["record"], "disabled")
        self.assertEqual(self.states()["stop"], "normal")
        self.assertEqual(self.info, "Starting…")
        self.settle()
        self.assertEqual(self.recorder.start_calls, 1)

    def test_record_while_not_idle_is_ignored(self):
        self.app.start_recording()
        self.app.start_recording()
        self.settle()
        self.app.start_recording()
        self.assertEqual(self.recorder.start_calls, 1)
        self.assertEqual(self.hide.call_count, 1)

    def test_live_counter_and_timer_while_recording(self):
        self.app.start_recording()
        self.settle()
        self.app._refresh()
        self.assertEqual(self.info, "● 7 events   0:01.5")

    def test_stop_button_trims_the_tail_and_hotkey_does_not(self):
        self.app.start_recording()
        self.settle()
        self.app._stop_btn.invoke()
        self.assertEqual(self.recorder.stop_calls, [True])
        self.app.start_recording()
        self.settle()
        self.app.on_hotkey("stop")
        self.assertEqual(self.recorder.stop_calls, [True, False])

    def test_stop_stores_the_events_and_restores_the_window(self):
        self.record_something()
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.app._events, MOUSE_EVENTS)
        self.assertTrue(self.app._dirty)
        self.assertEqual(self.deiconify.call_count, 1)
        self.assertEqual(self.status, "Recorded 3 events (0:00.6).")
        self.assertEqual(self.info, "3 events, 0:00.6  (not saved)")
        s = self.states()
        self.assertEqual((s["record"], s["stop"], s["play"], s["save"]), ("normal", "disabled", "normal", "normal"))

    def test_skipped_inputs_are_mentioned(self):
        self.recorder.skipped_keys = 2
        self.recorder.skipped_buttons = 1
        self.record_something()
        self.assertIn("3 inputs couldn't be recorded", self.status)

    def test_new_recording_replaces_the_old_one(self):
        self.record_something(MOUSE_EVENTS)
        self.record_something(KEY_EVENTS)
        self.assertEqual(self.app._events, KEY_EVENTS)

    def test_stop_while_still_waiting_cancels(self):
        self.app.start_recording()
        self.app.stop()
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.status, "Cancelled.")
        self.assertEqual(self.deiconify.call_count, 1)
        self.pump(timeout=self.DELAY * 4)
        self.assertEqual(self.recorder.start_calls, 0)
        self.assertEqual(self.recorder.stop_calls, [])

    def test_start_failure_goes_back_to_idle_with_the_message(self):
        self.recorder.start_error = RecordingError("Couldn't start recording: no display")
        self.app.start_recording()
        self.settle()
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.status, "Couldn't start recording: no display")
        self.assertEqual(self.deiconify.call_count, 1)
        self.assertEqual(self.states()["record"], "normal")

    def test_stop_when_idle_does_nothing(self):
        self.app.stop()
        self.assertEqual(self.recorder.stop_calls, [])
        self.assertEqual(self.player.stop_calls, 0)

    def test_auto_stop_from_a_worker_thread_finishes_the_recording(self):
        self.recorder.stop_message = "Recording stopped: it reached the limit of 200,000 events."
        self.app.start_recording()
        self.settle()
        self.run_in_thread(lambda: self.recorder.on_auto_stop("limit"))
        self.assertTrue(self.pump(lambda: self.app._state == "idle"))
        self.assertEqual(self.recorder.stop_calls, [False])
        self.assertTrue(self.status.startswith("Recording stopped: it reached the limit"))
        self.assertIn("Recorded 3 events", self.status)
        self.assertEqual(self.app._events, MOUSE_EVENTS)

    def test_late_auto_stop_after_a_normal_stop_is_ignored(self):
        self.record_something()
        self.recorder.stop_calls.clear()
        self.run_in_thread(lambda: self.recorder.on_auto_stop("limit"))
        self.pump(timeout=0.1)
        self.assertEqual(self.recorder.stop_calls, [])


class TestPlayback(AppCase):
    def setUp(self):
        super().setUp()
        self.record_something()
        self.hide.reset_mock()
        self.deiconify.reset_mock()

    def test_play_without_a_recording_says_so_and_does_not_minimize(self):
        self.app._events = []
        self.app.start_playback()
        self.assertEqual(self.app._state, "idle")
        self.assertIn("nothing to play", self.status)
        self.assertEqual(self.hide.call_count, 0)

    def test_play_goes_straight_to_playing_and_starts_after_the_delay(self):
        self.app.start_playback()
        self.assertEqual(self.app._state, "playing")
        self.assertEqual(self.player.started_with, [])
        self.assertEqual(self.hide.call_count, 1)
        self.assertEqual(self.states()["play"], "disabled")
        self.assertEqual(self.states()["save"], "disabled")
        self.assertEqual(self.states()["load"], "disabled")
        self.settle()
        self.assertEqual(self.player.started_with, [MOUSE_EVENTS])

    def test_hotkey_guard_is_up_before_the_player_has_started(self):
        guard = self.app._hotkey_guard
        self.assertFalse(guard())
        self.app.start_playback()
        self.assertTrue(guard())           # still waiting out the delay
        self.settle()
        self.assertTrue(guard())
        self.app.stop()
        self.assertFalse(guard())

    def test_play_while_not_idle_is_ignored(self):
        self.app.start_playback()
        self.app.start_playback()
        self.settle()
        self.app.start_playback()
        self.assertEqual(len(self.player.started_with), 1)

    def test_record_while_playing_is_ignored(self):
        self.app.start_playback()
        self.settle()
        self.app.start_recording()
        self.assertEqual(self.recorder.start_calls, 1)   # only the one from setUp
        self.assertEqual(self.app._state, "playing")

    def test_progress_is_shown_while_playing(self):
        self.app.start_playback()
        self.settle()
        self.app._refresh()
        self.assertEqual(self.info, "▶ 3 / 10 events   0:02.0")

    def test_finished_on_a_worker_thread_returns_to_idle(self):
        self.app.start_playback()
        self.settle()
        self.player.is_playing = False
        self.run_in_thread(lambda: self.player.on_finished("done"))
        self.assertTrue(self.pump(lambda: self.app._state == "idle"))
        self.assertEqual(self.status, "Playback finished.")
        self.assertEqual(self.deiconify.call_count, 1)
        self.assertEqual(self.states()["play"], "normal")

    def test_stale_finished_notice_does_not_end_a_newer_playback(self):
        self.app.start_playback()
        self.settle()
        self.player.is_playing = True      # a newer playback is running
        self.run_in_thread(lambda: self.player.on_finished("done"))
        self.pump(timeout=0.1)
        self.assertEqual(self.app._state, "playing")

    def test_stop_while_playing_stops_the_player_and_returns_to_idle(self):
        self.app.start_playback()
        self.settle()
        self.app.stop()
        self.assertEqual(self.player.stop_calls, 1)
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.status, "Playback stopped.")
        self.assertEqual(self.deiconify.call_count, 1)

    def test_stop_while_waiting_cancels_without_touching_the_player(self):
        self.app.start_playback()
        self.app.stop()
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.status, "Cancelled.")
        self.pump(timeout=self.DELAY * 4)
        self.assertEqual(self.player.started_with, [])
        self.assertEqual(self.player.stop_calls, 0)

    def test_start_failure_goes_back_to_idle_with_the_message(self):
        self.player.start_error = RecordingError("Couldn't take control of the mouse and keyboard: x")
        self.app.start_playback()
        self.settle()
        self.assertEqual(self.app._state, "idle")
        self.assertIn("Couldn't take control", self.status)
        self.assertEqual(self.deiconify.call_count, 1)

    def test_playback_leaves_the_recording_alone(self):
        self.app.start_playback()
        self.settle()
        self.assertEqual(self.app._events, MOUSE_EVENTS)
        self.assertTrue(self.app._dirty)


@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI libraries unavailable: {_IMPORT_ERROR}")
class TestRepeatHelpers(unittest.TestCase):
    def test_parse_repeat_accepts_whole_numbers(self):
        for text, expected in (("1", 1), ("5", 5), (" 12 ", 12), ("1,000", 1000), (str(config.MAX_REPEAT), config.MAX_REPEAT)):
            with self.subTest(text=text):
                self.assertEqual(parse_repeat(text), expected)

    def test_parse_repeat_rejects_everything_else(self):
        for text in ("", " ", "0", "-1", "1.5", "abc", "5x", "²", "٣", str(config.MAX_REPEAT + 1), "nan", "1e3"):
            with self.subTest(text=text), self.assertRaises(ValueError) as ctx:
                parse_repeat(text)
            self.assertIn("Repeat must be a whole number", str(ctx.exception))
            self.assertIn("Infinite", str(ctx.exception))

    def test_parse_delay_accepts_numbers(self):
        for text, expected in (("", 0.0), (" ", 0.0), ("0", 0.0), ("2", 2.0), ("1.5", 1.5), ("1,5", 1.5), ("1e1", 10.0),
                               (str(int(config.MAX_REPEAT_DELAY)), config.MAX_REPEAT_DELAY)):
            with self.subTest(text=text):
                self.assertEqual(parse_delay(text), expected)

    def test_parse_delay_rejects_everything_else(self):
        for text in ("abc", "-1", "nan", "inf", "-inf", "1..2", "1 2", str(config.MAX_REPEAT_DELAY + 1)):
            with self.subTest(text=text), self.assertRaises(ValueError) as ctx:
                parse_delay(text)
            self.assertIn("Delay must be a number of seconds", str(ctx.exception))

    def test_parse_repeat_settings_combines_them(self):
        self.assertEqual(parse_repeat_settings("3", False, "1.5"), (3, 1.5))
        self.assertEqual(parse_repeat_settings("3", True, ""), (0, 0.0))

    def test_infinite_ignores_the_repeat_box_but_still_checks_the_delay(self):
        self.assertEqual(parse_repeat_settings("garbage", True, "2"), (0, 2.0))
        with self.assertRaises(ValueError):
            parse_repeat_settings("garbage", True, "garbage")

    def test_playing_status(self):
        self.assertEqual(playing_status(1), "Playing… press F10 to stop.")
        self.assertEqual(playing_status(0, "NumLock or F10"), "Playing on repeat… press NumLock or F10 to stop.")
        self.assertEqual(playing_status(1500), "Playing 1,500 runs… press F10 to stop.")

    def test_playback_summary(self):
        base = settings_module.default_settings()
        self.assertEqual(playback_summary(base), "Repeat 1× · 1x speed")
        self.assertEqual(playback_summary({**base, "repeat": 3, "delay": 0.5, "speed": 2.0}),
                         "Repeat 3× · wait 0.5 s · 2x speed")
        self.assertEqual(playback_summary({**base, "infinite": True, "speed": 0.5}), "Repeat ∞ · 0.5x speed")
        self.assertEqual(playback_summary({**base, "delay": 4}), "Repeat 1× · 1x speed")   # no wait for one run

    def test_recording_summary(self):
        base = settings_module.default_settings()
        self.assertEqual(recording_summary(base), "")
        self.assertEqual(recording_summary({**base, "record_moves": False}), "Recording: keys, clicks")
        self.assertEqual(recording_summary({**base, "record_delay": False}), "no delay")


class TestFailsafes(AppCase):
    W_EVENTS = [
        {"t": 0.0, "type": "mouse_move", "x": 10, "y": 10},
        {"t": 0.5, "type": "key_press", "key": {"kind": "char", "value": "a"}, "w": {"exe": "BIR.exe", "cls": "X"}},
        {"t": 0.6, "type": "key_release", "key": {"kind": "char", "value": "a"}},
    ]

    def setUp(self):
        super().setUp()
        self.record_something()
        self.watch = self.app._watch
        import winutil
        self.win = winutil.NullWin32()
        self.running = None
        self.win.running_programs = lambda: self.running
        mock.patch.object(self.app, "_win32", return_value=self.win).start()

    def play(self):
        self.app.start_playback()
        self.settle()

    def test_playback_starts_the_watch_and_passes_the_guard(self):
        self.play()
        options, vks = self.watch.starts[-1]
        self.assertTrue(options["fs_keys"])
        self.assertEqual(vks, {0x7A, 0x79})          # Play / Stop (F11 / F10) are left to the hotkeys
        self.assertEqual(self.player.on_cursor, self.watch.note_cursor)
        self.assertIsNotNone(self.player.guard)
        self.app.stop()
        self.pump(lambda: self.app._state == "idle")
        self.assertGreaterEqual(self.watch.stops, 1)
        self.assertFalse(self.watch.active)

    def test_window_checks_off_means_no_guard_and_no_watch_means_no_on_cursor(self):
        self.app._settings.update(fs_window=False, fs_crash=False, fs_locked=False)
        self.watch.works = False
        self.play()
        self.assertIsNone(self.player.guard)
        self.assertIsNone(self.player.on_cursor)

    def test_you_take_over(self):
        self.play()
        self.run_in_thread(lambda: self.watch.on_trip("you moved the mouse"))
        self.assertTrue(self.pump(lambda: self.app._state == "idle"))
        self.assertEqual(self.player.halted, "you moved the mouse")      # halted on the hook thread
        self.assertEqual(self.status, "Playback stopped: you moved the mouse")
        self.assertEqual(self.tray.notes, ["Playback stopped: you moved the mouse"])
        self.assertFalse(self.watch.active)

    def test_a_late_trip_after_playback_is_harmless(self):
        self.play()
        self.app.stop()
        self.pump(lambda: self.app._state == "idle")
        calls = self.player.stop_calls
        self.watch.on_trip("you pressed a key")
        self.pump(timeout=0.1)
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.player.stop_calls, calls)

    def test_a_normal_stop_sends_no_notification(self):
        self.play()
        self.app.stop()
        self.pump(lambda: self.app._state == "idle")
        self.assertEqual(getattr(self.tray, "notes", []), [])

    def test_a_failing_start_stops_the_watch(self):
        self.player.start_error = RecordingError("nope")
        self.play()
        self.assertEqual(self.app._state, "idle")
        self.assertFalse(self.watch.active)

    def test_preflight_warns_about_a_closed_program(self):
        self.app._events = list(self.W_EVENTS)
        self.running = {"explorer.exe"}
        self.dialogs.confirm_answer = False
        self.app.start_playback()
        self.assertEqual(self.app._state, "idle")
        title, message = self.dialogs.confirms[-1]
        self.assertIn("BIR.exe", message)
        self.assertIn("Not played", self.status)
        self.dialogs.confirm_answer = True
        self.play()
        self.assertEqual(len(self.player.started_with), 1)

    def test_preflight_is_quiet_when_everything_is_open_or_it_is_off(self):
        self.app._events = list(self.W_EVENTS)
        self.running = {"bir.exe"}
        self.play()
        self.assertEqual(self.dialogs.confirms, [])
        self.app.stop()
        self.pump(lambda: self.app._state == "idle")
        self.running = set()
        self.app._settings["fs_preflight"] = False
        self.play()
        self.assertEqual(self.dialogs.confirms, [])
        self.assertEqual(len(self.player.started_with), 2)

    def test_a_broken_preflight_plays_anyway(self):
        self.app._events = list(self.W_EVENTS)
        self.win.running_programs = mock.Mock(side_effect=OSError("boom"))
        self.play()
        self.assertEqual(len(self.player.started_with), 1)

    def test_safety_dialog(self):
        self.dialogs.answers["safety"] = {"fs_keys": False, "fs_move_sensitivity": "high", "junk": 1}
        self.app.open_safety()
        name, options, stop_keys = self.dialogs.asked[-1]
        self.assertEqual(name, "safety")
        self.assertTrue(options["fs_keys"])
        self.assertEqual(stop_keys, "F11 or F10")
        self.assertFalse(self.app._settings["fs_keys"])
        self.assertEqual(self.app._settings["fs_move_sensitivity"], "high")
        self.assertNotIn("junk", self.app._settings)
        self.assertIn("9 of 10", self.status)
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["fs_keys"])

    def test_safety_cancel_and_menu(self):
        self.app.open_safety()
        self.assertTrue(self.app._settings["fs_keys"])
        self.assertTrue(self.menu_enabled("Settings", "Safety"))
        self.play()
        self.assertFalse(self.menu_enabled("Settings", "Safety"))


class TestBlackoutAndPower(AppCase):
    def setUp(self):
        super().setUp()
        self.record_something()
        self.watch = self.app._watch
        self.blackout = self.app._blackout
        self.dimmer = self.app._dimmer
        import winutil
        self.win = winutil.NullWin32()
        self.awake = []
        self.win.keep_awake = lambda on: self.awake.append(on) or True
        self.screens = [winutil.Monitor(0, 0, 1366, 768, True)]
        self.win.monitors = lambda: list(self.screens)
        mock.patch.object(self.app, "_win32", return_value=self.win).start()
        self.now = 1000.0
        self.app._clock = lambda: self.now
        self.app._settings.update(blackout=True, blackout_after=5.0)

    def play(self):
        self.app.start_playback()
        self.settle()

    def wait(self, seconds):
        self.now += seconds
        self.app._watch_playback()

    def test_blacks_out_after_the_set_time_and_keeps_playing(self):
        self.play()
        self.assertTrue(self.watch.start_kwargs[-1]["track"])
        self.wait(299)
        self.assertFalse(self.blackout.shown)
        self.wait(2)
        self.assertTrue(self.blackout.shown)
        self.assertTrue(self.watch.blacked_out)
        self.assertEqual(self.dimmer.calls[-1], "dim")
        self.assertEqual(self.app._state, "playing")
        self.assertTrue(self.blackout.texts[0].startswith("▶"))
        self.assertIsNone(self.app._overlay_shown)          # the border / pill step aside
        self.wait(1)
        self.assertEqual(self.blackout.shows, 1)              # shown once, then just updated

    def test_you_using_the_pc_puts_it_off(self):
        self.play()
        self.wait(200)
        self.watch.last_activity = self.now                   # you touched something
        self.wait(200)
        self.assertFalse(self.blackout.shown)
        self.wait(101)
        self.assertTrue(self.blackout.shown)

    def test_first_touch_wakes_it_and_playback_goes_on(self):
        self.play()
        self.wait(301)
        self.run_in_thread(self.watch.on_wake)                # from the Windows hook thread
        self.assertTrue(self.pump(lambda: not self.blackout.shown))
        self.assertEqual(self.dimmer.calls[-1], "restore")
        self.assertFalse(self.watch.blacked_out)
        self.assertEqual(self.app._state, "playing")
        self.wait(299)
        self.assertFalse(self.blackout.shown)                 # the timer starts over after a wake
        self.wait(2)
        self.assertTrue(self.blackout.shown)

    def test_everything_is_undone_when_playback_ends(self):
        self.play()
        self.assertEqual(self.awake, [True])
        self.wait(301)
        self.app.stop()
        self.pump(lambda: self.app._state == "idle")
        self.assertFalse(self.blackout.shown)
        self.assertEqual(self.dimmer.calls[-1], "restore")
        self.assertEqual(self.awake, [True, False])
        self.assertFalse(self.watch.active)

    def test_options(self):
        self.app._settings.update(blackout_dim=False, blackout_text=False, keep_awake=False)
        self.play()
        self.wait(301)
        self.assertTrue(self.blackout.shown)
        self.assertNotIn("dim", self.dimmer.calls)
        self.assertEqual(self.blackout.texts[0], "")
        self.assertEqual(self.awake, [])

    def test_off_by_default_and_never_without_the_watch(self):
        self.app._settings["blackout"] = False
        self.play()
        self.assertFalse(self.watch.start_kwargs[-1]["track"])
        self.wait(10000)
        self.assertFalse(self.blackout.shown)
        self.app.stop()
        self.pump(lambda: self.app._state == "idle")
        self.app._settings["blackout"] = True
        self.watch.works = False                              # nothing could wake it: never black out
        self.play()
        self.wait(10000)
        self.assertFalse(self.blackout.shown)

    def test_hotkey_blacks_out_now_and_wakes(self):
        self.app.on_hotkey("blackout")                        # idle: nothing
        self.assertFalse(self.blackout.shown)
        self.play()
        self.app.on_hotkey("blackout")
        self.assertTrue(self.blackout.shown)
        self.app.on_hotkey("blackout")
        self.assertFalse(self.blackout.shown)
        self.assertEqual(self.app._state, "playing")

    def test_blackout_hotkey_passes_through_the_watch(self):
        self.app._settings["blackout_hotkey"] = {"vk": 0x7B, "mods": ["ctrl"]}
        self.play()
        options, vks = self.watch.starts[-1]
        self.assertIn(0x7B, vks)
        self.assertIn(0x11, self.watch.start_kwargs[-1]["mod_vks"])

    def test_time_limit(self):
        self.app._settings.update(time_limit_on=True, time_limit=1.0)
        self.play()
        self.player.elapsed = 59.0
        self.wait(1)
        self.assertEqual(self.app._state, "playing")
        self.player.elapsed = 60.5
        self.wait(1)
        self.pump(lambda: self.app._state == "idle")
        self.assertEqual(self.status, "Playback stopped: it reached the time limit of 1 min")

    def test_screen_change_stops_it(self):
        import winutil
        self.play()
        self.wait(2)
        self.assertEqual(self.app._state, "playing")
        self.screens = [winutil.Monitor(0, 0, 1920, 1080, True)]
        self.wait(2)
        self.pump(lambda: self.app._state == "idle")
        self.assertIn("screen resolution or the monitors changed", self.status)

    def test_screen_check_can_be_off(self):
        import winutil
        self.app._settings["fs_screen"] = False
        self.play()
        self.screens = [winutil.Monitor(0, 0, 1920, 1080, True)]
        self.wait(2)
        self.assertEqual(self.app._state, "playing")

    def test_sleep_check_sets_the_players_lag_limit(self):
        self.play()
        self.assertEqual(self.player.max_lag, 5.0)
        self.app.stop()
        self.pump(lambda: self.app._state == "idle")
        self.app._settings["fs_sleep"] = False
        self.play()
        self.assertIsNone(self.player.max_lag)

    def test_blackout_dialog(self):
        self.dialogs.answers["blackout"] = {"blackout": True, "blackout_after": 12.0, "blackout_dim": False,
                                            "blackout_text": True, "keep_awake": True,
                                            "blackout_hotkey": {"vk": 0x7B, "mods": []}}
        self.assertTrue(self.menu_enabled("Settings", "Blackout"))
        self.app.open_blackout()
        name, options, main = self.dialogs.asked[-1]
        self.assertEqual(name, "blackout")
        self.assertEqual(main, self.app._settings["hotkeys"])
        self.assertEqual(self.app._settings["blackout_after"], 12.0)
        self.assertEqual(self.hotkeys.extras, {"blackout": {"vk": 0x7B, "mods": []}})
        self.assertEqual(self.hotkeys.enabled_calls, [False, True])
        self.assertIn("Blackout after 12 min", self.status)
        self.assertIn("F12", self.status)
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["blackout_hotkey"], {"vk": 0x7B, "mods": []})

    def test_rebinding_a_main_hotkey_onto_it_drops_the_blackout_hotkey(self):
        self.app._settings["blackout_hotkey"] = {"vk": 0x7B, "mods": []}
        self.app._apply_blackout_hotkey()
        self.dialogs.answers["hotkeys"] = dict(self.app._settings["hotkeys"], stop={"vk": 0x7B, "mods": []})
        self.app.open_hotkeys()
        self.assertIsNone(self.app._settings["blackout_hotkey"])

    def test_safety_time_limit_round_trip(self):
        self.dialogs.answers["safety"] = dict(failsafe_defaults(), time_limit_on=True, time_limit=30.0)
        self.app.open_safety()
        name, options, _ = self.dialogs.asked[-1]
        self.assertIn("time_limit", options)
        self.assertTrue(self.app._settings["time_limit_on"])
        self.assertEqual(self.app._settings["time_limit"], 30.0)
        self.assertIn("stop after 30 min", self.status)
        self.dialogs.answers["safety"] = dict(failsafe_defaults(), time_limit_on=False, time_limit=None)
        self.app.open_safety()
        self.assertEqual(self.app._settings["time_limit"], 30.0)     # a bad value while off is ignored

    def test_brightness_recovered_at_start_and_restored_at_exit(self):
        self.assertEqual(self.dimmer.calls[0], "recover")
        self.play()
        self.wait(301)
        self.app.exit_app()
        self.assertIn("restore", self.dimmer.calls)
        self.assertEqual(self.dimmer.calls[-1], "close")
        self.assertEqual(self.blackout.destroyed, 1)
        self.assertFalse(self.dimmer.dimmed)


def failsafe_defaults():
    import failsafe
    return dict(failsafe.DEFAULT_OPTIONS)


def seq_rec(letter, exe=None):
    events = [
        {"t": 0.0, "type": "mouse_move", "x": 1, "y": 1},
        {"t": 0.5, "type": "key_press", "key": {"kind": "char", "value": letter}},
        {"t": 1.0, "type": "key_release", "key": {"kind": "char", "value": letter}},
    ]
    if exe:
        events[1]["w"] = {"exe": exe, "cls": "X"}
    return {"version": 2 if exe else 1, "screen": {"width": 100, "height": 100}, "events": events}


def a_sequence(*names, rounds=2):
    import sequence as seq
    s = seq.new_sequence("Daily")
    s["rounds"] = rounds
    for name in names:
        s["steps"].append(seq.make_step(name, seq_rec(name[0].lower())))
    return s


class TestSequences(AppCase):
    def setUp(self):
        super().setUp()
        import winutil
        self.win = winutil.NullWin32()
        self.running = None
        self.win.running_programs = lambda: self.running
        mock.patch.object(self.app, "_win32", return_value=self.win).start()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def make(self, *names, rounds=2):
        self.dialogs.answers["sequence"] = a_sequence(*names, rounds=rounds)
        self.app.new_sequence()

    def play(self):
        self.app.start_playback()
        self.settle()

    def test_new_sequence_becomes_what_play_plays(self):
        self.make("Login", "Export")
        self.assertEqual(self.app._mode, "sequence")
        self.assertTrue(self.app._seq_dirty)
        self.assertIn("Sequence Daily", self.status)
        self.assertEqual(self.app._play_btn.cget("state"), "normal")
        self.assertIn("2 steps · In order · 2 rounds", self.app._summary_label.cget("text"))
        self.assertIn("Sequence: Daily", self.app._info_text())
        self.play()
        events = self.player.started_with[-1]
        self.assertEqual("".join(e["key"]["value"] for e in events if e["type"] == "key_press"), "le")
        self.assertEqual(self.player.start_options[-1], {"repeat": 2, "delay": 0.0, "speed": 1.0})
        self.assertTrue(callable(self.player.next_round))
        self.assertEqual(len(self.player.next_round(2)), len(events))

    def test_which_step_is_playing_shows_everywhere(self):
        self.make("Login", "Export")
        self.play()
        self.player.current_run, self.player.total_runs = 1, 2
        self.player.run_position = 1.5
        self.assertIn("Step 2/2 Export", self.app._info_text())
        self.assertIn("Round 1 / 2", self.app._info_text())
        self.assertIn("2/2 Export", self.app._overlay_text())
        self.player.run_position = 0.2
        self.assertIn("1/2 Login", self.app._overlay_text())

    def test_a_failsafe_stop_says_which_step(self):
        self.make("Login", "Export")
        self.play()
        self.player.run_position = 1.5
        self.run_in_thread(lambda: self.app._watch.on_trip("you pressed a key"))
        self.assertTrue(self.pump(lambda: self.app._state == "idle"))
        self.assertEqual(self.status, 'Playback stopped in step "Export": you pressed a key')
        self.assertIsNone(self.app._runner)

    def test_preflight_covers_every_step(self):
        s = a_sequence("Login")
        import sequence as seq
        s["steps"].append(seq.make_step("BIR", seq_rec("b", exe="BIR.exe")))
        self.dialogs.answers["sequence"] = s
        self.app.new_sequence()
        self.running = {"explorer.exe"}
        self.dialogs.confirm_answer = False
        self.app.start_playback()
        self.assertEqual(self.app._state, "idle")
        self.assertIn("BIR.exe", self.dialogs.confirms[-1][1])

    def test_save_and_open_round_trip(self):
        self.make("Login", "Export")
        self.dialogs.seq_save_to = str(self.dir / "daily.wseq")
        self.app.save_quick()
        self.assertEqual(self.dialogs.confirms[-1][0], "Keystrokes in this sequence")
        self.assertFalse(self.app._seq_dirty)
        self.assertIn("daily.wseq", self.status)
        saved = self.app._sequence
        self.app.save_quick()                               # Save again: same file, no questions
        self.assertEqual(self.dialogs.save_asked, 1)
        # a recording, then open the sequence file again
        self.record_something(MOUSE_EVENTS)
        self.assertEqual(self.app._mode, "recording")
        self.app._sequence = None
        self.dialogs.open_from = str(self.dir / "daily.wseq")
        self.app.load()
        self.assertEqual(self.app._sequence, saved)
        self.assertEqual(self.app._mode, "sequence")
        self.assertEqual(self.app._events, MOUSE_EVENTS)     # the recording is still there
        self.assertIn("Loaded sequence Daily", self.status)

    def test_a_sequence_saved_by_1_7_0_opens_and_plays(self):
        old = {"kind": "wackro-sequence", "version": 1, "name": "Old", "order": "shuffle_round", "picks": 0,
               "rounds": 2, "round_wait": {"min": 0.0, "max": 0.0}, "steps": [
                   {"name": n, "enabled": True, "locked": n in ("Login", "Logout"), "repeat": 1,
                    "wait": {"min": 0.0, "max": 0.0}, "recording": seq_rec(n[0].lower())}
                   for n in ("Login", "A", "B", "Logout")]}
        path = self.dir / "old.wseq"
        path.write_text(json.dumps(old), encoding="utf-8")
        self.dialogs.open_from = str(path)
        self.app.load()
        self.assertEqual([s["pin"] for s in self.app._sequence["steps"]], ["first", "none", "none", "last"])
        self.play()
        typed = "".join(e["key"]["value"] for e in self.player.started_with[-1] if e["type"] == "key_press")
        self.assertEqual((typed[0], typed[-1], sorted(typed[1:3])), ("l", "l", ["a", "b"]))

    def test_open_a_recording_switches_back(self):
        self.make("Login")
        path = self.dir / "r.json"
        storage.save_recording(path, storage.make_recording(KEY_EVENTS, 100, 100))
        self.dialogs.open_from = str(path)
        self.app.load()
        self.assertEqual(self.app._mode, "recording")
        self.assertEqual(self.app._events, KEY_EVENTS)
        self.assertIsNotNone(self.app._sequence)

    def test_unsaved_sequence_is_asked_about(self):
        self.make("Login")
        self.dialogs.save_changes_answer = "cancel"
        self.dialogs.answers["sequence"] = a_sequence("Other")
        self.app.new_sequence()
        self.assertEqual(self.dialogs.asked[-1][0], "save_changes")
        self.assertEqual(self.dialogs.asked[-1][2], "sequence")
        self.assertEqual(self.app._sequence["steps"][0]["name"], "Login")
        self.app.exit_app()
        self.assertFalse(self.app._closed)                 # Cancel keeps it open
        self.dialogs.save_changes_answer = "no"
        self.app.exit_app()
        self.assertTrue(self.app._closed)

    def test_edit_without_changes_stays_saved(self):
        self.make("Login")
        self.app._seq_dirty = False
        self.dialogs.answers["sequence"] = lambda current: current
        self.app.edit_sequence()
        self.assertFalse(self.app._seq_dirty)
        self.dialogs.answers["sequence"] = lambda current: dict(current, name="Changed")
        self.app.edit_sequence()
        self.assertTrue(self.app._seq_dirty)

    def test_the_current_recording_is_offered_to_the_editor(self):
        self.record_something(KEY_EVENTS)
        self.dialogs.answers["sequence"] = None
        self.app.new_sequence()
        name, sequence, current = self.dialogs.asked[-1]
        self.assertEqual(current[0], "Recording")
        self.assertEqual(current[1]["events"], KEY_EVENTS)
        self.assertEqual(self.app._mode, "recording")          # cancelled: nothing changed

    def test_menus(self):
        self.record_something(MOUSE_EVENTS)
        self.assertFalse(self.menu_enabled("Sequence", "Edit sequence"))
        self.assertFalse(self.menu_enabled("Sequence", "Play: sequence"))
        self.make("Login")
        self.assertTrue(self.menu_enabled("Sequence", "Edit sequence"))
        self.assertFalse(self.menu_enabled("Playback", "Repeat"))      # rounds live in the sequence
        self.app.set_mode("recording")
        self.assertEqual(self.app._mode, "recording")
        self.assertTrue(self.menu_enabled("Playback", "Repeat"))
        self.app.set_mode("sequence")
        self.assertEqual(self.app._mode, "sequence")
        self.assertIn("Sequence", self.app._menu_buttons)

    def test_new_recording_while_a_sequence_exists(self):
        self.make("Login")
        self.record_something(MOUSE_EVENTS)
        self.assertEqual(self.app._mode, "recording")
        self.app._dirty = False
        self.app.new_recording()
        self.assertEqual(self.app._mode, "sequence")             # falls back to the sequence

    def test_a_bad_round_is_a_status_not_a_crash(self):
        self.make("Login")
        self.app._sequence["steps"][0]["enabled"] = False
        self.play()
        self.assertEqual(self.app._state, "idle")
        self.assertIn("switched off", self.status)


class TestBoxesAndHotkeys(AppCase):
    """Hotkeys while a box waits for an answer (the box waits inside the inbox: see _drain_inbox)."""

    def box_that_waits(self, answer, during=None, seconds=0.5):
        """A Save changes? box like the real one: the event loop runs until it's answered."""
        shown = []

        def ask(detail="", what="recording"):
            shown.append(detail)
            if during is not None and len(shown) == 1:
                self.run_in_thread(during)
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                self.app.tk.dooneevent(tkinter._tkinter.DONT_WAIT)
                time.sleep(0.003)
            return answer
        self.dialogs.ask_save_changes = ask
        return shown

    def unsaved(self):
        self.record_something(MOUSE_EVENTS)
        self.assertTrue(self.app._dirty)

    def test_a_second_record_press_while_the_box_is_open_changes_nothing(self):
        # The reported bug: answering No then did nothing ("Cancelled."), Cancel showed it twice.
        for answer, state in (("no", "recording"), ("cancel", "idle")):
            with self.subTest(answer=answer):
                self.unsaved()
                starts = self.recorder.start_calls
                with mock.patch.object(app_module.ui, "bring_boxes_forward") as forward:
                    shown = self.box_that_waits(answer, during=lambda: self.hotkeys.press("record"))
                    self.hotkeys.press("record")
                    self.pump(lambda: bool(shown) and self.app._boxes_open == 0, timeout=3)
                    self.settle()
                    self.pump(timeout=0.2)
                self.assertEqual(len(shown), 1)                       # never a second box
                self.assertEqual(self.app._state, state)
                self.assertGreaterEqual(forward.call_count, 1)        # the press showed the box again
                if answer == "no":
                    self.assertEqual(self.recorder.start_calls, starts + 1)
                    self.app.stop()
                    self.pump(lambda: self.app._state == "idle")

    def test_panic_stop_and_playback_end_are_handled_while_a_box_is_open(self):
        self.unsaved()
        handled = []
        original = self.app.stop

        def stop(*args, **kwargs):
            handled.append(time.monotonic())
            return original(*args, **kwargs)
        self.app.stop = stop
        shown = self.box_that_waits("cancel", during=lambda: self.hotkeys.press("stop"), seconds=0.8)
        started = time.monotonic()
        self.hotkeys.press("record")
        self.pump(lambda: bool(shown) and self.app._boxes_open == 0, timeout=3)
        self.assertEqual(len(handled), 1)
        self.assertLess(handled[0] - started, 0.7)                     # while the box was still open

    def test_double_taps_count_once(self):
        self.app._double_press = 0.25
        now = [100.0]
        self.app._clock = lambda: now[0]
        self.app.on_hotkey("record")
        self.assertEqual(self.app._state, "recording")
        now[0] += 0.1
        self.app.on_hotkey("record")                                    # bounce: ignored
        self.assertEqual(self.app._state, "recording")
        now[0] += 0.3
        self.app.on_hotkey("record")                                    # a real second press: stop / cancel
        self.assertEqual(self.app._state, "idle")
        self.app.on_hotkey("stop")
        self.app.on_hotkey("stop")                                      # the panic stop is never held back

    def test_exit_while_a_box_is_open_shows_that_box_instead_of_a_second_one(self):
        self.unsaved()
        self.app._boxes_open = 1
        with mock.patch.object(app_module.ui, "bring_boxes_forward") as forward:
            self.tray.kw["on_exit"]()
        self.assertFalse(self.app._closed)
        forward.assert_called_once_with(self.app)
        self.assertEqual(self.dialogs.asked, [])
        self.app._boxes_open = 0

    def test_play_or_record_during_a_menu_box_brings_it_forward(self):
        with mock.patch.object(app_module.ui, "bring_boxes_forward") as forward:
            def answer(speed):
                self.app.on_hotkey("record")
                self.app.on_hotkey("play")
                return None
            self.dialogs.ask_speed = answer
            self.app.open_speed()
        self.assertEqual(forward.call_count, 2)
        self.assertEqual(self.app._state, "idle")


class TestHousekeeping(AppCase):
    """No slowdown the longer Wackro is open."""

    def tracked(self):
        from customtkinter.windows.widgets.scaling import ScalingTracker
        return [w for w in ScalingTracker.window_widgets_dict if w is not self.app]

    def test_closed_menus_and_boxes_are_forgotten(self):
        for _ in range(5):
            self.app.toggle_menu("Settings")
            self.app._close_menu()
            box = ui_dialogs.SaveChangesDialog(self.app)
            box.pick("cancel")
        self.assertEqual([w for w in self.tracked() if not w.winfo_exists()], [])

    def test_the_sweep_catches_anything_left(self):
        from customtkinter.windows.widgets.scaling import ScalingTracker
        stray = tkinter.Toplevel(self.app)
        ScalingTracker.window_widgets_dict[stray] = []
        ScalingTracker.window_dpi_scaling_dict[stray] = 1.0
        stray.destroy()
        self.assertGreaterEqual(ui_dialogs.sweep_dead_windows(), 2)
        self.assertNotIn(stray, ScalingTracker.window_widgets_dict)
        self.assertNotIn(stray, ScalingTracker.window_dpi_scaling_dict)
        self.assertIn(self.app, ScalingTracker.window_widgets_dict)       # the live window stays

    def test_the_tick_sweeps_every_so_often(self):
        with mock.patch.object(app_module.ui, "sweep_dead_windows", return_value=0) as sweep:
            self.app._next_sweep = 0
            self.app._tick()
            self.app._tick()
        self.assertEqual(sweep.call_count, 1)

    def test_labels_are_only_redrawn_when_their_text_changes(self):
        self.app._refresh()
        with mock.patch.object(self.app._status_label, "configure") as configure:
            for _ in range(20):
                self.app._refresh()
            self.assertEqual(configure.call_count, 0)
            self.app._set_status("Something new.")
            self.assertEqual(configure.call_count, 1)

    def test_idle_ticks_slow_down_busy_ticks_do_not(self):
        delays = []
        with mock.patch.object(self.app, "after", side_effect=lambda ms, fn: delays.append(ms) or "job"):
            self.app._tick()
            self.app._state = "recording"
            self.app._tick()
        self.app._state = "idle"
        self.assertEqual(delays, [app_module._IDLE_TICK_MS, app_module._TICK_MS])


class TestPlaybackSettings(AppCase):
    """Repeat / Speed now live in Playback > Repeat… / Speed… (dialogs) and settings.json."""

    def setUp(self):
        super().setUp()
        self.record_something()
        self.hide.reset_mock()
        self.deiconify.reset_mock()

    def play(self):
        self.app.start_playback()
        self.settle()

    def saved(self):
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def set_repeat(self, repeat=1, infinite=False, delay=0.0):
        self.dialogs.answers["repeat"] = {"repeat": repeat, "infinite": infinite, "delay": delay}
        self.app.open_repeat()

    def test_default_playback_is_a_single_run_at_normal_speed(self):
        self.play()
        self.assertEqual(self.player.start_options, [{"repeat": 1, "delay": 0.0, "speed": 1.0}])
        self.assertEqual(self.status, "Playing… press F11 or F10 to stop.")

    def test_repeat_dialog_gets_the_current_values_and_its_answer_reaches_the_player(self):
        self.set_repeat(5, False, 1.5)
        self.assertEqual(self.dialogs.asked[-1], ("repeat", 1, False, 0.0))
        self.play()
        self.assertEqual(self.player.start_options, [{"repeat": 5, "delay": 1.5, "speed": 1.0}])
        self.assertEqual(self.status, "Playing 5 runs… press F11 or F10 to stop.")

    def test_infinite_passes_zero(self):
        self.set_repeat(3, True, 2.0)
        self.play()
        self.assertEqual(self.player.start_options[-1]["repeat"], 0)
        self.assertEqual(self.status, "Playing on repeat… press F11 or F10 to stop.")

    def test_cancelled_repeat_dialog_changes_nothing(self):
        self.app.open_repeat()   # no answer = cancelled
        self.assertEqual(self.app._settings["repeat"], 1)
        self.assertFalse(self.settings_path.exists())

    def test_speed_dialog(self):
        self.dialogs.answers["speed"] = 2.5
        self.app.open_speed()
        self.assertEqual(self.dialogs.asked[-1], ("speed", 1.0))
        self.assertEqual(self.saved()["speed"], 2.5)
        self.play()
        self.assertEqual(self.player.start_options[-1]["speed"], 2.5)
        self.assertIn("2.5x speed", self.app._summary_label.cget("text"))

    def test_settings_are_saved_as_soon_as_they_change(self):
        self.set_repeat(9, True, 0.5)
        saved = self.saved()
        self.assertEqual((saved["repeat"], saved["infinite"], saved["delay"]), (9, True, 0.5))

    def test_the_hotkey_path_uses_the_same_settings(self):
        self.set_repeat(4)
        self.hotkeys.press("play")
        self.pump(lambda: self.app._state == "playing")
        self.settle()
        self.assertEqual(self.player.start_options[-1]["repeat"], 4)

    def test_dialogs_are_not_offered_while_busy(self):
        self.app.start_playback()
        self.assertFalse(self.menu_enabled("Playback", "Repeat"))
        self.assertFalse(self.menu_enabled("Playback", "Speed"))
        self.app.open_repeat()
        self.app.open_speed()
        self.assertEqual(self.dialogs.asked, [])
        self.settle()
        self.app.stop()

    def test_nothing_to_play_is_reported(self):
        self.app._events = []
        self.app.start_playback()
        self.assertIn("nothing to play", self.status)
        self.assertEqual(self.hide.call_count, 0)

    def test_info_shows_the_run_number(self):
        self.set_repeat(5)
        self.play()
        self.player.current_run = 2
        self.app._refresh()
        self.assertEqual(self.info, "▶ Run 2 / 5 · 3 / 10 events   0:02.0")

    def test_info_for_endless_playback_shows_infinity(self):
        self.set_repeat(1, True)
        self.play()
        self.player.current_run = 7
        self.app._refresh()
        self.assertEqual(self.info, "▶ Run 7 / ∞ · 3 / 10 events   0:02.0")

    def test_info_never_shows_run_zero(self):
        self.set_repeat(3)
        self.play()
        self.player.current_run = 0
        self.app._refresh()
        self.assertIn("Run 1 / 3", self.info)

    def test_the_players_finish_message_is_shown(self):
        self.set_repeat(3)
        self.play()
        self.player.finish_message = "Playback finished (3 runs)."
        self.player.is_playing = False
        self.app._handle_play_finished()
        self.assertEqual(self.status, "Playback finished (3 runs).")

    def test_a_failing_settings_write_never_blocks_closing_or_playing(self):
        with mock.patch.object(app_module, "save_settings", side_effect=OSError("disk full")):
            self.set_repeat(2)
            self.play()
            self.assertEqual(self.player.start_options[-1]["repeat"], 2)
            self.app.stop()
            self.app._dirty = False
            self.app.close()
        self.assertTrue(self.app._closed)


class TestRecordingOptions(AppCase):
    def test_defaults_record_everything(self):
        self.app.start_recording()
        self.settle()
        options = self.recorder.start_kwargs[-1]["options"]
        self.assertEqual(options, {"keyboard": True, "clicks": True, "moves": True, "delay": True})
        self.assertEqual(self.recorder.start_kwargs[-1]["hotkeys"], hotkey_spec.DEFAULT_BINDINGS)

    def test_dialog_answer_reaches_the_recorder_and_is_saved(self):
        self.dialogs.answers["record_options"] = {"keyboard": True, "clicks": True, "moves": False, "delay": False}
        self.app.open_record_options()
        self.assertEqual(self.dialogs.asked[-1][1], {"keyboard": True, "clicks": True, "moves": True, "delay": True})
        self.app.start_recording()
        self.settle()
        self.assertEqual(self.recorder.start_kwargs[-1]["options"],
                         {"keyboard": True, "clicks": True, "moves": False, "delay": False})
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertFalse(saved["record_moves"])
        self.assertIn("Recording: keys, clicks · no delay", self.app._summary_label.cget("text"))
        self.app.stop()


class TestHotkeyToggles(AppCase):
    """F9 toggles recording, F11 toggles playback, F10 stops anything (the defaults)."""

    def press(self, action):
        self.hotkeys.press(action)
        self.pump(timeout=0.05)

    def test_record_key_starts_and_stops_recording(self):
        self.press("record")
        self.assertEqual(self.app._state, "recording")
        self.settle()
        self.assertEqual(self.status, "Recording… press F9 to stop.")
        self.press("record")
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.recorder.stop_calls, [False])

    def test_stopping_with_ctrl_grave_drops_the_ctrl_press(self):
        self.app._settings["hotkeys"] = {a: dict(b, mods=list(b["mods"])) for a, b in
                                         {"record": {"mods": ["ctrl"], "vk": 0xC0},
                                          "play": {"mods": [], "vk": 0x90}, "stop": {"mods": [], "vk": 0x79}}.items()}
        self.press("record")
        self.settle()
        self.press("record")
        self.assertEqual(self.recorder.stop_modifiers, [{"ctrl"}])

    def test_panic_stop_has_no_modifiers_to_drop(self):
        self.press("record")
        self.settle()
        self.press("stop")
        self.assertEqual(self.recorder.stop_modifiers, [set()])

    def test_play_key_starts_and_stops_playback(self):
        self.record_something()
        self.press("play")
        self.assertEqual(self.app._state, "playing")
        self.settle()
        self.press("play")
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.player.stop_calls, 1)

    def test_play_key_is_ignored_while_recording_and_record_key_while_playing(self):
        self.press("record")
        self.settle()
        self.press("play")
        self.assertEqual(self.app._state, "recording")
        self.press("record")
        self.press("play")
        self.settle()
        self.press("record")
        self.assertEqual(self.app._state, "playing")
        self.press("stop")
        self.assertEqual(self.app._state, "idle")

    def test_record_key_during_the_start_delay_cancels(self):
        self.app._delay_ms = 400
        self.press("record")
        self.press("record")
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.status, "Cancelled.")

    def test_tray_menu_entries_do_the_same(self):
        self.tray.kw["on_record"]()
        self.assertEqual(self.app._state, "recording")
        self.settle()
        self.tray.kw["on_stop"]()
        self.assertEqual(self.app._state, "idle")
        self.tray.kw["on_play"]()
        self.assertEqual(self.app._state, "playing")
        self.settle()
        self.tray.kw["on_play"]()
        self.assertEqual(self.app._state, "idle")

    def test_record_and_play_wait_while_a_box_is_open_but_stop_works(self):
        self.record_something()
        seen = {}

        def speed_box(speed):
            self.app.on_hotkey("record")
            self.app.on_hotkey("play")
            seen["state"] = self.app._state
            return None

        self.dialogs.ask_speed = speed_box
        self.app.open_speed()
        self.assertEqual(seen["state"], "idle")
        self.assertEqual(self.app._boxes_open, 0)
        # the panic stop is never blocked
        self.app.start_playback()
        self.settle()
        self.app._boxes_open = 1
        self.app.on_hotkey("stop")
        self.app._boxes_open = 0
        self.assertEqual(self.app._state, "idle")

    def test_the_box_counter_survives_a_crashing_box(self):
        self.dialogs.ask_speed = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            self.app.open_speed()
        self.assertEqual(self.app._boxes_open, 0)

    def test_hotkeys_after_close_do_nothing(self):
        self.app.close()
        self.app.on_hotkey("record")
        self.assertEqual(self.recorder.start_calls, 0)


class TestSaveChangesBeforeRecording(AppCase):
    """Record over an unsaved recording: Save changes? Yes / No / Cancel (like ReMouse)."""

    def setUp(self):
        super().setUp()
        self.record_something(MOUSE_EVENTS)
        self.dialogs.asked.clear()
        self.hide.reset_mock()
        self.deiconify.reset_mock()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "first.json")

    def test_cancel_keeps_the_old_recording_and_records_nothing(self):
        self.dialogs.save_changes_answer = "cancel"
        self.app.start_recording()
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.app._events, MOUSE_EVENTS)
        self.assertTrue(self.app._dirty)
        self.assertEqual(self.recorder.start_calls, 1)       # only the one from setUp
        self.assertEqual(self.hide.call_count, 0)

    def test_no_throws_it_away_and_records(self):
        self.dialogs.save_changes_answer = "no"
        self.app.start_recording()
        self.assertEqual(self.app._state, "recording")
        self.assertEqual(self.dialogs.asked[0][0], "save_changes")
        self.settle()
        self.app.stop()

    def test_yes_the_first_time_opens_save_as_then_records(self):
        self.dialogs.save_changes_answer = "yes"
        self.dialogs.save_to = self.path
        self.app.start_recording()
        self.assertEqual(self.dialogs.save_asked, 1)
        self.assertEqual(storage.load_recording(self.path)["events"], MOUSE_EVENTS)
        self.assertEqual(self.app._state, "recording")
        self.settle()
        self.app.stop()

    def test_yes_with_a_known_file_saves_there_without_asking(self):
        self.dialogs.save_to = self.path
        self.app.save()
        self.app._dirty = True                              # changed again since
        self.dialogs.save_changes_answer = "yes"
        self.app.start_recording()
        self.assertEqual(self.dialogs.save_asked, 1)        # no second Save As
        self.assertEqual(self.app._state, "recording")
        self.settle()
        self.app.stop()

    def test_yes_but_the_save_dialog_is_cancelled_records_nothing(self):
        self.dialogs.save_changes_answer = "yes"
        self.dialogs.save_to = None
        self.app.start_recording()
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.app._events, MOUSE_EVENTS)

    def test_a_saved_recording_is_replaced_without_asking(self):
        self.dialogs.save_to = self.path
        self.app.save()
        self.dialogs.asked.clear()
        self.app.start_recording()
        self.assertEqual(self.dialogs.asked, [])
        self.assertEqual(self.app._state, "recording")
        self.settle()
        self.app.stop()

    def test_from_the_tray_only_the_question_appears_not_the_window(self):
        for answer in ("cancel", "no"):
            with self.subTest(answer=answer):
                self.app._state = "idle"
                self.app._dirty = True
                self.dialogs.asked.clear()
                self.deiconify.reset_mock()
                self.dialogs.save_changes_answer = answer
                with mock.patch.object(self.app, "_window_visible", return_value=False):
                    self.hotkeys.press("record")
                    self.pump(lambda: bool(self.dialogs.asked), timeout=1)
                    self.assertEqual(self.dialogs.asked[0][0], "save_changes")
                    self.assertEqual(self.deiconify.call_count, 0)      # the window stays hidden
                    if answer == "no":
                        self.assertEqual(self.app._state, "recording")
                        self.assertFalse(self.app._restore_after)       # and stays hidden after
                        self.settle()
                        self.app.stop()
                self.assertEqual(self.deiconify.call_count, 0)

    def test_tray_exit_with_an_unsaved_recording_asks_without_showing_the_window(self):
        self.dialogs.save_changes_answer = "cancel"
        with mock.patch.object(self.app, "_window_visible", return_value=False):
            self.tray.kw["on_exit"]()
        self.assertFalse(self.app._closed)
        self.assertEqual(self.deiconify.call_count, 0)

    def test_play_hotkey_is_ignored_while_the_question_is_open(self):
        def answer(detail):
            self.app.on_hotkey("play")
            self.assertEqual(self.app._state, "idle")
            return "cancel"
        self.dialogs.ask_save_changes = answer
        self.app.start_recording()
        self.assertEqual(self.app._state, "idle")

    def test_new_asks_too(self):
        self.dialogs.save_changes_answer = "cancel"
        self.app.new_recording()
        self.assertEqual(self.app._events, MOUSE_EVENTS)
        self.dialogs.save_changes_answer = "no"
        self.app.new_recording()
        self.assertEqual(self.app._events, [])
        self.assertEqual(self.status, "Ready.")


class TestIndicator(AppCase):
    """The on-screen sign: red while recording, green while playing, gone when idle."""

    def setUp(self):
        super().setUp()
        self.overlay = self.app._overlay

    def test_prepared_at_startup_and_hidden_when_idle(self):
        self.assertEqual(self.overlay.prepared, 1)
        self.assertEqual(self.overlay.shown, [])

    def test_recording_shows_it_at_once_then_the_timer_runs(self):
        self.app.start_recording()
        self.assertEqual(self.overlay.shown[-1], ("recording", "● REC", "both"))   # while starting
        self.settle()
        self.app._refresh()
        self.assertEqual(self.overlay.texts[-1], "● REC 0:01")                     # fake elapsed 1.5 s
        self.app.stop()
        self.assertIsNone(self.overlay.state)

    def test_playing_is_green_with_runs(self):
        self.record_something()
        self.app._settings.update(repeat=5)
        self.app.start_playback()
        self.settle()
        self.player.current_run = 2
        self.app._refresh()
        self.assertEqual(self.overlay.state, "playing")
        self.assertEqual(self.overlay.texts[-1], "▶ Run 2/5 · 0:02")
        self.app.stop()
        self.assertIsNone(self.overlay.state)

    def test_playback_indicator_can_be_turned_off(self):
        self.menu_command("Settings", "Indicator while playing")()
        self.assertFalse(self.app._settings["indicator_playback"])
        self.record_something()
        self.overlay.shown.clear()
        self.app.start_playback()
        self.settle()
        self.assertEqual(self.overlay.shown, [])
        self.app.stop()

    def test_styles_from_the_menu(self):
        for label, style in (("Indicator: Border only", "border"), ("Indicator: REC pill only", "pill"),
                             ("Indicator: Border + REC pill", "both")):
            with self.subTest(style=style):
                self.menu_command("Settings", label)()
                self.assertEqual(self.app._settings["indicator"], style)
                self.app.start_recording()
                self.assertEqual(self.overlay.shown[-1][2], style)
                self.app.stop()
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["indicator"], "both")

    def test_off_shows_nothing(self):
        self.menu_command("Settings", "Indicator: Off")()
        self.app.start_recording()
        self.assertEqual(self.overlay.shown, [])
        self.app.stop()

    def test_changing_the_style_while_recording_redraws_it(self):
        self.app.start_recording()
        self.app.set_indicator("pill")
        self.assertEqual(self.overlay.shown[-1][2], "pill")
        self.app.stop()

    def test_exit_removes_it(self):
        self.app.exit_app()
        self.assertEqual(self.overlay.destroyed, 1)

    def test_a_crashing_overlay_never_breaks_recording(self):
        self.overlay.show = mock.Mock(side_effect=RuntimeError("tk"))
        self.app.start_recording()
        self.assertEqual(self.app._state, "recording")
        self.app.stop()


class TestNativeDialogOwner(AppCase):
    """Native boxes (Save As, warnings) get an invisible topmost owner while the app is hidden."""

    def setUp(self):
        super().setUp()
        self.win = FakeForegroundWin()
        self.real = app_module.Dialogs(self.app, win32=self.win)

    def test_visible_window_owns_its_boxes(self):
        with mock.patch.object(app_module.ui, "parent_hidden", return_value=False), \
                mock.patch.object(app_module.messagebox, "askokcancel", return_value=True) as ask:
            self.assertTrue(self.real.confirm("t", "m"))
        self.assertIs(ask.call_args.kwargs["parent"], self.app)
        self.assertEqual(self.win.forced, [])

    def test_hidden_window_uses_a_topmost_anchor_and_gives_the_focus_back(self):
        seen = {}

        def ask(*args, parent=None, **kwargs):
            seen["parent"] = parent
            parent.update()                     # X11 only reports -topmost once it's mapped
            seen["topmost"] = bool(int(parent.attributes("-topmost")))
            seen["alive"] = bool(parent.winfo_exists())
            return False

        with mock.patch.object(app_module.messagebox, "askokcancel", side_effect=ask):
            self.assertFalse(self.real.confirm("t", "m"))   # self.app is withdrawn in these tests
        self.assertIsNot(seen["parent"], self.app)
        self.assertTrue(seen["topmost"])
        self.assertTrue(seen["alive"])
        self.assertFalse(seen["parent"].winfo_exists())     # cleaned up afterwards
        self.assertEqual(len(self.win.forced), 1)
        self.assertEqual(self.win.restored, [self.win.PREVIOUS])

    def test_the_anchor_is_removed_even_if_the_box_crashes(self):
        with mock.patch.object(app_module.filedialog, "asksaveasfilename", side_effect=RuntimeError("tk")):
            with self.assertRaises(RuntimeError):
                self.real.save_path()
        self.assertEqual(self.win.restored, [self.win.PREVIOUS])
        self.assertEqual([w for w in self.app.winfo_children() if isinstance(w, tkinter.Toplevel)
                          and not isinstance(w, customtkinter.CTkToplevel) and w.winfo_exists()
                          and w is not getattr(self.app, "_menu", None)
                          and w not in getattr(self.app._overlay, "_strips", [])
                          and w is not getattr(self.app._overlay, "_pill", None)], [])


class FakeForegroundWin:
    PREVIOUS = 4242

    def __init__(self):
        self.forced = []
        self.restored = []

    def monitors(self):
        import winutil
        return [winutil.Monitor(0, 0, 1024, 768, True)]

    def force_foreground(self, hwnd):
        self.forced.append(hwnd)
        return self.PREVIOUS

    def restore_foreground(self, previous):
        self.restored.append(previous)


class TestHiding(AppCase):
    def test_tray_state_follows_the_app(self):
        self.app.start_recording()
        self.assertEqual(self.tray.states[-1], "recording")
        self.settle()
        self.app.stop()
        self.assertEqual(self.tray.states[-1], "idle")
        self.app.start_playback()
        self.assertEqual(self.tray.states[-1], "playing")
        self.app.stop()

    def test_with_a_tray_the_window_is_withdrawn_not_minimized(self):
        self.app.start_recording()
        self.assertEqual(self.hide.call_count, 1)
        self.assertEqual(self.iconify.call_count, 0)
        self.app.stop()

    def test_without_a_tray_the_window_is_minimized(self):
        self.app._tray_ok = False
        self.app.start_recording()
        self.assertEqual(self.iconify.call_count, 1)
        self.assertEqual(self.hide.call_count, 0)
        self.app.stop()

    def test_a_window_that_was_hidden_stays_hidden_afterwards(self):
        with mock.patch.object(self.app, "_window_visible", return_value=False):
            self.app.start_recording()
            self.settle()
            self.app.stop()
        self.assertEqual(self.deiconify.call_count, 0)
        self.assertEqual(self.app._state, "idle")

    def test_hide_to_tray_and_show(self):
        self.app.hide_to_tray()
        self.assertEqual(self.hide.call_count, 1)
        self.tray.kw["on_show"]()
        self.assertEqual(self.deiconify.call_count, 1)

    def test_a_tray_that_wont_start_falls_back(self):
        self.app.close()
        FakeTray.works = False
        try:
            app = App(recorder_factory=FakeRecorder, player_factory=FakePlayer, hotkeys_factory=FakeHotkeys,
                      tray_factory=FakeTray, overlay_factory=FakeOverlay, watch_factory=FakeWatch, blackout_factory=FakeBlackout, dimmer=FakeDimmer(), dialogs=FakeDialogs(), delay=0.0,
                      settings_path=temp_settings_path(self))
        finally:
            FakeTray.works = True
        try:
            app.withdraw()
            self.assertFalse(app._tray_ok)
            self.assertFalse(dict((i[0].strip(" ✓"), i[2] if len(i) > 2 else True)
                                  for i in app.menu_items("Settings") if i)["Close button hides to tray"])
        finally:
            app.exit_app()


class TestHotkeySettings(AppCase):
    NEW = {"record": {"mods": ["ctrl", "shift"], "vk": 0x52}, "play": {"mods": [], "vk": 0x7A},
           "stop": {"mods": [], "vk": 0x7B}}

    def test_hotkeys_are_off_while_the_box_is_open(self):
        def check(bindings):
            self.assertEqual(self.hotkeys.enabled_calls, [False])
            return None
        self.dialogs.ask_hotkeys = check
        self.app.open_hotkeys()
        self.assertEqual(self.hotkeys.enabled_calls, [False, True])

    def test_new_hotkeys_apply_everywhere(self):
        self.dialogs.answers["hotkeys"] = self.NEW
        self.app.open_hotkeys()
        self.assertEqual(self.hotkeys.bindings, self.NEW)
        self.assertEqual(self.app._key_labels["record"].cget("text"), "Ctrl+Shift+R")
        self.assertEqual(self.app._key_labels["play"].cget("text"), "F11")
        self.assertEqual(self.tray.labels["stop"], "F12")
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["hotkeys"], self.NEW)
        self.app.start_recording()
        self.settle()
        self.assertEqual(self.recorder.start_kwargs[-1]["hotkeys"], self.NEW)
        self.app.on_hotkey("record")
        self.assertEqual(self.recorder.stop_modifiers[-1], {"ctrl", "shift"})

    def test_cancel_keeps_the_old_ones(self):
        self.app.open_hotkeys()
        self.assertEqual(self.hotkeys.bindings, hotkey_spec.DEFAULT_BINDINGS)
        self.assertEqual(self.hotkeys.enabled_calls, [False, True])

    def test_a_crashing_box_still_turns_the_hotkeys_back_on(self):
        self.dialogs.ask_hotkeys = mock.Mock(side_effect=RuntimeError("tk died"))
        with self.assertRaises(RuntimeError):
            self.app.open_hotkeys()
        self.assertEqual(self.hotkeys.enabled_calls, [False, True])


class TestMenus(AppCase):
    def test_every_menu_has_items(self):
        for name in self.app._menu_buttons:
            with self.subTest(menu=name):
                items = [i for i in self.app.menu_items(name) if i is not None]
                self.assertTrue(items)
                for item in items:
                    self.assertTrue(callable(item[1]))

    def test_menu_opens_and_closes(self):
        self.app.toggle_menu("File")
        menu = self.app._menu
        self.assertIsNotNone(menu)
        self.assertIn("Save As…", menu.item_buttons)
        self.app.toggle_menu("File")
        self.assertIsNone(self.app._menu)

    def test_opening_another_menu_closes_the_first(self):
        self.app.toggle_menu("File")
        first = self.app._menu
        self.app.toggle_menu("Help")
        self.assertIsNot(self.app._menu, first)
        self.assertFalse(first.winfo_exists())
        self.app._close_menu()

    def test_choosing_an_item_runs_it_after_closing(self):
        calls = []
        with mock.patch.object(self.app, "show_about", lambda: calls.append("about")):
            self.app.toggle_menu("Help")
            self.app._menu.item_buttons[f"About {config.APP_NAME}"].invoke()
            self.pump(lambda: calls == ["about"], timeout=1)
        self.assertEqual(calls, ["about"])
        self.assertIsNone(self.app._menu)

    def test_the_menu_is_a_plain_toplevel_not_a_ctk_one(self):
        # Regression: CTkToplevel hands the focus back to the main window on Windows 10 ms after
        # opening, and the old focus-based menu closed itself straight away.
        self.app.toggle_menu("File")
        self.assertNotIsInstance(self.app._menu, customtkinter.CTkToplevel)
        self.app._close_menu()

    def test_a_menu_survives_losing_the_focus_inside_the_app(self):
        self.app.toggle_menu("Playback")
        menu = self.app._menu
        self.pump(timeout=0.3)            # several ticks, whatever the focus does
        self.assertIs(self.app._menu, menu)
        self.app._close_menu()

    def test_a_click_elsewhere_closes_it_a_click_inside_doesnt(self):
        self.app.toggle_menu("File")
        menu = self.app._menu
        inside = list(menu.item_buttons.values())[0]
        self.app._close_menu_on_click(mock.Mock(widget=inside))
        self.assertIs(self.app._menu, menu)
        self.app._close_menu_on_click(mock.Mock(widget=self.app._menu_buttons["File"]))
        self.assertIs(self.app._menu, menu)          # the bar button toggles it itself
        self.app._close_menu_on_click(mock.Mock(widget=self.app._status_label))
        self.assertIsNone(self.app._menu)

    def test_switching_to_another_program_closes_it(self):
        self.app.toggle_menu("Help")
        with mock.patch.object(self.app, "focus_displayof", return_value=self.app):
            self.app._watch_menu()                   # the app had the focus...
        self.assertIsNotNone(self.app._menu)
        with mock.patch.object(self.app, "focus_displayof", return_value=None):
            self.app._watch_menu()                   # ...and lost it
        self.assertIsNone(self.app._menu)

    def test_never_having_had_the_focus_doesnt_close_it(self):
        self.app.toggle_menu("Help")
        with mock.patch.object(self.app, "focus_displayof", return_value=None):
            self.app._watch_menu()
        self.assertIsNotNone(self.app._menu)
        self.app._close_menu()

    def test_escape_is_bound_on_the_window(self):
        # A hidden test window can't receive key presses, so check the binding is there.
        self.assertTrue(self.app.bind("<Escape>"))

    def test_disabled_items_are_greyed_out(self):
        self.app.toggle_menu("File")
        self.assertEqual(self.app._menu.item_buttons["Save"].cget("state"), "disabled")
        self.app._close_menu()

    def test_hiding_closes_the_menu(self):
        self.app.toggle_menu("File")
        self.app.start_recording()
        self.assertIsNone(self.app._menu)
        self.app.stop()

    def test_about_mentions_version_credit_hotkeys_and_privacy(self):
        self.app.show_about()
        _, title, heading, text = self.dialogs.asked[-1]
        self.assertIn(config.APP_VERSION, text)
        self.assertIn("Trixian Wackyll Granado", text)
        self.assertIn("F9", text)
        self.assertIn("passwords", text)

    def test_open_log_folder_never_raises(self):
        with mock.patch.object(app_module.subprocess, "Popen", side_effect=OSError("no xdg-open")), \
                mock.patch.object(app_module.os, "startfile", side_effect=OSError("nope"), create=True):
            self.app.open_log_folder()
        self.assertIn(str(config.LOG_DIR), self.status)

    def test_close_action_items(self):
        self.menu_command("Settings", "Close button exits")()
        self.assertEqual(self.app._settings["close_action"], "exit")
        labels = {i[0].strip(" ✓"): i[0].startswith("✓") for i in self.app.menu_items("Settings") if i}
        self.assertTrue(labels["Close button exits"])
        self.assertFalse(labels["Close button asks"])


class TestSettingsStartup(unittest.TestCase):
    def make_app(self, path):
        try:
            window = App(
                recorder_factory=FakeRecorder, player_factory=FakePlayer, hotkeys_factory=FakeHotkeys,
                tray_factory=FakeTray, overlay_factory=FakeOverlay, watch_factory=FakeWatch, blackout_factory=FakeBlackout, dimmer=FakeDimmer(), dialogs=FakeDialogs(), delay=0.0, settings_path=path,
            )
        except tkinter.TclError as error:
            self.skipTest(f"no display: {error}")
        window.withdraw()
        self.addCleanup(window.exit_app)
        return window

    def test_remembered_settings_are_restored(self):
        path = temp_settings_path(self)
        path.write_text(json.dumps({"repeat": 25, "infinite": False, "delay": 1.25, "speed": 3,
                                    "hotkeys": {"play": {"vk": 0x7A, "mods": []}}}), encoding="utf-8")
        window = self.make_app(path)
        self.assertEqual(window._summary_label.cget("text"), "Repeat 25× · wait 1.25 s · 3x speed")
        self.assertEqual(window._key_labels["play"].cget("text"), "F11")
        self.assertEqual(window._hotkeys.kw["bindings"]["play"], {"mods": [], "vk": 0x7A})

    def test_a_corrupt_settings_file_gives_the_defaults(self):
        path = temp_settings_path(self)
        path.write_text("{{{ not json", encoding="utf-8")
        window = self.make_app(path)
        self.assertEqual(window._settings, settings_module.default_settings())

    def test_settings_survive_a_restart(self):
        path = temp_settings_path(self)
        first = self.make_app(path)
        first._dialogs.answers["speed"] = 4.0
        first.open_speed()
        second = self.make_app(path)
        self.assertEqual(second._settings["speed"], 4.0)


class TestSaveLoad(AppCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "r.json")

    def test_save_does_nothing_without_a_recording(self):
        self.app.save()
        self.assertEqual(self.dialogs.save_asked, 0)

    def test_save_mouse_only_recording_roundtrips_without_a_warning(self):
        self.record_something(MOUSE_EVENTS)
        self.dialogs.save_to = self.path
        self.app.save()
        self.assertEqual(self.dialogs.confirms, [])
        loaded = storage.load_recording(self.path)
        self.assertEqual(loaded["events"], MOUSE_EVENTS)
        self.assertEqual(loaded["screen"]["width"], self.app.winfo_screenwidth())
        self.assertFalse(self.app._dirty)
        self.assertEqual(self.status, "Saved to r.json.")
        self.assertNotIn("not saved", self.info)

    def test_save_with_keystrokes_warns_first(self):
        self.record_something(KEY_EVENTS)
        self.dialogs.save_to = self.path
        self.app.save()
        self.assertEqual(len(self.dialogs.confirms), 1)
        self.assertIn("passwords", self.dialogs.confirms[0][1])
        self.assertTrue(Path(self.path).exists())

    def test_declining_the_keystroke_warning_saves_nothing(self):
        self.record_something(KEY_EVENTS)
        self.dialogs.save_to = self.path
        self.dialogs.confirm_answer = False
        self.app.save()
        self.assertEqual(self.dialogs.save_asked, 0)
        self.assertFalse(Path(self.path).exists())
        self.assertTrue(self.app._dirty)

    def test_cancelling_the_file_dialog_saves_nothing(self):
        self.record_something(MOUSE_EVENTS)
        self.dialogs.save_to = None
        self.app.save()
        self.assertTrue(self.app._dirty)
        self.assertEqual(self.dialogs.errors, [])

    def test_save_failure_shows_the_error_and_stays_unsaved(self):
        self.record_something(MOUSE_EVENTS)
        self.dialogs.save_to = str(Path(self.tmp.name) / "missing-folder" / "r.json")
        self.app.save()
        self.assertEqual(len(self.dialogs.errors), 1)
        self.assertIn("Couldn't save", self.status)
        self.assertTrue(self.app._dirty)

    def test_load_replaces_the_recording(self):
        storage.save_recording(self.path, storage.make_recording(
            KEY_EVENTS, self.app.winfo_screenwidth(), self.app.winfo_screenheight()))
        self.dialogs.open_from = self.path
        self.app.load()
        self.assertEqual(self.app._events, KEY_EVENTS)
        self.assertFalse(self.app._dirty)
        self.assertEqual(self.status, "Loaded 5 events from r.json.")
        self.assertEqual(self.states()["play"], "normal")

    def test_load_over_an_unsaved_recording_marks_the_new_one_as_saved(self):
        self.record_something(MOUSE_EVENTS)
        storage.save_recording(self.path, storage.make_recording(
            KEY_EVENTS, self.app.winfo_screenwidth(), self.app.winfo_screenheight()))
        self.dialogs.open_from = self.path
        self.app.load()                      # confirm_answer is True: user accepts losing the old one
        self.assertFalse(self.app._dirty)
        self.assertNotIn("not saved", self.info)

    def test_load_warns_when_the_screen_size_differs(self):
        w, h = self.app.winfo_screenwidth() + 100, self.app.winfo_screenheight() + 100
        storage.save_recording(self.path, storage.make_recording(MOUSE_EVENTS, w, h))
        self.dialogs.open_from = self.path
        self.app.load()
        self.assertIn("clicks may land in the wrong place", self.status)
        self.assertIn(f"{w}×{h}", self.status)

    def test_load_with_matching_screen_has_no_warning(self):
        storage.save_recording(self.path, storage.make_recording(
            MOUSE_EVENTS, self.app.winfo_screenwidth(), self.app.winfo_screenheight()))
        self.dialogs.open_from = self.path
        self.app.load()
        self.assertNotIn("wrong place", self.status)

    def test_bad_file_shows_the_error_and_keeps_the_current_recording(self):
        self.record_something(MOUSE_EVENTS)
        Path(self.path).write_text("this is not json", encoding="utf-8")
        self.dialogs.open_from = self.path
        self.app.load()
        self.assertEqual(len(self.dialogs.errors), 1)
        self.assertEqual(self.app._events, MOUSE_EVENTS)
        self.assertTrue(self.app._dirty)   # the recording we still hold is still unsaved

    def test_cancelling_the_open_dialog_changes_nothing(self):
        self.record_something(MOUSE_EVENTS)
        self.dialogs.open_from = None
        self.dialogs.confirm_answer = True
        self.app.load()
        self.assertEqual(self.app._events, MOUSE_EVENTS)

    def test_load_over_an_unsaved_recording_asks_first(self):
        # Since 1.7 the file is picked first (it may be a recording or a sequence, and only the
        # kind it replaces is asked about); Cancel then keeps everything as it was.
        storage.save_recording(self.path, storage.make_recording(KEY_EVENTS, 100, 100))
        self.record_something(MOUSE_EVENTS)
        self.dialogs.save_changes_answer = "cancel"
        self.dialogs.open_from = self.path
        self.app.load()
        self.assertEqual([a[0] for a in self.dialogs.asked], ["save_changes"])
        self.assertEqual(self.app._events, MOUSE_EVENTS)
        self.assertTrue(self.app._dirty)

    def test_load_over_a_saved_recording_does_not_ask(self):
        storage.save_recording(self.path, storage.make_recording(MOUSE_EVENTS, 100, 100))
        self.dialogs.open_from = self.path
        self.app.load()
        self.app.load()
        self.assertEqual(self.dialogs.confirms, [])
        self.assertEqual(self.dialogs.asked, [])

    def test_save_and_load_are_ignored_while_recording_or_playing(self):
        self.record_something(MOUSE_EVENTS)
        self.dialogs.save_to = self.path
        self.dialogs.open_from = self.path
        self.app.start_playback()
        self.app.save()
        self.app.load()
        self.assertEqual(self.dialogs.save_asked, 0)
        self.assertEqual(self.dialogs.open_asked, 0)


class TestClose(AppCase):
    def test_close_asks_exit_tray_or_cancel(self):
        self.dialogs.close_answer = ("cancel", False)
        self.app.close()
        self.assertFalse(self.app._closed)
        self.assertEqual(self.dialogs.asked[-1], ("close_choice", True))

    def test_close_to_tray_hides_and_keeps_running(self):
        self.dialogs.close_answer = ("tray", False)
        self.app.close()
        self.assertFalse(self.app._closed)
        self.assertEqual(self.hide.call_count, 1)
        self.assertEqual(self.hotkeys.stopped, 0)

    def test_dont_ask_again_is_remembered(self):
        self.dialogs.close_answer = ("tray", True)
        self.app.close()
        self.assertEqual(self.app._settings["close_action"], "tray")
        self.app.close()                       # no question this time
        self.assertEqual(len([a for a in self.dialogs.asked if a[0] == "close_choice"]), 1)
        self.assertEqual(self.hide.call_count, 2)

    def test_close_set_to_tray_without_a_tray_asks_instead(self):
        self.app._settings["close_action"] = "tray"
        self.app._tray_ok = False
        self.dialogs.close_answer = ("cancel", False)
        self.app.close()
        self.assertEqual(self.dialogs.asked[-1], ("close_choice", False))

    def test_close_when_idle_and_clean_just_closes(self):
        self.app.close()
        self.assertTrue(self.app._closed)
        self.assertEqual(self.hotkeys.stopped, 1)
        self.assertEqual(self.tray.stopped, 1)
        self.assertEqual(self.dialogs.confirms, [])

    def test_close_with_unsaved_recording_asks_and_can_be_cancelled(self):
        self.record_something()
        self.dialogs.save_changes_answer = "cancel"
        self.app.close()
        self.assertFalse(self.app._closed)
        self.assertEqual(self.hotkeys.stopped, 0)
        self.assertEqual(self.dialogs.asked[-1][0], "save_changes")

    def test_close_with_unsaved_recording_yes_saves_then_quits(self):
        self.record_something()
        path = str(Path(tempfile.mkdtemp()) / "keep.json")
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        self.dialogs.save_changes_answer = "yes"
        self.dialogs.save_to = path
        self.app.close()
        self.assertTrue(self.app._closed)
        self.assertEqual(storage.load_recording(path)["events"], MOUSE_EVENTS)

    def test_quitting_while_playing_still_asks_about_an_unsaved_recording(self):
        self.record_something()
        self.app.start_playback()
        self.settle()
        self.dialogs.save_changes_answer = "cancel"
        self.app.exit_app()
        self.assertFalse(self.app._closed)
        self.assertEqual(self.dialogs.asked[-1][0], "save_changes")
        self.app.stop()

    def test_close_with_unsaved_recording_confirmed_closes(self):
        self.record_something()
        self.app.close()
        self.assertTrue(self.app._closed)
        self.assertEqual(self.hotkeys.stopped, 1)

    def test_tray_exit_is_a_real_exit(self):
        self.tray.kw["on_exit"]()
        self.assertTrue(self.app._closed)
        self.assertEqual(self.dialogs.asked, [])     # no Exit / Tray question

    def test_close_while_recording_stops_the_recorder(self):
        self.app.start_recording()
        self.settle()
        self.app.close()
        self.assertEqual(self.recorder.stop_calls, [False])
        self.assertTrue(self.app._closed)

    def test_close_while_playing_stops_the_player(self):
        self.record_something()
        self.dialogs.confirm_answer = True
        self.app.start_playback()
        self.settle()
        self.app.close()
        self.assertEqual(self.player.stop_calls, 1)

    def test_close_while_waiting_cancels_the_pending_start(self):
        self.app.start_recording()
        self.app.close()
        self.pump(timeout=self.DELAY * 4)
        self.assertEqual(self.recorder.start_calls, 0)

    def test_closing_saves_the_settings(self):
        self.app._settings["repeat"] = 6
        self.app.close()
        self.assertEqual(json.loads(self.settings_path.read_text(encoding="utf-8"))["repeat"], 6)

    def test_hop_after_close_does_nothing(self):
        self.app.close()
        self.app._hop(lambda: self.fail("must not run"))   # must not raise either


@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI libraries unavailable: {_IMPORT_ERROR}")
class TestIcon(AppCase):
    """The window icon comes from assets/Wackro.ico and must never be able to break start-up."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ico = Path(self._tmp.name) / "Wackro.ico"
        # Build the window as if assets/ had no icon. Otherwise, once a real Wackro.ico is in the
        # project, the window's own start-up timer would call iconbitmap too and these tests would
        # see two calls (one with the real icon) instead of only the one they set up.
        missing = Path(self._tmp.name) / "missing.ico"
        with mock.patch.object(app_module, "resource_path", return_value=missing):
            super().setUp()
        self.iconbitmap = mock.patch.object(self.app, "iconbitmap").start()

    def with_icon_file(self):
        self.ico.write_bytes(b"not a real icon, only is_file() matters")
        return mock.patch.object(app_module, "resource_path", return_value=self.ico)

    def test_looks_for_the_configured_icon(self):
        with mock.patch.object(app_module, "resource_path", return_value=self.ico) as resource_path:
            self.app._set_icon()
        resource_path.assert_called_once_with(config.ICON_RELATIVE_PATH)

    def test_missing_file_schedules_nothing(self):
        with mock.patch.object(app_module, "resource_path", return_value=self.ico):  # file not created
            with mock.patch.object(self.app, "after") as after:
                self.app._set_icon()
        after.assert_not_called()
        self.iconbitmap.assert_not_called()

    def test_icon_is_scheduled_after_a_delay_not_immediately(self):
        # CustomTkinter sets its own icon ~100 ms after the window shows (Windows); ours must come later.
        with self.with_icon_file(), mock.patch.object(self.app, "after") as after:
            self.app._set_icon()
        after.assert_called_once()
        self.assertGreaterEqual(after.call_args[0][0], 150)

    def ours(self):
        """Calls that set OUR icon. On Windows CustomTkinter also calls iconbitmap() with its own
        default icon ~200 ms after a window opens; those calls are not ours."""
        return [c for c in self.iconbitmap.call_args_list if c == mock.call(str(self.ico))]

    def test_icon_is_applied_shortly_after_start(self):
        with self.with_icon_file():
            self.app._set_icon()
        self.assertEqual(self.ours(), [])    # CTk sets its own icon first; ours must come later
        self.assertTrue(self.pump(lambda: bool(self.ours()), timeout=2.0))
        self.assertEqual(len(self.ours()), 1)

    def test_a_failing_iconbitmap_is_swallowed(self):
        self.iconbitmap.side_effect = tkinter.TclError("bitmap not supported")
        with self.with_icon_file():
            self.app._set_icon()
        self.assertTrue(self.pump(lambda: self.iconbitmap.called, timeout=2.0))
        self.assertEqual(self.app._state, "idle")  # the app carries on

    def test_not_applied_once_the_window_is_closing(self):
        with self.with_icon_file():
            self.app._set_icon()
        self.app._closed = True
        try:
            self.pump(timeout=0.5)
        finally:
            self.app._closed = False  # so the test cleanup destroys the window
        self.assertEqual(self.ours(), [])

    def test_a_failing_after_is_swallowed(self):
        with self.with_icon_file():
            with mock.patch.object(self.app, "after", side_effect=RuntimeError("no loop")):
                self.app._set_icon()  # must not raise


@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI libraries unavailable: {_IMPORT_ERROR}")
class TestIconWiring(unittest.TestCase):
    def test_the_window_sets_its_icon_while_starting_up(self):
        with mock.patch.object(App, "_set_icon", autospec=True) as set_icon:
            try:
                window = App(
                    recorder_factory=FakeRecorder, player_factory=FakePlayer,
                    hotkeys_factory=FakeHotkeys, tray_factory=FakeTray, overlay_factory=FakeOverlay, watch_factory=FakeWatch, blackout_factory=FakeBlackout, dimmer=FakeDimmer(), dialogs=FakeDialogs(), delay=0.03,
                    settings_path=temp_settings_path(self),
                )
            except tkinter.TclError as error:
                self.skipTest(f"no display: {error}")
        try:
            set_icon.assert_called_once_with(window)
        finally:
            window.tk.eval("proc bgerror {args} {}")
            window._closed = True
            window._cancel_pending()
            if window._tick_job is not None:
                window.after_cancel(window._tick_job)
            window.destroy()


class TestMain(unittest.TestCase):
    def test_enable_dpi_awareness_is_a_noop_off_windows(self):
        import main
        with mock.patch.object(main.sys, "platform", "linux"):
            main.enable_dpi_awareness()   # must not touch ctypes.windll

    def test_setup_logging_writes_to_the_log_file(self):
        import logging
        import main
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            root = logging.getLogger()
            before = list(root.handlers)
            old_hook, old_thread_hook, old_level = main.sys.excepthook, main.threading.excepthook, root.level
            try:
                with mock.patch.object(main, "LOG_DIR", log_dir), \
                        mock.patch.object(main, "LOG_PATH", log_dir / "x.log"):
                    main.setup_logging()
                logging.getLogger("t").error("hello log")
                for handler in root.handlers:
                    handler.flush()
                self.assertIn("hello log", (log_dir / "x.log").read_text(encoding="utf-8"))
            finally:
                for handler in list(root.handlers):
                    if handler not in before:
                        root.removeHandler(handler)
                        handler.close()
                root.setLevel(old_level)
                main.sys.excepthook, main.threading.excepthook = old_hook, old_thread_hook

    def test_setup_logging_falls_back_when_the_folder_is_unusable(self):
        import logging
        import main
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            root = logging.getLogger()
            before = list(root.handlers)
            old_hook, old_thread_hook, old_level = main.sys.excepthook, main.threading.excepthook, root.level
            try:
                with mock.patch.object(main, "LOG_DIR", blocker / "sub"), \
                        mock.patch.object(main, "LOG_PATH", blocker / "sub" / "x.log"):
                    main.setup_logging()   # must not raise
                added = [h for h in root.handlers if h not in before]
                self.assertEqual(len(added), 1)
                self.assertIsInstance(added[0], logging.StreamHandler)
            finally:
                for handler in list(root.handlers):
                    if handler not in before:
                        root.removeHandler(handler)
                        handler.close()
                root.setLevel(old_level)
                main.sys.excepthook, main.threading.excepthook = old_hook, old_thread_hook


if __name__ == "__main__":
    unittest.main()
