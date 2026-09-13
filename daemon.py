#!/usr/bin/env python3
"""WeClaude daemon — run the WeChat-Claude bridge as a supervised background daemon.

The supervisor keeps two things alive:
  - bridge.py: spawned and restarted with exponential backoff when it crashes
  - CC Switch: checked every 30 s; when it is not running, it is opened
    (a copy the user started by hand is left alone — it only gets started
     if nothing is running)

All output goes to logs/weclaude.log (rotated at 5 MB).

Usage:
    python daemon.py start     [-w DIR] [--ccswitch EXE | --no-ccswitch]
    python daemon.py stop                  stop the daemon (and bridge)
    python daemon.py restart   [-w DIR] [--ccswitch EXE | --no-ccswitch]
    python daemon.py status                show daemon and child status
    python daemon.py foreground [-w DIR] [--ccswitch EXE | --no-ccswitch]
    python daemon.py login                 interactive QR-code login (foreground)
    python daemon.py install-autostart     Windows: launch daemon at logon
    python daemon.py uninstall-autostart
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
BRIDGE = BASE_DIR / "bridge.py"
CONFIG_DIR = Path.home() / ".config" / "wechat-claude-bridge"
PID_FILE = CONFIG_DIR / "daemon.pid"
STATUS_FILE = CONFIG_DIR / "daemon_status.json"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "weclaude.log"
TASK_NAME = "WeClaude"

SUPERVISE_CMD = "__supervise"  # internal: run the supervisor loop

RESET_BACKOFF_AFTER = 600  # seconds of uptime that count as "stable"
RESTART_DELAY_CAP = 120  # max seconds between bridge restarts
CCSWITCH_CHECK_INTERVAL = 30  # seconds between cc-switch liveness checks
LLAMA_PORT = 8899  # llama-server listen port (bridge expects this)

IS_WINDOWS = sys.platform == "win32"


def _find_llama_model(exe: Path) -> Path | None:
    """Pick a main gguf model next to llama-server.exe.

    Prefers the distillation model, skips mmproj* (vision projectors).
    """
    candidates = [
        p
        for p in exe.parent.glob("*.gguf")
        if not p.name.lower().startswith("mmproj")
    ]
    if not candidates:
        return None
    for p in candidates:
        if "distill" in p.name.lower():
            return p
    return candidates[0]


def _find_llama() -> Path | None:
    """Locate llama-server.exe (searches drive roots for llama-* dirs)."""
    for root in (Path("D:/"), Path("C:/")):
        for d in sorted(root.glob("llama-*")):
            exe = d / "llama-server.exe"
            if exe.is_file():
                return exe
    return None

# The daemon must run fully silent: console children (bridge, tasklist,
# taskkill, schtasks) spawned from a console-less parent would otherwise
# each get a NEW visible console window on Windows. CREATE_NO_WINDOW
# prevents that; it must not be combined with DETACHED_PROCESS, which is
# used separately for the background supervisor itself.
if IS_WINDOWS:
    _SILENT: dict = {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    _DETACHED: dict = {
        "creationflags": 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    }
else:
    _SILENT = {}
    _DETACHED = {"start_new_session": True}


def _windowless_python() -> str:
    """pythonw.exe (no console) for e.g. the logon autostart task."""
    if IS_WINDOWS:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return sys.executable


def _print(msg: str) -> None:
    """print() that is safe under pythonw, where sys.stdout is None."""
    if sys.stdout is not None:
        print(msg)


def _find_ccswitch() -> Path | None:
    """Locate the CC Switch executable in its default install locations."""
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "CC Switch" / "cc-switch.exe")
    for c in candidates:
        if c.is_file():
            return c
    return None


# ── Logging ─────────────────────────────────────────────────────


def _open_log():
    """Open the log file, rotating it first if it grew past 5 MB."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            LOG_FILE.replace(LOG_FILE.with_suffix(LOG_FILE.suffix + ".1"))
    except OSError:
        pass  # another child holds the file — just append
    return open(LOG_FILE, "a", encoding="utf-8", buffering=1)


def _log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with _open_log() as f:
        f.write(f"{stamp} {msg}\n")


# ── Child env ───────────────────────────────────────────────────


