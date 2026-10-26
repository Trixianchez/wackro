# Wackro

**Automation that knows when to stop.**

Wackro is a Windows mouse and keyboard recorder and player. You record a task once, and Wackro plays it back with exact timing. What makes it different is that it **stops itself the moment something goes wrong**: a surprise popup, the wrong program in front, a hung app, a locked or sleeping PC. That makes it safe to leave running on repetitive work like form entry.

## Download

Grab the latest version from **[Releases](../../releases/latest)**.

| File | Use it when |
|---|---|
| `Wackro-Setup-<version>.exe` | **Most people.** Installs Wackro for you or for all users, adds Start menu shortcuts (plus "Wackro (Administrator)"), asks where to save recordings, and uninstalls from Settings > Apps. |
| `Wackro.exe` | Portable: no install, just download and run. |
| `Wackro-Admin.exe` | Portable, always asks for admin rights (UAC). Only for playing into programs running as administrator. |

### "Windows protected your PC"?

Wackro isn't code-signed yet, so Windows SmartScreen warns about it. Click **More info → Run anyway**.

### Antivirus warning?

Wackro uses a global keyboard and mouse hook so it can record your input and listen for its hotkeys. Keyloggers use the same kind of hook, so some antivirus tools flag it by mistake. Wackro:

- records **only** while you press Record, and only to a file you choose
- never saves window titles, which can contain document names, only the program's file name
- never sends anything over the internet

Each release lists its **SHA-256 hashes**, so you can check you got the exact file. In PowerShell:

```powershell
Get-FileHash .\Wackro.exe
```

## Full guide

Everything about every menu, setting and failsafe is in the **[User Guide](USER_GUIDE.md)**, including how to run from source and build the exes yourself.

## Hotkeys

| Key | Does |
|---|---|
| `F9` | Start / stop recording |
| `F11` | Start / stop playback |
| `F10` | Panic stop: stops everything immediately |

All three can be changed in Settings > Hotkeys. On laptops where the F-keys need **Fn**, hold Fn or turn on Fn Lock. Wackro swallows them, so they never reach the program underneath, and they never end up inside a recording.

## Features

- **Record and play:** global mouse and keyboard capture with exact-timing playback. Speed runs from 0.1x to 10x, repeat N times or forever, with an optional wait between runs.
- **Failsafes:** playback stops when you touch the mouse or keyboard, a popup appears, the wrong program is in front, a program hangs or crashes, the PC locks or sleeps, or the screen changes. There's also an optional time limit and a "program not open" check before playback starts.
- **Sequences:** chain several recordings into one playback. Play them in order, shuffle them, or pick at random. Pin steps first or last, set repeats per step, and add fixed or random pauses between steps.
- **Indicator:** a red (recording) or green (playing) border on every monitor plus a REC pill. It's click-through and hidden from screenshots.
- **Tray mode:** the window leaves the taskbar and Alt+Tab while working, and a tray dot shows the state (grey, red or green).
- **Blackout:** after N idle minutes of playback, the screen goes black and brightness drops to 0. The first touch only wakes it, so it doesn't interrupt anything. A keep-PC-awake option is included.

## Where your stuff lives

Recordings (`.json`) and sequences (`.wseq`) go to `Documents\Wackro` by default; change it in **Settings > Recordings folder…**. Settings and the log live in `%LOCALAPPDATA%\Wackro`.

## Requirements

Windows 10 or 11, 64-bit.

To run from source: Python 3.10+, then `pip install -r requirements.txt` and `python main.py`. To build `Wackro.exe` and `Wackro-Admin.exe` yourself, run `.\build.ps1` (details in the [User Guide](USER_GUIDE.md)).

---

Made by Trixian Wackyll Granado. All rights reserved.
