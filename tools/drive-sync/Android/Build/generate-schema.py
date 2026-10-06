# SPDX-License-Identifier: AGPL-3.0-only
"""Generate upstream Dart schemas in a separate, small Rust workspace."""
import os
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
frontend = root.parents[3] / 'frontend'
workspace = root / 'schema-workspace'
workspace.mkdir(exist_ok=True)
for name in ('flowy-ast', 'flowy-codegen'):
    shutil.copytree(frontend / 'rust-lib/build-tool' / name,
                    workspace / 'build-tool' / name, dirs_exist_ok=True)
shutil.copytree(root / 'schema-driver', workspace / 'schema-driver', dirs_exist_ok=True)
(workspace / 'Cargo.toml').write_text('''[workspace]
resolver = "2"
members = ["build-tool/flowy-ast", "build-tool/flowy-codegen", "schema-driver"]
[workspace.dependencies]
serde = "1"
serde_json = "1"
flowy-ast = { path = "build-tool/flowy-ast" }
[profile.dev]
debug = 0
''')
env = dict(os.environ)
env.update(CARGO_MAKE_WORKING_DIRECTORY=str(frontend),
           FLUTTER_FLOWY_SDK_PATH='appflowy_flutter/packages/appflowy_backend',
           CARGO_TARGET_DIR=str(root / 'schema-target'))
subprocess.run(['cargo', 'run', '--manifest-path', str(workspace / 'schema-driver/Cargo.toml'),
                '--jobs', '2', '--', str(frontend / 'rust-lib')], env=env, check=True)
