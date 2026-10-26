"""Sequences: several recordings played as one playback, in order, shuffled or picked at random.

A sequence file (.wseq) is JSON with a COPY of every recording inside, so one file has
everything and still works when moved or shared:

    {
      "kind": "wackro-sequence", "version": 2,
      "name": "Daily report",
      "order": "shuffle",                in_order / shuffle / random_picks
      "same_shuffle": false,             shuffle: keep one shuffled order for every round
      "picks": 3,                        random_picks: random steps per round (1 or more)
      "rounds": 3,                       0 = until stopped
      "round_wait": {"min": 2, "max": 5},  seconds before each round after the first
      "steps": [
        {"name": "Login", "enabled": true, "pin": "first", "repeat": 1,
         "wait": {"min": 1, "max": 3},  seconds after this step (random in the range)
         "recording": { a whole recording, see storage.py }},
        ...
      ]
    }

How a round is put together:
    1. Steps pinned "first" (in list order).
    2. The "anywhere" steps (pin "none"), by the order setting:
         in_order      as listed
         shuffle       every one once, in a random order (a new one each round, unless
                       same_shuffle: then one order for the whole playback)
         random_picks  `picks` random steps, the same one can come up more than once
    3. Steps pinned "last" (in list order).
Pinned steps ALWAYS play, every round. Switched-off steps never play. Each step plays `repeat`
times in a row, then pauses its `wait`.

Version 1 files (with "locked" steps and the old order names) are upgraded when opened: a locked
step in the top half of the list becomes "first", in the bottom half "last".

    runner = Runner(sequence)
    events = runner.round(1)               # events of round 1 (one long recording)
    runner.round(2) ...                    # called by the player before each round
    runner.step_at(run, seconds)           # (number, count, name) of the step playing there
    describe(sequence) / preview(sequence) # plain-English explanation / an example round
"""

from __future__ import annotations

import json
import math
import random
import threading
from pathlib import Path
from typing import Optional

from config import MAX_EVENTS, MAX_FILE_BYTES, MAX_RECORDING_SECONDS, MAX_REPEAT
from storage import RecordingError, read_json_file, validate_recording, write_text_atomic

KIND = "wackro-sequence"
VERSION = 2
SEQUENCE_EXTENSION = ".wseq"
ORDERS = ("in_order", "shuffle", "random_picks")
ORDER_LABELS = {"in_order": "In order", "shuffle": "Shuffle", "random_picks": "Pick at random"}
PINS = ("none", "first", "last")
PIN_LABELS = {"none": "Anywhere", "first": "Always first", "last": "Always last"}
MAX_STEPS = 200
MAX_STEP_REPEAT = 1000
MAX_WAIT = 24 * 60 * 60
MAX_NAME = 80
MAX_PICKS = 1000
MAX_SEQUENCE_BYTES = 4 * MAX_FILE_BYTES
REPEAT_GAP = 0.05                    # seconds between two repeats of the same step

_TOP = ("kind", "version", "name", "order", "same_shuffle", "picks", "rounds", "round_wait", "steps")
_STEP = ("name", "enabled", "pin", "repeat", "wait", "recording")
_TOP_V1 = ("kind", "version", "name", "order", "picks", "rounds", "round_wait", "steps")
_STEP_V1 = ("name", "enabled", "locked", "repeat", "wait", "recording")
_ORDERS_V1 = ("in_order", "shuffle_round", "shuffle_once", "random_picks")


# ---- Building ---------------------------------------------------------------------------------

def new_sequence(name: str = "New sequence") -> dict:
    return {"kind": KIND, "version": VERSION, "name": name, "order": "in_order", "same_shuffle": False,
            "picks": 1, "rounds": 1, "round_wait": {"min": 0.0, "max": 0.0}, "steps": []}


def make_step(name: str, recording: dict) -> dict:
    """A step for `recording` (validated, copied)."""
    validate_recording(recording)
    return {"name": clean_name(name), "enabled": True, "pin": "none", "repeat": 1,
            "wait": {"min": 0.0, "max": 0.0}, "recording": json.loads(json.dumps(recording))}


def clean_name(name) -> str:
    text = " ".join(str(name).split())[:MAX_NAME]
    return text or "Step"


