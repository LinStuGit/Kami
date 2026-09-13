"""Persistent memory system inspired by OpenClaw.

Stores long-term memories in MEMORY.md and daily conversation logs.
No vector DB — uses simple keyword matching for retrieval.

Old daily logs follow an Ebbinghaus-style decay: verbatim for a few days,
then compressed into a one-line MEMORY.md summary (via the local model),
then forgotten entirely once the summary itself expires.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from llm_local import ask_local

logger = logging.getLogger(__name__)

MEMORY_DIR = Path.home() / ".config" / "wechat-claude-bridge" / "memory"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
_lock = threading.Lock()

# Forgetting-curve retention tiers.
KEEP_RAW_DAYS = 3       # verbatim logs kept this long (matches /search window)
SUMMARY_TTL_DAYS = 30   # compressed summaries live this long, then are dropped
CHECK_INTERVAL_S = 6 * 3600  # consolidation pass cadence
_SUMMARY_RE = re.compile(r"^- \[dialog\] (\d{4}-\d{2}-\d{2})")


def _ensure_dir() -> None:
    """Create the memory directory if it does not exist."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


class MemoryStore:
    """Simple file-based memory system."""

    def __init__(self) -> None:
        _ensure_dir()

    def remember(self, content: str, category: str = "general") -> str:
        """Save a memory entry to MEMORY.md.

        Args:
            content: The fact/preference/decision to remember.
            category: Category tag (general, preference, fact, decision).

        Returns:
            Confirmation message.
        """
        _ensure_dir()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- [{category}] {content} _(saved {timestamp})_\n"

        with _lock:
            existing = ""
            if MEMORY_FILE.exists():
                existing = MEMORY_FILE.read_text()

            if not existing:
                existing = "# Memory\n\n"

            existing += entry
            MEMORY_FILE.write_text(existing)

        return f"Remembered: {content}"

    def forget(self, keyword: str) -> str:
        """Remove memory entries matching keyword.

        Args:
            keyword: Keyword to search for in memory entries.

        Returns:
            Result message indicating how many entries were removed.
        """
        with _lock:
            if not MEMORY_FILE.exists():
                return "No memories found."

            lines = MEMORY_FILE.read_text().splitlines(keepends=True)
            original_count = len([line for line in lines if line.startswith("- ")])

            keyword_lower = keyword.lower()
            filtered = [
                line
                for line in lines
                if not (line.startswith("- ") and keyword_lower in line.lower())
            ]

            removed = original_count - len(
                [line for line in filtered if line.startswith("- ")]
            )
            if removed == 0:
                return f"No memories matching '{keyword}' found."

            MEMORY_FILE.write_text("".join(filtered))

        return f"Forgot {removed} memory entries matching '{keyword}'."

    def list_memories(self) -> str:
        """List all stored memories.

        Returns:
            Formatted string of all memories, or a message if empty.
        """
        with _lock:
            if not MEMORY_FILE.exists():
                return "No memories stored yet.\nUse /remember <content> to save."

            content = MEMORY_FILE.read_text().strip()
            if not content or content == "# Memory":
                return "No memories stored yet.\nUse /remember <content> to save."

        return content

    def get_context(self) -> str:
        """Get memory content for injection into Claude's prompt.

        Returns:
            Memory context string, or empty string if no memories.
        """
        with _lock:
            if not MEMORY_FILE.exists():
                return ""

            content = MEMORY_FILE.read_text().strip()
            if not content or content == "# Memory":
                return ""

        return f"[Persistent Memory]\n{content}\n"

    def log_conversation(self, user_msg: str, bot_reply: str) -> None:
        """Append a conversation exchange to today's daily log.

        Args:
            user_msg: The user's message (first 200 chars).
            bot_reply: The bot's reply (first 200 chars).
        """
        _ensure_dir()
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = MEMORY_DIR / f"{today}.md"
        timestamp = datetime.now().strftime("%H:%M")

        entry = (
            f"### {timestamp}\n"
            f"**User:** {user_msg[:200]}\n"
            f"**Bot:** {bot_reply[:200]}\n\n"
        )

        with _lock:
            if not log_file.exists():
                header = f"# Conversation Log — {today}\n\n"
                log_file.write_text(header + entry)
            else:
                with open(log_file, "a") as f:
                    f.write(entry)

    def get_today_log(self) -> str:
        """Get today's conversation log.

        Returns:
            Today's log content, or message if no log exists.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = MEMORY_DIR / f"{today}.md"

        with _lock:
            if not log_file.exists():
                return "No conversation log for today."
            return log_file.read_text()

    def search(self, query: str) -> str:
        """Search memories and recent logs by keyword.

        Args:
            query: Search keyword.

        Returns:
            Matching entries from memory and recent logs.
        """
        query_lower = query.lower()
        results: list[str] = []

        with _lock:
            # Search MEMORY.md
            if MEMORY_FILE.exists():
                for line in MEMORY_FILE.read_text().splitlines():
                    if line.startswith("- ") and query_lower in line.lower():
                        results.append(f"[memory] {line}")

            # Search recent daily logs (last 3 days)
            for i in range(3):
                day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                log_file = MEMORY_DIR / f"{day}.md"
                if log_file.exists():
                    for line in log_file.read_text().splitlines():
                        if query_lower in line.lower() and line.strip():
                            results.append(f"[{day}] {line}")

        if not results:
            return f"No results for '{query}'."

        return f"Search results for '{query}':\n" + "\n".join(results[:20])

    # ── Forgetting-curve maintenance ────────────────────────────

    def start_maintenance(self) -> None:
        """Start the background consolidation loop (runs once at boot)."""
        threading.Thread(target=self._maintenance_loop, daemon=True).start()

    def _maintenance_loop(self) -> None:
        while True:
            try:
                self.consolidate()
            except Exception as e:
                logger.error("Memory consolidation failed: %s", e)
            time.sleep(CHECK_INTERVAL_S)

    def consolidate(self) -> None:
        """One forgetting-curve pass.

        Daily logs older than KEEP_RAW_DAYS are compressed into a single
        [dialog] line in MEMORY.md and the raw file removed; [dialog]
        summaries older than SUMMARY_TTL_DAYS are deleted outright.
        """
        _ensure_dir()
        cutoff = datetime.now() - timedelta(days=KEEP_RAW_DAYS)
        for path in sorted(MEMORY_DIR.glob("*.md")):
            if path == MEMORY_FILE:
                continue
            try:
                day = datetime.strptime(path.stem, "%Y-%m-%d")
            except ValueError:
                continue  # not a daily log
            if day >= cutoff:
                continue
            summary = self._summarize_log(path, day)
            if summary:
                self._append_summary(day, summary)
            path.unlink()
            logger.info("Consolidated %s into memory summary", path.name)
        self._prune_summaries()

    def _summarize_log(self, path: Path, day: datetime) -> str:
        """Compress one daily log to a summary line; extractive fallback."""
        turns: list[str] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("**User:**"):
                turns.append("用户: " + line[len("**User:**"):].strip())
            elif line.startswith("**Bot:**"):
                turns.append("助手: " + line[len("**Bot:**"):].strip())
        transcript = "\n".join(turns)[:6000]
        if not transcript:
            return ""

        date_str = day.strftime("%Y-%m-%d")
        summary = ask_local(
            f"以下是 {date_str} 一天的对话记录。请压缩成一段要点摘要（150字以内），"
            f"保留事实、决定、偏好和未完成事项，忽略寒暄。直接输出摘要正文：\n\n{transcript}",
            system="你是记忆压缩器。只输出摘要正文，不要评论。",
            max_tokens=300,
        )
        if not summary:
            # Local model down — keep the user's own words as the summary.
            user_lines = [t[4:] for t in turns if t.startswith("用户: ")]
            summary = "要点：" + "；".join(user_lines)
        return summary.replace("\n", " ").strip()[:400]

    def _append_summary(self, day: datetime, summary: str) -> None:
        entry = f"- [dialog] {day.strftime('%Y-%m-%d')} 对话摘要：{summary}\n"
        with _lock:
            existing = ""
            if MEMORY_FILE.exists():
                existing = MEMORY_FILE.read_text()
            if not existing.startswith("# Memory"):
                existing = "# Memory\n\n" + existing
            MEMORY_FILE.write_text(existing.rstrip("\n") + "\n" + entry)

    def _prune_summaries(self) -> None:
        """Drop [dialog] summaries that have outlived SUMMARY_TTL_DAYS."""
        ttl = datetime.now() - timedelta(days=SUMMARY_TTL_DAYS)
        with _lock:
            if not MEMORY_FILE.exists():
                return
            kept, dropped = [], 0
            for line in MEMORY_FILE.read_text().splitlines(keepends=True):
                m = _SUMMARY_RE.match(line)
                if m:
                    try:
                        if datetime.strptime(m.group(1), "%Y-%m-%d") < ttl:
                            dropped += 1
                            continue
                    except ValueError:
                        pass
                kept.append(line)
            if dropped:
                MEMORY_FILE.write_text("".join(kept))
                logger.info("Forgot %d expired dialog summaries", dropped)
