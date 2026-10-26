# Wackro

*Record it once, loop it forever.* Made by Trixian Wackyll Granado.

A small Windows 10/11 desktop app that records your mouse and keyboard and plays it back, in the
spirit of ReMouse. Built with Python, CustomTkinter, pynput and pystray.

## Install and run

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

### First-time setup (fresh Windows PC)

1. Install **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/windows/).
   On the first installer screen tick **"Add python.exe to PATH"**. Also keep **"tcl/tk and IDLE"**
   ticked (it is by default): the window needs it.
2. Open a **new** PowerShell window and check: `python --version`. If it opens the Microsoft Store
   instead, turn off the `python.exe` / `python3.exe` entries under Settings > Apps > Advanced app
   settings > App execution aliases, then open a new PowerShell window.
3. `cd` into the project folder and run the four commands above.
4. If `Activate.ps1` is blocked ("running scripts is disabled on this system"), run
   `Set-ExecutionPolicy -Scope Process Bypass` in that window, then activate again. It only
   applies to that window.
5. Optional: run the tests (see **Tests**) and build the .exe (see **Building the .exe**).

Next time you only need `.\venv\Scripts\Activate.ps1` and `python main.py` (or just double-click
the built exe).

## How to use it

| Action | Button | Default hotkey |
|---|---|---|
| Record / stop recording (same key) | **● Record** / **■ Stop** | **F9** |
| Play / stop playback (same key) | **▶ Play** / **■ Stop** | **F11** |
| Panic stop (stops anything) | **■ Stop** | **F10** |

- **Your own hotkeys:** Settings > Hotkeys…, click a box and press the keys. The current keys are
  printed under the buttons and in the tray menu. While playing, the record key does nothing; while
  recording, the play key does nothing; the panic stop always works.
- **Hotkeys are swallowed** (on Windows): they only control Wackro and never reach the program
  you are in, so F11 doesn't make your browser go full screen (and if you pick NumLock, it doesn't flip
  your number pad).
  Only the main key is swallowed; Ctrl / Alt / Shift work normally. Keys that Wackro itself
  plays back never trigger a hotkey.
- **Hidden while it works:** when recording or playback starts, the window disappears completely
  (not in the taskbar, not in Alt+Tab) and comes back when it's done. The tray icon by the clock
  shows what's happening: gray dot = idle, red = recording, green = playing. Click it to show the
  window, right-click for Record / Play / Stop / Exit.
- **"It's recording" sign:** while recording, a thin red border goes around every screen and a
  small `● REC 0:12` pill sits at the top (green `▶ Run 2/5` while playing). It's click-through,
  never takes the focus, and doesn't show up in screenshots or screen recordings. Change it in
  Settings > Indicator (border, pill, both or off; and whether it shows while playing).
- **Questions from the tray:** if Wackro is hidden and needs to ask something (for example
  Save changes? when you press the record key), only that box appears, on top of everything and
  ready for Enter / Esc. Afterwards you're back in the program you were using.
- **Closing:** the X asks *Exit / Hide to system tray / Cancel* (tick "Don't ask again" to
  remember; change it later in Settings). Hidden in the tray, the hotkeys keep working.
- **Playback > Repeat…:** play it N times or Infinite (until you stop it), with an optional wait
  between runs. **Playback > Speed…:** 0.1x to 10x (presets 0.5x / 1x / 2x / 5x). The line under
  the buttons always shows what Play will do, e.g. `Repeat 3× · wait 0.5 s · 2x speed`.
- **Recording > Options…:** record the keyboard, mouse clicks (and scrolling), mouse movement,
  and the real delay between actions (without it, everything plays back to back).
