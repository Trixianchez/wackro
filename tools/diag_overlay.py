"""Diagnostic for the recording indicator on Windows.

    python tools\\diag_overlay.py

Shows the red border + REC pill for a few seconds in two ways (plain Tk, then with the real
Windows click-through styles) and prints where Windows really put each piece. Paste the output
back. It changes nothing on your PC and doesn't record anything.
"""

import ctypes
import os
import sys
import time
import tkinter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import overlay  # noqa: E402
import winutil  # noqa: E402


def rect(hwnd):
    if not sys.platform.startswith("win") or not hwnd:
        return None

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

    r = RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    visible = ctypes.windll.user32.IsWindowVisible(hwnd)
    iconic = ctypes.windll.user32.IsIconic(hwnd)
    return f"win32 rect=({r.l},{r.t})-({r.r},{r.b}) visible={visible} iconic={iconic}"


def describe(name, window):
    window.update_idletasks()
    hwnd = winutil.hwnd_of(window)
    print(f"  {name}: geometry={window.geometry()} winfo=({window.winfo_x()},{window.winfo_y()} "
          f"{window.winfo_width()}x{window.winfo_height()}) mapped={window.winfo_ismapped()} "
          f"viewable={window.winfo_viewable()} state={window.state()} hwnd={hwnd} | {rect(hwnd)}")


def run(title, win32, root_withdrawn):
    print(f"\n=== {title} (main window {'hidden' if root_withdrawn else 'shown'}) ===")
    root = tkinter.Tk()
    root.geometry("300x80+200+200")
    if root_withdrawn:
        root.withdraw()
    root.update()
    o = overlay.Overlay(root, win32=win32)
    o.prepare()
    root.update()
    print(" after prepare (should be parked off-screen):")
    describe("strip0", o._strips[0])
    o.show("recording", "● REC 0:00", style="both")
    root.update()
    print(" after show (should be on the screen edges / top-center):")
    for i, s in enumerate(o._strips[:4]):
        describe(f"strip{i}", s)
    describe("pill", o._pill)
    print(f" placed() = {o.placed()}")
    end = time.time() + 4
    while time.time() < end:          # look at your screen now: is there a red border + pill?
        root.update()
        time.sleep(0.02)
    o.hide()
    root.update()
    print(" after hide:")
    describe("strip0", o._strips[0])
    o.destroy()
    root.destroy()


if __name__ == "__main__":
    print(sys.version, sys.platform, "Tk", tkinter.TkVersion)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    run("1. plain Tk", winutil.NullWin32(), root_withdrawn=True)
    run("2. real Windows styles", winutil.get_win32(), root_withdrawn=True)
    run("3. real Windows styles", winutil.get_win32(), root_withdrawn=False)
    print("\nDone. Did you see a red border + REC pill in 1, 2 and 3?")
