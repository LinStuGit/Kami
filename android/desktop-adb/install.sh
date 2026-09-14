#!/usr/bin/env bash
# Install desktop ADB control for Claude Code (one-time).
#
# Copies:
#   SKILL.md             -> <repo>/.claude/skills/android-control/SKILL.md
#   settings.local.json  -> <repo>/.claude/settings.local.json   (merges allow list)
#
# After this, Claude Code sessions in this repo can run adb without
# permission prompts and know the standard phone-control workflows.

set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

mkdir -p .claude/skills/android-control
cp android/desktop-adb/SKILL.md .claude/skills/android-control/SKILL.md

# Merge into existing settings.local.json if present.
if [[ -f .claude/settings.local.json ]]; then
    python - <<'PY'
import json
from pathlib import Path

p = Path(".claude/settings.local.json")
data = json.loads(p.read_text() or "{}")
allow = data.setdefault("permissions", {}).setdefault("allow", [])
if "Bash(adb:*)" not in allow:
    allow.append("Bash(adb:*)")
p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
print("merged permissions ->", p)
PY
else
    cp android/desktop-adb/settings.local.json .claude/settings.local.json
    echo "wrote .claude/settings.local.json"
fi

echo "Done. Restart Claude Code sessions in this repo to pick up the skill."
