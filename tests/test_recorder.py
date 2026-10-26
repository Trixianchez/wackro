"""Tests for recorder.py.

The handlers are driven directly with a fake clock, so no real input is needed (or possible in
a sandbox). Real global recording is checked by hand on Windows.

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import logging
import math
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform.startswith("linux"):
    os.environ.setdefault("PYNPUT_BACKEND", "dummy")

from pynput import keyboard  # noqa: E402

import config  # noqa: E402
import recorder  # noqa: E402
import storage  # noqa: E402
from recorder import Recorder, finish_events  # noqa: E402
from winutil import NullWin32  # noqa: E402

# The pre-1.7.4 defaults (Ctrl+` / NumLock / F10). The behaviour tests use them on purpose: a binding
# with a modifier (Ctrl+`) and a lock key (NumLock) exercise more of the matcher than plain F-keys.
CLASSIC = {"record": {"mods": ["ctrl"], "vk": 0xC0}, "play": {"mods": [], "vk": 0x90},
           "stop": {"mods": [], "vk": 0x79}}


# Tests that provoke errors on purpose would otherwise spray tracebacks over the test output.
logging.getLogger("recorder").addHandler(logging.NullHandler())


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeButton:
    def __init__(self, name):
        self.name = name


class FakeListener:
    def __init__(self, fail_on_start=False):
        self.fail_on_start = fail_on_start
        self.started = False
        self.stopped = False
        self.joined = False

    def start(self):
        if self.fail_on_start:
            raise OSError("no permission")
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = True


def char(c, vk=None):
    return keyboard.KeyCode(vk=vk, char=c)


def make(position=(100, 200), on_auto_stop=None, windows_info=None):
    clock = FakeClock()
    rec = Recorder(on_auto_stop=on_auto_stop, clock=clock, position_func=lambda: position,
                   windows_info=windows_info or NullWin32())
    return rec, clock


def started(position=(100, 200), on_auto_stop=None, windows=None, windows_info=None, **start_kwargs):
    """A recorder that is already recording, with fake listeners."""
    rec, clock = make(position, on_auto_stop, windows_info)
    if windows is not None:
        rec._windows = windows
    listeners = (FakeListener(), FakeListener())
    start_kwargs.setdefault("hotkeys", CLASSIC)
    with mock.patch.object(Recorder, "_make_listeners", return_value=listeners):
        rec.start(**start_kwargs)
    return rec, clock, listeners


class FakeSpecialKey:
    """Stands in for a keyboard.Key member (pynput's headless backend can't tell them apart)."""

    def __init__(self, name):
        self.name = name
        self.value = None


def json_for(key):
    """key_to_json that also understands FakeSpecialKey."""
    if isinstance(key, FakeSpecialKey):
        return {"kind": "special", "value": key.name}
    return storage.key_to_json(key)


CTRL = FakeSpecialKey("ctrl_l")
SHIFT = FakeSpecialKey("shift")
GRAVE = keyboard.KeyCode(vk=0xC0, char=None)          # what Windows gives for ` with Ctrl held
GRAVE_CHAR = keyboard.KeyCode(vk=0xC0, char="`")
NUMLOCK = keyboard.KeyCode.from_vk(0x90)
F10 = keyboard.KeyCode.from_vk(0x79)


def types(events):
    return [e["type"] for e in events]


def assert_valid(test, events):
    """Whatever the recorder produces must be saveable."""
    storage.make_recording(events, 1920, 1080)


class StartTests(unittest.TestCase):
    def test_first_event_is_the_starting_mouse_position_at_t0(self):
        rec, _, _ = started(position=(640, 480))
        rec_events = rec.stop()
        self.assertEqual(rec_events[0], {"t": 0.0, "type": "mouse_move", "x": 640, "y": 480})

    def test_float_and_negative_start_positions_are_rounded_and_kept(self):
        rec, _, _ = started(position=(-1919.6, 12.4))
        self.assertEqual(rec.stop()[0]["x"], -1920)
        self.assertEqual(rec.last_events[0]["y"], 12)

    def test_no_start_event_when_the_position_cannot_be_read(self):
        def broken():
            raise OSError("nope")

        for func in (broken, lambda: None, lambda: (math.nan, 1), lambda: ("a", "b")):
            with self.subTest(func=func):
                rec = Recorder(clock=FakeClock(), position_func=func, windows_info=NullWin32())
                with mock.patch.object(Recorder, "_make_listeners", return_value=(FakeListener(), FakeListener())):
                    rec.start()
                self.assertEqual(rec.stop(), [])

    def test_start_starts_both_listeners(self):
        _, _, listeners = started()
        self.assertTrue(all(listener.started for listener in listeners))

    def test_starting_twice_is_an_error(self):
        rec, _, _ = started()
        with self.assertRaises(RuntimeError):
            rec.start()

    def test_listener_failure_gives_a_friendly_error_and_leaves_the_recorder_idle(self):
        rec, _ = make()
        good, bad = FakeListener(), FakeListener(fail_on_start=True)
        with mock.patch.object(Recorder, "_make_listeners", return_value=(good, bad)):
            with self.assertRaises(storage.RecordingError) as ctx:
                rec.start()
        self.assertIn("no permission", str(ctx.exception))
        self.assertFalse(rec.is_recording)
        self.assertTrue(good.stopped)  # the one that did start is cleaned up

    def test_missing_pynput_backend_gives_a_friendly_error(self):
        rec, _ = make()
        with mock.patch.object(Recorder, "_make_listeners", side_effect=ImportError("no display")):
            with self.assertRaises(storage.RecordingError):
                rec.start()
        self.assertFalse(rec.is_recording)

    def test_restart_forgets_the_previous_recording(self):
        rec, clock, _ = started()
        rec._on_press(char("a", 65))
        rec.stop()
        with mock.patch.object(Recorder, "_make_listeners", return_value=(FakeListener(), FakeListener())):
            rec.start()
        self.assertEqual(types(rec.stop()), ["mouse_move"])
        self.assertEqual(rec.skipped_keys, 0)

    def test_real_pynput_listeners_accept_our_handlers(self):
        # Constructing (not starting) real listeners checks pynput accepts the handler signatures.
        rec, _ = make()
        mouse_listener, keyboard_listener = rec._make_listeners()
        self.assertTrue(hasattr(mouse_listener, "start") and hasattr(keyboard_listener, "stop"))

    def test_handlers_work_with_and_without_the_injected_argument(self):
        rec, _, _ = started()
        rec._on_move(1, 1, True)
        rec._on_click(1, 1, FakeButton("left"), True, False)
        rec._on_scroll(1, 1, 0, 1, True)
        rec._on_press(char("a", 65), True)
        rec._on_release(char("a", 65), False)
        self.assertEqual(rec.event_count, 5)  # start position + 4 (the move was too soon after t=0)


class StopTests(unittest.TestCase):
    def test_stop_stops_and_joins_the_listeners(self):
        rec, _, listeners = started()
        rec.stop()
        self.assertTrue(all(l.stopped and l.joined for l in listeners))
        self.assertFalse(rec.is_recording)

    def test_stop_twice_returns_the_same_events(self):
        rec, clock, _ = started()
        clock.advance(1)
        rec._on_press(char("a", 65))
        first = rec.stop()
        self.assertEqual(rec.stop(), first)

    def test_stop_without_start_returns_nothing(self):
        rec, _ = make()
        self.assertEqual(rec.stop(), [])

    def test_stop_survives_a_listener_that_blows_up(self):
        rec, _, listeners = started()
        listeners[0].stop = mock.Mock(side_effect=RuntimeError("boom"))
        listeners[1].join = mock.Mock(side_effect=RuntimeError("cannot join current thread"))
        rec.stop()
        self.assertFalse(rec.is_recording)

    def test_events_after_stop_are_ignored(self):
        rec, clock, _ = started()
        rec.stop()
        rec._on_click(5, 5, FakeButton("left"), True)
        rec._on_press(char("a", 65))
        self.assertEqual(types(rec.last_events), ["mouse_move"])
        self.assertEqual(rec.event_count, 1)

    def test_elapsed_follows_the_clock_then_freezes(self):
        rec, clock, _ = started()
        clock.advance(2.5)
        self.assertAlmostEqual(rec.elapsed, 2.5)
        rec.stop()
        clock.advance(10)
        self.assertAlmostEqual(rec.elapsed, 2.5)

    def test_stop_before_a_tail_trim_keeps_everything(self):
        rec, clock, _ = started()
        clock.advance(1.0)
        rec._on_click(1, 1, FakeButton("left"), True)
        clock.advance(0.1)
        rec._on_click(1, 1, FakeButton("left"), False)
        self.assertEqual(len(rec.stop()), 3)

    def test_trim_tail_cuts_the_last_half_second(self):
        rec, clock, _ = started()
        clock.advance(1.0)
        rec._on_press(char("a", 65))
        clock.advance(0.05)
        rec._on_release(char("a", 65))
        clock.advance(2.0)
        rec._on_click(9, 9, FakeButton("left"), True)   # the click on the Stop button...
        clock.advance(0.1)
        rec._on_click(9, 9, FakeButton("left"), False)
        events = rec.stop(trim_tail=True)
        self.assertEqual(types(events), ["mouse_move", "key_press", "key_release"])
        assert_valid(self, events)


class MouseTests(unittest.TestCase):
    def test_throttle_keeps_at_most_one_move_per_interval(self):
        rec, clock, _ = started()
        clock.advance(1.0)
        kept = 0
        for i in range(100):  # 100 moves, 2 ms apart = 200 ms
            clock.advance(0.002)
            rec._on_move(i, i)
        events = rec.stop()
        moves = [e for e in events if e["type"] == "mouse_move"]
        gaps = [b["t"] - a["t"] for a, b in zip(moves[1:], moves[2:])]
        self.assertTrue(gaps and all(g >= config.MOVE_MIN_INTERVAL - 1e-9 for g in gaps))
        self.assertLess(len(moves), 30)
        self.assertGreater(len(moves), 10)

    def test_a_move_exactly_one_interval_later_is_kept(self):
        rec, clock, _ = started()
        clock.advance(1.0)
        rec._on_move(1, 1)
        clock.advance(config.MOVE_MIN_INTERVAL)
        rec._on_move(2, 2)
        self.assertEqual(sum(e["type"] == "mouse_move" for e in rec.stop()), 3)

    def test_a_dropped_move_does_not_delay_the_next_kept_one(self):
        rec, clock, _ = started()
        clock.advance(1.0)
        rec._on_move(1, 1)            # kept at t=1.0
        clock.advance(0.004)
        rec._on_move(2, 2)            # dropped
        clock.advance(0.007)
        rec._on_move(3, 3)            # 11 ms after the last KEPT move: kept
        xs = [e["x"] for e in rec.stop() if e["type"] == "mouse_move"]
        self.assertEqual(xs, [100, 1, 3])

    def test_click_records_position_button_and_state(self):
        rec, clock, _ = started()
        clock.advance(0.5)
        rec._on_click(10, 20, FakeButton("right"), True)
        clock.advance(0.1)
        rec._on_click(10, 20, FakeButton("right"), False)
        events = rec.stop()
        self.assertEqual(events[1], {"t": 0.5, "type": "mouse_click", "x": 10, "y": 20, "button": "right", "pressed": True})
        self.assertEqual(events[2]["pressed"], False)

    def test_all_five_buttons_are_recorded(self):
        rec, clock, _ = started()
        for name in storage.BUTTON_NAMES:
            clock.advance(0.1)
            rec._on_click(1, 1, FakeButton(name), True)
            rec._on_click(1, 1, FakeButton(name), False)
        self.assertEqual(rec.event_count, 1 + 2 * len(storage.BUTTON_NAMES))

    def test_unknown_buttons_are_skipped_and_counted(self):
        rec, _, _ = started()
        rec._on_click(1, 1, FakeButton("unknown"), True)
        rec._on_click(1, 1, object(), True)
        self.assertEqual(rec.skipped_buttons, 2)
        self.assertEqual(rec.event_count, 1)

    def test_release_without_a_recorded_press_is_dropped(self):
        rec, _, _ = started()
        rec._on_click(1, 1, FakeButton("left"), False)
        self.assertEqual(rec.event_count, 1)
        self.assertIsNone(rec.stop_reason)

    def test_scroll_records_both_directions(self):
        rec, clock, _ = started()
        clock.advance(0.1)
        rec._on_scroll(5, 6, 0, -3)
        rec._on_scroll(5, 6, 2, 0)
        events = rec.stop()
        self.assertEqual((events[1]["dx"], events[1]["dy"]), (0, -3))
        self.assertEqual((events[2]["dx"], events[2]["dy"]), (2, 0))

    def test_scroll_of_nothing_is_dropped(self):
        rec, _, _ = started()
        rec._on_scroll(5, 6, 0, 0)
        rec._on_scroll(5, 6, 0.2, -0.1)  # rounds to nothing
        self.assertEqual(rec.event_count, 1)

    def test_coordinates_are_rounded_and_clamped_so_the_file_always_loads(self):
        rec, clock, _ = started()
        clock.advance(1)
        rec._on_click(10.6, -20.4, FakeButton("left"), True)
        rec._on_click(10**9, -(10**9), FakeButton("left"), False)
        rec._on_scroll(1, 1, 10**9, -(10**9))
        events = rec.stop()
        self.assertEqual((events[1]["x"], events[1]["y"]), (11, -20))
        self.assertEqual((events[2]["x"], events[2]["y"]), (config.MAX_COORD, -config.MAX_COORD))
        self.assertEqual((events[3]["dx"], events[3]["dy"]), (config.MAX_SCROLL, -config.MAX_SCROLL))
        assert_valid(self, events)

    def test_garbage_coordinates_are_skipped_without_crashing(self):
        rec, clock, _ = started()
        clock.advance(1)
        for bad in (math.nan, math.inf, None, "7", True):
            with self.subTest(bad=bad):
                rec._on_move(bad, 1)
                rec._on_click(1, bad, FakeButton("left"), True)
                rec._on_scroll(bad, 1, 1, 1)
        self.assertEqual(rec.event_count, 1)
        self.assertIsNone(rec.stop_reason)


class KeyboardTests(unittest.TestCase):
    def test_press_and_release_of_a_character(self):
        rec, clock, _ = started()
        clock.advance(1)
        rec._on_press(char("a", 65))
        clock.advance(0.05)
        rec._on_release(char("a", 65))
        events = rec.stop()
        self.assertEqual(events[1]["key"], {"kind": "char", "value": "a", "vk": 65})
        self.assertEqual(types(events[1:]), ["key_press", "key_release"])

    def test_holding_a_key_records_only_the_first_press(self):
        rec, clock, _ = started()
        for _ in range(30):  # Windows auto-repeat
            clock.advance(0.03)
            rec._on_press(char("a", 65))
        clock.advance(0.03)
        rec._on_release(char("a", 65))
        self.assertEqual(types(rec.stop()[1:]), ["key_press", "key_release"])

    def test_pressing_again_after_a_release_is_recorded(self):
        rec, clock, _ = started()
        for _ in range(3):
            clock.advance(0.1)
            rec._on_press(char("a", 65))
            rec._on_release(char("a", 65))
        self.assertEqual(rec.event_count, 7)

    def test_shift_changes_the_character_but_must_not_leave_the_key_stuck(self):
        rec, clock, _ = started()
        clock.advance(0.1)
        rec._on_press(char("A", 65))     # pressed with Shift held
        rec._on_release(char("a", 65))   # Shift let go first, so the release says 'a'
        rec._on_press(char("a", 65))     # must still be recorded
        events = rec.stop()
        self.assertEqual(types(events[1:]), ["key_press", "key_release", "key_press", "key_release"])
        self.assertEqual(events[2]["key"], events[1]["key"])  # the release matches the press

    def test_shifted_symbols_match_by_vk(self):
        # '!' and '1' are different characters on the same physical key; only the vk ties them.
        rec, clock, _ = started()
        clock.advance(0.1)
        rec._on_press(char("!", 49))
        rec._on_release(char("1", 49))
        rec._on_press(char("1", 49))
        events = rec.stop()
        self.assertEqual(types(events[1:]), ["key_press", "key_release", "key_press", "key_release"])
        self.assertEqual(events[2]["key"], {"kind": "char", "value": "!", "vk": 49})

    def test_characters_without_a_vk_match_regardless_of_case(self):
        rec, clock, _ = started()
        rec._on_press(char("A"))
        rec._on_release(char("a"))
        rec._on_press(char("A"))
        self.assertEqual(types(rec.stop()[1:]), ["key_press", "key_release", "key_press", "key_release"])

    def test_release_without_a_recorded_press_is_dropped(self):
        rec, _, _ = started()
        rec._on_release(char("a", 65))
        self.assertEqual(rec.event_count, 1)
        self.assertIsNone(rec.stop_reason)  # dropped on purpose, not because a handler crashed

    def test_vk_only_keys_are_recorded(self):
        rec, clock, _ = started()
        rec._on_press(keyboard.KeyCode.from_vk(0xAF))
        self.assertEqual(rec.stop()[1]["key"], {"kind": "vk", "value": 0xAF})

    def test_old_f9_f11_are_ordinary_keys_now(self):
        rec, clock, _ = started()
        for name in ("f9", "f11"):
            with mock.patch.object(recorder, "key_to_json", return_value={"kind": "special", "value": name}):
                rec._on_press(FakeSpecialKey(name))
                rec._on_release(FakeSpecialKey(name))
        self.assertEqual(rec.event_count, 1 + 4)

    def test_other_special_keys_are_not_filtered(self):
        rec, clock, _ = started()
        for name in ("enter", "f8", "f12", "esc", "shift"):
            with mock.patch.object(recorder, "key_to_json", return_value={"kind": "special", "value": name}):
                rec._on_press(object())
                rec._on_release(object())
        self.assertEqual(rec.event_count, 1 + 2 * 5)

    def test_unsaveable_keys_are_skipped_and_counted(self):
        rec, _, _ = started()
        rec._on_press(keyboard.KeyCode())          # no char and no vk
        rec._on_press(char("ab", 1))               # more than one character
        rec._on_release(None)                      # pynput sends None for keys it can't identify
        rec._on_press(object())                    # not a key at all
        self.assertEqual(rec.skipped_keys, 4)
        self.assertEqual(rec.event_count, 1)
        self.assertIsNone(rec.stop_reason)


class HotkeyFilterTests(unittest.TestCase):
    """The user's hotkeys (default Ctrl+` / NumLock / F10) never end up in a recording."""

    def setUp(self):
        patcher = mock.patch.object(recorder, "key_to_json", side_effect=json_for)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tap(self, rec, key):
        rec._on_press(key)
        rec._on_release(key)

    def test_ctrl_grave_is_left_out_but_ctrl_itself_is_recorded(self):
        rec, clock, _ = started(windows=True)
        rec._on_press(CTRL)
        self.tap(rec, GRAVE)
        rec._on_release(CTRL)
        events = rec.stop()
        self.assertEqual(types(events), ["mouse_move", "key_press", "key_release"])
        self.assertEqual(events[1]["key"], {"kind": "special", "value": "ctrl_l"})

    def test_a_plain_grave_is_typed_text_and_recorded(self):
        rec, clock, _ = started(windows=True)
        self.tap(rec, GRAVE_CHAR)
        self.assertEqual(types(rec.stop()), ["mouse_move", "key_press", "key_release"])

    def test_numlock_and_f10_are_left_out(self):
        rec, clock, _ = started(windows=True)
        self.tap(rec, NUMLOCK)
        self.tap(rec, F10)
        self.assertEqual(rec.event_count, 1)
        self.assertEqual(rec.skipped_keys, 0)   # on purpose, not a failure

    def test_numlock_with_shift_held_is_just_a_key(self):
        rec, clock, _ = started(windows=True)
        rec._on_press(SHIFT)
        self.tap(rec, NUMLOCK)
        rec._on_release(SHIFT)
        self.assertEqual(rec.event_count, 1 + 4)

    def test_auto_repeat_of_a_held_hotkey_stays_out(self):
        rec, clock, _ = started(windows=True)
        for _ in range(5):
            rec._on_press(NUMLOCK)
        rec._on_release(NUMLOCK)
        self.assertEqual(rec.event_count, 1)

    def test_custom_hotkeys_from_start(self):
        custom = {"record": {"vk": 0x78, "mods": []}, "play": {"vk": 0x7A, "mods": []},
                  "stop": {"vk": 0x7B, "mods": []}}
        rec, clock, _ = started(windows=True, hotkeys=custom)
        self.tap(rec, NUMLOCK)                               # not a hotkey any more: recorded
        self.tap(rec, keyboard.KeyCode.from_vk(0x7A))        # F11 now is
        self.assertEqual(rec.event_count, 1 + 2)

    def test_off_windows_the_character_is_used(self):
        rec, clock, _ = started(windows=False)
        rec._on_press(CTRL)
        self.tap(rec, keyboard.KeyCode.from_char("`"))
        rec._on_release(CTRL)
        self.assertEqual(rec.event_count, 1 + 2)

    def test_stopping_with_ctrl_grave_drops_the_held_ctrl(self):
        rec, clock, _ = started(windows=True)
        rec._on_press(keyboard.KeyCode(vk=0x41, char="a"))
        rec._on_release(keyboard.KeyCode(vk=0x41, char="a"))
        clock.advance(1)
        rec._on_press(CTRL)            # the user reaches for Ctrl+` to stop
        rec._on_press(GRAVE)
        events = rec.stop(drop_modifiers={"ctrl"})
        self.assertEqual(types(events), ["mouse_move", "key_press", "key_release"])
        self.assertNotIn("ctrl_l", str(events))
        assert_valid(self, events)

    def test_without_drop_modifiers_the_held_ctrl_gets_a_release(self):
        rec, clock, _ = started(windows=True)
        rec._on_press(CTRL)
        events = rec.stop()
        self.assertEqual(types(events), ["mouse_move", "key_press", "key_release"])


