package com.kami.ipreport

import android.os.ParcelFileDescriptor
import moe.shizuku.server.IShizukuService
import rikka.shizuku.Shizuku
import rikka.shizuku.ShizukuBinderWrapper

/** Run privileged (shell-uid) commands through Shizuku; no root needed. */
object ShizukuRunner {

    /** True when the Shizuku server is up and we hold its permission. */
    fun granted(): Boolean = try {
        Shizuku.pingBinder() &&
            Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
    } catch (t: Throwable) {
        false
    }

    /** Run one shell command; returns combined output ("" on failure). */
    fun run(cmd: String): String = try {
        val service = IShizukuService.Stub.asInterface(
            ShizukuBinderWrapper(Shizuku.getBinder()!!)
        )
        val p = service.newProcess(arrayOf("sh", "-c", cmd), null, null)
        val out = ParcelFileDescriptor.AutoCloseInputStream(p.inputStream)
            .bufferedReader().readText()
        val err = ParcelFileDescriptor.AutoCloseInputStream(p.errorStream)
            .bufferedReader().readText()
        p.waitFor()
        (out + err).trim()
    } catch (t: Throwable) {
        ""
    }
}
