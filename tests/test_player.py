"""Tests for player.py.

Time and input are faked: a fake clock whose waits just advance it, and a fake mouse / keyboard
that write into one shared call log, so ordering and exact timing can be asserted. Real playback
on Windows is checked by hand.

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import logging
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform.startswith("linux"):
    os.environ.setdefault("PYNPUT_BACKEND", "dummy")

from pynput import keyboard, mouse  # noqa: E402

import config  # noqa: E402
import player as player_module  # noqa: E402
import recorder  # noqa: E402
import storage  # noqa: E402
from player import Player  # noqa: E402
from winutil import NullWin32  # noqa: E402

# Tests that provoke errors on purpose would otherwise spray tracebacks over the test output.
logging.getLogger("player").addHandler(logging.NullHandler())


class FakeTime:
    """A clock plus a waiter that 'sleeps' by moving the clock forward."""

    def __init__(self):
        self.now = 500.0
        self.waits = []

    def clock(self):
        return self.now

    def wait(self, event, seconds):
        self.waits.append(seconds)
        if event.is_set():
            return True
        self.now += seconds
        return event.is_set()


class FakeMouse:
    def __init__(self, log, time_source=None):
        self.log = log
        self.time_source = time_source
        self.fail = None             # set to an exception to make press / scroll raise
        self.press_cost = 0.0        # seconds each press "takes"
        self._position = (0, 0)

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = value
        self.log.append(("pos", value))

    def _stamp(self):
        return round(self.time_source.now - 500.0, 4) if self.time_source else None

    def press(self, button):
        if self.fail:
            raise self.fail
        self.log.append(("mouse_press", button.name, self._stamp()))
        if self.time_source:
            self.time_source.now += self.press_cost

    def release(self, button):
        self.log.append(("mouse_release", button.name, self._stamp()))

    def scroll(self, dx, dy):
        if self.fail:
            raise self.fail
        self.log.append(("scroll", dx, dy))


class FakeKeyboard:
    def __init__(self, log, time_source=None):
        self.log = log
        self.time_source = time_source
        self.fail = None
        self.fail_for = None  # only fail for this key

    def _stamp(self):
        return round(self.time_source.now - 500.0, 4) if self.time_source else None

    def press(self, key):
        if self.fail and (self.fail_for is None or key == self.fail_for):
            raise self.fail
        self.log.append(("key_press", key, self._stamp()))

    def release(self, key):
        self.log.append(("key_release", key, self._stamp()))


def char(c, vk=None):
    return {"kind": "char", "value": c, **({"vk": vk} if vk is not None else {})}


def special(name):
    return {"kind": "special", "value": name}


def move(t, x, y):
    return {"t": t, "type": "mouse_move", "x": x, "y": y}


def click(t, x, y, button="left", pressed=True):
    return {"t": t, "type": "mouse_click", "x": x, "y": y, "button": button, "pressed": pressed}


def scroll(t, x, y, dx, dy):
    return {"t": t, "type": "mouse_scroll", "x": x, "y": y, "dx": dx, "dy": dy}


def press(t, key):
    return {"t": t, "type": "key_press", "key": key}


def release(t, key):
    return {"t": t, "type": "key_release", "key": key}


class Rig:
    """A player wired to fakes, plus helpers to run it to completion."""

    def __init__(self, fake_time=True, **player_kwargs):
        self.log = []
        self.time = FakeTime() if fake_time else None
        self.mouse = FakeMouse(self.log, self.time)
        self.keyboard = FakeKeyboard(self.log, self.time)
        self.reasons = []
        self.finished = threading.Event()
        self.playing_when_finished = []
        kwargs = dict(
            on_finished=self._finished,
            mouse_controller=self.mouse,
            keyboard_controller=self.keyboard,
        )
        if self.time:
            kwargs.update(clock=self.time.clock, waiter=self.time.wait)
        kwargs.update(player_kwargs)
        self.player = Player(**kwargs)

    def _finished(self, reason):
        self.playing_when_finished.append(self.player.is_playing)
        self.reasons.append(reason)
        self.finished.set()

    def run(self, events, timeout=10, **options):
        self.player.start(events, **options)
        assert self.finished.wait(timeout), "playback did not finish"
        return self

    def calls(self, *names):
        return [entry for entry in self.log if entry[0] in names]


class StartTests(unittest.TestCase):
    def test_nothing_to_play_is_a_friendly_error(self):
        rig = Rig()
        for events in ([], None):
            with self.subTest(events=events), self.assertRaises(storage.RecordingError) as ctx:
                rig.player.start(events)
            self.assertIn("nothing to play", str(ctx.exception))
        self.assertFalse(rig.player.is_playing)

    def test_invalid_events_are_rejected_before_anything_runs(self):
        rig = Rig()
        bad = [
            [{"t": 0, "type": "teleport"}],
            [move(1.0, 1, 1), move(0.5, 2, 2)],                       # time goes backwards
            [click(0, 1, 1, button="laser")],
            [press(0, {"kind": "special", "value": "banana"})],
            ["not even a dict"],
        ]
        for events in bad:
            with self.subTest(events=events), self.assertRaises(storage.RecordingError):
                rig.player.start(events)
        self.assertEqual(rig.log, [])
        self.assertFalse(rig.player.is_playing)

    def test_events_that_cannot_all_be_skipped_error_helpfully(self):
        rig = Rig()
        with without_x1_button(), self.assertRaises(storage.RecordingError) as ctx:
            rig.player.start([click(0, 1, 1, button="x1")])
        self.assertIn("None of the events", str(ctx.exception))

    def test_controller_failure_gives_a_friendly_error(self):
        player = Player()  # real controllers
        with mock.patch.object(mouse, "Controller", side_effect=OSError("access denied")):
            with self.assertRaises(storage.RecordingError) as ctx:
                player.start([move(0, 1, 1)])
        self.assertIn("access denied", str(ctx.exception))
        self.assertFalse(player.is_playing)

    def test_the_playing_guard_is_up_the_moment_start_returns(self):
        gate = threading.Event()
        rig = Rig(fake_time=False, waiter=lambda event, seconds: gate.wait(5) and False)
        rig.player.start([move(0, 1, 1)])
        self.assertTrue(rig.player.is_playing)
        gate.set()
        self.assertTrue(rig.finished.wait(5))

    def test_starting_while_playing_is_an_error(self):
        gate = threading.Event()
        rig = Rig(fake_time=False, waiter=lambda event, seconds: gate.wait(5) and False)
        rig.player.start([move(0, 1, 1)])
        with self.assertRaises(RuntimeError):
            rig.player.start([move(0, 1, 1)])
        gate.set()
        self.assertTrue(rig.finished.wait(5))


class PlaybackTests(unittest.TestCase):
    def test_every_event_type_is_performed_in_order(self):
        events = [
            move(0.0, 10, 20),
            click(0.1, 11, 21, "left", True),
            click(0.2, 11, 21, "left", False),
            scroll(0.3, 12, 22, 0, -3),
            press(0.4, char("a", 65)),
            release(0.5, char("a", 65)),
            press(0.6, special("enter")),
            release(0.7, special("enter")),
            press(0.8, {"kind": "vk", "value": 0xAF}),
            release(0.9, {"kind": "vk", "value": 0xAF}),
        ]
        rig = Rig().run(events)
        names = [(e[0], *e[1:2]) for e in rig.log]
        self.assertEqual(rig.reasons, ["done"])
        self.assertEqual(
            [e[0] for e in rig.log],
            ["pos", "pos", "mouse_press", "pos", "mouse_release", "pos", "scroll",
             "key_press", "key_release", "key_press", "key_release", "key_press", "key_release"],
        )
        self.assertEqual(rig.log[0], ("pos", (10, 20)))
        self.assertEqual(rig.log[1:3][0], ("pos", (11, 21)))   # moves to the click position first
        self.assertEqual(rig.log[6], ("scroll", 0, -3))
        self.assertEqual(rig.log[5], ("pos", (12, 22)))        # ...and to the scroll position first
        self.assertEqual(rig.player.progress, (10, 10))
        self.assertEqual(rig.player.finish_message, "Playback finished.")

    def test_keys_are_rebuilt_properly(self):
        rig = Rig().run([press(0, char("a", 65)), press(0.1, special("enter")), press(0.2, {"kind": "vk", "value": 175})])
        keys = [e[1] for e in rig.calls("key_press")]
        self.assertEqual(keys[0], keyboard.KeyCode(vk=65, char="a"))
        self.assertEqual(keys[1], keyboard.Key["enter"])
        self.assertEqual(keys[2], keyboard.KeyCode.from_vk(175))

    def test_all_mouse_buttons_this_system_has_are_pressed(self):
        rig = Rig().run([click(0, 1, 1, name, True) for name in ("left", "middle", "right")]
                        + [click(0.1, 1, 1, name, False) for name in ("left", "middle", "right")])
        self.assertEqual([e[1] for e in rig.calls("mouse_press")], ["left", "middle", "right"])
        self.assertEqual([e[1] for e in rig.calls("mouse_release")], ["left", "middle", "right"])

    def test_waits_for_the_start_delay_then_fires_events_at_their_absolute_times(self):
        rig = Rig().run([move(0.0, 1, 1), click(0.5, 2, 2, "left", True), click(1.25, 2, 2, "left", False)])
        self.assertEqual(rig.time.waits[0], config.PLAYBACK_START_DELAY)
        # Times in the log are relative to the clock's origin: delay first, then the recording's own t.
        delay = config.PLAYBACK_START_DELAY
        self.assertAlmostEqual(rig.calls("mouse_press")[0][2], delay + 0.5)
        self.assertAlmostEqual(rig.calls("mouse_release")[0][2], delay + 1.25)

    def test_slow_events_do_not_make_playback_drift(self):
        rig = Rig()
        rig.mouse.press_cost = 0.3  # every press "takes" 0.3 s
        events = [click(1.0, 1, 1, "left", True), click(2.0, 1, 1, "left", True), click(3.0, 1, 1, "left", True)]
        rig.run(events)
        delay = config.PLAYBACK_START_DELAY
        stamps = [e[2] for e in rig.calls("mouse_press")]
        for got, want in zip(stamps, (1.0, 2.0, 3.0)):
            self.assertAlmostEqual(got, delay + want, places=3)  # chained sleeps would give 1.0 / 2.3 / 3.6

    def test_catching_up_fires_late_events_immediately(self):
        rig = Rig()
        rig.mouse.press_cost = 5.0  # one press eats 5 s; the next two are already overdue
        rig.run([click(1.0, 1, 1, "left", True), click(2.0, 1, 1, "left", True), click(3.0, 1, 1, "left", True)])
        waits_after_start = rig.time.waits[1:]
        self.assertEqual(len(waits_after_start), 1)  # waited for the first event only
        self.assertEqual(len(rig.calls("mouse_press")), 3)

    def test_events_with_the_same_time_all_fire_without_waiting(self):
        rig = Rig().run([move(0, 1, 1), move(0, 2, 2), move(0, 3, 3)])
        self.assertEqual(len(rig.calls("pos")), 3)
        self.assertEqual(rig.time.waits, [config.PLAYBACK_START_DELAY])

    def test_on_finished_runs_once_after_the_playing_flag_drops(self):
        rig = Rig().run([move(0, 1, 1)])
        self.assertEqual(rig.reasons, ["done"])
        self.assertEqual(rig.playing_when_finished, [False])
        self.assertEqual(rig.player.finish_reason, "done")

    def test_elapsed_and_progress_freeze_when_playback_ends(self):
        rig = Rig().run([move(0, 1, 1), move(2.0, 2, 2)])
        self.assertAlmostEqual(rig.player.elapsed, 2.0)
        rig.time.now += 100
        self.assertAlmostEqual(rig.player.elapsed, 2.0)
        self.assertEqual(rig.player.progress, (2, 2))

    def test_can_play_again_after_finishing(self):
        rig = Rig()
        rig.run([move(0, 1, 1)])
        rig.finished.clear()
        rig.run([move(0, 5, 5), move(0.1, 6, 6)])
        self.assertEqual(rig.reasons, ["done", "done"])
        self.assertEqual(rig.player.progress, (2, 2))

    def test_a_crashing_on_finished_callback_is_logged_not_raised(self):
        rig = Rig(on_finished=mock.Mock(side_effect=RuntimeError("gui exploded")))
        with self.assertLogs("player", level="ERROR") as logs, mock.patch.object(threading, "excepthook") as hook:
            rig.player.start([move(0, 1, 1)])
            rig.player._thread.join(5)
        self.assertFalse(rig.player.is_playing)
        self.assertIn("on_finished callback failed", logs.output[0])
        hook.assert_not_called()  # nothing escaped the worker thread


class SkippingTests(unittest.TestCase):
    def test_hotkey_like_keys_are_played_like_any_other(self):
        # The hotkey listener ignores injected keys, so playing these can't stop playback.
        names = ("f9", "f10", "f11", "num_lock")
        events = [press(0, special(name)) for name in names]
        events += [release(0.1, special(name)) for name in names]
        rig = Rig().run(events)
        self.assertEqual(len(rig.calls("key_press", "key_release")), 8)
        self.assertEqual(rig.player.skipped_events, 0)
        self.assertEqual(rig.player.finish_message, "Playback finished.")

    def test_keys_this_system_does_not_have_are_skipped(self):
        real = storage.key_from_json

        def fake(data):
            if data == special("media_eject"):
                raise storage.RecordingError("not here")
            return real(data)

        with mock.patch.object(player_module, "key_from_json", side_effect=fake):
            rig = Rig().run([press(0, special("media_eject")), press(0.1, char("a", 65))])
        self.assertEqual(len(rig.calls("key_press")), 1)
        self.assertEqual(rig.player.skipped_events, 1)
        self.assertEqual(rig.player.finish_message, "Playback finished, but 1 event couldn't be played.")

    def test_buttons_this_system_does_not_have_are_skipped(self):
        with without_x1_button():
            rig = Rig().run([click(0, 1, 1, "x1", True), click(0.1, 1, 1, "left", True)])
        self.assertEqual([e[1] for e in rig.calls("mouse_press")], ["left"])
        self.assertEqual(rig.player.skipped_events, 1)
        self.assertEqual(rig.player.progress, (1, 1))  # progress counts what can actually be played


def without_x1_button():
    """Pretend this system's mouse has no X1 side button. Windows really has one (so does
    pynput's Windows backend); Linux's headless backend doesn't. Faking it makes these tests mean
    the same thing everywhere."""
    buttons = types.SimpleNamespace(**{name: getattr(mouse.Button, name)
                                       for name in ("left", "right", "middle") if hasattr(mouse.Button, name)})
    return mock.patch.object(mouse, "Button", buttons)


class FailureTests(unittest.TestCase):
    def test_a_failing_event_is_counted_and_playback_continues(self):
        rig = Rig()
        rig.keyboard.fail = ValueError("InvalidKeyException")
        rig.keyboard.fail_for = keyboard.KeyCode(vk=66, char="b")
        rig.run([press(0, char("a", 65)), press(0.1, char("b", 66)), press(0.2, char("c", 67))])
        self.assertEqual(rig.reasons, ["done"])
        self.assertEqual(len(rig.calls("key_press")), 2)
        self.assertEqual(rig.player.failed_events, 1)
        self.assertIn("1 event couldn't", rig.player.finish_message)

    def test_a_failure_that_keeps_happening_aborts_playback(self):
        rig = Rig()
        rig.mouse.fail = PermissionError("blocked by an elevated window")
        rig.run([scroll(i * 0.01, 1, 1, 0, 1) for i in range(200)])
        self.assertEqual(rig.reasons, ["error"])
        self.assertEqual(rig.player.failed_events, player_module._MAX_CONSECUTIVE_FAILURES)
        self.assertIn("log file", rig.player.finish_message)

    def test_one_success_resets_the_failure_streak(self):
        rig = Rig()
        events = []
        t = 0.0
        for i in range(60):  # fail, fail, ..., succeed, ... never 25 in a row
            events.append(scroll(t, 1, 1, 0, 1) if i % 10 != 9 else move(t, 1, 1))
            t += 0.01
        rig.mouse.fail = RuntimeError("x")
        rig.run(events)
        self.assertEqual(rig.reasons, ["done"])
        self.assertEqual(rig.player.failed_events, 54)

    def test_a_crash_in_the_worker_is_reported_as_an_error(self):
        rig = Rig()
        with mock.patch.object(Player, "_play", side_effect=RuntimeError("boom")):
            rig.run([move(0, 1, 1)])
        self.assertEqual(rig.reasons, ["error"])
        self.assertFalse(rig.player.is_playing)


class HeldInputTests(unittest.TestCase):
    def test_keys_and_buttons_left_held_by_a_recording_are_released_at_the_end(self):
        events = [
            press(0.0, special("shift")),
            press(0.1, char("a", 65)),
            click(0.2, 5, 6, "left", True),
        ]
        rig = Rig().run(events)
        releases = rig.calls("mouse_release", "key_release")
        self.assertEqual([e[0] for e in releases], ["mouse_release", "key_release", "key_release"])
        self.assertEqual(releases[0][1], "left")
        self.assertEqual(releases[1][1], keyboard.KeyCode(vk=65, char="a"))   # last pressed, first released
        self.assertEqual(releases[2][1], keyboard.Key["shift"])

    def test_a_properly_released_key_is_not_released_twice(self):
        rig = Rig().run([press(0, char("a", 65)), release(0.1, char("a", 65))])
        self.assertEqual(len(rig.calls("key_release")), 1)

    def test_shifted_release_still_matches_the_press(self):
        # 'A' pressed, 'a' released (same vk): must count as released, not "still held".
        rig = Rig().run([press(0, char("A", 65)), release(0.1, char("a", 65))])
        self.assertEqual(len(rig.calls("key_release")), 1)

    def test_a_failing_release_does_not_stop_the_other_releases(self):
        rig = Rig()
        original = rig.keyboard.release
        calls = []

        def flaky(key):
            calls.append(key)
            if len(calls) == 1:
                raise RuntimeError("release failed")
            original(key)

        rig.keyboard.release = flaky
        rig.run([press(0, char("a", 65)), press(0.1, char("b", 66))])
        self.assertEqual(len(calls), 2)
        self.assertEqual(rig.reasons, ["done"])


class StopTests(unittest.TestCase):
    """These use real threads and real time: Stop has to be instant."""

    def start_long(self, events):
        rig = Rig(fake_time=False)
        rig.player.start(events)
        return rig

    def wait_until(self, condition, timeout=5):
        end = threading.Event()
        for _ in range(int(timeout / 0.01)):
            if condition():
                return True
            end.wait(0.01)
        return False

    def test_stop_interrupts_a_long_wait_immediately(self):
        rig = self.start_long([move(0, 1, 1), move(600, 2, 2)])
        self.assertTrue(self.wait_until(lambda: rig.player.progress[0] == 1))
        import time

        began = time.perf_counter()
        rig.player.stop()
        took = time.perf_counter() - began
        self.assertLess(took, 1.0)
        self.assertFalse(rig.player.is_playing)
        self.assertEqual(rig.reasons, ["stopped"])
        self.assertEqual(rig.player.finish_message, "Playback stopped.")
        self.assertEqual(rig.player.progress, (1, 2))

    def test_stop_during_the_start_delay_plays_nothing(self):
        rig = Rig(fake_time=False)
        with mock.patch.object(player_module, "PLAYBACK_START_DELAY", 30):
            rig.player.start([move(0, 1, 1)])
            rig.player.stop()
        self.assertEqual(rig.reasons, ["stopped"])
        self.assertEqual(rig.log, [])
        self.assertEqual(rig.player.elapsed, 0.0)

    def test_stop_releases_whatever_is_held(self):
        rig = self.start_long([
            press(0.0, special("ctrl")), press(0.0, char("c", 67)), click(0.0, 3, 4, "right", True),
            move(600, 9, 9),
        ])
        self.assertTrue(self.wait_until(lambda: rig.player.progress[0] == 3))
        rig.player.stop()
        released = rig.calls("mouse_release", "key_release")
        self.assertEqual([e[0] for e in released], ["mouse_release", "key_release", "key_release"])
        self.assertEqual(released[0][1], "right")
        self.assertEqual(released[2][1], keyboard.Key["ctrl"])
        self.assertEqual(rig.calls("pos")[-1], ("pos", (3, 4)))  # the move at t=600 never happened

    def test_stop_is_noticed_even_when_playback_is_behind_schedule(self):
        # Late events never wait, so the per-event stop check is the only thing that can notice Stop.
        rig = Rig()
        seen = []
        real_set = FakeMouse.position.fset

        def stopping_setter(mouse_self, value):
            real_set(mouse_self, value)
            seen.append(value)
            if len(seen) == 2:
                rig.player.stop()  # called on the worker thread, like a hotkey firing mid-playback

        with mock.patch.object(FakeMouse, "position", FakeMouse.position.setter(stopping_setter)):
            rig.run([move(0, i, i) for i in range(1, 8)])
        self.assertEqual(rig.reasons, ["stopped"])
        self.assertEqual(len(rig.calls("pos")), 2)

    def test_stop_is_safe_when_idle_and_when_called_twice(self):
        rig = Rig()
        rig.player.stop()  # never started
        rig.run([move(0, 1, 1)])
        rig.player.stop()  # already finished
        rig.player.stop()
        self.assertEqual(rig.reasons, ["done"])

    def test_a_stopped_player_can_play_again(self):
        rig = self.start_long([move(0, 1, 1), move(600, 2, 2)])
        self.assertTrue(self.wait_until(lambda: rig.player.progress[0] == 1))
        rig.player.stop()
        rig.finished.clear()
        rig.player.start([move(0, 7, 7)])
        self.assertTrue(rig.finished.wait(5))
        self.assertEqual(rig.reasons, ["stopped", "done"])
        self.assertEqual(rig.calls("pos")[-1], ("pos", (7, 7)))

    def test_stop_from_inside_on_finished_does_not_deadlock(self):
        holder = {}
        done = threading.Event()

        def callback(reason):
            holder["player"].stop()  # runs on the worker thread: joining itself would raise
            done.set()

        rig = Rig(on_finished=callback)
        holder["player"] = rig.player
        rig.player.start([move(0, 1, 1)])
        self.assertTrue(done.wait(5))

    def test_the_default_waiter_wakes_early_when_the_event_is_set(self):
        import time

        event = threading.Event()
        threading.Timer(0.05, event.set).start()
        began = time.perf_counter()
        self.assertTrue(player_module._wait_on_event(event, 30))
        self.assertLess(time.perf_counter() - began, 2)
        self.assertFalse(player_module._wait_on_event(threading.Event(), 0.01))


class RecorderRoundTripTests(unittest.TestCase):
    def test_what_the_recorder_produces_the_player_replays(self):
        class Clock:
            now = 100.0

            def __call__(self):
                return self.now

        clock = Clock()
        rec = recorder.Recorder(clock=clock, position_func=lambda: (50, 60), windows_info=NullWin32())
        with mock.patch.object(recorder.Recorder, "_make_listeners", return_value=(mock.Mock(), mock.Mock())):
            rec.start()

        class Button:
            def __init__(self, name):
                self.name = name

        for step in range(1, 4):
            clock.now += 0.2
            rec._on_move(50 + step, 60 + step)
        clock.now += 0.2
        rec._on_click(53, 63, Button("left"), True)
        clock.now += 0.1
        rec._on_click(53, 63, Button("left"), False)
        clock.now += 0.2
        rec._on_press(keyboard.KeyCode(vk=72, char="H"))     # Shift+h style: 'H' down...
        clock.now += 0.1
        rec._on_release(keyboard.KeyCode(vk=72, char="h"))   # ...'h' up
        clock.now += 0.2
        rec._on_press(keyboard.Key.enter)                    # left held at the end on purpose
        events = rec.stop()

        rig = Rig().run(events)
        self.assertEqual(rig.reasons, ["done"])
        self.assertEqual(rig.player.skipped_events, 0)
        self.assertEqual(rig.player.failed_events, 0)
        self.assertEqual(len(rig.calls("mouse_press")), 1)
        self.assertEqual(len(rig.calls("key_press")), 2)
        self.assertEqual(len(rig.calls("key_release")), 2)  # including the one the recorder added for Enter


# ---- Repeat, delay between runs, speed (step 8) -----------------------------------------

CLICK_PAIR = [click(0.0, 5, 5), click(0.2, 5, 5, pressed=False)]


def stop_after_waits(rig, count):
    """Make the rig's player get stopped from inside its own waiter once it has waited `count` times."""
    original = rig.time.wait

    def waiter(event, seconds):
        if len(rig.time.waits) >= count:
            event.set()
        return original(event, seconds)

    rig.player._waiter = waiter


