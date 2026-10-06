# Experimental Google Drive sync

This is an unofficial source fork of AppFlowy with an experimental encrypted
Google Drive transport for Linux and Android. The changes were prepared on
2026-10-06 against upstream tag `0.14.4`, commit
`5cf3a365dec0d59f64bad1ee4bb1050471a39b93`.

It is a prototype tied to the tested AppFlowy backend, not a supported sync
provider or a plugin that works with arbitrary official releases. The normal
Android application cannot use this transport.

## Architecture

The Linux editor is unmodified. A separate Python helper receives collaboration
updates before the editor starts and uploads them after it exits. The supplied
launcher redirects the editor into a separate profile and a network-isolated
Bubblewrap sandbox. The helper connects to Drive using rclone.

The modified Android Flutter client includes a pairing/sync screen and a separate
editor process. The editor installs a native filter that denies internet sockets
before Flutter starts. Its sync process uses Google's OAuth and Drive APIs.
A separate Rust library reads and updates the closed collaboration store.

Both adapters preserve original Yjs update bytes, validate merges using a matching
Yrs decoder, and append distinct updates instead of replacing a physical database.
Navigation, document and database collaboration objects use the same transport.
Selected attachment directories are transferred alongside those objects.

Records are immutable, randomly named AES-256-GCM archives. The encryption key
remains on paired devices. Google can see outer filenames, sizes and transfer
times. Authentication preferences and editing identities remain device-local.

## Source map

- `frontend/appflowy_flutter/lib/drive_experiment`: Android pairing, encryption,
  transport and frontend/native compatibility helpers.
- `frontend/appflowy_flutter/android`: separate sync/editor processes and packaging.
- `tools/drive-sync/Sync`: Linux transport and closed-store adapter.
- `tools/drive-sync/Android/Native`: Rust closed-store adapter and network filter.
- `tools/drive-sync/Android/create_pairing.py`: private encrypted device bootstrap.
- `tools/drive-sync/Scripts`: configurable offline Linux launcher.
- `tools/drive-sync/Android/Build`: source-only schema, UI and native build helpers.

## Private runtime configuration

Keep live profiles outside this checkout. Set `APPFLOWY_EXPERIMENT_ROOT` to an
isolated experimental directory before running the desktop or pairing tools.
Start from an independent, closed copy of a local AppFlowy profile, including its
preferences and media. Never point these tools at a primary or running store.
Creating that initial profile automatically is outside this public export.

Copy `tools/drive-sync/Sync/config.example.json` to
`$APPFLOWY_EXPERIMENT_ROOT/Sync/config.json` and replace every placeholder. The
workspace ID and user ID must match the copied store. Choose a new vault ID and
device ID. The remote must be a configured Google Drive rclone remote targeting
`AppFlowy-Experiment-Sync/<vault_id>`. Use a private credential copy and a locally
generated 32-byte encryption key; neither belongs in Git.

`media_aliases` maps old absolute attachment-directory paths to configured media
groups. The offline Linux launcher redirects those paths to the private copies;
Android resolves them into its own media folders. Alias destination directories
must already exist. Only configure aliases for this isolated experiment.

The Linux launcher also requires `APPFLOWY_DESKTOP_LAUNCHER`, pointing to an
installed AppFlowy executable or launcher. `APPFLOWY_ORIGINAL_DATA_PATH` optionally
overrides the installed data directory, normally `~/.local/share/AppFlowy`.
The bind mount redirects that directory inside the sandbox; the original remains
outside the sandbox. Linux dependencies include Bubblewrap, dbus-run-session,
rclone, and optionally Zenity/notify-send for the desktop wrappers.

With the Python dependencies installed in a virtual environment:

```sh
python tools/drive-sync/Sync/drive_sync.py inspect
python tools/drive-sync/Sync/drive_sync.py sync
python tools/drive-sync/Sync/launch_with_sync.py
```

The Android pairing helper writes an encrypted `.afpair` and a separate pairing
code under the private runtime directory. This bootstrap includes the initial
database, Drive credentials and vault key. Transfer it directly to a trusted
phone; do not publish it or put it in the Drive archive. Pairing refuses to
overwrite an existing vault.

