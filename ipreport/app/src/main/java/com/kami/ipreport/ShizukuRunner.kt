package com.kami.ipreport

import android.content.pm.PackageManager
import rikka.shizuku.Shizuku

/** Run privileged (shell-uid) commands through Shizuku; no root needed. */
object ShizukuRunner {

    /** True when the Shizuku server is up and we hold its permission. */
    fun granted(): Boolean = try {
        Shizuku.pingBinder() &&
            Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
    } catch (t: Throwable) {
        false
    }

    /** Run one shell command; returns combined output ("" when silent). */
    fun run(cmd: String): String = try {
        val p = Shizuku.newProcess(arrayOf("sh", "-c", cmd), null, null)
        val out = p.inputStream.bufferedReader().readText()
        val err = p.errorStream.bufferedReader().readText()
        p.waitFor()
        (out + err).trim()
    } catch (t: Throwable) {
        ""
    }
}
