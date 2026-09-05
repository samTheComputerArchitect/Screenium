#!/usr/bin/env python3
"""Screenium - a lightweight, desktop-environment-independent screen recorder.

Works on any Wayland compositor (Hyprland, Sway, GNOME, KDE, ...) or X11
session by using the freedesktop ScreenCast portal to obtain a PipeWire
video stream, which is then encoded with GStreamer + hardware acceleration.

Commands:
    screenium record            start recording (prompts for save path on first run)
    screenium path              show the current save location
    screenium path <DIR>        set the save location to <DIR>
    screenium stop              stop the active recording and save it
    screenium pause             pause the active recording (without stopping)
    screenium resume            resume a paused recording
    screenium help              show this help
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

# PID file path for the running recording
RECORDING_PID_FILE = Path.home() / ".config" / "screenium" / "recording.pid"

# Set by the SIGINT/SIGTERM handler so every blocking wait can bail out fast.
STOP = threading.Event()

# Set by the SIGUSR1/SIGUSR2 handlers (pause/resume).
PAUSED = threading.Event()


class Config:
    """Persists Screenium settings in an XDG config file (~/.config/screenium)."""

    def __init__(self) -> None:
        config_home = Path(
            os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        )
        self.path = config_home / "screenium" / "config.json"
        self.data = {}
        self._load()

    def _load(self) -> None:
        try:
            self.data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")

    def get_save_dir(self) -> Path | None:
        raw = self.data.get("save_dir")
        return Path(raw).expanduser() if raw else None

    def set_save_dir(self, path: Path) -> None:
        self.data["save_dir"] = str(Path(path).expanduser().resolve())
        self._save()


class PortalCapture:
    """Drives the xdg-desktop-portal ScreenCast session over D-Bus.

    The session flow:
        CreateSession → SelectSources → Start → OpenPipeWireRemote

    Start() presents a dialog to the user asking to approve screen sharing.
    On success, the portal returns PipeWire stream node IDs.
    """

    def __init__(self, bus) -> None:
        import dbus

        self.bus = bus
        obj = bus.get_object(PORTAL_NAME, PORTAL_PATH)
        self.sc = dbus.Interface(obj, SCREENCAST_IFACE)
        self.session_handle: str | None = None
        self.stream_node_id: int | None = None
        self.pipewire_fd: int | None = None
        self._counter = 0

    def _token(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _call(self, method, *pos_args, options: dict, timeout: float = 20.0) -> tuple[bool, dict]:
        """Call a portal method that replies via a Request.Response signal.

        Portal methods return the Request object path immediately. The actual
        response (success/failure + results) arrives as a Response signal on
        that Request path. We wait up to ``timeout`` seconds.
        """
        handle_token = self._token("screenium_req_")
        options["handle_token"] = handle_token

        from gi.repository import GLib

        request_path = str(method(*pos_args, options))
        result: dict = {"ok": False, "results": {}, "timed_out": True}
        done = threading.Event()

        def on_response(response: int, results: dict) -> None:
            result["ok"] = response == 0
            result["results"] = results
            result["timed_out"] = False
            done.set()

        self.bus.add_signal_receiver(
            on_response, "Response", REQUEST_IFACE, PORTAL_NAME, path=request_path
        )
        GLib.timeout_add_seconds(
            int(timeout),
            lambda *_: done.set(),
        )

        self._pump_until(done, timeout + 2)
        self.bus.remove_signal_receiver(
            on_response, "Response", REQUEST_IFACE, PORTAL_NAME
        )

        if result["timed_out"]:
            if not STOP.is_set():
                print("Warning: portal did not respond in time.", file=sys.stderr)
            return False, {}
        return result["ok"], result["results"]

    @staticmethod
    def _pump_until(done: threading.Event, max_seconds: float) -> None:
        """Iterate the default GLib main context until an event is set.

        Uses context.iteration() instead of MainLoop.run() so we stay in
        control and never block in a way that swallows Ctrl+C.
        """
        from gi.repository import GLib

        context = GLib.MainContext.default()
        end = time.monotonic() + max_seconds
        while not done.is_set() and not STOP.is_set() and time.monotonic() < end:
            context.iteration(False)
            time.sleep(0.01)

    def _aborted(self) -> bool:
        return STOP.is_set()

    def start(self) -> bool:
        """Run the full portal handshake and obtain a PipeWire stream."""
        import dbus

        created = False
        for attempt in range(3):
            if STOP.is_set():
                return False
            ok, results = self._call(
                self.sc.CreateSession,
                options={"session_handle_token": self._token("screenium_sess_")},
                timeout=6.0,
            )
            if ok:
                created = True
                break
            if not STOP.is_set() and attempt < 2:
                print("Retrying portal session creation...", file=sys.stderr)
                time.sleep(1)
        if not created:
            if not STOP.is_set():
                print(
                    "Error: could not start a ScreenCast session. "
                    "Is a compositor / xdg-desktop-portal available?",
                    file=sys.stderr,
                )
            return False
        self.session_handle = results["session_handle"]

        ok, _ = self._call(
            self.sc.SelectSources,
            self.session_handle,
            options={"multiple": False, "types": dbus.UInt32(1)},
        )
        if not ok:
            print("Error: source selection failed.", file=sys.stderr)
            return False

        print("Approve screen sharing in the dialog that appears...")
        ok, results = self._call(
            self.sc.Start, self.session_handle, "", options={}
        )
        if not ok:
            print(
                "Error: screen sharing was denied or timed out.\n"
                "  Make sure you approve the screen share prompt.",
                file=sys.stderr,
            )
            return False

        streams = results.get("streams")
        if not streams:
            print("Error: no video streams returned.", file=sys.stderr)
            return False

        self.stream_node_id = streams[0][0]

        fd = self.sc.OpenPipeWireRemote(
            self.session_handle, dbus.Dictionary({}, signature="sv")
        )
        self.pipewire_fd = fd.take()
        return True

    def close(self) -> None:
        """Close the portal session so the compositor can release resources."""
        if self.session_handle:
            try:
                import dbus

                obj = self.bus.get_object(PORTAL_NAME, PORTAL_PATH)
                iface = dbus.Interface(obj, SCREENCAST_IFACE)
                iface.Close(self.session_handle, timeout=3)
            except Exception:
                pass


def find_encoder() -> str:
    """Probe GStreamer for an available H.264 encoder."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    for name in ("nvh264enc", "nvautogpuh264enc", "vah264enc", "x264enc"):
        if Gst.ElementFactory.find(name):
            return name
    return ""