def name_from_path(path) -> str:
    return clean_name(Path(path).stem.replace("_", " "))


# ---- Waits ------------------------------------------------------------------------------------

WAIT_ERROR = f"A pause is seconds (like 2) or a random range (like 1-5), up to {MAX_WAIT:,}."


def parse_wait(text: str) -> dict:
    """'' -> 0, '2' -> 2 s, '1-5' / '1–5' / '1 to 5' -> random 1..5 s. Raises ValueError."""
    cleaned = str(text).strip().lower().replace(",", ".").replace("–", "-").replace("—", "-")
    cleaned = cleaned.replace(" to ", "-").replace(" ", "")
    for unit in ("seconds", "second", "secs", "sec", "s"):
        cleaned = cleaned.replace(unit, "")
    if not cleaned:
        return {"min": 0.0, "max": 0.0}
    parts = cleaned.split("-")
    if len(parts) > 2 or not all(parts):
        raise ValueError(WAIT_ERROR)
    try:
        values = [float(p) for p in parts]
    except ValueError:
        raise ValueError(WAIT_ERROR) from None
    if not all(math.isfinite(v) and 0 <= v <= MAX_WAIT for v in values):
        raise ValueError(WAIT_ERROR)
    return {"min": min(values), "max": max(values)}


def format_wait(wait: dict) -> str:
    """'' for no pause, '2', or '1-5'."""
    low, high = wait["min"], wait["max"]
    if low == high:
        return "" if low == 0 else f"{low:g}"
    return f"{low:g}-{high:g}"


def wait_words(wait: dict) -> str:
    """'2 s' / '1–5 s' / '' (none)."""
    low, high = wait["min"], wait["max"]
    if high <= 0:
        return ""
    return f"{low:g} s" if low == high else f"{low:g}–{high:g} s"


def draw_wait(wait: dict, rng) -> float:
    low, high = wait["min"], wait["max"]
    return low if high <= low else rng.uniform(low, high)


# ---- Checking ---------------------------------------------------------------------------------

def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _wait_problem(where: str, wait) -> Optional[str]:
    if not isinstance(wait, dict) or set(wait) != {"min", "max"}:
        return f"{where} must be {{\"min\": seconds, \"max\": seconds}}"
    for value in wait.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                or not 0 <= value <= MAX_WAIT:
            return f"{where} must be from 0 to {MAX_WAIT:,} seconds"
    if wait["min"] > wait["max"]:
        return f"{where}: min is more than max"
    return None


def _check_fields(where: str, data: dict, fields: tuple) -> None:
    for key in data:
        if key not in fields:
            raise RecordingError(f"Unexpected field {str(key)[:40]!r} in {where}.")
    for key in fields:
        if key not in data:
            raise RecordingError(f"{where[0].upper() + where[1:]} is missing {key!r}.")


def _check_common(data: dict) -> None:
    if not isinstance(data["name"], str) or len(data["name"]) > MAX_NAME:
        raise RecordingError(f"The sequence name must be text, up to {MAX_NAME} characters.")
    if not _is_int(data["rounds"]) or not 0 <= data["rounds"] <= MAX_REPEAT:
        raise RecordingError(f"'rounds' must be a whole number from 0 to {MAX_REPEAT:,}.")
    problem = _wait_problem("'round_wait'", data["round_wait"])
    if problem:
        raise RecordingError(problem + ".")
    if not isinstance(data["steps"], list) or len(data["steps"]) > MAX_STEPS:
        raise RecordingError(f"'steps' must be a list of up to {MAX_STEPS} steps.")


def _check_step_common(where: str, step: dict) -> None:
    if not isinstance(step["name"], str) or not step["name"].strip() or len(step["name"]) > MAX_NAME:
        raise RecordingError(f"{where}: the name must be text, 1 to {MAX_NAME} characters.")
    if not isinstance(step["enabled"], bool):
        raise RecordingError(f"{where}: 'enabled' must be true or false.")
    if not _is_int(step["repeat"]) or not 1 <= step["repeat"] <= MAX_STEP_REPEAT:
        raise RecordingError(f"{where}: 'repeat' must be from 1 to {MAX_STEP_REPEAT:,}.")
    problem = _wait_problem(f"{where}: 'wait'", step["wait"])
    if problem:
        raise RecordingError(problem + ".")
    try:
        validate_recording(step["recording"])
    except RecordingError as error:
        raise RecordingError(f"{where} ({step['name'][:30]}): {error}") from None


