"""Tests for ui_dialogs.py: the real dark dialog boxes, driven without a person.

Skipped when tkinter / customtkinter or a display isn't available (use `xvfb-run -a` on Linux).

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("PYNPUT_BACKEND", "dummy")

import config  # noqa: E402
import hotkey_spec  # noqa: E402

try:
    import tkinter
    import customtkinter as ctk
    import ui_dialogs as ui
    _IMPORT_ERROR = None
except Exception as error:  # pragma: no cover
    _IMPORT_ERROR = error


@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI libraries unavailable: {_IMPORT_ERROR}")
class ParserTests(unittest.TestCase):
    def test_parse_speed(self):
        for text, expected in (("2", 2.0), ("2x", 2.0), (" 0.5X ", 0.5), ("1,5", 1.5), ("10", 10.0), ("0.1", 0.1)):
            with self.subTest(text=text):
                self.assertEqual(ui.parse_speed(text), expected)
        for text in ("", "fast", "0", "11", "-2", "nan", "inf", "2xx"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                ui.parse_speed(text)

    def test_format_speed(self):
        self.assertEqual(ui.format_speed(1.0), "1x")
        self.assertEqual(ui.format_speed(0.5), "0.5x")


@unittest.skipIf(_IMPORT_ERROR is not None, f"GUI libraries unavailable: {_IMPORT_ERROR}")
def cancel_pending_timers(root):
    """Cancel every after() job of this Tk window before it's destroyed. Tcl keeps ONE timer queue
    per thread, so CustomTkinter's own timers (on Windows: re-showing a box after colouring its
    title bar) would otherwise fire during a later test and print "application has been
    destroyed" errors."""
    try:
        for job in root.tk.splitlist(root.tk.call("after", "info")):
            try:
                root.after_cancel(job)
            except Exception:
                pass
    except Exception:
        pass


