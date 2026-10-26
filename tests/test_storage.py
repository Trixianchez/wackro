"""Tests for storage.py: key (de)serialization, validation, and atomic JSON save/load.

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import copy
import enum
import json
import os
import random
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform.startswith("linux"):
    # Sandboxes / CI have no display. pynput's dummy backend still gives us the real classes.
    os.environ.setdefault("PYNPUT_BACKEND", "dummy")

from pynput import keyboard  # noqa: E402

import storage  # noqa: E402
from storage import RecordingError  # noqa: E402


# ---- Test data --------------------------------------------------------------------------

def sample_recording():
    """One of every event type. Event numbers (1-based) are in the comments."""
    return {
        "version": 1,
        "screen": {"width": 1920, "height": 1080},
        "events": [
            {"t": 0.0, "type": "mouse_move", "x": 100, "y": 200},                               # 1
            {"t": 0.25, "type": "mouse_click", "x": 100, "y": 200, "button": "left", "pressed": True},   # 2
            {"t": 0.3, "type": "mouse_click", "x": 100, "y": 200, "button": "left", "pressed": False},  # 3
            {"t": 0.4, "type": "mouse_scroll", "x": 100, "y": 200, "dx": 0, "dy": -1},          # 4
            {"t": 0.9, "type": "key_press", "key": {"kind": "char", "value": "a"}},              # 5
            {"t": 0.95, "type": "key_release", "key": {"kind": "char", "value": "a"}},           # 6
            {"t": 1.0, "type": "key_press", "key": {"kind": "special", "value": "enter"}},       # 7
            {"t": 1.05, "type": "key_release", "key": {"kind": "special", "value": "enter"}},    # 8
            {"t": 1.1, "type": "key_press", "key": {"kind": "vk", "value": 65}},                 # 9
            {"t": 1.2, "type": "key_press", "key": {"kind": "char", "value": "\u00e9", "vk": 69}},  # 10
        ],
    }


def top_set(**fields):
    def mutate(rec):
        rec.update(fields)
    return mutate


def top_del(name):
    def mutate(rec):
        del rec[name]
    return mutate


def screen_set(**fields):
    def mutate(rec):
        rec["screen"].update(fields)
    return mutate


def screen_del(name):
    def mutate(rec):
        del rec["screen"][name]
    return mutate


def ev_set(index, **fields):
    def mutate(rec):
        rec["events"][index].update(fields)
    return mutate


def ev_del(index, name):
    def mutate(rec):
        del rec["events"][index][name]
    return mutate


# (description, mutation, text the error message must contain). Event indexes are 0-based.
BAD_RECORDINGS = [
    # version
    ("version missing", top_del("version"), "version"),
    ("version zero", top_set(version=0), "version"),
    ("version negative", top_set(version=-1), "version"),
    ("version from the future", top_set(version=3), "newer"),
    ("version as string", top_set(version="1"), "version"),
    ("version as float", top_set(version=1.0), "version"),
    ("version as bool", top_set(version=True), "version"),
    ("version null", top_set(version=None), "version"),
    ("unknown top-level field", top_set(author="me"), "unexpected"),
    # screen
    ("screen missing", top_del("screen"), "screen"),
    ("screen is a list", top_set(screen=[1920, 1080]), "screen"),
    ("screen width zero", screen_set(width=0), "width"),
    ("screen height negative", screen_set(height=-5), "height"),
    ("screen width float", screen_set(width=1920.0), "width"),
    ("screen width bool", screen_set(width=True), "width"),
    ("screen width absurd", screen_set(width=10 ** 6), "width"),
    ("screen height missing", screen_del("height"), "height"),
    ("screen unknown field", screen_set(depth=32), "unexpected"),
    # events list
    ("events missing", top_del("events"), "events"),
    ("events is a dict", top_set(events={}), "events"),
    ("events is a string", top_set(events="nope"), "events"),
    ("events null", top_set(events=None), "events"),
    ("event is a number", lambda r: r["events"].__setitem__(0, 5), "Event #1"),
    ("event is a list", lambda r: r["events"].__setitem__(0, []), "Event #1"),
    ("event is null", lambda r: r["events"].__setitem__(0, None), "Event #1"),
    ("event type missing", ev_del(0, "type"), "type"),
    ("event type is a number", ev_set(0, type=5), "type"),
    ("event type is a list", ev_set(0, type=[]), "type"),
    ("event type unknown", ev_set(0, type="mouse_teleport"), "unknown event type"),
    ("event type wrong case", ev_set(0, type="MOUSE_MOVE"), "unknown event type"),
    # mouse_move fields
    ("move x missing", ev_del(0, "x"), "'x'"),
    ("move y missing", ev_del(0, "y"), "'y'"),
    ("move x float", ev_set(0, x=1.5), "'x'"),
    ("move x integral float", ev_set(0, x=100.0), "'x'"),
    ("move x string", ev_set(0, x="5"), "'x'"),
    ("move x bool", ev_set(0, x=True), "'x'"),
    ("move x null", ev_set(0, x=None), "'x'"),
    ("move x too big", ev_set(0, x=100_001), "'x'"),
    ("move y too small", ev_set(0, y=-100_001), "'y'"),
    ("move unknown field", ev_set(0, z=1), "unexpected"),
    # time
    ("t missing", ev_del(0, "t"), "'t'"),
    ("t string", ev_set(0, t="0"), "'t'"),
    ("t bool", ev_set(0, t=True), "'t'"),
    ("t null", ev_set(0, t=None), "'t'"),
    ("t negative", ev_set(0, t=-0.5), "'t'"),
    ("t NaN", ev_set(0, t=float("nan")), "'t'"),
    ("t infinity", ev_set(0, t=float("inf")), "'t'"),
    ("t minus infinity", ev_set(0, t=float("-inf")), "'t'"),
    ("t absurdly large", ev_set(0, t=10 ** 9), "'t'"),
    ("time goes backwards", ev_set(3, t=0.1), "backwards"),
    # mouse_click
    ("click button unknown", ev_set(1, button="laser"), "button"),
    ("click button is a number", ev_set(1, button=1), "button"),
    ("click button missing", ev_del(1, "button"), "button"),
    ("click button null", ev_set(1, button=None), "button"),
    ("click button is a list", ev_set(1, button=["left"]), "button"),
    ("click pressed is a string", ev_set(1, pressed="yes"), "pressed"),
    ("click pressed is 1", ev_set(1, pressed=1), "pressed"),
    ("click pressed missing", ev_del(1, "pressed"), "pressed"),
    # mouse_scroll
    ("scroll dx missing", ev_del(3, "dx"), "'dx'"),
    ("scroll dy float", ev_set(3, dy=1.5), "'dy'"),
    ("scroll dy too big", ev_set(3, dy=10_001), "'dy'"),
    ("scroll dx bool", ev_set(3, dx=False), "'dx'"),
    # keys inside events
    ("key_press without key", ev_del(4, "key"), "key"),
    ("key_press with string key", ev_set(4, key="a"), "key"),
    ("key_press with unknown key kind", ev_set(4, key={"kind": "scancode", "value": 4}), "key"),
    ("key_press with unknown special", ev_set(6, key={"kind": "special", "value": "banana"}), "key"),
    ("key_release with 2-char key", ev_set(5, key={"kind": "char", "value": "ab"}), "key"),
    ("key_press with a mouse field", ev_set(4, button="left"), "unexpected"),
    # absurd values must not crash the error message itself
    ("huge integer x", ev_set(0, x=10 ** 5000), "'x'"),
    ("huge integer t", ev_set(0, t=10 ** 5000), "'t'"),
    ("huge integer version", top_set(version=10 ** 5000), "version"),
    ("huge string as event type", ev_set(0, type="x" * 10_000), "unknown event type"),
    ("huge integer as key kind", ev_set(4, key={"kind": 10 ** 5000, "value": "a"}), "key"),
]

BAD_KEYS = [
    ("not a dict", "a"),
    ("null", None),
    ("empty dict", {}),
    ("no kind", {"value": "a"}),
    ("unknown kind", {"kind": "scancode", "value": 1}),
    ("kind is a list", {"kind": ["char"], "value": "a"}),
    ("special: unknown name", {"kind": "special", "value": "banana"}),
    ("special: value is a number", {"kind": "special", "value": 5}),
    ("special: value is a list", {"kind": "special", "value": ["enter"]}),
    ("special: no value", {"kind": "special"}),
    ("special: extra field", {"kind": "special", "value": "enter", "vk": 13}),
    ("char: empty", {"kind": "char", "value": ""}),
    ("char: two characters", {"kind": "char", "value": "ab"}),
    ("char: number", {"kind": "char", "value": 97}),
    ("char: NUL", {"kind": "char", "value": "\x00"}),
    ("char: lone surrogate", {"kind": "char", "value": "\ud800"}),
    ("char: vk negative", {"kind": "char", "value": "a", "vk": -1}),
    ("char: vk zero", {"kind": "char", "value": "a", "vk": 0}),
    ("char: vk bool", {"kind": "char", "value": "a", "vk": True}),
    ("char: vk string", {"kind": "char", "value": "a", "vk": "65"}),
    ("char: extra field", {"kind": "char", "value": "a", "shift": True}),
    ("vk: no value", {"kind": "vk"}),
    ("vk: string", {"kind": "vk", "value": "65"}),
    ("vk: float", {"kind": "vk", "value": 65.0}),
    ("vk: bool", {"kind": "vk", "value": True}),
    ("vk: zero", {"kind": "vk", "value": 0}),
    ("vk: negative", {"kind": "vk", "value": -5}),
    ("vk: too big", {"kind": "vk", "value": 0x10000}),
    ("vk: extra field", {"kind": "vk", "value": 65, "char": "a"}),
]


# ---- Keys -------------------------------------------------------------------------------

class KeySerializationTests(unittest.TestCase):
    def test_every_pynput_special_key_round_trips(self):
        for name, key in keyboard.Key.__members__.items():
            with self.subTest(key=name):
                data = storage.key_to_json(key)
                self.assertEqual(data["kind"], "special")
                self.assertIs(storage.key_from_json(data), key)

    def test_every_pynput_special_key_is_known(self):
        # On Windows this is a free check of the hardcoded list against the real Windows key list.
        missing = sorted(set(keyboard.Key.__members__) - storage.SPECIAL_KEY_NAMES)
        self.assertEqual(missing, [], "pynput has special keys that storage.SPECIAL_KEY_NAMES lacks")

    def test_special_key_names_are_stored_by_name(self):
        # pynput's dummy backend makes every Key an alias of one another, so use a stub enum with
        # distinct values to really prove names (including f21 and media_stop) survive the trip.
        class FakeKey(enum.Enum):
            enter = 1
            f9 = 2
            f21 = 3
            media_stop = 4

        fake = types.SimpleNamespace(Key=FakeKey, KeyCode=keyboard.KeyCode)
        with mock.patch.object(storage, "_keyboard", return_value=fake):
            for key in FakeKey:
                with self.subTest(key=key.name):
                    data = storage.key_to_json(key)
                    self.assertEqual(data, {"kind": "special", "value": key.name})
                    self.assertIs(storage.key_from_json(data), key)

    def test_special_key_missing_on_this_platform_is_a_friendly_error(self):
        class FakeKey(enum.Enum):
            enter = 1

        fake = types.SimpleNamespace(Key=FakeKey, KeyCode=keyboard.KeyCode)
        with mock.patch.object(storage, "_keyboard", return_value=fake):
            with self.assertRaises(RecordingError) as caught:
                storage.key_from_json({"kind": "special", "value": "f21"})
        self.assertIn("f21", str(caught.exception))
        self.assertIn("isn't available", str(caught.exception))

    def test_char_keys_round_trip(self):
        for char in ["a", "Z", "5", "-", "[", " ", "\u00e9", "\u65e5", "\u00df", "\U0001F600", "\x01", "\t"]:
            with self.subTest(char=char):
                data = storage.key_to_json(keyboard.KeyCode.from_char(char))
                self.assertEqual(data, {"kind": "char", "value": char})
                back = storage.key_from_json(data)
                self.assertEqual((back.char, back.vk), (char, None))

    def test_char_key_that_also_has_a_vk(self):
        data = storage.key_to_json(keyboard.KeyCode(vk=65, char="a"))
        self.assertEqual(data, {"kind": "char", "value": "a", "vk": 65})
        back = storage.key_from_json(data)
        self.assertEqual((back.char, back.vk), ("a", 65))

    def test_vk_only_key(self):
        data = storage.key_to_json(keyboard.KeyCode.from_vk(0x70))
        self.assertEqual(data, {"kind": "vk", "value": 0x70})
        back = storage.key_from_json(data)
        self.assertEqual((back.char, back.vk), (None, 0x70))

    def test_serialized_keys_are_plain_json(self):
        keys = [keyboard.Key.enter, keyboard.KeyCode.from_char("x"), keyboard.KeyCode.from_vk(65)]
        for key in keys:
            json.dumps(storage.key_to_json(key))

    def test_key_to_json_rejects_things_that_are_not_keys(self):
        for thing in ("a", None, 65, object(), {"kind": "char", "value": "a"}):
            with self.subTest(thing=thing), self.assertRaises(RecordingError):
                storage.key_to_json(thing)

    def test_key_to_json_rejects_keys_that_could_not_be_loaded_again(self):
        unsaveable = [
            ("neither char nor vk", keyboard.KeyCode()),
            ("not one character", keyboard.KeyCode(char="ab")),
            ("NUL", keyboard.KeyCode(char="\x00")),
            ("vk out of range", keyboard.KeyCode(vk=10 ** 9)),
            ("bad vk next to a good char", keyboard.KeyCode(char="a", vk=-3)),
        ]
        for description, key in unsaveable:
            with self.subTest(description), self.assertRaises(RecordingError):
                storage.key_to_json(key)

    def test_object_with_a_broken_repr_gives_a_friendly_error_not_a_crash(self):
        class Evil:
            def __repr__(self):
                raise RuntimeError("boom")

        with self.assertRaises(RecordingError) as caught:
            storage.key_to_json(Evil())
        self.assertIn("unprintable", str(caught.exception))

    def test_key_from_json_rejects_bad_keys(self):
        for description, data in BAD_KEYS:
            with self.subTest(description), self.assertRaises(RecordingError) as caught:
                storage.key_from_json(data)
            self.assertNotIn("\n", str(caught.exception))
            self.assertLess(len(str(caught.exception)), 200)


# ---- Validation -------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def test_sample_recording_is_valid_and_returned_as_is(self):
        rec = sample_recording()
        self.assertIs(storage.validate_recording(rec), rec)

    def test_recording_with_no_events_is_valid(self):
        rec = sample_recording()
        rec["events"] = []
        storage.validate_recording(rec)

    def test_every_mouse_button_is_accepted(self):
        for button in storage.BUTTON_NAMES:
            with self.subTest(button=button):
                rec = sample_recording()
                rec["events"][1]["button"] = button
                storage.validate_recording(rec)

    def test_every_special_key_name_is_accepted(self):
        for name in sorted(storage.SPECIAL_KEY_NAMES):
            with self.subTest(key=name):
                rec = sample_recording()
                rec["events"][6]["key"] = {"kind": "special", "value": name}
                storage.validate_recording(rec)

    def test_equal_timestamps_are_fine(self):
        rec = sample_recording()
        for event in rec["events"]:
            event["t"] = 1.0
        storage.validate_recording(rec)

    def test_integer_timestamps_are_fine(self):
        rec = sample_recording()
        for index, event in enumerate(rec["events"]):
            event["t"] = index
        storage.validate_recording(rec)

    def test_negative_coordinates_are_fine_for_multi_monitor_setups(self):
        rec = sample_recording()
        rec["events"][0].update(x=-1920, y=-300)
        storage.validate_recording(rec)

    def test_limits_are_inclusive(self):
        rec = sample_recording()
        rec["events"][0].update(x=storage.MAX_COORD, y=-storage.MAX_COORD)
        rec["events"][3].update(dx=storage.MAX_SCROLL, dy=-storage.MAX_SCROLL)
        rec["events"][4]["t"] = storage.MAX_RECORDING_SECONDS
        rec["events"] = rec["events"][:5]
        storage.validate_recording(rec)

    def test_exactly_the_maximum_number_of_events_is_allowed_but_one_more_is_not(self):
        event = {"t": 0, "type": "mouse_move", "x": 0, "y": 0}
        rec = {"version": 1, "screen": {"width": 1, "height": 1}, "events": [event] * storage.MAX_EVENTS}
        storage.validate_recording(rec)
        rec["events"] = rec["events"] + [event]
        with self.assertRaises(RecordingError) as caught:
            storage.validate_recording(rec)
        self.assertIn("Too many events", str(caught.exception))

    def test_top_level_must_be_an_object(self):
        for root in ([], "text", None, 5, 1.5, True):
            with self.subTest(root=root), self.assertRaises(RecordingError):
                storage.validate_recording(root)

    def test_bad_recordings_are_rejected_with_a_helpful_message(self):
        for description, mutate, needle in BAD_RECORDINGS:
            with self.subTest(description):
                rec = sample_recording()
                mutate(rec)
                with self.assertRaises(RecordingError) as caught:
                    storage.validate_recording(rec)
                message = str(caught.exception)
                self.assertIn(needle.lower(), message.lower())
                self.assertTrue(message)
                self.assertNotIn("\n", message)
                self.assertLess(len(message), 250, "error message should stay short")

    def test_error_message_names_the_event_and_the_problem(self):
        rec = sample_recording()
        rec["events"][1]["button"] = "laser"
        with self.assertRaises(RecordingError) as caught:
            storage.validate_recording(rec)
        self.assertEqual(str(caught.exception), "Event #2: unknown mouse button 'laser'.")

    def test_validation_does_not_modify_the_recording(self):
        rec = sample_recording()
        before = copy.deepcopy(rec)
        storage.validate_recording(rec)
        self.assertEqual(rec, before)

    def test_make_recording_builds_a_valid_recording(self):
        events = sample_recording()["events"]
        rec = storage.make_recording(events, 2560, 1440)
        self.assertEqual(rec["version"], 1)
        self.assertEqual(rec["screen"], {"width": 2560, "height": 1440})
        self.assertIs(rec["events"], events)

    def test_make_recording_rejects_bad_screen_size(self):
        with self.assertRaises(RecordingError):
            storage.make_recording([], 0, 1080)


# ---- Save / load ------------------------------------------------------------------------

class SaveLoadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "rec.json"

    def write(self, name, content):
        path = self.dir / name
        if isinstance(content, str):
            content = content.encode("utf-8")
        path.write_bytes(content)
        return path

    def assert_load_fails(self, path, needle=None):
        with self.assertRaises(RecordingError) as caught:
            storage.load_recording(path)
        message = str(caught.exception)
        self.assertTrue(message)
        self.assertNotIn("\n", message)
        self.assertLess(len(message), 300)
        if needle:
            self.assertIn(needle.lower(), message.lower())
        return message

    # -- round trips --

    def test_save_then_load_gives_an_identical_recording(self):
        rec = sample_recording()
        storage.save_recording(self.path, rec)
        self.assertEqual(storage.load_recording(self.path), rec)

    def test_round_trip_of_a_big_random_recording_keeps_floats_exact(self):
        rng = random.Random(1234)
        events, t = [], 0.0
        for _ in range(3000):
            t += rng.random() * 0.05
            kind = rng.choice(["move", "click", "scroll", "key"])
            if kind == "move":
                events.append({"t": t, "type": "mouse_move", "x": rng.randint(-2000, 4000), "y": rng.randint(0, 2000)})
            elif kind == "click":
                events.append({"t": t, "type": "mouse_click", "x": rng.randint(0, 1900), "y": rng.randint(0, 1000),
                               "button": rng.choice(storage.BUTTON_NAMES), "pressed": rng.random() < 0.5})
            elif kind == "scroll":
                events.append({"t": t, "type": "mouse_scroll", "x": 5, "y": 6,
                               "dx": rng.randint(-3, 3), "dy": rng.randint(-3, 3)})
            else:
                events.append({"t": t, "type": rng.choice(["key_press", "key_release"]),
                               "key": {"kind": "char", "value": rng.choice("abcxyz019;'/")}})
        rec = storage.make_recording(events, 1920, 1080)
        storage.save_recording(self.path, rec)
        self.assertEqual(storage.load_recording(self.path), rec)

    def test_unicode_keys_survive_a_round_trip_through_the_file(self):
        rec = sample_recording()
        for index, char in enumerate(["\u00e9", "\u65e5", "\U0001F600", "\u00df"]):
            rec["events"].append({"t": 2.0 + index, "type": "key_press", "key": {"kind": "char", "value": char}})
        storage.save_recording(self.path, rec)
        self.assertEqual(storage.load_recording(self.path), rec)

    def test_saving_twice_gives_byte_identical_files(self):
        rec = sample_recording()
        storage.save_recording(self.path, rec)
        first = self.path.read_bytes()
        storage.save_recording(self.path, storage.load_recording(self.path))
        self.assertEqual(self.path.read_bytes(), first)

    # -- file format --

    def test_file_has_one_event_per_line(self):
        rec = sample_recording()
        storage.save_recording(self.path, rec)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 4 + len(rec["events"]) + 2)
        event_lines = lines[4:4 + len(rec["events"])]
        for line, event in zip(event_lines, rec["events"]):
            self.assertEqual(json.loads(line.strip().rstrip(",")), event)

    def test_file_is_plain_json_utf8_with_lf_and_a_final_newline(self):
        storage.save_recording(self.path, sample_recording())
        raw = self.path.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(json.loads(raw.decode("utf-8")), sample_recording())

    def test_file_with_no_events_is_still_valid_json(self):
        rec = storage.make_recording([], 800, 600)
        storage.save_recording(self.path, rec)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["events"], [])
        self.assertEqual(storage.load_recording(self.path), rec)

    # -- saving safely --

    def test_save_overwrites_an_existing_file(self):
        storage.save_recording(self.path, sample_recording())
        smaller = storage.make_recording([], 800, 600)
        storage.save_recording(self.path, smaller)
        self.assertEqual(storage.load_recording(self.path), smaller)

    def test_no_temp_files_are_left_behind_after_a_save(self):
        storage.save_recording(self.path, sample_recording())
        self.assertEqual(os.listdir(self.dir), ["rec.json"])

    def test_saving_an_invalid_recording_keeps_the_original_file(self):
        storage.save_recording(self.path, sample_recording())
        original = self.path.read_bytes()
        bad = sample_recording()
        bad["events"][1]["button"] = "laser"
        with self.assertRaises(RecordingError):
            storage.save_recording(self.path, bad)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(os.listdir(self.dir), ["rec.json"])

    def test_saving_an_invalid_recording_creates_no_file(self):
        with self.assertRaises(RecordingError):
            storage.save_recording(self.path, {"version": 1})
        self.assertEqual(os.listdir(self.dir), [])

    def test_failed_replace_keeps_the_original_and_cleans_up(self):
        storage.save_recording(self.path, sample_recording())
        original = self.path.read_bytes()
        with mock.patch.object(storage.os, "replace", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaises(RecordingError) as caught:
                storage.save_recording(self.path, storage.make_recording([], 800, 600))
        self.assertIn("Couldn't save", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(os.listdir(self.dir), ["rec.json"])

    def test_failed_write_keeps_the_original_and_cleans_up(self):
        storage.save_recording(self.path, sample_recording())
        original = self.path.read_bytes()
        with mock.patch.object(storage.os, "fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(RecordingError) as caught:
                storage.save_recording(self.path, storage.make_recording([], 800, 600))
        self.assertIn("No space left", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(os.listdir(self.dir), ["rec.json"])

    def test_saving_into_a_missing_folder_is_a_friendly_error(self):
        with self.assertRaises(RecordingError) as caught:
            storage.save_recording(self.dir / "nope" / "rec.json", sample_recording())
        self.assertIn("Couldn't save", str(caught.exception))

    def test_saving_over_a_folder_is_a_friendly_error(self):
        folder = self.dir / "iamafolder"
        folder.mkdir()
        with self.assertRaises(RecordingError) as caught:
            storage.save_recording(folder, sample_recording())
        self.assertIn("Couldn't save", str(caught.exception))
        self.assertEqual(sorted(os.listdir(self.dir)), ["iamafolder"])

    def test_save_accepts_str_and_path_and_unicode_folders(self):
        folder = self.dir / "r\u00e9sum\u00e9 \u65e5\u672c"
        folder.mkdir()
        for target in (str(folder / "a.json"), folder / "b.json"):
            storage.save_recording(target, sample_recording())
            self.assertEqual(storage.load_recording(target), sample_recording())

    def test_save_does_not_modify_the_recording(self):
        rec = sample_recording()
        before = copy.deepcopy(rec)
        storage.save_recording(self.path, rec)
        self.assertEqual(rec, before)

    # -- loading things that aren't recordings --

    def test_missing_file(self):
        self.assert_load_fails(self.dir / "ghost.json", "find")

    def test_folder_instead_of_file(self):
        folder = self.dir / "iamafolder"
        folder.mkdir()
        self.assert_load_fails(folder)

    def test_empty_and_whitespace_only_files(self):
        self.assert_load_fails(self.write("empty.json", ""), "empty")
        self.assert_load_fails(self.write("blank.json", " \n\t\n"), "empty")

    def test_binary_garbage(self):
        self.assert_load_fails(self.write("junk.json", bytes(range(256)) * 4), "text file")

    def test_not_json(self):
        self.assert_load_fails(self.write("a.json", "hello there"), "valid JSON")

    def test_truncated_json(self):
        text = self.path_text()
        self.assert_load_fails(self.write("cut.json", text[: len(text) // 2]), "valid JSON")

    def test_json_error_says_where(self):
        message = self.assert_load_fails(self.write("typo.json", '{\n  "version": 1,\n  oops\n}'), "valid JSON")
        self.assertIn("line 3", message)

    def test_wrong_top_level_type(self):
        for index, text in enumerate(["[]", '"hello"', "null", "42", "true"]):
            with self.subTest(text):
                self.assert_load_fails(self.write(f"root{index}.json", text), "JSON object")

    def test_nan_and_infinity_are_rejected(self):
        template = '{"version":1,"screen":{"width":1,"height":1},"events":[{"t":%s,"type":"mouse_move","x":0,"y":0}]}'
        # NaN / Infinity are not valid JSON; Python's parser would quietly accept them, so we refuse.
        for index, token in enumerate(["NaN", "Infinity", "-Infinity"]):
            with self.subTest(token):
                self.assert_load_fails(self.write(f"nan{index}.json", template % token), "valid number")
        # 1e999 is valid JSON but overflows to infinity, which validation must refuse.
        self.assert_load_fails(self.write("overflow.json", template % "1e999"), "'t'")

    def test_absurdly_deep_nesting_is_rejected_not_a_crash(self):
        head = '{"version":1,"screen":{"width":1,"height":1},"events":[{"t":0,"type":"key_press","key":'
        deep = {
            "deep list": "[" * 100_000,
            "deep objects": '{"a":' * 100_000,
            "deep list inside an event": head + "[" * 100_000 + "]" * 100_000 + "}]}",
        }
        for name, text in deep.items():
            with self.subTest(name):
                self.assert_load_fails(self.write("deep.json", text), "deeply")

    def test_number_with_thousands_of_digits_is_rejected_not_a_crash(self):
        text = ('{"version":1,"screen":{"width":1,"height":1},'
                '"events":[{"t":0,"type":"mouse_move","x":1' + "0" * 10_000 + ',"y":0}]}')
        self.assert_load_fails(self.write("digits.json", text))

    def test_oversized_file(self):
        big = self.write("big.json", self.path_text())
        with mock.patch.object(storage, "MAX_FILE_BYTES", 200):
            self.assert_load_fails(big, "too big")

    def test_file_exactly_at_the_size_limit_still_loads(self):
        target = self.write("edge.json", self.path_text())
        with mock.patch.object(storage, "MAX_FILE_BYTES", target.stat().st_size):
            self.assertEqual(storage.load_recording(target), sample_recording())

    def test_too_many_events_in_a_file(self):
        target = self.write("many.json", self.path_text())
        with mock.patch.object(storage, "MAX_EVENTS", 3):
            self.assert_load_fails(target, "too many events")

    def test_invalid_event_in_a_file_is_reported_with_its_number(self):
        rec = sample_recording()
        rec["events"][2]["pressed"] = "maybe"
        message = self.assert_load_fails(self.write("bad.json", json.dumps(rec)), "pressed")
        self.assertTrue(message.startswith("Event #3:"))

    def test_recording_from_a_newer_app_version(self):
        rec = sample_recording()
        rec["version"] = 3
        rec["events"][0]["brand_new_field"] = True
        self.assert_load_fails(self.write("future.json", json.dumps(rec)), "newer")

    # -- version 2: the window context --

    def context_recording(self, version=2, context=None):
        context = context if context is not None else {"exe": "notepad.exe", "cls": "Notepad"}
        return {"version": version, "screen": {"width": 100, "height": 100}, "events": [
            {"t": 0.0, "type": "mouse_move", "x": 1, "y": 1},
            {"t": 0.1, "type": "mouse_click", "x": 1, "y": 1, "button": "left", "pressed": True, "w": context},
            {"t": 0.2, "type": "mouse_click", "x": 1, "y": 1, "button": "left", "pressed": False},
            {"t": 0.3, "type": "key_press", "key": {"kind": "char", "value": "a"}, "w": context},
            {"t": 0.4, "type": "key_release", "key": {"kind": "char", "value": "a"}},
        ]}

    def test_version_2_with_window_context_loads_and_round_trips(self):
        rec = self.context_recording()
        path = self.write("v2.json", json.dumps(rec))
        self.assertEqual(storage.load_recording(path), rec)
        storage.save_recording(path, storage.load_recording(path))
        self.assertEqual(storage.load_recording(path), rec)

    def test_window_context_needs_version_2(self):
        self.assert_load_fails(self.write("v1w.json", json.dumps(self.context_recording(version=1))), "unexpected field")

    def test_bad_window_context(self):
        for bad in ({"exe": "a.exe"}, {"exe": "", "cls": "X"}, {"exe": "a.exe", "cls": 5},
                    {"exe": "a.exe", "cls": "X", "title": "secret"}, "notepad", {"exe": "x" * 300, "cls": "X"}):
            with self.subTest(bad=bad):
                self.assert_load_fails(self.write("badw.json", json.dumps(self.context_recording(context=bad))), "'w")

    def test_context_is_not_allowed_on_moves_or_releases(self):
        rec = self.context_recording()
        rec["events"][0]["w"] = {"exe": "a.exe", "cls": "X"}
        self.assert_load_fails(self.write("movew.json", json.dumps(rec)), "unexpected field")

    def test_make_recording_picks_the_lowest_version_that_fits(self):
        plain = storage.make_recording([{"t": 0.0, "type": "mouse_move", "x": 1, "y": 1}], 10, 10)
        self.assertEqual(plain["version"], 1)        # still opens in older versions
        with_context = storage.make_recording(self.context_recording()["events"], 10, 10)
        self.assertEqual(with_context["version"], 2)

    # -- loading things that are fine but unusual --

    def test_file_with_a_utf8_bom_loads(self):
        # Notepad and friends like to add one when someone hand-edits a recording.
        target = self.write("bom.json", b"\xef\xbb\xbf" + self.path_text().encode("utf-8"))
        self.assertEqual(storage.load_recording(target), sample_recording())

    def test_file_with_windows_line_endings_loads(self):
        target = self.write("crlf.json", self.path_text().replace("\n", "\r\n"))
        self.assertEqual(storage.load_recording(target), sample_recording())

    def test_hand_formatted_json_loads(self):
        target = self.write("pretty.json", json.dumps(sample_recording(), indent=2))
        self.assertEqual(storage.load_recording(target), sample_recording())

    def test_load_accepts_str_and_path(self):
        storage.save_recording(self.path, sample_recording())
        self.assertEqual(storage.load_recording(str(self.path)), storage.load_recording(self.path))

    def test_recording_error_is_a_normal_exception_with_a_readable_str(self):
        self.assertTrue(issubclass(RecordingError, Exception))
        self.assertEqual(str(RecordingError("Nope.")), "Nope.")

    # -- helper --

    def path_text(self):
        return json.dumps(sample_recording(), indent=2)


if __name__ == "__main__":
    unittest.main()