def encoder_keyframe_arg(encoder: str) -> str:
    """Parser argument forcing a ~1s keyframe interval on ``encoder``.

    Pausing splits the recording on a keyframe (via splitmuxsink), so a
    short GOP keeps pause/resume latency under a second instead of waiting
    for the encoder's default (often 250 frames, ~8s) keyframe.
    """
    props = {
        "nvh264enc": "gop-size=30",
        "nvautogpuh264enc": "gop-size=30",
        "vah264enc": "gop-size=30",
        "x264enc": "key-int-max=30",
    }
    return props.get(encoder, "")


def prompt_save_dir() -> Path | None:
    """Interactively ask the user where recordings should be saved."""
    print("No save location is configured yet.")
    while True:
        try:
            answer = input("Enter the folder to save recordings (e.g. ~/Videos): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not answer:
            print("  A path is required.")
            continue
        path = Path(answer).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"  Could not create folder: {exc}")
            continue
        return path


def _send_signal(sig: int) -> bool:
    """Send a signal to the running recording process.

    Returns True if the signal was dispatched, False if no recording is active.
    Reads the recording's PID from RECORDING_PID_FILE and validates it is alive.
    """
    pid_str = None
    try:
        pid_str = open(RECORDING_PID_FILE).read().strip()
    except FileNotFoundError:
        pass

    if not pid_str:
        print("No recording is currently running.", file=sys.stderr)
        return False

    try:
        pid = int(pid_str)
        if pid <= 0 or not _alive(pid):
            print("No recording is currently running (stale PID file).", file=sys.stderr)
            RECORDING_PID_FILE.unlink(missing_ok=True)
            return False
    except ValueError:
        print("Invalid PID file. Removing it.", file=sys.stderr)
        RECORDING_PID_FILE.unlink(missing_ok=True)
        return False

    try:
        os.kill(pid, sig)
    except OSError:
        print(f"Could not send signal {sig} to process {pid}", file=sys.stderr)
        return False

    return True


def stop_recording() -> int:
    """Stop the active recording and save it."""
    if _send_signal(signal.SIGTERM):
        print("Stopping recording...")
        return 0
    return 1


def pause_recording() -> int:
    """Pause the active recording without stopping it."""
    if _send_signal(signal.SIGUSR1):
        print("Pausing recording...")
        return 0
    return 1


