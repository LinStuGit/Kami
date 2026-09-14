"""Scheduled task system inspired by OpenClaw's cron.

Supports one-shot reminders, interval tasks, and cron expressions.
Jobs persist to JSON and survive restarts.
"""

import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import fmt

logger = logging.getLogger(__name__)

from paths import CONFIG_DIR

JOBS_FILE = CONFIG_DIR / "jobs.json"


def _parse_interval(spec: str) -> int | None:
    """Parse interval string like '30m', '2h', '1d' to seconds.

    Args:
        spec: Interval string (e.g., '30m', '2h', '1d', '90s').

    Returns:
        Seconds as int, or None if parsing fails.
    """
    match = re.match(r"^(\d+)([smhd])$", spec.strip().lower())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


def _parse_time_spec(spec: str) -> float | None:
    """Parse time specification like '17:00', '2026-03-25 09:00', 'in 30m'.

    Args:
        spec: Time specification string.

    Returns:
        Unix timestamp, or None if parsing fails.
    """
    spec = spec.strip()

    # "in 30m" / "in 2h" format
    if spec.startswith("in "):
        secs = _parse_interval(spec[3:])
        if secs:
            return time.time() + secs
        return None

    # "HH:MM" format (today or tomorrow)
    match = re.match(r"^(\d{1,2}):(\d{2})$", spec)
    if match:
        h, m = int(match.group(1)), int(match.group(2))
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    # "YYYY-MM-DD HH:MM" format
    try:
        dt = datetime.strptime(spec, "%Y-%m-%d %H:%M")  # noqa: DTZ007
        return dt.timestamp()
    except ValueError:
        pass

    return None


