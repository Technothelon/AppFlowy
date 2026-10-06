# SPDX-License-Identifier: AGPL-3.0-only
"""Sync outside the offline sandbox before launch and after AppFlowy exits."""
import fcntl
import os
import subprocess
import sys
from pathlib import Path

from drive_sync import EXPERIMENT, load_config, synchronize

SCRIPTS = Path(__file__).resolve().parents[1] / 'Scripts'

os.umask(0o077)


def notify(message):
    subprocess.run(['/usr/bin/notify-send', '--app-name=AppFlowy Experiment',
                    'AppFlowy Experiment', message], capture_output=True)


def sync(config):
    try:
        synchronize(config, progress=lambda x: print(x, flush=True))
    except Exception as error:
        print('Google Drive sync paused: ' + str(error), flush=True)
        notify('Google Drive sync paused. Local edits are kept. Use Sync AppFlowy Experiment to retry.')
        return False
    return True


with (EXPERIMENT / 'State/launch-with-sync.lock').open('a') as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        notify('AppFlowy Experiment is already open or synchronizing.')
        raise SystemExit(0)
    config = load_config(EXPERIMENT / 'Sync/config.json')
    sync(config)
    app = subprocess.Popen([str(SCRIPTS / 'launch_offline.sh')])
    try:
        result = app.wait()
    except KeyboardInterrupt:
        app.terminate()
        app.wait()
        raise
    if sync(config):
        notify('Google Drive sync finished.')
    raise SystemExit(result)
