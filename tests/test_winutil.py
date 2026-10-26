"""Tests for winutil.py. The Windows calls are faked (a recording fake user32 / kernel32), so the
logic is checked on any system; what Windows itself does with them is checked by hand.

Run from the project root:
    python -W error::ResourceWarning -m unittest discover -s tests
"""

import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import winutil  # noqa: E402
from winutil import Monitor, NullWin32, Win32  # noqa: E402

logging.getLogger("winutil").addHandler(logging.NullHandler())


class FakeUser32:
    def __init__(self, foreground=100, refuse=False):
        self.calls = []
        self.styles = {}
        self.fg = foreground
        self.refuse = refuse          # SetForegroundWindow refused until an Alt tap
        self.alt_tapped = False

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name,) + args)
            return 1
        return call

    def GetWindowLongW(self, hwnd, index):
        self.calls.append(("GetWindowLongW", hwnd, index))
        return self.styles.get(hwnd, 0x100)

    def SetWindowLongW(self, hwnd, index, value):
        self.calls.append(("SetWindowLongW", hwnd, index, value))
        self.styles[hwnd] = value
        return 1

    def GetForegroundWindow(self):
        return self.fg

    def GetWindowThreadProcessId(self, hwnd, _pid):
        return 7 if hwnd else 0

    def keybd_event(self, *args):
        self.calls.append(("keybd_event",) + args)
        self.alt_tapped = True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))
        if not self.refuse or self.alt_tapped:
            self.fg = hwnd
        return 1

    def names(self):
        return [c[0] for c in self.calls]


class FakeKernel32:
    def GetCurrentThreadId(self):
        return 3


def make(**kwargs):
    user32 = FakeUser32(**kwargs)
    return Win32(screen_size=lambda: (1280, 720), user32=user32, kernel32=FakeKernel32()), user32


class NullTests(unittest.TestCase):
    def test_everything_is_harmless(self):
        null = NullWin32(lambda: (1366, 768))
        self.assertEqual(null.monitors(), [Monitor(0, 0, 1366, 768, True)])
        self.assertFalse(null.make_overlay(5))
        self.assertFalse(null.exclude_from_capture(5))
        self.assertIsNone(null.force_foreground(5))
        null.restore_foreground(5)

    def test_default_screen(self):
        self.assertEqual(NullWin32().monitors()[0].width, 1920)

    def test_get_win32_off_windows_is_null(self):
        with mock.patch.object(winutil, "_instance", None), mock.patch.object(sys, "platform", "linux"):
            self.assertIsInstance(winutil.get_win32(lambda: (800, 600)), NullWin32)
            self.assertNotIsInstance(winutil.get_win32(), Win32)

    def test_get_win32_without_user32_falls_back(self):
        with mock.patch.object(winutil, "_instance", None), mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(winutil, "Win32", side_effect=OSError("no user32")):
            self.assertIsInstance(winutil.get_win32(), NullWin32)


class MonitorTests(unittest.TestCase):
    def test_monitor_size(self):
        m = Monitor(-1920, 0, 0, 1080)
        self.assertEqual((m.width, m.height, m.primary), (1920, 1080, False))

    def test_enumeration_failure_falls_back_to_the_screen(self):
        win, user32 = make()
        user32.EnumDisplayMonitors = mock.Mock(side_effect=OSError("nope"))
        self.assertEqual(win.monitors(), [Monitor(0, 0, 1280, 720, True)])

    def test_no_monitors_found_falls_back_to_the_screen(self):
        win, user32 = make()   # the fake EnumDisplayMonitors never calls back
        self.assertEqual(win.monitors(), [Monitor(0, 0, 1280, 720, True)])