def resume_recording() -> int:
    """Resume the active recording."""
    if _send_signal(signal.SIGUSR2):
        print("Resuming recording...")
        return 0
    return 1


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _current_segment_index(seg_dir: Path) -> int:
    """Highest splitmuxsink segment index present in ``seg_dir`` (or -1)."""
    best = -1
    try:
        for path in seg_dir.glob("seg_*.mp4"):
            try:
                index = int(path.stem.rsplit("_", 1)[-1])
            except ValueError:
                continue
            best = max(best, index)
    except OSError:
        pass
    return best


def _segment_total_size(seg_dir: Path) -> int:
    total = 0
    try:
        for path in seg_dir.glob("seg_*.mp4"):
            try:
                total += path.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def assemble_output(
    seg_dir: Path,
    state_at_open: dict,
    last_max: int,
    stopped_paused: bool,
    output_file: Path,
) -> None:
    """Merge the recorded segments into ``output_file``.

    Segments that were opened while the recording was paused contain the
    discarded pause period and are dropped, unless they are still the open
    segment at stop-time and the recording was running (in that case a
    resume-split never fired, so the segment absorbed the post-resume
    content and dropping it would lose real footage).
    """
    discard = {
        index
        for index, was_paused in state_at_open.items()
        if was_paused and not (index == last_max and not stopped_paused)
    }
    kept = sorted(
        index for index in sorted(state_at_open) if index not in discard
    )
    if not kept:
        print("Error: nothing was recorded.", file=sys.stderr)
        shutil.rmtree(seg_dir, ignore_errors=True)
        return

    try:
        paths = [seg_dir / f"seg_{index:05d}.mp4" for index in kept]
        if len(paths) == 1:
            shutil.move(str(paths[0]), str(output_file))
        else:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                print(
                    "Error: ffmpeg is required to merge paused segments.",
                    file=sys.stderr,
                )
                for path in paths:
                    print(f"  keeping segment: {path}")
                return
            list_file = seg_dir / "segments.txt"
            list_file.write_text(
                "".join(f"file '{path.resolve()}'\n" for path in paths)
            )
            subprocess.run(
                [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "0",
                    "-i", str(list_file), "-c", "copy", str(output_file),
                ],
                check=True,
                capture_output=True,
            )
        shutil.rmtree(seg_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - surface any assembly failure
        print(f"Error: could not assemble output: {exc}", file=sys.stderr)
        shutil.rmtree(seg_dir, ignore_errors=True)


def record(save_dir: Path) -> int:
    """Start recording using GStreamer through the portal, saving to save_dir."""
    import dbus
    import dbus.mainloop.glib
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    Gst.init(None)

    bus = dbus.SessionBus()

    # Install the signal handler up front so Ctrl+C works at every stage.
    def on_signal(signum, _frame):
        STOP.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # Pause (SIGUSR1) / resume (SIGUSR2). These only set flags; the actual
    # GStreamer state change happens on the main loop below.
    def on_pause(_signum, _frame):
        PAUSED.set()

    def on_resume(_signum, _frame):
        PAUSED.clear()

    signal.signal(signal.SIGUSR1, on_pause)
    signal.signal(signal.SIGUSR2, on_resume)

    # Write PID file so the stop command can stop the recording later
    RECORDING_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECORDING_PID_FILE.write_text(str(os.getpid()))

    if not Gst.ElementFactory.find("pipewiresrc"):
        print("Error: GStreamer pipewiresrc not available.", file=sys.stderr)
        return 1

    encoder = find_encoder()
    if not encoder:
        print("Error: no H.264 encoder found in GStreamer.", file=sys.stderr)
        return 1

    # Portal handshake (user approves dialog here)
    capture = PortalCapture(bus)
    if STOP.is_set():
        return 0
    if not capture.start():
        return 1

    save_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = save_dir / f"recording_{timestamp}.mp4"

    # Segments are written to a private temp dir and merged into a single
    # file on stop. Pausing splits on a keyframe via splitmuxsink, leaving a
    # "pause segment" that is dropped at assembly time - the live pipeline is
    # never flow-controlled mid-stream, which wedges the portal's PipeWire
    # source.
    seg_dir = Path(tempfile.mkdtemp(prefix="screenium_"))
    seg_pattern = str(seg_dir / "seg_%05d.mp4")

    fd = capture.pipewire_fd
    node = capture.stream_node_id
    keyframe_arg = encoder_keyframe_arg(encoder)
    pipeline_str = (
        f"pipewiresrc fd={fd} path={node} do-timestamp=true "
        f"! videoconvert ! videorate ! video/x-raw,framerate=30/1 "
        f"! {encoder} {keyframe_arg} ! h264parse "
        f"! splitmuxsink name=split location={seg_pattern} "
        f"max-size-time=0 max-size-bytes=0 send-keyframe-requests=true"
    )

    try:
        pipeline = Gst.parse_launch(pipeline_str)
    except Exception as exc:  # noqa: BLE001 - report and clean up
        shutil.rmtree(seg_dir, ignore_errors=True)
        print(f"Error: could not build the recording pipeline: {exc}", file=sys.stderr)
        return 1

    print(f"Recording to:\n  {output_file}")
    print("Press Ctrl+C to stop and save.")

    start = time.monotonic()
    last_beat = start
    context = GLib.MainContext.default()
    finalized = {"done": False}
    paused_now = False

    # When the pipeline reaches EOS (or errors), it is finalized.
    def on_bus_message(_bus, message):
        if message.type == Gst.MessageType.EOS:
            print("bus: EOS arrived.", flush=True)
            finalized["done"] = True
        elif message.type == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(
                f"bus: pipeline ERROR: {err.message}\n"
                f"  debug: {dbg or '(none)'}",
                file=sys.stderr,
            )
            finalized["done"] = True

    pipe_bus = pipeline.get_bus()
    pipe_bus.add_signal_watch()

    splitter = pipeline.get_by_name("split")

    pipeline.set_state(Gst.State.PLAYING)
    pipe_bus.connect("message", on_bus_message)

    try:
        # Pump the GLib context until told to stop (Ctrl+C or stop command).
        # Pausing does NOT change the pipeline state and never drops frames
        # mid-stream (the portal PipeWire source wedges if flow is stopped).
        # Instead, splitmuxsink is asked to split at the next keyframe, which
        # closes the current segment and opens a fresh one; the segment opened
        # while "paused" is discarded when the output is assembled at stop.
        state_at_open: dict[int, bool] = {}
        last_max = -1
        while not STOP.is_set():
            context.iteration(False)
            time.sleep(0.01)

            current_max = _current_segment_index(seg_dir)
            if current_max > last_max:
                for index in range(last_max + 1, current_max + 1):
                    state_at_open[index] = PAUSED.is_set()
                last_max = current_max

            if PAUSED.is_set() and not paused_now:
                paused_now = True
                splitter.emit("split-after")
                print("Recording paused. Run 'screenium resume' to continue.")
            elif not PAUSED.is_set() and paused_now:
                paused_now = False
                splitter.emit("split-after")
                print("Recording resumed.")
            now = time.monotonic()
            if now - last_beat > 5.0:
                print(
                    f"[beat {time.monotonic() - start:<5.0f}s] "
                    f"bytes={_segment_total_size(seg_dir)} "
                    f"segs={len(state_at_open)} paused={paused_now}",
                    flush=True,
                )
                last_beat = now

        print("Stopping recording...", flush=True)
        stopped_paused = PAUSED.is_set()

        # Stop work runs in a worker thread inside a hard time budget so a
        # wedged live stream (e.g. a portal/PipeWire node that stalls) can
        # never make `screenium stop`/Ctrl+C hang forever.
        def stop_and_teardown():
            pipeline.send_event(Gst.Event.new_eos())
            end = time.monotonic() + 5
            while not finalized["done"] and time.monotonic() < end:
                context.iteration(False)
                time.sleep(0.01)
            if not finalized["done"]:
                print(
                    "Warning: the pipeline did not finalize; saving what it can.",
                    file=sys.stderr,
                )
            pipeline.set_state(Gst.State.NULL)
            capture.close()
            assemble_output(
                seg_dir,
                state_at_open,
                last_max,
                stopped_paused,
                output_file,
            )
            cleanup_on_exit()

        stop_thread = threading.Thread(target=stop_and_teardown, daemon=True)
        stop_thread.start()
        stop_thread.join(timeout=10)
        if stop_thread.is_alive():
            # Last resort: nothing unsticks a wedged live pipeline quickly, so
            # force-exit rather than leave a process that ignores Ctrl+C and
            # `screenium stop`. All streaming has already been interrupted.
            print(
                "Error: the recording got stuck while stopping; forcing exit.",
                file=sys.stderr,
            )
            cleanup_on_exit()
            os._exit(1)
    finally:
        cleanup_on_exit()
        shutil.rmtree(seg_dir, ignore_errors=True)

    elapsed = time.monotonic() - start
    if output_file.exists() and output_file.stat().st_size > 0:
        mb = output_file.stat().st_size / 1_000_000
        print(f"\nSaved: {output_file} ({mb:.2f} MB, {elapsed:.0f}s)")
        return 0
    print("\nRecording failed — no output produced.", file=sys.stderr)
    return 1


def cmd_path(args: list[str]) -> int:
    """Handle the `path` subcommand: show or set the save location."""
    cfg = Config()
    if args:
        # `screenium path <DIR>` -> set it.
        target = Path(args[0]).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"Error: could not create folder: {exc}", file=sys.stderr)
            return 1
        cfg.set_save_dir(target)
        print(f"Recordings will be saved to: {cfg.get_save_dir()}")
        return 0

    current = cfg.get_save_dir()
    if current:
        print(f"Save location: {current}")
        print(f"Run 'screenium path <DIR>' to change it.")
    else:
        print("No save location is set yet.")
        print(f"Run 'screenium path <DIR>' to set it.")
    return 0