class RepeatTests(unittest.TestCase):
    def test_default_is_a_single_run_with_the_old_message(self):
        rig = Rig().run(CLICK_PAIR)
        self.assertEqual(rig.reasons, ["done"])
        self.assertEqual(rig.player.total_runs, 1)
        self.assertEqual(rig.player.completed_runs, 1)
        self.assertEqual(rig.player.finish_message, "Playback finished.")

    def test_repeat_plays_every_event_that_many_times(self):
        rig = Rig().run(CLICK_PAIR, repeat=3)
        self.assertEqual(rig.reasons, ["done"])
        self.assertEqual(len(rig.calls("mouse_press")), 3)
        self.assertEqual(len(rig.calls("mouse_release")), 3)
        self.assertEqual(rig.player.total_runs, 3)
        self.assertEqual(rig.player.current_run, 3)
        self.assertEqual(rig.player.completed_runs, 3)
        self.assertEqual(rig.player.finish_message, "Playback finished (3 runs).")

    def test_progress_counts_events_of_the_current_run(self):
        rig = Rig().run(CLICK_PAIR, repeat=3)
        self.assertEqual(rig.player.progress, (2, 2))

    def test_each_run_has_its_own_clock_and_the_delay_is_waited_between_runs(self):
        rig = Rig().run(CLICK_PAIR, repeat=3, delay=1.0)
        presses = [entry[2] for entry in rig.calls("mouse_press")]
        # start delay 0.3, then each run takes 0.2 s and is followed by a 1.0 s pause
        self.assertEqual(presses, [0.3, 1.5, 2.7])
        self.assertEqual(rig.time.waits.count(1.0), 2)   # between runs only, not after the last one

    def test_no_pause_after_a_single_run_even_with_a_delay(self):
        rig = Rig().run(CLICK_PAIR, repeat=1, delay=7.0)
        self.assertNotIn(7.0, rig.time.waits)

    def test_the_start_delay_is_only_waited_once(self):
        rig = Rig().run(CLICK_PAIR, repeat=3)
        self.assertEqual(rig.time.waits.count(config.PLAYBACK_START_DELAY), 1)

    def test_zero_means_until_stopped(self):
        rig = Rig()
        stop_after_waits(rig, 12)
        rig.run(CLICK_PAIR, repeat=0)
        self.assertEqual(rig.reasons, ["stopped"])
        self.assertEqual(rig.player.total_runs, 0)
        self.assertGreater(rig.player.completed_runs, 2)   # went well past any small count
        self.assertEqual(
            rig.player.finish_message, f"Playback stopped during run {rig.player.current_run}."
        )
        self.assertEqual(len(rig.calls("mouse_press")), len(rig.calls("mouse_release")))

    def test_stop_during_the_pause_between_runs_is_instant_and_says_which_run(self):
        rig = Rig()
        original = rig.time.wait

        def waiter(event, seconds):
            if seconds == 5.0:        # the pause between runs
                event.set()
            return original(event, seconds)

        rig.player._waiter = waiter
        rig.run(CLICK_PAIR, repeat=3, delay=5.0)
        self.assertEqual(rig.reasons, ["stopped"])
        self.assertEqual(len(rig.calls("mouse_press")), 1)       # run 2 never began
        self.assertEqual(rig.player.completed_runs, 1)
        self.assertEqual(rig.player.finish_message, "Playback stopped during run 1 of 3.")
        self.assertEqual(rig.playing_when_finished, [False])

    def test_stopped_in_a_finite_run_reports_run_x_of_y(self):
        rig = Rig()
        stop_after_waits(rig, 4)
        rig.run(CLICK_PAIR, repeat=5)
        self.assertEqual(rig.reasons, ["stopped"])
        self.assertRegex(rig.player.finish_message, r"^Playback stopped during run \d+ of 5\.$")

    def test_keys_held_at_the_end_of_a_run_are_released_before_the_next_run(self):
        shift = special("shift")
        events = [press(0.0, shift), click(0.1, 1, 1), click(0.2, 1, 1, pressed=False)]  # shift never released
        rig = Rig().run(events, repeat=2)
        names = [entry[0] for entry in rig.calls("key_press", "key_release")]
        self.assertEqual(names, ["key_press", "key_release", "key_press", "key_release"])

    def test_a_zero_length_recording_on_endless_repeat_cannot_spin(self):
        rig = Rig()
        stop_after_waits(rig, 20)
        rig.run([move(0.0, 1, 1)], repeat=0)
        pauses = rig.time.waits[1:]          # everything after the start delay
        self.assertTrue(pauses)
        for pause in pauses:
            self.assertGreaterEqual(pause, config.MIN_PASS_SECONDS - 1e-9)

    def test_a_pause_is_not_stretched_when_the_run_was_already_long_enough(self):
        rig = Rig().run([click(0.0, 1, 1), click(1.0, 1, 1, pressed=False)], repeat=2, delay=0.0)
        # the run lasted 1.0 s, far above the minimum, so no extra pause is added
        self.assertEqual([w for w in rig.time.waits if 0 < w < config.MIN_PASS_SECONDS], [])

    def test_failures_add_up_across_runs_and_abort_an_endless_loop(self):
        rig = Rig()
        rig.mouse.fail = OSError("blocked")        # e.g. playing into an elevated window
        rig.run([click(0.0, 1, 1)], repeat=0)
        self.assertEqual(rig.reasons, ["error"])
        self.assertGreaterEqual(rig.player.failed_events, 25)
        self.assertGreater(rig.player.current_run, 1)     # it took several runs to give up

    def test_finished_message_mentions_problems_and_runs(self):
        events = [click(0.0, 1, 1, button="x1"), click(0.1, 2, 2), click(0.2, 2, 2, pressed=False)]
        with without_x1_button():
            rig = Rig().run(events, repeat=2)
        self.assertEqual(rig.player.finish_message, "Playback finished (2 runs), but 1 event couldn't be played.")


