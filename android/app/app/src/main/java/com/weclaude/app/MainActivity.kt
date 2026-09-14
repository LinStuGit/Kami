package com.weclaude.app

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.widget.Toast
import dev.rikka.shizuku.Shizuku
import kotlin.concurrent.thread
import org.json.JSONObject

/**
 * Thin WebView shell around the bridge's local control UI
 * (http://127.0.0.1:8800, served by control_server.py inside Termux).
 *
 * The page detects this activity through window.WeClaudeApp and unlocks
 * the Shizuku card: elevated commands run via Shizuku (shell uid, no root),
 * and the whole stack can be started through Termux RunCommandService.
 */
class MainActivity : Activity() {

    private lateinit var webView: WebView
    private var shizukuGranted = false

    private val binderReceivedListener = Shizuku.OnBinderReceivedListener {
        if (Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED) {
            shizukuGranted = true
        } else {
            Shizuku.requestPermission(REQ_SHIZUKU)
        }
    }

    private val permissionResultListener =
        Shizuku.OnRequestPermissionResultListener { requestCode, grantResult ->
            if (requestCode == REQ_SHIZUKU) {
                shizukuGranted = grantResult == PackageManager.PERMISSION_GRANTED
                toast(if (shizukuGranted) "Shizuku 已授权" else "Shizuku 授权被拒绝")
            }
        }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        webView = WebView(this)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
        }
        webView.addJavascriptInterface(Bridge(), "WeClaudeApp")
        webView.loadUrl(CONTROL_URL)
        setContentView(webView)

        Shizuku.addBinderReceivedListenerSticky(binderReceivedListener)
        Shizuku.addRequestPermissionResultListener(permissionResultListener)
    }

    override fun onDestroy() {
        super.onDestroy()
        Shizuku.removeBinderReceivedListener(binderReceivedListener)
        Shizuku.removeRequestPermissionResultListener(permissionResultListener)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }

    private fun toast(msg: String) =
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()

    /** Methods exposed to the web UI as window.WeClaudeApp.* */
    private inner class Bridge {

        @JavascriptInterface
        fun isApp(): Boolean = true

        @JavascriptInterface
        fun requestShizuku() {
            runOnUiThread {
                try {
                    if (Shizuku.checkSelfPermission()
                        == PackageManager.PERMISSION_GRANTED
                    ) {
                        shizukuGranted = true
                        toast("Shizuku 已授权")
                    } else {
                        Shizuku.requestPermission(REQ_SHIZUKU)
                    }
                } catch (e: IllegalStateException) {
                    toast("Shizuku 未运行：先启动 Shizuku APP")
                }
            }
        }

        /** Run one shell command through Shizuku (shell uid, no root). */
        @JavascriptInterface
        fun shizukuExec(cmd: String) {
            val output = try {
                val p = Shizuku.newProcess(arrayOf("sh", "-c", cmd), null, null)
                val out = p.inputStream.bufferedReader().readText()
                val err = p.errorStream.bufferedReader().readText()
                (out + err).trim().ifEmpty { "(ok, no output)" }
            } catch (e: Throwable) {
                "Shizuku error: $e"
            }
            val payload = JSONObject.quote(output)
            runOnUiThread {
                webView.evaluateJavascript("window.__shizukuResult($payload)") {}
            }
        }

        /** Boot the whole stack inside Termux (control server + daemon). */
        @JavascriptInterface
        fun startViaTermux() {
            val boot = buildString {
                append("termux-wake-lock; cd ~/weclaude; ")
                append("(pgrep -f control_server.py >/dev/null || ")
                append("python control_server.py >/dev/null 2>&1 &); ")
                append("python daemon.py start --no-ccswitch --no-llama")
            }
            val intent = Intent().apply {
                setClassName("com.termux", "com.termux.app.RunCommandService")
                action = "com.termux.RUN_COMMAND"
                putExtra(
                    "com.termux.RUN_COMMAND_PATH",
                    "/data/data/com.termux/files/usr/bin/bash",
                )
                putExtra(
                    "com.termux.RUN_COMMAND_ARGUMENTS",
                    arrayOf("-lc", boot),
                )
                putExtra(
                    "com.termux.RUN_COMMAND_WORKDIR",
                    "/data/data/com.termux/files/home",
                )
                putExtra("com.termux.RUN_COMMAND_BACKGROUND", true)
            }
            runOnUiThread {
                try {
                    startService(intent)
                    toast("已通知 Termux 启动 WeClaude")
                } catch (e: Exception) {
                    toast("Termux 未安装或不可达")
                }
            }
        }
    }

    companion object {
        private const val CONTROL_URL = "http://127.0.0.1:8800"
        private const val REQ_SHIZUKU = 7001
    }
}
