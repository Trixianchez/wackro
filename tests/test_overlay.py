"""Tests for overlay.py: real Tk windows (skipped without a display), fake Windows calls.

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import winutil  # noqa: E402
from overlay import COLORS, THICKNESS, Overlay, pill_text  # noqa: E402

logging.getLogger("overlay").addHandler(logging.NullHandler())

try:
    import tkinter
    _IMPORT_ERROR = None
except Exception as error:  # pragma: no cover
    _IMPORT_ERROR = error


class FakeWin(winutil.NullWin32):
    """Fake monitors and no click-through styles, but REAL window moves on Windows (moving a
    test window is harmless, and it's exactly what has to work there)."""

    def __init__(self, monitors):
        super().__init__()
        self._monitors = monitors
        self.overlays = []
        self.excluded = []
        self.moves = []
        self._real = winutil.Win32() if sys.platform.startswith("win") else None

    def move(self, hwnd, x, y, width, height):
        self.moves.append((hwnd, x, y, width, height))
        return self._real.move(hwnd, x, y, width, height) if self._real else False

    def monitors(self):
        return list(self._monitors)

    def make_overlay(self, hwnd, alpha=255):
        self.overlays.append((hwnd, alpha))
        return True

    def exclude_from_capture(self, hwnd):
        self.excluded.append(hwnd)
        return True


ONE = [winutil.Monitor(0, 0, 800, 600, True)]
TWO = [winutil.Monitor(0, 0, 800, 600, True), winutil.Monitor(800, 0, 1440, 480, False)]


class PillTextTests(unittest.TestCase):
    def test_texts(self):
        self.assertEqual(pill_text("recording", 12.7), "● REC 0:12")
        self.assertEqual(pill_text("recording", 75), "● REC 1:15")
        self.assertEqual(pill_text("recording", 3, pending=True), "● REC")
        self.assertEqual(pill_text("playing", 7, run=2, runs=5), "▶ Run 2/5 · 0:07")
        self.assertEqual(pill_text("playing", 7, run=3, runs=0), "▶ Run 3/∞ · 0:07")
        self.assertEqual(pill_text("playing", 7, run=0, runs=4), "▶ Run 1/4 · 0:07")
        self.assertEqual(pill_text("playing", 61), "▶ 1:01")
        self.assertEqual(pill_text("playing", 0, pending=True), "▶ PLAY")
        self.assertEqual(pill_text("recording", -3), "● REC 0:00")
        self.assertEqual(pill_text("playing", 72, run=2, runs=0, step="3/5 Export"), "▶ Round 2/∞ · 3/5 Export · 1:12")
        self.assertEqual(pill_text("playing", 5, step="1/2 Login"), "▶ 1/2 Login · 0:05")


@unittest.skipIf(_IMPORT_ERROR is not None, f"tkinter unavailable: {_IMPORT_ERROR}")
class OverlayCase(unittest.TestCase):
    monitors = ONE

    def setUp(self):
        try:
            self.root = tkinter.Tk()
        except tkinter.TclError as error:
            self.skipTest(f"no display: {error}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.win = FakeWin(self.monitors)
        self.overlay = Overlay(self.root, win32=self.win)
        self.addCleanup(self.overlay.destroy)
        self.overlay.prepare()

    def strips(self):
        return [p for p in self.overlay.placed() if p[2] == THICKNESS or p[3] == THICKNESS]


class OverlayTests(OverlayCase):
    def test_prepare_builds_everything_hidden(self):
        self.assertEqual(len(self.overlay._strips), 4)
        self.assertIsNotNone(self.overlay._pill)
        self.assertEqual(self.overlay.placed(), [])
        # every piece made click-through and hidden from captures, the pill a bit see-through
        self.assertEqual(len(self.win.overlays), 5)
        self.assertEqual(len(self.win.excluded), 5)
        self.assertLess(self.win.overlays[-1][1], 255)

    def test_border_hugs_the_screen_edges(self):
        self.overlay.show("recording", "● REC 0:00", style="border")
        placed = sorted(self.overlay.placed())
        self.assertEqual(placed, sorted([
            (0, 0, 800, THICKNESS), (0, 600 - THICKNESS, 800, THICKNESS),
            (0, 0, THICKNESS, 600), (800 - THICKNESS, 0, THICKNESS, 600),
        ]))
        for strip in self.overlay._strips:
            self.assertEqual(strip.cget("background"), COLORS["recording"])

    def test_both_adds_the_pill_top_center(self):
        self.overlay.show("recording", "● REC 0:00", style="both")
        pill = [p for p in self.overlay.placed() if p not in self.strips()]
        self.assertEqual(len(pill), 1)
        x, y, w, h = pill[0]
        self.assertAlmostEqual(x + w / 2, 400, delta=1)
        self.assertGreater(y, THICKNESS)
        self.assertEqual(self.overlay._pill_label.cget("text"), "● REC 0:00")

    def test_pill_only(self):
        self.overlay.show("recording", "● REC 0:00", style="pill")
        self.assertEqual(len(self.overlay.placed()), 1)

    def test_off_shows_nothing(self):
        self.overlay.show("recording", "x", style="off")
        self.assertEqual(self.overlay.placed(), [])

    def test_playing_is_green(self):
        self.overlay.show("playing", "▶ 0:01")
        self.assertEqual(self.overlay._strips[0].cget("background"), COLORS["playing"])
        self.assertEqual(self.overlay._pill_label.cget("bg"), COLORS["playing"])

    def test_text_updates(self):
        self.overlay.show("recording", "● REC 0:00")
        self.overlay.set_text("● REC 0:05")
        self.assertEqual(self.overlay._pill_label.cget("text"), "● REC 0:05")

    def test_text_before_show_is_ignored(self):
        self.overlay.set_text("● REC 0:05")
        self.assertEqual(self.overlay.placed(), [])

    def test_hide(self):
        self.overlay.show("recording", "x")
        self.overlay.hide()
        self.assertEqual(self.overlay.placed(), [])
        self.assertIsNone(self.overlay.state)

    def test_unknown_state_and_style(self):
        with self.assertRaises(ValueError):
            self.overlay.show("dancing")
        self.overlay.show("recording", "x", style="sparkles")   # falls back to both
        self.assertEqual(len(self.overlay.placed()), 5)

    def test_showing_never_creates_windows(self):
        # New windows can steal the focus on Windows; after prepare() only moves happen.
        before = len(self.win.overlays)
        self.overlay.show("recording", "x")
        self.overlay.hide()
        self.overlay.show("playing", "y")
        self.assertEqual(len(self.win.overlays), before)

    def test_positions_survive_a_full_event_loop_pass(self):
        # Regression: a resize left pending while parked used to land after the move on screen
        # and drag the pill back off-screen. Run the whole event loop, not just idle tasks.
        self.root.update()
        for state in ("recording", "playing", "recording"):
            with self.subTest(state=state):
                self.overlay.show(state, "● REC 0:00")
                for _ in range(3):
                    self.root.update()
                self.assertEqual(len(self.overlay.placed()), 5)
                self.overlay.hide()
                self.root.update()
                self.assertEqual(self.overlay.placed(), [])

    def test_every_piece_is_also_moved_through_windows(self):
        # On Windows, Tk alone leaves these windows at the parking spot (seen on a real PC).
        self.win.moves.clear()
        self.overlay.show("recording", "● REC 0:00")
        moved = {(x, y, w, h) for _, x, y, w, h in self.win.moves}
        self.assertIn((0, 0, 800, THICKNESS), moved)
        self.assertIn((800 - THICKNESS, 0, THICKNESS, 600), moved)
        self.assertTrue(all(hwnd for hwnd, *_ in self.win.moves))
        self.win.moves.clear()
        self.overlay.hide()
        self.assertTrue(all((x, y) == (-10000, -10000) for _, x, y, _, _ in self.win.moves))

    def test_destroy_twice(self):
        self.overlay.destroy()
        self.overlay.destroy()

    def test_a_broken_overlay_never_raises(self):
        self.overlay.ok = False
        self.overlay.show("recording", "x")
        self.assertEqual(self.overlay.placed(), [])


class ParkingTests(unittest.TestCase):
    def test_never_parks_at_the_windows_minimized_position(self):
        import overlay
        self.assertNotEqual(overlay._PARK, -32000)
        self.assertGreater(overlay._PARK, -32000 + 1000)


class TwoMonitorTests(OverlayCase):
    monitors = TWO

    def test_every_monitor_gets_a_border_and_the_pill_goes_on_the_main_one(self):
        self.overlay.show("recording", "● REC 0:00")
        strips = self.strips()
        self.assertEqual(len(strips), 8)
        self.assertIn((800, 0, 640, THICKNESS), strips)                 # second monitor's top
        self.assertIn((1440 - THICKNESS, 0, THICKNESS, 480), strips)    # second monitor's right
        pill = [p for p in self.overlay.placed() if p not in strips][0]
        self.assertLess(pill[0] + pill[2], 800)                          # on the main monitor

    def test_unplugging_a_monitor_parks_its_border(self):
        self.overlay.show("recording", "x")
        self.win._monitors = ONE
        self.overlay.show("recording", "x")
        self.assertEqual(len(self.strips()), 4)


if __name__ == "__main__":
    unittest.main()