class SpeedTests(unittest.TestCase):
    def test_double_speed_halves_the_gaps(self):
        rig = Rig().run([click(0.0, 1, 1), click(1.0, 1, 1, pressed=False)], speed=2.0)
        self.assertEqual(rig.calls("mouse_release")[0][2], 0.8)    # 0.3 start delay + 1.0 / 2

    def test_half_speed_doubles_the_gaps(self):
        rig = Rig().run([click(0.0, 1, 1), click(1.0, 1, 1, pressed=False)], speed=0.5)
        self.assertEqual(rig.calls("mouse_release")[0][2], 2.3)

    def test_speed_applies_to_every_run(self):
        rig = Rig().run([click(0.0, 1, 1), click(1.0, 1, 1, pressed=False)], repeat=2, speed=2.0)
        self.assertEqual([entry[2] for entry in rig.calls("mouse_press")], [0.3, 0.8])


class OptionValidationTests(unittest.TestCase):
    def assert_rejected(self, **options):
        rig = Rig()
        with self.assertRaises(storage.RecordingError, msg=str(options)):
            rig.player.start(CLICK_PAIR, **options)
        self.assertFalse(rig.player.is_playing)
        self.assertEqual(rig.log, [])

    def test_bad_repeat_values(self):
        for value in (-1, 1.5, True, "3", None, config.MAX_REPEAT + 1):
            with self.subTest(repeat=value):
                self.assert_rejected(repeat=value)

    def test_bad_delay_values(self):
        for value in (-0.1, float("nan"), float("inf"), "1", None, True, config.MAX_REPEAT_DELAY + 1):
            with self.subTest(delay=value):
                self.assert_rejected(delay=value)

    def test_bad_speed_values(self):
        for value in (0, -1, config.MIN_SPEED / 2, config.MAX_SPEED + 1, float("nan"), float("inf"), "2", True):
            with self.subTest(speed=value):
                self.assert_rejected(speed=value)

    def test_the_limits_themselves_are_accepted(self):
        for options in (
            {"repeat": 0},
            {"repeat": config.MAX_REPEAT},
            {"delay": 0},
            {"delay": config.MAX_REPEAT_DELAY},
            {"speed": config.MIN_SPEED},
            {"speed": config.MAX_SPEED},
        ):
            with self.subTest(options=options):
                rig = Rig()
                stop_after_waits(rig, 3)      # keep the huge ones short
                rig.run(CLICK_PAIR, **options)
                self.assertTrue(rig.reasons)

    def test_messages_are_gui_ready(self):
        rig = Rig()
        for options, fragment in (({"repeat": -1}, "Repeat"), ({"delay": -1}, "delay"), ({"speed": 0}, "Speed")):
            with self.subTest(options=options), self.assertRaises(storage.RecordingError) as ctx:
                rig.player.start(CLICK_PAIR, **options)
            self.assertIn(fragment, str(ctx.exception))

    def test_start_while_playing_still_raises_runtime_error(self):
        gate = threading.Event()
        rig = Rig(fake_time=False, waiter=lambda event, seconds: gate.wait(5) and False)
        rig.player.start(CLICK_PAIR, repeat=2)
        with self.assertRaises(RuntimeError):
            rig.player.start(CLICK_PAIR, repeat=2)
        gate.set()
        self.assertTrue(rig.finished.wait(5))


