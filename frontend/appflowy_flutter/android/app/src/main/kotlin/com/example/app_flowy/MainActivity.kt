package io.appflowy.appflowy

import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import android.app.ActivityManager
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.Process

class MainActivity: FlutterActivity() {
    private external fun enforceOffline(): Boolean

    override fun onCreate(savedInstanceState: Bundle?) {
        System.loadLibrary("afdrive")
        check(enforceOffline()) { "The experimental editor could not be kept offline." }
        super.onCreate(savedInstanceState)
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "appflowy_drive_experiment")
            .setMethodCallHandler { call, result ->
                if (call.method == "closeAndSync") {
                    result.success(null)
                    finish()
                } else result.notImplemented()
            }
    }

    override fun onDestroy() {
        val closing = isFinishing
        super.onDestroy()
        if (closing) {
            startActivity(Intent(this, SyncActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            // AppFlowy has no shutdown export in this version. Give outstanding
            // autosaves time to finish, then release the closed editor process.
            Handler(Looper.getMainLooper()).postDelayed({ Process.killProcess(Process.myPid()) }, 1000)
        }
    }
}

class SyncActivity: FlutterActivity() {
    override fun getDartEntrypointFunctionName(): String = "driveEntryPoint"
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "appflowy_drive_experiment")
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "editorStopped" -> {
                        val manager = getSystemService(ACTIVITY_SERVICE) as ActivityManager
                        val running = manager.runningAppProcesses?.any { it.processName == "$packageName:editor" } ?: false
                        result.success(!running)
                    }
                    "openEditor" -> {
                        result.success(null)
                        startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                        finish()
                    }
                    else -> result.notImplemented()
                }
            }
    }
}
