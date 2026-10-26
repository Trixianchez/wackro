"""Tests for failsafe.py: the take-over watch (fed fake input events) and the window guard (fed a
fake Windows that says which program is where).

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import logging
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform.startswith("linux"):
    os.environ.setdefault("PYNPUT_BACKEND", "dummy")

from pynput import keyboard  # noqa: E402

import failsafe  # noqa: E402
from failsafe import InputWatch, WindowGuard, corners_of, missing_programs, options_from  # noqa: E402
from winutil import Monitor, NullWin32  # noqa: E402

logging.getLogger("failsafe").addHandler(logging.NullHandler())
logging.getLogger("failsafe").propagate = False


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeListener:
    def __init__(self, fail=False):
        self.started = self.stopped = False
        self.fail = fail

    def start(self):
        if self.fail:
            raise OSError("no hook for you")
        self.started = True

    def stop(self):
        self.stopped = True


def watch(options=None, hotkey_vks=(), corners=None):
    trips = []
    clock = Clock()
    listeners = [FakeListener(), FakeListener()]
    w = InputWatch(trips.append, clock=clock, listener_factory=lambda _w: listeners,
                   screen_corners=(lambda: corners) if corners is not None else None)
    opts = dict(failsafe.DEFAULT_OPTIONS)
    opts.update(options or {})
    assert w.start(opts, hotkey_vks)
    clock.now += failsafe.GRACE_SECONDS + 0.01
    return w, trips, clock, listeners


class OptionTests(unittest.TestCase):
    def test_defaults_and_cleanup(self):
        self.assertEqual(options_from({}), failsafe.DEFAULT_OPTIONS)
        got = options_from({"fs_keys": 0, "fs_move_sensitivity": "insane", "other": 1})
        self.assertIs(got["fs_keys"], False)
        self.assertEqual(got["fs_move_sensitivity"], "medium")
        self.assertNotIn("other", got)


class TakeOverTests(unittest.TestCase):
    def test_any_key_stops_it_once(self):
        w, trips, clock, _ = watch()
        w._on_press(keyboard.KeyCode.from_char("a"))
        w._on_press(keyboard.KeyCode.from_char("b"))
        self.assertEqual(trips, ["you pressed a key"])

    def test_the_players_own_input_is_ignored(self):
        w, trips, clock, _ = watch()
        w._on_press(keyboard.KeyCode.from_char("a"), True)
        w._on_click(5, 5, None, True, True)
        w._on_scroll(5, 5, 0, -1, True)
        w._on_move(900, 900, True)
        self.assertEqual(trips, [])

    def test_nothing_counts_during_the_grace_period(self):
        trips = []
        clock = Clock()
        w = InputWatch(trips.append, clock=clock, listener_factory=lambda _w: [FakeListener()])
        w.start({})
        w._on_press(keyboard.KeyCode.from_char("a"))
        w._on_click(5, 5, None, True)
        self.assertEqual(trips, [])
        clock.now += 1
        w._on_click(5, 5, None, True)
        self.assertEqual(trips, ["you clicked the mouse"])

    def test_play_and_stop_hotkeys_are_left_to_themselves(self):
        w, trips, clock, _ = watch(hotkey_vks={0x90, 0x79})
        w._on_press(keyboard.KeyCode.from_vk(0x90))
        w._on_press(keyboard.KeyCode.from_vk(0x79))
        self.assertEqual(trips, [])
        w._on_press(keyboard.KeyCode.from_vk(0x41))
        self.assertEqual(trips, ["you pressed a key"])

    def test_clicks_and_scrolls(self):
        w, trips, clock, _ = watch()
        w._on_click(5, 5, None, False)          # a release alone: nothing
        self.assertEqual(trips, [])
        w._on_scroll(5, 5, 0, 1)
        self.assertEqual(trips, ["you scrolled the mouse wheel"])

    def test_switched_off_checks_do_nothing(self):
        w, trips, clock, _ = watch({"fs_keys": False, "fs_clicks": False, "fs_move": False, "fs_corner": True})
        w._on_press(keyboard.KeyCode.from_char("a"))
        w._on_click(5, 5, None, True)
        w._on_scroll(5, 5, 0, 1)
        for x in range(0, 500, 10):
            w._on_move(x + 100, 300)
        self.assertEqual(trips, [])

    def test_all_off_means_no_watching(self):
        w = InputWatch(lambda r: None, listener_factory=lambda _w: [FakeListener()])
        self.assertFalse(w.start({"fs_keys": False, "fs_clicks": False, "fs_move": False, "fs_corner": False}))
        self.assertFalse(w.active)

    def test_off_windows_without_a_factory_it_never_starts(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(InputWatch(lambda r: None).start({}))

    def test_a_listener_that_fails_to_start_is_cleaned_up(self):
        good, bad = FakeListener(), FakeListener(fail=True)
        w = InputWatch(lambda r: None, listener_factory=lambda _w: [good, bad])
        self.assertFalse(w.start({}))
        self.assertTrue(good.stopped)
        self.assertFalse(w.active)

    def test_stop_stops_the_listeners_and_the_checks(self):
        w, trips, clock, listeners = watch()
        w.stop()
        self.assertTrue(all(listener.stopped for listener in listeners))
        w._on_press(keyboard.KeyCode.from_char("a"))
        self.assertEqual(trips, [])

    def test_a_restart_is_fresh(self):
        w, trips, clock, _ = watch()
        w._on_press(keyboard.KeyCode.from_char("a"))
        w.start({})
        clock.now += 1
        w._on_press(keyboard.KeyCode.from_char("a"))
        self.assertEqual(len(trips), 2)

    def test_a_crashing_callback_is_logged_not_raised(self):
        clock = Clock()
        w = InputWatch(mock.Mock(side_effect=RuntimeError("boom")), clock=clock,
                       listener_factory=lambda _w: [FakeListener()])
        w.start({})
        clock.now += 1
        w._on_press(keyboard.KeyCode.from_char("a"))     # no exception


class MouseMoveTests(unittest.TestCase):
    def glide(self, w, clock, start, end, steps=10, dt=0.01):
        (x0, y0), (x1, y1) = start, end
        for i in range(1, steps + 1):
            clock.now += dt
            w._on_move(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)

    def test_moving_the_mouse_past_the_sensitivity_trips(self):
        for level, limit in failsafe.SENSITIVITY_PIXELS.items():
            with self.subTest(level=level):
                w, trips, clock, _ = watch({"fs_move_sensitivity": level})
                w._on_move(500, 500)
                self.glide(w, clock, (500, 500), (500 + limit - 2, 500))
                self.assertEqual(trips, [], "just under the limit")
                self.glide(w, clock, (500 + limit - 2, 500), (500 + limit + 10, 500), steps=3)
                self.assertEqual(trips, ["you moved the mouse"])

    def test_small_bumps_far_apart_never_add_up(self):
        w, trips, clock, _ = watch({"fs_move_sensitivity": "high"})
        x = 500
        for _ in range(20):
            clock.now += failsafe.MOVE_IDLE_RESET + 0.5
            w._on_move(x, 500)
            w._on_move(x + 5, 500)
            x += 5
        self.assertEqual(trips, [])

    def test_the_players_own_moves_never_trip_even_when_windows_reports_them_as_real(self):
        w, trips, clock, _ = watch({"fs_move_sensitivity": "high"})
        for x, y in ((10, 10), (800, 600), (30, 700), (1200, 40)):
            clock.now += 0.01
            w.note_cursor(x, y)
            w._on_move(x + 1, y)              # SetCursorPos echoed as a non-injected move
        self.assertEqual(trips, [])

    def test_a_late_event_from_the_old_position_is_not_a_jump(self):
        w, trips, clock, _ = watch({"fs_move_sensitivity": "high"})
        w.note_cursor(100, 100)
        w.note_cursor(900, 900)
        w._on_move(100, 100)                  # processed after the jump was announced
        self.assertEqual(trips, [])

    def test_your_move_while_the_player_moves_too(self):
        w, trips, clock, _ = watch()
        for i in range(20):
            clock.now += 0.01
            w.note_cursor(100 + i * 50, 100)                  # the player sweeps right
            w._on_move(100 + i * 50, 100 + 8 * (i + 1))       # you pull down 8 px each time
        self.assertEqual(trips, ["you moved the mouse"])

    def test_the_corner(self):
        corners = corners_of([Monitor(0, 0, 1366, 768, True)])
        self.assertIn((1365, 767), corners)
        w, trips, clock, _ = watch({"fs_move": False}, corners=corners)
        w._on_move(1364, 766)
        self.assertEqual(trips, ["you moved the mouse into a screen corner"])

    def test_the_player_moving_into_a_corner_is_fine(self):
        corners = corners_of([Monitor(0, 0, 1366, 768, True)])
        w, trips, clock, _ = watch(corners=corners)
        w.note_cursor(0, 0)
        w._on_move(0, 0)
        self.assertEqual(trips, [])

    def test_broken_corner_lookup(self):
        w = InputWatch(lambda r: None, listener_factory=lambda _w: [FakeListener()],
                       screen_corners=mock.Mock(side_effect=OSError))
        self.assertTrue(w.start({}))


class Windows(NullWin32):
    """A fake Windows: hwnd -> (exe, class); which one is in front, which is at a point."""

    def __init__(self):
        super().__init__()
        self.windows = {1: ("notepad.exe", "Notepad"), 2: ("chrome.exe", "Chrome_WidgetWin_1"),
                        3: ("BIR.exe", "#32770"), 4: ("WerFault.exe", "#32770"), 5: ("bir.exe", "ThunderRT6FormDC")}
        self.front = 1
        self.at = 1
        self.hung = set()
        self.disabled = set()
        self.texts = {3: "VAT rate value should be 10 or 12 only"}

    def foreground(self):
        return self.front

    def window_at(self, x, y):
        return self.at

    def context_of(self, hwnd):
        if hwnd not in self.windows:
            return None
        exe, cls = self.windows[hwnd]
        return {"exe": exe, "cls": cls}

    def foreground_context(self):
        return self.context_of(self.front)

    def is_hung(self, hwnd):
        return hwnd in self.hung

    def is_enabled(self, hwnd):
        return hwnd not in self.disabled

    def dialog_text(self, hwnd):
        return self.texts.get(hwnd, "")

    locked = False

    def input_desktop_blocked(self):
        return self.locked


NOTEPAD = {"exe": "notepad.exe", "cls": "Notepad"}
BIR = {"exe": "bir.exe", "cls": "ThunderRT6FormDC"}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.win = Windows()
        self.clock = Clock()
        self.guard = WindowGuard(self.win, {}, clock=self.clock)

    def check(self, operation="press", context=NOTEPAD, x=None, y=None):
        return self.guard.problem(operation, x, y, context)

    def test_all_good(self):
        self.assertIsNone(self.check())
        self.assertIsNone(self.check("click", NOTEPAD, 5, 5))

    def test_keys_going_to_another_program(self):
        self.win.front = 2
        self.assertEqual(self.check(), "keys were going to notepad.exe, but chrome.exe is in front")

    def test_click_landing_on_another_program(self):
        self.win.at = 2
        self.assertEqual(self.check("click", NOTEPAD, 5, 5),
                         "that click was meant for notepad.exe, but chrome.exe is there")

    def test_program_names_are_compared_without_case(self):
        self.win.front = 5
        self.assertIsNone(self.check("press", {"exe": "BIR.EXE", "cls": "ThunderRT6FormDC"}))

    def test_the_bir_popup_in_front_of_the_typing(self):
        self.win.front = 3
        self.assertEqual(self.check("press", BIR),
                         'a popup appeared in BIR.exe: "VAT rate value should be 10 or 12 only"')

    def test_a_click_on_a_window_blocked_by_its_popup(self):
        self.win.at = 5
        self.win.front = 3
        self.win.disabled.add(5)
        self.assertEqual(self.check("click", BIR, 5, 5),
                         'a popup appeared in BIR.exe: "VAT rate value should be 10 or 12 only"')

    def test_blocked_by_something_that_is_not_a_message_box(self):
        self.win.at = 5
        self.win.front = 2
        self.win.disabled.add(5)
        self.assertEqual(self.check("click", BIR, 5, 5), "bir.exe is blocked by another window")

    def test_popups_that_were_there_when_recording_are_fine(self):
        self.win.front = 3
        self.assertIsNone(self.check("press", {"exe": "bir.exe", "cls": "#32770"}))

    def test_popup_without_text(self):
        self.win.texts = {}
        self.win.front = 3
        self.assertEqual(self.check("press", BIR), "a popup appeared in BIR.exe")

    def test_long_popup_text_is_cut(self):
        self.win.texts = {3: "word " * 100}
        self.win.front = 3
        self.assertLess(len(self.check("press", BIR)), 170)

    def test_not_responding(self):
        self.win.hung.add(1)
        self.assertEqual(self.check(), "notepad.exe stopped responding")
        self.assertEqual(self.check(context=None), "notepad.exe stopped responding")   # old recordings too

    def test_locked_pc(self):
        self.win.locked = True
        for context in (NOTEPAD, None):
            self.assertEqual(self.check(context=context), "the PC got locked (or a Windows security prompt is up)")
        guard = WindowGuard(self.win, {"fs_locked": False})
        self.assertIsNone(guard.problem("press", None, None, NOTEPAD))

    def test_windows_crash_reporter(self):
        self.win.front = 4
        self.assertEqual(self.check(context=None), "a program crashed (the Windows error report is open)")

    def test_unknown_windows_never_stop_it(self):
        self.win.front = 99           # ours, or can't be read
        self.assertIsNone(self.check())
        self.win.front = None
        self.assertIsNone(self.check())

    def test_recordings_without_notes_skip_the_window_check(self):
        self.win.front = 2
        self.assertIsNone(self.check(context=None))

    def test_switches(self):
        self.win.front = 2
        self.win.hung.add(2)
        guard = WindowGuard(self.win, {"fs_window": False, "fs_crash": False, "fs_locked": False})
        self.assertFalse(guard.wanted)
        self.assertIsNone(guard.problem("press", None, None, NOTEPAD))
        guard = WindowGuard(self.win, {"fs_crash": False})
        self.assertIn("keys were going", guard.problem("press", None, None, NOTEPAD))

    def test_a_mismatch_gets_time_to_sort_itself_out(self):
        self.win.front = 2
        waits = []

        class Event:
            def wait(_self, seconds):
                waits.append(seconds)
                self.clock.now += seconds
                if len(waits) == 3:
                    self.win.front = 1          # the window finished opening
                return False

        self.assertIsNone(self.guard("press", None, None, NOTEPAD, Event()))
        self.assertEqual(len(waits), 3)

    def test_a_mismatch_that_stays_stops_after_the_settle_time(self):
        self.win.front = 2
        event = mock.Mock()

        def wait(seconds):
            self.clock.now += seconds
            return False

        event.wait.side_effect = wait
        self.assertIn("keys were going", self.guard("press", None, None, NOTEPAD, event))
        self.assertAlmostEqual(event.wait.call_count, failsafe.SETTLE_SECONDS / failsafe.SETTLE_STEP, delta=2)

    def test_stopping_while_settling_returns_quietly(self):
        self.win.front = 2
        event = threading.Event()
        event.set()
        self.assertIsNone(self.guard("press", None, None, NOTEPAD, event))


class BlackoutWakeTests(unittest.TestCase):
    """The Windows-hook filter while the blackout is up (InputWatch._filter)."""

    def make(self, options=None):
        wakes = []
        trips = []
        clock = Clock()
        w = InputWatch(trips.append, clock=clock, listener_factory=lambda _w: [FakeListener()])
        assert w.start(options if options is not None else {}, hotkey_vks={0x79, 0x90}, track=True,
                       on_wake=lambda: wakes.append(clock.now), mod_vks={0x11})
        clock.now += 5
        return w, wakes, trips, clock

    def test_first_touch_wakes_and_is_swallowed_then_nothing_trips_for_a_while(self):
        w, wakes, trips, clock = self.make()
        w.set_blacked_out(True)
        self.assertEqual(w._filter("mouse", None, False, False), "suppress")
        self.assertEqual(len(wakes), 1)
        self.assertFalse(w.blacked_out)
        self.assertEqual(w._filter("mouse", None, False, False), "suppress")     # rest of the wiggle
        clock.now += 0.5
        self.assertEqual(w._filter("mouse", None, False, False), "pass")
        w._on_move(500, 500)
        w._on_move(900, 900)
        w._on_press(keyboard.KeyCode.from_char("a"))
        self.assertEqual(trips, [])                                              # wake grace
        clock.now += failsafe.WAKE_GRACE
        w._on_press(keyboard.KeyCode.from_char("a"))
        self.assertEqual(trips, ["you pressed a key"])
        self.assertEqual(len(wakes), 1)

    def test_keys_wake_too(self):
        w, wakes, trips, clock = self.make()
        w.set_blacked_out(True)
        self.assertEqual(w._filter("key", 0x41, False, True), "suppress")
        self.assertEqual(len(wakes), 1)

    def test_hotkeys_and_their_modifiers_always_pass_and_never_wake(self):
        w, wakes, trips, clock = self.make()
        w.set_blacked_out(True)
        for vk in (0x79, 0x90, 0x11):
            self.assertEqual(w._filter("key", vk, False, True), "pass")
        self.assertEqual(wakes, [])
        self.assertTrue(w.blacked_out)
        w.set_blacked_out(False)
        clock.now += 5
        w._on_press(keyboard.KeyCode.from_vk(0x11))                  # Ctrl of a hotkey: no trip
        self.assertEqual(trips, [])

    def test_the_players_input_passes_and_never_wakes(self):
        w, wakes, trips, clock = self.make()
        w.set_blacked_out(True)
        self.assertEqual(w._filter("mouse", None, True, True), "pass")
        self.assertEqual(w._filter("key", 0x41, True, True), "pass")
        self.assertEqual(wakes, [])
        self.assertEqual(w.last_activity, -1e9)

    def test_nothing_is_swallowed_when_not_blacked_out(self):
        w, wakes, trips, clock = self.make()
        self.assertEqual(w._filter("key", 0x41, False, True), "pass")
        self.assertEqual(w.last_activity, clock.now)
        w.stop()
        self.assertEqual(w._filter("key", 0x41, False, True), "pass")

    def test_tracking_only_never_trips(self):
        off = {"fs_keys": False, "fs_clicks": False, "fs_move": False, "fs_corner": False}
        w, wakes, trips, clock = self.make(off)
        self.assertTrue(w.active)
        w._on_press(keyboard.KeyCode.from_char("a"))
        w._on_click(1, 1, None, True)
        for x in range(0, 900, 20):
            w._on_move(x, 5)
        self.assertEqual(trips, [])
        self.assertEqual(w.last_activity, clock.now)

    def test_without_tracking_all_off_still_means_no_watch(self):
        w = InputWatch(lambda r: None, listener_factory=lambda _w: [FakeListener()])
        self.assertFalse(w.start({"fs_keys": False, "fs_clicks": False, "fs_move": False, "fs_corner": False}))

    def test_a_crashing_wake_callback_is_logged(self):
        clock = Clock()
        w = InputWatch(lambda r: None, clock=clock, listener_factory=lambda _w: [FakeListener()])
        w.start({}, track=True, on_wake=mock.Mock(side_effect=RuntimeError("boom")))
        w.set_blacked_out(True)
        self.assertEqual(w._filter("mouse", None, False, True), "suppress")

    def test_hotkey_vks_helper(self):
        bindings = {"play": {"vk": 0x90, "mods": []}, "stop": {"vk": 0x79, "mods": ["ctrl", "shift"]},
                    "blackout": None}
        mains, mods = failsafe.hotkey_vks(bindings, ("play", "stop", "blackout", "nope"))
        self.assertEqual(mains, {0x90, 0x79})
        self.assertEqual(mods, {0x11, 0xA2, 0xA3, 0x10, 0xA0, 0xA1})


class PreflightTests(unittest.TestCase):
    def test_missing_programs(self):
        events = [
            {"t": 0, "type": "mouse_move", "x": 1, "y": 1},
            {"t": 1, "type": "key_press", "key": {"kind": "char", "value": "a"}, "w": BIR},
            {"t": 2, "type": "key_press", "key": {"kind": "char", "value": "a"}, "w": {"exe": "BIR.exe", "cls": "X"}},
            {"t": 3, "type": "key_press", "key": {"kind": "char", "value": "a"}, "w": NOTEPAD},
            {"t": 4, "type": "key_press", "key": {"kind": "char", "value": "a"}, "w": {"exe": "excel.exe", "cls": "X"}},
        ]
        self.assertEqual(missing_programs(events, {"Notepad.exe"}), ["bir.exe", "excel.exe"])
        self.assertEqual(missing_programs(events, {"notepad.exe", "bir.exe", "excel.exe"}), [])
        self.assertEqual(missing_programs(events, None), [])
        self.assertEqual(missing_programs(events[:1], set()), [])


class RealPlayerIntegrationTests(unittest.TestCase):
    """The real Player on its real thread with the real failsafes; only the mouse, keyboard and
    Windows are fakes."""

    def rig(self):
        from player import Player

        sent = []
        mouse = types.SimpleNamespace(position=(0, 0), press=lambda b: sent.append(("click", b)),
                                      release=lambda b: None, scroll=lambda dx, dy: None)
        board = types.SimpleNamespace(press=lambda k: sent.append(("key", k)), release=lambda k: None)
        done = threading.Event()
        player = Player(on_finished=lambda reason: done.set(), mouse_controller=mouse, keyboard_controller=board)
        return player, sent, done

    def typing(self, count, step=0.05):
        events = [{"t": 0.0, "type": "mouse_move", "x": 1, "y": 1}]
        for i in range(count):
            events.append({"t": 0.1 + i * step, "type": "key_press", "key": {"kind": "char", "value": "a"}, "w": BIR})
            events.append({"t": 0.1 + i * step + 0.01, "type": "key_release", "key": {"kind": "char", "value": "a"}})
        return events

    def test_the_bir_popup_stops_typing_mid_way(self):
        player, sent, done = self.rig()
        win = Windows()
        win.front = 5                                   # BIR's main window
        pressed = []

        def keyboard_press(key):
            pressed.append(key)
            if len(pressed) == 3:
                win.front = 3                           # the "VAT rate" box pops up
        player._keyboard_controller.press = keyboard_press
        player.start(self.typing(20), guard=WindowGuard(win, {}))
        self.assertTrue(done.wait(10))
        self.assertEqual(len(pressed), 3)               # nothing typed into the popup
        self.assertEqual(player.finish_message,
                         'Playback stopped: a popup appeared in BIR.exe: "VAT rate value should be 10 or 12 only"')

    def test_blacked_out_touch_wakes_and_playback_finishes(self):
        player, sent, done = self.rig()
        trips, wakes = [], []
        watch = InputWatch(lambda reason: (trips.append(reason), player.halt(reason)),
                           listener_factory=lambda _w: [FakeListener()])
        self.assertTrue(watch.start({}, track=True, on_wake=lambda: wakes.append(1)))
        watch._t0 -= 1
        player.start(self.typing(30), on_cursor=watch.note_cursor)
        watch.set_blacked_out(True)

        def touch():
            time.sleep(0.4)
            decisions = [watch._filter("mouse", None, False, False) for _ in range(5)]
            for i in range(5):
                watch._on_move(300 + i * 30, 300)     # the rest of the wiggle reaches the callbacks
            touch.decisions = decisions
        threading.Thread(target=touch, daemon=True).start()
        self.assertTrue(done.wait(10))
        self.assertEqual(touch.decisions, ["suppress"] * 5)    # the waking wiggle was swallowed
        self.assertEqual(wakes, [1])
        self.assertEqual(trips, [])                            # ...and didn't stop anything
        self.assertEqual(player.finish_reason, "done")
        self.assertEqual(len([s for s in sent if s[0] == "key"]), 30)
        watch.stop()

    def test_you_grab_the_mouse_mid_way(self):
        player, sent, done = self.rig()
        trips = []
        watch = InputWatch(lambda reason: (trips.append(reason), player.halt(reason)),
                           listener_factory=lambda _w: [FakeListener()])
        self.assertTrue(watch.start({}))
        watch._t0 -= 1                                  # skip the grace period
        player.start(self.typing(40), on_cursor=watch.note_cursor)

        def grab():
            time.sleep(0.6)
            for i in range(10):
                watch._on_move(300 + i * 10, 300)       # you, dragging 100 px
        threading.Thread(target=grab, daemon=True).start()
        self.assertTrue(done.wait(10))
        self.assertEqual(trips, ["you moved the mouse"])
        self.assertEqual(player.finish_message, "Playback stopped: you moved the mouse")
        self.assertLess(len([s for s in sent if s[0] == "key"]), 40)
        watch.stop()


if __name__ == "__main__":
    unittest.main()
