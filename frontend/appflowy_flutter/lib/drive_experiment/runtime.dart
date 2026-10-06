// SPDX-License-Identifier: AGPL-3.0-only
import 'dart:convert';
import 'dart:ffi';
import 'dart:io';
import 'dart:isolate';
import 'dart:typed_data';

import 'package:archive/archive.dart';
import 'package:crypto/crypto.dart' as crypto;
import 'package:cryptography/cryptography.dart';
import 'package:ffi/ffi.dart';
import 'package:http/http.dart' as http;
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:uuid/uuid.dart';

typedef _RunNative = Pointer<Utf8> Function(Pointer<Utf8>);
typedef _FreeNative = Void Function(Pointer<Utf8>);

class DriveRuntime {
  static late Directory root;
  static late Map<String, dynamic> config;
  static bool ready = false;
  static const channelName = 'appflowy_drive_experiment';
  static const magic = 'AFDRIVE1\n';
  static const pairMagic = 'AFPAIR1\n';
  static const maxPack = 256 * 1024 * 1024;

  static Future<bool> initialize() async {
    final support = await getApplicationSupportDirectory();
    root = Directory(p.join(support.path, 'DriveExperiment'));
    final file = File(p.join(root.path, 'Sync', 'config.json'));
    ready = await file.exists();
    if (ready) {
      config = Map<String, dynamic>.from(
          jsonDecode(await file.readAsString()) as Map);
      config['profile'] = root.path;
    }
    return ready;
  }

  static Future<void> atomicJson(File file, Object value) async {
    await file.parent.create(recursive: true);
    final tmp = File('${file.path}.${const Uuid().v4()}.tmp');
    await tmp.writeAsString(jsonEncode(value), flush: true);
    await tmp.rename(file.path);
  }

  static Future<void> saveConfig() =>
      atomicJson(File(p.join(root.path, 'Sync', 'config.json')), config);

  static Future<Map<String, dynamic>> engine(String action,
      [Map<String, dynamic> args = const {}]) async {
    final input = jsonEncode({'action': action, 'config': config, ...args});
    final response = await Isolate.run(() {
      final lib = DynamicLibrary.open(const String.fromEnvironment(
          'AFDRIVE_NATIVE_LIBRARY',
          defaultValue: 'libafdrive.so'));
      final run =
          lib.lookupFunction<Pointer<Utf8> Function(Pointer<Utf8>), _RunNative>(
              'afdrive_json');
      final free =
          lib.lookupFunction<_FreeNative, void Function(Pointer<Utf8>)>(
              'afdrive_free');
      final request = input.toNativeUtf8();
      final result = run(request);
      try {
        return result.toDartString();
      } finally {
        calloc.free(request);
        free(result);
      }
    });
    final value = jsonDecode(response) as Map;
    if (value['ok'] != true) throw StateError(value['error'] as String);
    return Map<String, dynamic>.from(value['result'] as Map);
  }

