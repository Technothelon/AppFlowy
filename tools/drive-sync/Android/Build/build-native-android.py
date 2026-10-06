# SPDX-License-Identifier: AGPL-3.0-only
"""Build the source adapter with a locally installed Android NDK and Rust."""
import os
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
native = root.parent / 'Native'
sdk = Path(os.environ['ANDROID_HOME'])
ndk = Path(os.environ.get('ANDROID_NDK_HOME', str(sdk / 'ndk/24.0.8215888')))
llvm = ndk / 'toolchains/llvm/prebuilt/linux-x86_64'
env = dict(os.environ)
env.update(
    CC_aarch64_linux_android=str(llvm / 'bin/aarch64-linux-android29-clang'),
    CXX_aarch64_linux_android=str(llvm / 'bin/aarch64-linux-android29-clang++'),
    AR_aarch64_linux_android=str(llvm / 'bin/llvm-ar'),
    CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER=str(llvm / 'bin/aarch64-linux-android29-clang'),
    LIBCLANG_PATH=os.environ.get('LIBCLANG_PATH', '/usr/lib/llvm-18/lib'),
    BINDGEN_EXTRA_CLANG_ARGS_aarch64_linux_android=(
        f'--target=aarch64-linux-android --sysroot={llvm / "sysroot"} -D__ANDROID_API__=29'
    ),
    CXXFLAGS_aarch64_linux_android='-g0',
    CARGO_TARGET_DIR=str(native / 'target'),
)
subprocess.run(['cargo', 'build', '--release', '--target', 'aarch64-linux-android',
                '--manifest-path', str(native / 'Cargo.toml'), '--jobs', '2'], env=env, check=True)
destination = root.parents[3] / 'frontend/appflowy_flutter/android/app/src/main/jniLibs/arm64-v8a'
destination.mkdir(parents=True, exist_ok=True)
shutil.copy2(native / 'target/aarch64-linux-android/release/libafdrive.so', destination / 'libafdrive.so')
