# SPDX-License-Identifier: AGPL-3.0-only
"""Verify filesystem and network isolation with a synthetic desktop executable."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


class LauncherTests(unittest.TestCase):
    def test_editor_writes_private_copies_and_cannot_reach_internet(self):
        with tempfile.TemporaryDirectory(prefix='launcher-fixture-', dir=Path(__file__).parent) as directory:
            root = Path(directory).resolve()
            profile = root / 'Profile'
            original_data = root / 'OriginalData'
            original_media = root / 'OriginalMedia'
            private_data = profile / 'Data/AppFlowy'
            private_media = profile / 'Media/Assets'
            store = private_data / 'data_beta.appflowy.cloud/1/collab_db'
            for path in (original_data, original_media, private_media, store, root / 'Sync'):
                path.mkdir(parents=True, exist_ok=True)
            (store / 'CURRENT').write_text('synthetic-store-only')
            for path in (original_data, original_media):
                (path / 'marker.txt').write_text('original')
            for path in (private_data, private_media):
                (path / 'marker.txt').write_text('private')
            (root / 'key').write_bytes(os.urandom(32))
            vault = str(uuid.uuid4())
            config = {
                'protocol': 1, 'vault_id': vault, 'device_id': str(uuid.uuid4()),
                'workspace_id': str(uuid.uuid4()), 'uid': 1,
                'profile': str(profile), 'key_file': str(root / 'key'),
                'rclone_config': str(root / 'rclone.conf'),
                'remote': 'gdrive:AppFlowy-Experiment-Sync/' + vault,
                'media': {'Assets': 'Media/Assets'},
                'media_aliases': {str(original_media): 'Assets'},
            }
            (root / 'Sync/config.json').write_text(json.dumps(config))
            executable = root / 'synthetic-editor.py'
            executable.write_text('#!/usr/bin/python3\n' + '''
import socket
from pathlib import Path
try:
    connection = socket.create_connection(('1.1.1.1', 443), timeout=0.5)
except OSError:
    pass
else:
    connection.close()
    raise AssertionError('Editor reached the internet')
''' + f"Path({str(original_data / 'marker.txt')!r}).write_text('isolated edit')\n"
                + f"Path({str(original_media / 'marker.txt')!r}).write_text('isolated media edit')\n")
            executable.chmod(0o755)
            environment = dict(os.environ, APPFLOWY_EXPERIMENT_ROOT=str(root),
                               APPFLOWY_DESKTOP_LAUNCHER=str(executable),
                               APPFLOWY_ORIGINAL_DATA_PATH=str(original_data))
            launcher = Path(__file__).resolve().parents[2] / 'Scripts/launch_offline.py'
            result = subprocess.run([sys.executable, str(launcher)], env=environment,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((original_data / 'marker.txt').read_text(), 'original')
            self.assertEqual((original_media / 'marker.txt').read_text(), 'original')
            self.assertEqual((private_data / 'marker.txt').read_text(), 'isolated edit')
            self.assertEqual((private_media / 'marker.txt').read_text(), 'isolated media edit')


if __name__ == '__main__':
    unittest.main()
