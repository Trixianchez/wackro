"""Wackro entry point:  python main.py"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

from config import APP_NAME, APP_VERSION, LOG_DIR, LOG_PATH, carry_over_old_data
from single_instance import SingleInstance


def setup_logging() -> None:
    """Log to a rotating file (the .exe has no console). Falls back to stderr if the folder is unusable."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)

    def log_uncaught(exc_type, exc, tb):
        logging.getLogger("main").critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = log_uncaught
    threading.excepthook = lambda args: log_uncaught(args.exc_type, args.exc_value, args.exc_traceback)


def enable_dpi_awareness() -> None:
    """Without this, Windows display scaling (125% / 150%) makes recorded and played mouse
    coordinates disagree with what's on screen. Must run before any window exists. No-op elsewhere."""
    if not sys.platform.startswith("win"):
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            logging.getLogger("main").warning("Could not enable DPI awareness", exc_info=True)


def show_message(text: str, error: bool = False) -> None:
    """A native message box that works before (or without) the main window.

    Uses a hidden Tk root, so no empty window flashes up behind the box. Never raises: if even
    this fails there is nothing more we can do, and the text is already in the log.
    """
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        try:
            show = messagebox.showerror if error else messagebox.showinfo
            show(APP_NAME, text, parent=root)
        finally:
            root.destroy()
    except Exception:
        logging.getLogger("main").warning("Could not show a message box", exc_info=True)


ADMIN_FLAG = "--admin"


def restart_as_admin(argv: list, win32=None) -> bool:
    """`--admin` (the "Wackro (Administrator)" shortcut): if this copy isn't elevated, start a
    new one through UAC and return True (this one then exits). Already elevated, not asked for,
    or the user said No to UAC: False, and this copy carries on (normal rights in that last case).

    One installed program can so run either way, instead of shipping a second Admin exe."""
    if ADMIN_FLAG not in argv:
        return False
    if win32 is None:
        import winutil
        win32 = winutil.get_win32()
    if win32.is_admin():
        return False
    import subprocess
    if getattr(sys, "frozen", False):
        program, args = sys.executable, list(argv)
    else:
        program, args = sys.executable, [str(Path(__file__).resolve())] + list(argv)
    if win32.run_as_admin(program, subprocess.list2cmdline(args)):
        logging.getLogger("main").info("Restarted with administrator rights")
        return True
    logging.getLogger("main").warning("Administrator rights refused; running without them")
    return False


def main(guard_factory=SingleInstance, app_factory=None, show=show_message, argv=None, win32=None) -> int:
    """Start the app. Returns the process exit code. The arguments exist for the tests."""
    try:
        carried = carry_over_old_data()
    except Exception:
        carried = []
    setup_logging()
    if carried:
        logging.getLogger(__name__).info("Carried over from Wackylloop: %s", ", ".join(carried))
    log = logging.getLogger("main")
    log.info("Starting %s %s", APP_NAME, APP_VERSION)
    enable_dpi_awareness()

    if restart_as_admin(list(sys.argv[1:] if argv is None else argv), win32):
        return 0     # the elevated copy takes over (before the single-instance lock is taken)

    guard = guard_factory()
    if not guard.acquire():
        log.info("Another copy of %s is already running; exiting", APP_NAME)
        show(f"{APP_NAME} is already running. Look for its window in the taskbar.")
        return 0

    try:
        if app_factory is None:
            from app import App as app_factory
        app = app_factory()
        app.mainloop()
    except Exception as error:
        log.critical("%s crashed", APP_NAME, exc_info=True)
        show(f"{APP_NAME} couldn't start or crashed:\n\n{error}\n\nDetails are in {LOG_PATH}", error=True)
        return 1
    finally:
        guard.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
