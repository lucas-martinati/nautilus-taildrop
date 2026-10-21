#!/usr/bin/env python3
"""Nautilus Taildrop extension and standalone, single-instance transfer monitor."""
from __future__ import annotations

import codecs
from collections import deque
import errno
import json
import os
import pty
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import weakref

import gi
from gi.repository import GLib

TAILSCALE_BIN = shutil.which("tailscale") or "/usr/bin/tailscale"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PROGRESS = re.compile(r"\s(\d+(?:\.\d+)?)%\s+ETA\s+\S+\s*$")


def _notify(title, message, icon="network-transmit"):
    """Notifications supplement the monitor; they never own transfer state."""
    def worker():
        try:
            cmd = ["notify-send", "-a", "Taildrop", "-i", icon,
                   title, GLib.markup_escape_text(message)]
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    threading.Thread(target=worker, daemon=True).start()


def _launch_monitor(request=None):
    """A separate process survives Nautilus restarts and owns all CLI children."""
    try:
        subprocess.Popen(["/usr/bin/python3", os.path.realpath(__file__),
                          "--transfers", json.dumps(request or {})],
                         start_new_session=True, close_fds=True)
    except OSError as exc:
        _notify("Taildrop", f"Unable to open transfer monitor: {exc}", "dialog-error")


class Taildrop:
    _devices_cache = []
    _last_cache_time = 0.0
    _CACHE_TTL = 20
    _is_updating = False
    _lock = threading.Lock()
    _providers = weakref.WeakSet()
    _status_message = "Device list has not been loaded yet."

    @classmethod
    def _publish_devices(cls):
        # Nautilus must be told about the change on its main thread.
        for provider in list(cls._providers):
            provider.emit_items_updated_signal()
        return GLib.SOURCE_REMOVE

    @classmethod
    def _fetch_devices(cls, notify_on_error=False):
        error = None
        items = []
        try:
            result = subprocess.run([TAILSCALE_BIN, "status", "--json"],
                                    capture_output=True, text=True, check=False, timeout=8)
            if result.returncode:
                raise ValueError(result.stderr.strip() or result.stdout.strip() or "Tailscale status failed.")
            status = json.loads(result.stdout)
            if status.get("BackendState") != "Running":
                raise ValueError("Tailscale is disconnected. Connect Tailscale and refresh the device list.")
            self_user = (status.get("Self") or {}).get("UserID")
            for peer in (status.get("Peer") or {}).values():
                if peer.get("HostName") == "funnel-ingress-node":
                    continue
                if self_user and peer.get("UserID") != self_user:
                    continue
                # Prefer a stable Tailscale IP; MagicDNS may be disabled locally.
                addresses = peer.get("TailscaleIPs") or []
                target = next((ip for ip in addresses if ":" not in ip), None)
                if not target:
                    target = peer.get("DNSName", "").rstrip(".")
                if not target and addresses:
                    target = f"[{addresses[0]}]"
                if not target:
                    continue
                name = peer.get("HostName") or target
                online = bool(peer.get("Online"))
                os_part = f" ({peer['OS']})" if peer.get("OS") else ""
                items.append(dict(target=target, hostname=name, is_online=online,
                                  label=f"{'🟢' if online else '🔴'} {name}{os_part}"))
            items.sort(key=lambda item: (not item["is_online"], item["hostname"].casefold()))
        except subprocess.TimeoutExpired:
            error = "Timed out retrieving devices. Check that Tailscale is running."
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            error = str(exc)
        finally:
            with cls._lock:
                # Never leave stale devices looking current after a failed refresh.
                cls._devices_cache = items if error is None else []
                cls._last_cache_time = time.monotonic()
                cls._status_message = error or ("" if items else "No devices found for your Tailscale account.")
                cls._is_updating = False
            GLib.idle_add(cls._publish_devices)
        if notify_on_error:
            _notify("Taildrop", error or f"Device list updated: {len(items)} device(s).",
                    "dialog-warning" if error else "view-refresh")

    @classmethod
    def _start_refresh(cls, explicit=False):
        with cls._lock:
            if cls._is_updating:
                return
            cls._is_updating = True
        # Do not hold the cache lock during network/CLI work.
        GLib.idle_add(cls._publish_devices)
        threading.Thread(target=cls._fetch_devices, args=(explicit,), daemon=True).start()

    @classmethod
    def get_devices(cls):
        with cls._lock:
            stale = time.monotonic() - cls._last_cache_time >= cls._CACHE_TTL
            devices = list(cls._devices_cache)
        if stale:
            cls._start_refresh()
        return devices

    @classmethod
    def invalidate_cache(cls):
        cls._start_refresh(explicit=True)

    @staticmethod
    def send_files(paths, target, display_name):
        if paths:
            _launch_monitor(dict(action="send", paths=paths, target=target, display_name=display_name))

    @staticmethod
    def receive_files(dest_dir):
        _launch_monitor(dict(action="receive", directory=dest_dir))


