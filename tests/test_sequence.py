"""Tests for sequence.py: the file format (and upgrading old files), checking, how rounds are
planned and built, and the plain-English description / preview.

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import json
import random
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sequence as seq  # noqa: E402
import storage  # noqa: E402
from storage import RecordingError  # noqa: E402


def recording(letter="a", length=1.0):
    return {"version": 1, "screen": {"width": 100, "height": 100}, "events": [
        {"t": 0.0, "type": "mouse_move", "x": 1, "y": 1},
        {"t": length / 2, "type": "key_press", "key": {"kind": "char", "value": letter}},
        {"t": length, "type": "key_release", "key": {"kind": "char", "value": letter}},
    ]}


def sample(*names, order="in_order"):
    s = seq.new_sequence("Test")
    s["order"] = order
    for name in names:
        s["steps"].append(seq.make_step(name, recording(name[0].lower())))
    return s


def letters(events):
    return "".join(e["key"]["value"] for e in events if e["type"] == "key_press")


def names(s, order):
    return [s["steps"][i]["name"] for i in order]


def old_v1(*names_, order="in_order", locked=(), picks=0):
    """A version-1 file as 1.7.0 wrote it."""
    steps = []
    for i, name in enumerate(names_):
        steps.append({"name": name, "enabled": True, "locked": i in locked, "repeat": 1,
                      "wait": {"min": 0.0, "max": 0.0}, "recording": recording(name[0].lower())})
    return {"kind": "wackro-sequence", "version": 1, "name": "Old", "order": order, "picks": picks,
            "rounds": 2, "round_wait": {"min": 0.0, "max": 0.0}, "steps": steps}


class WaitTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(seq.parse_wait(""), {"min": 0.0, "max": 0.0})
        self.assertEqual(seq.parse_wait("2"), {"min": 2.0, "max": 2.0})
        self.assertEqual(seq.parse_wait("1-5"), {"min": 1.0, "max": 5.0})
        self.assertEqual(seq.parse_wait("1–5"), {"min": 1.0, "max": 5.0})
        self.assertEqual(seq.parse_wait("5 to 1"), {"min": 1.0, "max": 5.0})
        self.assertEqual(seq.parse_wait("1,5 s"), {"min": 1.5, "max": 1.5})
        self.assertEqual(seq.parse_wait("1s-5s"), {"min": 1.0, "max": 5.0})
        self.assertEqual(seq.parse_wait("3 seconds"), {"min": 3.0, "max": 3.0})
        for bad in ("abc", "1-", "-2", "1-2-3", "nan", "999999"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                seq.parse_wait(bad)

    def test_format_words_and_draw(self):
        self.assertEqual(seq.format_wait({"min": 0.0, "max": 0.0}), "")      # empty box = no pause
        self.assertEqual(seq.format_wait({"min": 2.0, "max": 2.0}), "2")
        self.assertEqual(seq.format_wait({"min": 1.0, "max": 5.5}), "1-5.5")
        self.assertEqual(seq.wait_words({"min": 1.0, "max": 5.0}), "1–5 s")
        self.assertEqual(seq.wait_words({"min": 0.0, "max": 0.0}), "")
        rng = random.Random(1)
        draws = [seq.draw_wait({"min": 1.0, "max": 5.0}, rng) for _ in range(200)]
        self.assertTrue(all(1.0 <= d <= 5.0 for d in draws))
        self.assertGreater(max(draws) - min(draws), 2)
        self.assertEqual(seq.draw_wait({"min": 3.0, "max": 3.0}, rng), 3.0)


class PlanTests(unittest.TestCase):
    def pinned(self, order, pins, **extra):
        s = sample(*pins.keys(), order=order)
        for step in s["steps"]:
            step["pin"] = pins[step["name"]]
        s.update(extra)
        return s

    def test_in_order_puts_pins_first_and_last_and_skips_off_steps(self):
        s = self.pinned("in_order", {"Logout": "last", "A": "none", "Login": "first", "B": "none"})
        s["steps"][3]["enabled"] = False
        self.assertEqual(names(s, seq.plan_order(s, random.Random(1))), ["Login", "A", "Logout"])

    def test_shuffle_moves_only_the_anywhere_steps(self):
        s = self.pinned("shuffle", {"Login": "first", "A": "none", "B": "none", "C": "none", "D": "none",
                                    "Logout": "last"})
        rng = random.Random(3)
        seen = set()
        for _ in range(40):
            order = names(s, seq.plan_order(s, rng))
            self.assertEqual((order[0], order[-1]), ("Login", "Logout"))
            self.assertEqual(sorted(order[1:-1]), ["A", "B", "C", "D"])
            seen.add(tuple(order))
        self.assertGreater(len(seen), 5)

    def test_random_picks_always_play_the_pins_plus_n_random_ones(self):
        s = self.pinned("random_picks", {"Login": "first", "A": "none", "B": "none", "Logout": "last"},
                        picks=5)
        rng = random.Random(7)
        repeated = False
        for _ in range(30):
            order = names(s, seq.plan_order(s, rng))
            self.assertEqual(len(order), 7)
            self.assertEqual((order[0], order[-1]), ("Login", "Logout"))
            self.assertTrue(all(n in ("A", "B") for n in order[1:-1]))
            repeated |= len(set(order[1:-1])) < 5
        self.assertTrue(repeated)

    def test_no_trap_a_pinned_step_always_plays_whatever_the_picks(self):
        # 1.7.0's "lock" could make a step never play (locked to spot 3 with 1 pick). Not any more.
        for picks in (1, 2, 50):
            s = self.pinned("random_picks", {"A": "none", "B": "none", "C": "last"}, picks=picks)
            for _ in range(20):
                order = names(s, seq.plan_order(s, random.Random()))
                self.assertEqual(order[-1], "C")
                self.assertEqual(len(order), picks + 1)

    def test_everything_pinned_with_random_picks_plays_just_the_pins(self):
        s = self.pinned("random_picks", {"A": "first", "B": "last"}, picks=3)
        self.assertEqual(names(s, seq.plan_order(s, random.Random(1))), ["A", "B"])

    def test_nothing_enabled(self):
        s = sample("A")
        s["steps"][0]["enabled"] = False
        self.assertEqual(seq.plan_order(s, random.Random(1)), [])


class DescribeTests(unittest.TestCase):
    def test_sentences(self):
        s = sample("Login", "Fill form", "Export", "Logout", order="random_picks")
        s["steps"][0]["pin"] = "first"
        s["steps"][3]["pin"] = "last"
        s.update(picks=2, rounds=3, round_wait={"min": 2.0, "max": 5.0})
        self.assertEqual(seq.describe(s),
                         "Each round: Login first, then 2 random steps from Fill form and Export "
                         "(the same one can come up more than once), then Logout last. "
                         "Repeats 3 rounds, 2–5 s between rounds.")
        s.update(order="shuffle", same_shuffle=False, rounds=0, round_wait={"min": 0.0, "max": 0.0})
        self.assertEqual(seq.describe(s),
                         "Each round: Login first, then Fill form and Export shuffled (a new order every "
                         "round), then Logout last. Repeats until you stop it.")
        s.update(same_shuffle=True)
        self.assertIn("the same order every round", seq.describe(s))
        s.update(order="in_order", rounds=1)
        self.assertEqual(seq.describe(s), "Each round: Login first, then Fill form and Export in list order, "
                                          "then Logout last. Plays 1 round.")

    def test_many_steps_are_counted_not_listed(self):
        s = sample("A", "B", "C", "D", "E", order="shuffle")
        self.assertIn("5 other steps shuffled", seq.describe(s))

    def test_nothing_on(self):
        s = sample("A")
        s["steps"][0]["enabled"] = False
        self.assertEqual(seq.describe(s), "Every step is switched off, so there's nothing to play.")

    def test_warnings(self):
        s = sample("A", "B", order="random_picks")
        self.assertEqual(seq.warnings(s), [])
        for step in s["steps"]:
            step["pin"] = "first"
        self.assertIn("nothing to pick at random", seq.warnings(s)[0])
        s = sample("Login", "A", order="shuffle")
        s["steps"][0]["pin"] = "first"
        self.assertIn("shuffling changes nothing", seq.warnings(s)[0])
        s["steps"][0]["enabled"] = s["steps"][1]["enabled"] = False
        self.assertIn("nothing to play", seq.warnings(s)[0])

    def test_preview(self):
        s = sample("Login", "Export", "Logout")
        s["steps"][1]["repeat"] = 3
        self.assertEqual(seq.preview(s, random.Random(1)), "Login → Export ×3 → Logout")
        s["steps"][0]["enabled"] = s["steps"][1]["enabled"] = s["steps"][2]["enabled"] = False
        self.assertEqual(seq.preview(s), "(nothing to play)")

    def test_summary(self):
        s = sample("A", "B", "C", order="shuffle")
        s["steps"][2]["enabled"] = False
        s["rounds"] = 0
        self.assertEqual(seq.summary(s), "2 steps (of 3) · Shuffle · ∞ rounds")


class BuildTests(unittest.TestCase):
    def test_steps_repeats_and_waits_line_up(self):
        s = sample("A", "B")
        s["steps"][0]["repeat"] = 2
        s["steps"][0]["wait"] = {"min": 3.0, "max": 3.0}
        events, segments = seq.build_round(s, [0, 1], random.Random(1))
        self.assertEqual(letters(events), "aab")
        times = [e["t"] for e in events]
        self.assertEqual(times, sorted(times))
        storage.validate_recording({"version": 2, "screen": {"width": 1, "height": 1}, "events": events})
        (a_start, a_end, a_index), (b_start, b_end, b_index) = segments
        self.assertEqual((a_index, b_index), (0, 1))
        self.assertAlmostEqual(a_end, 2.0 + seq.REPEAT_GAP)
        self.assertAlmostEqual(b_start, a_end + 3.0)

    def test_lead_shifts_the_round(self):
        events, segments = seq.build_round(sample("A"), [0], random.Random(1), lead=4.0)
        self.assertEqual(events[0]["t"], 4.0)

    def test_window_notes_survive(self):
        s = sample("A")
        rec = s["steps"][0]["recording"]
        rec["version"] = 2
        rec["events"][1]["w"] = {"exe": "bir.exe", "cls": "X"}
        events, _ = seq.build_round(s, [0], random.Random(1))
        self.assertEqual(events[1]["w"], {"exe": "bir.exe", "cls": "X"})
        self.assertEqual(seq.programs_used(s)[1]["w"]["exe"], "bir.exe")

    def test_too_long_or_too_big(self):
        s = sample("A")
        s["steps"][0]["wait"] = {"min": 86400.0, "max": 86400.0}
        with self.assertRaises(RecordingError):
            seq.build_round(s, [0, 0], random.Random(1))
        s = sample("A")
        s["steps"][0]["repeat"] = 1000
        with unittest.mock.patch.object(seq, "MAX_EVENTS", 500), self.assertRaises(RecordingError):
            seq.build_round(s, [0], random.Random(1))


class RunnerTests(unittest.TestCase):
    def test_same_shuffle_keeps_one_order_new_shuffle_changes(self):
        s = sample("Login", "A", "B", "C", "D", "E", order="shuffle")
        s["steps"][0]["pin"] = "first"
        s["same_shuffle"] = True
        runner = seq.Runner(s, random.Random(11))
        first = letters(runner.round(1))
        self.assertTrue(first.startswith("l"))
        self.assertTrue(all(letters(runner.round(n)) == first for n in range(2, 6)))
        s["same_shuffle"] = False
        runner = seq.Runner(s, random.Random(11))
        rounds = {letters(runner.round(n)) for n in range(1, 12)}
        self.assertGreater(len(rounds), 3)
        self.assertTrue(all(r.startswith("l") for r in rounds))

    def test_round_wait_comes_before_later_rounds(self):
        s = sample("A")
        s["round_wait"] = {"min": 2.0, "max": 2.0}
        runner = seq.Runner(s, random.Random(1))
        self.assertEqual(runner.round(1)[0]["t"], 0.0)
        self.assertEqual(runner.round(2)[0]["t"], 2.0)

    def test_step_at(self):
        runner = seq.Runner(sample("Login", "Export"), random.Random(1))
        runner.round(1)
        self.assertEqual(runner.step_at(1, 0.2), (1, 2, "Login"))
        self.assertEqual(runner.step_at(1, 1.5), (2, 2, "Export"))
        self.assertIsNone(runner.step_at(9, 1.0))

    def test_all_off_is_a_clear_error(self):
        s = sample("A")
        s["steps"][0]["enabled"] = False
        with self.assertRaises(RecordingError) as caught:
            seq.Runner(s).round(1)
        self.assertIn("switched off", str(caught.exception))


class UpgradeTests(unittest.TestCase):
    """Sequences saved by 1.7.0 (version 1) keep working."""

    def test_locks_become_pins_top_half_first_bottom_half_last(self):
        s = seq.upgrade(old_v1("Login", "A", "B", "C", "Logout", order="shuffle_round", locked=(0, 4)))
        self.assertEqual(s["version"], 2)
        self.assertEqual([st["pin"] for st in s["steps"]], ["first", "none", "none", "none", "last"])
        self.assertEqual((s["order"], s["same_shuffle"]), ("shuffle", False))
        self.assertNotIn("locked", s["steps"][0])

    def test_orders(self):
        self.assertEqual(seq.upgrade(old_v1("A", order="in_order"))["order"], "in_order")
        once = seq.upgrade(old_v1("A", "B", order="shuffle_once"))
        self.assertEqual((once["order"], once["same_shuffle"]), ("shuffle", True))
        picks = seq.upgrade(old_v1("A", "B", "C", order="random_picks", picks=0, locked=(0,)))
        self.assertEqual((picks["order"], picks["picks"]), ("random_picks", 2))    # 0 = one per free step
        picks = seq.upgrade(old_v1("A", "B", order="random_picks", picks=7))
        self.assertEqual(picks["picks"], 7)

    def test_a_middle_lock_in_an_odd_list(self):
        s = seq.upgrade(old_v1("A", "B", "C", locked=(1,)))           # index 1 of 3: top half
        self.assertEqual(s["steps"][1]["pin"], "first")

    def test_old_files_open_and_save_as_version_2(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.wseq"
            path.write_text(json.dumps(old_v1("Login", "A", locked=(0,))), encoding="utf-8")
            kind, data = seq.load_any(path)
            self.assertEqual(kind, "sequence")
            self.assertEqual(data["steps"][0]["pin"], "first")
            seq.save_sequence(path, data)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 2)
            self.assertEqual(seq.load_sequence(path), data)

    def test_broken_old_files_are_still_refused(self):
        bad = old_v1("A")
        bad["steps"][0]["locked"] = "yes"
        with self.assertRaises(RecordingError):
            seq.upgrade(bad)
        bad = old_v1("A", order="sideways")
        with self.assertRaises(RecordingError):
            seq.upgrade(bad)
        bad = old_v1("A")
        bad["steps"][0]["pin"] = "first"                    # a v2 field in a v1 file
        with self.assertRaises(RecordingError):
            seq.upgrade(bad)


class FileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def test_round_trip_with_copies_inside(self):
        s = sample("Login", "Export", order="random_picks")
        s["steps"][0].update(pin="first", repeat=3, wait={"min": 1.0, "max": 5.0})
        s["picks"] = 4
        path = self.dir / "daily.wseq"
        seq.save_sequence(path, s)
        self.assertEqual(seq.load_sequence(path), s)
        self.assertEqual(seq.load_any(path), ("sequence", s))

    def test_load_any_also_opens_recordings(self):
        path = self.dir / "rec.json"
        storage.save_recording(path, recording())
        self.assertEqual(seq.load_any(path), ("recording", recording()))

    def test_make_step_copies_the_recording(self):
        rec = recording()
        step = seq.make_step("  Log   in ", rec)
        rec["events"].clear()
        self.assertEqual(len(step["recording"]["events"]), 3)
        self.assertEqual((step["name"], step["pin"]), ("Log in", "none"))
        self.assertEqual(seq.name_from_path("C:/x/daily_export.json"), "daily export")

    def test_bad_sequences_are_refused_clearly(self):
        def broken(change):
            s = sample("A")
            change(s)
            return s

        cases = [
            (lambda s: s.update(kind="nope"), "isn't a Wackro sequence"),
            (lambda s: s.update(version=3), "newer version"),
            (lambda s: s.update(extra=1), "Unexpected field"),
            (lambda s: s.pop("rounds"), "missing 'rounds'"),
            (lambda s: s.update(order="shuffle_round"), "unknown 'order'"),
            (lambda s: s.update(rounds=-1), "'rounds'"),
            (lambda s: s.update(picks=0), "'picks'"),
            (lambda s: s.update(picks=True), "'picks'"),
            (lambda s: s.update(same_shuffle="no"), "'same_shuffle'"),
            (lambda s: s.update(round_wait={"min": 5, "max": 1}), "min is more than max"),
            (lambda s: s["steps"][0].update(repeat=0), "'repeat'"),
            (lambda s: s["steps"][0].update(name=""), "the name"),
            (lambda s: s["steps"][0].update(pin="middle"), "'pin'"),
            (lambda s: s["steps"][0].update(locked=True), "Unexpected field"),
            (lambda s: s["steps"][0].update(wait={"min": float("inf"), "max": 1}), "'wait'"),
            (lambda s: s["steps"][0]["recording"]["events"].append({"t": 0}), "Step 1 (A)"),
            (lambda s: s.update(steps=[s["steps"][0]] * (seq.MAX_STEPS + 1)), "up to"),
        ]
        for change, words in cases:
            with self.subTest(words=words):
                with self.assertRaises(RecordingError) as caught:
                    seq.upgrade(broken(change))
                self.assertIn(words, str(caught.exception))

    def test_broken_files(self):
        path = self.dir / "bad.wseq"
        path.write_text("{nope", encoding="utf-8")
        with self.assertRaises(RecordingError):
            seq.load_sequence(path)
        path.write_text(json.dumps({"kind": "wackro-sequence"}), encoding="utf-8")
        with self.assertRaises(RecordingError):
            seq.load_any(path)


class RealPlayerTests(unittest.TestCase):
    """The real Player on its real thread playing a real Runner (fake mouse / keyboard)."""

    def rig(self):
        import os
        import threading
        import types
        if sys.platform.startswith("linux"):
            os.environ.setdefault("PYNPUT_BACKEND", "dummy")
        from player import Player

        typed = []
        mouse = types.SimpleNamespace(position=(0, 0), press=lambda b: None, release=lambda b: None,
                                      scroll=lambda dx, dy: None)
        board = types.SimpleNamespace(press=lambda k: typed.append(getattr(k, "char", None)),
                                      release=lambda k: None)
        done = threading.Event()
        player = Player(on_finished=lambda reason: done.set(), mouse_controller=mouse, keyboard_controller=board)
        return player, typed, done

    def fast(self, s):
        for step in s["steps"]:
            for event in step["recording"]["events"]:
                event["t"] = round(event["t"] / 20, 4)       # 1 s steps -> 0.05 s, so the test is quick
        return s

    def test_shuffled_rounds_really_differ_and_all_play(self):
        s = self.fast(sample("A", "B", "C", "D", order="shuffle"))
        s["rounds"] = 6
        runner = seq.Runner(s, random.Random(4))
        player, typed, done = self.rig()
        player.start(runner.round(1), repeat=6, next_round=runner.round)
        self.assertTrue(done.wait(15))
        self.assertEqual(player.finish_reason, "done")
        rounds = ["".join(typed[i:i + 4]) for i in range(0, 24, 4)]
        self.assertEqual(len(typed), 24)
        self.assertTrue(all(sorted(r) == list("abcd") for r in rounds))
        self.assertGreater(len(set(rounds)), 1)

    def test_a_popup_in_step_3_stops_the_whole_sequence_there(self):
        import failsafe
        from winutil import NullWin32

        class Windows(NullWin32):
            front = "app"

            def foreground(self):
                return 1

            def context_of(self, hwnd):
                return {"exe": "bir.exe", "cls": "#32770" if Windows.front == "popup" else "Main"}

            def foreground_context(self):
                return self.context_of(1)

            def dialog_text(self, hwnd):
                return "VAT rate value should be 10 or 12 only"

        s = self.fast(sample("Login", "Fill", "Export", "Logout"))
        for step in s["steps"]:
            step["recording"]["version"] = 2
            step["recording"]["events"][1]["w"] = {"exe": "bir.exe", "cls": "Main"}
        runner = seq.Runner(s, random.Random(1))
        player, typed, done = self.rig()
        original = player._keyboard_controller.press

        def press(key):
            original(key)
            if len(typed) == 2:
                Windows.front = "popup"                     # appears after step 2, before step 3 types
        player._keyboard_controller.press = press
        player.start(runner.round(1), guard=failsafe.WindowGuard(Windows(), {}))
        self.assertTrue(done.wait(15))
        self.assertEqual(typed, ["l", "f"])
        self.assertEqual(runner.step_at(player.current_run, player.run_position)[2], "Export")
        self.assertIn("VAT rate value", player.stop_text)

if __name__ == "__main__":
    unittest.main()
