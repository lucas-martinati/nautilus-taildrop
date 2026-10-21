"""Run with dbus-run-session -- xvfb-run -a /usr/bin/python3 tests/monitor_smoke.py."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import taildrop
from gi.repository import Gio, GLib


def main():
    with tempfile.TemporaryDirectory(prefix='taildrop-ui-') as directory:
        root = Path(directory)
        cli = root / 'tailscale'
        cli.write_text('''#!/usr/bin/python3
import sys, time
from pathlib import Path
name = Path(sys.argv[3]).name
if name == 'blocked':
    print('Waiting for receiver', flush=True)
    while True: time.sleep(.1)
if name == 'failed':
    print('Receiver refused this file', file=sys.stderr)
    sys.exit(1)
print('\\r\\x1b[Kfile 12.00MiB 1.00MiB/s 25.00% ETA 00:00:01', flush=True)
time.sleep(.3)
print('\\r\\x1b[Kfile 48.00MiB 1.00MiB/s 100.00% ETA 00:00:00', flush=True)
''')
        cli.chmod(0o755)
        for name in ('blocked', 'success', 'failed'):
            (root / name).write_text('example')
        taildrop.TAILSCALE_BIN = str(cli)
        taildrop._notify = lambda *args, **kwargs: None
        script = str(Path(taildrop.__file__).resolve())

        def request(*names):
            return dict(action='send', paths=[str(root / n) for n in names],
                        target='100.64.0.2', display_name='Test phone')

        def remote(payload):
            return subprocess.Popen(['/usr/bin/python3', script, '--transfers', json.dumps(payload)])

        sys.argv = [script, '--transfers', json.dumps(request('blocked'))]
        stage = 0
        secondary = None
        deadline = time.monotonic() + 15
        failure = None

        def tick():
            nonlocal stage, secondary, failure
            app = Gio.Application.get_default()
            try:
                if time.monotonic() > deadline:
                    raise AssertionError(f'Timed out at stage {stage}')
                if not app or not getattr(app, 'rows', None):
                    return True
                if stage == 0 and app.jobs[0].snapshot()[2] == 'Waiting for receiver':
                    secondary = remote(request('blocked', 'success', 'failed'))
                    stage = 1
                elif stage == 1 and len(app.jobs) == 3 and secondary.poll() is not None:
                    assert secondary.returncode == 0
                    assert app.jobs[1].snapshot()[0] == 'queued'
                    assert app.rows[1].box.get_parent() == app.active_list
                    assert not app.rows[1].retry.get_visible()
                    app.rows[0].cancel.emit('clicked')  # Cancel via the actual button.
                    stage = 2
                elif stage == 2 and all(j.snapshot()[0] in taildrop.Transfer.TERMINAL for j in app.jobs):
                    assert [j.snapshot()[0] for j in app.jobs] == ['cancelled', 'completed', 'failed']
                    app._update()
                    assert all(row.box.get_parent() == app.history_list for row in app.rows)
                    assert not app.rows[1].progress.get_visible()
                    assert app.rows[2].details.get_visible()
                    app._copy_details(app.jobs[2])
                    app.rows[2].retry.emit('clicked')  # Retry via the actual button.
                    assert len(app.jobs) == 4
                    stage = 3
                elif stage == 3 and app.jobs[-1].snapshot()[0] == 'failed':
                    app._update()
                    app._close()
                    assert not app.window.get_visible()
                    secondary = remote({})
                    stage = 4
                elif stage == 4 and secondary.poll() is not None:
                    assert secondary.returncode == 0
                    assert app.window.get_visible()
                    app._clear()
                    assert len(app.jobs) == 0
                    app._add(taildrop.Transfer(str(root / 'blocked'), '100.64.0.2', 'Test phone'))
                    app._add(taildrop.Transfer(str(root / 'success'), '100.64.0.2', 'Test phone'))
                    stage = 5
                elif stage == 5 and app.jobs[0].snapshot()[2] == 'Waiting for receiver':
                    with app.jobs[0].lock:
                        app.jobs[0].last_activity -= 31
                    app._update()
                    assert app.rows[0].detail.get_text().startswith('No progress for 30 seconds')
                    app._clear()
                    assert len(app.jobs) == 2, 'Clearing history must preserve active and queued transfers'
                    app._cancel_all()
                    stage = 6
                elif stage == 6 and all(j.snapshot()[0] in taildrop.Transfer.TERMINAL for j in app.jobs):
                    assert [j.snapshot()[0] for j in app.jobs] == ['cancelled', 'cancelled']
                    print('GTK integration OK: single instance, queue, deduplication, cancel, cancel all, stall warning, retry, copy, clear, hide/reopen')
                    app.quit()
                    return False
            except Exception as exc:
                failure = exc
                if app:
                    for job in getattr(app, 'jobs', []):
                        job.cancel()
                    app.quit()
                return False
            return True

        GLib.timeout_add(50, tick)
        taildrop._run_monitor()
        if secondary is not None:
            secondary.wait(timeout=5)
        if failure:
            raise failure


if __name__ == '__main__':
    main()
