"""Hotkey bindings: how they are stored, shown and matched. Pure Python, no pynput / tkinter.

A binding is a small dict, stored as-is in settings.json:

    {"mods": ["ctrl"], "vk": 192}      Ctrl+`
    {"mods": [], "vk": 144}            NumLock
    {"mods": [], "vk": 121}            F10

"vk" is the Windows virtual-key code of the main key and "mods" the modifiers that must be held
(any of "ctrl", "alt", "shift", "win"), so a binding means the same physical keys whatever the
keyboard layout does with the character (with Ctrl held, ` doesn't even produce a character).

There are three actions:

    record  Record / Stop    idle -> start recording, recording -> stop
    play    Play / Stop      idle -> play, playing -> stop
    stop    Panic stop       stops whatever is running

HotkeyMatcher follows the keyboard (key down / key up, with an "injected" flag for keys that a
program such as our own player sent) and says which action fired and whether the key should be
swallowed. Only the main key of a binding is swallowed, never the modifiers, so Ctrl itself still
works normally everywhere.
"""

from __future__ import annotations

import string
from typing import Optional

ACTIONS = ("record", "play", "stop")
ACTION_LABELS = {"record": "Record / Stop", "play": "Play / Stop", "stop": "Panic stop"}
# Optional extra hotkeys: no key by default, None = not set. Stored in settings next to "hotkeys".
EXTRA_ACTIONS = ("blackout",)
EXTRA_LABELS = {"blackout": "Black out screen"}

DEFAULT_BINDINGS = {
    "record": {"mods": [], "vk": 0x78},         # F9
    "play": {"mods": [], "vk": 0x7A},           # F11
    "stop": {"mods": [], "vk": 0x79},           # F10
}

MODIFIERS = ("ctrl", "alt", "shift", "win")            # also the display order
MODIFIER_LABELS = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}

# Windows virtual-key codes of the modifier keys (generic, left and right).
MODIFIER_VKS = {
    0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
    0x10: "shift", 0xA0: "shift", 0xA1: "shift",
    0x12: "alt", 0xA4: "alt", 0xA5: "alt",
    0x5B: "win", 0x5C: "win",
}

# pynput special-key names of the modifiers (as they appear in recordings).
MODIFIER_KEY_NAMES = {
    "ctrl": frozenset({"ctrl", "ctrl_l", "ctrl_r"}),
    "shift": frozenset({"shift", "shift_l", "shift_r"}),
    "alt": frozenset({"alt", "alt_l", "alt_r", "alt_gr"}),
    "win": frozenset({"cmd", "cmd_l", "cmd_r"}),
}

# Keys that can't be a hotkey: Esc cancels the "press a key" box, and the mouse buttons
# (VK 1, 2, 4, 5, 6) aren't keys.
_FORBIDDEN_VKS = frozenset({0x1B, 0x01, 0x02, 0x04, 0x05, 0x06})

# ---- Names ------------------------------------------------------------------------------

_VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x14: "CapsLock",
    0x20: "Space", 0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down", 0x2C: "PrintScreen",
    0x2D: "Insert", 0x2E: "Delete", 0x5D: "Menu",
    0x6A: "Num *", 0x6B: "Num +", 0x6D: "Num -", 0x6E: "Num .", 0x6F: "Num /",
    0x90: "NumLock", 0x91: "ScrollLock",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
    0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
    0xAD: "Mute", 0xAE: "Volume Down", 0xAF: "Volume Up",
    0xB0: "Next Track", 0xB1: "Previous Track", 0xB2: "Stop Media", 0xB3: "Play/Pause",
}
for _i in range(24):
    _VK_NAMES[0x70 + _i] = f"F{_i + 1}"
for _i in range(10):
    _VK_NAMES[0x30 + _i] = str(_i)
    _VK_NAMES[0x60 + _i] = f"Num {_i}"
for _c in string.ascii_uppercase:
    _VK_NAMES[ord(_c)] = _c

