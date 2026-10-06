// SPDX-License-Identifier: AGPL-3.0-only
import 'dart:io';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'runtime.dart';

const driveChannel = MethodChannel(DriveRuntime.channelName);

class DriveSyncApp extends StatelessWidget {
  const DriveSyncApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'AppFlowy Drive Experiment',
        theme: ThemeData(colorSchemeSeed: Colors.teal, useMaterial3: true),
        home: const _DriveHome(),
      );
}

class _DriveHome extends StatefulWidget {
  const _DriveHome();
  @override
  State<_DriveHome> createState() => _DriveHomeState();
}

class _DriveHomeState extends State<_DriveHome> {
  final password = TextEditingController();
  bool busy = false;
  bool paired = DriveRuntime.ready;
  String status = DriveRuntime.ready
      ? 'Ready to synchronize your experimental vault.'
      : 'Choose the private pairing file created on your computer.';

  @override
  void initState() {
    super.initState();
    if (paired) WidgetsBinding.instance.addPostFrameCallback((_) => sync());
  }

  @override
  void dispose() {
    password.dispose();
    super.dispose();
  }

  void progress(String value) {
    if (mounted) setState(() => status = value);
  }

  Future<void> pair() async {
    final selected = await FilePicker.platform.pickFiles();
    if (selected?.files.single.path == null || !mounted) return;
    setState(() {
      busy = true;
      status = 'Opening the private pairing file…';
    });
    try {
      await DriveRuntime.pair(
          File(selected!.files.single.path!), password.text.trim());
      password.clear();
      if (mounted) setState(() => paired = true);
      progress('Pairing finished.');
    } catch (_) {
      progress(
          'Pairing failed. Check the pairing file and code. No existing vault was replaced.');
    } finally {
      if (mounted) setState(() => busy = false);
    }
    if (paired && mounted) await sync();
  }

  Future<void> sync() async {
    if (busy || !paired) return;
    setState(() {
      busy = true;
      status = 'Waiting for the editor to close…';
    });
    try {
      var closed = false;
      for (var i = 0; i < 20; i++) {
        closed =
            await driveChannel.invokeMethod<bool>('editorStopped') ?? false;
        if (closed) break;
        await Future<void>.delayed(const Duration(milliseconds: 500));
      }
      if (!closed)
        throw StateError('Close the experimental editor before syncing.');
      await DriveRuntime.sync(progress);
    } catch (e) {
      progress(
          'Sync paused: ${e is StateError ? e.message : 'Check your connection and retry.'}');
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('AppFlowy Drive Experiment')),
        body: SafeArea(
            child: ListView(padding: const EdgeInsets.all(24), children: [
          const Icon(Icons.lock_outline, size: 48),
          const SizedBox(height: 24),
          Text(paired ? 'Your private experimental vault' : 'Pair this phone',
              style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 12),
          const Text(
              'AppFlowy edits stay on this phone. The sync component sends encrypted records to your Google Drive.'),
          const SizedBox(height: 24),
          if (!paired) ...[
            TextField(
                controller: password,
                obscureText: true,
                autocorrect: false,
                enableSuggestions: false,
                decoration: const InputDecoration(
                    labelText: 'Pairing code', border: OutlineInputBorder())),
            const SizedBox(height: 16),
            FilledButton(
                onPressed: busy ? null : pair,
                child: const Text('Choose pairing file')),
          ],
          if (busy) const LinearProgressIndicator(),
          const SizedBox(height: 16),
          SelectableText(status),
          const SizedBox(height: 24),
          if (paired) ...[
            FilledButton.icon(
                onPressed: busy ? null : sync,
                icon: const Icon(Icons.sync),
                label: const Text('Sync with Google Drive')),
            const SizedBox(height: 12),
            OutlinedButton(
                onPressed: busy
                    ? null
                    : () => driveChannel.invokeMethod<void>('openEditor'),
                child: const Text('Open AppFlowy')),
            const SizedBox(height: 16),
            const Text(
                'When finished editing, use the Drive sync button on AppFlowy’s home screen. Interrupted transfers can be retried.'),
          ],
        ])),
      );
}

class DriveSyncButton extends StatefulWidget {
  const DriveSyncButton({super.key});
  @override
  State<DriveSyncButton> createState() => _DriveSyncButtonState();
}

class _DriveSyncButtonState extends State<DriveSyncButton> {
  bool closing = false;
  @override
  Widget build(BuildContext context) => IconButton(
        tooltip: 'Close AppFlowy and sync with Drive',
        icon: closing
            ? const SizedBox(
                width: 20,
                height: 20,
                child: CircularProgressIndicator(strokeWidth: 2))
            : const Icon(Icons.cloud_sync_outlined),
        onPressed: closing
            ? null
            : () async {
                setState(() => closing = true);
                await Future<void>.delayed(const Duration(seconds: 1));
                await driveChannel.invokeMethod<void>('closeAndSync');
              },
      );
}
