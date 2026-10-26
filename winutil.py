"""Small Windows helpers (user32 through ctypes) for the overlay and the dialogs.

    win = get_win32()                 # the real thing on Windows, a harmless stand-in elsewhere
    win.monitors()                    # [Monitor(left, top, right, bottom, primary), ...]
    win.make_overlay(hwnd)            # click-through, never activated, not in Alt+Tab, topmost
    win.exclude_from_capture(hwnd)    # invisible to screenshots / OBS (Windows 10 2004+)
    previous = win.force_foreground(hwnd)   # bring a box to the front WITH keyboard focus
    win.restore_foreground(previous)        # give the focus back to where it was

For the playback failsafe (failsafe.py) it also tells which program / kind of window is where:
    win.foreground_context()  /  win.context_at(x, y)   # {"exe": "notepad.exe", "cls": "Notepad"}
    win.is_hung(hwnd), win.dialog_text(hwnd), win.running_programs()
Only the program file name and the window class are read, never window titles (those can hold
document names, e-mail subjects, ...). Wackro's own windows report None.

Why force_foreground is needed: Windows doesn't let a background program pull its window to
the front (anti pop-up protection). A box opened from the tray would appear behind the program
you're using. The usual fix, used by many tray apps, is to attach to the input of the window
that has the focus for a moment (AttachThreadInput), which makes Windows accept the switch. If
that's still refused, a tap of Alt (an "input event" from us) unlocks it. The box is also
always-on-top, so even then it's visible above everything and one click answers it.

Every call is wrapped: a failure is logged and ignored, never raised. On other systems
NullWin32 does nothing, except monitors(), which reports the Tk screen.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import NamedTuple, Optional

log = logging.getLogger(__name__)

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020        # mouse clicks go through to the window underneath
WS_EX_TOOLWINDOW = 0x00000080         # not in Alt+Tab / the taskbar
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000         # never becomes the active window
LWA_ALPHA = 0x2
WDA_EXCLUDEFROMCAPTURE = 0x11
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
MONITORINFOF_PRIMARY = 0x1
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x2
_EXE_CACHE_SECONDS = 5.0
CSIDL_PERSONAL = 0x0005   # SHGetFolderPathW: the Documents folder
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
DESKTOP_SWITCHDESKTOP = 0x0100
MB_ICONEXCLAMATION = 0x30
FLASHW_ALL = 0x3

OVERLAY_EX_STYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST


class Monitor(NamedTuple):
    left: int
    top: int
    right: int
    bottom: int
    primary: bool = False

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def hwnd_of(widget) -> Optional[int]:
    """The Windows handle of a Tk toplevel's real (outer) window, or None."""
    try:
        frame = widget.wm_frame()
        return int(frame, 16) if isinstance(frame, str) else int(frame)
    except Exception:
        return None