# pynput Key names -> Windows virtual-key codes. Used to find the vk of a key on systems where
# pynput doesn't give us Windows codes (Linux / macOS), and for special keys everywhere.
KEY_NAME_VKS = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "pause": 0x13, "caps_lock": 0x14,
    "esc": 0x1B, "space": 0x20, "page_up": 0x21, "page_down": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "print_screen": 0x2C, "insert": 0x2D, "delete": 0x2E, "menu": 0x5D,
    "num_lock": 0x90, "scroll_lock": 0x91,
    "shift": 0x10, "shift_l": 0xA0, "shift_r": 0xA1,
    "ctrl": 0x11, "ctrl_l": 0xA2, "ctrl_r": 0xA3,
    "alt": 0x12, "alt_l": 0xA4, "alt_r": 0xA5, "alt_gr": 0xA5,
    "cmd": 0x5B, "cmd_l": 0x5B, "cmd_r": 0x5C,
    "media_volume_mute": 0xAD, "media_volume_down": 0xAE, "media_volume_up": 0xAF,
    "media_next": 0xB0, "media_previous": 0xB1, "media_stop": 0xB2, "media_play_pause": 0xB3,
}
for _i in range(24):
    KEY_NAME_VKS[f"f{_i + 1}"] = 0x70 + _i

# Characters -> Windows virtual-key codes (US layout), for systems without Windows codes.
_CHAR_VKS = {
    "`": 0xC0, "~": 0xC0, "-": 0xBD, "_": 0xBD, "=": 0xBB, "+": 0xBB, "[": 0xDB, "{": 0xDB,
    "]": 0xDD, "}": 0xDD, "\\": 0xDC, "|": 0xDC, ";": 0xBA, ":": 0xBA, "'": 0xDE, '"': 0xDE,
    ",": 0xBC, "<": 0xBC, ".": 0xBE, ">": 0xBE, "/": 0xBF, "?": 0xBF, " ": 0x20,
}
for _c in string.ascii_letters:
    _CHAR_VKS[_c] = ord(_c.upper())
for _i, _c in enumerate(")!@#$%^&*("):
    _CHAR_VKS[_c] = 0x30 + _i
for _i in range(10):
    _CHAR_VKS[str(_i)] = 0x30 + _i

# Tk keysym names (from the "press a key" box on non-Windows systems) -> vk.
_KEYSYM_VKS = {
    "BackSpace": 0x08, "Tab": 0x09, "Return": 0x0D, "Pause": 0x13, "Caps_Lock": 0x14,
    "Escape": 0x1B, "space": 0x20, "Prior": 0x21, "Next": 0x22, "End": 0x23, "Home": 0x24,
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28, "Print": 0x2C, "Insert": 0x2D,
    "Delete": 0x2E, "Num_Lock": 0x90, "Scroll_Lock": 0x91, "grave": 0xC0, "asciitilde": 0xC0,
    "minus": 0xBD, "equal": 0xBB, "bracketleft": 0xDB, "bracketright": 0xDD, "backslash": 0xDC,
    "semicolon": 0xBA, "apostrophe": 0xDE, "comma": 0xBC, "period": 0xBE, "slash": 0xBF,
    "KP_Multiply": 0x6A, "KP_Add": 0x6B, "KP_Subtract": 0x6D, "KP_Decimal": 0x6E,
    "KP_Divide": 0x6F,
    "Shift_L": 0xA0, "Shift_R": 0xA1, "Control_L": 0xA2, "Control_R": 0xA3,
    "Alt_L": 0xA4, "Alt_R": 0xA5, "Super_L": 0x5B, "Super_R": 0x5C,
}
for _i in range(24):
    _KEYSYM_VKS[f"F{_i + 1}"] = 0x70 + _i
for _i in range(10):
    _KEYSYM_VKS[f"KP_{_i}"] = 0x60 + _i


def vk_name(vk: int) -> str:
    return _VK_NAMES.get(vk, f"Key {vk:#04x}")


def label(binding: Optional[dict]) -> str:
    """{"mods": ["ctrl"], "vk": 192} -> "Ctrl+`".  None -> "(none)"."""
    if not binding:
        return "(none)"
    parts = [MODIFIER_LABELS[m] for m in MODIFIERS if m in binding["mods"]]
    parts.append(vk_name(binding["vk"]))
    return "+".join(parts)


# ---- Validation -------------------------------------------------------------------------

def make_binding(vk: int, mods) -> dict:
    """A binding in its canonical form (modifiers de-duplicated and in MODIFIERS order)."""
    mods = set(mods)
    return {"mods": [m for m in MODIFIERS if m in mods], "vk": int(vk)}


