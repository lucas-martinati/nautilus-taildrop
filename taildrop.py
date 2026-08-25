#!/usr/bin/env python3
"""
Taildrop — Nautilus extension to send and receive files via Tailscale.
Install: ~/.local/share/nautilus-python/extensions/taildrop.py
Reload:  nautilus -q && nautilus &
"""

from __future__ import annotations

import json
import os
import shutil as _shutil
import subprocess
import threading
import time

import gi
try:
    gi.require_version('Nautilus', '4.0')
except ValueError:
    try:
        gi.require_version('Nautilus', '3.0')
    except ValueError:
        pass

from gi.repository import GObject, Nautilus

TAILSCALE_BIN: str = _shutil.which("tailscale") or "/usr/bin/tailscale"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _copy_to_clipboard(text: str) -> None:
    """Copy text to clipboard supporting Wayland and X11."""
    if not text:
        return

    # Wayland
    if _shutil.which("wl-copy") and ("WAYLAND_DISPLAY" in os.environ or os.environ.get("XDG_SESSION_TYPE") == "wayland"):
        try:
            subprocess.run(["wl-copy"], input=text, text=True, check=True)
            return
        except Exception:
            pass

    # X11
    if _shutil.which("xclip"):
        try:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, check=True)
            return
        except Exception:
            pass

    if _shutil.which("xsel"):
        try:
            subprocess.run(["xsel", "--clipboard", "--input"], input=text, text=True, check=True)
            return
        except Exception:
            pass


def _notify(title: str, message: str, icon: str = "network-transmit", copy_content: str | None = None) -> None:
    """Desktop notification. If copy_content is provided, clicking the notification copies it to clipboard."""
    def _worker():
        cmd = ["notify-send", "-a", "Taildrop", "-i", icon, title, message]
        if copy_content:
            cmd.extend([
                "--action=default=Copy error",
                "--action=copy=📋 Copy error",
            ])
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                action = res.stdout.strip()
                if action in ("default", "copy", "0", "1"):
                    _copy_to_clipboard(copy_content)
                    _notify("Tailscale", "Error copied to clipboard!", icon="edit-copy")
            except FileNotFoundError:
                pass
        else:
            try:
                subprocess.Popen(cmd, close_fds=True)
            except FileNotFoundError:
                pass

    threading.Thread(target=_worker, daemon=True).start()


def _tailscale_available() -> bool:
    return os.path.isfile(TAILSCALE_BIN) and os.access(TAILSCALE_BIN, os.X_OK)


# ---------------------------------------------------------------------------
# Device cache + sending/receiving logic
# ---------------------------------------------------------------------------

