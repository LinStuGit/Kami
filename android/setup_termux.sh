#!/data/data/com.termux/files/usr/bin/bash
# Kami — Termux one-shot bootstrap.
#
# Installs Node + Python, sets up the repo, Claude Code CLI, home-screen
# quick-launch shortcuts (Termux:Widget), boot autostart (Termux:Boot) and
# the Shizuku shell (rish) used by the Kami APK for elevated actions.
#
# Usage:  bash setup_termux.sh [REPO_URL]
# If REPO_URL is given the repo is cloned into ~/weclaude, otherwise an
# existing ~/weclaude (or $PWD copy of this repo) is used.

set -euo pipefail

PREFIX=/data/data/com.termux/files/usr
TARGET="$HOME/weclaude"
REPO_URL="${1:-}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

# ── 1. Base environment: node + python ─────────────────────────
say "Installing Termux packages (python, nodejs, git, termux-api)..."
pkg update -y
pkg install -y python nodejs-lts git termux-api
# cryptography has no manylinux wheel for Termux (bionic); use the pkg.
# httpx / qrcode are pure-python and install fine via pip.
pkg install -y python-cryptography || true

say "Python / Node versions:"
python -V
node -v

# ── 2. Repo ─────────────────────────────────────────────────────
if [[ -n "$REPO_URL" ]]; then
    say "Cloning $REPO_URL -> $TARGET"
    git clone "$REPO_URL" "$TARGET"
elif [[ ! -d "$TARGET" ]]; then
    if [[ -f "./bridge.py" ]]; then
        say "Using current directory as $TARGET"
        mkdir -p "$TARGET"
        cp -r ./* "$TARGET"/
    else
        echo "ERROR: no REPO_URL given and no existing ~/weclaude found." >&2
        exit 1
    fi
fi
cd "$TARGET"

say "Installing Python dependencies..."
# Termux forbids `pip install -U pip` (would shadow the python-pip package).
pip install -r requirements.txt

# ── 3. Claude Code CLI ──────────────────────────────────────────
say "Installing Claude Code CLI (npm)..."
if npm install -g @anthropic-ai/claude-code; then
    claude --version || true
else
    echo "WARN: claude-code install failed — run 'npm install -g @anthropic-ai/claude-code' later." >&2
fi

# ── 4. First-login hint (QR needs a real terminal) ─────────────
if [[ ! -f "$HOME/.config/kami/token.json" ]]; then
    say "WeChat not logged in yet. Run once in this terminal:  python bridge.py --login"
    say "(scan the QR with WeChat; token is then saved for background runs)"
fi

# ── 5. Termux:Widget home-screen shortcuts ─────────────────────
say "Installing home-screen shortcuts (needs the Termux:Widget app)..."
SHORTCUTS="$HOME/.shortcuts"
mkdir -p "$SHORTCUTS"

cat > "$SHORTCUTS/Kami 启动.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
cd ~/weclaude
pgrep -f "control_server.py" >/dev/null || python control_server.py >/dev/null 2>&1 &
python daemon.py start --no-ccswitch --no-llama
command -v termux-toast >/dev/null && termux-toast "Kami 已启动"
EOF

cat > "$SHORTCUTS/Kami 停止.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
cd ~/weclaude
python daemon.py stop
termux-wake-unlock
command -v termux-toast >/dev/null && termux-toast "Kami 已停止"
EOF

cat > "$SHORTCUTS/Kami 状态.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
cd ~/weclaude
python daemon.py status
echo
curl -s "http://127.0.0.1:8800/api/status?t=$(cat ~/.config/kami/control_token 2>/dev/null)" && echo
read -rn1 -p "... tap to close"
EOF

cat > "$SHORTCUTS/Kami 控制台.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
termux-open-url "http://127.0.0.1:8800"
EOF

chmod +x "$SHORTCUTS"/*.sh

# ── 6. Termux:Boot autostart ────────────────────────────────────
say "Installing boot autostart (needs the Termux:Boot app, open it once first)..."
BOOT="$HOME/.termux/boot"
mkdir -p "$BOOT"

cat > "$BOOT/00-weclaude.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
cd ~/weclaude
pgrep -f "control_server.py" >/dev/null || python control_server.py >/dev/null 2>&1 &
python daemon.py start --no-ccswitch --no-llama
EOF

chmod +x "$BOOT"/*.sh

# ── 7. Control server token + start now ────────────────────────
say "Starting control server (UI: http://127.0.0.1:8800)..."
pgrep -f "control_server.py" >/dev/null || \
    nohup python control_server.py >"$TARGET/logs-control.out" 2>&1 &
sleep 1
echo "    token: $(cat "$HOME/.config/kami/control_token")"

# ── 8. Shizuku shell (rish) for the APK's elevated buttons ─────
say "Shizuku (optional):"
cat <<'EOF'
    1. Install the Shizuku APP, start it (wireless debugging pairing).
    2. In Shizuku APP -> "在终端应用中使用 rish" -> export rish to Termux,
       then place it at $PREFIX/bin/rish and run:  chmod +x $PREFIX/bin/rish
    3. The Kami APK's Shizuku buttons (battery whitelist, keep-alive...)
       then work from the phone UI.
EOF

say "Done. Next steps:"
cat <<EOF
    - Open the console:   termux-open-url http://127.0.0.1:8800
    - Or install the Kami APK (android/app, built via GitHub Actions)
    - Widget shortcuts are on your home screen after adding a Termux:Widget
    - Reboot (or open Termux:Boot once) to test autostart
EOF
