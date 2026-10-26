"""Tests for hotkeys.py.

pynput's listener is faked. The Windows hook filter is called directly with fake KBDLLHOOKSTRUCT
data, exactly the way pynput calls it on Windows, so swallowing is tested on any system. Real key
presses are checked by hand on Windows (WINDOWS_CHECKLIST.md).

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import logging
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform.startswith("linux"):
    os.environ.setdefault("PYNPUT_BACKEND", "dummy")

from pynput import keyboard  # noqa: E402

import hotkey_spec as hs  # noqa: E402
import hotkeys as hotkeys_module  # noqa: E402
from hotkeys import Hotkeys  # noqa: E402

# The pre-1.7.4 defaults (Ctrl+` / NumLock / F10). The behaviour tests use them on purpose: a binding
# with a modifier (Ctrl+`) and a lock key (NumLock) exercise more of the matcher than plain F-keys.
CLASSIC = {"record": {"mods": ["ctrl"], "vk": 0xC0}, "play": {"mods": [], "vk": 0x90},
           "stop": {"mods": [], "vk": 0x79}}


logging.getLogger("hotkeys").addHandler(logging.NullHandler())

WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
LCTRL, GRAVE, NUMLOCK, F10 = 0xA2, 0xC0, 0x90, 0x79


class Swallowed(Exception):
    """What FakeListener.suppress_event raises (pynput raises its own SuppressException)."""


class FakeData:
    def __init__(self, vk, flags=0):
        self.vkCode = vk
        self.flags = flags


class FakeListener:
    instances = []
    fail_on_start = None
    comes_up = True

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.daemon = False
        self.running = False
        self.started = self.waited = self.stopped = 0
        self.stop_error = None
        FakeListener.instances.append(self)

    def start(self):
        self.started += 1
        if FakeListener.fail_on_start is not None:
            raise FakeListener.fail_on_start
        self.running = FakeListener.comes_up

    def wait(self):
        self.waited += 1

    def stop(self):
        self.stopped += 1
        self.running = False
        if self.stop_error is not None:
            raise self.stop_error

    def suppress_event(self):
        raise Swallowed()


def _tracked_only():
    """A modifier reader that always fails, so the matcher tracks modifiers from the keys it sees.
    Without it, on a real Windows machine the tests would read the real keyboard (nothing held)."""
    raise OSError("tests track modifiers from the fake key events")


class HotkeysCase(unittest.TestCase):
    windows = True
    track_modifiers = staticmethod(_tracked_only)

    def setUp(self):
        FakeListener.instances = []
        FakeListener.fail_on_start = None
        FakeListener.comes_up = True
        self.actions = []

    def make(self, **kwargs):
        options = dict(on_action=self.actions.append, windows=self.windows, listener_factory=FakeListener,
                       mods_reader=self.track_modifiers, bindings=CLASSIC)
        options.update(kwargs)
        hk = Hotkeys(**options)
        self.addCleanup(hk.stop)
        return hk

    @property
    def listener(self):
        return FakeListener.instances[-1]

    def started(self, **kwargs):
        hk = self.make(**kwargs)
        hk.start()
        return hk

    def send(self, msg, vk, flags=0):
        """Run the hook filter like pynput does. Returns "swallowed" or "passed"."""
        try:
            self.listener.kwargs["win32_event_filter"](msg, FakeData(vk, flags))
        except Swallowed:
            return "swallowed"
        return "passed"

    def tap(self, vk, flags=0, down=WM_KEYDOWN, up=WM_KEYUP):
        return (self.send(down, vk, flags), self.send(up, vk, flags))


class TestLifecycle(HotkeysCase):
    def test_not_running_before_start(self):
        self.assertFalse(self.make().is_running)

    def test_windows_uses_the_hook_filter(self):
        self.started()
        self.assertEqual(set(self.listener.kwargs), {"win32_event_filter"})

    def test_elsewhere_uses_press_and_release(self):
        self.windows = False
        self.started()
        self.assertEqual(set(self.listener.kwargs), {"on_press", "on_release"})

    def test_daemon_started_and_waited_for(self):
        hk = self.started()
        self.assertTrue(hk.is_running)
        self.assertTrue(self.listener.daemon)
        self.assertEqual((self.listener.started, self.listener.waited), (1, 1))

    def test_start_twice_is_a_no_op(self):
        hk = self.started()
        hk.start()
        self.assertEqual(len(FakeListener.instances), 1)

    def test_start_failure_is_a_runtime_error(self):
        FakeListener.fail_on_start = OSError("no hook for you")
        with self.assertRaises(RuntimeError) as ctx:
            self.make().start()
        self.assertIn("no hook for you", str(ctx.exception))

    def test_listener_that_never_comes_up(self):
        FakeListener.comes_up = False
        hk = self.make()
        with self.assertRaises(RuntimeError):
            hk.start()
        self.assertFalse(hk.is_running)

    def test_stop_is_safe_any_time(self):
        hk = self.make()
        hk.stop()
        hk.start()
        hk.stop()
        hk.stop()
        self.assertEqual(self.listener.stopped, 1)
        self.assertFalse(hk.is_running)

    def test_stop_error_is_swallowed(self):
        hk = self.started()
        self.listener.stop_error = RuntimeError("boom")
        hk.stop()

    def test_default_bindings(self):
        self.assertEqual(self.make(bindings=None).bindings, hs.DEFAULT_BINDINGS)

    def test_swallowing_only_on_windows(self):
        self.assertTrue(self.make().swallowing)
        self.assertFalse(self.make(swallow=False).swallowing)
        self.assertFalse(self.make(windows=False).swallowing)

    def test_real_pynput_listener_accepts_our_arguments(self):
        # Without a factory the real keyboard.Listener is used; just check it can be built.
        hk = Hotkeys(on_action=lambda a: None, windows=False)
        listener = hk._make_listener()
        self.assertIsInstance(listener, keyboard.Listener)


class TestWindowsSwallowing(HotkeysCase):
    def test_numlock_plays_and_is_swallowed(self):
        self.started()
        self.assertEqual(self.tap(NUMLOCK), ("swallowed", "swallowed"))
        self.assertEqual(self.actions, ["play"])

    def test_ctrl_grave_records_ctrl_passes_grave_is_swallowed(self):
        self.started()
        self.assertEqual(self.send(WM_KEYDOWN, LCTRL), "passed")
        self.assertEqual(self.tap(GRAVE), ("swallowed", "swallowed"))
        self.assertEqual(self.send(WM_KEYUP, LCTRL), "passed")
        self.assertEqual(self.actions, ["record"])

    def test_sys_messages_count_too(self):
        # Keys pressed with Alt arrive as WM_SYSKEYDOWN / WM_SYSKEYUP.
        self.started(bindings={"record": {"vk": 0x52, "mods": ["alt"]}, "play": CLASSIC["play"],
                               "stop": CLASSIC["stop"]})
        self.send(WM_SYSKEYDOWN, 0xA4)
        self.assertEqual(self.tap(0x52, down=WM_SYSKEYDOWN, up=WM_SYSKEYUP), ("swallowed", "swallowed"))
        self.assertEqual(self.actions, ["record"])

    def test_ordinary_keys_pass(self):
        self.started()
        self.assertEqual(self.tap(0x41), ("passed", "passed"))
        self.assertEqual(self.tap(GRAVE), ("passed", "passed"))   # ` without Ctrl
        self.assertEqual(self.actions, [])

    def test_injected_keys_pass_and_do_nothing(self):
        self.started()
        for flags in (0x10, 0x02):
            with self.subTest(flags=flags):
                self.assertEqual(self.tap(NUMLOCK, flags=flags), ("passed", "passed"))
        self.assertEqual(self.actions, [])

    def test_swallow_off_still_fires(self):
        self.started(swallow=False)
        self.assertEqual(self.tap(F10), ("passed", "passed"))
        self.assertEqual(self.actions, ["stop"])

    def test_disabled_passes_everything(self):
        hk = self.started()
        hk.set_enabled(False)
        self.assertFalse(hk.enabled)
        self.assertEqual(self.tap(NUMLOCK), ("passed", "passed"))
        hk.set_enabled(True)
        self.assertEqual(self.tap(NUMLOCK), ("swallowed", "swallowed"))
        self.assertEqual(self.actions, ["play"])

    def test_new_bindings_apply_at_once(self):
        hk = self.started()
        hk.set_bindings({"record": {"vk": 0x78, "mods": []}, "play": {"vk": 0x7A, "mods": []},
                         "stop": {"vk": F10, "mods": []}})
        self.assertEqual(self.tap(NUMLOCK), ("passed", "passed"))
        self.assertEqual(self.tap(0x7A), ("swallowed", "swallowed"))
        self.assertEqual(self.actions, ["play"])

    def test_actions_go_through_dispatch(self):
        hops = []
        self.started(dispatch=hops.append)
        self.tap(NUMLOCK)
        self.assertEqual(self.actions, [])
        self.assertEqual(len(hops), 1)
        hops[0]()
        self.assertEqual(self.actions, ["play"])

    def test_a_failing_handler_is_contained(self):
        def boom(action):
            raise ValueError(action)
        self.started(on_action=boom)
        self.assertEqual(self.tap(NUMLOCK), ("swallowed", "swallowed"))

    def test_a_failing_dispatch_is_contained(self):
        def bad_dispatch(fn):
            raise RuntimeError("main loop gone")
        self.started(dispatch=bad_dispatch)
        self.assertEqual(self.tap(NUMLOCK), ("swallowed", "swallowed"))

    def test_garbage_data_passes(self):
        self.started()
        try:
            result = self.listener.kwargs["win32_event_filter"](WM_KEYDOWN, object())
        except Swallowed:
            self.fail("garbage must not be swallowed")
        self.assertIs(result, False)

    def test_other_messages_are_ignored(self):
        self.started()
        self.assertEqual(self.send(0x0200, NUMLOCK), "passed")
        self.assertEqual(self.actions, [])

    def test_filter_works_before_start_returns(self):
        # The hook can deliver a key while start() is still waiting for it to come up.
        hk = self.make()
        original_wait = FakeListener.wait

        def wait_and_press(listener):
            original_wait(listener)
            try:
                listener.kwargs["win32_event_filter"](WM_KEYDOWN, FakeData(NUMLOCK))
            except Swallowed:
                self.swallowed_early = True

        self.swallowed_early = False
        FakeListener.wait = wait_and_press
        try:
            hk.start()
        finally:
            FakeListener.wait = original_wait
        self.assertTrue(self.swallowed_early)


class TestModifierReader(HotkeysCase):
    def test_the_live_state_is_used_on_windows(self):
        held = set()
        self.started(mods_reader=lambda: held)
        self.send(WM_KEYDOWN, LCTRL)                 # its key up never arrives (lock screen)
        self.assertEqual(self.tap(NUMLOCK), ("swallowed", "swallowed"))
        self.assertEqual(self.actions, ["play"])

    def test_without_windll_it_falls_back_to_tracking(self):
        try:
            hotkeys_module.windows_modifier_reader()
        except Exception:
            pass
        else:
            self.skipTest("this machine can read the real modifier keys")
        with self.assertLogs("hotkeys", level="WARNING"):
            hk = Hotkeys(on_action=self.actions.append, windows=True, listener_factory=FakeListener, bindings=CLASSIC)
        self.addCleanup(hk.stop)
        hk.start()
        self.send(WM_KEYDOWN, LCTRL)
        self.assertEqual(self.tap(GRAVE), ("swallowed", "swallowed"))
        self.assertEqual(self.actions, ["record"])


class TestOtherSystems(HotkeysCase):
    windows = False

    def press(self, key, injected=False):
        self.listener.kwargs["on_press"](key, injected)

    def release(self, key, injected=False):
        self.listener.kwargs["on_release"](key, injected)

    def test_ctrl_grave(self):
        self.started()
        self.press(_Named("ctrl_l"))
        self.press(keyboard.KeyCode.from_char("`"))
        self.release(keyboard.KeyCode.from_char("`"))
        self.release(_Named("ctrl_l"))
        self.assertEqual(self.actions, ["record"])

    def test_numlock_and_injected(self):
        self.started()
        self.press(_Named("num_lock"), injected=True)
        self.release(_Named("num_lock"), injected=True)
        self.press(_Named("num_lock"))
        self.release(_Named("num_lock"))
        self.assertEqual(self.actions, ["play"])

    def test_unknown_keys_and_errors_are_ignored(self):
        self.started()
        self.press(keyboard.KeyCode.from_char("é"))
        self.release(keyboard.KeyCode.from_char("é"))
        self.press(None)
        self.release(None)
        self.assertEqual(self.actions, [])


class _Named:
    """A stand-in for a keyboard.Key member (see test_hotkey_spec.FakeKey for why)."""

    def __init__(self, name):
        self.name = name
        self.value = None


if __name__ == "__main__":
    unittest.main()