class Transfer:
    """One file (or inbox retrieval), with bounded output and cancellable process."""
    TERMINAL = {"completed", "failed", "cancelled"}
    STALL_SECONDS = 30
    KILL_SECONDS = 2

    def __init__(self, path, target=None, display_name=None):
        self.path = os.path.abspath(path)
        self.target = target
        self.display_name = display_name or target or self.path
        self.receiving = target is None
        self.state = "queued"
        self.progress = None
        self.detail = "Waiting in queue"
        self.last_activity = time.monotonic()
        self.output = deque(maxlen=80)
        self.cancelled = threading.Event()
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            return self.state, self.progress, self.detail, self.last_activity

    def cancel(self):
        with self.lock:
            if self.state in self.TERMINAL:
                return
            self.cancelled.set()
            if self.state == "queued":
                self.state, self.detail = "cancelled", "Cancelled before starting"
            else:
                self.state, self.detail = "cancelling", "Cancelling…"

    def _consume_line(self, line):
        line = _ANSI.sub("", line).strip()
        if not line:
            return
        progress = _PROGRESS.search(line) if not self.receiving else None
        with self.lock:
            if progress:
                value = min(100.0, max(0.0, float(progress[1]))) / 100
                if self.progress is None or value > self.progress:
                    self.last_activity = time.monotonic()
                self.progress = value
                self.detail = line
            else:
                # Keep complete CLI errors, including offline warnings.
                self.output.append(line[-2048:])
                self.detail = line[-2048:]
                if self.receiving:
                    self.last_activity = time.monotonic()

    def _command(self):
        if self.receiving:
            if not os.path.isdir(self.path):
                raise ValueError(f"Destination folder does not exist: {self.path}")
            return [TAILSCALE_BIN, "file", "get", "--verbose", "--conflict=rename", self.path]
        if not os.path.isfile(self.path):
            raise ValueError(f"Not a regular file: {self.path}. Compress folders before sending them.")
        return [TAILSCALE_BIN, "file", "cp", self.path, f"{self.target}:"]

    def run(self):
        process = None
        master = None
        try:
            with self.lock:
                if self.cancelled.is_set():
                    return
                self.state, self.detail = "running", "Connecting…" if not self.receiving else "Checking inbox…"
                self.last_activity = time.monotonic()
            cmd = self._command()
            # Tailscale only emits progress when stderr is a terminal. Drain a
            # pseudo-terminal continuously, including carriage-return updates.
            master, slave = pty.openpty()
            try:
                process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                           stdout=slave, stderr=slave,
                                           start_new_session=True, close_fds=True)
            finally:
                os.close(slave)
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            pending = ""
            terminated_at = None
            eof = False
            while not eof or process.poll() is None:
                if self.cancelled.is_set() and process.poll() is None:
                    if terminated_at is None:
                        self._signal(process, signal.SIGTERM)
                        terminated_at = time.monotonic()
                    elif time.monotonic() - terminated_at >= self.KILL_SECONDS:
                        self._signal(process, signal.SIGKILL)
                if eof:
                    time.sleep(0.05)
                    continue
                readable, _, _ = select.select([master], [], [], 0.1)
                if not readable:
                    continue
                try:
                    chunk = os.read(master, 8192)
                except OSError as exc:
                    # Linux PTYs report EIO after the last slave closes.
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    eof = True
                    pending += decoder.decode(b"", final=True)
                else:
                    pending += decoder.decode(chunk)
                lines = re.split(r"[\r\n]", pending)
                pending = lines.pop()
                for line in lines:
                    self._consume_line(line)
                # A broken CLI must not grow memory indefinitely without newlines.
                if len(pending) > 8192:
                    self._consume_line(pending[:8192])
                    pending = pending[8192:]
            if pending:
                self._consume_line(pending)
            code = process.wait()
            with self.lock:
                if self.cancelled.is_set():
                    self.state, self.detail = "cancelled", "Cancelled; files already delivered are kept on the receiver."
                elif code == 0:
                    self.state, self.progress = "completed", 1.0
                    if self.receiving:
                        self.detail = "Inbox checked. " + (self.output[-1] if self.output else "No pending files.")
                    else:
                        self.detail = "Delivered to the receiving device"
                else:
                    self.state = "failed"
                    self.detail = "\n".join(self.output) or f"Tailscale exited with status {code}."
        except Exception as exc:
            with self.lock:
                self.state = "cancelled" if self.cancelled.is_set() else "failed"
                self.detail = "Cancelled" if self.cancelled.is_set() else str(exc)
        finally:
            if process is not None and process.poll() is None:
                self._signal(process, signal.SIGKILL)
                process.wait()
            if master is not None:
                os.close(master)

    @staticmethod
    def _signal(process, sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass


def _run_monitor():
    # Import GTK only in the helper. Nautilus 3 and 4 use different GTK majors.
    gtk_version = "4.0"
    try:
        gi.require_version("Gtk", gtk_version)
    except ValueError:
        gtk_version = "3.0"
        gi.require_version("Gtk", gtk_version)
    gi.require_version("Gdk", gtk_version)
    from gi.repository import Gdk, Gio, Gtk, Pango
    gtk4 = Gtk.get_major_version() == 4

    def append(box, widget, expand=False):
        if gtk4:
            box.append(widget)
        else:
            box.pack_start(widget, expand, expand, 0)

    def style(widget, *classes):
        for name in classes:
            if gtk4:
                widget.add_css_class(name)
            else:
                widget.get_style_context().add_class(name)
        return widget

    def margins(widget, size):
        for side in ("top", "bottom", "start", "end"):
            getattr(widget, f"set_margin_{side}")(size)

    def icon(name, size=20):
        image = Gtk.Image.new_from_icon_name(name) if gtk4 else Gtk.Image.new_from_icon_name(name, Gtk.IconSize.BUTTON)
        image.set_pixel_size(size)
        return image

    def button(text, icon_name=None, tooltip=None):
        result = Gtk.Button()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.set_halign(Gtk.Align.CENTER)
        if icon_name:
            append(content, icon(icon_name, 16))
        if text:
            append(content, Gtk.Label(label=text))
        result.set_child(content) if gtk4 else result.add(content)
        if tooltip:
            result.set_tooltip_text(tooltip)
        return result

    class TransferRow:
        def __init__(self, app, job):
            self.job = job
            self.finished = False
            self.box = style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12), "transfer-card")
            margins(self.box, 2)
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            if job.receiving:
                file_icon = icon("folder-download-symbolic", 24)
            else:
                content_type, _ = Gio.content_type_guess(job.path, None)
                file_gicon = Gio.content_type_get_symbolic_icon(content_type)
                file_icon = Gtk.Image.new_from_gicon(file_gicon) if gtk4 else Gtk.Image.new_from_gicon(file_gicon, Gtk.IconSize.LARGE_TOOLBAR)
                file_icon.set_pixel_size(24)
            style(file_icon, "file-icon")
            append(header, file_icon)
            names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            names.set_hexpand(True)
            title = Gtk.Label(label=os.path.basename(job.path) or job.path, xalign=0)
            title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            title.set_tooltip_text(job.path)
            style(title, "file-name")
            append(names, title)
            metadata = f"Save into {job.path}" if job.receiving else f"To {job.display_name}"
            if not job.receiving:
                try:
                    metadata += " · " + GLib.format_size(os.path.getsize(job.path))
                except OSError:
                    pass
            subtitle = style(Gtk.Label(label=metadata, xalign=0), "muted")
            subtitle.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            subtitle.set_tooltip_text(metadata)
            append(names, subtitle)
            append(header, names, expand=True)
            self.badge = style(Gtk.Label(), "status-badge")
            append(header, self.badge)
            append(self.box, header)
            self.progress = style(Gtk.ProgressBar(), "transfer-progress")
            append(self.box, self.progress)
            bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            self.detail = style(Gtk.Label(xalign=0, wrap=True, selectable=True), "transfer-detail", "muted")
            self.detail.set_hexpand(True)
            self.detail.set_max_width_chars(64)
            append(bottom, self.detail, expand=True)
            self.cancel = button("Cancel", "process-stop-symbolic", "Cancel this transfer")
            self.cancel.connect("clicked", lambda *_: job.cancel())
            append(bottom, self.cancel)
            self.retry = style(button("Retry", "view-refresh-symbolic"), "suggested-action")
            self.retry.connect("clicked", lambda *_: app._add(Transfer(job.path, job.target, job.display_name)))
            append(bottom, self.retry)
            append(self.box, bottom)
            self.details = Gtk.Expander(label="Details")
            details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self.raw_detail = style(Gtk.Label(xalign=0, wrap=True, selectable=True), "technical-detail")
            self.raw_detail.set_max_width_chars(76)
            if gtk4:
                self.raw_detail.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            else:
                self.raw_detail.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            details_scroll = Gtk.ScrolledWindow()
            details_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            details_scroll.set_max_content_height(160)
            details_scroll.set_propagate_natural_height(True)
            details_scroll.set_child(self.raw_detail) if gtk4 else details_scroll.add(self.raw_detail)
            append(details_box, details_scroll)
            copy = button("Copy details", "edit-copy-symbolic")
            copy.set_halign(Gtk.Align.START)
            copy.connect("clicked", lambda *_: app._copy_details(job))
            append(details_box, copy)
            self.details.set_child(details_box) if gtk4 else self.details.add(details_box)
            append(self.box, self.details)
            self.last_state = None

        def update(self):
            state, fraction, message, last_activity = self.job.snapshot()
            stalled = state == "running" and time.monotonic() - last_activity >= Transfer.STALL_SECONDS
            labels = {"queued": "Queued", "running": "Receiving" if self.job.receiving else "Sending",
                      "cancelling": "Cancelling", "completed": "Inbox checked" if self.job.receiving else "Delivered",
                      "failed": "Failed", "cancelled": "Cancelled"}
            label = "Waiting for device" if stalled else labels[state]
            if state == "running" and fraction is not None and not stalled:
                label += f" · {fraction:.0%}"
            self.badge.set_text(label)
            tone = "warning" if stalled else {"running": "active", "completed": "success", "failed": "error"}.get(state, "neutral")
            if self.last_state != tone:
                for name in ("active", "success", "error", "warning", "neutral"):
                    if gtk4:
                        self.badge.remove_css_class("status-" + name)
                    else:
                        self.badge.get_style_context().remove_class("status-" + name)
                style(self.badge, "status-" + tone)
                self.last_state = tone
            self.progress.set_visible(state in ("running", "cancelling"))
            if fraction is None and state == "running":
                self.progress.pulse()
            else:
                self.progress.set_fraction(fraction or 0)
            if stalled:
                text = "No progress for 30 seconds. Check the receiving device and its folder permissions, or cancel and retry."
            elif state == "failed":
                text = message.splitlines()[0][:240]
            elif state == "running" and fraction is not None:
                # Keep CLI filenames and terminal formatting out of the main UI.
                match = re.search(r"\s(\S+B/s)\s+\d+(?:\.\d+)?%\s+ETA\s+(\S+)", message)
                text = f"{match[1]} · {match[2]} remaining" if match and match[2] != "-" else "Transferring…"
                if fraction >= 1:
                    text = "Waiting for the receiving device to confirm…"
            else:
                text = {"queued": "Will start after the current transfer",
                        "running": "Checking inbox…" if self.job.receiving else "Connecting to the receiving device…",
                        "cancelling": "Stopping the transfer…",
                        "completed": message if self.job.receiving else "Received by the destination device",
                        "cancelled": "Transfer stopped. Files already delivered are kept."}[state]
            self.detail.set_text(text)
            self.raw_detail.set_text(message)
            self.cancel.set_visible(state in ("queued", "running", "cancelling"))
            self.cancel.set_sensitive(state in ("queued", "running"))
            self.retry.set_visible(state in ("failed", "cancelled"))
            self.details.set_visible(state in ("failed", "cancelled") or stalled)
            return state

    class Monitor(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="io.github.nautilus_taildrop.Transfers",
                             flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
            self.jobs = []
            self.pending = queue.Queue()
            self.rows = []
            self.window = None
            threading.Thread(target=self._worker, daemon=True).start()

        def _worker(self):
            while True:
                job = self.pending.get()
                try:
                    job.run()
                    state, _, detail, _ = job.snapshot()
                    if state == "failed":
                        _notify("Taildrop — transfer failed", f"{os.path.basename(job.path)}: {detail}", "dialog-error")
                    elif state == "completed":
                        _notify("Taildrop", f"Inbox checked: {job.path}" if job.receiving else f"Sent {os.path.basename(job.path)} to {job.display_name}", "emblem-default")
                finally:
                    self.pending.task_done()

        def do_command_line(self, command_line):
            try:
                args = command_line.get_arguments()
                request = json.loads(args[1]) if len(args) > 1 else {}
                self.activate()
                action = request.get("action")
                if action == "send":
                    for path in request["paths"]:
                        self._add(Transfer(path, request["target"], request["display_name"]))
                elif action == "receive":
                    self._add(Transfer(request["directory"]))
                self._update()
                return 0
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                command_line.printerr_literal(f"Unable to start Taildrop: {exc}\n")
                return 1

        def _install_style(self):
            css = b"""
            .taildrop-window .page { padding: 24px; }
            .taildrop-window .page-title { font-size: 24px; font-weight: 700; }
            .taildrop-window .muted { opacity: 0.68; }
            .taildrop-window .section-title { font-size: 13px; font-weight: 700; }
            .taildrop-window .transfer-card {
                background: @theme_base_color; border: 1px solid alpha(@theme_fg_color, 0.10);
                border-radius: 14px; padding: 16px;
            }
            .taildrop-window .file-name { font-size: 14px; font-weight: 700; }
            .taildrop-window .file-icon { color: mix(@theme_selected_bg_color, @theme_fg_color, 0.3); }
            .taildrop-window .transfer-detail { font-size: 12px; }
            .taildrop-window .status-badge {
                border-radius: 20px; padding: 5px 10px; font-size: 11px; font-weight: 700;
                color: @theme_fg_color;
            }
            .taildrop-window .status-neutral { background: alpha(@theme_fg_color, 0.07); }
            .taildrop-window .status-active { background: alpha(@theme_selected_bg_color, 0.15); }
            .taildrop-window .status-success { background: alpha(#27a867, 0.15); }
            .taildrop-window .status-error { background: alpha(#e23c3c, 0.13); }
            .taildrop-window .status-warning { background: alpha(#e5a50a, 0.18); }
            .taildrop-window .transfer-progress trough { min-height: 6px; border-radius: 6px; }
            .taildrop-window .transfer-progress progress { min-height: 6px; border-radius: 6px; }
            .taildrop-window .technical-detail {
                font-family: monospace; font-size: 11px; padding: 10px;
                background: alpha(@theme_fg_color, 0.04); border-radius: 8px;
            }
            .taildrop-window .empty-icon { opacity: 0.35; margin-bottom: 8px; }
            .taildrop-window .empty-title { font-size: 19px; font-weight: 700; }
            """
            provider = Gtk.CssProvider()
            provider.load_from_data(css)
            if gtk4:
                Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            else:
                Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.css_provider = provider

        def do_activate(self):
            if self.window is None:
                self.window = style(Gtk.ApplicationWindow(application=self, title="Taildrop — Transfers"), "taildrop-window")
                self.window.set_default_size(760, 620)
                self.window.set_size_request(540, 380)
                self.window.connect("close-request" if gtk4 else "delete-event", self._close)
                self._install_style()
                header = Gtk.HeaderBar()
                title = Gtk.Label(label="Taildrop")
                style(title, "section-title")
                if gtk4:
                    header.set_title_widget(title)
                else:
                    header.set_custom_title(title)
                    header.set_show_close_button(True)
                self.window.set_titlebar(header)
                self.cancel_all = button("Cancel all", "process-stop-symbolic", "Cancel all active and queued transfers")
                self.cancel_all.connect("clicked", self._cancel_all)
                header.pack_start(self.cancel_all)
                self.clear = button(None, "edit-clear-all-symbolic", "Clear finished transfers")
                self.clear.connect("clicked", self._clear)
                header.pack_end(self.clear)
                scroll = Gtk.ScrolledWindow()
                scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                page = style(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20), "page")
                heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                append(heading, style(Gtk.Label(label="Your transfers", xalign=0), "page-title"))
                self.summary = style(Gtk.Label(xalign=0, wrap=True), "muted")
                append(heading, self.summary)
                append(page, heading)
                self.empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                self.empty.set_valign(Gtk.Align.CENTER)
                self.empty.set_vexpand(True)
                margins(self.empty, 36)
                append(self.empty, style(icon("folder-publicshare-symbolic", 64), "empty-icon"))
                append(self.empty, style(Gtk.Label(label="Ready when you are"), "empty-title"))
                explanation = style(Gtk.Label(label="Right-click a file in Nautilus and choose\nSend with Tailscale. Follow its progress here.",
                                               justify=Gtk.Justification.CENTER, wrap=True), "muted")
                append(self.empty, explanation)
                append(page, self.empty, expand=True)
                self.active_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                self.active_title = style(Gtk.Label(xalign=0), "section-title")
                append(self.active_section, self.active_title)
                self.active_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                append(self.active_section, self.active_list)
                append(page, self.active_section)
                self.history_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                self.history_title = style(Gtk.Label(xalign=0), "section-title")
                append(self.history_section, self.history_title)
                self.history_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                append(self.history_section, self.history_list)
                append(page, self.history_section)
                if gtk4:
                    scroll.set_child(page)
                    self.window.set_child(scroll)
                else:
                    scroll.add(page)
                    self.window.add(scroll)
                    self.window.show_all()
                GLib.timeout_add(200, self._update)
            self._update()
            self.window.present()

        def _add(self, job):
            # Repeated clicks must not enqueue duplicate active sends.
            for existing in self.jobs:
                if (existing.path, existing.target) == (job.path, job.target) and existing.snapshot()[0] not in Transfer.TERMINAL:
                    return
            row = TransferRow(self, job)
            self.jobs.append(job)
            self.rows.append(row)
            append(self.active_list, row.box)
            if not gtk4:
                row.box.show_all()
            self._update()
            self.pending.put(job)

        def _copy_details(self, job):
            text = job.snapshot()[2]
            if gtk4:
                self.window.get_clipboard().set(text)
            else:
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)

        def _cancel_all(self, *_):
            for job in self.jobs:
                job.cancel()
            self._update()

        def _update(self):
            active = queued = finished = failures = 0
            for row in self.rows:
                state = row.update()
                active += state in ("running", "cancelling")
                queued += state == "queued"
                failures += state == "failed"
                terminal = state in Transfer.TERMINAL
                finished += terminal
                if terminal and not row.finished:
                    self.active_list.remove(row.box)
                    if gtk4:
                        self.history_list.prepend(row.box)
                    else:
                        append(self.history_list, row.box)
                        self.history_list.reorder_child(row.box, 0)
                    row.finished = True
            if active or queued:
                text = f"{active} in progress · {queued} queued"
            elif failures:
                text = f"{failures} transfer(s) need your attention. You can retry them below."
            elif self.jobs:
                text = "All transfers are finished. You can send more files from Nautilus."
            else:
                text = "Send files securely between your Tailscale devices."
            self.summary.set_text(text)
            self.active_title.set_text(f"In progress · {active + queued}")
            self.history_title.set_text(f"Recent transfers · {finished}")
            self.active_section.set_visible(bool(active or queued))
            self.history_section.set_visible(bool(finished))
            self.empty.set_visible(not self.jobs)
            self.cancel_all.set_visible(bool(active or queued))
            self.clear.set_sensitive(bool(finished))
            return GLib.SOURCE_CONTINUE

        def _clear(self, *_):
            for row in list(self.rows):
                if row.job.snapshot()[0] in Transfer.TERMINAL:
                    parent = row.box.get_parent()
                    if parent:
                        parent.remove(row.box)
                    self.rows.remove(row)
                    self.jobs.remove(row.job)
            self._update()

        def _close(self, *_):
            # Retain the window/history while hidden; all transfers keep running.
            self.window.set_visible(False)
            return True

    app = Monitor()
    return app.run([sys.argv[0], sys.argv[2] if len(sys.argv) > 2 else "{}"])