def binding_problem(vk, mods) -> Optional[str]:
    """Why (vk, mods) can't be a hotkey, as a sentence for the GUI, or None if it's fine."""
    if isinstance(vk, bool) or not isinstance(vk, int) or not 1 <= vk <= 0xFE:
        return "That key can't be used."
    if vk in MODIFIER_VKS:
        return "Press a key together with the modifier (Ctrl, Alt, Shift or Win alone can't be a hotkey)."
    if vk in _FORBIDDEN_VKS:
        return "Esc is reserved for cancelling. Pick another key."
    if any(m not in MODIFIERS for m in mods):
        return "Unknown modifier."
    return None


def sanitize_binding(data) -> Optional[dict]:
    """A binding read from settings.json, or None if it is unusable."""
    if not isinstance(data, dict):
        return None
    vk, mods = data.get("vk"), data.get("mods", [])
    if not isinstance(mods, list) or not all(isinstance(m, str) for m in mods):
        return None
    if binding_problem(vk, mods):
        return None
    return make_binding(vk, mods)


def same_keys(a: Optional[dict], b: Optional[dict]) -> bool:
    return bool(a) and bool(b) and a["vk"] == b["vk"] and set(a["mods"]) == set(b["mods"])


def sanitize_bindings(data) -> dict:
    """All three bindings from settings.json. Anything broken, or clashing with an action that
    comes earlier in ACTIONS, falls back to its default (or, if the default clashes too, to the
    original defaults for all three)."""
    data = data if isinstance(data, dict) else {}
    result = {}
    for action in ACTIONS:
        binding = sanitize_binding(data.get(action))
        if binding is None or any(same_keys(binding, other) for other in result.values()):
            binding = dict(DEFAULT_BINDINGS[action])
        result[action] = binding
    if len({(b["vk"], tuple(b["mods"])) for b in result.values()}) != len(ACTIONS):
        result = {a: dict(DEFAULT_BINDINGS[a]) for a in ACTIONS}
    return {a: make_binding(b["vk"], b["mods"]) for a, b in result.items()}


def find_clash(bindings: dict) -> Optional[tuple]:
    """(action_a, action_b) of the first two actions that share the same keys, else None."""
    for i, a in enumerate(ACTIONS):
        for b in ACTIONS[i + 1:]:
            if same_keys(bindings.get(a), bindings.get(b)):
                return (a, b)
    return None


# ---- Turning keys into vk codes ---------------------------------------------------------

def key_to_vk(key, windows: bool) -> Optional[int]:
    """The Windows vk of a pynput Key / KeyCode, or None.

    On Windows pynput gives real vk codes (even with Ctrl held, when there's no character).
    Elsewhere the vk is a platform code, so it's worked out from the key name or character.
    """
    name = getattr(key, "name", None)
    if name is not None:                       # keyboard.Key member
        if windows:
            value = getattr(key, "value", None)
            vk = getattr(value, "vk", None)
            if isinstance(vk, int):
                return vk
        return KEY_NAME_VKS.get(name)
    vk = getattr(key, "vk", None)
    if windows and isinstance(vk, int):
        return vk
    char = getattr(key, "char", None)
    if isinstance(char, str) and len(char) == 1:
        if char in _CHAR_VKS:
            return _CHAR_VKS[char]
        code = ord(char)
        if 1 <= code <= 26:                    # Ctrl+letter arrives as a control character
            return 0x40 + code
    return None


def keysym_to_vk(keysym: str, keycode: int, windows: bool) -> Optional[int]:
    """The vk of a Tk <KeyPress> event. On Windows Tk's keycode *is* the vk."""
    if windows:
        return keycode or None
    if keysym in _KEYSYM_VKS:
        return _KEYSYM_VKS[keysym]
    if len(keysym) == 1 and keysym in _CHAR_VKS:
        return _CHAR_VKS[keysym]
    return None


# Tk event.state bits for the modifiers.
_TK_STATE_WINDOWS = {"shift": 0x0001, "ctrl": 0x0004, "alt": 0x20000}
_TK_STATE_OTHER = {"shift": 0x0001, "ctrl": 0x0004, "alt": 0x0008, "win": 0x0040}


def tk_state_mods(state: int, windows: bool) -> set:
    table = _TK_STATE_WINDOWS if windows else _TK_STATE_OTHER
    return {mod for mod, bit in table.items() if state & bit}


