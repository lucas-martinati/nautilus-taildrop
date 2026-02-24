#!/usr/bin/env python3
import json
import subprocess
import time
from urllib.parse import unquote
from gi.repository import Nautilus, GObject

class Taildrop:
    _devices_cache = []
    _last_cache_time = 0
    _CACHE_TTL = 15  # Menu opens instantly thanks to this cache

    @classmethod
    def get_devices(cls):
        now = time.time()
        if now - cls._last_cache_time < cls._CACHE_TTL and cls._devices_cache:
            return cls._devices_cache

        try:
            process = subprocess.run(
                ['/usr/bin/tailscale', 'status', '--json'], 
                capture_output=True, 
                text=True, 
                check=False
            )
            
            if process.returncode != 0:
                error_msg = process.stderr.strip() if process.stderr else "Unknown error"
                subprocess.Popen(['notify-send', '-i', 'dialog-error', 'Tailscale Error', f"Status error: {error_msg}"])
                return cls._devices_cache
                
            status = json.loads(process.stdout)
            items = []
            
            for _host, data in status.get('Peer', {}).items():
                if data.get('HostName') == "funnel-ingress-node":
                    continue
                    
                clean_name = data.get('DNSName', '').split('.')[0]
                if not clean_name:
                    clean_name = data.get('HostName', 'Inconnu')
                    
                os_name = data.get('OS', '')
                is_online = data.get('Online', False)
                status_icon = "🟢" if is_online else "🔴"
                
                items.append({
                    'hostname': clean_name,
                    'label': f"{status_icon} {clean_name} ({os_name})",
                    'is_online': is_online
                })
                
            # Sort items: Online first (not is_online = False = 0), then alphabetically
            items.sort(key=lambda x: (not x['is_online'], x['hostname'].lower()))
            
            cls._devices_cache = items
            cls._last_cache_time = now
            return items

        except Exception as e:
            subprocess.Popen(['notify-send', '-i', 'dialog-error', 'Tailscale Error', f"Execution error: {str(e)}"])
            return cls._devices_cache

    @staticmethod
    def send_files(paths, host):
        if not paths:
            return
            
        # Smart notification (1 file vs multiple files)
        if len(paths) == 1:
            filename = paths[0].split('/')[-1]
            message = f"Sending '{filename}' to {host}..."
        else:
            message = f"Sending {len(paths)} files to {host}..."
            
        # 1. Single and immediate notification (does not block Nautilus)
        subprocess.Popen(['notify-send', '-i', 'transfer', 'Tailscale', message])
        
        # 2. Instant batch sending in "Fire and forget" mode (no Thread)
        cmd = ['/usr/bin/tailscale', 'file', 'cp'] + paths + [f"{host}:"]
        subprocess.Popen(cmd)


class TaildropMenuProvider(GObject.GObject, Nautilus.MenuProvider):
    def __init__(self):
        super().__init__()

    @staticmethod
    def callback_send(_menu, hostname, files):
        paths = []
        for file in files:
            location = file.get_location()
            if location:
                filepath = location.get_path()
                if filepath:
                    paths.append(filepath)
                    
        if paths:
            Taildrop.send_files(paths, hostname)

    def get_file_items(self, files):
        top_menuitem = Nautilus.MenuItem(
            name='Taildrop::Main', 
            label='Send with Tailscale', 
            tip='Share via Taildrop',
            icon='transfer'
        )
        
        submenu = Nautilus.Menu()
        top_menuitem.set_submenu(submenu)

        try:
            devices = Taildrop.get_devices()
        except Exception:
            return []

        if not devices:
            return []

        for device in devices:
            item = Nautilus.MenuItem(
                name=f'Taildrop::Device_{device["hostname"]}', 
                label=device['label']
            )
            item.connect('activate', TaildropMenuProvider.callback_send, device['hostname'], files)
            submenu.append_item(item)

        return [top_menuitem]

    def get_background_items(self, file):
        return []