def _check_kind_and_version(data) -> int:
    if not isinstance(data, dict) or data.get("kind") != KIND:
        raise RecordingError("This isn't a Wackro sequence.")
    version = data.get("version")
    if not _is_int(version) or version < 1:
        raise RecordingError("This sequence has an unsupported 'version'.")
    if version > VERSION:
        raise RecordingError("This sequence was made by a newer version of Wackro. Update the app to open it.")
    return version


def validate_sequence(data) -> dict:
    """Check a version-2 sequence. Returns it, or raises RecordingError. (Use upgrade() for
    anything read from a file: it also accepts version 1.)"""
    version = _check_kind_and_version(data)
    if version != VERSION:
        raise RecordingError("This sequence is in the old format; open it with upgrade().")
    _check_fields("the sequence", data, _TOP)
    _check_common(data)
    if data["order"] not in ORDERS:
        raise RecordingError("The sequence has an unknown 'order'.")
    if not isinstance(data["same_shuffle"], bool):
        raise RecordingError("'same_shuffle' must be true or false.")
    if not _is_int(data["picks"]) or not 1 <= data["picks"] <= MAX_PICKS:
        raise RecordingError(f"'picks' must be a whole number from 1 to {MAX_PICKS:,}.")
    for number, step in enumerate(data["steps"], start=1):
        where = f"Step {number}"
        if not isinstance(step, dict):
            raise RecordingError(f"{where} must be an object.")
        _check_fields(f"step {number}", step, _STEP)
        if step["pin"] not in PINS:
            raise RecordingError(f"{where}: 'pin' must be none, first or last.")
        _check_step_common(where, step)
    return data


def upgrade(data) -> dict:
    """Any supported sequence (version 1 or 2) as a checked version-2 sequence. Version 1 is
    converted: shuffle_round / shuffle_once -> shuffle (+ same_shuffle), picks 0 -> one per
    unlocked step, locked steps -> pinned first (top half of the list) or last (bottom half)."""
    version = _check_kind_and_version(data)
    if version == VERSION:
        return validate_sequence(data)
    _check_fields("the sequence", data, _TOP_V1)
    _check_common(data)
    if data["order"] not in _ORDERS_V1:
        raise RecordingError("The sequence has an unknown 'order'.")
    if not _is_int(data["picks"]) or not 0 <= data["picks"] <= MAX_PICKS:
        raise RecordingError(f"'picks' must be a whole number from 0 to {MAX_PICKS:,}.")
    steps = []
    count = len(data["steps"])
    for number, step in enumerate(data["steps"], start=1):
        where = f"Step {number}"
        if not isinstance(step, dict):
            raise RecordingError(f"{where} must be an object.")
        _check_fields(f"step {number}", step, _STEP_V1)
        if not isinstance(step["locked"], bool):
            raise RecordingError(f"{where}: 'locked' must be true or false.")
        _check_step_common(where, step)
        pin = "none"
        if step["locked"]:
            pin = "first" if (number - 1) < count / 2 else "last"
        new = {key: step[key] for key in ("name", "enabled", "repeat", "wait", "recording")}
        new["pin"] = pin
        steps.append(new)
    order = data["order"]
    unlocked = sum(1 for s in steps if s["enabled"] and s["pin"] == "none")
    result = {
        "kind": KIND, "version": VERSION, "name": data["name"],
        "order": "shuffle" if order in ("shuffle_round", "shuffle_once") else order,
        "same_shuffle": order == "shuffle_once",
        "picks": data["picks"] or max(1, unlocked),
        "rounds": data["rounds"], "round_wait": data["round_wait"], "steps": steps,
    }
    return validate_sequence(result)


def save_sequence(path, sequence: dict) -> None:
    validate_sequence(sequence)
    write_text_atomic(path, json.dumps(sequence, allow_nan=False, separators=(",", ":")) + "\n")