class OptionsTests(unittest.TestCase):
    def test_defaults_record_everything(self):
        rec, clock, _ = started()
        self.assertEqual(rec._options, recorder.DEFAULT_OPTIONS)

    def test_keyboard_off(self):
        rec, clock, _ = started(options={"keyboard": False})
        rec._on_press(char("a", 65))
        rec._on_release(char("a", 65))
        rec._on_click(5, 5, FakeButton("left"), True)
        self.assertEqual(types(rec.stop()), ["mouse_move", "mouse_click", "mouse_click"])

    def test_clicks_off_also_leaves_out_scrolling(self):
        rec, clock, _ = started(options={"clicks": False})
        rec._on_click(5, 5, FakeButton("left"), True)
        rec._on_click(5, 5, FakeButton("left"), False)
        rec._on_scroll(5, 5, 0, -1)
        rec._on_press(char("a", 65))
        self.assertEqual(types(rec.stop()), ["mouse_move", "key_press", "key_release"])

    def test_moves_off_keeps_the_start_position(self):
        rec, clock, _ = started(options={"moves": False})
        clock.advance(1)
        rec._on_move(300, 300)
        rec._on_click(300, 300, FakeButton("left"), True)
        events = rec.stop()
        self.assertEqual(types(events), ["mouse_move", "mouse_click", "mouse_click"])
        self.assertEqual(events[1]["x"], 300)   # clicks still carry their position

    def test_delay_off_spaces_events_evenly(self):
        rec, clock, _ = started(options={"delay": False})
        clock.advance(5)
        rec._on_press(char("a", 65))
        clock.advance(7)
        rec._on_release(char("a", 65))
        events = rec.stop()
        self.assertEqual([e["t"] for e in events], [0.0, config.NO_DELAY_GAP, 2 * config.NO_DELAY_GAP])
        assert_valid(self, events)

    def test_unknown_options_are_ignored_and_values_become_bools(self):
        rec, clock, _ = started(options={"keyboard": 0, "bogus": True})
        self.assertFalse(rec._options["keyboard"])
        self.assertNotIn("bogus", rec._options)

    def test_options_reset_on_the_next_start(self):
        rec, clock, listeners = started(options={"keyboard": False})
        rec.stop()
        with mock.patch.object(Recorder, "_make_listeners", return_value=(FakeListener(), FakeListener())):
            rec.start()
        self.assertTrue(rec._options["keyboard"])