class NullWin32:
    """Everything a no-op; monitors() is the Tk screen. Used off Windows and in tests."""

    available = False

    def __init__(self, screen_size=None):
        self._screen_size = screen_size   # callable -> (width, height)

    def monitors(self) -> list:
        width, height = self._screen_size() if self._screen_size else (1920, 1080)
        return [Monitor(0, 0, int(width), int(height), True)]

    def make_overlay(self, hwnd, alpha: int = 255) -> bool:
        return False

    def exclude_from_capture(self, hwnd) -> bool:
        return False

    def set_topmost(self, hwnd) -> bool:
        return False

    def move(self, hwnd, x: int, y: int, width: int, height: int) -> bool:
        return False

    def foreground(self) -> Optional[int]:
        return None

    # window context (failsafe): {"exe": "notepad.exe", "cls": "Notepad"} or None
    def context_of(self, hwnd) -> Optional[dict]:
        return None

    def foreground_context(self) -> Optional[dict]:
        return None

    def context_at(self, x: int, y: int) -> Optional[dict]:
        return None

    def is_hung(self, hwnd) -> bool:
        return False

    def is_enabled(self, hwnd) -> bool:
        """False when a window is blocked by a popup (a modal box disables its owner)."""
        return True

    def window_at(self, x: int, y: int) -> Optional[int]:
        return None

    def dialog_text(self, hwnd) -> str:
        return ""

    def running_programs(self) -> Optional[set]:
        """Lower-case exe names of running programs, or None if that can't be told."""
        return None

    def force_foreground(self, hwnd) -> Optional[int]:
        return None

    def restore_foreground(self, previous) -> None:
        return None

    # blackout / power (v1.6)
    def set_alpha(self, hwnd, alpha: int) -> bool:
        return False

    def alert(self, hwnd) -> bool:
        """Get attention for an urgent box: the Windows exclamation sound and a few taskbar flashes."""
        return False

    def keep_awake(self, on: bool) -> bool:
        """Keep the PC (and the screen) from going to sleep / locking itself while on."""
        return False

    def input_desktop_blocked(self) -> bool:
        """True when the PC is locked or a UAC / Ctrl+Alt+Del screen is up: input can't reach
        programs then."""
        return False

    def documents_folder(self):
        """The user's Documents folder as Windows knows it (also when OneDrive moved it), or
        None if unknown."""
        return None


