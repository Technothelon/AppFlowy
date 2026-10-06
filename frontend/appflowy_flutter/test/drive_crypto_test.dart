// SPDX-License-Identifier: AGPL-3.0-only
import 'dart:convert';
import 'dart:io';
import 'package:appflowy/drive_experiment/runtime.dart';
import 'package:appflowy/drive_experiment/start.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:path_provider_platform_interface/path_provider_platform_interface.dart';

class _TestPaths extends PathProviderPlatform {
  _TestPaths(this.path);
  final String path;
  @override
  Future<String?> getApplicationSupportPath() async => path;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final fixtures = Directory(Directory(Platform.environment['APPFLOWY_DRIVE_TEST_FIXTURES'] ?? '../../tools/drive-sync/Android/Tests').resolveSymbolicLinksSync());
  final vectors =
      jsonDecode(File('${fixtures.path}/vectors.json').readAsStringSync())
          as Map;
  setUp(() {
    DriveRuntime.config = {
      'vault_id': vectors['vault_id'],
      'key': base64Encode(List<int>.from(vectors['key'] as List)),
      'media': {'Assets': 'Media/Assets'},
      'aliases': {'/desktop/Assets': 'Assets'}
    };
    DriveRuntime.ready = true;
    DriveRuntime.root = Directory('${fixtures.path}/DartProfile');
  });
  test(
      'Dart decrypts the desktop AES-GCM format and Python can decrypt its output',
      () async {
    final clear = await DriveRuntime.unseal(
        File('${fixtures.path}/python.afsync').readAsBytesSync());
    expect(utf8.decode(clear), vectors['content']);
    final output = await DriveRuntime.seal(clear);
    expect(utf8.decode(output.sublist(0, 9)), 'AFDRIVE1\n');
    await File('${fixtures.path}/dart.afsync').writeAsBytes(output);
  });
  test('tampered ciphertext and another vault identity are rejected', () async {
    final bytes = File('${fixtures.path}/python.afsync').readAsBytesSync();
    bytes[bytes.length - 1] ^= 1;
    await expectLater(DriveRuntime.unseal(bytes), throwsA(anything));
    DriveRuntime.config['vault_id'] = '44444444-4444-4444-8444-444444444444';
    await expectLater(
        DriveRuntime.unseal(
            File('${fixtures.path}/python.afsync').readAsBytesSync()),
        throwsA(anything));
  });
  test('desktop media paths resolve privately and traversal is rejected', () {
    expect(DriveRuntime.resolveMedia('/desktop/Assets/book/cover.png'),
        '${DriveRuntime.root.path}/Media/Assets/book/cover.png');
    expect(() => DriveRuntime.filePath('Assets/../escape'), throwsStateError);
    expect(() => DriveRuntime.filePath('/Assets/escape'), throwsStateError);
  });
  test(
      'pairing decrypts desktop bootstrap and refuses overwriting a paired vault',
      () async {
    DriveRuntime.ready = false;
    DriveRuntime.root = Directory('${fixtures.path}/PairingProfile');
    if (DriveRuntime.root.existsSync())
      DriveRuntime.root.deleteSync(recursive: true);
    // initialize() resolves this same isolated support directory after import.
    final oldPaths = PathProviderPlatform.instance;
    PathProviderPlatform.instance = _TestPaths(fixtures.path);
    addTearDown(() => PathProviderPlatform.instance = oldPaths);
    DriveRuntime.root = Directory('${fixtures.path}/DriveExperiment');
    if (DriveRuntime.root.existsSync())
      DriveRuntime.root.deleteSync(recursive: true);
    await DriveRuntime.pair(
        File('${fixtures.path}/python.afpair'), vectors['password'] as String);
    expect(DriveRuntime.ready, isTrue);
    expect(DriveRuntime.config['device_id'],
        isNot('33333333-3333-4333-8333-333333333333'));
    final stored = File(
        '${DriveRuntime.root.path}/Data/AppFlowy/data_beta.appflowy.cloud/123/collab_db/CURRENT');
    expect(stored.readAsStringSync(), 'test-only');
    await expectLater(
        DriveRuntime.pair(
            File('${fixtures.path}/python.afpair'), 'test-pair-code'),
        throwsStateError);
    DriveRuntime.root.deleteSync(recursive: true);
  });
  testWidgets('an unpaired phone offers pairing without starting the editor',
      (tester) async {
    DriveRuntime.ready = false;
    await tester.pumpWidget(const DriveSyncApp());
    expect(find.text('Pair this phone'), findsOneWidget);
    expect(find.text('Choose pairing file'), findsOneWidget);
    expect(find.text('Open AppFlowy'), findsNothing);
    expect(find.byType(TextField), findsOneWidget);
  });
}