  static Future<void> pair(File file, String password) async {
    if (ready || await root.exists())
      throw StateError(
          'This app already has a vault. Pairing cannot overwrite it.');
    final data = await file.readAsBytes();
    final header = utf8.encode(pairMagic);
    if (data.length < header.length + 44 ||
        data.length > maxPack ||
        !_same(data.sublist(0, header.length), header)) {
      throw StateError('This is not an experimental AppFlowy pairing file.');
    }
    var offset = header.length;
    final salt = data.sublist(offset, offset += 16);
    final nonce = data.sublist(offset, offset += 12);
    final key = await Pbkdf2(
            macAlgorithm: Hmac.sha256(), iterations: 310000, bits: 256)
        .deriveKey(secretKey: SecretKey(utf8.encode(password)), nonce: salt);
    final clear = await AesGcm.with256bits().decrypt(
      SecretBox(data.sublist(offset, data.length - 16),
          nonce: nonce, mac: Mac(data.sublist(data.length - 16))),
      secretKey: key,
      aad: header,
    );
    final archive = ZipDecoder().decodeBytes(clear, verify: true);
    _validateArchive(archive);
    final metadata = archive.findFile('pairing.json');
    if (metadata == null) throw StateError('Pairing metadata is missing.');
    final value = Map<String, dynamic>.from(
        jsonDecode(utf8.decode(metadata.content as List<int>)) as Map);
    if (value['protocol'] != 1 ||
        value['vault_id'] == null ||
        value['workspace_id'] == null)
      throw StateError('Unsupported pairing format.');
    final temporary = Directory('${root.path}.pairing-${const Uuid().v4()}');
    await temporary.create(recursive: true);
    try {
      for (final entry in archive) {
        if (!entry.isFile || entry.name == 'pairing.json') continue;
        if (!entry.name.startsWith('Data/AppFlowy/'))
          throw StateError('Unexpected pairing entry.');
        final target = File(p.join(temporary.path, entry.name));
        await target.parent.create(recursive: true);
        await target.writeAsBytes(entry.content as List<int>, flush: true);
      }
      value['profile'] = root.path;
      value['uid'] = value['uid'].toString();
      value['device_id'] = const Uuid().v4();
      value.remove('editor_client_id');
      value.remove('editor_device_id');
      final seed = value.remove('initial_sync_state');
      if (seed != null) {
        await atomicJson(
            File(p.join(temporary.path, 'State', 'drive-sync-state.json')),
            seed);
      }
      await atomicJson(
          File(p.join(temporary.path, 'Sync', 'config.json')), value);
      if (!await File(p.join(
              temporary.path,
              'Data/AppFlowy/data_beta.appflowy.cloud',
              value['uid'] as String,
              'collab_db/CURRENT'))
          .exists()) {
        throw StateError('The paired local store is missing.');
      }
      await temporary.rename(root.path);
    } catch (_) {
      if (await temporary.exists()) await temporary.delete(recursive: true);
      rethrow;
    }
    await initialize();
  }

  static bool _same(List<int> a, List<int> b) =>
      a.length == b.length &&
      List.generate(a.length, (i) => a[i] == b[i]).every((x) => x);
  static void _validateArchive(Archive archive) {
    final names = <String>{};
    var total = 0;
    for (final file in archive) {
      total += file.size;
      if (!names.add(file.name) ||
          file.name.startsWith('/') ||
          file.name.contains('\\') ||
          p.split(file.name).contains('..') ||
          file.isSymbolicLink ||
          file.size > maxPack ||
          total > 3 * maxPack) {
        throw StateError('Unsafe or oversized archive.');
      }
    }
  }

  static Future<SecretKey> _key() async =>
      SecretKey(base64Decode(config['key'] as String));
  static Future<Uint8List> seal(List<int> clear) async {
    final cipher = AesGcm.with256bits();
    final nonce = cipher.newNonce();
    final box = await cipher.encrypt(clear,
        secretKey: await _key(),
        nonce: nonce,
        aad: utf8.encode('$magic${config['vault_id']}'));
    return Uint8List.fromList(
        [...utf8.encode(magic), ...nonce, ...box.cipherText, ...box.mac.bytes]);
  }

  static Future<List<int>> unseal(List<int> bytes) async {
    final header = utf8.encode(magic);
    if (bytes.length > maxPack ||
        bytes.length < header.length + 28 ||
        !_same(bytes.sublist(0, header.length), header))
      throw StateError('Invalid encrypted sync record.');
    return AesGcm.with256bits().decrypt(
      SecretBox(bytes.sublist(header.length + 12, bytes.length - 16),
          nonce: bytes.sublist(header.length, header.length + 12),
          mac: Mac(bytes.sublist(bytes.length - 16))),
      secretKey: await _key(),
      aad: utf8.encode('$magic${config['vault_id']}'),
    );
  }

  static String filePath(String logical) {
    final parts = p.posix.split(logical);
    final media = config['media'] as Map;
    if (parts.length < 2 ||
        parts.contains('..') ||
        logical.contains('\\') ||
        logical.startsWith('/') ||
        !media.containsKey(parts.first))
      throw StateError('Invalid attachment path.');
    final target = p.normalize(
        p.joinAll([root.path, media[parts.first] as String, ...parts.skip(1)]));
    final groupRoot =
        p.normalize(p.join(root.path, media[parts.first] as String));
    if (!p.isWithin(groupRoot, target))
      throw StateError('Attachment path escaped its folder.');
    return target;
  }

  static String resolveMedia(String source) {
    if (!ready) return source;
    for (final pair in (config['aliases'] as Map? ?? {}).entries) {
      final prefix = pair.key as String;
      if (source.startsWith('$prefix/'))
        return filePath('${pair.value}/${source.substring(prefix.length + 1)}');
    }
    if (source.startsWith('afdrive://'))
      return filePath(source.substring('afdrive://'.length));
    return source;
  }