class Taildrop:
    _devices_cache: list = []
    _last_cache_time: float = 0.0
    _CACHE_TTL: int = 20          # seconds
    _is_updating: bool = False
    _lock = threading.Lock()
    _tailscale_missing_warned: bool = False

    # ------------------------------------------------------------------ cache

    @classmethod
    def _warn_missing(cls) -> None:
        if not cls._tailscale_missing_warned:
            err_msg = f"Binary not found: {TAILSCALE_BIN}"
            _notify("Tailscale not found",
                    err_msg,
                    icon="dialog-error",
                    copy_content=err_msg)
            cls._tailscale_missing_warned = True

    @classmethod
    def _fetch_devices(cls) -> None:
        """Fetch devices from Tailscale CLI and populate the cache."""
        with cls._lock:
            try:
                if not _tailscale_available():
                    cls._warn_missing()
                    return

                try:
                    process = subprocess.run(
                        [TAILSCALE_BIN, "status", "--json"],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=8,
                    )
                except subprocess.TimeoutExpired:
                    err_msg = "Timeout while retrieving devices."
                    _notify("Tailscale", err_msg,
                            icon="dialog-warning",
                            copy_content=err_msg)
                    return
                except OSError as exc:
                    err_msg = f"Unable to launch tailscale: {exc}"
                    _notify("Tailscale Error", err_msg,
                            icon="dialog-error",
                            copy_content=err_msg)
                    return

                if process.returncode != 0:
                    error_msg = process.stderr.strip() or "Unknown status error"
                    _notify("Tailscale Error", f"Status error: {error_msg}",
                            icon="dialog-error",
                            copy_content=error_msg)
                    return

                try:
                    status = json.loads(process.stdout)
                except json.JSONDecodeError as exc:
                    err_msg = f"Invalid JSON response: {exc}"
                    _notify("Tailscale Error", err_msg,
                            icon="dialog-error",
                            copy_content=err_msg)
                    return

                # Only show peers belonging to the same user (important in shared/corp tailnets)
                self_user_id = status.get("Self", {}).get("UserID")

                items = []
                for _key, data in status.get("Peer", {}).items():
                    # Skip Tailscale internal nodes
                    if data.get("HostName") == "funnel-ingress-node":
                        continue

                    # Skip peers that belong to other users (e.g. colleagues on a shared tailnet)
                    if self_user_id and data.get("UserID") != self_user_id:
                        continue

                    dns = data.get("DNSName", "").rstrip(".")
                    dns_name = dns.split(".")[0] if dns else ""
                    host_name = data.get("HostName", "")
                    clean_name = host_name or dns_name or "Unknown"

                    # Prefer full MagicDNS name to avoid hostname collision between identical devices
                    target = dns if dns else (dns_name or host_name or "Unknown")

                    os_name = data.get("OS", "")
                    is_online = data.get("Online", False)
                    status_icon = "🟢" if is_online else "🔴"
                    os_part = f" ({os_name})" if os_name else ""

                    items.append({
                        "target": target,
                        "hostname": clean_name,
                        "label": f"{status_icon} {clean_name}{os_part}",
                        "is_online": is_online,
                    })

                # Online first, then alphabetical
                items.sort(key=lambda x: (not x["is_online"], x["hostname"].lower()))

                cls._devices_cache = items
                cls._last_cache_time = time.monotonic()
                cls._tailscale_missing_warned = False
            finally:
                cls._is_updating = False

    @classmethod
    def get_devices(cls) -> list:
        now = time.monotonic()
        # If cache is completely empty, fetch synchronously so devices appear on first click
        if not cls._devices_cache:
            cls._fetch_devices()
            return cls._devices_cache

        # Stale-while-revalidate: return cache immediately and refresh asynchronously if expired
        if (now - cls._last_cache_time >= cls._CACHE_TTL) and not cls._is_updating:
            cls._is_updating = True
            threading.Thread(target=cls._fetch_devices, daemon=True).start()

        return cls._devices_cache

    @classmethod
    def invalidate_cache(cls) -> None:
        """Force a refresh and trigger immediate background fetch."""
        cls._last_cache_time = 0.0
        cls._is_updating = True
        _notify("Tailscale", "Refreshing device list…", icon="view-refresh")
        threading.Thread(target=cls._fetch_devices, daemon=True).start()

    # ---------------------------------------------------------------- receiving

    @staticmethod
    def receive_files(dest_dir: str) -> None:
        """Pull pending Taildrop files into *dest_dir* asynchronously with feedback."""
        folder_name = os.path.basename(dest_dir) or dest_dir
        _notify("Tailscale", f"Receiving files in {folder_name}…",
                icon="network-receive")

        def _receive_worker():
            cmd = [TAILSCALE_BIN, "file", "get", dest_dir]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            except subprocess.TimeoutExpired:
                err_msg = "Timeout while checking for incoming files."
                _notify("Tailscale Error", err_msg, icon="dialog-warning", copy_content=err_msg)
                return

            if res.returncode == 0:
                _notify("Tailscale", f"Files received in {folder_name}!",
                        icon="emblem-default")
            else:
                raw_err = (res.stderr or res.stdout or "").strip()
                err = raw_err or "No pending files or Tailscale error."
                _notify("Tailscale", err, icon="dialog-warning", copy_content=err)

        threading.Thread(target=_receive_worker, daemon=True).start()

    # ----------------------------------------------------------------- sending

    @staticmethod
    def send_files(paths: list[str], target: str, display_name: str) -> None:
        """Send files to target peer asynchronously with bounded timeout and immediate error notification."""
        if not paths:
            return

        valid_paths = [p for p in paths if os.path.exists(p)]
        if not valid_paths:
            _notify("Tailscale", "No valid local file selected.", icon="dialog-warning")
            return

        count = len(valid_paths)
        if count == 1:
            filename = os.path.basename(valid_paths[0])
            message = f"Sending '{filename}' to {display_name}…"
        else:
            message = f"Sending {count} files to {display_name}…"

        _notify("Tailscale", message, icon="network-transmit")

        def _send_worker():
            # Dynamic timeout: minimum 15 seconds, + 1s per 2 MB payload
            try:
                total_size = sum(os.path.getsize(p) for p in valid_paths if os.path.isfile(p))
            except Exception:
                total_size = 0
            timeout_sec = max(15, int(total_size / (2 * 1024 * 1024)) + 15)

            cmd = [TAILSCALE_BIN, "file", "cp"] + valid_paths + [f"{target}:"]
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_sec,
                )
            except subprocess.TimeoutExpired:
                err_msg = f"{display_name} did not respond within {timeout_sec}s (device may be asleep or offline)."
                _notify(
                    "Tailscale Error",
                    f"Timeout: {display_name} is not responding (offline or sleep mode).",
                    icon="dialog-error",
                    copy_content=f"Tailscale timeout sending to {display_name} ({target}): {err_msg}",
                )
                return
            except Exception as exc:
                _notify(
                    "Tailscale Error",
                    f"Failed to launch transfer: {exc}",
                    icon="dialog-error",
                    copy_content=str(exc),
                )
                return

            if res.returncode == 0:
                _notify("Tailscale", f"Successfully sent files to {display_name}!",
                        icon="emblem-default")
            else:
                raw_err = (res.stderr or res.stdout or "").strip()
                # Clean up terminal progress lines
                err_lines = [
                    line.strip() for line in raw_err.splitlines()
                    if line.strip() and not ("0.00B" in line or "ETA" in line)
                ]
                clean_err = " | ".join(err_lines) if err_lines else "Device unreachable or connection failed"

                if "reportedly offline" in clean_err.lower():
                    user_msg = f"{display_name} is offline or unreachable."
                else:
                    user_msg = clean_err

                full_error = f"Tailscale error sending to {display_name} ({target}): {clean_err}"
                _notify(
                    "Tailscale Error",
                    f"Failed to send to {display_name}: {user_msg}",
                    icon="dialog-error",
                    copy_content=full_error,
                )

        threading.Thread(target=_send_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Nautilus menu provider
# ---------------------------------------------------------------------------

class TaildropMenuProvider(GObject.GObject, Nautilus.MenuProvider):

    def __init__(self):
        super().__init__()

    # -------------------------------------------------------------- callback

    @staticmethod
    def _on_activate(_menu_item, target: str, display_name: str, files) -> None:
        paths = []
        for f in files:
            location = f.get_location()
            if location is None:
                continue
            path = location.get_path()
            # get_path() returns None for non-local URIs (smb://, sftp://…)
            if path and os.path.exists(path):
                paths.append(path)

        if not paths:
            _notify("Tailscale", "No local file selected.",
                    icon="dialog-warning")
            return

        Taildrop.send_files(paths, target, display_name)

    # ----------------------------------------------------------- menu builder

    def _build_menu(self, files) -> list:
        """Return the top-level menu item, or [] if no devices."""
        try:
            devices = Taildrop.get_devices()
        except Exception:
            return []

        if not devices:
            return []

        top = Nautilus.MenuItem(
            name="Taildrop::Main",
            label="Send with Tailscale",
            tip="Share via Taildrop",
            icon="network-transmit",
        )
        submenu = Nautilus.Menu()
        top.set_submenu(submenu)

        for device in devices:
            safe_name = str(device["hostname"]).replace(" ", "_")
            item = Nautilus.MenuItem(
                name=f'Taildrop::Device_{safe_name}',
                label=device["label"],
            )
            item.connect("activate", TaildropMenuProvider._on_activate,
                         device["target"], device["hostname"], files)
            submenu.append_item(item)

        # ── "Refresh" entry at the bottom ──────────────────────────────────
        sep = Nautilus.MenuItem(
            name="Taildrop::Sep",
            label="─────────────",   # visual separator
            sensitive=False,
        )
        submenu.append_item(sep)

        refresh = Nautilus.MenuItem(
            name="Taildrop::Refresh",
            label="🔄 Refresh device list",
        )
        refresh.connect("activate", lambda *_: Taildrop.invalidate_cache())
        submenu.append_item(refresh)

        return [top]

    # ------------------------------------------------- Nautilus entry points

    def get_file_items(self, files):
        """Called when right-clicking on selected files."""
        return self._build_menu(files)

    def get_background_items(self, window_or_file, file=None):
        """Right-click on the folder background → offer to receive pending files."""
        # Nautilus passes either (window, file) or just (file,) depending on version.
        folder = file if file is not None else window_or_file

        item = Nautilus.MenuItem(
            name="Taildrop::Receive",
            label="📥 Receive with Tailscale",
            tip="Retrieve pending Taildrop files in this folder",
            icon="network-receive",
        )

        def _on_receive(_menu_item, f):
            location = f.get_location() if f else None
            dest = location.get_path() if location else None
            if not dest:
                _notify("Tailscale", "Unable to determine target folder.",
                        icon="dialog-error")
                return
            Taildrop.receive_files(dest)

        item.connect("activate", _on_receive, folder)
        return [item]