class RetimeTests(unittest.TestCase):
    def test_retime(self):
        events = [{"t": 0.0, "type": "mouse_move", "x": 1, "y": 1},
                  {"t": 9.9, "type": "mouse_move", "x": 2, "y": 2}]
        done = recorder.retime(events, 0.05)
        self.assertEqual([e["t"] for e in done], [0.0, 0.05])
        self.assertEqual(events[1]["t"], 9.9)   # input untouched

    def test_empty(self):
        self.assertEqual(recorder.retime([]), [])


class FinishEventsTests(unittest.TestCase):
    def test_drop_modifiers_removes_only_held_presses_of_those_modifiers(self):
        ctrl = {"kind": "special", "value": "ctrl_l"}
        shift = {"kind": "special", "value": "shift"}
        events = [
            {"t": 0.0, "type": "mouse_move", "x": 1, "y": 1},
            {"t": 0.5, "type": "key_press", "key": ctrl},         # pressed and released: stays
            {"t": 0.6, "type": "key_release", "key": ctrl},
            {"t": 1.0, "type": "key_press", "key": shift},        # held, but not asked for: stays
            {"t": 2.0, "type": "key_press", "key": ctrl},         # held at the end: dropped
        ]
        done = finish_events(events, 3.0, drop_modifiers={"ctrl"})
        self.assertEqual(done[:4], events[:4])
        self.assertEqual(done[4], {"t": 3.0, "type": "key_release", "key": shift})
        self.assertEqual(len(done), 5)

    def test_drop_modifiers_knows_all_sides(self):
        for name in ("ctrl", "ctrl_l", "ctrl_r"):
            with self.subTest(name=name):
                events = [{"t": 1.0, "type": "key_press", "key": {"kind": "special", "value": name}}]
                self.assertEqual(finish_events(events, 2.0, drop_modifiers={"ctrl"}), [])
        events = [{"t": 1.0, "type": "key_press", "key": {"kind": "special", "value": "alt_gr"}}]
        self.assertEqual(finish_events(events, 2.0, drop_modifiers={"alt"}), [])


    def test_held_key_gets_a_release_at_the_end(self):
        events = [
            {"t": 0.0, "type": "mouse_move", "x": 1, "y": 1},
            {"t": 1.0, "type": "key_press", "key": {"kind": "special", "value": "shift"}},
            {"t": 1.5, "type": "key_press", "key": {"kind": "char", "value": "a", "vk": 65}},
        ]
        done = finish_events(events, 3.0)
        self.assertEqual(done[:3], events)
        self.assertEqual([e["type"] for e in done[3:]], ["key_release", "key_release"])
        self.assertEqual([e["key"]["value"] for e in done[3:]], ["a", "shift"])  # last pressed, first released
        self.assertTrue(all(e["t"] == 3.0 for e in done[3:]))

    def test_held_mouse_button_is_released_where_the_mouse_last_was(self):
        events = [
            {"t": 0.0, "type": "mouse_move", "x": 1, "y": 1},
            {"t": 1.0, "type": "mouse_click", "x": 5, "y": 6, "button": "left", "pressed": True},
            {"t": 1.2, "type": "mouse_move", "x": 50, "y": 60},
        ]
        done = finish_events(events, 2.0)
        self.assertEqual(done[-1], {"t": 2.0, "type": "mouse_click", "x": 50, "y": 60, "button": "left", "pressed": False})

    def test_nothing_is_added_when_nothing_is_held(self):
        events = [
            {"t": 0.0, "type": "mouse_move", "x": 1, "y": 1},
            {"t": 1.0, "type": "key_press", "key": {"kind": "char", "value": "a", "vk": 65}},
            {"t": 1.1, "type": "key_release", "key": {"kind": "char", "value": "A", "vk": 65}},
        ]
        self.assertEqual(finish_events(events, 5.0), events)

    def test_trimming_that_cuts_between_press_and_release_still_releases(self):
        events = [
            {"t": 0.0, "type": "mouse_move", "x": 1, "y": 1},
            {"t": 4.0, "type": "key_press", "key": {"kind": "char", "value": "a", "vk": 65}},
            {"t": 4.8, "type": "key_release", "key": {"kind": "char", "value": "a", "vk": 65}},
        ]
        done = finish_events(events, 5.0, trim_tail=True)  # cutoff 4.5
        self.assertEqual([e["type"] for e in done], ["mouse_move", "key_press", "key_release"])
        self.assertEqual(done[-1]["t"], 4.5)
        storage.make_recording(done, 100, 100)

    def test_trim_never_removes_the_start_position(self):
        events = [{"t": 0.0, "type": "mouse_move", "x": 1, "y": 1}]
        self.assertEqual(finish_events(events, 0.2, trim_tail=True), events)

    def test_trim_boundary_event_is_kept(self):
        events = [{"t": 0.0, "type": "mouse_move", "x": 1, "y": 1}, {"t": 4.5, "type": "mouse_scroll", "x": 1, "y": 1, "dx": 0, "dy": 1}]
        self.assertEqual(len(finish_events(events, 5.0, trim_tail=True)), 2)

    def test_does_not_modify_the_input(self):
        events = [{"t": 1.0, "type": "key_press", "key": {"kind": "char", "value": "a", "vk": 65}}]
        before = list(events)
        finish_events(events, 2.0)
        self.assertEqual(events, before)

    def test_empty(self):
        self.assertEqual(finish_events([], 3.0), [])
        self.assertEqual(finish_events([], 3.0, trim_tail=True), [])


