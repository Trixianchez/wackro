"""Run the test suite with a watchdog:  python tools/run_tests.py  [seconds per test, default 60]

Same as `python -W error::ResourceWarning -m unittest discover -s tests`, plus: if one test runs
longer than the limit (it's stuck, e.g. waiting for a window that never answers), the name of
that test and exactly where every thread is stuck are printed, and the run stops (exit code
not 0) instead of hanging forever. build.ps1 uses this.
"""

import faulthandler
import os
import sys
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class WatchdogResult(unittest.TextTestResult):
    limit = 60.0

    def startTest(self, test):
        sys.stderr.flush()
        # Still running after `limit` seconds: dump every thread's stack (the test's name and line
        # are in it) and exit; a hung Tk / Windows call can't be interrupted any other way.
        faulthandler.dump_traceback_later(self.limit, exit=True)
        super().startTest(test)

    def stopTest(self, test):
        faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


def main() -> int:
    limit = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    WatchdogResult.limit = limit
    warnings.simplefilter("error", ResourceWarning)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    faulthandler.enable(file=sys.__stderr__, all_threads=True)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))
    extra = {"durations": 15} if sys.version_info >= (3, 12) else {}   # the slowest tests, at the end
    runner = unittest.TextTestRunner(resultclass=WatchdogResult, verbosity=1, **extra)
    print(f"Running tests (a test stuck for more than {limit:g} s is reported and stops the run)...")
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
