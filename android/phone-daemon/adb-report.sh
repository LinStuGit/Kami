#!/data/data/com.termux/files/usr/bin/bash
# adb-report — keep wireless debugging alive and report the endpoint to the PC.
#
# Loop every REPORT_INTERVAL s:
#   1. read the wireless-debugging TLS port (service.adb.tls.port)
#   2. when it is empty, try to enable wireless debugging via Shizuku (rish);
#      ColorOS may ignore the setting toggle — best effort
#   3. POST {"ip": <wlan0>, "port": <port>} to the PC control server, which
#      runs `adb connect` on receipt
#
# Token: $ADB_REPORT_TOKEN, or falls back to the bridge control_token.
# Single instance: callers should guard with pgrep -f adb-report.sh.

set -u

PC_URL="${ADB_REPORT_URL:-http://59.66.31.61:8800/api/adb/report}"
INTERVAL="${ADB_REPORT_INTERVAL:-60}"
TOKEN_FILE="$HOME/.config/kami/control_token"
LOG_TAG="adb-report"

log() { echo "$(date '+%m-%d %H:%M:%S') [$LOG_TAG] $*"; }

get_port() {
    # Wireless debugging TLS port; empty when the feature is off.
    getprop service.adb.tls.port 2>/dev/null | tr -d '\r[:space:]'
}

get_ip() {
    ip -4 addr show wlan0 2>/dev/null \
        | grep -o 'inet [0-9.]*' | awk '{print $2}' | head -1
}

enable_wireless_debugging() {
    # Needs WRITE_SECURE_SETTINGS — works through rish (Shizuku, shell uid).
    command -v rish >/dev/null 2>&1 || { log "rish not installed, cannot auto-enable"; return 1; }
    rish -c "settings put global adb_wifi_enabled 1" >/dev/null 2>&1 || return 1
    sleep 3
    get_port >/dev/null 2>&1
}

log "started (url=$PC_URL, interval=${INTERVAL}s)"

while true; do
    port=$(get_port)
    if [ -z "$port" ]; then
        enable_wireless_debugging >/dev/null 2>&1
        port=$(get_port)
    fi

    if [ -n "$port" ]; then
        ipaddr=$(get_ip)
        if [ -n "$ipaddr" ]; then
            token="${ADB_REPORT_TOKEN:-}"
            if [ -z "$token" ] && [ -r "$TOKEN_FILE" ]; then
                token=$(head -c 64 "$TOKEN_FILE" | tr -d '\r[:space:]')
            fi
            http_code=$(curl -s -o /dev/null -w '%{http_code}' \
                -X POST "$PC_URL?t=$token" \
                -H 'Content-Type: application/json' \
                -d "{\"ip\":\"$ipaddr\",\"port\":$port}" \
                --connect-timeout 8 --max-time 15)
            if [ "$http_code" = "200" ]; then
                log "reported $ipaddr:$port"
            else
                log "report failed (http $http_code)"
            fi
        else
            log "no wlan0 address (wifi down?)"
        fi
    else
        log "wireless debugging off and could not be enabled"
    fi

    sleep "$INTERVAL"
done