class StopAtLimitTests(unittest.TestCase):
    def test_event_limit_stops_recording_and_notifies_once(self):
        calls = []
        with mock.patch.object(recorder, "MAX_EVENTS", 30), mock.patch.object(recorder, "_RESERVE_EVENTS", 5):
            rec, clock, _ = started(on_auto_stop=calls.append)
            for i in range(100):
                clock.advance(0.1)
                rec._on_scroll(1, 1, 0, 1)
            self.assertEqual(calls, ["limit"])
            self.assertEqual(rec.stop_reason, "limit")
            self.assertIn("30", rec.stop_message)
            self.assertEqual(rec.event_count, 25)
            self.assertTrue(rec.is_recording)  # the GUI still has to call stop()
            events = rec.stop()
        self.assertLessEqual(len(events), 30)

    def test_the_reserve_leaves_room_for_the_releases_even_at_the_limit(self):
        with mock.patch.object(recorder, "MAX_EVENTS", 20), mock.patch.object(recorder, "_RESERVE_EVENTS", 5):
            rec, clock, _ = started()
            rec._on_click(1, 1, FakeButton("left"), True)
            rec._on_press(char("a", 65))
            for _ in range(50):
                clock.advance(0.1)
                rec._on_scroll(1, 1, 0, 1)
            events = rec.stop()
        self.assertEqual([e["type"] for e in events[-2:]], ["mouse_click", "key_release"])
        self.assertLessEqual(len(events), 20)

    def test_real_limits_are_consistent(self):
        self.assertLess(recorder._RESERVE_EVENTS, config.MAX_EVENTS)
        self.assertGreaterEqual(recorder._RESERVE_EVENTS, 20)

    def test_time_limit_stops_recording(self):
        calls = []
        with mock.patch.object(recorder, "MAX_RECORDING_SECONDS", 5):
            rec, clock, _ = started(on_auto_stop=calls.append)
            clock.advance(4)
            rec._on_scroll(1, 1, 0, 1)
            clock.advance(2)
            rec._on_scroll(1, 1, 0, 1)
            rec._on_scroll(1, 1, 0, 1)
            self.assertEqual(calls, ["time"])
            self.assertEqual(rec.event_count, 2)
            self.assertIn("hours", rec.stop_message)
            rec.stop()

    def test_crash_in_a_handler_stops_recording_and_notifies(self):
        calls = []
        rec, clock, _ = started(on_auto_stop=calls.append)
        with mock.patch.object(recorder, "key_to_json", side_effect=ValueError("boom")):
            with self.assertLogs("recorder", level="ERROR"):
                rec._on_press(char("a", 65))
        self.assertEqual(calls, ["error"])
        self.assertEqual(rec.stop_reason, "error")
        rec._on_click(1, 1, FakeButton("left"), True)  # nothing more is recorded
        self.assertEqual(rec.event_count, 1)
        self.assertIn("log", rec.stop_message)

    def test_a_crashing_callback_does_not_break_the_recorder(self):
        def bad(reason):
            raise RuntimeError("gui exploded")

        with mock.patch.object(recorder, "MAX_EVENTS", 10), mock.patch.object(recorder, "_RESERVE_EVENTS", 2):
            rec, clock, _ = started(on_auto_stop=bad)
            with self.assertLogs("recorder", level="ERROR"):
                for _ in range(20):
                    clock.advance(0.1)
                    rec._on_scroll(1, 1, 0, 1)
            rec.stop()

    def test_stop_message_is_empty_without_a_reason(self):
        rec, _, _ = started()
        self.assertEqual(rec.stop_message, "")


