#!/usr/bin/env python3
"""Kami control server — mobile web UI + local control API.

Serves webui/index.html and a small JSON API used by the phone UI and the
Android wrapper app (see android/). Standard library only, so it runs on
Termux with zero extra installs.

Security model:
  - every mutating endpoint requires the token stored in control_token
  - arbitrary shell via /api/exec is loopback-only (the phone itself);
    non-loopback callers are limited to read-only actions

Usage:
    python control_server.py            # 127.0.0.1:8800
    python control_server.py --host 0.0.0.0 --port 8800
"""

import argparse
import json
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import daemon as daemon_mod  # reuse PID/status helpers; no side effects on import

BASE_DIR = Path(__file__).resolve().parent
from paths import CONFIG_DIR
TOKEN_FILE = CONFIG_DIR / "control_token"
LOG_FILE = BASE_DIR / "logs" / "weclaude.log"
WEBUI_FILE = BASE_DIR / "webui" / "index.html"
PY = sys.executable

_started = time.time()
_state_lock = threading.Lock()


def _load_token() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_hex(16)
    TOKEN_FILE.write_text(token)
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


TOKEN = _load_token()


# ── Actions ─────────────────────────────────────────────────────


def _daemon_cmd(*args: str) -> dict:
    """Run a daemon.py subcommand and capture its output."""
    try:
        r = subprocess.run(
            [PY, str(BASE_DIR / "daemon.py"), *args],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return {"code": r.returncode, "out": out[-4000:]}
    except Exception as e:
        return {"code": 1, "out": f"{type(e).__name__}: {e}"}


def _status() -> dict:
    llama_up = False
    try:
        import httpx

        llama_up = httpx.get(
            "http://127.0.0.1:8899/health", timeout=1.0
        ).status_code == 200
    except Exception:
        pass
    running = daemon_mod._is_running()
    bridge = daemon_mod._read_status().get("bridge")
    bridge_up = bool(bridge and daemon_mod._pid_alive(int(bridge)))
    return {
        "daemon": bool(running),
        "daemon_pid": running,
        "bridge": bridge_up,
        "bridge_pid": bridge,
        "llama": llama_up,
        "uptime_s": int(time.time() - _started),
        "log_exists": LOG_FILE.exists(),
    }


ACTIONS = {
    "status": lambda _: _status(),
    "daemon.status": lambda _: _daemon_cmd("status"),
    "daemon.start": lambda _: _daemon_cmd("start", "--no-ccswitch", "--no-llama"),
    "daemon.stop": lambda _: _daemon_cmd("stop"),
    "daemon.restart": lambda _: _daemon_cmd("restart", "--no-ccswitch", "--no-llama"),
    "log.tail": lambda args: _tail_log(int(args.get("n", ["200"])[0])),
}


# ── ADB endpoint auto-report (phone -> PC) ──────────────────────

ADB_ENDPOINT_FILE = CONFIG_DIR / "adb_endpoint.json"


def _adb_report(payload: dict) -> dict:
    """Store a reported wireless-debugging endpoint and connect to it."""
    ip = str(payload.get("ip", "")).strip()
    port = str(payload.get("port", "")).strip()
    if not re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", ip):
        return {"error": "bad endpoint"}
    try:
        ADB_ENDPOINT_FILE.write_text(
            json.dumps({"ip": ip, "port": port, "ts": time.time()}, indent=2)
        )
    except OSError:
        pass
    if not port.isdigit():
        # IP-only report (wireless debugging off): endpoint stored; the
        # adb watchdog will rescan ports on the new address.
        return {"code": 0, "out": "ip stored, no port — watchdog will scan"}
    try:
        r = subprocess.run(
            ["adb", "connect", f"{ip}:{port}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {"code": r.returncode, "out": ((r.stdout or "") + (r.stderr or "")).strip()}
    except Exception as e:
        return {"code": 1, "out": f"{type(e).__name__}: {e}"}


def _adb_status() -> dict:
    try:
        endpoint = json.loads(ADB_ENDPOINT_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        endpoint = None
    try:
        r = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=10
        )
        devices = r.stdout.strip()
    except Exception as e:
        devices = f"adb error: {e}"
    return {"endpoint": endpoint, "devices": devices}


def _tail_log(n: int) -> dict:
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()
        return {"code": 0, "out": "\n".join(lines[-n:])}
    except FileNotFoundError:
        return {"code": 0, "out": "(no log yet)"}


# ── HTTP handler ────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    # -- helpers --

    def _client_loopback(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _authorized(self, qs: dict) -> bool:
        supplied = (
            self.headers.get("X-Token")
            or (qs.get("t", [""])[0] if qs else "")
        )
        return bool(supplied) and secrets.compare_digest(supplied, TOKEN)

    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes --

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            self._serve_ui()
        elif parsed.path == "/api/status":
            if not self._authorized(qs):
                self._send_json({"error": "unauthorized"}, 401)
                return
            self._send_json(_status())
        elif parsed.path == "/api/adb":
            if not self._authorized(qs):
                self._send_json({"error": "unauthorized"}, 401)
                return
            self._send_json(_adb_status())
        elif parsed.path == "/api/action":
            self._do_action(qs)
        elif parsed.path == "/api/log/stream":
            self._stream_log(qs)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/adb/report":
            if not self._authorized(qs):
                self._send_json({"error": "unauthorized"}, 401)
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json({"error": "bad json"}, 400)
                return
            self._send_json(_adb_report(payload))
            return
        if parsed.path != "/api/exec":
            self._send_json({"error": "not found"}, 404)
            return
        if not self._authorized(qs):
            self._send_json({"error": "unauthorized"}, 401)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "bad json"}, 400)
            return
        cmd = str(payload.get("cmd", "")).strip()
        if not cmd:
            self._send_json({"error": "cmd required"}, 400)
            return
        if not self._client_loopback():
            # Remote callers may only run the read-only whitelist.
            if cmd not in ("daemon.status",):
                self._send_json(
                    {"error": "arbitrary exec is loopback-only"}, 403
                )
                return
        action = ACTIONS.get(cmd)
        if action is not None:
            self._send_json(action(qs))
            return
        # Arbitrary shell — loopback + token only (enforced above).
        self._send_json(_shell(cmd))

    def _do_action(self, qs: dict) -> None:
        if not self._authorized(qs):
            self._send_json({"error": "unauthorized"}, 401)
            return
        name = qs.get("name", [""])[0]
        action = ACTIONS.get(name)
        if action is None:
            self._send_json({"error": f"unknown action {name}"}, 400)
            return
        self._send_json(action(qs))

    def _serve_ui(self) -> None:
        try:
            body = WEBUI_FILE.read_bytes()
        except FileNotFoundError:
            self._send_json({"error": "webui/index.html missing"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream_log(self, qs: dict) -> None:
        """SSE stream: last 100 lines, then follow the file until close."""
        if not self._authorized(qs):
            self._send_json({"error": "unauthorized"}, 401)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def push(chunk: str) -> bool:
            try:
                self.wfile.write(chunk.encode("utf-8", "replace"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        try:
            offset = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
            if LOG_FILE.exists():
                text = LOG_FILE.read_text(errors="replace")
                for line in text.splitlines()[-100:]:
                    if not push(f"data: {line}\n\n"):
                        return
            if not push("data: ── live ──\n\n"):
                return
            while True:
                try:
                    size = LOG_FILE.stat().st_size
                except FileNotFoundError:
                    size = 0
                if size > offset:
                    with LOG_FILE.open("r", errors="replace") as f:
                        f.seek(offset)
                        chunk = f.read()
                    offset = size
                    for line in chunk.splitlines():
                        if not push(f"data: {line}\n\n"):
                            return
                elif size < offset:  # rotated
                    offset = 0
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def _shell(cmd: str) -> dict:
    """Run one shell command (loopback + token only). Output capped 8KB."""
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return {"code": r.returncode, "out": out[-8000:]}
    except subprocess.TimeoutExpired:
        return {"code": 124, "out": "(timeout)"}
    except Exception as e:
        return {"code": 1, "out": f"{type(e).__name__}: {e}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Kami control server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"Control server on http://{args.host}:{args.port}")
    print(f"Token: {TOKEN}  (also in {TOKEN_FILE})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nControl server stopped.")


if __name__ == "__main__":
    main()
