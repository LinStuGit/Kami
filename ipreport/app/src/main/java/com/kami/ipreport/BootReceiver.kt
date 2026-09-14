package com.kami.ipreport

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restart the reporter after boot (skipped until the server is set). */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val cfg = context.getSharedPreferences("cfg", Context.MODE_PRIVATE)
        if (cfg.getString("url", "").isNullOrEmpty()) return
        context.startForegroundService(Intent(context, ReportService::class.java))
    }
}