  static Future<String> addImage(String source) async {
    final name = '${const Uuid().v4()}${p.extension(source)}';
    final logical = 'Assets/Android/$name';
    final destination = File(filePath(logical));
    await destination.parent.create(recursive: true);
    await File(source).copy(destination.path);
    // Persist the canonical desktop alias, so both clients resolve this path.
    return '${config['canonical_assets']}/Android/$name';
  }

  static Future<Map<String, String>> mediaHashes() async {
    final result = <String, String>{};
    for (final entry in (config['media'] as Map).entries) {
      final folder = Directory(p.join(root.path, entry.value as String));
      if (!await folder.exists()) continue;
      await for (final file
          in folder.list(recursive: true, followLinks: false)) {
        if (file is File) {
          final relative = p
              .relative(file.path, from: folder.path)
              .split(p.separator)
              .join('/');
          result['${entry.key}/$relative'] =
              (await crypto.sha256.bind(file.openRead()).first).toString();
        }
      }
    }
    return result;
  }

  static Future<void> sync(void Function(String) progress) async {
    final identity = await engine('identity');
    config.addAll(identity);
    await saveConfig();
    final stateFile = File(p.join(root.path, 'State', 'drive-sync-state.json'));
    final state = await stateFile.exists()
        ? Map<String, dynamic>.from(
            jsonDecode(await stateFile.readAsString()) as Map)
        : <String, dynamic>{
            'protocol': 1,
            'seen': <String>[],
            'published_objects': <String, dynamic>{},
            'published_media': <String, dynamic>{}
          };
    if (state['vault_id'] != null && state['vault_id'] != config['vault_id'])
      throw StateError('Sync state belongs to another vault.');
    state['vault_id'] = config['vault_id'];
    final seen = Set<String>.from(state['seen'] as List);
    final drive = _Drive(config['drive'] as Map);
    try {
      final packs = await drive.list();
      var index = 0;
      for (final pack in packs) {
        index++;
        final name = pack['name'] as String;
        if (seen.contains(name)) continue;
        if (!RegExp(r'^[0-9a-f]{32}\.afsync$').hasMatch(name) ||
            int.parse(pack['size'] as String) > maxPack)
          throw StateError('Unexpected Drive record.');
        progress('Receiving record $index of ${packs.length}…');
        final archive = ZipDecoder().decodeBytes(
            await unseal(await drive.download(pack['id'] as String)),
            verify: true);
        _validateArchive(archive);
        final manifestFile = archive.findFile('manifest.json');
        if (manifestFile == null) throw StateError('Sync manifest is missing.');
        final manifest =
            jsonDecode(utf8.decode(manifestFile.content as List<int>)) as Map;
        if (manifest['protocol'] != 1 ||
            manifest['vault_id'] != config['vault_id'] ||
            manifest['workspace_id'] != config['workspace_id'])
          throw StateError('Record belongs to another vault.');
        final objects = <String, dynamic>{};
        for (final entry in (manifest['objects'] as Map).entries) {
          final record = Map<String, dynamic>.from(entry.value as Map);
          final chunks = <String>[];
          for (final name in record['chunks'] as List) {
            if (!RegExp('^objects/${entry.key}/[0-9]+\\.bin\$')
                .hasMatch(name as String))
              throw StateError('Invalid collaboration entry.');
            final file = archive.findFile(name);
            if (file == null) throw StateError('Missing collaboration entry.');
            chunks.add(base64Encode(file.content as List<int>));
          }
          record['chunks'] = chunks;
          objects[entry.key as String] = record;
        }
        final additions = <String, List<int>>{};
        for (final entry in (manifest['media'] as Map).entries) {
          final record = entry.value as Map;
          final hash = record['hash'] as String;
          if (record['entry'] != 'media/$hash')
            throw StateError('Invalid attachment entry.');
          final file = archive.findFile(record['entry'] as String);
          if (file == null) throw StateError('Missing attachment entry.');
          final content = file.content as List<int>;
          if (crypto.sha256.convert(content).toString() != hash)
            throw StateError('Attachment checksum failed.');
          final destination = File(filePath(entry.key as String));
          if (await destination.exists()) {
            if ((await crypto.sha256.bind(destination.openRead()).first)
                    .toString() !=
                hash)
              throw StateError(
                  'An attachment changed on both devices. Existing bytes were preserved.');
          } else {
            additions[destination.path] = content;
          }
        }
        // Additions are kept on a failed import; retry is idempotent, including
        // an uncertain failure after the native atomic database commit.
        for (final entry in additions.entries) {
          final file = File(entry.key);
          await file.parent.create(recursive: true);
          if (await file.exists())
            throw StateError(
                'An attachment was created during import. Retry sync.');
          await file.writeAsBytes(entry.value, flush: true);
        }
        await engine('import', {'objects': objects});
        seen.add(name);
        state['seen'] = seen.toList();
        await atomicJson(stateFile, state);
      }
      progress('Checking local changes…');
      final objects = (await engine('inspect'))['objects'] as Map;
      final media = await mediaHashes();
      final publishedObjects = state['published_objects'] as Map;
      final publishedMedia = state['published_media'] as Map;
      final changed = objects.entries
          .where((entry) =>
              publishedObjects[entry.key] != (entry.value as Map)['hash'])
          .map((e) => e.key as String)
          .toList();
      final changedMedia =
          media.keys.where((key) => publishedMedia[key] != media[key]).toList();
      // Keep records small; one unusually large source attachment can require
      // a larger desktop transfer and will be refused by this mobile prototype.
      while (changed.isNotEmpty || changedMedia.isNotEmpty) {
        final ids = changed.take(150).toList();
        changed.removeRange(0, ids.length);
        final files = <String>[];
        var bytes = 0;
        while (changedMedia.isNotEmpty && files.length < 200) {
          final path = changedMedia.first;
          final size = await File(filePath(path)).length();
          if (size > 128 * 1024 * 1024)
            throw StateError(
                'An attachment exceeds this mobile prototype’s size limit.');
          if (files.isNotEmpty && bytes + size > 16 * 1024 * 1024) break;
          bytes += size;
          files.add(changedMedia.removeAt(0));
        }
        final exported =
            (await engine('export', {'ids': ids}))['objects'] as Map;
        final archive = Archive();
        final objectManifest = <String, dynamic>{};
        final mediaManifest = <String, dynamic>{};
        final entries = <String>{};
        for (final entry in exported.entries) {
          final record = Map<String, dynamic>.from(entry.value as Map);
          final names = <String>[];
          var i = 0;
          for (final raw in record['chunks'] as List) {
            final name = 'objects/${entry.key}/${i++}.bin';
            final data = base64Decode(raw as String);
            archive.addFile(ArchiveFile(name, data.length, data));
            names.add(name);
          }
          record['chunks'] = names;
          objectManifest[entry.key as String] = record;
        }
        for (final logical in files) {
          final file = File(filePath(logical));
          final content = await file.readAsBytes();
          final hash = crypto.sha256.convert(content).toString();
          if (hash != media[logical])
            throw StateError(
                'An attachment changed during export. Retry sync.');
          final entry = 'media/$hash';
          if (entries.add(entry))
            archive.addFile(ArchiveFile(entry, content.length, content));
          mediaManifest[logical] = {'hash': hash, 'entry': entry};
        }
        final manifest = utf8.encode(jsonEncode({
          'protocol': 1,
          'vault_id': config['vault_id'],
          'workspace_id': config['workspace_id'],
          'device_id': config['device_id'],
          'created': DateTime.now().toUtc().toIso8601String(),
          'objects': objectManifest,
          'media': mediaManifest
        }));
        archive
            .addFile(ArchiveFile('manifest.json', manifest.length, manifest));
        final sealed = await seal(ZipEncoder().encode(archive)!);
        if (sealed.length > maxPack)
          throw StateError('Sync record exceeds the mobile size limit.');
        final name = '${const Uuid().v4().replaceAll('-', '')}.afsync';
        progress('Sending encrypted changes…');
        await drive.upload(name, sealed);
        for (final id in ids) {
          publishedObjects[id] = (objects[id] as Map)['hash'];
        }
        for (final logical in files) {
          publishedMedia[logical] = media[logical];
        }
        seen.add(name);
        state['seen'] = seen.toList();
        await atomicJson(stateFile, state);
      }
      state['last_success'] = DateTime.now().toUtc().toIso8601String();
      await atomicJson(stateFile, state);
      progress('Google Drive sync finished.');
    } finally {
      drive.close();
    }
  }
}

