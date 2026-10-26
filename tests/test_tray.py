"""Tests for tray.py with a fake pystray (the real tray is checked by hand on Windows).

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import logging
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import config  # noqa: E402
import tray as tray_module  # noqa: E402
from tray import Tray  # noqa: E402

logging.getLogger("tray").addHandler(logging.NullHandler())


class FakeIcon:
    instances = []
    fail_run = None
    never_ready = False

    def __init__(self, name, icon, title, menu):
        self.name, self.icon, self.title, self.menu = name, icon, title, menu
        self.visible = False
        self.stopped = threading.Event()
        self.menu_updates = 0
        FakeIcon.instances.append(self)

    def run(self, setup):
        if FakeIcon.fail_run is not None:
            raise FakeIcon.fail_run
        if not FakeIcon.never_ready:
            setup(self)
        self.stopped.wait(5)

    def stop(self):
        self.stopped.set()

    def update_menu(self):
        self.menu_updates += 1

    def notify(self, message, title=None):
        if getattr(self, "notify_fails", False):
            raise OSError("no balloons here")
        self.notes = getattr(self, "notes", []) + [(message, title)]


class FakeMenuItem:
    def __init__(self, text, action, default=False):
        self.text, self.action, self.default = text, action, default

    def label(self):
        return self.text(self) if callable(self.text) else self.text


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


def fake_pystray():
    return types.SimpleNamespace(Icon=FakeIcon, Menu=FakeMenu, MenuItem=FakeMenuItem)


class TrayCase(unittest.TestCase):
    def setUp(self):
        FakeIcon.instances = []
        FakeIcon.fail_run = None
        FakeIcon.never_ready = False
        self.calls = []

    def make(self, **kwargs):
        options = dict(
            on_show=lambda: self.calls.append("show"),
            on_record=lambda: self.calls.append("record"),
            on_play=lambda: self.calls.append("play"),
            on_stop=lambda: self.calls.append("stop"),
            on_exit=lambda: self.calls.append("exit"),
            pystray_module=fake_pystray(),
        )
        options.update(kwargs)
        tray = Tray(**options)
        self.addCleanup(tray.stop)
        return tray

    @property
    def icon(self):
        return FakeIcon.instances[-1]

    def items(self):
        return [i for i in self.icon.menu.items if i is not FakeMenu.SEPARATOR]

    def item(self, prefix):
        return next(i for i in self.items() if i.label().startswith(prefix))


class LifecycleTests(TrayCase):
    def test_start_shows_the_icon(self):
        tray = self.make()
        self.assertTrue(tray.start())
        self.assertTrue(tray.available)
        self.assertTrue(self.icon.visible)
        self.assertEqual(self.icon.name, config.APP_NAME)
        self.assertEqual(self.icon.title, config.APP_NAME)

    def test_runs_on_a_daemon_thread(self):
        tray = self.make()
        tray.start()
        self.assertTrue(tray._thread.daemon)

    def test_start_twice_is_a_no_op(self):
        tray = self.make()
        tray.start()
        self.assertTrue(tray.start())
        self.assertEqual(len(FakeIcon.instances), 1)

    def test_stop(self):
        tray = self.make()
        tray.start()
        icon = self.icon
        tray.stop()
        self.assertFalse(tray.available)
        self.assertTrue(icon.stopped.is_set())
        self.assertFalse(icon.visible)
        tray.stop()   # again: fine

    def test_stop_before_start(self):
        self.make().stop()

    def test_no_pystray_means_no_tray(self):
        broken = types.SimpleNamespace(Icon=mock.Mock(side_effect=ImportError("no display")),
                                       Menu=FakeMenu, MenuItem=FakeMenuItem)
        tray = self.make(pystray_module=broken)
        self.assertFalse(tray.start())
        self.assertFalse(tray.available)

    def test_real_import_failure_means_no_tray(self):
        tray = self.make(pystray_module=None)
        with mock.patch.dict(sys.modules, {"pystray": None}):   # makes "import pystray" fail
            self.assertFalse(tray.start())

    def test_a_crashing_loop_means_no_tray(self):
        FakeIcon.fail_run = OSError("Shell_NotifyIcon failed")
        tray = self.make()
        self.assertFalse(tray.start())
        self.assertFalse(tray.available)

    def test_an_icon_that_never_comes_up_means_no_tray(self):
        FakeIcon.never_ready = True
        with mock.patch.object(tray_module, "_READY_TIMEOUT", 0.05):
            tray = self.make()
            self.assertFalse(tray.start())
        self.assertTrue(self.icon.stopped.is_set())


class StateTests(TrayCase):
    def test_states_change_icon_and_title(self):
        tray = self.make()
        tray.start()
        images = tray._images
        tray.set_state("recording")
        self.assertIs(self.icon.icon, images["recording"])
        self.assertIn("recording", self.icon.title)
        tray.set_state("playing")
        self.assertIs(self.icon.icon, images["playing"])
        tray.set_state("idle")
        self.assertEqual(self.icon.title, config.APP_NAME)

    def test_three_different_images(self):
        tray = self.make()
        tray.start()
        data = {state: tray._images[state].tobytes() for state in tray_module.STATES}
        self.assertEqual(len(set(data.values())), 3)

    def test_state_before_start_is_remembered(self):
        tray = self.make()
        tray.set_state("playing")
        tray.start()
        self.assertIs(self.icon.icon, tray._images["playing"])

    def test_unknown_state(self):
        with self.assertRaises(ValueError):
            self.make().set_state("dancing")

    def test_missing_png_falls_back_to_a_dot(self):
        def loader(state):
            if state == "playing":
                raise FileNotFoundError(state)
            return Image.new("RGBA", (32, 32), (1, 2, 3, 255))
        tray = self.make(image_loader=loader)
        tray.start()
        self.assertEqual(tray._images["playing"].size, (32, 32))
        self.assertEqual(tray._images["playing"].getpixel((16, 16)), (46, 184, 92, 255))

    def test_the_real_pngs_load(self):
        images = tray_module.load_images()
        for state in tray_module.STATES:
            with self.subTest(state=state):
                self.assertEqual(images[state].size, (32, 32))
                self.assertEqual(images[state].mode, "RGBA")


class NotifyTests(TrayCase):
    def test_notify_shows_a_notification(self):
        tray = self.make()
        tray.notify("before start")            # no icon yet: nothing, no error
        self.assertTrue(tray.start())
        tray.notify("Playback stopped: you moved the mouse")
        icon = FakeIcon.instances[-1]
        self.assertEqual(icon.notes, [("Playback stopped: you moved the mouse", config.APP_NAME)])
        tray.notify("x" * 600)
        self.assertEqual(len(icon.notes[-1][0]), 250)

    def test_notify_failures_are_swallowed(self):
        tray = self.make()
        tray.start()
        FakeIcon.instances[-1].notify_fails = True
        tray.notify("hi")

    def test_backends_without_notifications_are_skipped(self):
        tray = self.make()
        tray.start()
        icon = FakeIcon.instances[-1]
        icon.HAS_NOTIFICATION = False
        tray.notify("hi")
        self.assertFalse(hasattr(icon, "notes"))


class MenuTests(TrayCase):
    def test_menu_items(self):
        tray = self.make()
        tray.start()
        labels = [i.label() for i in self.items()]
        self.assertEqual(labels, [f"Show {config.APP_NAME}", "Record / Stop", "Play / Stop", "Stop", "Exit"])
        self.assertTrue(self.items()[0].default)   # clicking the icon shows the window

    def test_hotkey_labels_appear(self):
        tray = self.make()
        tray.start()
        tray.set_labels({"record": "Ctrl+`", "play": "NumLock", "stop": "F10"})
        self.assertEqual(self.item("Record").label(), "Record / Stop\tCtrl+`")
        self.assertEqual(self.item("Play").label(), "Play / Stop\tNumLock")
        self.assertGreaterEqual(self.icon.menu_updates, 1)

    def test_each_item_fires_its_action(self):
        tray = self.make()
        tray.start()
        for item in self.items():
            item.action(self.icon, item)
        self.assertEqual(self.calls, ["show", "record", "play", "stop", "exit"])

    def test_actions_go_through_dispatch(self):
        hops = []
        tray = self.make(dispatch=hops.append)
        tray.start()
        self.item("Exit").action(self.icon, None)
        self.assertEqual(self.calls, [])
        hops[0]()
        self.assertEqual(self.calls, ["exit"])

    def test_a_failing_action_or_dispatch_is_contained(self):
        def boom():
            raise RuntimeError("gui gone")
        tray = self.make(on_show=boom)
        tray.start()
        self.item("Show").action(self.icon, None)
        tray2 = self.make(dispatch=mock.Mock(side_effect=RuntimeError("no loop")))
        tray2.fire("exit")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