def cmd_record() -> int:
    """Start recording, prompting for a save path on first run."""
    cfg = Config()
    save_dir = cfg.get_save_dir()
    if save_dir is None:
        # First run: ask where recordings should go.
        chosen = prompt_save_dir()
        if chosen is None:
            print("Setup cancelled.", file=sys.stderr)
            return 1
        cfg.set_save_dir(chosen)
        save_dir = cfg.get_save_dir()

    # Import pystray on the main thread. Its Linux backend loads GTK/AppIndicator
    # through gi, and doing that serially here (before record() imports the
    # GStreamer gi bindings) avoids a gi repository race that can otherwise drop
    # shared attributes (e.g. GLib.Idle) when namespaces load from two threads
    # at once. The icon itself still runs in a daemon thread below.
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        # No tray backend / pystray unavailable: recording continues without
        # the icon rather than dying because the tray failed to set up.
        print(
            "Warning: could not set up tray icon; continuing without it.",
            file=sys.stderr,
        )
        return record(save_dir)

    # Tray icon runs in a background thread; it dies when this process exits
    # (i.e. when the recording stops), so the icon appears only while recording.
    tray_thread = threading.Thread(target=create_tray_icon, daemon=True)
    tray_thread.start()

    return record(save_dir)


def cmd_stop() -> int:
    """Stop the active recording and save it."""
    return stop_recording()


