#!/usr/bin/env python3
"""Screenium - a lightweight, desktop-environment-independent screen recorder.

Works on any Wayland compositor (Hyprland, Sway, GNOME, KDE, ...) or X11
session by using the freedesktop ScreenCast portal to obtain a PipeWire
video stream, which is then encoded with GStreamer + hardware acceleration.

Usage:
    uv run python3 screenrecorder.py    # start recording
    Ctrl+C (or kill)                    # stop and save the recording
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import dbus
import dbus.mainloop.glib
import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

OUTPUT_DIR = Path.home() / "tmp"

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_IFACE = "org.freedesktop.portal.Request"

# Set by the SIGINT/SIGTERM handler so every blocking wait can bail out fast.
STOP = threading.Event()


class PortalCapture:
    """Drives the xdg-desktop-portal ScreenCast session over D-Bus.

    The session flow:
        CreateSession → SelectSources → Start → OpenPipeWireRemote

    Start() presents a dialog to the user asking to approve screen sharing.
    On success, the portal returns PipeWire stream node IDs.
    """

    def __init__(self, bus: dbus.Bus) -> None:
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

        request_path = str(method(*pos_args, options))

        result: dict = {"ok": False, "results": {}, "timed_out": True}
        done = threading.Event()

        def on_response(response: int, results: dict) -> None:
            result["ok"] = response == 0
            result["results"] = results
            result["timed_out"] = False
            done.set()

        # DBus dispatch is set up for the default GLib main context; we pump
        # it manually below on the calling (main) thread.
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
        context = GLib.MainContext.default()
        end = time.monotonic() + max_seconds
        while not done.is_set() and not STOP.is_set() and time.monotonic() < end:
            context.iteration(False)
            time.sleep(0.01)

    def _aborted(self) -> bool:
        return STOP.is_set()

    def start(self) -> bool:
        """Run the full portal handshake and obtain a PipeWire stream."""
        # 1. Create session (retry in case the portal is slow/busy)
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

        # 2. Select sources — type 1 = monitor, cursor embedded in stream
        ok, _ = self._call(
            self.sc.SelectSources,
            self.session_handle,
            options={"multiple": False, "types": dbus.UInt32(1)},
        )
        if not ok:
            print("Error: source selection failed.", file=sys.stderr)
            return False

        # 3. Start — this may present an approval dialog to the user
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

        # 4. Get PipeWire file descriptor
        fd = self.sc.OpenPipeWireRemote(
            self.session_handle, dbus.Dictionary({}, signature="sv")
        )
        self.pipewire_fd = fd.take()
        return True

    def close(self) -> None:
        """Close the portal session so the compositor can release resources."""
        if self.session_handle:
            try:
                obj = self.bus.get_object(PORTAL_NAME, PORTAL_PATH)
                iface = dbus.Interface(obj, SCREENCAST_IFACE)
                iface.Close(self.session_handle)
            except Exception:
                pass


def find_encoder() -> str:
    """Probe GStreamer for an available H.264 encoder."""
    for name in ("nvh264enc", "nvautogpuh264enc", "vah264enc", "x264enc"):
        if Gst.ElementFactory.find(name):
            return name
    return ""


def main() -> None:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    Gst.init(None)

    bus = dbus.SessionBus()

    # Install the signal handler up front so Ctrl+C works at every stage
    # (including during the portal handshake / any approval dialog).
    def on_signal(signum, _frame):
        STOP.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    if not Gst.ElementFactory.find("pipewiresrc"):
        print("Error: GStreamer pipewiresrc not available.", file=sys.stderr)
        sys.exit(1)

    encoder = find_encoder()
    if not encoder:
        print("Error: no H.264 encoder found in GStreamer.", file=sys.stderr)
        sys.exit(1)

    # Portal handshake (user approves dialog here)
    capture = PortalCapture(bus)
    if STOP.is_set():
        sys.exit(0)
    if not capture.start():
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = OUTPUT_DIR / f"recording_{timestamp}.mp4"

    fd = capture.pipewire_fd
    node = capture.stream_node_id
    pipeline_str = (
        f"pipewiresrc fd={fd} path={node} do-timestamp=true "
        f"! videoconvert ! videorate ! video/x-raw,framerate=30/1 "
        f"! {encoder} ! h264parse ! mp4mux fragment-duration=1000 "
        f"! filesink location={output_file}"
    )

    pipeline = Gst.parse_launch(pipeline_str)

    print(f"Recording to:\n  {output_file}")
    print("Press Ctrl+C to stop and save.")

    start = time.monotonic()
    context = GLib.MainContext.default()
    finalized = {"done": False}
    stop_started = time.monotonic()

    # When the pipeline reaches EOS (or errors), it is finalized.
    def on_bus_message(_bus, message):
        if message.type in (Gst.MessageType.EOS, Gst.MessageType.ERROR):
            finalized["done"] = True

    pipe_bus = pipeline.get_bus()
    pipe_bus.add_signal_watch()

    pipeline.set_state(Gst.State.PLAYING)
    pipe_bus.connect("message", on_bus_message)

    try:
        # Pump the GLib context until told to stop (Ctrl+C). We don't use
        # MainLoop.run() so Ctrl+C fires our handler instead of being
        # swallowed by GLib's sigint fallback.
        while not STOP.is_set():
            context.iteration(False)
            time.sleep(0.01)
        # Ask the pipeline to finish and flush final fragments, bounded so
        # we always exit promptly. With a fragmented MP4 the file is valid
        # even without a full EOS finalization.
        stop_started = time.monotonic()
        pipeline.send_event(Gst.Event.new_eos())
        while (
            not finalized["done"]
            and time.monotonic() - stop_started < 5.0
        ):
            context.iteration(False)
            time.sleep(0.01)
    finally:
        pipeline.set_state(Gst.State.NULL)
        capture.close()

    elapsed = time.monotonic() - start
    if output_file.exists() and output_file.stat().st_size > 0:
        mb = output_file.stat().st_size / 1_000_000
        print(f"\nSaved: {output_file} ({mb:.2f} MB, {elapsed:.0f}s)")
    else:
        print("\nRecording failed — no output produced.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