On Android, open the experimental app, wait for sync to finish, then open the
editor. After editing, return home and tap the cloud-sync button. Keep the sync
screen open until it finishes. Sync requires the editor process to be closed.

## Validation and limits

The private implementation was tested on Linux and an arm64 Android 15 phone.
Desktop-created pages, text and images arrived on the phone. Android-created
pages, text and identical image bytes returned to the desktop and remained
readable after reopening. Public tests use only synthetic stores and keys;
private-vault evidence and screenshots are deliberately absent from this fork.

Android database views/editing, private sections, every attachment type, export
and other stock UI operations have not been validated. Existing public frontend
schemas differed from the tested native backend's document-session and page
creation APIs; this fork contains specific compatibility handling. Neither a
successful build nor successful page sync establishes all-feature compatibility.

Other limits:

- Only configured `Media/Assets` and `Media/Full-Source` groups are transferred.
- Different bytes at an existing attachment path pause import.
- Attachment deletions and physical database purges are not propagated.
- Old remote records accumulate; remote compaction is not implemented.
- Mobile records are limited to 256 MiB and new attachments to 128 MiB each.
- Android copied BackupEngine snapshots require the matching restoration API;
  there is no recovery UI or automatic retention management.
- Updating AppFlowy requires source rebasing, native compatibility checks and
  cross-device migration tests. Keep a tested desktop/Android version pair.

The exported desktop wrapper uses configurable paths and remote names. Its
synthetic tests are distinct from the original private GUI round-trip evidence.
Do not assume this source export has been freshly installed and tested on another
person's devices.

## Build and synthetic tests

The original Android experiment used Flutter 3.27.4, Rust 1.85, Android SDK 35,
NDK 24.0.8215888, Gradle 8.9, AGP 8.5.2 and Kotlin 1.9.24. The adapter uses
Yrs 0.21.3 and RocksDB 0.22. Install the Python dependencies from
`tools/drive-sync/Sync/requirements.txt`.

```sh
python -m unittest discover -s tools/drive-sync/Sync/Tests -p 'test_*.py'
cargo build --release --manifest-path tools/drive-sync/Android/Native/Cargo.toml
python tools/drive-sync/Android/Tests/test_native.py
python tools/drive-sync/Android/Tests/prepare_crypto.py
```

For an Android native adapter build, install the `aarch64-linux-android` Rust
target, set `ANDROID_HOME` and, if needed, `ANDROID_NDK_HOME` and `LIBCLANG_PATH`,
then run `tools/drive-sync/Android/Build/build-native-android.py`. It copies only
the newly built adapter into the Flutter JNI directory.

The schema helper constructs its own small Rust workspace without replacing
AppFlowy's original Cargo workspace. It requires Cargo, protoc and Dart's
`protoc_plugin` on PATH. Flutter dependencies and the upstream resource/code
generation steps are also required before a full frontend build. Run the UI
generator from `tools/drive-sync/Android/Build/generate-ui.py`, and run
`dart run build_runner build --delete-conflicting-outputs` in the frontend.

The synthetic encryption test runs from `frontend/appflowy_flutter` using
`flutter test test/drive_crypto_test.dart` after preparing its public fixtures.

**A complete Android build additionally requires a verified matching AppFlowy
native backend and its dependency/license checks.** The private experiment reused
a prebuilt backend whose API differs from the public frontend schemas. This
repository publishes no vendor backend binary, APK, signing key or promise of a
fully reproducible APK distribution. The original Rust workspace remains intact
for further source-build investigation.

## Licensing and publication

The upstream AGPLv3 license and notices are retained. New experimental sync source
is provided under AGPL-3.0-only; modified files are identified by this document and
the commit history. The fork is unofficial and is not endorsed by AppFlowy.

Only source, build definitions, synthetic tests and documentation are included.
There are no private notes, databases, screenshots, Drive credentials, pairing
bundles, vault keys, signing identities or APKs in these changes. Source publication
does not by itself complete corresponding-source or license checks for a future
binary distribution.
