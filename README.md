# Screenium

A lightweight, desktop-environment-independent screen recorder for Linux
(Wayland or X11). It captures your full screen while it runs and saves a
timestamped MP4 when you stop it.

This is the base version — Screenium will grow into a proper command-line
tool with more features over time.

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python package manager)
- GStreamer with the `pipewiresrc` element and an H.264 encoder
  (`nvh264enc`, `vah264enc`, or `x264enc`)
- `xdg-desktop-portal` with a ScreenCast backend (provided by GNOME, KDE,
  Hyprland, Sway, etc.)

## Usage

```bash
uv sync                 # install dependencies (first time only)
uv run screenrecorder.py
```

When you start Screenium, your desktop environment shows a dialog asking you
to approve screen sharing. Click **Allow** to begin.

- **Ctrl+C** (or `kill`) stops the recording and saves it
- Output goes to `~/tmp/recording_YYYYMMDD_HHMMSS.mp4`
- The recording directory is auto-created if it doesn't exist

If the screen-share portal fails to respond, Screenium retries a few times
then exits with a clear message; run
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
README.md               this file
```

## Development

Dependencies are managed exclusively with `uv` (never `pip`):

```bash
uv add <package>        # add a dependency
uv run <script>         # run with the project environment
```
