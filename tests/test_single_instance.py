"""Tests for single_instance.py. The POSIX path is tested with real lock files and real
subprocesses; the Windows path with a fake kernel32."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from single_instance import SingleInstance  # noqa: E402

POSIX = os.name == "posix"


class FakeKernel32:
    """CreateMutexW returns a handle; the 'last error' says whether it already existed."""

    def __init__(self, handle=1234, error=0):
        self.CreateMutexW = mock.MagicMock(return_value=handle)
        self.CloseHandle = mock.MagicMock()
        self.error = error

    def last_error(self):
        return self.error


@unittest.skipUnless(POSIX, "flock path is POSIX only")
class PosixTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def make(self, **kwargs):
        guard = SingleInstance(lock_dir=self.dir, use_windows=False, **kwargs)
        self.addCleanup(guard.release)
        return guard

    def test_first_acquire_succeeds_and_holds(self):
        guard = self.make()
        self.assertFalse(guard.held)
        self.assertTrue(guard.acquire())
        self.assertTrue(guard.held)

    def test_second_instance_is_refused(self):
        first, second = self.make(), self.make()
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        self.assertFalse(second.held)

    def test_refused_instance_leaves_the_first_one_alone(self):
        first, second = self.make(), self.make()
        first.acquire()
        second.acquire()
        second.release()
        self.assertTrue(first.held)
        self.assertFalse(self.make().acquire())  # still locked by `first`

    def test_release_lets_the_next_one_in(self):
        first, second = self.make(), self.make()
        first.acquire()
        first.release()
        self.assertFalse(first.held)
        self.assertTrue(second.acquire())

    def test_release_closes_the_lock_file_itself(self):
        guard = self.make()
        guard.acquire()
        lock_file = guard._file  # keep our own reference, so garbage collection can't close it for us
        guard.release()
        self.assertTrue(lock_file.closed)

    def test_acquire_twice_on_same_object_is_fine(self):
        guard = self.make()
        self.assertTrue(guard.acquire())
        self.assertTrue(guard.acquire())
        self.assertTrue(guard.held)

    def test_release_is_idempotent_and_works_without_acquire(self):
        guard = self.make()
        guard.release()
        guard.acquire()
        guard.release()
        guard.release()
        self.assertFalse(guard.held)

    def test_different_names_do_not_collide(self):
        a = self.make(name="AppA")
        b = self.make(name="AppB")
        self.assertTrue(a.acquire())
        self.assertTrue(b.acquire())

    def test_lock_file_is_named_after_the_app(self):
        self.make(name="Wackro").acquire()
        self.assertTrue((self.dir / "wackro.lock").exists())

    def test_creates_the_folder_if_missing(self):
        nested = self.dir / "a" / "b"
        guard = SingleInstance(lock_dir=nested, use_windows=False)
        self.addCleanup(guard.release)
        self.assertTrue(guard.acquire())
        self.assertTrue(nested.is_dir())

    def test_unusable_folder_starts_anyway(self):
        blocker = self.dir / "file"
        blocker.write_text("x")  # a file where the folder should be: mkdir fails
        guard = SingleInstance(lock_dir=blocker / "sub", use_windows=False)
        with self.assertLogs("single_instance", level="WARNING"):
            self.assertTrue(guard.acquire())
        self.assertFalse(guard.held)

    def test_unexpected_flock_error_starts_anyway(self):
        import fcntl

        guard = self.make()
        with mock.patch.object(fcntl, "flock", side_effect=OSError("boom")):
            with self.assertLogs("single_instance", level="WARNING"):
                self.assertTrue(guard.acquire())
        self.assertFalse(guard.held)

    def test_a_real_second_process_is_refused_and_a_dead_one_frees_the_lock(self):
        child_code = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from pathlib import Path\n"
            "from single_instance import SingleInstance\n"
            f"g = SingleInstance(lock_dir=Path({str(self.dir)!r}), use_windows=False)\n"
            "print('got' if g.acquire() else 'refused', flush=True)\n"
            "sys.stdin.readline()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline().strip(), "got")

        mine = self.make()
        self.assertFalse(mine.acquire())      # the child holds it

        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(timeout=10)                # the child exits without calling release()
        self.assertTrue(mine.acquire())       # the OS freed the lock


class WindowsTests(unittest.TestCase):
    def make(self, kernel32):
        guard = SingleInstance(use_windows=True, kernel32=kernel32, last_error=kernel32.last_error)
        self.addCleanup(guard.release)
        return guard

    def test_first_instance_gets_the_mutex(self):
        kernel32 = FakeKernel32(handle=77, error=0)
        guard = self.make(kernel32)
        self.assertTrue(guard.acquire())
        self.assertTrue(guard.held)
        args = kernel32.CreateMutexW.call_args[0]
        self.assertEqual(args, (None, False, "Local\\Wackro"))

    def test_existing_mutex_means_another_instance(self):
        kernel32 = FakeKernel32(handle=77, error=183)
        guard = self.make(kernel32)
        self.assertFalse(guard.acquire())
        self.assertFalse(guard.held)
        kernel32.CloseHandle.assert_called_once_with(77)  # our duplicate handle is closed again

    def test_release_closes_the_handle_once(self):
        kernel32 = FakeKernel32(handle=77)
        guard = self.make(kernel32)
        guard.acquire()
        guard.release()
        guard.release()
        kernel32.CloseHandle.assert_called_once_with(77)
        self.assertFalse(guard.held)

    def test_failed_create_starts_anyway(self):
        kernel32 = FakeKernel32(handle=0, error=8)  # ERROR_NOT_ENOUGH_MEMORY, i.e. not an "already running" case
        guard = self.make(kernel32)
        with self.assertLogs("single_instance", level="WARNING"):
            self.assertTrue(guard.acquire())
        self.assertFalse(guard.held)

    def test_access_denied_means_an_elevated_copy_owns_it(self):
        # A normal Wackro.exe started while Wackro-Admin.exe is running: the mutex exists but
        # Windows won't let a non-elevated process open it (error 5, handle NULL).
        kernel32 = FakeKernel32(handle=0, error=5)
        guard = self.make(kernel32)
        self.assertFalse(guard.acquire())
        self.assertFalse(guard.held)
        kernel32.CloseHandle.assert_not_called()  # there is no handle to close

    def test_api_exception_starts_anyway(self):
        kernel32 = FakeKernel32()
        kernel32.CreateMutexW.side_effect = OSError("no kernel32")
        guard = self.make(kernel32)
        with self.assertLogs("single_instance", level="WARNING"):
            self.assertTrue(guard.acquire())

    def test_close_handle_failure_is_swallowed(self):
        kernel32 = FakeKernel32(handle=77)
        kernel32.CloseHandle.side_effect = OSError("bad handle")
        guard = self.make(kernel32)
        guard.acquire()
        guard.release()  # must not raise
        self.assertFalse(guard.held)

    def test_defaults_to_the_current_platform(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(SingleInstance()._use_windows)
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(SingleInstance()._use_windows)


if __name__ == "__main__":
    unittest.main()