def _cron_matches(expr: str, dt: datetime) -> bool:
    """Check if a cron expression matches a given datetime.

    Supports standard 5-field cron: minute hour day month weekday.
    Supports *, specific values, ranges (1-5), and step (*/5).

    Args:
        expr: Cron expression string (5 fields).
        dt: Datetime to check against.

    Returns:
        True if the expression matches the datetime.
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        return False

    values = [dt.minute, dt.hour, dt.day, dt.month, dt.isoweekday() % 7]

    for field, value in zip(fields, values):
        if not _cron_field_matches(field, value):
            return False
    return True


def _cron_field_matches(field: str, value: int) -> bool:
    """Check if a single cron field matches a value."""
    if field == "*":
        return True

    for part in field.split(","):
        # Step: */5 or 1-10/2
        if "/" in part:
            range_part, step_str = part.split("/", 1)
            step = int(step_str)
            if range_part == "*":
                if value % step == 0:
                    return True
            elif "-" in range_part:
                low, high = map(int, range_part.split("-", 1))
                if low <= value <= high and (value - low) % step == 0:
                    return True
        # Range: 1-5
        elif "-" in part:
            low, high = map(int, part.split("-", 1))
            if low <= value <= high:
                return True
        # Exact value
        elif value == int(part):
            return True

    return False


class Job:
    """A scheduled job."""

    def __init__(
        self,
        job_id: str,
        job_type: str,
        message: str,
        user_id: str,
        schedule: str,
        next_run: float,
        enabled: bool = True,
        run_claude: bool = False,
    ) -> None:
        self.job_id = job_id
        self.job_type = job_type  # "once", "interval", "cron"
        self.message = message
        self.user_id = user_id
        self.schedule = schedule  # time spec, interval spec, or cron expression
        self.next_run = next_run
        self.enabled = enabled
        self.run_claude = run_claude

    def to_dict(self) -> dict:
        """Serialize job to dictionary."""
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "message": self.message,
            "user_id": self.user_id,
            "schedule": self.schedule,
            "next_run": self.next_run,
            "enabled": self.enabled,
            "run_claude": self.run_claude,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        """Deserialize job from dictionary."""
        return cls(
            job_id=d["job_id"],
            job_type=d["job_type"],
            message=d["message"],
            user_id=d["user_id"],
            schedule=d["schedule"],
            next_run=d["next_run"],
            enabled=d.get("enabled", True),
            run_claude=d.get("run_claude", False),
        )


# Type for the callback: (user_id, message, run_claude) -> None
SendCallback = Callable[[str, str, bool], None]


class Scheduler:
    """Job scheduler with persistence and background execution."""

    def __init__(self) -> None:
        self._jobs: list[Job] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._callback: SendCallback | None = None
        self._load_jobs()

    def set_callback(self, callback: SendCallback) -> None:
        """Set the callback for delivering job messages.

        Args:
            callback: Function(user_id, message, run_claude) called when a job fires.
        """
        self._callback = callback

    def _load_jobs(self) -> None:
        """Load jobs from persistent JSON file."""
        try:
            if JOBS_FILE.exists():
                data = json.loads(JOBS_FILE.read_text())
                with self._lock:
                    self._jobs = [Job.from_dict(d) for d in data]
                logger.info("Loaded %d scheduled jobs.", len(self._jobs))
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning("Failed to load jobs: %s", e)

    def _save_jobs(self) -> None:
        """Persist jobs to JSON file."""
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data = [j.to_dict() for j in self._jobs]
        JOBS_FILE.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(JOBS_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def add_reminder(self, user_id: str, time_spec: str, message: str) -> str:
        """Add a one-shot reminder.

        Args:
            user_id: WeChat user ID.
            time_spec: When to fire (e.g., '17:00', 'in 30m').
            message: Reminder message.

        Returns:
            Confirmation or error message.
        """
        ts = _parse_time_spec(time_spec)
        if ts is None:
            return (
                f"⚠️ 时间格式看不懂：「{time_spec}」\n"
                "试试 17:00 · in 30m · 2026-03-25 09:00"
            )

        job = Job(
            job_id=uuid.uuid4().hex[:8],
            job_type="once",
            message=message,
            user_id=user_id,
            schedule=time_spec,
            next_run=ts,
        )
        with self._lock:
            self._jobs.append(job)
        self._save_jobs()

        dt = datetime.fromtimestamp(ts)  # noqa: DTZ006
        return fmt.block(
            "⏰", f"提醒已设 · {job.job_id}",
            fmt.kv("时间", dt.strftime("%m-%d %H:%M")),
            fmt.kv("内容", message[:60]),
        )

    def add_interval(
        self,
        user_id: str,
        interval: str,
        message: str,
        run_claude: bool = False,
    ) -> str:
        """Add a recurring interval task.

        Args:
            user_id: WeChat user ID.
            interval: Interval string (e.g., '30m', '2h').
            message: Task message or Claude prompt.
            run_claude: If True, run message through Claude and send result.

        Returns:
            Confirmation or error message.
        """
        secs = _parse_interval(interval)
        if secs is None:
            return f"⚠️ 间隔格式看不懂：「{interval}」\n试试 30s · 5m · 2h · 1d"

        job = Job(
            job_id=uuid.uuid4().hex[:8],
            job_type="interval",
            message=message,
            user_id=user_id,
            schedule=interval,
            next_run=time.time() + secs,
            run_claude=run_claude,
        )
        with self._lock:
            self._jobs.append(job)
        self._save_jobs()

        return fmt.block(
            "🔁", f"循环任务 · {job.job_id}",
            fmt.kv("间隔", f"每 {interval}"),
            fmt.kv("内容", message[:60]),
            ("🤖 结果经 Claude 处理" if run_claude else ""),
        )

    def add_cron(
        self,
        user_id: str,
        cron_expr: str,
        message: str,
        run_claude: bool = False,
    ) -> str:
        """Add a cron-scheduled task.

        Args:
            user_id: WeChat user ID.
            cron_expr: Standard 5-field cron expression.
            message: Task message or Claude prompt.
            run_claude: If True, run message through Claude and send result.

        Returns:
            Confirmation or error message.
        """
        fields = cron_expr.strip().split()
        if len(fields) != 5:
            return (
                "⚠️ cron 表达式需要 5 段：分 时 日 月 周\n"
                "例：/cron 0 9 * * 1-5 早报"
            )

        job = Job(
            job_id=uuid.uuid4().hex[:8],
            job_type="cron",
            message=message,
            user_id=user_id,
            schedule=cron_expr,
            next_run=0,  # ensure first match fires immediately
            run_claude=run_claude,
        )
        with self._lock:
            self._jobs.append(job)
        self._save_jobs()

        return fmt.block(
            "📅", f"cron 任务 · {job.job_id}",
            fmt.kv("表达式", cron_expr),
            fmt.kv("内容", message[:60]),
            ("🤖 结果经 Claude 处理" if run_claude else ""),
        )

    def list_jobs(self, user_id: str) -> str:
        """List all jobs for a user.

        Args:
            user_id: WeChat user ID.

        Returns:
            Formatted job list.
        """
        with self._lock:
            user_jobs = [j for j in self._jobs if j.user_id == user_id]

        if not user_jobs:
            return ("⏰ 还没有定时任务。\n"
                    "/remind 17:00 提交代码 · /every 2h 巡检 · /cron …")

        lines = [f"## ⏰ 定时任务 · {len(user_jobs)}", ""]
        for i, j in enumerate(user_jobs):
            status = "🟢" if j.enabled else "⚪"
            if j.job_type == "once":
                dt = datetime.fromtimestamp(j.next_run)  # noqa: DTZ006
                when = dt.strftime("%m-%d %H:%M")
                kind = "单次"
            elif j.job_type == "interval":
                when = f"每 {j.schedule}"
                kind = "循环"
            else:
                when = j.schedule
                kind = "cron"

            claude_tag = " 🤖" if j.run_claude else ""
            lines.append(
                f"{fmt.CIRCLED[i]} {status} {kind} {when}{claude_tag}"
            )
            lines.append(f"   {j.message[:40]}")

        lines.append(fmt.footer("/cancel id 取消"))
        return "\n".join(lines)

    def cancel_job(self, user_id: str, job_id: str) -> str:
        """Cancel a job by ID.

        Args:
            user_id: WeChat user ID (for authorization).
            job_id: Job ID to cancel.

        Returns:
            Confirmation or error message.
        """
        found = None
        with self._lock:
            for i, j in enumerate(self._jobs):
                if j.job_id == job_id and j.user_id == user_id:
                    found = self._jobs.pop(i)
                    break
        if found:
            self._save_jobs()
            return fmt.block(
                "🗑", f"任务已取消 · {found.job_id}",
                fmt.kv("内容", found.message[:60]),
            )
        return f"🔍 找不到任务 {job_id}。/jobs 查看。"

    def start(self) -> None:
        """Start the scheduler background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Scheduler started.")

    def stop(self) -> None:
        """Stop the scheduler."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Scheduler stopped.")

    def _run(self) -> None:
        """Main scheduler loop -- checks every 30 seconds."""
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error("Scheduler tick error: %s", e)
            self._stop_event.wait(30)

    def _tick(self) -> None:
        """Check and fire due jobs."""
        now = time.time()
        now_dt = datetime.now()  # noqa: DTZ005
        to_fire: list[Job] = []

        with self._lock:
            for job in self._jobs:
                if not job.enabled:
                    continue

                if job.job_type == "once" and now >= job.next_run:
                    to_fire.append(job)
                elif job.job_type == "interval" and now >= job.next_run:
                    to_fire.append(job)
                elif (
                    job.job_type == "cron"
                    and _cron_matches(job.schedule, now_dt)
                    and now - job.next_run >= 60
                ):
                    to_fire.append(job)

        for job in to_fire:
            self._fire(job)

    def _fire(self, job: Job) -> None:
        """Execute a job and update its state."""
        logger.info("Firing job %s: %s", job.job_id, job.message[:50])

        if self._callback:
            try:
                self._callback(job.user_id, job.message, job.run_claude)
            except Exception as e:
                logger.error("Job callback error: %s", e)

        with self._lock:
            if job.job_type == "once":
                self._jobs = [j for j in self._jobs if j.job_id != job.job_id]
            elif job.job_type == "interval":
                secs = _parse_interval(job.schedule)
                if secs:
                    job.next_run = time.time() + secs
            elif job.job_type == "cron":
                job.next_run = time.time()  # mark as fired this minute

        self._save_jobs()