def _child_env() -> dict:
    """Env for child processes — force UTF-8 so QR blocks and emoji
    survive redirection to the log file (Windows defaults to GBK)."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ── PID / status helpers ────────────────────────────────────────


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_pid(pid: int) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))


def _pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                # tasklist prints in the console codepage (GBK here); we only
                # match ASCII pids, so decode tolerantly.
                encoding="utf-8",
                errors="replace",
                timeout=10,
                **_SILENT,
            )
            return str(pid) in (result.stdout or "")
        except (subprocess.TimeoutExpired, OSError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _image_running(image: str) -> bool:
    """Check whether any process with this image name is running."""
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                **_SILENT,
            )
            return image.lower() in (result.stdout or "").lower()
        result = subprocess.run(
            ["pgrep", "-x", image],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _kill_tree(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=15,
            **_SILENT,
        )
    else:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            time.sleep(2)
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


_status_lock = threading.Lock()


def _set_status(key: str, value: int | str) -> None:
    """Record a child's pid in the shared status file."""
    with _status_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(STATUS_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        data[key] = value
        data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            STATUS_FILE.write_text(json.dumps(data, indent=2))
        except OSError:
            pass


def _read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# ── Supervisor ──────────────────────────────────────────────────


def _supervise_bridge(workdir: str | None, stop: threading.Event) -> None:
    """Keep bridge.py alive: spawn, wait, restart with backoff forever."""
    failures = 0
    while not stop.is_set():
        started = time.time()
        try:
            cmd = [sys.executable, str(BRIDGE)]
            if workdir:
                cmd.extend(["-w", workdir])
            with _open_log() as logf:
                logf.write(f"\n{time.strftime('%F %T')} ── launching bridge ──\n")
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(BASE_DIR),
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=_child_env(),
                    **_SILENT,
                )
            _set_status("bridge", proc.pid)
            _log(f"Bridge launched (pid {proc.pid})")
            returncode = proc.wait()
            uptime = time.time() - started
            _log(f"Bridge exited (code {returncode}, uptime {uptime:.0f}s)")
            if uptime >= RESET_BACKOFF_AFTER:
                failures = 0
        except Exception as e:
            _log(f"Bridge launch failed: {e}")

        failures += 1
        delay = min(2**failures, RESTART_DELAY_CAP)
        _log(f"Restarting bridge in {delay}s (failure #{failures})")
        stop.wait(delay)


def _ensure_ccswitch(exe: Path, stop: threading.Event) -> None:
    """Periodically check CC Switch; open it whenever it is not running.

    An instance the user started by hand is left untouched — we only
    ever launch the app, never monitor or restart a specific process.
    """
    image = exe.name
    while not stop.is_set():
        try:
            if not _image_running(image):
                _log(f"{image} not running, opening {exe}")
                # GUI app: nothing useful on stdout; keep it off our log
                # so foreign-encoded output can't corrupt the file.
                proc = subprocess.Popen(
                    [str(exe)],
                    cwd=str(exe.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    **_SILENT,
                )
                _set_status("ccswitch", proc.pid)
                _log(f"ccswitch opened (pid {proc.pid})")
                stop.wait(10)  # give it a moment before the next check
        except Exception as e:
            _log(f"ccswitch launch failed: {e}")
        stop.wait(CCSWITCH_CHECK_INTERVAL)


def _ensure_llamaserver(exe: Path, model: Path, stop: threading.Event) -> None:
    """Periodically check llama-server; open it whenever it is not running."""
    image = exe.name
    while not stop.is_set():
        try:
            if not _image_running(image):
                cmd = [
                    str(exe),
                    "-m",
                    str(model),
                    "--port",
                    str(LLAMA_PORT),
                    "--host",
                    "127.0.0.1",
                    "-ngl",
                    "99",  # offload all layers to GPU
                    "-c",
                    "4096",
                ]
                _log(f"{image} not running, starting (model={model.name})")
                # Server logs to its own file / console; keep it off ours.
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(exe.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    **_SILENT,
                )
                _set_status("llamaserver", proc.pid)
                _log(f"llamaserver opened (pid {proc.pid})")
                stop.wait(20)  # model load time before the next check
        except Exception as e:
            _log(f"llamaserver launch failed: {e}")
        stop.wait(CCSWITCH_CHECK_INTERVAL)


def _supervise(
    workdir: str | None, ccswitch: Path | None, llama: Path | None
) -> None:
    """Run forever: bridge supervised with restart, ccswitch kept alive."""
    pid = os.getpid()
    _write_pid(pid)
    _set_status("supervisor", pid)
    _log(f"Daemon started (pid {pid}), workdir={workdir or '(default)'}")

    if ccswitch is None:
        _log("ccswitch: not found, managing bridge only")
    if llama is None:
        _log("llamaserver: not found, skipping")

    stop = threading.Event()
    threads = [
        threading.Thread(target=_supervise_bridge, args=(workdir, stop), daemon=True)
    ]
    if ccswitch is not None:
        threads.append(
            threading.Thread(target=_ensure_ccswitch, args=(ccswitch, stop), daemon=True)
        )
    if llama is not None:
        model = _find_llama_model(llama)
        if model is None:
            _log("llamaserver: no .gguf model found next to exe, skipping")
        else:
            threads.append(
                threading.Thread(
                    target=_ensure_llamaserver, args=(llama, model, stop), daemon=True
                )
            )
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        _log("Daemon stopping")
        STATUS_FILE.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)


