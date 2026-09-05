# Screenium

A lightweight, desktop-environment-independent screen recorder for Linux
(Wayland or X11). It captures your full screen while it runs and saves a
timestamped MP4 when you stop it.

This is the base version — Screenium will grow into a proper command-line
tool with more features over time.

## Requirements

- GStreamer with the `pipewiresrc` element and an H.264 encoder
  (`nvh264enc`, `vah264enc`, or `x264enc`)
- `xdg-desktop-portal` with a ScreenCast backend (provided by GNOME, KDE,
  Hyprland, Sway, etc.)
- [uv](https://docs.astral.sh/uv/) — installed automatically if missing

## Install

Screenium installs as a global `screenium` command from a single clone.

### Linux / macOS

```bash
git clone https://github.com/samTheComputerArchitect/Screenium.git
cd Screenium
./install.sh
```

The installer:

- Installs `uv` if it isn't already present
- Creates the project environment (`uv sync`)
- Installs the `screenium` command into `~/.local/bin` (and adds it to
  your PATH / shell rc file if needed)

Open a new terminal, then:

```bash
screenium record
```

### Windows

```bat
git clone https://github.com/samTheComputerArchitect/Screenium.git
cd Screenium
install.bat
```

This installs `screenium.cmd` into `%USERPROFILE%\.local\bin` and adds it to
your user PATH. Open a new terminal and run `screenium record`.

## Usage

Screenium is a small CLI with three subcommands:

### `screenium record`

Starts a recording. On **first run** it asks where recordings should be
saved; your answer is remembered for future runs.

When recording starts, your desktop environment shows a dialog asking you to
approve screen sharing. Click **Allow** to begin.

- **Ctrl+C** (or `kill`) stops the recording and saves it
- Output goes to `<save location>/recording_YYYYMMDD_HHMMSS.mp4`
- The recording directory is auto-created if it doesn't exist

### `screenium path`

Shows the current save location, or changes it:

```bash
screenium path              # show current save location
screenium path ~/Videos     # save recordings to ~/Videos
```

### `screenium stop`

Stops an active recording and saves it to disk. This can be called from any
terminal, even if `screenium record` is not running in the current shell.

### System tray indicator

While a recording is active, a red dot appears in your system tray. It
disappears when the recording is stopped. The tray icon has a "Stop
Recording" item that does the same thing as `screenium stop`.

Settings are stored in `~/.config/screenium/config.json`.

If the screen-share portal fails to respond, Screenium retries a few times
then exits with a clear message; on Linux run
`systemctl --user restart xdg-desktop-portal xdg-desktop-portal-hyprland`
and try again.

## How it works

Screenium uses the **freedesktop ScreenCast portal** to obtain a PipeWire
video stream — the same standard interface used by OBS Studio and GNOME's
screen recorder. Because it talks to the portal rather than any specific
desktop environment, it works the same way on GNOME, KDE, Hyprland, Sway,
and other compositors.

The PipeWire stream is piped through GStreamer and encoded to H.264 in real
time, using hardware acceleration (NVENC) when available.

## Project layout

```
pyproject.toml          uv project metadata & dependencies
screenrecorder.py       the recorder entry point
screenium               Linux launcher for the `screenium` command
screenium.cmd           Windows launcher for the `screenium` command
install.sh              Linux/macOS installer
install.bat             Windows installer
README.md               this file
```

## Development

Dependencies are managed exclusively with `uv` (never `pip`):

```bash
uv add pystray pillow    # add tray needed dependencies
uv run python3 screenrecorder.py record   # run recorder directly
```