"""Laptop screen brightness for the blackout (brightness 0 while blacked out, back afterwards).

    dimmer = Dimmer()          # one per app
    dimmer.recover()           # at start-up: put back a brightness a crash left at 0
    dimmer.dim()               # remember the current level, go to 0       (returns at once)
    dimmer.restore()           # back to the remembered level                (returns at once)
    dimmer.close(timeout=3)    # at exit: finish what's queued (restores first)

How: Windows' own WMI brightness class, through a hidden PowerShell (the same thing the
brightness slider uses). It only exists for built-in laptop screens; on a desktop monitor it
simply does nothing and the black cover does the work.

Every change runs on one background thread, in order (PowerShell takes a moment to start, and the
Tk window must never wait for it). Before dimming, the old level is written to a small file in
%LOCALAPPDATA%\\Wackro, so if the app is killed while dimmed, the next start puts it back.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from config import LOG_DIR

log = logging.getLogger(__name__)

RESTORE_FILE = LOG_DIR / "brightness_restore.json"
_TIMEOUT = 10
_CREATE_NO_WINDOW = 0x08000000

_GET = ("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop "
        "| Select-Object -First 1).CurrentBrightness")
_SET = ("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop "
        "| Invoke-CimMethod -MethodName WmiSetBrightness "
        "-Arguments @{{Timeout=[uint32]1; Brightness=[byte]{level}}} | Out-Null")


def _powershell(command: str) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        # stdin too: in the windowed .exe there's no console, and leaving stdin unset can fail
        # with "the handle is invalid".
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=_TIMEOUT,
        creationflags=_CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip()[:300] or f"powershell exited with {result.returncode}")
    return result.stdout


class WmiBrightness:
    """get() -> 0..100 or None; set(level) -> bool. Never raises."""

    def __init__(self, runner: Callable[[str], str] = _powershell):
        self._run = runner
        self.available = sys.platform.startswith("win")

    def get(self) -> Optional[int]:
        if not self.available:
            return None
        try:
            text = self._run(_GET).strip()
            level = int(text.splitlines()[-1]) if text else None
        except Exception:
            log.info("Screen brightness can't be read here (not a laptop screen?)", exc_info=True)
            self.available = False
            return None
        return level if level is not None and 0 <= level <= 100 else None

    def set(self, level: int) -> bool:
        if not self.available:
            return False
        try:
            self._run(_SET.format(level=max(0, min(100, int(level)))))
            return True
        except Exception:
            log.warning("Could not set the screen brightness", exc_info=True)
            return False


class Dimmer:
    """See the module docstring. control: something with get() / set(level) (tests pass a fake)."""

    def __init__(self, control=None, restore_file: Path = RESTORE_FILE, threaded: bool = True):
        self._control = control if control is not None else WmiBrightness()
        self._file = Path(restore_file)
        self._saved: Optional[int] = None
        self._dimmed = False
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._threaded = threaded
        self._thread: Optional[threading.Thread] = None
        self._idle = threading.Event()
        self._idle.set()

    @property
    def dimmed(self) -> bool:
        return self._dimmed

    # ---- public (any thread; return at once) ----

    def recover(self) -> None:
        self._submit(self._do_recover)

    def dim(self) -> None:
        if not self._dimmed:
            self._dimmed = True
            self._submit(self._do_dim)

    def restore(self) -> None:
        if self._dimmed:
            self._dimmed = False
            self._submit(self._do_restore)

    def close(self, timeout: float = 3.0) -> None:
        """At exit: restore if dimmed and wait (up to timeout) for the queue to finish."""
        self.restore()
        self._idle.wait(timeout)

    # ---- worker ----

    def _submit(self, job) -> None:
        if not self._threaded:
            self._safe(job)
            return
        self._idle.clear()
        self._queue.put(job)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._work, name="Wackro-brightness", daemon=True)
            self._thread.start()

    def _work(self) -> None:
        while True:
            try:
                job = self._queue.get(timeout=2.0)
            except queue.Empty:
                if self._queue.empty():
                    self._idle.set()
                    return
                continue
            self._safe(job)
            if self._queue.empty():
                self._idle.set()

    @staticmethod
    def _safe(job) -> None:
        try:
            job()
        except Exception:
            log.exception("Brightness change failed")

    def _do_dim(self) -> None:
        level = self._control.get()
        if level is None:
            return                              # nothing to dim here (no laptop screen)
        if level == 0 and self._saved is not None:
            return
        self._saved = level
        self._write(level)
        self._control.set(0)

    def _do_restore(self) -> None:
        if self._saved is None:
            return
        if self._control.set(self._saved):
            self._saved = None
            self._delete()

    def _do_recover(self) -> None:
        level = self._read()
        if level is None:
            return
        log.info("Restoring the screen brightness (%d%%) left dimmed by an earlier run", level)
        if self._control.set(level):
            self._delete()

    # ---- the restore note ----

    def _write(self, level: int) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({"level": int(level)}), encoding="utf-8")
        except OSError:
            log.warning("Could not save the brightness to restore", exc_info=True)

    def _read(self) -> Optional[int]:
        try:
            level = json.loads(self._file.read_text(encoding="utf-8")).get("level")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, AttributeError):
            self._delete()
            return None
        if isinstance(level, int) and not isinstance(level, bool) and 1 <= level <= 100:
            return level
        self._delete()
        return None

    def _delete(self) -> None:
        try:
            self._file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.debug("Could not remove the brightness note", exc_info=True)
