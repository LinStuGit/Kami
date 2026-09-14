package com.kami.ipreport

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import dev.rikka.shizuku.Shizuku

/**
 * Config screen + switches for the IP reporter. No Termux involved: the
 * reporter is [ReportService]; Shizuku (optional) adds keep-alive
 * hardening and auto re-enables wireless debugging.
 */
class MainActivity : Activity() {

    private lateinit var etUrl: EditText
    private lateinit var etToken: EditText
    private lateinit var tvStatus: TextView

    private val binderReceivedListener = Shizuku.OnBinderReceivedListener {
        if (!ShizukuRunner.granted()) Shizuku.requestPermission(REQ_SHIZUKU)
    }

    private val permissionResultListener =
        Shizuku.OnRequestPermissionResultListener { requestCode, grantResult ->
            if (requestCode == REQ_SHIZUKU) {
                toast(
                    if (grantResult == PackageManager.PERMISSION_GRANTED)
                        "Shizuku 已授权，服务下次心跳自动加固"
                    else
                        "Shizuku 授权被拒绝"
                )
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = (16 * resources.displayMetrics.density).toInt()

        etUrl = EditText(this).apply {
            setSingleLine()
            hint = "PC 地址，如 http://192.168.1.10:8800"
        }
        etToken = EditText(this).apply {
            setSingleLine()
            hint = "control_token（PC ~/.config/kami/control_token）"
        }
        tvStatus = TextView(this)

        val cfg = getSharedPreferences(PREFS_CFG, MODE_PRIVATE)
        etUrl.setText(cfg.getString("url", DEFAULT_URL))
        etToken.setText(cfg.getString("token", ""))

        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }

        fun row(v: View) {
            box.addView(
                v,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                ).apply { topMargin = pad / 2 },
            )
        }

        row(TextView(this).apply {
            text = "Kami Reporter · 网络变化即上报新 IP"
            textSize = 18f
        })
        row(etUrl)
        row(etToken)
        row(Button(this).apply {
            text = "保存并启动"
            setOnClickListener {
                cfg.edit()
                    .putString("url", etUrl.text.toString().trim())
                    .putString("token", etToken.text.toString().trim())
                    .apply()
                askNotificationPermission()
                startReporter()
                toast("已保存，服务已启动")
                refreshStatus()
            }
        })
        row(Button(this).apply {
            text = "立即上报一次"
            setOnClickListener {
                startForegroundService(
                    Intent(this@MainActivity, ReportService::class.java)
                        .putExtra("test", true),
                )
            }
        })
        row(Button(this).apply {
            text = "授权 Shizuku（加固保活）"
            setOnClickListener {
                try {
                    if (ShizukuRunner.granted()) {
                        toast("Shizuku 已授权")
                    } else {
                        Shizuku.requestPermission(REQ_SHIZUKU)
                    }
                } catch (t: Throwable) {
                    toast("Shizuku 未运行：先打开 Shizuku APP 启动")
                }
            }
        })
        row(Button(this).apply {
            text = "电池优化豁免"
            setOnClickListener {
                try {
                    startActivity(
                        Intent(
                            Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                            Uri.parse("package:$packageName"),
                        ),
                    )
                } catch (t: Throwable) {
                    try {
                        startActivity(
                            Intent(
                                Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS,
                            ),
                        )
                    } catch (t2: Throwable) {
                        toast("请到系统设置里手动关闭电池优化")
                    }
                }
            }
        })
        row(tvStatus)
        setContentView(box)

        try {
            Shizuku.addBinderReceivedListenerSticky(binderReceivedListener)
            Shizuku.addRequestPermissionResultListener(permissionResultListener)
        } catch (t: Throwable) {
            // Shizuku not installed / not running yet — the button handles it.
        }
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    override fun onDestroy() {
        super.onDestroy()
        Shizuku.removeBinderReceivedListener(binderReceivedListener)
        Shizuku.removeRequestPermissionResultListener(permissionResultListener)
    }

    private fun startReporter() {
        startForegroundService(Intent(this, ReportService::class.java))
    }

    private fun askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    private fun refreshStatus() {
        val s = getSharedPreferences(PREFS_STATUS, MODE_PRIVATE)
        val text = s.getString("text", "") ?: ""
        tvStatus.text = "状态：${text.ifEmpty { "（未运行，保存后启动）" }}"
    }

    private fun toast(msg: String) =
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()

    companion object {
        private const val PREFS_CFG = "cfg"
        private const val PREFS_STATUS = "status"
        private const val DEFAULT_URL = "http://59.66.31.61:8800"
        private const val REQ_SHIZUKU = 7001
    }
}