# ── Control commands ────────────────────────────────────────────


def _is_running() -> int | None:
    pid = _read_pid()
    if pid and _pid_alive(pid):
        return pid
    if PID_FILE.exists():
        PID_FILE.unlink(missing_ok=True)  # stale
    return None


def cmd_start(workdir: str | None, ccswitch: Path | None, llama: Path | None) -> None:
    running = _is_running()
    if running:
        _print(f"Daemon already running (pid {running}).")
        return

    kwargs = dict(_DETACHED)

    cmd = [sys.executable, str(Path(__file__).resolve()), SUPERVISE_CMD]
    if workdir:
        cmd.extend(["-w", workdir])
    if ccswitch is None:
        cmd.append("--no-ccswitch")
    else:
        cmd.extend(["--ccswitch", str(ccswitch)])
    if llama is None:
        cmd.append("--no-llama")
    else:
        cmd.extend(["--llama", str(llama)])

    with _open_log() as logf:
        subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_child_env(),
            **kwargs,
        )

    # Wait briefly and confirm the supervisor is up.
    for _ in range(20):
        time.sleep(0.25)
        pid = _is_running()
        if pid:
            _print(f"Daemon started (pid {pid}).")
            _print(f"Log: {LOG_FILE}")
            return
    _print("Daemon failed to start — check logs/weclaude.log")


def cmd_stop() -> None:
    pid = _is_running()
    if not pid:
        print("Daemon is not running.")
        return
    print(f"Stopping daemon (pid {pid})...")
    _kill_tree(pid)
    PID_FILE.unlink(missing_ok=True)
    STATUS_FILE.unlink(missing_ok=True)
    print("Daemon stopped.")


def cmd_status() -> None:
    pid = _is_running()
    if not pid:
        print("Daemon: not running")
        return
    print(f"Daemon: running (pid {pid})")

    bridge_pid = _read_status().get("bridge")
    if bridge_pid and _pid_alive(int(bridge_pid)):
        print(f"  bridge: running (pid {bridge_pid})")
    else:
        print("  bridge: not running")

    ccswitch = _find_ccswitch()
    if ccswitch is None:
        print("  ccswitch: not installed")
    elif _image_running(ccswitch.name):
        print(f"  ccswitch: running ({ccswitch.name})")
    else:
        print("  ccswitch: not running (daemon will open it within 30s)")
    llama = _find_llama()
    if llama is None:
        print("  llamaserver: not installed")
    elif _image_running(llama.name):
        print(f"  llamaserver: running (port {LLAMA_PORT})")
    else:
        print("  llamaserver: not running (daemon will open it within 30s)")
    print(f"Log: {LOG_FILE}")


def cmd_login() -> None:
    """Interactive QR login — must run in a real console."""
    print("Starting interactive login (scan the QR code with WeChat)...\n")
    result = subprocess.run([sys.executable, str(BRIDGE), "--login"])
    sys.exit(result.returncode)