# ---- Matching ---------------------------------------------------------------------------

class HotkeyMatcher:
    """Follows key downs / ups and decides what fires and what gets swallowed.

        action, swallow = matcher.key_down(vk, injected)
        swallow = matcher.key_up(vk, injected)

    * A binding fires when its main key goes down while exactly its modifiers are held.
    * Holding the key down (auto-repeat) fires once; the repeats and the key up are swallowed too,
      so the focused program never sees half a key press.
    * Injected keys (sent by a program, e.g. our player) never fire or get swallowed, so a
      recording that contains a hotkey can't stop itself.
    * Modifier keys are tracked but never swallowed.
    * While disabled (the hotkey settings box is open) nothing fires or gets swallowed.
    * mods_reader (optional): a function returning the modifiers really held right now. On
      Windows that comes from GetAsyncKeyState, because a keyboard hook never sees the key ups
      around Ctrl+Alt+Del / the lock screen: tracked state would think Ctrl is still down and plain
      NumLock would stop matching until Ctrl was tapped again. Without it, modifiers are tracked
      from the key downs / ups seen here.
    """

    def __init__(self, bindings: dict, mods_reader=None):
        self.enabled = True
        self._bindings = sanitize_bindings(bindings)
        self._mods_reader = mods_reader
        self._mods_down: dict = {}       # vk -> modifier name
        self._swallowing: set = set()    # main keys that fired and are still held down
        self._extras: dict = {}          # extra action -> binding (see set_extra)

    @property
    def bindings(self) -> dict:
        return {a: dict(b, mods=list(b["mods"])) for a, b in self._bindings.items()}

    def set_bindings(self, bindings: dict) -> None:
        self._bindings = sanitize_bindings(bindings)
        self._extras = {a: b for a, b in self._extras.items()
                        if not any(same_keys(b, main) for main in self._bindings.values())}

    @property
    def extras(self) -> dict:
        return {a: dict(b, mods=list(b["mods"])) for a, b in self._extras.items()}

    def set_extra(self, action: str, binding) -> bool:
        """Bind an optional extra action (EXTRA_ACTIONS), or unbind it with None. Refused (False)
        when broken or when it clashes with one of the main hotkeys."""
        clean = sanitize_binding(binding) if binding is not None else None
        if clean is None:
            self._extras.pop(action, None)
            return binding is None
        if any(same_keys(clean, main) for main in self._bindings.values()):
            self._extras.pop(action, None)
            return False
        self._extras[action] = make_binding(clean["vk"], clean["mods"])
        return True

    def held_modifiers(self) -> set:
        return set(self._mods_down.values())

    def key_down(self, vk: int, injected: bool = False) -> tuple:
        if vk in MODIFIER_VKS:
            if not injected:
                self._mods_down[vk] = MODIFIER_VKS[vk]
            return (None, False)
        if injected or not self.enabled:
            return (None, False)
        if vk in self._swallowing:                      # auto-repeat of a hotkey being held
            return (None, True)
        mods = self.held_modifiers()
        if self._mods_reader is not None:
            try:
                mods = set(self._mods_reader())
            except Exception:
                pass                                    # fall back to the tracked state
        for action in ACTIONS:
            binding = self._bindings[action]
            if binding["vk"] == vk and set(binding["mods"]) == mods:
                self._swallowing.add(vk)
                return (action, True)
        for action, binding in self._extras.items():
            if binding["vk"] == vk and set(binding["mods"]) == mods:
                self._swallowing.add(vk)
                return (action, True)
        return (None, False)

    def key_up(self, vk: int, injected: bool = False) -> bool:
        if vk in MODIFIER_VKS:
            if not injected:
                self._mods_down.pop(vk, None)
                # A generic VK_CONTROL up also means both sides are up.
                name = MODIFIER_VKS[vk]
                if vk in (0x10, 0x11, 0x12):
                    for other, other_name in list(self._mods_down.items()):
                        if other_name == name:
                            del self._mods_down[other]
            return False
        if injected:
            return False
        if vk in self._swallowing:
            self._swallowing.discard(vk)
            return True
        return False

    def reset(self) -> None:
        """Forget which keys are held (e.g. after the hotkeys were off for a while)."""
        self._mods_down.clear()
        self._swallowing.clear()