class DialogCase(unittest.TestCase):
    def setUp(self):
        # Never the real Windows calls in tests: on Windows they would pull the focus to these
        # test boxes (and tap Alt) while build.ps1 runs.
        import winutil
        patcher = mock.patch.object(winutil, "get_win32", lambda screen_size=None: winutil.NullWin32(screen_size))
        patcher.start()
        self.addCleanup(patcher.stop)
        try:
            self.root = ctk.CTk()
        except tkinter.TclError as error:
            self.skipTest(f"no display: {error}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self):
        self.root.tk.eval("proc bgerror {args} {}")
        cancel_pending_timers(self.root)
        self.root.destroy()

    def keep(self, dialog):
        self.addCleanup(lambda: dialog.winfo_exists() and dialog.destroy())
        return dialog

    def closed(self, dialog):
        try:
            return not dialog.winfo_exists()
        except tkinter.TclError:
            return True


class RunTests(DialogCase):
    def test_run_is_modal_and_returns_the_result(self):
        dialog = self.keep(ui.SpeedDialog(self.root, 1.0))
        dialog.after(100, lambda: (dialog.pick(5.0), dialog.ok()))
        self.assertEqual(dialog.run(), 5.0)

    def test_run_takes_the_focus_back_after_it_appears(self):
        # Windows: CTkToplevel hands the focus to the main window ~10 ms after appearing.
        dialog = self.keep(ui.SpeedDialog(self.root, 1.0))
        calls = []
        original = dialog.focus_force
        dialog.focus_force = lambda: (calls.append(1), original())
        dialog.after(400, dialog.cancel)
        dialog.run()
        self.assertGreaterEqual(len(calls), 3)   # once at once, then twice more after a delay

    def test_refocus_after_close_is_harmless(self):
        dialog = self.keep(ui.SpeedDialog(self.root, 1.0))
        dialog.cancel()
        dialog._refocus()

    def test_escape_and_return_keys(self):
        dialog = self.keep(ui.SpeedDialog(self.root, 2.0))
        dialog._on_return()
        self.assertEqual(dialog.result, 2.0)
        dialog = self.keep(ui.SpeedDialog(self.root, 2.0))
        dialog._on_escape()
        self.assertIsNone(dialog.result)
        self.assertTrue(self.closed(dialog))

    def test_window_close_button_cancels(self):
        dialog = self.keep(ui.RepeatDialog(self.root, 3, False, 0))
        dialog.protocol("WM_DELETE_WINDOW")   # registered
        dialog.cancel()
        self.assertIsNone(dialog.result)

    def test_dark_colours(self):
        dialog = self.keep(ui.SpeedDialog(self.root, 1.0))
        self.assertEqual(dialog.cget("fg_color"), ui.BG)


class SpeedTests(DialogCase):
    def test_shows_the_current_speed_and_presets(self):
        dialog = self.keep(ui.SpeedDialog(self.root, 1.5))
        self.assertEqual(dialog.speed_entry.get(), "1.5x")
        self.assertEqual([b.cget("text") for b in dialog.preset_buttons], ["0.5x", "1x", "2x", "5x"])

    def test_preset_then_ok(self):
        dialog = self.keep(ui.SpeedDialog(self.root, 1.0))
        dialog.preset_buttons[2].invoke()
        dialog.ok()
        self.assertEqual(dialog.result, 2.0)
        self.assertTrue(self.closed(dialog))

    def test_bad_value_keeps_the_box_open_with_a_message(self):
        dialog = self.keep(ui.SpeedDialog(self.root, 1.0))
        ui.set_entry(dialog.speed_entry, "warp 9")
        dialog.ok()
        self.assertFalse(self.closed(dialog))
        self.assertIn("Speed must be", dialog.error_label.cget("text"))
        self.assertIsNone(dialog.result)


class RepeatTests(DialogCase):
    def test_values_and_ok(self):
        dialog = self.keep(ui.RepeatDialog(self.root, 3, False, 0.5))
        self.assertEqual(dialog.repeat_entry.get(), "3")
        self.assertEqual(dialog.delay_entry.get(), "0.5")
        ui.set_entry(dialog.repeat_entry, "1,000")
        dialog.ok()
        self.assertEqual(dialog.result, {"repeat": 1000, "infinite": False, "delay": 0.5})

    def test_infinite_greys_out_the_count(self):
        dialog = self.keep(ui.RepeatDialog(self.root, 3, False, 0))
        dialog.infinite_check.toggle()
        self.assertEqual(dialog.repeat_entry.cget("state"), "disabled")
        dialog.infinite_check.toggle()
        self.assertEqual(dialog.repeat_entry.cget("state"), "normal")

    def test_infinite_with_nonsense_in_the_greyed_box(self):
        dialog = self.keep(ui.RepeatDialog(self.root, 3, True, 2))
        ui.set_entry(dialog.repeat_entry, "lots")
        dialog.ok()
        self.assertEqual(dialog.result, {"repeat": 1, "infinite": True, "delay": 2.0})

    def test_bad_repeat_and_bad_delay(self):
        dialog = self.keep(ui.RepeatDialog(self.root, 3, False, 0))
        ui.set_entry(dialog.repeat_entry, "0")
        dialog.ok()
        self.assertIn("Repeat must be", dialog.error_label.cget("text"))
        ui.set_entry(dialog.repeat_entry, "2")
        ui.set_entry(dialog.delay_entry, "-1")
        dialog.ok()
        self.assertIn("Delay must be", dialog.error_label.cget("text"))
        self.assertFalse(self.closed(dialog))


class RecordOptionsTests(DialogCase):
    def test_checkboxes_reflect_and_return_the_options(self):
        options = {"keyboard": True, "clicks": False, "moves": True, "delay": False}
        dialog = self.keep(ui.RecordOptionsDialog(self.root, options))
        self.assertEqual({k: v.get() for k, v in dialog.vars.items()}, options)
        dialog.checks["clicks"].toggle()
        dialog.ok()
        self.assertEqual(dialog.result, dict(options, clicks=True))

    def test_recording_nothing_is_refused(self):
        dialog = self.keep(ui.RecordOptionsDialog(self.root, {"keyboard": False, "clicks": False,
                                                                "moves": False, "delay": True}))
        dialog.ok()
        self.assertFalse(self.closed(dialog))
        self.assertIn("at least one", dialog.error_label.cget("text"))


class SafetyDialogTests(DialogCase):
    ALL_ON = {"fs_keys": True, "fs_clicks": True, "fs_move": True, "fs_move_sensitivity": "medium",
              "fs_corner": True, "fs_window": True, "fs_preflight": True, "fs_crash": True,
              "fs_locked": True, "fs_screen": True, "fs_sleep": True}
    LIMIT = {"time_limit_on": False, "time_limit": 60.0}

    def test_shows_and_returns_every_switch(self):
        dialog = self.keep(ui.SafetyDialog(self.root, dict(self.ALL_ON, **self.LIMIT), stop_keys="NumLock or F10"))
        self.assertEqual(set(dialog.vars), set(self.ALL_ON) - {"fs_move_sensitivity"})
        self.assertEqual(dialog.sensitivity.get(), "Medium")
        dialog.checks["fs_keys"].toggle()
        dialog.checks["fs_window"].toggle()
        dialog.sensitivity.set("High")
        dialog.ok()
        self.assertEqual(dialog.result, dict(self.ALL_ON, fs_keys=False, fs_window=False,
                                             fs_move_sensitivity="high", **self.LIMIT))

    def test_sensitivity_greys_out_with_mouse_moves_off(self):
        dialog = self.keep(ui.SafetyDialog(self.root, dict(self.ALL_ON, fs_move=False, fs_move_sensitivity="low")))
        self.assertEqual(dialog.sensitivity._state, "disabled")
        self.assertEqual(dialog.sensitivity.get(), "Low")
        dialog.checks["fs_move"].toggle()
        self.assertEqual(dialog.sensitivity._state, "normal")

    def test_everything_off_is_allowed(self):
        off = {k: (v if k == "fs_move_sensitivity" else False) for k, v in self.ALL_ON.items()}
        dialog = self.keep(ui.SafetyDialog(self.root, off))
        dialog.ok()
        self.assertEqual(dialog.result, dict(off, **self.LIMIT))

    def test_time_limit(self):
        dialog = self.keep(ui.SafetyDialog(self.root, dict(self.ALL_ON, **self.LIMIT)))
        dialog.limit_check.toggle()
        ui.set_entry(dialog.limit_entry, "abc")
        dialog.ok()
        self.assertFalse(self.closed(dialog))
        self.assertIn("minutes from 1 to 10,080", dialog.error_label.cget("text"))
        ui.set_entry(dialog.limit_entry, "2,5")
        dialog.ok()
        self.assertEqual((dialog.result["time_limit_on"], dialog.result["time_limit"]), (True, 2.5))

    def test_a_bad_limit_while_off_keeps_the_old_one(self):
        dialog = self.keep(ui.SafetyDialog(self.root, dict(self.ALL_ON, **self.LIMIT)))
        ui.set_entry(dialog.limit_entry, "")
        dialog.ok()
        self.assertIsNone(dialog.result["time_limit"])


class BlackoutDialogTests(DialogCase):
    OPTIONS = {"blackout": False, "blackout_after": 5.0, "blackout_dim": True, "blackout_text": True,
               "keep_awake": True, "blackout_hotkey": None}

    def make(self, **options):
        return self.keep(ui.BlackoutDialog(self.root, dict(self.OPTIONS, **options),
                                           hotkey_spec.DEFAULT_BINDINGS, windows=True))

    def test_defaults_round_trip(self):
        dialog = self.make()
        self.assertEqual(dialog.key_button.cget("text"), "Not set")
        dialog.ok()
        self.assertEqual(dialog.result, self.OPTIONS)

    def test_turn_it_on_with_a_hotkey(self):
        dialog = self.make()
        dialog.checks["blackout"].toggle()
        ui.set_entry(dialog.after_entry, "10")
        dialog.key_button.invoke()
        self.assertEqual(dialog.key_button.cget("text"), "Press keys…")
        dialog.capture(0x11, ["ctrl"])                   # Ctrl alone: keeps waiting
        self.assertEqual(dialog.key_button.cget("text"), "Ctrl+…")
        dialog.capture(0x7B, ["ctrl"])
        self.assertEqual(dialog.key_button.cget("text"), "Ctrl+F12")
        dialog.ok()
        self.assertEqual(dialog.result, dict(self.OPTIONS, blackout=True, blackout_after=10.0,
                                             blackout_hotkey={"vk": 0x7B, "mods": ["ctrl"]}))

    def test_a_main_hotkey_is_refused(self):
        dialog = self.make()
        dialog.begin_capture()
        dialog.capture(0x79, [])                         # F10 = Panic stop
        self.assertIn("already Panic stop", dialog.error_label.cget("text"))
        self.assertTrue(dialog.capturing)

    def test_clear_and_escape(self):
        dialog = self.make(blackout_hotkey={"vk": 0x7B, "mods": []})
        self.assertEqual(dialog.key_button.cget("text"), "F12")
        dialog.begin_capture()
        dialog.on_key(SimpleNamespace(keysym="Escape", keycode=27, state=0))
        self.assertEqual(dialog.key_button.cget("text"), "F12")
        dialog.clear_button.invoke()
        dialog.ok()
        self.assertIsNone(dialog.result["blackout_hotkey"])

    def test_bad_delay(self):
        dialog = self.make()
        ui.set_entry(dialog.after_entry, "0")
        dialog.ok()
        self.assertFalse(self.closed(dialog))
        self.assertIn("blackout delay", dialog.error_label.cget("text"))

    def test_still_capturing_blocks_ok(self):
        dialog = self.make()
        dialog.begin_capture()
        dialog.ok()
        self.assertFalse(self.closed(dialog))


def seq_recording(letter="a"):
    return {"version": 1, "screen": {"width": 100, "height": 100}, "events": [
        {"t": 0.0, "type": "mouse_move", "x": 1, "y": 1},
        {"t": 0.5, "type": "key_press", "key": {"kind": "char", "value": letter}},
        {"t": 1.0, "type": "key_release", "key": {"kind": "char", "value": letter}},
    ]}


class SequenceDialogTests(DialogCase):
    def make(self, names=("Login", "Export"), add_files=None, current=None, **settings):
        import sequence as seq
        s = seq.new_sequence("Daily")
        for name in names:
            s["steps"].append(seq.make_step(name, seq_recording(name[0].lower())))
        s.update(settings)
        return self.keep(ui.SequenceDialog(self.root, s, add_files=add_files, current=current))

    def names(self, dialog):
        dialog._sync()
        return [row["name"] for row in dialog.rows]

    def explained(self, dialog):
        return dialog.explain_label.cget("text")

    def test_plain_column_names_each_with_a_tip(self):
        dialog = self.make()
        labels = [w.cget("text") for w in dialog.header_labels.values()]
        self.assertEqual(labels, ["Play ⓘ", "Step name ⓘ", "Pin ⓘ", "Times ⓘ", "Pause after ⓘ"])
        for key in ("play", "name", "pin", "times", "pause", "in_order", "shuffle", "same_shuffle",
                    "random_picks", "rounds", "forever", "round_pause", "reroll"):
            self.assertIn(key, dialog.tips)
        self.assertIn("EVERY round", ui.SEQUENCE_TIPS["pin"])

    def test_a_tip_shows_and_hides_and_stays_on_screen(self):
        dialog = self.make()
        tip = dialog.tips["pin"]
        with mock.patch.object(tip.widget, "winfo_pointerx", return_value=self.root.winfo_screenwidth() - 5), \
                mock.patch.object(tip.widget, "winfo_pointery", return_value=self.root.winfo_screenheight() - 5):
            tip.show()
        window = tip._tip
        window.update_idletasks()
        geometry = window.geometry().split("+")
        x, y = int(geometry[1]), int(geometry[2])
        self.assertLessEqual(x + window.winfo_reqwidth(), self.root.winfo_screenwidth())
        self.assertLessEqual(y + window.winfo_reqheight(), self.root.winfo_screenheight())
        tip.hide()
        self.assertIsNone(tip._tip)

    def test_no_emoji_tk_cannot_draw(self):
        # Tk on Windows can't draw characters outside the basic plane (📌, 🎲, ...).
        texts = list(ui.SEQUENCE_TIPS.values()) + [w.cget("text") for w in self.make().header_labels.values()]
        self.assertTrue(all(ord(c) <= 0xFFFF for text in texts for c in text))

    def test_pins_and_the_order_come_back_in_the_result(self):
        dialog = self.make(("Login", "A", "B", "Logout"))
        dialog.rows[0]["pin"].set("Always first")
        dialog.rows[3]["pin"].set("Always last")
        row = dialog.rows[1]["widgets"]
        ui.set_entry(row["repeat"], "3")
        ui.set_entry(row["wait"], "1-5")
        ui.set_entry(row["name"], "Fill form")
        dialog.order_var.set("shuffle")
        dialog.same_check.select()
        dialog.forever_check.select()
        ui.set_entry(dialog.round_wait_entry, "2")
        dialog._changed()
        dialog.ok()
        result = dialog.result
        self.assertEqual([s["pin"] for s in result["steps"]], ["first", "none", "none", "last"])
        self.assertEqual((result["order"], result["same_shuffle"], result["rounds"]), ("shuffle", True, 0))
        self.assertEqual(result["round_wait"], {"min": 2.0, "max": 2.0})
        step = result["steps"][1]
        self.assertEqual((step["name"], step["repeat"], step["wait"]), ("Fill form", 3, {"min": 1.0, "max": 5.0}))

    def test_the_sentence_and_example_follow_every_change(self):
        dialog = self.make(("Login", "A", "B", "Logout"))
        self.assertTrue(self.explained(dialog).startswith("Each round: Login, A, B and Logout in list order"))
        dialog.rows[0]["pin"].set("Always first")
        dialog.order_var.set("random_picks")
        ui.set_entry(dialog.picks_entry, "1")
        dialog._changed()
        self.assertIn("Login first, then 1 random step from A, B and Logout", self.explained(dialog))
        self.assertTrue(dialog.preview_label.cget("text").startswith("Example round:  Login → "))
        ui.set_entry(dialog.rows[1]["widgets"]["repeat"], "zero")
        dialog._changed()
        self.assertIn("Step 2 (A): Times must be", self.explained(dialog))
        self.assertEqual(dialog.preview_label.cget("text"), "Example round:  -")

    def test_reroll_changes_the_example_but_not_the_settings(self):
        dialog = self.make(("A", "B", "C", "D", "E", "F"))
        dialog.order_var.set("shuffle")
        dialog._changed()
        examples = set()
        for _ in range(8):
            dialog.reroll()
            examples.add(dialog.preview_label.cget("text"))
        self.assertGreater(len(examples), 1)
        self.assertEqual(dialog._order(), "shuffle")

    def test_typing_does_not_reshuffle_the_example(self):
        dialog = self.make(("A", "B", "C", "D", "E", "F"))
        dialog.order_var.set("shuffle")
        dialog._changed()
        before = dialog.preview_label.cget("text")
        ui.set_entry(dialog.name_entry, "Other name")
        dialog._changed()
        self.assertEqual(dialog.preview_label.cget("text"), before)

    def test_warnings_show_and_go(self):
        dialog = self.make(("A", "B"))
        dialog.order_var.set("random_picks")
        for row in dialog.rows:
            row["pin"].set("Always first")
        dialog._changed()
        self.assertTrue(dialog._warning_shown)
        self.assertIn("nothing to pick at random", dialog.warning_label.cget("text"))
        dialog.rows[0]["pin"].set("Anywhere")
        dialog._changed()
        self.assertFalse(dialog._warning_shown)

    def test_only_the_settings_of_the_chosen_order_are_active(self):
        dialog = self.make()
        self.assertEqual(dialog.picks_entry.cget("state"), "disabled")
        self.assertEqual(dialog.same_check.cget("state"), "disabled")
        dialog.order_var.set("random_picks")
        dialog._changed()
        self.assertEqual(dialog.picks_entry.cget("state"), "normal")
        dialog.forever_check.select()
        dialog._changed()
        self.assertEqual(dialog.rounds_entry.cget("state"), "disabled")

    def test_an_empty_pause_box_means_no_pause(self):
        dialog = self.make()
        self.assertEqual(dialog.rows[0]["widgets"]["wait"].get(), "")
        dialog.ok()
        self.assertEqual(dialog.result["steps"][0]["wait"], {"min": 0.0, "max": 0.0})

    def test_move_and_remove_keep_what_was_typed(self):
        dialog = self.make(("A", "B", "C"))
        ui.set_entry(dialog.rows[2]["widgets"]["repeat"], "7")
        dialog.rows[2]["pin"].set("Always last")
        dialog.move(2, "up")
        self.assertEqual(self.names(dialog), ["A", "C", "B"])
        self.assertEqual((dialog.rows[1]["repeat"], dialog.rows[1]["pin"].get()), ("7", "Always last"))
        dialog.move(0, "remove")
        self.assertEqual(self.names(dialog), ["C", "B"])
        self.assertEqual(dialog.rows[0]["widgets"]["up"].cget("state"), "disabled")
        self.assertEqual(dialog.rows[-1]["widgets"]["down"].cget("state"), "disabled")

    def test_add_files_and_current(self):
        import storage
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        good = Path(folder.name) / "daily_export.json"
        storage.save_recording(good, seq_recording("x"))
        bad = Path(folder.name) / "broken.json"
        bad.write_text("{", encoding="utf-8")
        dialog = self.make((), add_files=lambda parent: [str(good), str(bad)],
                           current=("Last recording", seq_recording("z")))
        self.assertTrue(dialog.empty_label.winfo_manager())
        self.assertIn("Add at least one recording", self.explained(dialog))
        dialog.add_button.invoke()
        dialog.add_current_button.invoke()
        self.assertEqual(self.names(dialog), ["daily export", "Last recording"])
        self.assertIn("broken.json", dialog.error_label.cget("text"))
        self.assertTrue(self.explained(dialog).startswith("Each round:"))

    def test_without_sources_the_buttons_are_off(self):
        dialog = self.make()
        self.assertEqual(dialog.add_button.cget("state"), "disabled")
        self.assertEqual(dialog.add_current_button.cget("state"), "disabled")

    def test_opens_an_old_version_1_sequence(self):
        import sequence as seq
        old = {"kind": "wackro-sequence", "version": 1, "name": "Old", "order": "shuffle_once", "picks": 0,
               "rounds": 2, "round_wait": {"min": 0.0, "max": 0.0}, "steps": [
                   {"name": n, "enabled": True, "locked": n == "Login", "repeat": 1,
                    "wait": {"min": 0.0, "max": 0.0}, "recording": seq_recording("q")} for n in ("Login", "A", "B")]}
        dialog = self.keep(ui.SequenceDialog(self.root, old))
        self.assertEqual(dialog.rows[0]["pin"].get(), "Always first")
        self.assertEqual(dialog.order_var.get(), "shuffle")
        self.assertTrue(dialog.same_var.get())

    def test_problems_keep_it_open(self):
        cases = [
            (lambda d: [d.move(0, "remove"), d.move(0, "remove")], "at least one recording"),
            (lambda d: ui.set_entry(d.rows[0]["widgets"]["repeat"], "0"), "Step 1 (Login): Times"),
            (lambda d: ui.set_entry(d.rows[1]["widgets"]["wait"], "soon"), "Step 2 (Export)"),
            (lambda d: [r["enabled"].set(False) for r in d.rows], "Tick Play on at least one step"),
            (lambda d: ui.set_entry(d.rounds_entry, "0"), "Rounds must be"),
            (lambda d: ui.set_entry(d.round_wait_entry, "x"), "Pause between rounds"),
            (lambda d: (d.order_var.set("random_picks"), d._changed(), ui.set_entry(d.picks_entry, "0")),
             "Pick at random"),
        ]
        for change, words in cases:
            with self.subTest(words=words):
                dialog = self.make()
                change(dialog)
                dialog.ok()
                self.assertFalse(self.closed(dialog))
                self.assertIn(words, dialog.error_label.cget("text"))

    def test_enter_never_closes_it(self):
        dialog = self.make()
        dialog._on_return()
        self.assertFalse(self.closed(dialog))


def key(keysym, keycode, state=0):
    return SimpleNamespace(keysym=keysym, keycode=keycode, state=state)


class RecordingsFolderDialogTests(DialogCase):
    def make(self, current=None, default=None, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.default = default or str(self.tmp / "Documents" / "Wackro")
        return self.keep(ui.RecordingsFolderDialog(self.root, current or self.default, self.default, **kwargs))

    def test_shows_the_current_folder(self):
        dialog = self.make()
        self.assertEqual(dialog.path_entry.get(), self.default)

    def test_ok_on_the_default_stores_none_and_creates_it(self):
        dialog = self.make()
        dialog.ok()
        self.assertEqual(dialog.result, {"recordings_dir": None})
        self.assertTrue(Path(self.default).is_dir())

    def test_a_folder_of_your_own(self):
        dialog = self.make()
        own = str(self.tmp / "Mine" / "Recs")
        ui.set_entry(dialog.path_entry, own)
        dialog.ok()
        self.assertEqual(dialog.result, {"recordings_dir": own})
        self.assertTrue(Path(own).is_dir())

    def test_reset_to_default(self):
        dialog = self.make()
        ui.set_entry(dialog.path_entry, str(self.tmp / "Other"))
        dialog.default_button.invoke()
        self.assertEqual(dialog.path_entry.get(), self.default)

    def test_bad_paths_keep_it_open(self):
        dialog = self.make(make_dir=lambda path: False)
        for text, message in (("relative\\folder", "full folder path"), (self.default, "can't create")):
            with self.subTest(text=text):
                ui.set_entry(dialog.path_entry, text)
                dialog.ok()
                self.assertIsNone(dialog.result)
                self.assertTrue(dialog.winfo_exists())
                self.assertIn(message, dialog.error_label.cget("text"))

    def test_browse_and_open_folder_use_the_given_helpers(self):
        opened = []
        picked = str(Path(tempfile.gettempdir()) / "Picked")
        dialog = self.make(browse=lambda parent, start: picked, open_folder=opened.append,
                           make_dir=lambda path: True)
        dialog.browse_button.invoke()
        self.assertEqual(dialog.path_entry.get(), picked)
        dialog.open_button.invoke()
        self.assertEqual(opened, [picked])
        cancel = self.make(browse=lambda parent, start: None)
        cancel.browse_button.invoke()
        self.assertEqual(cancel.path_entry.get(), self.default)


class HotkeysDialogTests(DialogCase):
    def make(self, bindings=None, windows=True):
        return self.keep(ui.HotkeysDialog(self.root, bindings or hotkey_spec.DEFAULT_BINDINGS, windows=windows))

    def label(self, dialog, action):
        return dialog.key_buttons[action].cget("text")

    def test_shows_the_current_keys(self):
        dialog = self.make()
        self.assertEqual(self.label(dialog, "record"), "F9")
        self.assertEqual(self.label(dialog, "play"), "F11")
        self.assertEqual(self.label(dialog, "stop"), "F10")

    def test_capture_ctrl_grave_on_windows(self):
        dialog = self.make({"record": {"vk": 0x78, "mods": []}, "play": hotkey_spec.DEFAULT_BINDINGS["play"],
                            "stop": hotkey_spec.DEFAULT_BINDINGS["stop"]})
        dialog.key_buttons["record"].invoke()
        self.assertEqual(self.label(dialog, "record"), "Press keys…")
        self.assertEqual(dialog.on_key(key("Control_L", 17)), "break")
        self.assertEqual(self.label(dialog, "record"), "Ctrl+…")
        self.assertEqual(dialog.on_key(key("grave", 192, state=0x0004)), "break")
        self.assertEqual(self.label(dialog, "record"), "Ctrl+`")
        dialog.ok()
        self.assertEqual(dialog.result["record"], {"mods": ["ctrl"], "vk": 0xC0})

    def test_capture_numlock_off_windows(self):
        dialog = self.make({"record": hotkey_spec.DEFAULT_BINDINGS["record"], "play": {"vk": 0x7A, "mods": []},
                            "stop": hotkey_spec.DEFAULT_BINDINGS["stop"]}, windows=False)
        dialog.begin_capture("play")
        dialog.on_key(key("Num_Lock", 77, state=0x0010))
        dialog.ok()
        self.assertEqual(dialog.result["play"], {"mods": [], "vk": 0x90})

    def test_clash_is_refused(self):
        dialog = self.make()
        dialog.begin_capture("record")
        dialog.on_key(key("F11", 122))
        self.assertIn("already Play / Stop", dialog.error_label.cget("text"))
        self.assertEqual(dialog.capturing, "record")          # still waiting for another key
        dialog.on_key(key("F8", 119))
        self.assertIsNone(dialog.capturing)
        self.assertEqual(self.label(dialog, "record"), "F8")

    def test_escape_cancels_the_capture_not_the_box(self):
        dialog = self.make()
        dialog.begin_capture("stop")
        dialog.on_key(key("Escape", 27))
        self.assertIsNone(dialog.capturing)
        self.assertEqual(self.label(dialog, "stop"), "F10")
        self.assertFalse(self.closed(dialog))
        self.assertEqual(dialog._on_return(), "break")
        self.assertTrue(self.closed(dialog))   # Enter = OK once nothing is being captured

    def test_enter_during_capture_is_captured_not_ok(self):
        dialog = self.make()
        dialog.begin_capture("stop")
        self.assertEqual(dialog._on_return(), "break")
        self.assertFalse(self.closed(dialog))
        dialog.on_key(key("Return", 13))
        self.assertEqual(self.label(dialog, "stop"), "Enter")

    def test_ok_while_capturing_is_refused(self):
        dialog = self.make()
        dialog.begin_capture("record")
        dialog.ok()
        self.assertFalse(self.closed(dialog))
        self.assertIn("Press the keys", dialog.error_label.cget("text"))

    def test_unusable_keys(self):
        dialog = self.make(windows=False)
        dialog.begin_capture("record")
        dialog.on_key(key("XF86AudioMicMute", 198))
        self.assertIn("can't be used", dialog.error_label.cget("text"))

    def test_keys_ignored_when_not_capturing(self):
        dialog = self.make()
        self.assertIsNone(dialog.on_key(key("a", 65)))

    def test_switching_boxes_restores_the_first(self):
        dialog = self.make()
        dialog.begin_capture("record")
        dialog.begin_capture("play")
        self.assertEqual(self.label(dialog, "record"), "F9")
        self.assertEqual(dialog.capturing, "play")

    def test_reset_to_defaults(self):
        dialog = self.make({"record": {"vk": 0xC0, "mods": ["ctrl"]}, "play": {"vk": 0x90, "mods": []},
                            "stop": {"vk": 0x7B, "mods": []}})
        dialog.reset_button.invoke()
        dialog.ok()
        self.assertEqual(dialog.result, hotkey_spec.DEFAULT_BINDINGS)

    def test_result_is_a_copy(self):
        dialog = self.make()
        dialog.ok()
        dialog.result["record"]["mods"].append("alt")
        self.assertEqual(hotkey_spec.DEFAULT_BINDINGS["record"]["mods"], [])


class CloseDialogTests(DialogCase):
    def test_choices(self):
        for button, expected in (("exit_button", ("exit", False)), ("tray_button", ("tray", False)),
                                 ("cancel_choice", ("cancel", False))):
            with self.subTest(button=button):
                dialog = self.keep(ui.CloseDialog(self.root))
                getattr(dialog, button).invoke()
                self.assertEqual(dialog.result, expected)
                self.assertTrue(self.closed(dialog))

    def test_dont_ask_again(self):
        dialog = self.keep(ui.CloseDialog(self.root))
        dialog.remember_check.toggle()
        dialog.tray_button.invoke()
        self.assertEqual(dialog.result, ("tray", True))

    def test_remember_never_sticks_to_cancel(self):
        dialog = self.keep(ui.CloseDialog(self.root))
        dialog.remember_check.toggle()
        dialog.cancel_choice.invoke()
        self.assertEqual(dialog.result, ("cancel", False))

    def test_no_tray_hides_that_choice(self):
        dialog = self.keep(ui.CloseDialog(self.root, tray=False))
        self.assertEqual(dialog.tray_button.winfo_manager(), "")
        dialog._on_return()
        self.assertEqual(dialog.result, ("exit", False))

    def test_window_x_is_cancel(self):
        dialog = self.keep(ui.CloseDialog(self.root))
        dialog.cancel()
        self.assertEqual(dialog.result, ("cancel", False))


class KeyboardChoiceTests(DialogCase):
    def key(self, dialog, keysym):
        dialog.event_generate(f"<{keysym}>" if len(keysym) > 1 else keysym)

    def test_save_changes_arrows_and_enter(self):
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        self.assertEqual(dialog.selected_value(), "yes")         # Yes highlighted to start
        dialog.move_choice(1)
        self.assertEqual(dialog.selected_value(), "no")
        self.assertEqual(dialog.no_button.cget("border_color"), ui.FOCUS_RING)
        self.assertNotEqual(dialog.yes_button.cget("border_color"), ui.FOCUS_RING)
        dialog.move_choice(1)
        dialog.move_choice(1)                                    # wraps around to Yes
        self.assertEqual(dialog.selected_value(), "yes")
        dialog.move_choice(-1)
        self.assertEqual(dialog.selected_value(), "cancel")
        dialog._on_return()
        self.assertEqual(dialog.result, "cancel")
        self.assertTrue(self.closed(dialog))

    def test_every_key_is_wired(self):
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        bound = set(dialog.bind())
        for sequence in ("<Key-Left>", "<Key-Right>", "<Key-Up>", "<Key-Down>", "<Key-Tab>", "<Key-space>",
                         "<Key-Return>", "<Key-Escape>", "<Key>"):
            self.assertIn(sequence, bound)

    def test_real_key_presses(self):
        # Faked keys only reach a window that really has the keyboard focus. On Windows a test
        # window usually can't take it from the console running the tests, so this part only
        # runs where it can; the wiring and every key's action are tested above and below.
        for keys, expected in ((["Right", "Return"], "no"), (["Right", "Right", "space"], "cancel"),
                               (["Left", "Return"], "cancel"), (["Return"], "yes"), (["Tab", "Return"], "no")):
            with self.subTest(keys=keys):
                dialog = self.keep(ui.SaveChangesDialog(self.root))
                dialog.focus_force()
                dialog.update()
                if dialog.focus_get() is None or dialog.focus_get().winfo_toplevel() is not dialog:
                    self.skipTest("this test window can't get the keyboard focus here")
                for keysym in keys:
                    self.key(dialog, keysym)
                    dialog.update()
                self.assertEqual(dialog.result, expected)

    def test_letters(self):
        for letter, expected in (("y", "yes"), ("n", "no"), ("c", "cancel"), ("N", "no")):
            with self.subTest(letter=letter):
                dialog = self.keep(ui.SaveChangesDialog(self.root))
                dialog._on_letter(SimpleNamespace(keysym=letter))
                self.assertEqual(dialog.result, expected)
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        self.assertIsNone(dialog._on_letter(SimpleNamespace(keysym="q")))     # other keys do nothing
        self.assertFalse(self.closed(dialog))

    def test_save_changes_is_urgent(self):
        dialog = self.keep(ui.SaveChangesDialog(self.root, "Starting a new recording replaces it."))
        dialog.update()
        self.assertTrue(dialog.urgent)
        self.assertTrue(int(dialog.attributes("-topmost")))
        self.assertTrue(dialog.warning_bar.winfo_manager())
        self.assertIn("←", dialog.hint.cget("text"))

    def test_urgent_box_comes_forward_and_alerts_even_with_the_window_open(self):
        self.root.deiconify()
        self.root.update()
        win = FakeWin()
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        dialog._win = win
        self.assertFalse(dialog.standalone)
        self.assertNotEqual(dialog.transient(), "")              # still attached to the window
        dialog.after(350, lambda: dialog.no_button.invoke())
        self.assertEqual(dialog.run(), "no")
        self.assertGreaterEqual(len(win.forced), 1)
        self.assertEqual(len(win.alerts), 1)

    def test_close_box_up_down(self):
        dialog = self.keep(ui.CloseDialog(self.root))
        self.assertEqual(dialog.selected_value(), "tray")
        dialog.move_choice(-1)
        dialog._on_return()
        self.assertEqual(dialog.result, ("exit", False))
        dialog = self.keep(ui.CloseDialog(self.root))
        dialog._on_letter(SimpleNamespace(keysym="h"))
        self.assertEqual(dialog.result, ("tray", False))
        dialog = self.keep(ui.CloseDialog(self.root, tray=False))
        self.assertEqual([v for v, _ in dialog.choices], ["exit", "cancel"])
        self.assertIsNone(dialog._on_letter(SimpleNamespace(keysym="h")))   # no tray: H does nothing


class FakeWin:
    def __init__(self):
        self.forced = []
        self.restored = []

    def monitors(self):
        import winutil
        return [winutil.Monitor(0, 0, 1000, 800, True), winutil.Monitor(1000, 0, 2000, 800, False)]

    def force_foreground(self, hwnd):
        self.forced.append(hwnd)
        return 99

    def restore_foreground(self, previous):
        self.restored.append(previous)

    def alert(self, hwnd):
        self.alerts = getattr(self, "alerts", []) + [hwnd]
        return True


class StandaloneTests(DialogCase):
    """Main window hidden (in the tray): the box stands alone, on top, focused."""

    def test_hidden_parent_makes_it_standalone_and_topmost(self):
        dialog = self.keep(ui.SaveChangesDialog(self.root))     # self.root is withdrawn
        dialog.update()                                          # X11 reports -topmost once shown
        self.assertTrue(dialog.standalone)
        self.assertTrue(int(dialog.attributes("-topmost")))
        self.assertEqual(dialog.transient(), "")                 # not tied to the hidden window

    def test_visible_parent_keeps_it_attached(self):
        self.root.deiconify()
        self.root.update()
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        self.assertFalse(dialog.standalone)
        self.assertNotEqual(dialog.transient(), "")

    def test_run_forces_it_to_the_front_and_gives_the_focus_back_after(self):
        win = FakeWin()
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        dialog._win = win
        dialog.after(350, lambda: dialog.no_button.invoke())
        self.assertEqual(dialog.run(), "no")
        self.assertGreaterEqual(len(win.forced), 1)
        self.assertEqual(win.restored, [99])     # back to the program that had the focus

    def test_centered_on_the_monitor_with_the_mouse(self):
        win = FakeWin()
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        dialog._win = win
        asked = []
        # Check the position the box asks for: on Windows CustomTkinter hides and re-shows a new
        # box, so where it really is only settles later.
        with mock.patch.object(dialog, "winfo_pointerxy", return_value=(1500, 300)), \
                mock.patch.object(dialog, "geometry", side_effect=lambda g=None: asked.append(g)):
            dialog._center()
        x = int(asked[-1].split("+")[1])
        self.assertGreaterEqual(x, 1000)                          # on the second monitor
        self.assertLess(x, 2000)

    def test_pointer_monitor_falls_back_to_the_main_one(self):
        win = FakeWin()
        with mock.patch.object(self.root, "winfo_pointerxy", side_effect=tkinter.TclError("x")):
            self.assertTrue(ui.pointer_monitor(self.root, win).primary)


class NoRealWindowsCallsTests(DialogCase):
    def test_dialogs_in_tests_use_the_harmless_stand_in(self):
        import winutil
        dialog = self.keep(ui.SpeedDialog(self.root, 1.0))
        self.assertIs(type(dialog._win), winutil.NullWin32)


class SaveChangesTests(DialogCase):
    def test_buttons(self):
        for button, expected in (("yes_button", "yes"), ("no_button", "no"), ("cancel_choice", "cancel")):
            with self.subTest(button=button):
                dialog = self.keep(ui.SaveChangesDialog(self.root, "detail"))
                getattr(dialog, button).invoke()
                self.assertEqual(dialog.result, expected)
                self.assertTrue(self.closed(dialog))

    def test_enter_is_yes_and_escape_is_cancel(self):
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        dialog._on_return()
        self.assertEqual(dialog.result, "yes")
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        dialog._on_escape()
        self.assertEqual(dialog.result, "cancel")

    def test_order_is_yes_no_cancel(self):
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        dialog.update_idletasks()
        xs = [dialog.yes_button.winfo_x(), dialog.no_button.winfo_x(), dialog.cancel_choice.winfo_x()]
        self.assertEqual(xs, sorted(xs))

    def test_run_returns_the_choice(self):
        dialog = self.keep(ui.SaveChangesDialog(self.root))
        dialog.after(100, lambda: dialog.no_button.invoke())
        self.assertEqual(dialog.run(), "no")


class InfoTests(DialogCase):
    def test_info(self):
        dialog = self.keep(ui.InfoDialog(self.root, "About", config.APP_NAME, "hello"))
        self.assertEqual(dialog.text_label.cget("text"), "hello")
        dialog.ok()
        self.assertTrue(dialog.result)


if __name__ == "__main__":
    unittest.main()