def load_sequence(path) -> dict:
    return upgrade(read_json_file(path, MAX_SEQUENCE_BYTES, "a sequence"))


def looks_like_sequence(data) -> bool:
    return isinstance(data, dict) and data.get("kind") == KIND


def load_any(path) -> tuple:
    """("recording" | "sequence", data) from either kind of file."""
    data = read_json_file(path, MAX_SEQUENCE_BYTES, "a recording or sequence")
    if looks_like_sequence(data):
        return "sequence", upgrade(data)
    from storage import MAX_FILE_BYTES as recording_limit
    if Path(path).stat().st_size > recording_limit:
        raise RecordingError("That file is too big to be a recording.")
    return "recording", validate_recording(data)


# ---- Summaries --------------------------------------------------------------------------------

def enabled_steps(sequence: dict) -> list:
    return [i for i, step in enumerate(sequence["steps"]) if step["enabled"]]


def _groups(sequence: dict) -> tuple:
    """(pinned first, anywhere, pinned last) indexes of the switched-on steps, in list order."""
    steps = sequence["steps"]
    on = enabled_steps(sequence)
    return ([i for i in on if steps[i]["pin"] == "first"],
            [i for i in on if steps[i]["pin"] == "none"],
            [i for i in on if steps[i]["pin"] == "last"])


def programs_used(sequence: dict) -> list:
    """Every event of every switched-on step (for the pre-flight check)."""
    events = []
    for i in enabled_steps(sequence):
        events.extend(sequence["steps"][i]["recording"]["events"])
    return events


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def _rounds_words(rounds: int) -> str:
    return "until you stop it" if rounds == 0 else _plural(rounds, "round")


def summary(sequence: dict) -> str:
    """'4 steps · Shuffle · 3 rounds' for the main window."""
    on = len(enabled_steps(sequence))
    total = len(sequence["steps"])
    steps = _plural(on, "step") + (f" (of {total})" if on != total else "")
    rounds = "∞ rounds" if sequence["rounds"] == 0 else _plural(sequence["rounds"], "round")
    return f"{steps} · {ORDER_LABELS[sequence['order']]} · {rounds}"


def _names(sequence: dict, indexes: list) -> str:
    names = [sequence["steps"][i]["name"] for i in indexes]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def describe(sequence: dict) -> str:
    """The whole sequence in one or two plain sentences, e.g.
    'Each round: Login first, then 2 random steps from Fill form and Export (the same one can
    come up twice), then Logout last. 3 rounds, 2–5 s apart.'"""
    first, middle, last = _groups(sequence)
    if not first and not middle and not last:
        return "Every step is switched off, so there's nothing to play."
    parts = []
    if first:
        parts.append(f"{_names(sequence, first)} first")
    order = sequence["order"]
    if middle:
        some = _names(sequence, middle) if len(middle) <= 4 else _plural(len(middle), "other step")
        if order == "in_order":
            parts.append(some + ("" if len(middle) == 1 else " in list order"))
        elif order == "shuffle":
            if len(middle) == 1:
                parts.append(some)
            else:
                again = "the same order every round" if sequence["same_shuffle"] else "a new order every round"
                parts.append(f"{some} shuffled ({again})")
        else:
            picks = sequence["picks"]
            repeat = " (the same one can come up more than once)" if len(middle) > 1 and picks > 1 else ""
            parts.append(f"{_plural(picks, 'random step')} from {some}{repeat}")
    if last:
        parts.append(f"{_names(sequence, last)} last")
    text = "Each round: " + ", then ".join(parts) + "."
    rounds = sequence["rounds"]
    pause = wait_words(sequence["round_wait"])
    if rounds == 1:
        text += " Plays 1 round."
    else:
        text += f" Repeats {_rounds_words(rounds)}" + (f", {pause} between rounds." if pause else ".")
    return text


def warnings(sequence: dict) -> list:
    """Things worth pointing out before playing (plain sentences)."""
    first, middle, last = _groups(sequence)
    found = []
    if not (first or middle or last):
        found.append("Every step is switched off, so there's nothing to play.")
    elif sequence["order"] == "random_picks" and not middle:
        found.append("Every switched-on step is pinned, so there's nothing to pick at random: "
                     "only the pinned steps play.")
    elif sequence["order"] == "shuffle" and len(middle) == 1:
        found.append("Only one step can move, so shuffling changes nothing.")
    return found


