"""Makes sure only one copy of Wackro runs at a time.

Two copies would both register the global hotkeys, so one F9 press would start two recordings.

    guard = SingleInstance()
    if not guard.acquire():
        ...                      # another copy is already running: tell the user and quit
    try:
        ...                      # run the app
    finally:
        guard.release()

How it works:

* Windows: a named mutex (Local\\Wackro, so it is per logon session). The OS drops it when the
  process dies, however it dies. If an elevated copy (Wackro-Admin.exe) owns it, a normal copy
  gets "access denied" instead of "already exists"; that counts as already running too.
* Everywhere else: an exclusive flock() on a lock file in LOG_DIR. Also dropped by the OS when the
  process dies. This path exists mainly so the guard can be tested on non-Windows machines.

If the lock mechanism itself is broken (no write access to the folder, no fcntl, ...), acquire()
returns True and logs a warning. A broken guard must never stop the app from starting.

ctypes / fcntl are imported lazily, so importing this module is safe on every platform.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from config import APP_NAME, LOG_DIR

log = logging.getLogger(__name__)

_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5


def _load_kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


class SingleInstance:
    def __init__(
        self,
        name: str = APP_NAME,
        lock_dir: Optional[Path] = None,
        kernel32=None,
        use_windows: Optional[bool] = None,
        last_error=None,
    ):
        """lock_dir, kernel32, use_windows and last_error exist so tests can point the lock
        somewhere else and fake the Windows API (last_error: callable returning GetLastError)."""
        self._name = name
        self._lock_path = Path(lock_dir if lock_dir is not None else LOG_DIR) / f"{name.lower()}.lock"
        self._kernel32 = kernel32
        self._last_error = last_error
        self._use_windows = sys.platform.startswith("win") if use_windows is None else use_windows
        self._handle = None      # mutex handle (Windows)
        self._file = None        # open lock file (elsewhere)
        self._held = False

    @property
    def held(self) -> bool:
        """True if this object currently holds the lock."""
        return self._held

    def acquire(self) -> bool:
        """True if we are the only copy (or the guard couldn't be set up). False if another copy runs."""
        if self._held:
            return True
        try:
            if self._use_windows:
                return self._acquire_windows()
            return self._acquire_posix()
        except Exception:
            log.warning("Single-instance check failed; starting anyway", exc_info=True)
            return True

    def release(self) -> None:
        """Let go of the lock. Safe to call any number of times, and without acquire()."""
        handle, self._handle = self._handle, None
        lock_file, self._file = self._file, None
        self._held = False
        if handle is not None:
            try:
                self._kernel32.CloseHandle(handle)
            except Exception:
                log.debug("Could not close the mutex handle", exc_info=True)
        if lock_file is not None:
            try:
                lock_file.close()  # closing the file drops the flock
            except Exception:
                log.debug("Could not close the lock file", exc_info=True)

    # ---- Windows ------------------------------------------------------------------------

    def _acquire_windows(self) -> bool:
        import ctypes

        if self._kernel32 is None:
            self._kernel32 = _load_kernel32()
        kernel32 = self._kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]

        get_last_error = self._last_error
        if get_last_error is None:
            ctypes.set_last_error(0)  # these two exist on Windows only
            get_last_error = ctypes.get_last_error
        handle = kernel32.CreateMutexW(None, False, f"Local\\{self._name}")
        error = get_last_error()
        if not handle:
            if error == _ERROR_ACCESS_DENIED:
                # The mutex exists but belongs to a process we may not open: an elevated
                # (administrator) copy while we are a normal one. That is still "already running".
                return False
            raise OSError(f"CreateMutexW failed (error {error})")
        if error == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        self._held = True
        return True

    # ---- Everywhere else ----------------------------------------------------------------

    def _acquire_posix(self) -> bool:
        import fcntl

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(self._lock_path, "a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return False
        except Exception:
            lock_file.close()
            raise
        self._file = lock_file
        self._held = True
        return True
