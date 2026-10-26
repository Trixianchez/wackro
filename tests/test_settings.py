"""Tests for settings.py (repeat, speed, recording options, hotkeys, close action).

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import settings  # noqa: E402

logging.getLogger("settings").addHandler(logging.NullHandler())

import hotkey_spec  # noqa: E402

DEFAULTS = {
    "repeat": 1, "infinite": False, "delay": 0.0, "speed": 1.0,
    "record_keyboard": True, "record_clicks": True, "record_moves": True, "record_delay": True,
    "hotkeys": hotkey_spec.DEFAULT_BINDINGS,
    "close_action": "ask",
    "indicator": "both",
    "indicator_playback": True,
    "fs_keys": True, "fs_clicks": True, "fs_move": True, "fs_move_sensitivity": "medium",
    "fs_corner": True, "fs_window": True, "fs_preflight": True, "fs_crash": True,
    "fs_locked": True, "fs_screen": True, "fs_sleep": True,
    "time_limit_on": False, "time_limit": 60.0, "keep_awake": True, "blackout": False,
    "blackout_after": 5.0, "blackout_dim": True, "blackout_text": True, "blackout_hotkey": None,
}
REPEAT_KEYS = ("repeat", "infinite", "delay")


def repeat_part(data):
    return {k: data[k] for k in REPEAT_KEYS}


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "settings.json"


class SanitizeTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(settings.default_settings(), DEFAULTS)

    def test_not_a_dict_gives_defaults(self):
        for data in (None, [], "text", 5, [1, 2]):
            with self.subTest(data=data):
                self.assertEqual(settings.sanitize(data), DEFAULTS)

    def test_good_values_are_kept(self):
        data = {"repeat": 5, "infinite": True, "delay": 1.5}
        self.assertEqual(settings.sanitize(data), {**DEFAULTS, **data})

    def test_each_bad_field_falls_back_on_its_own(self):
        result = settings.sanitize({"repeat": 0, "infinite": True, "delay": -3})
        self.assertEqual(repeat_part(result), {"repeat": 1, "infinite": True, "delay": 0.0})
        result = settings.sanitize({"repeat": 7, "infinite": "yes", "delay": 2})
        self.assertEqual(repeat_part(result), {"repeat": 7, "infinite": False, "delay": 2.0})

    def test_wrong_types_and_out_of_range(self):
        for repeat in (True, 1.5, "3", None, -1, 0, config.MAX_REPEAT + 1):
            with self.subTest(repeat=repeat):
                self.assertEqual(settings.sanitize({"repeat": repeat})["repeat"], 1)
        for delay in (True, "1", None, -0.5, float("nan"), float("inf"), config.MAX_REPEAT_DELAY + 1):
            with self.subTest(delay=delay):
                self.assertEqual(settings.sanitize({"delay": delay})["delay"], 0.0)

    def test_the_limits_are_valid(self):
        self.assertEqual(settings.sanitize({"repeat": config.MAX_REPEAT})["repeat"], config.MAX_REPEAT)
        self.assertEqual(settings.sanitize({"delay": config.MAX_REPEAT_DELAY})["delay"], config.MAX_REPEAT_DELAY)
        self.assertEqual(settings.sanitize({"delay": 0})["delay"], 0.0)

    def test_unknown_keys_are_dropped(self):
        self.assertEqual(settings.sanitize({"repeat": 2, "x": 1}), {**DEFAULTS, "repeat": 2})

    def test_the_input_is_not_modified(self):
        data = {"repeat": 3}
        settings.sanitize(data)
        self.assertEqual(data, {"repeat": 3})


class NewFieldTests(unittest.TestCase):
    def test_speed(self):
        self.assertEqual(settings.sanitize({"speed": 2})["speed"], 2.0)
        self.assertEqual(settings.sanitize({"speed": config.MIN_SPEED})["speed"], config.MIN_SPEED)
        self.assertEqual(settings.sanitize({"speed": config.MAX_SPEED})["speed"], config.MAX_SPEED)
        for bad in (0, 0.05, 11, -1, True, "2", None, float("nan"), float("inf")):
            with self.subTest(speed=bad):
                self.assertEqual(settings.sanitize({"speed": bad})["speed"], 1.0)

    def test_record_options(self):
        result = settings.sanitize({"record_keyboard": False, "record_clicks": "no", "record_delay": 0})
        self.assertFalse(result["record_keyboard"])
        self.assertTrue(result["record_clicks"])
        self.assertTrue(result["record_delay"])
        self.assertTrue(result["record_moves"])

    def test_close_action(self):
        for action in ("ask", "tray", "exit"):
            self.assertEqual(settings.sanitize({"close_action": action})["close_action"], action)
        for bad in ("minimize", None, 1, True):
            self.assertEqual(settings.sanitize({"close_action": bad})["close_action"], "ask")

    def test_indicator(self):
        for style in ("both", "border", "pill", "off"):
            self.assertEqual(settings.sanitize({"indicator": style})["indicator"], style)
        for bad in ("rainbow", None, 3):
            self.assertEqual(settings.sanitize({"indicator": bad})["indicator"], "both")
        self.assertFalse(settings.sanitize({"indicator_playback": False})["indicator_playback"])
        self.assertTrue(settings.sanitize({"indicator_playback": "no"})["indicator_playback"])

    def test_safety(self):
        data = {"fs_keys": False, "fs_move_sensitivity": "high", "fs_crash": False}
        self.assertEqual(settings.sanitize(data), {**DEFAULTS, **data})
        bad = settings.sanitize({"fs_keys": "no", "fs_move_sensitivity": "ludicrous", "fs_window": 0})
        self.assertEqual(bad, DEFAULTS)

    def test_blackout_and_power(self):
        data = {"blackout": True, "blackout_after": 12.5, "blackout_dim": False, "blackout_text": False,
                "keep_awake": False, "time_limit_on": True, "time_limit": 90,
                "blackout_hotkey": {"vk": 0x7B, "mods": ["ctrl"]}}
        got = settings.sanitize(data)
        self.assertEqual(got, {**DEFAULTS, **data, "time_limit": 90.0})
        bad = settings.sanitize({"blackout": "yes", "blackout_after": 0, "time_limit": 99999,
                                 "keep_awake": 1, "blackout_hotkey": {"vk": "F12"}})
        self.assertEqual(bad, DEFAULTS)

    def test_blackout_hotkey_cannot_steal_a_main_hotkey(self):
        got = settings.sanitize({"blackout_hotkey": {"vk": 0x79, "mods": []}})   # F10 = Panic stop
        self.assertIsNone(got["blackout_hotkey"])

    def test_hotkeys_are_sanitized(self):
        result = settings.sanitize({"hotkeys": {"record": {"vk": 0x41, "mods": ["shift", "ctrl"]}, "play": "junk"}})
        self.assertEqual(result["hotkeys"]["record"], {"mods": ["ctrl", "shift"], "vk": 0x41})
        self.assertEqual(result["hotkeys"]["play"], hotkey_spec.DEFAULT_BINDINGS["play"])
        self.assertEqual(result["hotkeys"]["stop"], hotkey_spec.DEFAULT_BINDINGS["stop"])

    def test_default_hotkeys_are_a_copy(self):
        first = settings.default_settings()
        first["hotkeys"]["record"]["mods"].append("alt")
        self.assertEqual(settings.default_settings()["hotkeys"]["record"]["mods"], [])
        self.assertEqual(hotkey_spec.DEFAULT_BINDINGS["record"]["mods"], [])

    def test_old_settings_file_from_1_2_still_loads(self):
        self.assertEqual(settings.sanitize({"repeat": 3, "infinite": True, "delay": 1.0}),
                         {**DEFAULTS, "repeat": 3, "infinite": True, "delay": 1.0})


class LoadTests(TempDirCase):
    def test_missing_file_gives_defaults(self):
        self.assertEqual(settings.load_settings(self.path), DEFAULTS)

    def test_reads_a_good_file(self):
        self.path.write_text(json.dumps({"repeat": 4, "infinite": False, "delay": 2.5}), encoding="utf-8")
        self.assertEqual(settings.load_settings(self.path), {**DEFAULTS, "repeat": 4, "delay": 2.5})

    def test_broken_files_give_defaults(self):
        for content in (b"", b"{", b"not json", b"[1, 2]", b"null", b"\xff\xfe\x00bad", b'{"repeat": NaN}'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.assertEqual(settings.load_settings(self.path), DEFAULTS)

    def test_a_partly_bad_file_keeps_the_good_fields(self):
        self.path.write_text('{"repeat": "lots", "infinite": true, "delay": 3}', encoding="utf-8")
        self.assertEqual(repeat_part(settings.load_settings(self.path)), {"repeat": 1, "infinite": True, "delay": 3.0})

    def test_a_huge_file_is_ignored(self):
        self.path.write_text('{"repeat": 9, "pad": "' + "x" * 70_000 + '"}', encoding="utf-8")
        self.assertEqual(settings.load_settings(self.path), DEFAULTS)

    def test_a_directory_in_the_way_gives_defaults(self):
        self.path.mkdir()
        self.assertEqual(settings.load_settings(self.path), DEFAULTS)

    def test_uses_the_default_path_from_config(self):
        self.path.write_text('{"repeat": 8}', encoding="utf-8")
        with mock.patch.object(settings, "SETTINGS_PATH", self.path):
            self.assertEqual(settings.load_settings()["repeat"], 8)


class SaveTests(TempDirCase):
    def test_round_trip(self):
        data = {**DEFAULTS, "repeat": 12, "infinite": True, "delay": 0.25, "speed": 2.5,
                "record_moves": False, "close_action": "tray",
                "hotkeys": {"record": {"mods": ["ctrl", "shift"], "vk": 0x52},
                            "play": {"mods": [], "vk": 0x7A}, "stop": {"mods": [], "vk": 0x7B}}}
        self.assertTrue(settings.save_settings(data, self.path))
        self.assertEqual(settings.load_settings(self.path), data)

    def test_the_file_is_readable_json(self):
        settings.save_settings({"repeat": 3, "infinite": False, "delay": 1.0}, self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(text), {**DEFAULTS, "repeat": 3, "delay": 1.0})
        self.assertTrue(text.endswith("\n"))

    def test_bad_values_are_sanitized_not_written(self):
        settings.save_settings({"repeat": -5, "infinite": "x", "delay": float("nan"), "junk": 1}, self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), DEFAULTS)

    def test_creates_missing_folders(self):
        nested = self.dir / "a" / "b" / "settings.json"
        self.assertTrue(settings.save_settings({"repeat": 2}, nested))
        self.assertEqual(settings.load_settings(nested)["repeat"], 2)

    def test_overwrites_and_leaves_no_temp_files(self):
        settings.save_settings({"repeat": 2}, self.path)
        settings.save_settings({"repeat": 3}, self.path)
        self.assertEqual(settings.load_settings(self.path)["repeat"], 3)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["settings.json"])

    def test_failure_returns_false_and_keeps_the_old_file(self):
        settings.save_settings({"repeat": 2}, self.path)
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(settings.save_settings({"repeat": 9}, self.path))
        self.assertEqual(settings.load_settings(self.path)["repeat"], 2)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["settings.json"])   # temp file cleaned up

    def test_unwritable_location_returns_false(self):
        blocker = self.dir / "blocker"
        blocker.write_text("a file, not a folder", encoding="utf-8")
        self.assertFalse(settings.save_settings({"repeat": 2}, blocker / "settings.json"))

    def test_uses_the_default_path_from_config(self):
        with mock.patch.object(settings, "SETTINGS_PATH", self.path):
            self.assertTrue(settings.save_settings({"repeat": 6}))
        self.assertEqual(settings.load_settings(self.path)["repeat"], 6)


if __name__ == "__main__":
    unittest.main()
