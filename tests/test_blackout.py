"""Tests for blackout.py (real Tk windows, fake Windows calls) and brightness.py (fake brightness
control, real background thread).

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import blackout as blackout_module  # noqa: E402
import brightness  # noqa: E402
import winutil  # noqa: E402
from blackout import Blackout  # noqa: E402
from brightness import Dimmer, WmiBrightness  # noqa: E402

logging.getLogger("blackout").addHandler(logging.NullHandler())
logging.getLogger("brightness").addHandler(logging.NullHandler())
logging.getLogger("brightness").propagate = False

try:
    import tkinter
    _IMPORT_ERROR = None
except Exception as error:  # pragma: no cover
    _IMPORT_ERROR = error


class FakeWin(winutil.NullWin32):
    def __init__(self, monitors):
        super().__init__()
        self._monitors = monitors
        self.overlays, self.excluded, self.alphas = [], [], []

    def monitors(self):
        return list(self._monitors)

    def make_overlay(self, hwnd, alpha=255):
        self.overlays.append(hwnd)
        return True

    def exclude_from_capture(self, hwnd):
        self.excluded.append(hwnd)
        return True

    def set_alpha(self, hwnd, alpha):
        self.alphas.append(alpha)
        return True


TWO = [winutil.Monitor(0, 0, 800, 600, True), winutil.Monitor(800, 0, 1440, 480, False)]


@unittest.skipIf(_IMPORT_ERROR is not None, f"tkinter unavailable: {_IMPORT_ERROR}")
class BlackoutTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tkinter.Tk()
        except tkinter.TclError as error:
            self.skipTest(f"no display: {error}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.win = FakeWin(TWO)
        self.cover = Blackout(self.root, win32=self.win)
        self.addCleanup(self.cover.destroy)
        self.cover.prepare()

    def test_built_hidden_click_through_and_out_of_captures(self):
        self.assertEqual(len(self.cover._covers), 2)
        self.assertEqual(self.cover.placed(), [])
        self.assertEqual(len(self.win.overlays), 2)
        self.assertEqual(len(self.win.excluded), 2)

    def test_covers_every_monitor_with_the_text_on_the_main_one(self):
        self.cover.show("▶ Run 3/10 · 12:04")
        self.assertEqual(sorted(self.cover.placed()), [(0, 0, 800, 600), (800, 0, 640, 480)])
        self.assertEqual(self.cover._label.cget("text"), "▶ Run 3/10 · 12:04")
        self.assertEqual(self.cover._label.master, self.cover._covers[0])
        self.cover.set_text("▶ Run 3/10 · 12:05")
        self.assertEqual(self.cover._label.cget("text"), "▶ Run 3/10 · 12:05")

    def test_fades_in(self):
        self.cover.show("")
        deadline = self.root.tk.call("clock", "milliseconds") + 2000
        while self.win.alphas[-1] != 255 and self.root.tk.call("clock", "milliseconds") < deadline:
            self.root.update()
        self.assertEqual(self.win.alphas[-1], 255)
        self.assertLess(self.win.alphas[0], 255)

    def test_hide_parks_everything_and_stops_the_fade(self):
        self.cover.show("x")
        self.cover.hide()
        self.assertEqual(self.cover.placed(), [])
        self.assertIsNone(self.cover._fade_job)
        self.assertFalse(self.cover.shown)

    def test_a_new_monitor_gets_a_cover_too(self):
        self.win._monitors = TWO + [winutil.Monitor(-1024, 0, 0, 768, False)]
        self.cover.show("")
        self.assertEqual(len(self.cover.placed()), 3)

    def test_broken_never_raises(self):
        self.cover.ok = False
        self.cover.show("x")
        self.assertEqual(self.cover.placed(), [])
        self.cover.destroy()
        self.cover.destroy()


class FakeControl:
    def __init__(self, level=70):
        self.level = level
        self.sets = []
        self.fail_set = False

    def get(self):
        return self.level

    def set(self, level):
        if self.fail_set:
            return False
        self.sets.append(level)
        self.level = level
        return True


class DimmerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.note = Path(folder.name) / "sub" / "brightness_restore.json"
        self.control = FakeControl()

    def make(self, threaded=False):
        return Dimmer(self.control, restore_file=self.note, threaded=threaded)

    def test_dim_and_restore(self):
        d = self.make()
        d.dim()
        self.assertEqual(self.control.level, 0)
        self.assertEqual(json.loads(self.note.read_text()), {"level": 70})   # crash note written first
        d.dim()                                                             # twice: nothing more
        self.assertEqual(self.control.sets, [0])
        d.restore()
        self.assertEqual(self.control.level, 70)
        self.assertFalse(self.note.exists())
        d.restore()
        self.assertEqual(self.control.sets, [0, 70])

    def test_recover_after_a_crash(self):
        self.note.parent.mkdir(parents=True)
        self.note.write_text(json.dumps({"level": 55}))
        self.control.level = 0
        self.make().recover()
        self.assertEqual(self.control.level, 55)
        self.assertFalse(self.note.exists())

    def test_bad_notes_are_thrown_away(self):
        self.note.parent.mkdir(parents=True)
        for text in ("garbage", json.dumps({"level": 0}), json.dumps({"level": True}), json.dumps([1])):
            with self.subTest(text=text):
                self.note.write_text(text)
                self.make().recover()
                self.assertFalse(self.note.exists())
                self.assertEqual(self.control.sets, [])

    def test_no_laptop_screen_means_nothing_happens(self):
        self.control.level = None
        d = self.make()
        d.dim()
        d.restore()
        self.assertEqual(self.control.sets, [])
        self.assertFalse(self.note.exists())

    def test_a_failed_restore_keeps_the_note_for_next_time(self):
        d = self.make()
        d.dim()
        self.control.fail_set = True
        d.restore()
        self.assertTrue(self.note.exists())

    def test_threaded_jobs_run_in_order_and_close_waits(self):
        d = self.make(threaded=True)
        d.dim()
        d.restore()
        d.dim()
        d.close(timeout=5)
        self.assertEqual(self.control.sets, [0, 70, 0, 70])
        self.assertFalse(d.dimmed)


class WmiTests(unittest.TestCase):
    def test_get_and_set_through_powershell(self):
        commands = []

        def runner(command):
            commands.append(command)
            return "  64\r\n" if "WmiMonitorBrightness " in command or command.endswith("CurrentBrightness") else ""

        wmi = WmiBrightness(runner)
        wmi.available = True
        self.assertEqual(wmi.get(), 64)
        self.assertTrue(wmi.set(150))
        self.assertIn("Brightness=[byte]100", commands[-1])

    def test_a_desktop_monitor_turns_it_off_for_good(self):
        wmi = WmiBrightness(mock.Mock(side_effect=OSError("Not supported")))
        wmi.available = True
        self.assertIsNone(wmi.get())
        self.assertFalse(wmi.available)
        self.assertFalse(wmi.set(10))

    def test_off_windows_it_does_nothing(self):
        runner = mock.Mock()
        with mock.patch.object(sys, "platform", "linux"):
            wmi = WmiBrightness(runner)
        self.assertIsNone(wmi.get())
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
