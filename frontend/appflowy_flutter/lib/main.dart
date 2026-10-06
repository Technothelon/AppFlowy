import 'package:scaled_app/scaled_app.dart';
import 'package:flutter/widgets.dart';
import 'drive_experiment/runtime.dart';
import 'drive_experiment/start.dart';

import 'startup/startup.dart';

Future<void> main() async {
  ScaledWidgetsFlutterBinding.ensureInitialized(
    scaleFactor: (_) => 1.0,
  );

  if (!await DriveRuntime.initialize()) {
    throw StateError('Pair the experimental vault before opening AppFlowy.');
  }
  DriveRuntime.config.addAll(await DriveRuntime.engine('identity'));
  await DriveRuntime.saveConfig();
  await runAppFlowy(isAnon: true);
}

@pragma('vm:entry-point')
Future<void> driveEntryPoint() async {
  WidgetsFlutterBinding.ensureInitialized();
  await DriveRuntime.initialize();
  runApp(const DriveSyncApp());
}