class OverlayStyleTests(unittest.TestCase):
    def test_make_overlay_sets_all_the_bits_and_opacity(self):
        win, user32 = make()
        self.assertTrue(win.make_overlay(42, alpha=230))
        style = user32.styles[42]
        for bit in (winutil.WS_EX_TRANSPARENT, winutil.WS_EX_TOOLWINDOW, winutil.WS_EX_NOACTIVATE,
                    winutil.WS_EX_LAYERED, winutil.WS_EX_TOPMOST):
            self.assertTrue(style & bit, hex(bit))
        self.assertTrue(style & 0x100)          # existing bits kept
        self.assertIn(("SetLayeredWindowAttributes", 42, 0, 230, winutil.LWA_ALPHA), user32.calls)
        self.assertEqual(user32.names()[-1], "SetWindowPos")

    def test_no_hwnd(self):
        win, user32 = make()
        self.assertFalse(win.make_overlay(None))
        self.assertFalse(win.exclude_from_capture(0))
        self.assertEqual(user32.calls, [])

    def test_failures_are_swallowed(self):
        win, user32 = make()
        user32.SetWindowLongW = mock.Mock(side_effect=OSError("access denied"))
        self.assertFalse(win.make_overlay(42))
        user32.SetWindowDisplayAffinity = mock.Mock(side_effect=AttributeError("old Windows"))
        self.assertFalse(win.exclude_from_capture(42))

    def test_exclude_from_capture(self):
        win, user32 = make()
        self.assertTrue(win.exclude_from_capture(42))
        self.assertIn(("SetWindowDisplayAffinity", 42, winutil.WDA_EXCLUDEFROMCAPTURE), user32.calls)


class MoveTests(unittest.TestCase):
    def test_move_never_activates(self):
        win, user32 = make()
        self.assertTrue(win.move(42, 10, 20, 300, 3))
        self.assertIn(("SetWindowPos", 42, winutil.HWND_TOPMOST, 10, 20, 300, 3, winutil.SWP_NOACTIVATE),
                      user32.calls)

    def test_move_failures(self):
        win, user32 = make()
        self.assertFalse(win.move(None, 0, 0, 1, 1))
        user32.SetWindowPos = mock.Mock(side_effect=OSError("gone"))
        self.assertFalse(win.move(42, 0, 0, 1, 1))
        self.assertFalse(NullWin32().move(42, 0, 0, 1, 1))


class FocusTests(unittest.TestCase):
    def test_attaches_to_the_focused_thread_then_detaches(self):
        win, user32 = make(foreground=100)
        previous = win.force_foreground(55)
        self.assertEqual(previous, 100)
        self.assertEqual(user32.fg, 55)
        names = user32.names()
        self.assertEqual(names[0], "AttachThreadInput")
        self.assertIn(("AttachThreadInput", 7, 3, True), user32.calls)
        self.assertIn(("AttachThreadInput", 7, 3, False), user32.calls)
        self.assertNotIn("keybd_event", names)

    def test_alt_tap_when_still_refused(self):
        win, user32 = make(foreground=100, refuse=True)
        win.force_foreground(55)
        self.assertTrue(user32.alt_tapped)
        self.assertEqual(user32.fg, 55)

    def test_already_in_front(self):
        win, user32 = make(foreground=55)
        self.assertIsNone(win.force_foreground(55))
        self.assertEqual(user32.calls, [])

    def test_restore(self):
        win, user32 = make(foreground=55)
        win.restore_foreground(100)
        self.assertEqual(user32.fg, 100)
        win.restore_foreground(None)

    def test_detach_even_if_switching_fails(self):
        win, user32 = make(foreground=100)
        user32.BringWindowToTop = mock.Mock(side_effect=OSError("boom"))
        win.force_foreground(55)
        self.assertIn(("AttachThreadInput", 7, 3, False), user32.calls)


class HwndTests(unittest.TestCase):
    def test_hwnd_of(self):
        self.assertEqual(winutil.hwnd_of(mock.Mock(wm_frame=lambda: "0x1a2b")), 0x1A2B)
        self.assertEqual(winutil.hwnd_of(mock.Mock(wm_frame=lambda: 77)), 77)
        self.assertIsNone(winutil.hwnd_of(mock.Mock(wm_frame=mock.Mock(side_effect=RuntimeError))))


