# assets

- **`Wackro.ico`**: the app icon (.exe, window, taskbar). 16, 32, 48, 64, 128 and 256 px, every
  size taken pixel-exact from a hand-drawn 16x16 or 32x32 sprite.
- **`tray_idle.png`, `tray_recording.png`, `tray_playing.png`**: the system tray icon, with a gray,
  red or green dot.

All four are drawn by `python tools/make_icons.py` (needs Pillow). Edit the pixel maps in that
file and run it again to change them. `build.ps1` / `Wackro.spec` bundle them into the .exe.
Without them everything still works, with the default icon and plain coloured dots in the tray.