NOTEPAD = {"exe": "notepad.exe", "cls": "Notepad"}


class GuardTests(unittest.TestCase):
    """The failsafe hook: guard(operation, x, y, context, stop_event) before risky events."""

    def events(self):
        return [
            move(0.0, 1, 1),
            dict(click(0.1, 5, 6), w=NOTEPAD), click(0.2, 5, 6, pressed=False),
            dict(scroll(0.3, 7, 8, 0, -1), w=NOTEPAD),
            dict(press(0.4, char("a")), w=NOTEPAD), release(0.5, char("a")),
            press(0.6, char("b")), release(0.7, char("b")),
        ]

    def test_guard_sees_presses_clicks_and_scrolls_with_their_context(self):
        seen = []
        rig = Rig()
        rig.run(self.events(), guard=lambda op, x, y, w, ev: seen.append((op, x, y, w)) or None)
        self.assertEqual(seen, [("click", 5, 6, NOTEPAD), ("scroll", 7, 8, NOTEPAD),
                                ("press", None, None, NOTEPAD), ("press", None, None, None)])
        self.assertEqual(rig.reasons, ["done"])
        self.assertEqual(rig.player.finish_message.split()[0] if rig.player.stop_text else "", "")

    def test_a_problem_stops_before_the_event_and_says_why(self):
        def guard(op, x, y, w, ev):
            return "expected notepad.exe in front, found chrome.exe" if op == "press" else None
        rig = Rig()
        rig.run(self.events(), guard=guard)
        self.assertEqual(rig.reasons, ["stopped"])
        self.assertEqual(rig.calls("key_press"), [])          # the key never went out
        self.assertEqual(len(rig.calls("mouse_press")), 1)
        self.assertEqual(rig.player.finish_message,
                         "Playback stopped: expected notepad.exe in front, found chrome.exe")

    def test_a_crashing_guard_never_stops_playback(self):
        rig = Rig()
        rig.run(self.events(), guard=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(rig.reasons, ["done"])
        self.assertEqual(len(rig.calls("key_press")), 2)

    def test_stop_with_a_reason(self):
        rig = Rig(fake_time=False)
        rig.player.start([move(0.0, 1, 1), press(30.0, char("a"))])
        rig.player.stop("you pressed a key")
        assert rig.finished.wait(5)
        self.assertEqual(rig.player.finish_message, "Playback stopped: you pressed a key")
        # the first reason wins, and a plain stop afterwards keeps it
        rig.player.stop("something else")
        self.assertEqual(rig.player.stop_text, "you pressed a key")
        # the next playback starts clean
        rig.finished.clear()
        rig.run([move(0.0, 1, 1)])
        self.assertIsNone(rig.player.stop_text)

    def test_a_reason_given_while_not_playing_is_ignored(self):
        rig = Rig()
        rig.player.stop("late")
        self.assertIsNone(rig.player.stop_text)

    def test_on_cursor_hears_every_position_before_the_mouse_moves(self):
        positions = []
        rig = Rig()

        def on_cursor(x, y):
            positions.append((x, y, rig.mouse.position))

        rig.run(self.events(), on_cursor=on_cursor)
        self.assertEqual([(x, y) for x, y, _ in positions], [(1, 1), (5, 6), (5, 6), (7, 8)])
        self.assertEqual(positions[1][2], (1, 1))            # told BEFORE the move happened

    def test_falling_far_behind_stops_it(self):
        rig = Rig()
        rig.mouse.press_cost = 9.0            # the PC "sleeps" for 9 s during the first click
        rig.run([move(0.0, 1, 1), click(0.1, 1, 1), click(0.2, 1, 1, pressed=False), press(0.3, char("a"))],
                max_lag=5.0)
        self.assertEqual(rig.reasons, ["stopped"])
        self.assertEqual(rig.calls("key_press"), [])
        self.assertIn("behind schedule (the PC was asleep or froze)", rig.player.finish_message)

    def test_small_lag_or_no_limit_plays_on(self):
        for lag, limit in ((2.0, 5.0), (30.0, None)):
            with self.subTest(lag=lag, limit=limit):
                rig = Rig()
                rig.mouse.press_cost = lag
                rig.run([move(0.0, 1, 1), click(0.1, 1, 1), click(0.2, 1, 1, pressed=False),
                         press(0.3, char("a"))], max_lag=limit)
                self.assertEqual(rig.reasons, ["done"])

    def test_next_round_gives_every_run_its_own_events(self):
        rig = Rig()
        asked = []

        def next_round(run):
            asked.append(run)
            return [move(0.0, run, run), press(0.1, char(str(run))), release(0.2, char(str(run)))]

        rig.run([move(0.0, 1, 1), press(0.1, char("1")), release(0.2, char("1"))], repeat=3, next_round=next_round)
        self.assertEqual(asked, [2, 3])
        typed = [entry[1] for entry in rig.calls("key_press")]
        self.assertEqual([getattr(k, "char", k) for k in typed], ["1", "2", "3"])
        self.assertEqual(rig.player.completed_runs, 3)

    def test_a_bad_next_round_stops_with_its_message(self):
        rig = Rig()
        rig.run([move(0.0, 1, 1)], repeat=0,
                next_round=mock.Mock(side_effect=storage.RecordingError("Every step is switched off.")))
        self.assertEqual(rig.reasons, ["stopped"])
        self.assertEqual(rig.player.finish_message, "Playback stopped: Every step is switched off")
        rig2 = Rig()
        rig2.run([move(0.0, 1, 1)], repeat=2, next_round=lambda run: [{"t": -1}])
        self.assertIn("Playback stopped", rig2.player.finish_message)

    def test_run_position_is_in_recording_time(self):
        rig = Rig()
        rig.run([move(0.0, 1, 1), move(4.0, 2, 2)], speed=2.0)
        self.assertAlmostEqual(rig.player.run_position, 4.0, delta=0.2)

    def test_version_2_events_with_context_play(self):
        rig = Rig()
        rig.run(self.events())
        self.assertEqual(rig.reasons, ["done"])


if __name__ == "__main__":
    unittest.main()