class _Drive {
  _Drive(this.credentials);
  final Map credentials;
  final client = http.Client();
  String? token;
  DateTime? tokenExpiry;
  bool descendedToPacks = false;
  void close() => client.close();
  Future<Map<String, String>> headers() async {
    if (token == null ||
        tokenExpiry == null ||
        DateTime.now().isAfter(tokenExpiry!)) {
      final response = await client
          .post(Uri.https('oauth2.googleapis.com', '/token'), body: {
        'grant_type': 'refresh_token',
        'client_id': credentials['client_id'] as String,
        'client_secret': credentials['client_secret'] as String,
        'refresh_token': credentials['refresh_token'] as String,
      }).timeout(const Duration(seconds: 30));
      if (response.statusCode != 200)
        throw StateError(
            'Google Drive sign-in needs to be renewed on the desktop.');
      final value = jsonDecode(response.body) as Map;
      token = value['access_token'] as String;
      tokenExpiry = DateTime.now()
          .add(Duration(seconds: (value['expires_in'] as int? ?? 3600) - 60));
    }
    return {'Authorization': 'Bearer $token'};
  }

  Future<List<Map<String, dynamic>>> list() async {
    final files = <Map<String, dynamic>>[];
    String? next;
    do {
      final response = await client
          .get(
              Uri.https('www.googleapis.com', '/drive/v3/files', {
                'q':
                    "'${credentials['folder_id']}' in parents and trashed = false",
                'fields': 'nextPageToken,files(id,name,size,mimeType)',
                'pageSize': '1000',
                if (next != null) 'pageToken': next,
              }),
              headers: await headers())
          .timeout(const Duration(seconds: 30));
      if (response.statusCode != 200)
        throw StateError('Cannot list the experimental Drive folder.');
      final value = jsonDecode(response.body) as Map;
      files.addAll((value['files'] as List)
          .map((x) => Map<String, dynamic>.from(x as Map)));
      next = value['nextPageToken'] as String?;
    } while (next != null);
    // Upgrade the first pairing bundle's outer vault folder to the same
    // immutable-records subfolder used by the desktop transport.
    final folders = files.where((f) => f['mimeType'] == 'application/vnd.google-apps.folder').toList();
    if (folders.isNotEmpty) {
      if (descendedToPacks || files.length != 1 || folders.single['name'] != 'packs') {
        throw StateError('Unexpected folder inside the experimental Drive archive.');
      }
      descendedToPacks = true;
      credentials['folder_id'] = folders.single['id'];
      await DriveRuntime.saveConfig();
      return list();
    }
    files.sort((a, b) => (a['name'] as String).compareTo(b['name'] as String));
    if (files.map((f) => f['name']).toSet().length != files.length)
      throw StateError('Duplicate remote record names.');
    return files;
  }