def cmd_pause() -> int:
    """Pause the active recording without stopping it."""
    return pause_recording()


def cmd_resume() -> int:
    """Resume the active recording."""
    return resume_recording()


def create_tray_icon():
    """Show a tray icon while recording is active.

    Runs in a daemon thread: it disappears when the recording process exits
    (recording stopped via Ctrl+C or `screenium stop`). The icon is red while
    recording and turns yellow while paused; the menu offers Pause/Resume and
    Stop Recording.
    """
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print("Error: pystray not installed. Run: uv add pystray pillow", file=sys.stderr)
        return

    def make_icon(paused=False):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        color = (240, 200, 0) if paused else (220, 30, 30)
        draw.ellipse((12, 12, 52, 52), fill=color)
        return img

    def on_stop(icon, item):
        stop_recording()

    def on_toggle(icon, item):
        if PAUSED.is_set():
            resume_recording()
        else:
            pause_recording()

    def on_quit(icon, item):
        icon.stop()

    def build_menu(paused):
        label = "Resume Recording" if paused else "Pause Recording"
        return pystray.Menu(
            pystray.MenuItem(label, on_toggle),
            pystray.MenuItem("Stop Recording", on_stop),
            pystray.MenuItem("Quit", on_quit),
        )

    menu = build_menu(PAUSED.is_set())
    icon = pystray.Icon("screenium", make_icon(PAUSED.is_set()), "Screenium Recording", menu)

    # Poll the pause state and refresh icon color + menu when it changes.
    def refresh():
        last = None
        while True:
            current = PAUSED.is_set()
            if current != last:
                icon.icon = make_icon(current)
                icon.update_menu()
                last = current
            time.sleep(0.5)

    threading.Thread(target=refresh, daemon=True).start()

    try:
        icon.run()
    except Exception:
        # No tray/status notifier available (headless, or no AppIndicator).
        # Recording continues without an icon.
        pass


def cleanup_on_exit():
    """Clean up on exit."""
    if RECORDING_PID_FILE.exists():
        RECORDING_PID_FILE.unlink(missing_ok=True)


def main(argv=None):
    """Main entry point for screenium CLI."""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    sub = argv[0]
    rest = argv[1:]

    if sub == "record":
        return cmd_record()
    if sub == "path":
        return cmd_path(rest)
    if sub == "stop":
        return cmd_stop()
    if sub == "pause":
        return cmd_pause()
    if sub == "resume":
        return cmd_resume()

    print(f"Unknown command: {sub}\n", file=sys.stderr)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
