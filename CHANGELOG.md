# Changelog

All notable changes to Wackro (called PyReMouse, then Wackylloop, before 1.7.0). Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [1.7.5] - 2026-10-26

### Added
- **Recordings folder** (Settings > Recordings folder…): every Save / Open box (recordings,
  sequences, Add recordings) now starts in one folder, `Documents\Wackro` by default. Pick your
  own with Browse…, go back with Reset to default, or open it in File Explorer. Documents is asked
  from Windows, so a OneDrive-moved Documents works. The folder is created when needed; if it
  can't be (missing drive, no rights), the box starts in Documents instead. New `folders.py`.
- Ready for the installer: a recordings folder chosen in Setup (`install.json` next to the exe) is
  used as the default.

### Fixed
- **Recordings were saved next to the exe** (e.g. in Downloads): the Save / Open boxes had no
  starting folder, so Windows used the folder Wackro was started from.

## [1.7.4] - 2026-10-26

### Changed
- **New default hotkeys: F9 = Record / Stop, F11 = Play / Stop** (F10 = panic stop as before),
  easier for new users than Ctrl+` and NumLock. Only the defaults changed: anyone who already has
  a settings.json keeps their own keys, and Settings > Hotkeys > Reset to defaults now gives F9 /
  F11 / F10. The modifier tests (Ctrl+` drops its Ctrl press, NumLock is swallowed) still run, on
  an explicit Ctrl+` / NumLock binding.
- README rewritten as a download page; the full guide moved to USER_GUIDE.md.

## [1.7.3] - 2026-10-23

### Fixed
- **Pressing Record twice could show "Save changes?" twice, or record nothing.** While a box was
  open, no hotkey was handled at all; they piled up and all ran when the box closed, so a second
  press cancelled the recording the box had just let start ("Cancelled."), or (after Cancel)
  opened the box again. Hotkeys are now handled while a box is open: Record / Play bring the box
  back to the front instead, and F10 (panic stop) works at once. Reproduced before the fix and
  checked after.
- **Wackro got slower (and used more memory) the longer it was open.** CustomTkinter keeps every
  window it ever saw (each menu you opened, each box) in a list it checks 10 times a second, and
  never removes closed ones: after 1,000 menus / boxes that was 701 dead windows and 57 MB. Closed
  windows are now taken out of that list at once, and a sweep every 30 s catches anything left.
  Measured after 1,000 menus and boxes: 1 window, 0.1 MB, the check 130x cheaper.
- Tray > Exit while a box is open brings that box forward instead of stacking a second one.

### Changed
- A double tap of the Record / Play hotkey within 0.25 s counts as one press.
- The window only redraws a label when its text changed (it rewrote three of them 10 times a
  second), and refreshes 4 times a second while idle (10 while recording / playing): the idle
  refresh is about 50x cheaper.

## [1.7.2] - 2026-10-17

### Changed
- **"Save changes?" is urgent and works from the keyboard.**
  - Gold warning bar and a ⚠ heading; always on top and takes the keyboard focus, even when the
    main window is open; plays the Windows warning sound and flashes on the taskbar.
  - **← → (or Tab) to choose, Enter or Space to confirm**, or press **Y / N / Esc**. The chosen
    button has a white ring; Yes is chosen to start. A hint line under the question says so.
- The Close box (Exit / Hide to tray / Cancel) works the same way: ↑ ↓ + Enter, or E / H / Esc.

## [1.7.1] - 2026-10-17

### Changed
- **The Sequence window explains itself.**
  - Plain column names: **Play** · **Step name** · **Pin** · **Times** · **Pause after**, each with
    a hover tip (ⓘ), as do every button and option.
  - **Pin** replaces Lock: Anywhere / **Always first** / **Always last**. Pinned steps play at the
    start / end of EVERY round, whatever the order. (Lock tied a step to a row number, so with
    "Random picks" a locked step could silently never play.)
  - "How should the steps play?": three plain choices, each with a description: **Play in
    order**, **Shuffle** (+ "keep the same order every round", which replaces "Shuffle once")
    and **Pick at random** (N random steps per round; pinned steps always play on top).
  - A live sentence describes what will happen ("Each round: Login first, then 2 random steps
    from Fill form and Export, then Logout last. Repeats 3 rounds, 2–5 s between rounds."),
    plus an **example round** with a "↻ Another example" button, and warnings in plain words
    (e.g. everything pinned with Pick at random).
  - Empty pause boxes say "no pause".
- Sequence files are now version 2. Version-1 files from 1.7.0 still open: a locked step in the
  top half of the list becomes "Always first", one in the bottom half "Always last"; Shuffle once
  becomes Shuffle + "keep the same order". Saving writes version 2.

## [1.7.0] - 2026-10-17

### Changed
- **Renamed to Wackro.** The exes are now `Wackro.exe` / `Wackro-Admin.exe`, built from
  `Wackro.spec`, with `assets/Wackro.ico`. Settings and logs moved to `%LOCALAPPDATA%\Wackro`; on
  the first start your settings (and a pending brightness restore) are copied over from the old
  `%LOCALAPPDATA%\Wackylloop` folder, never overwriting anything. Recordings didn't change.

### Added
- **Sequences** (new **Sequence** menu, `sequence.py`): several saved recordings played as one
  playback.
  - The **Sequence window**: a list of steps, each with on/off, name, **lock** (keeps its place
    when shuffling, e.g. Login always first), **repeat ×N**, and a **wait after**: fixed (`2`) or
    a random range (`1-5`) seconds. Move up / down, remove, **Add recordings…** (several at once)
    and **Add current recording**.
  - **Order:** In order, Shuffle every round, Shuffle once, or Random picks (each slot picks any
    step, so one can come up twice; set how many picks per round). Plus **rounds** (or ∞) and a
    fixed or random **wait between rounds**.
  - Saved as one `.wseq` file with **a copy of every recording inside**, so it still works when
    moved or shared. Checked as strictly as recordings when opened. File > Open opens both kinds.
  - Plays through the same player: every failsafe, the blackout, keep-awake, the time limit and
    the hotkeys work across the whole sequence. A failsafe stop names the step
    (`Playback stopped in step "Export": …`). The pre-flight check covers every step's programs.
  - The window, the indicator pill and the blackout line show `▶ Round 2/∞ · 3/5 Export · 1:12`.
  - "Save changes to the current sequence?" when it would be replaced or on exit.
  - Sequence > "Play: current recording / Play: sequence" picks what Play (and Save) use.
- `Player.start(..., next_round=)`: each run can be different (a new shuffle, new random waits);
  `Player.run_position`.

### Fixed
- Tests could freeze forever on Windows (seen in TestPlayback): the test helper that waits for
  the window ended Tk's main loop with a timer, and an error inside that timer (e.g. a
  ResourceWarning raised by the garbage collector) killed it. It's now a plain loop with a hard
  time limit, and any error inside a Tk callback during the tests is printed instead of hidden.
  It handles events in small batches and re-checks after each one (on Windows there is almost
  always another event waiting, so the first version waited out every timeout: 496 s runs).
- Tests: CustomTkinter's own timers left behind by a closed test window no longer fire during a
  later test ("application has been destroyed" noise on Windows); they're cancelled first.
- New `tools/run_tests.py` (used by build.ps1): a watchdog stops any test stuck for more than 60
  seconds and prints exactly where it's stuck, so a hang can never freeze the build again.
- File > Open now asks "Save changes?" after you pick the file (it only asks about the kind of
  thing the file would replace).

## [1.6.0] - 2026-10-16

### Added
- **Blackout while playing** (`blackout.py`, Settings > Blackout & power…, **off by default**):
  after N minutes of playing (default 5) with nobody touching the PC, every monitor fades to
  black and playback keeps working underneath. It's a black cover, not "monitor off": Windows
  wakes a sleeping monitor on any input, including the player's own clicks, so a real monitor-off
  would light straight back up. The cover lets clicks through, never takes the focus, isn't in
  Alt+Tab and doesn't show in screenshots / screen recordings.
  - **Brightness 0** on the laptop screen as well (`brightness.py`, Windows' own WMI brightness),
    put back afterwards. The old level is noted on disk first, so even a crash can't leave the
    screen dark: the next start restores it.
  - **Your first key, click or mouse move only wakes it up**: it's swallowed (never typed or
    clicked into the program being played into), playback keeps going, and the take-over
    failsafes wait 2 seconds before they count anything. Your Play / Stop hotkeys always work.
  - Optional small status line on the black screen (`▶ Run 3/10 · 12:04`), on by default.
  - Optional **"black out now" hotkey** (not set by default): blacks out at once, or wakes it.
  - It never blacks out if nothing could notice you touching the PC.
- **Keep the PC awake while playing** (on by default): no sleep / auto-lock during a long run.
- **More failsafes** (Settings > Safety…, on by default): the PC got locked or a UAC /
  Ctrl+Alt+Del screen is up; the screen resolution or the monitors changed; the PC slept or froze
  (playback fell more than 5 s behind schedule, instead of firing everything it missed at once).
- **Optional time limit** (Settings > Safety…, off by default): stop after N minutes.

### Changed
- Indicator border / pill step aside while the blackout is up.
- `Player.start(..., max_lag=)`; `InputWatch.start(..., track=, on_wake=, mod_vks=)`;
  `Hotkeys.set_extra()` for optional extra hotkeys.

## [1.5.0] - 2026-10-16

### Added
- **Playback failsafes** (`failsafe.py`, Settings > Safety…, all on by default, each one can be
  switched off). Playback stops, and the status line plus a tray notification say why, when:
  - **you take over:** you press any key, click, scroll, move the mouse yourself (sensitivity
    Low / Medium / High = about 120 / 40 / 15 px in one go) or fling it into a screen corner.
    Only real input counts: what the player itself types and clicks is marked by Windows and
    ignored, and so are the player's own mouse moves. Your Play / Stop hotkeys keep working as
    before.
  - **a popup is in the way:** a standard Windows message box (like BIR's "VAT rate value should
    be 10 or 12 only") is in front of where keys were going, or has blocked the window a click
    was meant for. The box's message is shown in the status line.
  - **the wrong program is there:** the click / keys were recorded in one program and another one
    is there now (it crashed, closed, or Alt+Tab landed somewhere else). A window that's still
    opening gets half a second to show up first.
  - **a program crashed or froze:** "not responding", or Windows' crash report is open. This one
    also works with old recordings.
  - **before playing:** if a program the recording used isn't open, it asks "Play anyway?".
- Recordings now note which program each click, scroll and key press went to: the program file
  name and window type only (like `notepad.exe` / `Notepad`), **never window titles**. That's
  recording format version 2; recordings without these notes are still saved as version 1, and
  every older recording still opens and plays.
- Tray notifications (`Tray.notify`).

### Changed
- `Player.stop(reason)` and the new `Player.halt(reason)` (stops without waiting, safe inside a
  Windows input hook); `Player.start(..., guard=, on_cursor=)` for the failsafes.
- Settings file: new `fs_*` keys (see settings.py).

### Fixed
- The player checked every recording as format version 1 before playing, so a version 2
  recording would have been refused.

## [1.4.0] - 2026-10-16

### Added
- **On-screen indicator** (`overlay.py`): a thin red border around every monitor and a small
  `● REC 0:12` pill at the top of the main one while recording; green with `▶ Run 2/5 · 0:07`
  while playing. Click-through (clicks land on the program underneath, so nothing extra is
  recorded), never takes the focus, not in Alt+Tab or the taskbar, always on top, and hidden
  from screenshots / OBS / screen recorders (Windows 10 2004+). The pieces are built once at
  start-up and only moved on and off screen, because showing a new window could take the
  keyboard focus away from the program being recorded.
- **Settings > Indicator:** Border + REC pill (default) / Border only / REC pill only / Off, and
  "Indicator while playing". Remembered in settings.json.
- `winutil.py`: the Windows calls behind it (monitors, click-through window styles, capture
  exclusion, bringing a box to the front with the focus). Harmless no-ops on other systems.

### Fixed
- Windows: the indicator's pieces were parked off-screen at -32000 while idle. That's the
  position Windows uses for minimized windows, so they were treated as minimized and never moved
  back on screen. They now park at -10000. Found by the test suite on a real Windows PC.
- Windows: the border never appeared (the REC pill did). Tk applied the border strips' new size
  but ignored their new position, so they stayed parked off-screen. Found with
  tools/diag_overlay.py on a real Windows PC. Every overlay piece is now also moved with
  SetWindowPos (top-most, without activating it), and every Windows call declares its exact
  argument types so 64-bit window handles and HWND_TOPMOST are passed correctly.
- The indicator could jump back off-screen right after being shown: a size change left pending
  while it was parked was applied after the move and dragged it back. Every pending change is
  now applied at once while parking and after each move. New regression test runs the full event
  loop between showing and checking.
- `tools/diag_overlay.py`: shows the indicator for a few seconds and prints where Windows put
  each piece, for troubleshooting.
- Tests: the "box opens on the monitor with the mouse" test read the box's position before
  Windows had applied it; it now checks the position the app asks for.

### Changed
- **Questions asked while Wackylloop is hidden in the tray** (Save changes? when you press the
  record key, Exit with an unsaved recording, Save As, the keystroke warning) now appear on their
  own, centered on the screen with the mouse, always on top and with the keyboard focus, so
  Enter / Esc answer them at once. The main window no longer pops up for them, and afterwards
  the focus goes back to the program you were in. Windows normally stops background programs
  from coming to the front; Wackylloop uses the usual AttachThreadInput workaround (plus an Alt
  tap if Windows still refuses), and the box is on top either way.


## [1.3.0] - 2026-10-15

Phase 1: your own hotkeys, the system tray, and a new dark window with menus.

### Added
- **Custom hotkeys** (Settings > Hotkeys…): click a box, press the keys. Defaults are
  **Ctrl+`** = Record / Stop (one key toggles recording), **NumLock** = Play / Stop (toggles
  playback) and **F10** = panic stop. Stored by Windows key code, so they don't depend on the
  keyboard layout. New `hotkey_spec.py` (names, validation, matching).