def preview(sequence: dict, rng=None) -> str:
    """An example round: 'Login → Export ×3 → Fill form → Logout'."""
    order = plan_order(sequence, rng if rng is not None else random.Random())
    if not order:
        return "(nothing to play)"
    names = []
    for i in order:
        step = sequence["steps"][i]
        names.append(step["name"] + (f" ×{step['repeat']}" if step["repeat"] > 1 else ""))
    text = " → ".join(names)
    return text if len(text) <= 300 else text[:297] + "…"


# ---- Planning a round -------------------------------------------------------------------------

def plan_order(sequence: dict, rng, middle_order: Optional[list] = None) -> list:
    """Step indexes for one round: pinned-first steps, the anywhere steps by the order setting,
    pinned-last steps. middle_order: a fixed order for the anywhere steps (same_shuffle)."""
    first, middle, last = _groups(sequence)
    order = sequence["order"]
    if middle_order is not None:
        middle = list(middle_order)
    elif order == "shuffle":
        middle = list(middle)
        rng.shuffle(middle)
    elif order == "random_picks":
        middle = [rng.choice(middle) for _ in range(sequence["picks"])] if middle else []
    return first + middle + last


def build_round(sequence: dict, order: list, rng, lead: float = 0.0) -> tuple:
    """(events, segments) for one round. segments: [(start_t, end_t, step_index)] per step."""
    events, segments = [], []
    t = float(lead)
    for index in order:
        step = sequence["steps"][index]
        start = t
        recording_events = step["recording"]["events"]
        for repeat in range(step["repeat"]):
            base = t
            for event in recording_events:
                events.append(dict(event, t=round(base + event["t"], 4)))
            t = base + (recording_events[-1]["t"] if recording_events else 0.0)
            if repeat < step["repeat"] - 1:
                t += REPEAT_GAP
            if len(events) > MAX_EVENTS:
                raise RecordingError(
                    f"One round of this sequence is more than {MAX_EVENTS:,} events. "
                    "Lower some Times or the random picks.")
        segments.append((start, t, index))
        t += draw_wait(step["wait"], rng)
        if t > MAX_RECORDING_SECONDS:
            raise RecordingError("One round of this sequence is longer than 24 hours. "
                                 "Shorten some pauses or Times.")
    return events, segments


class Runner:
    """Makes each round's events for the player (thread-safe: the player asks from its thread)."""

    def __init__(self, sequence: dict, rng=None):
        self.sequence = validate_sequence(sequence)
        self._rng = rng if rng is not None else random.Random()
        self._lock = threading.Lock()
        self._same: Optional[list] = None      # the kept shuffle (same_shuffle)
        self._segments: dict = {}               # run number -> segments (the last few)

    def round(self, number: int) -> list:
        with self._lock:
            s = self.sequence
            if s["order"] == "shuffle" and s["same_shuffle"]:
                if self._same is None:
                    first, middle, last = _groups(s)
                    self._same = list(middle)
                    self._rng.shuffle(self._same)
                order = plan_order(s, self._rng, middle_order=self._same)
            else:
                order = plan_order(s, self._rng)
            if not order:
                raise RecordingError("Every step of this sequence is switched off.")
            lead = draw_wait(s["round_wait"], self._rng) if number > 1 else 0.0
            events, segments = build_round(s, order, self._rng, lead)
            if not events:
                raise RecordingError("The steps of this sequence have nothing to play.")
            self._segments[number] = segments
            for old in [n for n in self._segments if n < number - 3]:
                del self._segments[old]
            return events

    def step_at(self, number: int, position: float) -> Optional[tuple]:
        """(step number in the round, steps in the round, step name) at `position` seconds into
        round `number`, or None."""
        with self._lock:
            segments = self._segments.get(number)
        if not segments:
            return None
        chosen = 0
        for slot, (start, end, index) in enumerate(segments):
            if position >= start:
                chosen = slot
        index = segments[chosen][2]
        return chosen + 1, len(segments), self.sequence["steps"][index]["name"]
