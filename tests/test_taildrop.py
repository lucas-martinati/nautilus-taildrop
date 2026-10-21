"""Regression tests use a local fake CLI; no file is sent over Tailscale."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import taildrop


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cli = self.root / 'tailscale'
        cli.write_text('''#!/usr/bin/python3
import os, signal, sys, time
from pathlib import Path
assert sys.stderr.isatty(), 'progress needs a terminal'
if sys.argv[2] == 'get':
    assert '--conflict=rename' in sys.argv
    print('no files to get', file=sys.stderr)
    sys.exit(0)
path = Path(sys.argv[3])
assert path.is_absolute(), 'absolute paths protect leading hyphens'
if path.name == 'blocked':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print(f'READY {os.getpid()}', file=sys.stderr, flush=True)
    while True: time.sleep(.05)
if path.name == 'failed':
    print('permission denied: configure a Tailscale operator', file=sys.stderr)
    print('request failed with HTTP 403', file=sys.stderr)
    sys.exit(1)
if path.name == 'large-output':
    for _ in range(150):
        print('x' * 10000, file=sys.stderr)
    sys.exit(1)
if path.name == 'early-close':
    os.close(1)
    os.close(2)
    time.sleep(.2)
    sys.exit(0)
for percent in (0, 25, 100):
    data = f'\\r\\x1b[Kéchantillon    12.00MiB    1.00MiB/s    {percent:.2f}%    ETA 00:00:01'.encode()
    os.write(2, data[:7])
    os.write(2, data[7:])
    time.sleep(.08)
print(file=sys.stderr)
''')
        cli.chmod(0o755)
        patch = mock.patch.object(taildrop, 'TAILSCALE_BIN', str(cli))
        patch.start()
        self.addCleanup(patch.stop)

    def job(self, name='success'):
        path = self.root / name
        path.write_bytes(b'example')
        return taildrop.Transfer(str(path), '100.64.0.2', 'Phone')

    def run_async(self, job):
        worker = threading.Thread(target=job.run, daemon=True)
        worker.start()
        self.addCleanup(lambda: (job.cancel(), worker.join(4)))
        return worker

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for fake CLI')

    def test_real_pty_progress_and_success(self):
        job = self.job()
        worker = self.run_async(job)
        self.wait_for(lambda: job.snapshot()[1] == .25)
        self.assertEqual(job.snapshot()[0], 'running')
        self.assertNotIn('\x1b', job.snapshot()[2])
        self.assertIn('échantillon', job.snapshot()[2])
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(job.snapshot()[:2], ('completed', 1.0))

    def test_cancel_kills_an_uncooperative_process_and_reaps_it(self):
        job = self.job('blocked')
        job.KILL_SECONDS = .1
        worker = self.run_async(job)
        self.wait_for(lambda: job.snapshot()[2].startswith('READY '))
        pid = int(job.snapshot()[2].split()[1])
        job.cancel()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(job.snapshot()[0], 'cancelled')
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_cancel_queued_never_launches_cli(self):
        job = self.job()
        job.cancel()
        with mock.patch.object(taildrop.subprocess, 'Popen') as popen:
            job.run()
        popen.assert_not_called()
        self.assertEqual(job.snapshot()[0], 'cancelled')

    def test_full_multiline_cli_error_is_retained(self):
        job = self.job('failed')
        job.run()
        self.assertEqual(job.snapshot()[0], 'failed')
        self.assertIn('configure a Tailscale operator', job.snapshot()[2])
        self.assertIn('HTTP 403', job.snapshot()[2])

    def test_missing_cli_is_reported(self):
        job = self.job()
        with mock.patch.object(taildrop, 'TAILSCALE_BIN', '/nonexistent/tailscale'):
            job.run()
        self.assertEqual(job.snapshot()[0], 'failed')
        self.assertIn('/nonexistent/tailscale', job.snapshot()[2])

    def test_directories_and_missing_files_are_not_silently_skipped(self):
        for path in (self.root, self.root / 'missing'):
            job = taildrop.Transfer(str(path), 'phone')
            with mock.patch.object(taildrop.subprocess, 'Popen') as popen:
                job.run()
            popen.assert_not_called()
            self.assertEqual(job.snapshot()[0], 'failed')
            self.assertIn(str(path), job.snapshot()[2])

    def test_failed_output_is_bounded(self):
        job = self.job('large-output')
        job.run()
        self.assertEqual(job.snapshot()[0], 'failed')
        self.assertLess(len(job.snapshot()[2]), 165000)

    def test_closing_output_before_exit_does_not_hang(self):
        job = self.job('early-close')
        worker = self.run_async(job)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(job.snapshot()[0], 'completed')

    def test_receive_renames_conflicts_and_reports_empty_inbox(self):
        job = taildrop.Transfer(str(self.root))
        job.run()
        self.assertEqual(job.snapshot()[0], 'completed')
        self.assertIn('no files to get', job.snapshot()[2])

    def test_static_progress_does_not_reset_stall_clock(self):
        job = self.job()
        job._consume_line('file 0.00B 0.00B/s 0.00% ETA -')
        then = job.last_activity
        time.sleep(.01)
        job._consume_line('file 0.00B 0.00B/s 0.00% ETA -')
        self.assertEqual(job.last_activity, then)
        job._consume_line('file 1.00B 1.00B/s 1.00% ETA 00:00:01')
        self.assertGreater(job.last_activity, then)


class DeviceTests(unittest.TestCase):
    def setUp(self):
        taildrop.Taildrop._devices_cache = []
        taildrop.Taildrop._last_cache_time = 0
        taildrop.Taildrop._is_updating = False
        patch = mock.patch.object(taildrop.GLib, 'idle_add')
        self.idle = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(taildrop, '_notify')
        self.notify = patch.start()
        self.addCleanup(patch.stop)

    def fetch(self, status):
        result = mock.Mock(returncode=0, stdout=json.dumps(status))
        with mock.patch.object(taildrop.subprocess, 'run', return_value=result):
            taildrop.Taildrop._fetch_devices()

    def test_selects_own_peers_with_ip_targets_and_sorts_online_first(self):
        self.fetch(dict(BackendState='Running', Self=dict(UserID=1), Peer={
            'a': dict(UserID=1, HostName='A', Online=False, TailscaleIPs=['100.64.0.2']),
            'b': dict(UserID=1, HostName='B', Online=True, TailscaleIPs=['100.64.0.3']),
            'c': dict(UserID=2, HostName='Other', Online=True, TailscaleIPs=['100.64.0.4']),
            'd': dict(UserID=1, HostName='IPv6', Online=False, TailscaleIPs=['fd7a:115c:a1e0::1']),
        }))
        devices = taildrop.Taildrop._devices_cache
        self.assertEqual([item['hostname'] for item in devices], ['B', 'A', 'IPv6'])
        self.assertEqual(devices[0]['target'], '100.64.0.3')
        self.assertEqual(devices[-1]['target'], '[fd7a:115c:a1e0::1]')
        self.idle.assert_called_with(taildrop.Taildrop._publish_devices)

    def test_null_peer_map_is_an_empty_success(self):
        self.fetch(dict(BackendState='Running', Peer=None))
        self.assertEqual(taildrop.Taildrop._devices_cache, [])
        self.assertIn('No devices', taildrop.Taildrop._status_message)

    def test_failure_clears_stale_devices_and_resets_refresh_flag(self):
        taildrop.Taildrop._devices_cache = [dict(target='stale')]
        taildrop.Taildrop._is_updating = True
        with mock.patch.object(taildrop.subprocess, 'run', side_effect=OSError('daemon unavailable')):
            taildrop.Taildrop._fetch_devices(True)
        self.assertEqual(taildrop.Taildrop._devices_cache, [])
        self.assertFalse(taildrop.Taildrop._is_updating)
        self.assertIn('daemon unavailable', taildrop.Taildrop._status_message)
        self.notify.assert_called_once()

    def test_disconnected_state_has_a_useful_error(self):
        self.fetch(dict(BackendState='NeedsLogin'))
        self.assertIn('disconnected', taildrop.Taildrop._status_message)

    def test_first_menu_does_not_wait_for_status_command(self):
        with mock.patch.object(taildrop.threading, 'Thread') as thread:
            self.assertEqual(taildrop.Taildrop.get_devices(), [])
        thread.return_value.start.assert_called_once()
        self.assertTrue(taildrop.Taildrop._is_updating)

    def test_concurrent_refresh_requests_are_coalesced(self):
        with mock.patch.object(taildrop.threading, 'Thread') as thread:
            for _ in range(10):
                taildrop.Taildrop.invalidate_cache()
        thread.return_value.start.assert_called_once()

    def test_empty_devices_are_cached_to_avoid_repeated_status_calls(self):
        taildrop.Taildrop._last_cache_time = time.monotonic()
        with mock.patch.object(taildrop.threading, 'Thread') as thread:
            self.assertEqual(taildrop.Taildrop.get_devices(), [])
        thread.assert_not_called()

    def test_empty_device_menu_still_offers_refresh_and_monitor(self):
        with mock.patch.object(taildrop.Taildrop, '_start_refresh'):
            provider = taildrop.TaildropMenuProvider()
        with mock.patch.object(taildrop.Taildrop, 'get_devices', return_value=[]):
            items = provider.get_file_items([])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].get_property('label'), 'Send with Tailscale')

    def test_cache_completion_emits_nautilus_items_updated(self):
        provider = mock.Mock()
        taildrop.Taildrop._providers.add(provider)
        self.addCleanup(lambda: taildrop.Taildrop._providers.discard(provider))
        taildrop.Taildrop._publish_devices()
        provider.emit_items_updated_signal.assert_called_once()


if __name__ == '__main__':
    unittest.main()