class ThreadingTests(unittest.TestCase):
    def test_two_threads_hammering_never_produce_a_file_storage_would_reject(self):
        rec = Recorder(position_func=lambda: (0, 0), windows_info=NullWin32())  # the real clock this time
        with mock.patch.object(Recorder, "_make_listeners", return_value=(FakeListener(), FakeListener())):
            rec.start()

        def mouse_thread():
            for i in range(3000):
                rec._on_move(i % 500, i % 300)
                rec._on_scroll(i % 500, 5, 0, 1)

        def keyboard_thread():
            for i in range(1500):
                key = char("abc"[i % 3], 65 + i % 3)
                rec._on_press(key)
                rec._on_release(key)

        with mock.patch.object(recorder, "MOVE_MIN_INTERVAL", 0.0):
            threads = [threading.Thread(target=mouse_thread), threading.Thread(target=keyboard_thread)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        events = rec.stop()
        self.assertGreater(len(events), 4000)
        assert_valid(self, events)  # includes "time never goes backwards"
        self.assertEqual(sorted(e["t"] for e in events), [e["t"] for e in events])


class RoundTripTests(unittest.TestCase):
    def test_a_realistic_session_saves_and_loads_back_identically(self):
        import tempfile

        rec, clock, _ = started(position=(300, 300))
        script = [
            ("move", 310, 305), ("click", 310, 305, "left", True), ("click", 310, 305, "left", False),
            ("scroll", 310, 305, 0, -2), ("press", "h", 72), ("release", "h", 72),
            ("press", "I", 73), ("release", "i", 73),
        ]
        for step in script:
            clock.advance(0.123)
            kind = step[0]
            if kind == "move":
                rec._on_move(step[1], step[2])
            elif kind == "click":
                rec._on_click(step[1], step[2], FakeButton(step[3]), step[4])
            elif kind == "scroll":
                rec._on_scroll(*step[1:])
            elif kind == "press":
                rec._on_press(char(step[1], step[2]))
            else:
                rec._on_release(char(step[1], step[2]))
        recording = storage.make_recording(rec.stop(), 1920, 1080)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "r.json"
            storage.save_recording(path, recording)
            self.assertEqual(storage.load_recording(path), recording)


class FakeWindowsInfo:
    """Which program is where: the foreground program, and one program left of x=500."""

    def __init__(self):
        self.front = {"exe": "notepad.exe", "cls": "Notepad"}
        self.calls = 0
        self.fail = False

    def foreground_context(self):
        self.calls += 1
        if self.fail:
            raise OSError("access denied")
        return self.front

    def context_at(self, x, y):
        self.calls += 1
        if self.fail:
            raise OSError("access denied")
        return {"exe": "chrome.exe", "cls": "Chrome_WidgetWin_1"} if x < 500 else None


class WindowContextTests(unittest.TestCase):
    def test_clicks_scrolls_and_key_presses_note_their_program(self):
        info = FakeWindowsInfo()
        rec, clock, _ = started(windows_info=info)
        clock.advance(0.1)
        rec._on_click(10, 10, FakeButton("left"), True)
        rec._on_click(10, 10, FakeButton("left"), False)
        rec._on_scroll(20, 20, 0, -1)
        rec._on_scroll(900, 20, 0, -1)                   # nothing known there
        rec._on_press(char("a"))
        rec._on_release(char("a"))
        events = rec.stop()
        by_type = [(e["type"], e.get("w")) for e in events[1:]]
        chrome = {"exe": "chrome.exe", "cls": "Chrome_WidgetWin_1"}
        self.assertEqual(by_type, [
            ("mouse_click", chrome), ("mouse_click", None),       # releases carry nothing
            ("mouse_scroll", chrome), ("mouse_scroll", None),
            ("key_press", {"exe": "notepad.exe", "cls": "Notepad"}), ("key_release", None),
        ])
        # it all saves as a version 2 file
        self.assertEqual(storage.make_recording(events, 100, 100)["version"], 2)
        storage.validate_recording(storage.make_recording(events, 100, 100))

    def test_auto_repeat_and_moves_never_ask_windows(self):
        info = FakeWindowsInfo()
        rec, clock, _ = started(windows_info=info)
        rec._on_press(char("a"))
        calls = info.calls
        for _ in range(5):
            rec._on_press(char("a"))                     # held down: auto-repeat
        rec._on_move(5, 5)
        self.assertEqual(info.calls, calls)
        rec.stop()

    def test_a_failing_lookup_just_records_without_it(self):
        info = FakeWindowsInfo()
        info.fail = True
        rec, clock, _ = started(windows_info=info)
        rec._on_press(char("a"))
        rec._on_click(10, 10, FakeButton("left"), True)
        events = rec.stop()
        self.assertTrue(all("w" not in e for e in events))
        self.assertEqual(storage.make_recording(events, 100, 100)["version"], 1)

    def test_odd_answers_are_ignored_and_long_ones_cut(self):
        info = FakeWindowsInfo()
        rec, clock, _ = started(windows_info=info)
        for front in ({"exe": "", "cls": "X"}, "nope", None, {"exe": "a.exe"}):
            info.front = front
            rec._on_press(char("b"))
            rec._on_release(char("b"))
        info.front = {"exe": "x" * 400, "cls": "y" * 400}
        rec._on_press(char("c"))
        events = rec.stop()
        with_w = [e for e in events if "w" in e]
        self.assertEqual(len(with_w), 1)
        self.assertEqual(len(with_w[0]["w"]["exe"]), 260)
        storage.validate_recording(storage.make_recording(events, 100, 100))

    def test_nothing_is_asked_when_keys_and_clicks_are_not_recorded(self):
        info = FakeWindowsInfo()
        rec, clock, _ = started(windows_info=info, options={"keyboard": False, "clicks": False})
        rec._on_press(char("a"))
        rec._on_click(10, 10, FakeButton("left"), True)
        self.assertEqual(info.calls, 0)
        rec.stop()

    def test_default_is_the_real_one_only_on_windows(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(Recorder()._windows_info)


if __name__ == "__main__":
    unittest.main()