def cmd_install_autostart() -> None:
    if not IS_WINDOWS:
        print("install-autostart is Windows-only for now.")
        sys.exit(1)
    tr = f'"{_windowless_python()}" "{Path(__file__).resolve()}" start'
    result = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/F",
            "/TN",
            TASK_NAME,
            "/SC",
            "ONLOGON",
            "/TR",
            tr,
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        **_SILENT,
    )
    if result.returncode == 0:
        print(f'Autostart task "{TASK_NAME}" created (runs at logon).')
    else:
        print(f"Failed: {(result.stderr or result.stdout or '').strip()}")


def cmd_uninstall_autostart() -> None:
    if not IS_WINDOWS:
        print("uninstall-autostart is Windows-only for now.")
        sys.exit(1)
    result = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        **_SILENT,
    )
    if result.returncode == 0:
        print(f'Autostart task "{TASK_NAME}" removed.')
    else:
        print(f"Failed: {(result.stderr or result.stdout or '').strip()}")


# ── CLI ─────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="WeClaude daemon controller")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kw) -> argparse.ArgumentParser:
        p = sub.add_parser(name, **kw)
        if name in ("start", "restart", "foreground", SUPERVISE_CMD):
            p.add_argument(
                "-w", "--workdir", default=None, help="bridge working directory"
            )
            p.add_argument(
                "--ccswitch",
                type=Path,
                default=None,
                metavar="EXE",
                help="path to cc-switch.exe to keep alive (default: auto-find)",
            )
            p.add_argument(
                "--no-ccswitch",
                action="store_true",
                help="do not manage CC Switch",
            )
            p.add_argument(
                "--llama",
                type=Path,
                default=None,
                metavar="EXE",
                help="path to llama-server.exe to keep alive (default: auto-find)",
            )
            p.add_argument(
                "--no-llama",
                action="store_true",
                help="do not manage llama-server",
            )
        return p

    add("start", help="start the daemon in the background")
    add("stop", help="stop the daemon")
    add("restart", help="restart the daemon")
    add("status", help="show daemon and child status")
    add("foreground", help="run the supervisor in this console")
    add("login", help="interactive QR-code login")
    add("install-autostart", help="Windows: launch daemon at logon")
    add("uninstall-autostart", help="Windows: remove logon autostart")
    add(SUPERVISE_CMD, help=argparse.SUPPRESS)  # internal

    args = parser.parse_args()
    workdir = getattr(args, "workdir", None)

    ccswitch: Path | None = None
    if getattr(args, "no_ccswitch", False):
        ccswitch = None
    else:
        ccswitch = getattr(args, "ccswitch", None) or _find_ccswitch()
        if ccswitch is None and args.command in ("start", "restart", "foreground"):
            _print("CC Switch not found — managing bridge only.")

    llama: Path | None = None
    if getattr(args, "no_llama", False):
        llama = None
    else:
        llama = getattr(args, "llama", None) or _find_llama()
        if llama is None and args.command in ("start", "restart", "foreground"):
            _print("llama-server not found — fast local model disabled.")

    if args.command == "start":
        cmd_start(workdir, ccswitch, llama)
    elif args.command == "stop":
        cmd_stop()
    elif args.command == "restart":
        cmd_stop()
        time.sleep(1)
        cmd_start(workdir, ccswitch, llama)
    elif args.command == "status":
        cmd_status()
    elif args.command == "foreground":
        names = ["bridge"] + (["ccswitch"] if ccswitch else [])
        names += ["llamaserver"] if llama else []
        _print(f"Supervisor running in foreground ({', '.join(names)}).")
        _print(f"Log: {LOG_FILE}. Ctrl+C to stop.")
        try:
            _supervise(workdir, ccswitch, llama)
        except KeyboardInterrupt:
            _print("\nSupervisor stopped.")
    elif args.command == "login":
        cmd_login()
    elif args.command == "install-autostart":
        cmd_install_autostart()
    elif args.command == "uninstall-autostart":
        cmd_uninstall_autostart()
    elif args.command == SUPERVISE_CMD:
        _supervise(workdir, ccswitch, llama)


if __name__ == "__main__":
    main()