- **Swallowed hotkeys** on Windows: a low-level keyboard hook keeps the hotkey's main key away
  from other programs (NumLock no longer flips the number pad, Ctrl+` no longer reaches the
  focused app). Modifiers are never swallowed; keys sent by Wackylloop's own playback never
  trigger a hotkey.
- **System tray icon** (`tray.py`, pystray): gray / red / green dot for idle / recording / playing.
  Click to show the window; right-click for Show / Record / Play / Stop / Exit (with your keys).
- **Hidden while working:** recording and playback now withdraw the window (gone from the taskbar
  and Alt+Tab) instead of minimizing it, and bring it back afterwards only if it was open. Without
  a tray it falls back to minimizing.
- **Close prompt:** the X asks Exit / Hide to system tray / Cancel, with "Don't ask again"
  (also in Settings).
- **Dark UI with a menu bar** (`ui_dialogs.py`): File (New, Open…, Save, Save As…, Hide to tray,
  Exit), Playback (Speed…, Repeat…), Recording (Options…), Settings (Hotkeys…, close button),
  Help (About, Open log folder). Big Play / Record / Stop buttons with the hotkeys under them, and
  a line that says what Play will do (`Repeat 3× · wait 0.5 s · 2x speed`).
- **Playback > Speed…** in the window (0.1x to 10x, presets 0.5x / 1x / 2x / 5x).
- **Recording > Options…:** record keyboard / mouse clicks and scrolling / mouse movement / real
  delays (without delays, events are spaced 20 ms apart).
- **File > Save** saves straight back to the file it came from.
- **"Save changes to the current recording?" Yes / No / Cancel** (like ReMouse) before anything
  replaces an unsaved recording: a new recording (button, hotkey or tray), File > New, File >
  Open and quitting. Yes saves first (Save As the first time; if that is cancelled nothing is
  lost), No throws it away, Cancel does nothing. If the window is hidden in the tray when you
  press the record hotkey, it is shown for the question and hidden again on Cancel.
- **New icon:** a hand-placed pixel-art sprite (hair, round glasses, collar, red REC dot), separate
  16x16 and 32x32 drawings, every .ico size pixel-exact. `tools/make_icons.py` draws it and the
  tray icons.

### Changed
- Repeat / Infinite / Delay moved from the main window to Playback > Repeat… (same rules).
- Stopping a recording with a hotkey that uses modifiers (Ctrl+`) also removes that modifier's
  press from the recording, so Ctrl isn't left held at the end.
