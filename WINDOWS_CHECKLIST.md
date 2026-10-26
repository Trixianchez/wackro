# Windows smoke test (Wackro 1.7.4)

Run through this on a real Windows 10/11 machine and note anything that looks off. Everything
below was NOT verified in the Linux sandbox the code was built in.

Setup: `pip install -r requirements.txt`, then `python main.py`.

The keys below are the defaults since 1.7.4: **F9** = Record / Stop, **F11** = Play / Stop, **F10** = panic stop.
If you changed them (Settings > Hotkeys…, or an older settings.json), use **Reset to defaults** first or read your own keys instead.
On a laptop where the F-keys need **Fn**, hold Fn (or turn on Fn Lock).

## 1. Basics (Notepad)
- [ ] The dark window opens: menu bar, Play / Record / Stop with `F11`, `F9`, `F10` under them. Stop and Play start dimmed.
- [ ] The tray icon (your pixel face, gray dot) is by the clock (it may be under the ^ arrow: drag it out).
- [ ] Open Notepad. Press **F9**: the window **disappears** (not in the taskbar, not in Alt+Tab), the tray dot turns **red**.
- [ ] Click in Notepad, type `hello`, move the mouse, **Alt+Tab** to another window and back. Wackro never shows up in Alt+Tab.
- [ ] Press **F9** again: recording stops, the window comes back, "Recorded N events" and "(not saved)".
- [ ] Clear Notepad, press **F11**: `hello` is typed again, the dot is **green** while it plays, the window returns with "Playback finished."
- [ ] **The hotkeys never reached Notepad or the browser**: F11 didn't make a browser go full screen, F9 / F10 didn't do anything in the focused program.
- [ ] Press F11 during a long playback: it stops. Press **F10** during playback or recording: it stops.
- [ ] F9 during playback and F11 during recording do nothing.

## 2. Save and load (File menu)
- [ ] **File > Save As…** warns about keystrokes. Confirm, save as `test.json`. Open it in an editor: readable JSON, one event per line, none of your hotkeys (F9, F10, F11) in it.
- [ ] File > Exit with an unsaved recording: it asks first. Cancel keeps it open.
- [ ] After Save As, change nothing and use **File > Save**: it saves to the same file without asking.
- [ ] Restart `python main.py`, **File > Open…** `test.json`, press **F11**: it plays back the same.
- [ ] Load a broken file (e.g. a `.txt` renamed to `.json`): a clear error box, the app keeps working.

- [ ] Record something, don't save, press **F9** again: "Save changes to the current recording?" **Cancel**: nothing records, the old recording is still there. **No**: it records over it. **Yes**: the Save dialog opens, then recording starts.
- [ ] Hide the app to the tray with an unsaved recording, press F9: the window pops up for the question; Cancel hides it again.

## 2a. Indicator and questions from the tray (1.4.0)
- [ ] Press F9 in Notepad: a thin **red border** goes around the screen (every monitor) and a **● REC 0:00** pill at the top counts up. Stop: both vanish at once.
- [ ] While recording, click and type *through* the border and pill area: Notepad gets the clicks; the border never takes the focus and isn't in Alt+Tab.
- [ ] Take a screenshot (Win+Shift+S) or record with OBS while recording: the border and pill are NOT in the capture.
- [ ] Play (F11): **green** border and **▶ Run 1/3 · 0:02**. Settings > "Indicator while playing" off: nothing while playing.
- [ ] Settings > Indicator: Border only / REC pill only / Off each do what they say.
- [ ] Hide Wackro to the tray with an **unsaved** recording, click into Notepad, press F9: ONLY the "Save changes?" box appears, **in front of Notepad**, and **Esc** cancels / **Enter** = Yes without clicking it first. The main window doesn't appear.
- [ ] Same, answer **No**: recording starts, and typing goes straight into Notepad (the focus came back).
- [ ] Same, answer **Yes** on a never-saved recording: the Save As box also appears in front.
- [ ] Tray > Exit with an unsaved recording: only the question appears, in front.