  Future<Uint8List> download(String id) async {
    final response = await client
        .get(
            Uri.https(
                'www.googleapis.com', '/drive/v3/files/$id', {'alt': 'media'}),
            headers: await headers())
        .timeout(const Duration(minutes: 3));
    if (response.statusCode != 200)
      throw StateError('Cannot download an encrypted Drive record.');
    if (response.bodyBytes.length > DriveRuntime.maxPack)
      throw StateError('Drive record exceeds the mobile size limit.');
    return response.bodyBytes;
  }

  Future<void> upload(String name, Uint8List bytes) async {
    final boundary = 'afdrive${const Uuid().v4().replaceAll('-', '')}';
    final metadata = jsonEncode({
      'name': name,
      'parents': [credentials['folder_id']]
    });
    final body = BytesBuilder(copy: false)
      ..add(utf8.encode(
          '--$boundary\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n$metadata\r\n--$boundary\r\nContent-Type: application/octet-stream\r\n\r\n'))
      ..add(bytes)
      ..add(utf8.encode('\r\n--$boundary--\r\n'));
    final response = await client
        .post(
            Uri.https('www.googleapis.com', '/upload/drive/v3/files',
                {'uploadType': 'multipart'}),
            headers: {
              ...await headers(),
              'Content-Type': 'multipart/related; boundary=$boundary'
            },
            body: body.takeBytes())
        .timeout(const Duration(minutes: 3));
    if (response.statusCode != 200 && response.statusCode != 201)
      throw StateError(
          'Cannot upload the encrypted Drive record. Local changes were retained.');
  }
}