- The recorder only leaves out a key when the whole combination is a hotkey: a plain ` is typed
  text and is recorded.
- The player no longer skips F9 / F10 / F11 found in a recording; hotkeys ignore played-back keys.
- New dependencies: `pystray` and `Pillow`. Version is now 1.3.0.

### Fixed
- Work handed from other threads (hotkeys, tray, recorder, player) to the window no longer calls
  tkinter from those threads. Such a call blocks until the window's thread is free, so a hotkey
  pressed while the window was busy (for example while a recording was being stopped) arrived late
  or every other press seemed lost, and on Windows it could stall the keyboard hook long enough
  for Windows to drop it. The work now goes into a queue that the window empties every 15 ms.
  Found by an end-to-end test with real key presses.
- On Windows the hotkeys read the modifier keys' real state (GetAsyncKeyState) instead of only
  tracking them. A keyboard hook never sees the key ups around Ctrl+Alt+Del / the lock screen, so
  after unlocking the PC Ctrl and Alt looked held forever and NumLock stopped matching.
- Windows: the menus (File, Playback, …) opened and closed again at once, so they seemed dead.
  CustomTkinter's popup windows hand the focus back to the main window ~10 ms after appearing
  (to colour their title bar), and the menu closed whenever it lost the focus. The menu is now a
  plain tkinter popup that never needs the focus: it closes on a click on an item or anywhere
  else, on Esc, or when you switch to another program. Dialog boxes take the focus back after
  that hand-off too, so Enter / Esc and the hotkey capture reach them.
- Tests: 5 tests failed on Windows only (found by running the suite on a real Windows PC). Two
  icon tests didn't expect CustomTkinter's own default-icon call on Windows, and three playback
  tests assumed the mouse has no X1 button (Windows has one). They now ignore CustomTkinter's call
  and fake a mouse without X1, so they test the same thing on every system.
- Tests: one GUI test started a real tray icon (it was missing the fake tray), which would pop
  up by the clock during build.ps1. A guard test now checks every test window uses fakes.
- Record / Play hotkeys are ignored while a dialog box is open (they would start behind the box);
  the panic stop always works.

### Removed
- The fixed F9 / F10 / F11 hotkeys (`config.HOTKEY_*`). Old settings files still load; the new
  fields start at their defaults.


## [1.2.0] - 2026-10-15

Renamed from PyReMouse to **Wackylloop**. Older entries below still use the old name.

### Changed
- The app is now called Wackylloop: window title, messages, the file type in Save / Load, docs.
- The exes are now `Wackylloop.exe` and `Wackylloop-Admin.exe`, built from `Wackylloop.spec`
  (was `PyReMouse.spec`). The admin build switch is now `WACKYLLOOP_ADMIN=1`.
- Settings and the log now live in `%LOCALAPPDATA%\Wackylloop\` (`wackylloop.log`). Settings from
  PyReMouse aren't carried over, so the repeat controls start at their defaults once.
- The single-instance lock is now `Local\Wackylloop`.
- New icon `assets\Wackylloop.ico` with the TW initials (replaces `PyReMouse.ico`).

### Unchanged
- The recording format. Recordings saved by PyReMouse load as they are.

## [1.1.1] - 2026-10-15

Ready to set up and build.

### Added
- App icon: `assets\PyReMouse.ico` (16 to 256 px), used for the .exe, the window and the taskbar.
- README: a "First-time setup" section (installing Python, the venv, the PowerShell execution
  policy) so a fresh Windows PC can go from zip to running app.

### Fixed
- Tests: `test_close_with_unsaved_recording_asks_and_can_be_cancelled` failed whenever it ran right
  after `test_close_while_waiting_cancels_the_pending_start`. The GUI test helper `pump()` ran the
  Tk main loop on an already-closed window, which left a timer behind that later called `quit()`
  during the next test (Tk's quit flag is shared by the whole process). `pump()` now only
  processes events by hand once the window is closed, and always cancels its timer. The app itself
  was fine, but the failure made `build.ps1` stop before building.
- Tests: `test_icon_is_applied_shortly_after_start` failed as soon as a real
  `assets\PyReMouse.ico` existed (the window's own start-up icon timer called `iconbitmap` a
  second time), so adding an icon as the README says also blocked `build.ps1`. The icon tests now
  build their window as if there were no icon, and pass with or without one.

## [1.1.0] - 2026-10-15

Playback repeat.

### Added
- Step 8: repeat controls in the window: **Repeat** (number of runs), **Infinite** (until Stop) and
  **Delay between runs (s)**. They are checked when Play is pressed, so a typo shows up in the
  status line instead of starting anything, and they are locked while recording or playing.
- Step 8: `Player.start(events, repeat=1, delay=0.0, speed=1.0)`. `repeat=0` means until `stop()`.
  Every run has its own absolute clock, keys and buttons still held at the end of a run are
  released before the next one, Stop is instant even during the pause between runs, and a run
  never takes less than 50 ms so an endless loop of a zero-length recording can't spin the CPU.
  `speed` (0.1x to 10x) scales all event times; there is no control for it in the window yet.
- Step 8: progress shows the run ("Run 3 / 10", "Run 3 / ∞"), and the finish message says how many
  runs completed or during which run Stop was pressed.
- Step 8: `settings.py` remembers the repeat controls between runs in
  `%LOCALAPPDATA%\PyReMouse\settings.json`. A missing or broken file falls back to the defaults
  field by field, and failing to save never blocks playing or closing.

### Changed
- Step 8: failed events are counted across runs, so an endless loop that can't play anything (for
  example into an elevated window) gives up after 25 failures in a row instead of looping forever.
- Step 8: the window is a bit taller (the repeat row); version is now 1.1.0.

## [1.0.0] - 2026-10-15

First release. Steps 1 to 6 (below) plus the packaging from step 7.

### Added
- Step 7: PyInstaller packaging. `PyReMouse.spec` builds `PyReMouse.exe` (normal) and, with
  `PYREMOUSE_ADMIN=1`, `PyReMouse-Admin.exe` (UAC manifest). Both are one-file, windowed builds
  that bundle CustomTkinter's themes and fonts and the right pynput backend.
- Step 7: `build.ps1` (installs, tests, cleans, builds one or both exes, prints size and SHA-256)
  and `requirements-build.txt`.
- Step 7: window and exe icon from `assets\PyReMouse.ico` (optional), `config.resource_path()` for
  finding bundled files from source or from the exe.
- Step 7: `.exe` section in WINDOWS_CHECKLIST.md and the README.

### Fixed
- Step 7: a normal copy started while the admin copy is running now counts as "already running".
  Windows answers "access denied" in that case, which the guard used to treat as a broken guard
  and start anyway (two copies fighting over F9-F11).

### Added
- Step 6: single-instance guard (`single_instance.py`): a second copy shows "already running" and
  exits instead of fighting over F9-F11. A broken guard never blocks startup.
- Step 6: README.md and WINDOWS_CHECKLIST.md.
- Step 5: CustomTkinter window (`app.py`) with Record / Stop / Play, live counter and timer,
  Save / Load, keystroke privacy warnings, screen-size mismatch warning, unsaved-recording prompts.
- Step 5: `main.py` with rotating log file, Windows DPI awareness and a startup-crash message box.
- Step 4: global hotkeys F9 / F10 / F11 (`hotkeys.py`) with a "playing" guard.
- Step 3: playback (`player.py`) with absolute-time scheduling, instant Stop and release of held
  keys and buttons.
- Step 2: recorder (`recorder.py`) with move throttling, hotkey filtering, tail trim and a 200k
  event cap.
- Step 1: constants (`config.py`) and the validated, atomic JSON save/load (`storage.py`).

### Changed
- Step 7: version is now 1.0.0.
- Step 7: `.gitignore` no longer ignores `*.spec`, so `PyReMouse.spec` is committed.
- Step 6: the startup-crash box now uses a hidden Tk root (no stray empty window), and the startup
  log line includes the version.
- Step 6: removed stale references in the `hotkeys.py` and `app.py` docstrings; documented that
  F9-F11 still reach the focused program.
