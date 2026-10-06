# SPDX-License-Identifier: AGPL-3.0-only
"""One-click sync with a small desktop progress dialog."""
import json
import os
import subprocess
from pathlib import Path

from drive_sync import EXPERIMENT, load_config, synchronize

os.umask(0o077)
dialog = subprocess.Popen([
    '/usr/bin/zenity', '--progress', '--pulsate', '--auto-close', '--no-cancel',
    '--title=AppFlowy Experiment — Google Drive', '--text=Preparing private sync…',
    '--width=440'], stdin=subprocess.PIPE, text=True)


def progress(message):
    try:
        dialog.stdin.write('# ' + message + '\n')
        dialog.stdin.flush()
    except (BrokenPipeError, OSError):
        pass


try:
    report = synchronize(load_config(EXPERIMENT / 'Sync/config.json'), progress=progress)
except Exception as error:
    dialog.terminate()
    dialog.wait()
    subprocess.run(['/usr/bin/zenity', '--error', '--width=440',
                    '--title=AppFlowy Experiment — Sync paused', '--text=' + str(error)])
    raise SystemExit(1)
else:
    try:
        dialog.stdin.write('100\n')
        dialog.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    dialog.wait()
    subprocess.run(['/usr/bin/zenity', '--info', '--width=440',
                    '--title=AppFlowy Experiment — Synced',
                    '--text=Google Drive sync finished.\n\nYour pages and attachments are stored as encrypted records.'])
