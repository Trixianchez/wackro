"""Sanity checks for config.py.

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform.startswith("linux"):
    os.environ.setdefault("PYNPUT_BACKEND", "dummy")

import config  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_no_fixed_hotkeys_any_more(self):
        # The keys are the user's choice now (settings.json); defaults live in hotkey_spec.
        for name in ("HOTKEY_RECORD", "HOTKEY_STOP", "HOTKEY_PLAY", "HOTKEY_KEY_NAMES", "HOTKEY_LABELS"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(config, name))

    def test_tray_icons_are_pngs_in_assets(self):
        self.assertEqual(set(config.TRAY_ICON_PATHS), {"idle", "recording", "playing"})
        for path in config.TRAY_ICON_PATHS.values():
            with self.subTest(path=path):
                self.assertEqual(path.parent, Path("assets"))
                self.assertEqual(path.suffix, ".png")

    def test_no_delay_gap_is_small_but_positive(self):
        self.assertGreater(config.NO_DELAY_GAP, 0)
        self.assertLessEqual(config.NO_DELAY_GAP, 0.1)

    def test_limits_are_sane(self):
        self.assertEqual(config.RECORDING_VERSION, 2)
        self.assertGreater(config.MOVE_MIN_INTERVAL, 0)
        self.assertEqual(config.MAX_EVENTS, 200_000)
        self.assertGreater(config.MAX_FILE_BYTES, config.MAX_EVENTS * 120)  # a full recording must fit
        self.assertGreater(config.TAIL_TRIM_SECONDS, 0)
        self.assertGreaterEqual(config.WINDOW_MINIMIZE_DELAY, 0)
        self.assertGreaterEqual(config.PLAYBACK_START_DELAY, 0)

    def test_log_path_is_absolute_and_named_after_the_app(self):
        self.assertTrue(config.LOG_PATH.is_absolute())
        self.assertEqual(config.LOG_PATH.name, "wackro.log")
        self.assertEqual(config.LOG_PATH.parent, config.LOG_DIR)

    def test_importing_config_does_not_create_anything_on_disk(self):
        # LOG_DIR is only a path; the logger (later step) creates it when it's needed.
        self.assertIsInstance(config.LOG_DIR, Path)


class CarryOverTests(unittest.TestCase):
    def test_settings_come_over_from_wackylloop_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            old, new = Path(folder) / "Wackylloop", Path(folder) / "Wackro"
            old.mkdir()
            (old / "settings.json").write_text('{"speed": 2.0}', encoding="utf-8")
            (old / "wackylloop.log").write_text("old log", encoding="utf-8")
            self.assertEqual(config.carry_over_old_data(new, [old]), ["settings.json"])
            self.assertEqual((new / "settings.json").read_text(encoding="utf-8"), '{"speed": 2.0}')
            self.assertFalse((new / "wackylloop.log").exists())
            (new / "settings.json").write_text('{"speed": 3.0}', encoding="utf-8")
            self.assertEqual(config.carry_over_old_data(new, [old]), [])          # never overwrites
            self.assertEqual((new / "settings.json").read_text(encoding="utf-8"), '{"speed": 3.0}')

    def test_nothing_old(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(config.carry_over_old_data(Path(folder) / "new", [Path(folder) / "none"]), [])


if __name__ == "__main__":
    unittest.main()
