"""Tests for main.py: the start-up flow, the message boxes and the DPI call. No window is opened."""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_module  # noqa: E402
from config import APP_NAME, APP_VERSION  # noqa: E402



class FakeAdminWin:
    def __init__(self, admin, runs):
        self.admin, self.runs, self.started = admin, runs, []

    def is_admin(self):
        return self.admin

    def run_as_admin(self, program, parameters):
        self.started.append((program, parameters))
        return self.runs

class FakeGuard:
    def __init__(self, free=True):
        self.free = free
        self.released = 0

    def acquire(self):
        return self.free

    def release(self):
        self.released += 1


class FakeApp:
    def __init__(self, fail_in_mainloop=None, fail_in_init=None):
        if fail_in_init:
            raise fail_in_init
        self._fail = fail_in_mainloop
        self.ran = False

    def mainloop(self):
        self.ran = True
        if self._fail:
            raise self._fail


class MainFlowTests(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        before = list(root.handlers)
        hook, thread_hook = sys.excepthook, __import__("threading").excepthook
        level = root.level

        def restore():
            for handler in list(root.handlers):
                if handler not in before:
                    root.removeHandler(handler)
                    handler.close()
            root.setLevel(level)
            sys.excepthook = hook
            __import__("threading").excepthook = thread_hook

        self.addCleanup(restore)
        patcher = mock.patch.object(main_module, "setup_logging")
        self.setup_logging = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(main_module, "enable_dpi_awareness")
        self.dpi = patcher.start()
        self.addCleanup(patcher.stop)
        self.show = mock.MagicMock()

    def run_main(self, guard, app_factory):
        return main_module.main(guard_factory=lambda: guard, app_factory=app_factory, show=self.show)

    def test_admin_flag_restarts_elevated_and_exits(self):
        win = FakeAdminWin(admin=False, runs=True)
        guard, apps = FakeGuard(), []
        code = main_module.main(guard_factory=lambda: guard, app_factory=lambda: apps.append(FakeApp()) or apps[-1],
                                show=self.show, argv=["--admin"], win32=win)
        self.assertEqual(code, 0)
        self.assertEqual(apps, [])                        # this copy never opened a window
        self.assertEqual(guard.released, 0)               # nor took the single-instance lock
        program, parameters = win.started[0]
        self.assertIn("--admin", parameters)
        self.assertEqual(program, sys.executable)

    def test_admin_flag_when_already_admin_or_uac_refused_just_runs(self):
        for win in (FakeAdminWin(admin=True, runs=True), FakeAdminWin(admin=False, runs=False)):
            with self.subTest(admin=win.admin):
                apps = []
                code = main_module.main(guard_factory=FakeGuard, app_factory=lambda: apps.append(FakeApp()) or apps[-1],
                                        show=self.show, argv=["--admin"], win32=win)
                self.assertEqual(code, 0)
                self.assertTrue(apps[0].ran)

    def test_without_the_flag_nothing_is_asked(self):
        win = FakeAdminWin(admin=False, runs=True)
        self.assertFalse(main_module.restart_as_admin([], win))
        self.assertEqual(win.started, [])

    def test_frozen_restart_passes_only_the_arguments(self):
        win = FakeAdminWin(admin=False, runs=True)
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", r"C:\Program Files\Wackro\Wackro.exe"):
            self.assertTrue(main_module.restart_as_admin(["--admin"], win))
        self.assertEqual(win.started, [(r"C:\Program Files\Wackro\Wackro.exe", "--admin")])

    def test_normal_run_returns_0_and_releases_the_lock(self):
        guard, apps = FakeGuard(), []
        code = self.run_main(guard, lambda: apps.append(FakeApp()) or apps[-1])
        self.assertEqual(code, 0)
        self.assertTrue(apps[0].ran)
        self.assertEqual(guard.released, 1)
        self.show.assert_not_called()

    def test_logging_and_dpi_are_set_up_before_the_app(self):
        order = []
        self.setup_logging.side_effect = lambda: order.append("logging")
        self.dpi.side_effect = lambda: order.append("dpi")
        self.run_main(FakeGuard(), lambda: order.append("app") or FakeApp())
        self.assertEqual(order, ["logging", "dpi", "app"])

    def test_second_instance_exits_0_with_a_message_and_never_builds_the_app(self):
        built = []
        guard = FakeGuard(free=False)
        code = self.run_main(guard, lambda: built.append(1) or FakeApp())
        self.assertEqual(code, 0)
        self.assertEqual(built, [])
        self.show.assert_called_once()
        message = self.show.call_args[0][0]
        self.assertIn("already running", message)
        self.assertFalse(self.show.call_args[1].get("error", False))

    def test_start_up_crash_shows_an_error_returns_1_and_releases_the_lock(self):
        guard = FakeGuard()
        with self.assertLogs("main", level="CRITICAL"):
            code = self.run_main(guard, lambda: FakeApp(fail_in_init=ValueError("no display")))
        self.assertEqual(code, 1)
        self.assertEqual(guard.released, 1)
        self.show.assert_called_once()
        self.assertIn("no display", self.show.call_args[0][0])
        self.assertTrue(self.show.call_args[1]["error"])

    def test_crash_inside_mainloop_is_handled_the_same_way(self):
        guard = FakeGuard()
        with self.assertLogs("main", level="CRITICAL"):
            code = self.run_main(guard, lambda: FakeApp(fail_in_mainloop=RuntimeError("boom")))
        self.assertEqual(code, 1)
        self.assertEqual(guard.released, 1)
        self.assertIn("boom", self.show.call_args[0][0])

    def test_error_text_points_to_the_log_file(self):
        with self.assertLogs("main", level="CRITICAL"):
            self.run_main(FakeGuard(), lambda: FakeApp(fail_in_init=ValueError("x")))
        self.assertIn(str(main_module.LOG_PATH), self.show.call_args[0][0])

    def test_start_up_is_logged_with_the_version(self):
        with self.assertLogs("main", level="INFO") as logs:
            self.run_main(FakeGuard(), FakeApp)
        self.assertTrue(any(APP_NAME in line and APP_VERSION in line for line in logs.output))


class ShowMessageTests(unittest.TestCase):
    """show_message must use a hidden root, pick the right box and never raise."""

    def patched(self):
        root = mock.MagicMock(name="root")
        tk = mock.MagicMock(name="tkinter")
        tk.Tk.return_value = root
        box = mock.MagicMock(name="messagebox")
        patch = mock.patch.dict(sys.modules, {"tkinter": tk, "tkinter.messagebox": box})
        tk.messagebox = box
        patch.start()
        self.addCleanup(patch.stop)
        return tk, root, box

    def test_info_box_uses_a_hidden_root_and_destroys_it(self):
        tk, root, box = self.patched()
        main_module.show_message("hello")
        root.withdraw.assert_called_once()
        box.showinfo.assert_called_once_with(APP_NAME, "hello", parent=root)
        box.showerror.assert_not_called()
        root.destroy.assert_called_once()

    def test_error_box(self):
        tk, root, box = self.patched()
        main_module.show_message("bad", error=True)
        box.showerror.assert_called_once_with(APP_NAME, "bad", parent=root)
        box.showinfo.assert_not_called()

    def test_root_is_hidden_before_the_box_is_shown(self):
        tk, root, box = self.patched()
        calls = []
        root.withdraw.side_effect = lambda: calls.append("withdraw")
        box.showinfo.side_effect = lambda *a, **k: calls.append("box")
        main_module.show_message("x")
        self.assertEqual(calls, ["withdraw", "box"])

    def test_root_is_destroyed_even_if_the_box_fails(self):
        tk, root, box = self.patched()
        box.showinfo.side_effect = RuntimeError("tcl died")
        with self.assertLogs("main", level="WARNING"):
            main_module.show_message("x")  # must not raise
        root.destroy.assert_called_once()

    def test_no_display_does_not_raise(self):
        tk, root, box = self.patched()
        tk.Tk.side_effect = RuntimeError("no display")
        with self.assertLogs("main", level="WARNING"):
            main_module.show_message("x")


class DpiTests(unittest.TestCase):
    def test_noop_off_windows(self):
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch.dict(sys.modules, {"ctypes": None}):  # importing it would raise
                main_module.enable_dpi_awareness()

    def test_windows_prefers_per_monitor_awareness(self):
        ctypes = mock.MagicMock()
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"ctypes": ctypes}):
            main_module.enable_dpi_awareness()
        ctypes.windll.shcore.SetProcessDpiAwareness.assert_called_once_with(2)
        ctypes.windll.user32.SetProcessDPIAware.assert_not_called()

    def test_windows_falls_back_to_the_old_call(self):
        ctypes = mock.MagicMock()
        ctypes.windll.shcore.SetProcessDpiAwareness.side_effect = OSError("old Windows")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"ctypes": ctypes}):
            main_module.enable_dpi_awareness()
        ctypes.windll.user32.SetProcessDPIAware.assert_called_once_with()

    def test_windows_logs_but_survives_when_both_fail(self):
        ctypes = mock.MagicMock()
        ctypes.windll.shcore.SetProcessDpiAwareness.side_effect = OSError("a")
        ctypes.windll.user32.SetProcessDPIAware.side_effect = OSError("b")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(sys.modules, {"ctypes": ctypes}):
            with self.assertLogs("main", level="WARNING"):
                main_module.enable_dpi_awareness()


if __name__ == "__main__":
    unittest.main()
