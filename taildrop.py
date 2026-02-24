#!/usr/bin/env python3
"""
Taildrop — Nautilus extension to send files via Tailscale.
Install: ~/.local/share/nautilus-python/extensions/taildrop.py
Reload:  nautilus -q && nautilus &
"""

import json
import os
import subprocess
import time

from gi.repository import GObject, Nautilus

# Path to the tailscale binary — adjust if needed
TAILSCALE_BIN = "/usr/bin/tailscale"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _notify(title: str, message: str, icon: str = "network-transmit") -> None:
    """Fire-and-forget desktop notification (no zombie: double-fork via shell)."""
    # Using 'systemd-run --user' avoids zombie processes without threads.
    # Fallback to plain Popen if systemd-run is unavailable.
    try:
        subprocess.run(
            ["systemd-run", "--user", "--no-block",
             "notify-send", "-i", icon, title, message],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        # systemd-run not available — plain Popen (rare zombie risk but harmless)
        subprocess.Popen(
            ["notify-send", "-i", icon, title, message],
            close_fds=True,
        )


def _tailscale_available() -> bool:
    return os.path.isfile(TAILSCALE_BIN) and os.access(TAILSCALE_BIN, os.X_OK)


# ---------------------------------------------------------------------------
# Device cache + sending logic
# ---------------------------------------------------------------------------

class Taildrop:
    _devices_cache: list = []
    _last_cache_time: float = 0.0
    _CACHE_TTL: int = 15          # seconds — menu opens instantly thanks to this
    _tailscale_missing_warned: bool = False

    # ------------------------------------------------------------------ cache

    @classmethod
    def _warn_missing(cls) -> None:
        if not cls._tailscale_missing_warned:
            _notify("Tailscale introuvable",
                    f"Binaire non trouvé : {TAILSCALE_BIN}",
                    icon="dialog-error")
            cls._tailscale_missing_warned = True

    @classmethod
    def invalidate_cache(cls) -> None:
        """Force a refresh on the next get_devices() call."""
        cls._last_cache_time = 0.0

    @classmethod
    def get_devices(cls) -> list:
        now = time.monotonic()           # monotonic: immune to clock adjustments
        if now - cls._last_cache_time < cls._CACHE_TTL and cls._devices_cache:
            return cls._devices_cache

        if not _tailscale_available():
            cls._warn_missing()
            return cls._devices_cache   # return stale cache rather than nothing

        try:
            process = subprocess.run(
                [TAILSCALE_BIN, "status", "--json"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,              # avoid blocking Nautilus if Tailscale hangs
            )
        except subprocess.TimeoutExpired:
            _notify("Tailscale", "Timeout lors de la récupération des appareils.",
                    icon="dialog-warning")
            return cls._devices_cache
        except OSError as exc:
            _notify("Tailscale Error", f"Impossible de lancer tailscale : {exc}",
                    icon="dialog-error")
            return cls._devices_cache

        if process.returncode != 0:
            error_msg = process.stderr.strip() or "Erreur inconnue"
            _notify("Tailscale Error", f"Status error : {error_msg}",
                    icon="dialog-error")
            # Don't wipe cache — stale is better than empty
            return cls._devices_cache

        try:
            status = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            _notify("Tailscale Error", f"Réponse JSON invalide : {exc}",
                    icon="dialog-error")
            return cls._devices_cache

        items = []
        for _key, data in status.get("Peer", {}).items():
            # Skip Tailscale internal nodes
            if data.get("HostName") == "funnel-ingress-node":
                continue

            dns = data.get("DNSName", "")
            clean_name = dns.split(".")[0] if dns else data.get("HostName", "Inconnu")
            if not clean_name:
                clean_name = "Inconnu"

            os_name    = data.get("OS", "")
            is_online  = data.get("Online", False)
            status_icon = "🟢" if is_online else "🔴"
            os_part     = f" ({os_name})" if os_name else ""

            items.append({
                "hostname":  clean_name,
                "label":     f"{status_icon} {clean_name}{os_part}",
                "is_online": is_online,
            })

        # Online first, then alphabetical
        items.sort(key=lambda x: (not x["is_online"], x["hostname"].lower()))

        cls._devices_cache    = items
        cls._last_cache_time  = now
        cls._tailscale_missing_warned = False   # reset warning flag after success
        return items

    # ----------------------------------------------------------------- sending

    @staticmethod
    def send_files(paths: list[str], host: str) -> None:
        if not paths:
            return

        if len(paths) == 1:
            filename = os.path.basename(paths[0])
            message  = f"Envoi de « {filename} » vers {host}…"
        else:
            message = f"Envoi de {len(paths)} fichiers vers {host}…"

        _notify("Tailscale", message, icon="network-transmit")

        # Fire-and-forget via systemd-run (no threads, no zombies)
        cmd = [TAILSCALE_BIN, "file", "cp"] + paths + [f"{host}:"]
        try:
            subprocess.run(
                ["systemd-run", "--user", "--no-block"] + cmd,
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            # systemd-run unavailable — fall back to plain Popen
            subprocess.Popen(cmd, close_fds=True)


# ---------------------------------------------------------------------------
# Nautilus menu provider
# ---------------------------------------------------------------------------

class TaildropMenuProvider(GObject.GObject, Nautilus.MenuProvider):

    def __init__(self):
        super().__init__()

    # -------------------------------------------------------------- callback

    @staticmethod
    def _on_activate(_menu_item, hostname: str, files) -> None:
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
            _notify("Tailscale", "Aucun fichier local sélectionné.",
                    icon="dialog-warning")
            return

        Taildrop.send_files(paths, hostname)

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
            label="Envoyer avec Tailscale",
            tip="Partager via Taildrop",
            icon="network-transmit",
        )
        submenu = Nautilus.Menu()
        top.set_submenu(submenu)

        for device in devices:
            item = Nautilus.MenuItem(
                name=f'Taildrop::Device_{device["hostname"]}',
                label=device["label"],
            )
            item.connect("activate", TaildropMenuProvider._on_activate,
                         device["hostname"], files)
            submenu.append_item(item)

        # ── "Refresh" entry at the bottom ──────────────────────────────────
        sep = Nautilus.MenuItem(
            name="Taildrop::Sep",
            label="─────────────",   # visual separator (Nautilus has no real separator)
        )
        submenu.append_item(sep)

        refresh = Nautilus.MenuItem(
            name="Taildrop::Refresh",
            label="🔄 Actualiser la liste",
        )
        refresh.connect("activate", lambda *_: Taildrop.invalidate_cache())
        submenu.append_item(refresh)

        return [top]

    # ------------------------------------------------- Nautilus entry points

    def get_file_items(self, files):
        """Called when right-clicking on selected files."""
        return self._build_menu(files)

    def get_background_items(self, window, file):          # noqa: signature varies
        """Called when right-clicking on the folder background — not needed."""
        return []