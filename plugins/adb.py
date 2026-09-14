#!/usr/bin/env python3
"""ADB extension — phone network-ADB watchdog + remote adb commands.

Ported out of daemon.py so the phone link is just another pluggable
capability. The watchdog keeps adbd reachable (fixed port 5555 set once via
`adb tcpip 5555`), falls back to the last reported endpoint, and rescans the
ephemeral port range after a phone reboot. No phone-side software involved.

Config via environment:
    WECLAUDE_PHONE_IP    phone IP        (default campus address)
    WECLAUDE_PHONE_PORT  fixed adbd port (default 5555)

WeChat commands:
    /phone        connection status
    /adb <args>   run an adb command, e.g. /adb shell dumpsys battery
"""

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fmt
from plugins import Plugin, PluginContext

logger = logging.getLogger(__name__)

PHONE_IP = os.environ.get("WECLAUDE_PHONE_IP", "183.173.44.182")
PHONE_PORT = int(os.environ.get("WECLAUDE_PHONE_PORT", "5555"))
SCAN_INTERVAL_S = 900  # full port rescan cadence after a phone reboot
CHECK_INTERVAL_S = 30
from paths import CONFIG_DIR
ADB_OUTPUT_LIMIT = 3000  # chars sent back to WeChat

_SILENT = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


class AdbPlugin(Plugin):
    name = "adb"
    description = "手机网络 ADB 看门狗 + 远程 adb 命令"
    commands = {
        "/phone": "/phone - 手机 ADB 连接状态",
        "/adb": "/adb <args> - 执行 adb 命令（如 /adb shell dumpsys battery）",
    }

    # ── lifecycle ──

    def on_start(self) -> None:
        self.ip = PHONE_IP
        self.fixed = f"{self.ip}:{PHONE_PORT}"
        self._stop = threading.Event()
        self._last_scan = 0.0
        self.is_up = False
        self._endpoint = self.fixed
        self._last_ok = 0.0
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info("adb plugin started (endpoint %s)", self.fixed)

    def on_stop(self) -> None:
        self._stop.set()

    # ── adb helpers ──

    def _adb(self, *args: str, timeout: int = 10) -> str:
        try:
            r = subprocess.run(
                ["adb", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                errors="replace",
                **_SILENT,
            )
            return (r.stdout or "") + (r.stderr or "")
        except Exception as e:
            return f"[adb error: {e}]"

    def _connected(self) -> bool:
        out = self._adb("devices")
        return any(
            len(parts) >= 2 and parts[1] == "device"
            for parts in (line.split() for line in out.splitlines()[1:])
        )

    # ── watchdog (ported from daemon.py) ──

    def _scan_port(self, port: int) -> int | None:
        try:
            s = socket.create_connection((self.ip, port), timeout=0.3)
            s.close()
            return port
        except OSError:
            return None

    def _rescan(self) -> int | None:
        """Find the live ADB port by scanning the ephemeral range."""
        logger.info("scanning %s for ADB port...", self.ip)
        with ThreadPoolExecutor(max_workers=400) as pool:
            open_ports = [
                p for p in pool.map(self._scan_port, range(32768, 61000))
                if p is not None
            ]
        logger.info("scan found open ports: %s", open_ports[:10])
        for port in open_ports:
            self._adb("connect", f"{self.ip}:{port}")
            if self._connected():
                logger.info("phone found on port %s", port)
                return port
        return None

    def _watchdog_tick(self) -> None:
        if not self._connected():
            self._adb("connect", self.fixed)
            if not self._connected():
                # stale endpoint from a previous TLS session as a fallback
                try:
                    data = json.loads(
                        (CONFIG_DIR / "adb_endpoint.json").read_text()
                    )
                    if data.get("port"):
                        alt = f"{data.get('ip', self.ip)}:{data['port']}"
                        if alt != self.fixed:
                            self._adb("connect", alt)
                except (OSError, ValueError, KeyError):
                    pass
            if (
                not self._connected()
                and time.time() - self._last_scan > SCAN_INTERVAL_S
            ):
                self._last_scan = time.time()
                if self._rescan() is not None:
                    return
        if self._connected():
            self.is_up = True
            self._last_ok = time.time()
        else:
            self.is_up = False

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self._watchdog_tick()
            except Exception as e:
                logger.error("phone watchdog error: %s", e)
            self._stop.wait(CHECK_INTERVAL_S)

    # ── WeChat commands ──

    def handle_command(self, cmd: str, args: str, ctx: PluginContext):
        if cmd == "/phone":
            connected = self.is_up or self._connected()
            age = (
                f"{int(time.time() - self._last_ok)}s 前"
                if self._last_ok
                else "从未"
            )
            return (
                fmt.block(
                    "📶", "手机连接",
                    fmt.kv("状态", "✅ 已连接" if connected else "⚠️ 未连接"),
                    fmt.kv("端点", self._endpoint),
                    fmt.kv("最近在线", age),
                    fmt.footer(
                        f"看门狗每 {CHECK_INTERVAL_S}s 检查 · "
                        f"失联后每 {SCAN_INTERVAL_S // 60} 分钟全端口扫描"
                    ),
                )
            )
        if cmd == "/adb":
            if not args:
                return "Usage: /adb <args>\nExample: /adb shell dumpsys battery"
            out = self._adb(*args.split(), timeout=30)
            out = out.strip() or "(no output)"
            if len(out) > ADB_OUTPUT_LIMIT:
                out = out[:ADB_OUTPUT_LIMIT] + f"\n…(truncated, {len(out)} chars)"
            return out
        return None
