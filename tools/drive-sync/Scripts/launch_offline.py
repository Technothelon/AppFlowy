# SPDX-License-Identifier: AGPL-3.0-only
"""Launch an existing Linux AppFlowy binary against an isolated offline profile."""
import fcntl
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Sync'))
from drive_sync import EXPERIMENT, load_config


def main():
    os.umask(0o077)
    config = load_config(EXPERIMENT / 'Sync/config.json')
    profile = config['_profile']
    launcher = Path(os.environ['APPFLOWY_DESKTOP_LAUNCHER']).resolve(strict=True)
    original_data = Path(os.environ.get(
        'APPFLOWY_ORIGINAL_DATA_PATH', str(Path.home() / '.local/share/AppFlowy')
    )).resolve(strict=True)
    for directory in ('State', 'Config', 'Cache', 'Run', 'Temp'):
        (profile / directory).mkdir(exist_ok=True)
    with (profile / 'State/session.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        command = [
            'bwrap', '--ro-bind', '/', '/', '--dev-bind', '/dev', '/dev',
            '--unshare-net', '--unshare-pid', '--proc', '/proc',
            '--die-with-parent', '--new-session',
            '--bind', str(profile), str(profile),
            '--bind', str(profile / 'Data/AppFlowy'), str(original_data),
            '--bind', str(profile / 'Temp'), '/tmp',
            '--ro-bind-try', '/tmp/.X11-unix', '/tmp/.X11-unix',
        ]
        for original_path, group in config.get('media_aliases', {}).items():
            if group not in config['media']:
                raise ValueError('Media alias refers to an unknown group.')
            source = profile / config['media'][group]
            destination = Path(original_path).resolve(strict=True)
            command += ['--bind', str(source), str(destination)]
        environment = {
            'XDG_DATA_HOME': str(profile / 'Data'),
            'XDG_CONFIG_HOME': str(profile / 'Config'),
            'XDG_CACHE_HOME': str(profile / 'Cache'),
            'XDG_RUNTIME_DIR': str(profile / 'Run'),
            'TMPDIR': str(profile / 'Temp'),
            'GDK_BACKEND': 'x11',
        }
        for name, value in environment.items():
            command += ['--setenv', name, value]
        command += [
            '--chdir', str(profile), '--', 'dbus-run-session',
            '--dbus-daemon=' + str(Path(__file__).with_name('session_bus.sh')),
            '--', str(launcher),
        ]
        return subprocess.run(command).returncode


if __name__ == '__main__':
    raise SystemExit(main())