- **Safety (failsafes), Settings > Safety…:** playback stops by itself when you take over (any
  key, a click or scroll, moving the mouse, or flinging it into a screen corner), when a popup or
  another program is in the way of a click or key (e.g. an error box like *"VAT rate value should
  be 10 or 12 only"*), or when a program crashed or stopped responding. The status line and a
  tray notification say why. Before playing, it warns if a program the recording used isn't
  open. Each check can be switched off; the mouse sensitivity is Low / Medium / High. The window
  and popup checks need recordings made with 1.5.0 or newer.
- **Blackout & power, Settings > Blackout & power…:** (off by default) after N minutes of
  playing with nobody touching the PC, the screen goes black (and the laptop brightness to 0)
  while playback keeps working underneath. Your first key / click / mouse move only wakes it up,
  it's never typed or clicked anywhere, and playback goes on. Optional status line on the black
  screen and an optional "black out now" hotkey. The PC is kept awake while playing (on by
  default). Brightness always comes back, even after a crash (on the next start).
- **Time limit, Settings > Safety…:** optionally stop playing after N minutes. The Safety box also
  stops playback when the PC gets locked, the screen resolution / monitors change, or the PC
  slept or froze.
- **Sequences, Sequence > New sequence…:** stack saved recordings into one playback. Each step
  has **Play** (on/off), a **name**, a **Pin** (Anywhere / Always first / Always last), **Times**
  (plays that many times in a row) and **Pause after** (`2` seconds, or `1-5` for a random 1 to
  5 s). Choose how the steps play: **in order**, **shuffled** (optionally the same shuffle every
  round) or **picked at random** (N random steps per round). Pinned steps always play at the
  start / end of every round. A sentence at the bottom of the window explains exactly what will
  happen, with an example round; hover any ⓘ for help. Set the rounds (or until stopped) and the
  pause between rounds, then **Use sequence** and press Play. Saved as a `.wseq` file that has
  copies of its recordings inside, so it works anywhere.
- **File:** New, Open… (recordings and sequences), Save, Save As…, Hide to tray, Exit.
- **Recordings folder, Settings > Recordings folder…:** every Save / Open box starts in this
  folder, `Documents\Wackro` by default (created when needed). Browse… picks another one, Reset to
  default goes back, Open folder shows it in File Explorer.
- **Unsaved recordings are never lost by accident:** recording again, New, Open or quitting asks
  *Save changes to the current recording? Yes / No / Cancel*. Yes saves first (the Save dialog
  opens the first time), No throws it away, Cancel leaves everything as it was.
- Only one copy can run at a time (two copies would both grab the hotkeys). Starting a second one
  shows a message and exits.

## Good to know

- **Privacy:** a recording contains every key you pressed, including passwords, and a saved file
  stores them as plain text. Don't record while typing secrets, and don't share such files.
- **Hotkeys never end up in a recording.** If your record key uses a modifier (say Ctrl+`), stopping
  with it also removes that Ctrl press, so Ctrl isn't left held at the end. A plain ` (without Ctrl)
  is still recorded as typed text.
- **No tray?** If Windows can't show the tray icon, the window minimizes instead of hiding, so
  you can always get back to it.
- **Admin windows:** Windows doesn't let a normal program control an elevated one. Playback into
  such a window fails (the status line says some events couldn't be played). Run Wackro as
  administrator if you need that.
- **Screen size:** clicks are recorded as absolute positions. Loading a recording made on a
  different screen size shows a warning, and clicks may land in the wrong place.
- **Limits:** a recording stops by itself at 200,000 events or 24 hours.
- **Endless loops:** the recording keeps controlling your mouse and keyboard until you press your
  play key or **F10** (or Stop in the tray menu), or a failsafe stops it (with the take-over
  checks on, just touch the mouse or a key). Try a new recording with a small Repeat first.
- **What a recording knows about your programs:** since 1.5.0 each click / key notes the program
  file name and window type (`notepad.exe`, `Notepad`) so playback can check it's still there.
  Window titles are never saved.
- **Playback timing** is as accurate as Windows sleep allows, so expect small jitter.

## Files

- Recordings: JSON, one event per line, so they're easy to read and diff. The format is described
  at the top of `storage.py`. Sequences (`.wseq`): JSON with a copy of each recording, see `sequence.py`.
- Settings (repeat, speed, recording options, hotkeys, what the X does, safety):
  `%LOCALAPPDATA%\Wackro\settings.json`. Delete it to reset everything.
- Log file: `%LOCALAPPDATA%\Wackro\wackro.log` (rotating, about 1 MB x 3). Look here first if
  something goes wrong.

## Tests

```powershell
python tools\run_tests.py
```

(Same as `python -W error::ResourceWarning -m unittest discover -s tests`, but a test stuck for
more than 60 seconds is reported with where it's stuck, instead of freezing.)

The GUI tests open a window briefly and skip themselves if tkinter or a display isn't available.

## Project layout

```
config.py          constants (hotkeys, limits, paths)
storage.py         recording format: validation, save, load
recorder.py        global input recorder
player.py          playback (single run, Repeat, delay between runs, speed)
settings.py        remembers repeat, speed, recording options, hotkeys
hotkey_spec.py     hotkey bindings: names, validation, matching
hotkeys.py         global hotkeys (swallowed on Windows)
tray.py            system tray icon
overlay.py         the on-screen recording / playing border and pill
failsafe.py        playback failsafes (you take over, popups, wrong program, crashes, lock)
blackout.py        the black screen while playing
brightness.py      laptop brightness down / back (with crash recovery)
sequence.py        sequences: the .wseq file, shuffling, locks, random waits
winutil.py         Windows helpers (monitors, click-through windows, focus)
ui_dialogs.py      the dark dialog boxes (Speed, Repeat, Options, Hotkeys, Safety, Blackout, Sequence, Close, About)
single_instance.py one-copy-at-a-time guard
app.py             the CustomTkinter window
main.py            entry point (logging, DPI awareness, error boxes)
Wackro.spec    PyInstaller recipe (normal and admin exe)
build.ps1          builds both exes
assets/            Wackro.ico and the tray icons (tray_*.png)
tools/             make_icons.py: redraws the pixel-art icons
tests/             unit and GUI tests
```

## Building the .exe

Two single-file Windows executables, built with PyInstaller from `Wackro.spec`:

| File | What it is |
|---|---|
| `dist\Wackro.exe` | The normal app. No UAC prompt. |
| `dist\Wackro-Admin.exe` | The same app, but it always asks for administrator rights (UAC). Use it if you need to control elevated windows. |

```powershell
.\venv\Scripts\Activate.ps1
.\build.ps1                  # installs the build requirements, runs the tests, builds both exes
.\build.ps1 -Only Normal     # just Wackro.exe
.\build.ps1 -Only Admin      # just Wackro-Admin.exe
.\build.ps1 -SkipTests       # skip the unit tests
.\build.ps1 -Installer       # also dist\Wackro-Setup-<version>.exe (needs Inno Setup 6, jrsoftware.org)
```

If PowerShell refuses to run the script, run `Set-ExecutionPolicy -Scope Process Bypass` first. The
script prints each exe's size and SHA-256 at the end and never touches git. `build\` and `dist\`
are git-ignored; `Wackro.spec` is committed.

By hand, without the script: `pip install -r requirements-build.txt`, then
`python -m PyInstaller --noconfirm --clean Wackro.spec`. Set `$env:WACKRO_ADMIN = "1"` first
to get the admin build.

**Icons:** `assets\Wackro.ico` is the .exe, window and taskbar icon, and `assets\tray_*.png`
are the tray icons (gray / red / green dot). They are hand-placed pixel art drawn by
`python tools\make_icons.py`; edit the pixel maps in that file and run it again to change them.

**Antivirus and SmartScreen:** unsigned one-file PyInstaller exes are flagged by some antivirus
programs, and Windows SmartScreen may say "Windows protected your PC" on first run (More info >
Run anyway). That is a known side effect of unsigned, self-extracting exes, not a sign that
something is wrong. Building the exe yourself from this source, and checking the SHA-256 the build
script prints, is the safest way to know what you're running. If a scanner keeps complaining, add an
exception for the `dist` folder. Code signing would fix it properly but isn't set up.

**Normal and admin together:** the two exes share one single-instance lock, so starting one while
the other is running shows "already running".

**First start is slower** than `python main.py`: a one-file exe unpacks itself to a temporary
folder every time.