## 2a+. Failsafes (1.5.0) — make a NEW recording for these
- [ ] Record typing in Notepad (`aaaaaaaaaa` slowly, ~10 s). Set Repeat to 5. Play, then **press any letter**: it stops at once, status "Playback stopped: you pressed a key", and a Windows notification says the same.
- [ ] Play again, **move the mouse a bit** (a few cm): "you moved the mouse". A tiny nudge on Low shouldn't stop it; on High it should.
- [ ] Play again, **click** somewhere: "you clicked the mouse". Play again, **fling the mouse into a corner**: "into a screen corner".
- [ ] A recording with lots of mouse movement plays all the way through **without you touching anything** (the player's own moves must never trip it).
- [ ] F11 / F10 still stop it normally (status just "Playback stopped.").
- [ ] **Wrong program:** play the Notepad recording, and during the start delay Alt+Tab to another program (e.g. Chrome): it stops before typing, "keys were going to notepad.exe, but chrome.exe is in front". Nothing got typed into Chrome.
- [ ] **Program closed:** close Notepad, press Play: "This recording used notepad.exe, which isn't open right now… Play anyway?" Cancel = not played.
- [ ] **BIR popup:** record entering a value in BIR (one that's accepted). Play it back with a value that makes the *"VAT rate value should be 10 or 12 only"* box appear (or just leave that box open): playback stops, status says `a popup appeared in …: "VAT rate value should be 10 or 12 only"`. Nothing was clicked or typed into the popup.
- [ ] **Frozen program:** (optional) a program that shows "Not responding" during playback stops it with "… stopped responding".
- [ ] Settings > Safety…: switch "I press any key" off, play, press a letter: it keeps going (the letter types into Notepad). Switch it back on. Settings survive a restart.
- [ ] An OLD recording (made before 1.5.0) still opens and plays; the take-over checks still work with it.

## 2a++. Blackout & power (1.6.0)
- [ ] Settings > Blackout & power…: tick "Black out the screen", set **0.5** minutes, OK. Make a ~2 minute recording (typing in Notepad works), Repeat 3, play, **don't touch anything**.
- [ ] After 30 s the screen **fades to black** on every monitor and the **laptop brightness drops to 0**. A dim `▶ Run 1/3 · 0:31` line near the bottom. The red/green border is gone meanwhile.
- [ ] Wait a bit, then **move the mouse**: the screen comes back, brightness returns to what it was, **playback keeps going** (the status line doesn't say stopped). Notepad got nothing extra from your touch.
- [ ] Do the same but **press a letter** to wake it: the letter is NOT typed into Notepad, playback continues.
- [ ] After waking, touching the mouse again (more than 2 s later) stops playback as before ("you moved the mouse").
- [ ] Black out again, then press **F11 or F10**: playback stops, the screen and brightness come back.
- [ ] Set a "Black out now" hotkey (e.g. Ctrl+F12): during playback it blacks out at once; pressing it again wakes it.
- [ ] Brightness crash test (optional): black out, then kill Wackro in Task Manager. Brightness stays at 0. Start Wackro again: brightness comes back by itself.
- [ ] Desktop / external monitor: it still goes black (only the brightness part is skipped).
- [ ] **Keep awake:** set Windows to sleep after 1 minute (Settings > Power), play a long recording without touching anything: the PC doesn't sleep. After playback ends, it sleeps normally again. (Put your sleep setting back.)
- [ ] **Locked:** during playback press **Win+L**: playback stops ("the PC got locked…") before anything else is clicked. Unlock and check the status line.
- [ ] **Screen change:** during playback change the display resolution or unplug an external monitor: it stops ("the screen resolution or the monitors changed").
- [ ] **Sleep:** during playback close the laptop lid (or Start > Sleep), wake it after a minute: it stopped ("fell … behind schedule").
- [ ] **Time limit:** Settings > Safety… "Stop playing after 1 minutes", play on Infinite: it stops after a minute with "it reached the time limit of 1 min".

## 2a+++. Rename + sequences (1.7.0)
- [ ] The window, tray tooltip, About box and boxes all say **Wackro**. `dist\Wackro.exe` and `dist\Wackro-Admin.exe` are built.
- [ ] Your settings (hotkeys, speed, safety, blackout) are the same as before the rename.
- [ ] Record three short Notepad recordings typing `111`, `222`, `333` and save them as `one.json`, `two.json`, `three.json`.
- [ ] Sequence > New sequence… > **Add recordings…**, pick all three at once: three steps named one / two / three.
- [ ] The columns read Play · Step name · Pin · Times · Pause after. Hovering any ⓘ, button or option shows a short explanation. Empty pause boxes say "no pause".
- [ ] **Play in order**, Rounds 2, **Use sequence**, Play: Notepad gets `111222333111222333`. The pill says `▶ Round 1/2 · 2/3 two · …`.
- [ ] Pin **one** = Always first, **Shuffle**, Rounds 4: the sentence at the bottom says "Each round: one first, then two and three shuffled…". Every round starts with `111`; the other two change order. Tick "keep the same order every round": all rounds the same.
- [ ] **Pick at random**, 1: every round is `111` plus ONE random step (two or three). Pin **three** Always last: every round ends with `333`, it never goes missing.
- [ ] "↻ Another example" shows different example rounds; typing in boxes doesn't change the example by itself.
- [ ] Set Times 0 on a step: the sentence turns into "Step …: Times must be…", and OK refuses with the same message.
- [ ] Pause after `1-4` on two: the pause after `222` varies.
- [ ] Untick Play on a step: it never plays.
- [ ] A `.wseq` saved by 1.7.0 still opens (locked steps now show Always first / Always last).
- [ ] File > Save As… `test.wseq`, restart Wackro, File > Open… `test.wseq`: same sequence, plays the same. Move the .wseq to another folder (the .json files can be deleted): still opens and plays.
- [ ] During a sequence, press a letter: status says `Playback stopped in step "…": you pressed a key` with the right step.
- [ ] Record something new: Play now plays that recording; Sequence > "Play: sequence" switches back.
- [ ] Change the sequence and Exit: "Save changes to the current sequence?"
- [ ] Blackout + sequence: the black screen's status line shows the step.

## 2a++++. Keyboard boxes (1.7.2)
- [ ] Record something, don't save, click into Notepad, press F9: the "Save changes?" box pops up in front with a gold bar, a warning sound and a taskbar flash, and you can answer it WITHOUT clicking: → → Enter = Cancel, Enter = Yes, N = No, Esc = Cancel.
- [ ] Same with the Wackro window open (not in the tray): the box still comes to the front with the keyboard focus.
- [ ] The window's X (Close box): ↑ ↓ + Enter, or E (Exit) / H (Hide to tray) / Esc.

## 2a+++++. Double presses and long sessions (1.7.3)
- [ ] Unsaved recording, press F9 → the box. Press F9 AGAIN while it's open: no second box (it just comes to the front). Answer No: recording starts. Repeat and answer Cancel: the box closes and does NOT come back.
- [ ] With the box open, press F10: nothing breaks; answer the box, everything is normal.
- [ ] Tap F9 twice very fast while idle: it starts recording once (it doesn't start and cancel).
- [ ] Leave Wackro open for a few hours while opening menus / boxes now and then: it stays as snappy as at the start (Task Manager: its memory stays about the same).

## 2b. Repeat and speed (Playback menu)
- [ ] Record `hello`. **Playback > Repeat…**: 3 times. The line under the buttons says `Repeat 3× · 1x speed`. Clear Notepad, press F11: `hellohellohello`, "Playback finished (3 runs)."
- [ ] Bring the window back from the tray while it plays: "Run 2 / 3 ..." progress, Play and Record dimmed, Playback menu items greyed out.
- [ ] Repeat with **Wait 2 seconds**: about 2 s between runs, not after the last one.
- [ ] Repeat **Infinite**: let it run 3+ times, press F11 (or F10): "Playback stopped during run N." No key or mouse button stays stuck (check Shift, Ctrl).
- [ ] Press F10 during the wait between runs (use 5 s): it stops at once.
- [ ] Type `abc` in the Repeat box, or `-1` as the wait, and press OK: a red message, the box stays open.
- [ ] **Playback > Speed…** 2x: the recording plays twice as fast. 0.5x: half speed. `warp` is refused.
- [ ] Record something that ends with Shift held (hold Shift, click, stop while still holding). Play 2 runs: the second run starts clean, Shift isn't left down.
- [ ] Close and reopen: repeat, speed, recording options and hotkeys are what you last set. `%LOCALAPPDATA%\Wackro\settings.json` exists.
- [ ] Put garbage in `settings.json` (for example `{{{`) and start: defaults, no error box.
- [ ] Infinite loop of a short click recording for 10 minutes: CPU stays low, memory doesn't keep growing.
- [ ] Infinite loop into an elevated window with the normal exe: it gives up by itself after a few runs.

## 2c. Hotkeys box, recording options, tray, closing
- [ ] **Settings > Hotkeys…**: click Record / Stop, press **Ctrl+Shift+R**: the box shows it. Click Play / Stop and press **Ctrl+Shift+R** too: "already Record / Stop", refused. Esc cancels a capture. OK: the captions under the buttons and the tray menu show the new keys, and they work at once.
- [ ] While that box is open, pressing F11 does NOT start playback.
- [ ] **Reset to defaults** brings back F9, F11, F10.
- [ ] Set Record to **Ctrl+`** in Settings > Hotkeys. Record with Ctrl+` then stop with Ctrl+`, save, open the file: no `ctrl` press left at the end, no `` ` ``. Reset to defaults afterwards.
- [ ] Set Play to **NumLock**: pressing it plays and the NumLock light does NOT change. Reset to defaults afterwards.
- [ ] **Recording > Options…**: untick Mouse movement, record a few clicks: the file has no `mouse_move` lines except the first. Untick Delay: playback runs back to back.
- [ ] Tray: click the icon → the window shows. Right-click → Record / Stop, Play / Stop (with your keys), Stop, Exit all work.
- [ ] **X** → Exit / Hide to system tray / Cancel. Hide: the app is gone from the taskbar, the hotkeys still work, the tray brings it back. Start a recording with F9 while it is hidden: when you stop, the window stays hidden.
- [ ] Tick "Don't ask again" + Hide: the next X hides straight away. Settings > "Close button asks" brings the question back.
- [ ] After **Exit** the tray icon disappears (hover over where it was) and no Wackro process is left in Task Manager.

## 3. Display scaling (DPI)
- [ ] Settings > Display > Scale at **125%**: record a click on a specific button, play it back. It lands on the same button.
- [ ] Same at **150%**.
- [ ] The window itself isn't blurry or oversized.
- [ ] With two monitors (if you have them): record on the second one, play back. Note the result.
- [ ] Load a recording made on another screen size: the status line warns about the mismatch.

## 4. Hotkeys inside other apps
- [ ] F9 / F11 / F10 work while Notepad, a browser, VS Code and File Explorer are focused, and none of those programs reacts to them (the browser doesn't go full screen, VS Code doesn't start debugging).
- [ ] A plain ` (no Ctrl) still types in Notepad and is recorded as text.
- [ ] Show the window from the tray while recording: nothing breaks.

## 5. Admin windows
- [ ] Record clicks in a normal app, then play them over an elevated one (e.g. Task Manager). They should not work, and the status line should say some events couldn't be played.
- [ ] With an elevated window focused, the normal exe's hotkeys may not reach it (Windows rule). Note what happens.
- [ ] Run PowerShell as administrator, start `python main.py` from there: playback into the elevated window works.

## 6. Single instance
- [ ] With Wackro open, run `python main.py` again in a second terminal: a box says it's already running, and the second process exits.
- [ ] Close the first one, start again: it opens normally.
- [ ] Kill it from Task Manager, start again: it opens normally (the lock doesn't get stuck).

## 7. Log file
- [ ] `%LOCALAPPDATA%\Wackro\wackro.log` exists and has a "Starting Wackro 1.2.0" line per launch.

## 8. The .exe files
Build first: `.\build.ps1` (see README.md). Then, from `dist\`:
- [ ] `Wackro.exe` starts with **no console window** (not even a flash), and the window looks the same as with `python main.py` (theme, fonts, button colors).
- [ ] The window and taskbar show your icon (if you put `assets\Wackro.ico` in before building).
- [ ] Sections 1 and 2 above work the same from the exe (record in Notepad, save, load, play).
- [ ] Startup time: note how long from double-click to window (one-file exes unpack first).
- [ ] `Wackro-Admin.exe` shows the **UAC prompt**, then runs. Its window title bar or Task Manager shows it as elevated.
- [ ] From the admin exe: record clicks in a normal app, then play them over an elevated window (Task Manager). This time it works.
- [ ] Start `Wackro.exe` while `Wackro-Admin.exe` is running: a box says it's already running, and no second window appears. (Also try the other order.)
- [ ] SmartScreen / antivirus: note any warning. "More info > Run anyway" is expected for an unsigned exe. The SHA-256 printed by the build script lets you verify the file.
- [ ] The log file (`%LOCALAPPDATA%\Wackro\wackro.log`) is written by the exe too.

## Report back
Anything that failed or felt odd, with the log file if there was an error box.
