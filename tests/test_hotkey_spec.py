"""Tests for hotkey_spec.py (bindings, labels, key -> vk, and the matcher).

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

from pynput import keyboard  # noqa: E402

import hotkey_spec as hs  # noqa: E402
from hotkey_spec import HotkeyMatcher  # noqa: E402

# The pre-1.7.4 defaults (Ctrl+` / NumLock / F10). The behaviour tests use them on purpose: a binding
# with a modifier (Ctrl+`) and a lock key (NumLock) exercise more of the matcher than plain F-keys.
CLASSIC = {"record": {"mods": ["ctrl"], "vk": 0xC0}, "play": {"mods": [], "vk": 0x90},
           "stop": {"mods": [], "vk": 0x79}}


CTRL, LCTRL, RCTRL, SHIFT, ALT, LWIN = 0x11, 0xA2, 0xA3, 0xA0, 0xA4, 0x5B
GRAVE, NUMLOCK, F10, A = 0xC0, 0x90, 0x79, 0x41


class DefaultsTests(unittest.TestCase):
    def test_defaults_are_the_users_keys(self):
        self.assertEqual(hs.label(hs.DEFAULT_BINDINGS["record"]), "F9")
        self.assertEqual(hs.label(hs.DEFAULT_BINDINGS["play"]), "F11")
        self.assertEqual(hs.label(hs.DEFAULT_BINDINGS["stop"]), "F10")

    def test_defaults_are_valid_and_distinct(self):
        self.assertEqual(hs.sanitize_bindings(hs.DEFAULT_BINDINGS), hs.DEFAULT_BINDINGS)
        self.assertIsNone(hs.find_clash(hs.DEFAULT_BINDINGS))

    def test_every_action_has_a_label(self):
        self.assertEqual(set(hs.ACTION_LABELS), set(hs.ACTIONS))


class LabelTests(unittest.TestCase):
    def test_modifiers_come_in_a_fixed_order(self):
        binding = hs.make_binding(A, ["shift", "win", "ctrl", "alt"])
        self.assertEqual(binding["mods"], ["ctrl", "alt", "shift", "win"])
        self.assertEqual(hs.label(binding), "Ctrl+Alt+Shift+Win+A")

    def test_names(self):
        cases = {0x70: "F1", 0x87: "F24", 0x30: "0", 0x5A: "Z", 0x60: "Num 0", 0x20: "Space",
                 0x91: "ScrollLock", 0xDC: "\\", 0x13: "Pause"}
        for vk, name in cases.items():
            with self.subTest(vk=vk):
                self.assertEqual(hs.vk_name(vk), name)

    def test_unknown_key_shows_its_code(self):
        self.assertEqual(hs.vk_name(0xE9), "Key 0xe9")

    def test_none(self):
        self.assertEqual(hs.label(None), "(none)")


class ValidationTests(unittest.TestCase):
    def test_modifier_alone_is_rejected(self):
        for vk in hs.MODIFIER_VKS:
            with self.subTest(vk=vk):
                self.assertIn("modifier", hs.binding_problem(vk, []))

    def test_escape_and_mouse_buttons_are_rejected(self):
        self.assertIn("Esc", hs.binding_problem(0x1B, []))
        self.assertIsNotNone(hs.binding_problem(0x01, []))

    def test_bad_values(self):
        for vk in (0, -1, 0xFF, 300, True, 1.5, "65", None):
            with self.subTest(vk=vk):
                self.assertIsNotNone(hs.binding_problem(vk, []))
        self.assertIsNotNone(hs.binding_problem(A, ["hyper"]))

    def test_good(self):
        self.assertIsNone(hs.binding_problem(A, ["ctrl", "shift"]))
        self.assertIsNone(hs.binding_problem(NUMLOCK, []))

    def test_sanitize_binding(self):
        self.assertEqual(hs.sanitize_binding({"vk": A, "mods": ["shift", "ctrl", "ctrl"]}),
                         {"mods": ["ctrl", "shift"], "vk": A})
        self.assertEqual(hs.sanitize_binding({"vk": A}), {"mods": [], "vk": A})
        for bad in (None, [], {"vk": "A"}, {"vk": A, "mods": "ctrl"}, {"vk": A, "mods": [1]},
                    {"vk": 0x11, "mods": []}):
            with self.subTest(bad=bad):
                self.assertIsNone(hs.sanitize_binding(bad))


class SanitizeBindingsTests(unittest.TestCase):
    def test_garbage_gives_defaults(self):
        for data in (None, [], "x", {"record": "nope"}):
            with self.subTest(data=data):
                self.assertEqual(hs.sanitize_bindings(data), hs.DEFAULT_BINDINGS)

    def test_one_bad_binding_falls_back_on_its_own(self):
        data = {"record": {"vk": A, "mods": ["ctrl"]}, "play": {"vk": "bad"}, "stop": {"vk": 0x7B, "mods": []}}
        result = hs.sanitize_bindings(data)
        self.assertEqual(result["record"], {"mods": ["ctrl"], "vk": A})
        self.assertEqual(result["play"], hs.DEFAULT_BINDINGS["play"])
        self.assertEqual(result["stop"], {"mods": [], "vk": 0x7B})

    def test_a_clash_falls_back_to_the_default(self):
        data = {"record": {"vk": A, "mods": []}, "play": {"vk": A, "mods": []}}
        result = hs.sanitize_bindings(data)
        self.assertEqual(result["record"], {"mods": [], "vk": A})
        self.assertEqual(result["play"], hs.DEFAULT_BINDINGS["play"])

    def test_clash_with_a_default_resets_everything(self):
        # play taken by record's keys, and play's default (F11) taken too: all defaults
        data = {"record": {"vk": 0x7A, "mods": []}, "play": {"vk": 0x7A, "mods": []},
                "stop": {"vk": F10, "mods": []}}
        self.assertEqual(hs.sanitize_bindings(data), hs.DEFAULT_BINDINGS)

    def test_find_clash(self):
        same = {"vk": A, "mods": ["ctrl"]}
        self.assertEqual(hs.find_clash({"record": same, "play": dict(same), "stop": {"vk": F10, "mods": []}}),
                         ("record", "play"))
        self.assertIsNone(hs.find_clash({"record": same, "play": {"vk": A, "mods": []},
                                         "stop": {"vk": F10, "mods": []}}))


class FakeKey:
    """Looks like a keyboard.Key member: .name, and .value with a .vk."""

    def __init__(self, name, vk=None):
        self.name = name
        self.value = keyboard.KeyCode.from_vk(vk) if vk is not None else None


class KeyToVkTests(unittest.TestCase):
    def test_special_keys_by_name_off_windows(self):
        # Stand-ins for keyboard.Key members: pynput's headless "dummy" backend gives every Key
        # the same value, so the real enum can't be used to test names here.
        self.assertEqual(hs.key_to_vk(FakeKey("num_lock"), windows=False), NUMLOCK)
        self.assertEqual(hs.key_to_vk(FakeKey("f10"), windows=False), F10)
        self.assertEqual(hs.key_to_vk(FakeKey("ctrl_l"), windows=False), LCTRL)
        self.assertIsNone(hs.key_to_vk(FakeKey("no_such_key"), windows=False))

    def test_special_keys_on_windows_use_their_vk(self):
        self.assertEqual(hs.key_to_vk(FakeKey("num_lock", vk=NUMLOCK), windows=True), NUMLOCK)
        self.assertEqual(hs.key_to_vk(FakeKey("f10", vk=None), windows=True), F10)   # falls back to the name

    def test_characters_off_windows(self):
        self.assertEqual(hs.key_to_vk(keyboard.KeyCode.from_char("`"), windows=False), GRAVE)
        self.assertEqual(hs.key_to_vk(keyboard.KeyCode.from_char("~"), windows=False), GRAVE)
        self.assertEqual(hs.key_to_vk(keyboard.KeyCode.from_char("a"), windows=False), A)
        self.assertEqual(hs.key_to_vk(keyboard.KeyCode.from_char("A"), windows=False), A)
        self.assertEqual(hs.key_to_vk(keyboard.KeyCode.from_char("!"), windows=False), 0x31)

    def test_control_characters_map_back_to_letters(self):
        self.assertEqual(hs.key_to_vk(keyboard.KeyCode.from_char("\x01"), windows=False), A)

    def test_windows_uses_the_real_vk_even_without_a_character(self):
        # With Ctrl held, Windows gives ` as a bare vk code with no character.
        self.assertEqual(hs.key_to_vk(keyboard.KeyCode.from_vk(GRAVE), windows=True), GRAVE)
        self.assertEqual(hs.key_to_vk(keyboard.KeyCode(vk=0x41, char="a"), windows=True), 0x41)

    def test_unknown(self):
        self.assertIsNone(hs.key_to_vk(keyboard.KeyCode.from_char("é"), windows=False))
        self.assertIsNone(hs.key_to_vk(object(), windows=False))


class TkTests(unittest.TestCase):
    def test_keysym_on_windows_is_the_keycode(self):
        self.assertEqual(hs.keysym_to_vk("grave", 192, windows=True), 192)
        self.assertIsNone(hs.keysym_to_vk("??", 0, windows=True))

    def test_keysym_elsewhere(self):
        self.assertEqual(hs.keysym_to_vk("grave", 49, windows=False), GRAVE)
        self.assertEqual(hs.keysym_to_vk("Num_Lock", 77, windows=False), NUMLOCK)
        self.assertEqual(hs.keysym_to_vk("F10", 76, windows=False), F10)
        self.assertEqual(hs.keysym_to_vk("a", 38, windows=False), A)
        self.assertIsNone(hs.keysym_to_vk("XF86Something", 1, windows=False))

    def test_state_mods(self):
        self.assertEqual(hs.tk_state_mods(0x0004, windows=True), {"ctrl"})
        self.assertEqual(hs.tk_state_mods(0x0005 | 0x20000, windows=True), {"ctrl", "shift", "alt"})
        self.assertEqual(hs.tk_state_mods(0x0008 | 0x0040, windows=False), {"alt", "win"})
        self.assertEqual(hs.tk_state_mods(0x0010, windows=False), set())   # NumLock state bit


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.m = HotkeyMatcher(CLASSIC)

    def test_ctrl_grave_fires_record_and_swallows_only_the_grave(self):
        self.assertEqual(self.m.key_down(LCTRL), (None, False))
        self.assertEqual(self.m.key_down(GRAVE), ("record", True))
        self.assertTrue(self.m.key_up(GRAVE))
        self.assertFalse(self.m.key_up(LCTRL))

    def test_grave_alone_is_just_a_key(self):
        self.assertEqual(self.m.key_down(GRAVE), (None, False))
        self.assertFalse(self.m.key_up(GRAVE))

    def test_extra_modifiers_dont_match(self):
        self.m.key_down(LCTRL)
        self.m.key_down(SHIFT)
        self.assertEqual(self.m.key_down(GRAVE), (None, False))

    def test_numlock_and_f10(self):
        self.assertEqual(self.m.key_down(NUMLOCK), ("play", True))
        self.assertTrue(self.m.key_up(NUMLOCK))
        self.assertEqual(self.m.key_down(F10), ("stop", True))
        self.assertTrue(self.m.key_up(F10))

    def test_numlock_with_ctrl_held_is_not_play(self):
        self.m.key_down(RCTRL)
        self.assertEqual(self.m.key_down(NUMLOCK), (None, False))

    def test_auto_repeat_fires_once_and_keeps_swallowing(self):
        self.assertEqual(self.m.key_down(NUMLOCK), ("play", True))
        self.assertEqual(self.m.key_down(NUMLOCK), (None, True))
        self.assertEqual(self.m.key_down(NUMLOCK), (None, True))
        self.assertTrue(self.m.key_up(NUMLOCK))
        self.assertEqual(self.m.key_down(NUMLOCK), ("play", True))

    def test_releasing_ctrl_first_still_swallows_the_grave_up(self):
        self.m.key_down(LCTRL)
        self.m.key_down(GRAVE)
        self.m.key_up(LCTRL)
        self.assertTrue(self.m.key_up(GRAVE))

    def test_injected_keys_never_fire_or_swallow(self):
        self.assertEqual(self.m.key_down(NUMLOCK, injected=True), (None, False))
        self.assertFalse(self.m.key_up(NUMLOCK, injected=True))

    def test_injected_modifiers_dont_count(self):
        # Our player holding Ctrl must not turn the user's ` into Ctrl+`.
        self.m.key_down(LCTRL, injected=True)
        self.assertEqual(self.m.key_down(GRAVE), (None, False))

    def test_disabled_does_nothing(self):
        self.m.enabled = False
        self.assertEqual(self.m.key_down(NUMLOCK), (None, False))
        self.assertFalse(self.m.key_up(NUMLOCK))

    def test_a_key_that_fired_before_disabling_still_has_its_up_swallowed(self):
        self.m.key_down(NUMLOCK)
        self.m.enabled = False
        self.assertTrue(self.m.key_up(NUMLOCK))

    def test_generic_ctrl_up_clears_both_sides(self):
        self.m.key_down(LCTRL)
        self.m.key_down(RCTRL)
        self.m.key_up(CTRL)
        self.assertEqual(self.m.held_modifiers(), set())

    def test_one_ctrl_up_keeps_the_other_side(self):
        self.m.key_down(LCTRL)
        self.m.key_down(RCTRL)
        self.m.key_up(LCTRL)
        self.assertEqual(self.m.held_modifiers(), {"ctrl"})

    def test_win_modifier(self):
        self.m.set_bindings({"record": {"vk": A, "mods": ["win"]}, "play": CLASSIC["play"],
                             "stop": CLASSIC["stop"]})
        self.m.key_down(LWIN)
        self.assertEqual(self.m.key_down(A), ("record", True))

    def test_set_bindings_takes_effect(self):
        new = {"record": {"vk": 0x78, "mods": []}, "play": {"vk": 0x7A, "mods": []}, "stop": {"vk": F10, "mods": []}}
        self.m.set_bindings(new)
        self.assertEqual(self.m.key_down(NUMLOCK), (None, False))
        self.m.key_up(NUMLOCK)
        self.assertEqual(self.m.key_down(0x7A), ("play", True))

    def test_bindings_are_copies(self):
        bindings = self.m.bindings
        bindings["record"]["mods"].append("alt")
        self.assertEqual(self.m.bindings["record"]["mods"], ["ctrl"])

    def test_reset(self):
        self.m.key_down(LCTRL)
        self.m.key_down(NUMLOCK)
        self.m.reset()
        self.assertEqual(self.m.held_modifiers(), set())
        self.assertFalse(self.m.key_up(NUMLOCK))

    def test_live_modifier_state_beats_stuck_tracked_state(self):
        # Ctrl+Alt+Del: the hook saw Ctrl and Alt go down but never saw them come up.
        held = set()
        m = HotkeyMatcher(CLASSIC, mods_reader=lambda: held)
        m.key_down(LCTRL)
        m.key_down(ALT)
        self.assertEqual(m.key_down(NUMLOCK), ("play", True))   # really nothing is held
        m.key_up(NUMLOCK)
        held.add("ctrl")
        self.assertEqual(m.key_down(GRAVE), ("record", True))

    def test_a_failing_reader_falls_back_to_tracking(self):
        def broken():
            raise OSError("no user32")
        m = HotkeyMatcher(CLASSIC, mods_reader=broken)
        m.key_down(LCTRL)
        self.assertEqual(m.key_down(GRAVE), ("record", True))

    def test_alt_based_hotkey(self):
        self.m.set_bindings({"record": {"vk": 0x52, "mods": ["alt"]}, "play": CLASSIC["play"],
                             "stop": CLASSIC["stop"]})
        self.m.key_down(ALT)
        self.assertEqual(self.m.key_down(0x52), ("record", True))


class ExtraHotkeyTests(unittest.TestCase):
    def test_extra_fires_and_is_swallowed(self):
        m = hs.HotkeyMatcher(CLASSIC)
        self.assertTrue(m.set_extra("blackout", {"vk": 0x7B, "mods": ["ctrl"]}))   # Ctrl+F12
        m.key_down(0x11)
        self.assertEqual(m.key_down(0x7B), ("blackout", True))
        self.assertTrue(m.key_up(0x7B))
        self.assertEqual(m.extras, {"blackout": {"vk": 0x7B, "mods": ["ctrl"]}})

    def test_unset_broken_and_clashing(self):
        m = hs.HotkeyMatcher(CLASSIC)
        self.assertTrue(m.set_extra("blackout", None))
        self.assertEqual(m.key_down(0x7B), (None, False))
        self.assertFalse(m.set_extra("blackout", {"vk": "nope"}))
        self.assertFalse(m.set_extra("blackout", {"vk": 0x79, "mods": []}))     # F10 = Panic stop
        self.assertEqual(m.extras, {})
        self.assertEqual(m.key_down(0x79), ("stop", True))

    def test_main_hotkeys_win_when_rebound_onto_the_extra(self):
        m = hs.HotkeyMatcher(CLASSIC)
        m.set_extra("blackout", {"vk": 0x7B, "mods": []})
        m.set_bindings(dict(CLASSIC, stop={"vk": 0x7B, "mods": []}))
        self.assertEqual(m.extras, {})

    def test_injected_never_fires(self):
        m = hs.HotkeyMatcher(CLASSIC)
        m.set_extra("blackout", {"vk": 0x7B, "mods": []})
        self.assertEqual(m.key_down(0x7B, injected=True), (None, False))


if __name__ == "__main__":
    unittest.main()
