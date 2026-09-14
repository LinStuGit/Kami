package com.kami.ipreport

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.concurrent.thread
import org.json.JSONObject

/**
 * Foreground reporter: posts the phone's Wi-Fi IPv4 (plus the wireless
 * debugging port when readable) to the PC control server on every network
 * change, with a heartbeat and failure backoff. Shizuku, when granted,
 * hardens keep-alive (battery whitelist + background appops), re-enables
 * wireless debugging when the port disappears, and reports ip-only updates
 * when it cannot.
 */
class ReportService : Service() {

    private val handler = Handler(Looper.getMainLooper())
    private var cm: ConnectivityManager? = null
    private var callback: ConnectivityManager.NetworkCallback? = null
    private var pending = false          // debounce flag for network events
    private var inFlight = false         // one report at a time
    private var failStep = 0             // index into BACKOFFS_MS
    private var hardened = false         // Shizuku keep-alive applied

    private val heartbeat = object : Runnable {
        override fun run() {
            maybeHarden()
            report("心跳")
            handler.postDelayed(this, HEARTBEAT_MS)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startInForeground("启动中…")
        if (cm == null) {
            cm = getSystemService(Context.CONNECTIVITY_SERVICE)
                as ConnectivityManager
            registerNetworkWatch()
        }
        maybeHarden()
        handler.removeCallbacks(heartbeat)
        handler.postDelayed(heartbeat, HEARTBEAT_MS)
        if (intent?.getBooleanExtra("test", false) == true) {
            report("手动")
        } else {
            scheduleReport()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        callback?.let { cm?.unregisterNetworkCallback(it) }
        callback = null
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    // ── Shizuku keep-alive hardening ────────────────────────────────

    private fun maybeHarden() {
        if (hardened || !ShizukuRunner.granted()) return
        hardened = true
        thread(start = true) {
            val me = packageName
            ShizukuRunner.run("dumpsys deviceidle whitelist +$me")
            ShizukuRunner.run("cmd appops set $me RUN_ANY_IN_BACKGROUND allow")
            ShizukuRunner.run("cmd appops set $me RUN_IN_BACKGROUND allow")
        }
    }

    // ── network watch ───────────────────────────────────────────────

    private fun registerNetworkWatch() {
        val mgr = cm ?: return
        val request = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = scheduleReport()
            override fun onLinkPropertiesChanged(
                network: Network,
                lp: LinkProperties,
            ) = scheduleReport()
        }
        mgr.registerNetworkCallback(request, callback!!)
    }

    /** Debounce bursts of network callbacks into one report. */
    private fun scheduleReport() {
        if (pending) return
        pending = true
        handler.postDelayed({ pending = false; report("网络变化") }, DEBOUNCE_MS)
    }

    // ── reporting ───────────────────────────────────────────────────

    private fun report(reason: String) {
        if (inFlight) return
        inFlight = true
        thread(start = true) {
            try {
                doReport(reason)
            } finally {
                inFlight = false
            }
        }
    }

    private fun doReport(reason: String) {
        val ip = currentWifiIp()
        if (ip.isEmpty()) {
            updateStatus("⚠️ 未连 Wi-Fi（$reason）")
            return
        }

        var port = prop("service.adb.tls.port").ifEmpty {
            prop("service.adb.tcp.port")
        }
        if (port.isEmpty() && ShizukuRunner.granted()) {
            // Wireless debugging off — switch it back on via shell, as
            // the old Termux adb-report.sh did through rish.
            ShizukuRunner.run("settings put global adb_wifi_enabled 1")
            try {
                Thread.sleep(3000)
            } catch (_: InterruptedException) {
            }
            port = prop("service.adb.tls.port").ifEmpty {
                prop("service.adb.tcp.port")
            }
        }

        val ok = try {
            httpPost(ip, port)
            true
        } catch (t: Throwable) {
            false
        }
        if (ok) {
            failStep = 0
            val shown = if (port.isEmpty()) ip else "$ip:$port"
            updateStatus("✅ 已上报 $shown · ${nowHm()}（$reason）")
        } else {
            val delay = BACKOFFS_MS[failStep.coerceAtMost(BACKOFFS_MS.size - 1)]
            failStep++
            updateStatus("❌ 上报失败，${delay / 1000}s 后重试")
            handler.postDelayed({ report("重试") }, delay)
        }
    }

    /** First wlan-prefixed IPv4 across active networks; "" when none. */
    private fun currentWifiIp(): String {
        val mgr = cm ?: return ""
        for (n in mgr.allNetworks) {
            val caps = mgr.getNetworkCapabilities(n) ?: continue
            if (!caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) {
                continue
            }
            val lp = mgr.getLinkProperties(n) ?: continue
            if (lp.interfaceName?.startsWith("wlan") != true) continue
            for (la in lp.linkAddresses) {
                val addr = la.address
                if (addr is Inet4Address && !addr.isLoopbackAddress &&
                    !addr.isLinkLocalAddress
                ) {
                    return addr.hostAddress ?: ""
                }
            }
        }
        return ""
    }

    private fun prop(name: String): String = try {
        val p = Runtime.getRuntime().exec(arrayOf("getprop", name))
        p.inputStream.bufferedReader().readText().trim()
    } catch (t: Throwable) {
        ""
    }

    @Throws(Exception::class)
    private fun httpPost(ip: String, port: String) {
        val prefs = getSharedPreferences("cfg", Context.MODE_PRIVATE)
        val base = prefs.getString("url", "")?.trim().orEmpty()
        require(base.isNotEmpty()) { "server url not configured" }
        val conn = URL(base.trimEnd('/') + "/api/adb/report")
            .openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.connectTimeout = 8000
        conn.readTimeout = 15000
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json")
        prefs.getString("token", "")?.takeIf { it.isNotEmpty() }?.let {
            conn.setRequestProperty("X-Token", it)
        }
        val body = JSONObject().put("ip", ip).put("port", port).toString()
        conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
        val code = conn.responseCode
        conn.disconnect()
        check(code == 200) { "http $code" }
    }

    // ── notification / status ───────────────────────────────────────

    private fun updateStatus(text: String) {
        getSharedPreferences("status", Context.MODE_PRIVATE).edit()
            .putString("text", text)
            .apply()
        startInForeground(text)
    }

    private fun nowHm(): String =
        SimpleDateFormat("HH:mm", Locale.US).format(Date())

    private fun startInForeground(text: String) {
        val mgr = getSystemService(Context.NOTIFICATION_SERVICE)
            as NotificationManager
        mgr.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                "IP 上报",
                NotificationManager.IMPORTANCE_LOW,
            ),
        )
        val n = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.sym_def_app_icon)
            .setContentTitle("Kami Reporter")
            .setContentText(text)
            .setOngoing(true)
            .build()
        startForeground(NOTIFY_ID, n)
    }

    companion object {
        private const val CHANNEL_ID = "reporter"
        private const val NOTIFY_ID = 1
        private const val HEARTBEAT_MS = 5 * 60_000L
        private const val DEBOUNCE_MS = 3_000L
        private val BACKOFFS_MS = longArrayOf(10_000, 30_000, 60_000, 300_000)
    }
}