class ContextUser32(FakeUser32):
    """Windows 10 and 20 belong to pids 1000 / 2000; 30 is ours."""
    PIDS = {10: 1000, 20: 2000}
    CLASSES = {10: "Notepad", 20: "#32770"}

    def __init__(self):
        super().__init__()
        self.hung = set()
        self.disabled = set()
        self.at = 10

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = self.CLASSES.get(hwnd, "TkTopLevel")
        return len(buffer.value)

    def GetWindowThreadProcessId(self, hwnd, pid_ref):
        if pid_ref is not None:
            pid_ref._obj.value = self.PIDS.get(hwnd, os.getpid())
        return 7

    def WindowFromPoint(self, point):
        self.calls.append(("WindowFromPoint", point.x, point.y))
        return 99

    def GetAncestor(self, hwnd, flag):
        return self.at

    def IsHungAppWindow(self, hwnd):
        return hwnd in self.hung

    def IsWindowEnabled(self, hwnd):
        return hwnd not in self.disabled


class ContextKernel32(FakeKernel32):
    def __init__(self):
        self.exes = {1000: "C:\\Windows\\notepad.exe", 2000: "D:\\BIR\\BIR.exe"}
        self.opened = []
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append(pid)
        return pid if pid in self.exes else 0

    def QueryFullProcessImageNameW(self, handle, flags, buffer, size_ref):
        buffer.value = self.exes[handle]
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)


def make_context():
    user32, kernel32 = ContextUser32(), ContextKernel32()
    return Win32(screen_size=lambda: (1280, 720), user32=user32, kernel32=kernel32), user32, kernel32


class ContextTests(unittest.TestCase):
    def test_program_and_class_never_the_title(self):
        win, user32, kernel32 = make_context()
        self.assertEqual(win.context_of(10), {"exe": "notepad.exe", "cls": "Notepad"})
        self.assertEqual(win.context_of(20), {"exe": "bir.exe", "cls": "#32770"})
        self.assertNotIn("GetWindowTextW", user32.names())
        self.assertEqual(kernel32.closed, [1000, 2000])     # every process handle closed

    def test_our_own_windows_and_nothing_are_unknown(self):
        win, user32, kernel32 = make_context()
        self.assertIsNone(win.context_of(30))
        self.assertIsNone(win.context_of(None))
        self.assertIsNone(NullWin32().context_at(1, 2))

    def test_unreadable_process(self):
        win, user32, kernel32 = make_context()
        kernel32.exes.pop(1000)
        self.assertIsNone(win.context_of(10))

    def test_lookups_are_cached_but_not_forever(self):
        win, user32, kernel32 = make_context()
        win.context_of(10)
        win.context_of(10)
        self.assertEqual(kernel32.opened, [1000])
        with mock.patch.object(winutil.time, "monotonic", return_value=winutil.time.monotonic() + 60):
            win.context_of(10)
        self.assertEqual(kernel32.opened, [1000, 1000])

    def test_window_at_a_point(self):
        win, user32, kernel32 = make_context()
        user32.at = 20
        self.assertEqual(win.context_at(5, 6), {"exe": "bir.exe", "cls": "#32770"})
        self.assertIn(("WindowFromPoint", 5, 6), user32.calls)

    def test_foreground_context(self):
        win, user32, kernel32 = make_context()
        user32.fg = 10
        self.assertEqual(win.foreground_context()["exe"], "notepad.exe")

    def test_hung_and_enabled(self):
        win, user32, kernel32 = make_context()
        user32.hung.add(10)
        user32.disabled.add(10)
        self.assertTrue(win.is_hung(10))
        self.assertFalse(win.is_hung(20))
        self.assertFalse(win.is_hung(None))
        self.assertFalse(win.is_enabled(10))
        self.assertTrue(win.is_enabled(20))
        self.assertTrue(win.is_enabled(None))
        user32.IsHungAppWindow = mock.Mock(side_effect=OSError)
        self.assertFalse(win.is_hung(10))

    def test_keep_awake(self):
        win, user32, kernel32 = make_context()
        kernel32.SetThreadExecutionState = mock.Mock(return_value=1)
        self.assertTrue(win.keep_awake(True))
        kernel32.SetThreadExecutionState.assert_called_with(
            winutil.ES_CONTINUOUS | winutil.ES_SYSTEM_REQUIRED | winutil.ES_DISPLAY_REQUIRED)
        win.keep_awake(False)
        kernel32.SetThreadExecutionState.assert_called_with(winutil.ES_CONTINUOUS)
        kernel32.SetThreadExecutionState.side_effect = OSError
        self.assertFalse(win.keep_awake(True))

    def test_locked_desktop(self):
        win, user32, kernel32 = make_context()
        user32.OpenInputDesktop = mock.Mock(return_value=5)
        user32.SwitchDesktop = mock.Mock(return_value=1)
        user32.CloseDesktop = mock.Mock()
        self.assertFalse(win.input_desktop_blocked())
        user32.CloseDesktop.assert_called_with(5)
        user32.SwitchDesktop.return_value = 0                 # locked / UAC screen
        self.assertTrue(win.input_desktop_blocked())
        user32.OpenInputDesktop.return_value = 0
        self.assertTrue(win.input_desktop_blocked())
        user32.OpenInputDesktop.side_effect = OSError
        self.assertFalse(win.input_desktop_blocked())         # can't tell: don't stop anything

    def test_alert_beeps_and_flashes(self):
        win, user32, kernel32 = make_context()
        self.assertTrue(win.alert(42))
        names = user32.names()
        self.assertIn(("MessageBeep", winutil.MB_ICONEXCLAMATION), user32.calls)
        self.assertIn("FlashWindowEx", names)
        user32.MessageBeep = mock.Mock(side_effect=OSError)
        user32.FlashWindowEx = mock.Mock(side_effect=OSError)
        self.assertFalse(win.alert(42))                      # never raises
        self.assertFalse(NullWin32().alert(42))

    def test_set_alpha(self):
        win, user32, kernel32 = make_context()
        self.assertTrue(win.set_alpha(9, 300))
        self.assertIn(("SetLayeredWindowAttributes", 9, 0, 255, winutil.LWA_ALPHA), user32.calls)
        self.assertFalse(win.set_alpha(None, 10))

    def test_null_answers(self):
        null = NullWin32()
        self.assertIsNone(null.foreground_context())
        self.assertFalse(null.is_hung(1))
        self.assertTrue(null.is_enabled(1))
        self.assertEqual(null.dialog_text(1), "")
        self.assertIsNone(null.running_programs())
        self.assertIsNone(null.window_at(1, 1))
        self.assertFalse(null.keep_awake(True))
        self.assertFalse(null.input_desktop_blocked())
        self.assertFalse(null.set_alpha(1, 1))