class Win32(NullWin32):
    """The real calls. user32 / kernel32 can be injected (tests)."""

    available = True

    def __init__(self, screen_size=None, user32=None, kernel32=None):
        super().__init__(screen_size)
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self.user32 = user32 if user32 is not None else ctypes.windll.user32
        self.kernel32 = kernel32 if kernel32 is not None else ctypes.windll.kernel32
        if user32 is None:
            self._declare_types()

    def _declare_types(self) -> None:
        """Exact argument / result types. Without them ctypes passes plain ints as 32-bit, which
        on 64-bit Windows can garble window handles and HWND_TOPMOST (-1)."""
        ctypes, w = self._ctypes, self._wintypes
        u = self.user32
        HWND, INT, UINT, BOOL, LONG, DWORD = w.HWND, ctypes.c_int, ctypes.c_uint, w.BOOL, ctypes.c_long, w.DWORD
        table = {
            "GetWindowLongW": ([HWND, INT], LONG),
            "SetWindowLongW": ([HWND, INT, LONG], LONG),
            "SetLayeredWindowAttributes": ([HWND, DWORD, ctypes.c_ubyte, DWORD], BOOL),
            "SetWindowDisplayAffinity": ([HWND, DWORD], BOOL),
            "SetWindowPos": ([HWND, HWND, INT, INT, INT, INT, UINT], BOOL),
            "GetForegroundWindow": ([], HWND),
            "SetForegroundWindow": ([HWND], BOOL),
            "BringWindowToTop": ([HWND], BOOL),
            "IsWindow": ([HWND], BOOL),
            "GetWindowThreadProcessId": ([HWND, ctypes.c_void_p], DWORD),
            "AttachThreadInput": ([DWORD, DWORD, BOOL], BOOL),
            "GetClassNameW": ([HWND, w.LPWSTR, INT], INT),
            "GetWindowTextW": ([HWND, w.LPWSTR, INT], INT),
            "WindowFromPoint": ([w.POINT], HWND),
            "GetAncestor": ([HWND, UINT], HWND),
            "IsHungAppWindow": ([HWND], BOOL),
            "IsWindowEnabled": ([HWND], BOOL),
            "OpenInputDesktop": ([DWORD, BOOL, DWORD], w.HANDLE),
            "SwitchDesktop": ([w.HANDLE], BOOL),
            "CloseDesktop": ([w.HANDLE], BOOL),
        }
        k = self.kernel32
        kernel_table = {
            "OpenProcess": ([DWORD, BOOL, DWORD], w.HANDLE),
            "CloseHandle": ([w.HANDLE], BOOL),
            "SetThreadExecutionState": ([DWORD], DWORD),
            "QueryFullProcessImageNameW": ([w.HANDLE, DWORD, w.LPWSTR, ctypes.POINTER(DWORD)], BOOL),
        }
        for name, (args, result) in kernel_table.items():
            try:
                function = getattr(k, name)
                function.argtypes = args
                function.restype = result
            except Exception:
                log.debug("Could not declare types for %s", name, exc_info=True)
        for name, (args, result) in table.items():
            try:
                function = getattr(u, name)
                function.argtypes = args
                function.restype = result
            except Exception:
                log.debug("Could not declare types for %s", name, exc_info=True)

    # ---- monitors ----

    def monitors(self) -> list:
        ctypes, wintypes = self._ctypes, self._wintypes
        found = []
        try:
            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                            ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

            proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.POINTER(RECT), ctypes.c_void_p)

            def callback(hmonitor, _hdc, _rect, _data):
                info = MONITORINFO()
                info.cbSize = ctypes.sizeof(MONITORINFO)
                if self.user32.GetMonitorInfoW(ctypes.c_void_p(hmonitor), ctypes.byref(info)):
                    r = info.rcMonitor
                    found.append(Monitor(r.left, r.top, r.right, r.bottom,
                                         bool(info.dwFlags & MONITORINFOF_PRIMARY)))
                return 1

            self.user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
        except Exception:
            log.warning("Could not list the monitors", exc_info=True)
        return found or super().monitors()

    # ---- overlay windows ----

    def make_overlay(self, hwnd, alpha: int = 255) -> bool:
        """Click-through, never activated, not in Alt+Tab, always on top."""
        if not hwnd:
            return False
        try:
            style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | OVERLAY_EX_STYLE)
            # WS_EX_LAYERED windows stay invisible until their opacity is set.
            self.user32.SetLayeredWindowAttributes(hwnd, 0, int(alpha), LWA_ALPHA)
            self.set_topmost(hwnd)
            return True
        except Exception:
            log.warning("Could not make an overlay window click-through", exc_info=True)
            return False

    def exclude_from_capture(self, hwnd) -> bool:
        if not hwnd:
            return False
        try:
            return bool(self.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
        except Exception:
            log.info("This Windows can't hide windows from screen captures", exc_info=True)
            return False

    def set_topmost(self, hwnd) -> bool:
        if not hwnd:
            return False
        try:
            return bool(self.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))
        except Exception:
            log.debug("SetWindowPos failed", exc_info=True)
            return False

    def move(self, hwnd, x: int, y: int, width: int, height: int) -> bool:
        """Put a window exactly here, on top, WITHOUT activating it. Used for the overlay: on
        Windows, Tk applies a new size to those borderless windows but ignores a new position."""
        if not hwnd:
            return False
        try:
            return bool(self.user32.SetWindowPos(hwnd, HWND_TOPMOST, int(x), int(y), int(width), int(height),
                                                 SWP_NOACTIVATE))
        except Exception:
            log.debug("SetWindowPos (move) failed", exc_info=True)
            return False

    # ---- window context (failsafe) ----

    def _exe_of_pid(self, pid: int, cached: bool = True) -> Optional[str]:
        """Lower-case exe name of a process. Cached briefly per pid (the guard asks a lot), but
        Windows reuses pids, so the cache is dropped every few seconds and never used for the
        list of running programs."""
        cache = self.__dict__.setdefault("_exe_cache", {})
        now = time.monotonic()
        if now - self.__dict__.get("_exe_cache_t", 0.0) > _EXE_CACHE_SECONDS:
            cache.clear()
            self._exe_cache_t = now
        if cached and pid in cache:
            return cache[pid]
        ctypes, w = self._ctypes, self._wintypes
        name = None
        handle = self.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                size = w.DWORD(1024)
                buffer = ctypes.create_unicode_buffer(1024)
                if self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    name = buffer.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
            finally:
                self.kernel32.CloseHandle(handle)
        if len(cache) > 512:
            cache.clear()
        cache[pid] = name
        return name

    def context_of(self, hwnd) -> Optional[dict]:
        """Program + window type (class) of a top-level window. Never the title."""
        if not hwnd:
            return None
        ctypes, w = self._ctypes, self._wintypes
        try:
            buffer = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, buffer, 256)
            pid = w.DWORD(0)
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid():
                return None     # our own window (dialog, indicator): never a playback target
            exe = self._exe_of_pid(pid.value)
            if not exe or not buffer.value:
                return None
            return {"exe": exe[:260], "cls": buffer.value[:260]}
        except Exception:
            log.debug("Could not read a window's context", exc_info=True)
            return None

    def foreground_context(self) -> Optional[dict]:
        return self.context_of(self.foreground())

    def window_at(self, x: int, y: int) -> Optional[int]:
        try:
            hwnd = self.user32.WindowFromPoint(self._wintypes.POINT(int(x), int(y)))
            return self.user32.GetAncestor(hwnd, 2) if hwnd else None    # GA_ROOT
        except Exception:
            return None

    def context_at(self, x: int, y: int) -> Optional[dict]:
        return self.context_of(self.window_at(x, y))

    def is_hung(self, hwnd) -> bool:
        try:
            return bool(hwnd) and bool(self.user32.IsHungAppWindow(hwnd))
        except Exception:
            return False

    def is_enabled(self, hwnd) -> bool:
        try:
            return not hwnd or bool(self.user32.IsWindowEnabled(hwnd))
        except Exception:
            return True

    def dialog_text(self, hwnd) -> str:
        """The message of a standard Windows message box (its Static child texts), shown in the
        status line when a popup stops playback. Read on the spot, never saved."""
        ctypes, w = self._ctypes, self._wintypes
        texts = []
        try:
            proc_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

            def callback(child, _param):
                cls = ctypes.create_unicode_buffer(64)
                self.user32.GetClassNameW(child, cls, 64)
                if cls.value.lower() == "static":
                    text = ctypes.create_unicode_buffer(512)
                    self.user32.GetWindowTextW(child, text, 512)
                    if text.value.strip():
                        texts.append(text.value.strip())
                return True

            self.user32.EnumChildWindows(hwnd, proc_type(callback), 0)
        except Exception:
            log.debug("Could not read the popup text", exc_info=True)
        return " ".join(texts)[:200]

    def running_programs(self) -> Optional[set]:
        ctypes, w = self._ctypes, self._wintypes
        try:
            pids = (w.DWORD * 4096)()
            needed = w.DWORD(0)
            if not self.kernel32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
                return None
            count = needed.value // ctypes.sizeof(w.DWORD)
            return {name for name in (self._exe_of_pid(pids[i], cached=False) for i in range(count)) if name}
        except Exception:
            log.warning("Could not list running programs", exc_info=True)
            return None

    # ---- attention ----

    def alert(self, hwnd) -> bool:
        ctypes, w = self._ctypes, self._wintypes
        ok = False
        try:
            ok = bool(self.user32.MessageBeep(MB_ICONEXCLAMATION))
        except Exception:
            log.debug("MessageBeep failed", exc_info=True)
        if not hwnd:
            return ok
        try:
            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", w.UINT), ("hwnd", w.HWND), ("dwFlags", w.DWORD),
                            ("uCount", w.UINT), ("dwTimeout", w.DWORD)]

            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL, 3, 0)
            self.user32.FlashWindowEx(ctypes.byref(info))
            ok = True
        except Exception:
            log.debug("FlashWindowEx failed", exc_info=True)
        return ok

    # ---- blackout / power ----

    def set_alpha(self, hwnd, alpha: int) -> bool:
        if not hwnd:
            return False
        try:
            return bool(self.user32.SetLayeredWindowAttributes(hwnd, 0, max(0, min(255, int(alpha))), LWA_ALPHA))
        except Exception:
            log.debug("Could not change a window's opacity", exc_info=True)
            return False

    def documents_folder(self):
        """SHGetFolderPathW(CSIDL_PERSONAL): follows a Documents folder moved by OneDrive or by
        hand, unlike %USERPROFILE%\\Documents."""
        try:
            ctypes, w = self._ctypes, self._wintypes
            if getattr(self, "shell32", None) is None:
                self.shell32 = ctypes.windll.shell32
                self.shell32.SHGetFolderPathW.argtypes = [w.HWND, ctypes.c_int, w.HANDLE, w.DWORD, w.LPWSTR]
                self.shell32.SHGetFolderPathW.restype = ctypes.c_long
            buffer = ctypes.create_unicode_buffer(1024)
            if self.shell32.SHGetFolderPathW(None, CSIDL_PERSONAL, None, 0, buffer) != 0:
                return None
            return buffer.value or None
        except Exception:
            log.warning("Could not ask Windows for the Documents folder", exc_info=True)
            return None

    def keep_awake(self, on: bool) -> bool:
        """SetThreadExecutionState. Must be switched off on the same thread it was switched on
        (the app does both on the Tk thread)."""
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
        try:
            return bool(self.kernel32.SetThreadExecutionState(flags))
        except Exception:
            log.warning("Could not change the keep-awake state", exc_info=True)
            return False

    def input_desktop_blocked(self) -> bool:
        """The usual test: the desktop that gets the input can't be switched to while the PC is
        locked or a secure screen (UAC, Ctrl+Alt+Del) is up."""
        try:
            desktop = self.user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
            if not desktop:
                return True
            try:
                return not self.user32.SwitchDesktop(desktop)
            finally:
                self.user32.CloseDesktop(desktop)
        except Exception:
            log.debug("Could not check for the lock screen", exc_info=True)
            return False

    # ---- focus ----

    def foreground(self) -> Optional[int]:
        try:
            return self.user32.GetForegroundWindow() or None
        except Exception:
            return None

    def force_foreground(self, hwnd) -> Optional[int]:
        """Bring hwnd to the front with the keyboard focus. Returns the window that had the focus
        before (for restore_foreground), or None."""
        if not hwnd:
            return None
        previous = self.foreground()
        if previous == hwnd:
            return None
        user32 = self.user32
        try:
            ours = self.kernel32.GetCurrentThreadId()
            theirs = user32.GetWindowThreadProcessId(previous, None) if previous else 0
            attached = bool(theirs and theirs != ours and user32.AttachThreadInput(theirs, ours, True))
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(theirs, ours, False)
            if self.foreground() != hwnd:
                # Still refused: an input event from us makes Windows allow it.
                user32.keybd_event(VK_MENU, 0, 0, 0)
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
                user32.SetForegroundWindow(hwnd)
        except Exception:
            log.warning("Could not bring the box to the front", exc_info=True)
        return previous

    def restore_foreground(self, previous) -> None:
        if not previous:
            return
        try:
            if self.user32.IsWindow(previous):
                self.user32.SetForegroundWindow(previous)
        except Exception:
            log.debug("Could not give the focus back", exc_info=True)


_instance = None


def get_win32(screen_size=None):
    """Win32 on Windows (if user32 loads), NullWin32 elsewhere. Cached."""
    global _instance
    if _instance is None:
        if sys.platform.startswith("win"):
            try:
                _instance = Win32(screen_size)
            except Exception:
                log.warning("user32 unavailable; overlay and focus helpers are off", exc_info=True)
                _instance = NullWin32(screen_size)
        else:
            _instance = NullWin32(screen_size)
    if _instance._screen_size is None and screen_size is not None:
        _instance._screen_size = screen_size
    return _instance