if __name__ != "__main__":
    # Nautilus 50 introduced typelib 4.1. Keep older Nautilus releases working.
    for version in ("4.1", "4.0", "3.0"):
        try:
            gi.require_version("Nautilus", version)
            break
        except ValueError:
            continue
    from gi.repository import GObject, Nautilus

    class TaildropMenuProvider(GObject.GObject, Nautilus.MenuProvider):
        def __init__(self):
            super().__init__()
            Taildrop._providers.add(self)
            Taildrop._start_refresh()

        @staticmethod
        def _on_activate(_item, target, name, files):
            paths = []
            for file in files:
                location = file.get_location()
                path = location.get_path() if location else None
                if not path:
                    _notify("Taildrop", "Only local files can be sent. Copy remote files locally first.", "dialog-warning")
                    return
                paths.append(path)
            Taildrop.send_files(paths, target, name)

        def _build_menu(self, files):
            devices = Taildrop.get_devices()
            top = Nautilus.MenuItem(name="Taildrop::Main", label="Send with Tailscale", icon="network-transmit")
            submenu = Nautilus.Menu()
            top.set_submenu(submenu)
            for index, device in enumerate(devices):
                item = Nautilus.MenuItem(name=f"Taildrop::Device_{index}", label=device["label"])
                item.connect("activate", self._on_activate, device["target"], device["hostname"], files)
                submenu.append_item(item)
            if Taildrop._is_updating or not devices:
                message = "Refreshing devices…" if Taildrop._is_updating else Taildrop._status_message
                submenu.append_item(Nautilus.MenuItem(name="Taildrop::Status", label=message, sensitive=False))
            refresh = Nautilus.MenuItem(name="Taildrop::Refresh", label="Refresh device list", sensitive=not Taildrop._is_updating)
            refresh.connect("activate", lambda *_: Taildrop.invalidate_cache())
            submenu.append_item(refresh)
            submenu.append_item(self._monitor_item())
            return [top]

        @staticmethod
        def _monitor_item():
            item = Nautilus.MenuItem(name="Taildrop::Transfers", label="Taildrop transfers…", icon="network-transmit-receive")
            item.connect("activate", lambda *_: _launch_monitor())
            return item

        def get_file_items(self, *args):
            return self._build_menu(args[-1])

        def get_background_items(self, *args):
            folder = args[-1]
            location = folder.get_location() if folder else None
            dest = location.get_path() if location else None
            items = [self._monitor_item()]
            if dest:
                item = Nautilus.MenuItem(name="Taildrop::Receive", label="📥 Receive with Tailscale", icon="network-receive")
                item.connect("activate", lambda *_: Taildrop.receive_files(dest))
                items.insert(0, item)
            return items


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "--transfers":
        sys.exit("Use --transfers to open the Taildrop monitor.")
    sys.exit(_run_monitor())