class FakeShell32:
    def __init__(self, admin=1, execute=42, folder=0):
        self.admin, self.execute, self.folder, self.calls = admin, execute, folder, []

    def IsUserAnAdmin(self):
        return self.admin

    def ShellExecuteW(self, hwnd, verb, program, parameters, directory, show):
        self.calls.append((verb, program, parameters, show))
        return self.execute

    def SHGetFolderPathW(self, hwnd, csidl, token, flags, buffer):
        buffer.value = r"D:\OneDrive\Documents"
        return self.folder


class ShellTests(unittest.TestCase):
    def make(self, **kwargs):
        win, _ = make()
        win.shell32 = FakeShell32(**kwargs)
        return win

    def test_is_admin(self):
        self.assertTrue(self.make(admin=1).is_admin())
        self.assertFalse(self.make(admin=0).is_admin())
        self.assertFalse(NullWin32().is_admin())

    def test_run_as_admin_uses_runas_and_reads_the_result(self):
        win = self.make(execute=42)
        self.assertTrue(win.run_as_admin("C:\\W\\Wackro.exe", "--admin"))
        self.assertEqual(win.shell32.calls, [("runas", "C:\\W\\Wackro.exe", "--admin", 1)])
        self.assertFalse(self.make(execute=5).run_as_admin("x", ""))      # <= 32: refused / failed
        self.assertFalse(self.make(execute=None).run_as_admin("x", ""))
        self.assertFalse(NullWin32().run_as_admin("x", ""))

    def test_documents_folder(self):
        self.assertEqual(self.make().documents_folder(), r"D:\OneDrive\Documents")
        self.assertIsNone(self.make(folder=1).documents_folder())       # an error HRESULT
        self.assertIsNone(NullWin32().documents_folder())

    def test_a_crashing_shell32_never_raises(self):
        win, _ = make()
        win.shell32 = object()                  # every call raises AttributeError
        self.assertFalse(win.is_admin())
        self.assertFalse(win.run_as_admin("x", ""))
        self.assertIsNone(win.documents_folder())


if __name__ == "__main__":
    unittest.main()
