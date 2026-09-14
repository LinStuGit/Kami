#!/usr/bin/env python3
# ruff: noqa: DTZ005, DTZ006, DTZ007, DTZ001
# Naive local datetimes by design — single machine, single timezone, and the
# whole bridge (scheduler, tickets, logs) runs on naive time.
"""Android life-sync plugin — calendar, assistant memories, smart reminders.

Syncs the phone's life data over network ADB and turns every newly detected
calendar event into a *reminder plan* instead of a single ping:

    明早 08:00 高数课  ⇒  今晚 23:30 微信早睡提醒 + 闹钟确认
                          明早 07:15 手机闹钟
                          07:45 微信临开始提醒

Data sources are pluggable (``register_source``): calendar (Android
CalendarProvider via ``content query``), 小布助手记忆 (best-effort DB pull
via root, with /mem add as the always-available manual mirror), and more can
be added later. Reminder planners are pluggable too (``register_planner``):
the default rule planner encodes the sleep/alarm/lead-time heuristics; an
LLM planner can be selected with ``/sync planner llm`` (used for /plan and
/addevent; background auto-planning stays rule-based).

WeChat commands:
    /sync [on|off|now|planner rule|llm|digest on|off [HH:MM]]
    /agenda [days]          upcoming events (from the local cache)
    /addevent <描述>        create a phone calendar event, auto-plan reminders
    /alarm [set HH:MM 标签 | del <id|all>]   phone alarm management
    /mem [add <text> | del <n>]              小布/手动记忆
    /plan <描述>            dry-run: preview the reminder plan for an event

State lives in ~/.config/kami/phone_sync_state.json so plans
survive restarts; actions whose time passed while the bridge was down are
surfaced as 错过 in /sync instead of firing stale.
"""

import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

import fmt
from plugins import Plugin, PluginContext

logger = logging.getLogger(__name__)

from paths import CONFIG_DIR
STATE_FILE = CONFIG_DIR / "phone_sync_state.json"

SYNC_INTERVAL_S = 600       # background re-sync cadence
TICK_S = 30                 # engine loop cadence
MISSED_GRACE_S = 1800       # older than this on wake-up ⇒ skipped, not fired
HORIZON_DAYS = 30           # calendar look-ahead window
DEFAULT_WAKE_LEAD = 45      # minutes before a morning event for the alarm
NIGHT_REMIND = (23, 30)     # "go to bed" ping for next-morning events
ALLDAY_REMIND_HOUR = 20     # day-before ping hour for all-day events
MAX_ACTIONS_PER_EVENT = 6
CONFIRM_WORDS = {"y", "yes", "ok", "好", "好的", "确认", "设置", "要"}
DECLINE_WORDS = {"n", "no", "不", "不用", "不用了", "取消"}

_SILENT = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

WEEK_CN = "一二三四五六日"
CIRCLED = "①②③④⑤⑥⑦⑧"


# ── data model ──────────────────────────────────────────────────


@dataclass
class Item:
    """One synced life-data record (calendar event, assistant memory, …)."""

    uid: str                 # globally unique, stable across syncs
    source: str              # "calendar" / "xiaobu" / "manual" / …
    kind: str                # "event" | "memory" | "todo"
    title: str
    start: datetime | None = None
    end: datetime | None = None
    detail: str = ""
    all_day: bool = False

    def when(self) -> str:
        if not self.start:
            return ""
        wd = f"周{WEEK_CN[self.start.weekday()]}"
        day = self.start.strftime("%m-%d")
        if self.all_day:
            return f"{day} {wd} 全天"
        span = self.start.strftime("%H:%M")
        if self.end:
            span += f"-{self.end.strftime('%H:%M')}"
        return f"{day} {wd} {span}"


@dataclass
class Action:
    """One planned reminder step for an event."""

    at: datetime
    kind: str                # "wechat" | "alarm" | "check_alarm"
    text: str = ""           # wechat body
    label: str = ""          # alarm label
    desc: str = ""           # human line for the plan preview
    target: tuple = field(default_factory=tuple)  # (h, m) for alarm steps


class SourceUnavailable(RuntimeError):
    """Raised by a data source when the phone/backing store is unreachable."""


# ── data sources ────────────────────────────────────────────────


class DataSource:
    """Extension point: subclass, implement fetch(), register an instance.

    Other plugins can extend the sync via::

        mod = sys.modules["weclaude_plugin_phone_sync"]
        mod.register_source(lambda plugin: MySource(plugin.phone))
    """

    name = "base"
    kind = "generic"         # "event" sources get reminder planning

    def __init__(self) -> None:
        self.ok = False
        self.note = "尚未同步"
        self.last_fetch: datetime | None = None

    def fetch(self) -> list[Item]:
        """Return the current item list; raise SourceUnavailable if offline."""
        raise NotImplementedError

    def status(self) -> str:
        when = (
            f"（{int((datetime.now() - self.last_fetch).total_seconds()) // 60}"
            f" 分钟前）" if self.last_fetch else ""
        )
        mark = "✅" if self.ok else "⚠️"
        return f"  {mark} {self.name}: {self.note}{when}"


_SOURCE_FACTORIES: list = []


def register_source(fn) -> None:
    """Register ``fn(plugin) -> DataSource``; called once at plugin start."""
    _SOURCE_FACTORIES.append(fn)


class Phone:
    """Thin adb wrapper bound to one network endpoint."""

    ALARM_PROVIDERS: ClassVar[list[str]] = [
        "content://com.android.deskclock/alarmclock",         # AOSP
        "content://com.google.android.deskclock/alarmclock",  # Google
        "content://com.coloros.alarmclock/alarmclock",        # OPPO ColorOS
        "content://com.oneplus.deskclock/alarmclock",         # OnePlus
        "content://com.oplus.clock/alarmclock",               # oplus newer
        "content://com.sec.android.app.clockprovider/alarm",  # Samsung
    ]

    def __init__(self) -> None:
        self.endpoint = self._resolve_endpoint()
        self._alarm_uri: str | None = None

    @staticmethod
    def _resolve_endpoint() -> str:
        ip = os.environ.get("WECLAUDE_PHONE_IP", "183.173.44.182")
        port = os.environ.get("WECLAUDE_PHONE_PORT", "5555")
        try:
            data = json.loads((CONFIG_DIR / "adb_endpoint.json").read_text())
            return f"{data.get('ip', ip)}:{data.get('port', port)}"
        except (OSError, ValueError, KeyError):
            return f"{ip}:{port}"

    def _adb(self, *args: str, timeout: int = 15) -> str:
        try:
            r = subprocess.run(
                ["adb", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                **_SILENT,
            )
            return (r.stdout or "") + (r.stderr or "")
        except (subprocess.TimeoutExpired, OSError) as e:
            return f"[adb error: {e}]"

    def pull_bytes(self, remote: str, timeout: int = 20) -> bytes:
        """exec-out a remote shell command's output as raw bytes."""
        try:
            r = subprocess.run(
                ["adb", "-s", self.endpoint, "exec-out", remote],
                capture_output=True, timeout=timeout, check=False, **_SILENT,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except (subprocess.TimeoutExpired, OSError):
            pass
        return b""

    def sh(self, *parts: str, timeout: int = 15) -> str:
        """Run one device shell command (args quoted device-side)."""
        return self._adb("-s", self.endpoint, "shell", shlex.join(parts),
                         timeout=timeout)

    def ensure(self) -> tuple[bool, str]:
        out = self._adb("devices")
        if self._has_device(out):
            return True, "已连接"
        self._adb("connect", self.endpoint)
        out = self._adb("devices")
        if self._has_device(out):
            return True, "已连接"
        return False, f"离线（{self.endpoint}）"

    @staticmethod
    def _has_device(out: str) -> bool:
        return any(
            len(p) >= 2 and p[1] == "device"
            for p in (ln.split() for ln in out.splitlines()[1:])
        )

    # ── content provider helpers ──

    def content_query(
        self, uri: str, projection: str | None = None
    ) -> list[dict] | None:
        """Query a provider; parsed rows, or None when unreadable."""
        parts = ["content", "query", "--uri", uri]
        if projection:
            parts += ["--projection", projection]
        out = self.sh(*parts)
        if not out:
            return None
        first = out.splitlines()[0]
        if first.startswith(("Error", "Exception")):
            return None
        rows: list[dict] = []
        for raw in out.splitlines():
            _parse_rows(raw, rows)
        return rows

    def content_insert(self, uri: str, binds: list[tuple[str, str, str]]) -> bool:
        parts = ["content", "insert", "--uri", uri]
        for key, typ, val in binds:
            parts += ["--bind", f"{key}:{typ}:{val}"]
        out = self.sh(*parts, timeout=20)
        return "Error" not in out

    # ── alarms ──

    def _probe_alarm_uri(self) -> str | None:
        if self._alarm_uri:
            if self.content_query(self._alarm_uri, "_id") is not None:
                return self._alarm_uri
            self._alarm_uri = None
        for uri in self.ALARM_PROVIDERS:
            if self.content_query(uri, "_id") is not None:
                self._alarm_uri = uri
                logger.info("alarm provider: %s", uri)
                return uri
        return None

    def list_alarms(self) -> list[dict] | None:
        """All alarms as dicts, or None when the vendor hides them."""
        uri = self._probe_alarm_uri()
        if not uri:
            return None
        rows = self.content_query(uri)
        if rows is None:
            return None
        alarms = []
        for r in rows:
            try:
                alarms.append({
                    "id": r.get("_id", "?"),
                    "h": int(r.get("hour", r.get("hours", -1))),
                    "m": int(r.get("minutes", -1)),
                    "enabled": str(r.get("enabled", "1")) in ("1", "true"),
                    "label": (r.get("label") or "").strip(),
                })
            except (ValueError, TypeError):
                continue
        return alarms

    def set_alarm(self, hour: int, minute: int, label: str) -> bool:
        """Create an alarm via the standard SET_ALARM broadcast."""
        out = self.sh(
            "am", "broadcast", "-a", "android.intent.action.SET_ALARM",
            "--ei", "android.intent.extra.alarm.HOUR", str(hour),
            "--ei", "android.intent.extra.alarm.MINUTES", str(minute),
            "--es", "android.intent.extra.alarm.MESSAGE", label,
            "--ez", "android.intent.extra.alarm.SKIP_UI", "true",
            timeout=20,
        )
        return "Broadcast completed" in out

    def delete_alarm(self, alarm_id: str) -> bool:
        uri = self._alarm_uri or self._probe_alarm_uri()
        if not uri:
            return False
        out = self.sh(
            "content", "delete", "--uri", uri,
            "--where", f"_id={alarm_id}", timeout=15,
        )
        return "Error" not in out

    # ── calendar write ──

    def create_event(
        self, title: str, start: datetime, end: datetime, detail: str = ""
    ) -> tuple[bool, str]:
        """Insert an event; falls back to opening the calendar editor UI."""
        if not self.ensure()[0]:
            return False, "手机离线"
        cals = self.content_query(
            "content://com.android.calendar/calendars", "_id,name"
        )
        if cals:
            ms = lambda dt: str(int(dt.timestamp() * 1000))
            ok = self.content_insert(
                "content://com.android.calendar/events",
                [
                    ("calendar_id", "i", cals[0].get("_id", "1")),
                    ("title", "s", title),
                    ("dtstart", "i", ms(start)),
                    ("dtend", "i", ms(end)),
                    ("eventTimezone", "s", "Asia/Shanghai"),
                    ("description", "s", detail or "via Kami"),
                ],
            )
            if ok:
                return True, "已写入手机日历"
        # provider write refused → let the user confirm in the editor
        self.sh(
            "am", "start", "-a", "android.intent.action.INSERT",
            "-t", "vnd.android.cursor.item/event",
            "--es", "title", title,
            "--el", "beginTime", str(int(start.timestamp() * 1000)),
            "--el", "endTime", str(int(end.timestamp() * 1000)),
            "--es", "description", detail or "via Kami",
            timeout=20,
        )
        return True, "已在手机打开日历编辑页——请在手机上点保存"


def _parse_rows(line: str, rows: list[dict]) -> None:
    """Parse ``Row: 0 _id=5, title=xxx`` output into ``rows`` in place.

    Values containing ``", "`` are merged back into the previous key.
    Lines that are not ``Row:`` headers (e.g. a value's embedded newline
    spilling over) are skipped — truncating a value beats corrupting the
    numeric columns that would otherwise get merged into it.
    """
    m = re.match(r"^Row:\s*\d+\s+(.*)$", line)
    if not m:
        return
    line, cur = m.group(1), {}
    for tok in line.split(", "):
        kv = re.match(r"^(\w+)=(.*)$", tok, re.DOTALL)
        if kv:
            cur[kv.group(1)] = kv.group(2)
        elif cur:
            cur[list(cur)[-1]] += ", " + tok
    if cur:
        rows.append(cur)


class CalendarSource(DataSource):
    """Android CalendarProvider — events in the [now-2h, now+30d] window."""

    name = "日历 calendar"
    kind = "event"

    def __init__(self, phone: Phone) -> None:
        super().__init__()
        self.phone = phone

    def fetch(self) -> list[Item]:
        ok, note = self.phone.ensure()
        if not ok:
            raise SourceUnavailable(note)
        now = datetime.now()
        ms0 = int((now - timedelta(hours=2)).timestamp() * 1000)
        ms1 = int((now + timedelta(days=HORIZON_DAYS)).timestamp() * 1000)
        uri = f"content://com.android.calendar/instances/when/{ms0}/{ms1}"
        rows = self.phone.content_query(
            uri, "event_id,title,begin,end,allDay,eventLocation"
        )
        if rows is None:
            raise SourceUnavailable("日历 provider 读取失败（权限或厂商限制）")
        items = []
        for r in rows:
            try:
                begin_ms = int(r.get("begin") or 0)
                if not begin_ms:
                    continue
                begin = datetime.fromtimestamp(begin_ms // 1000)
                end_ms = int(r.get("end") or 0)
                end = datetime.fromtimestamp(end_ms // 1000) if end_ms else None
            except (ValueError, OSError, OverflowError):
                continue
            title = (r.get("title") or "").strip() or "(未命名日程)"
            items.append(Item(
                uid=f"cal:{r.get('event_id', '?')}:{begin_ms}",
                source="calendar",
                kind="event",
                title=title,
                start=begin,
                end=end,
                detail=(r.get("eventLocation") or "").strip(),
                all_day=str(r.get("allDay")) == "1",
            ))
        items.sort(key=lambda i: (i.start, i.title))
        return items


class XiaobuSource(DataSource):
    """小布助手 (OPPO/OnePlus) memories — best effort, needs root.

    Strategy: locate the assistant package, ``su``-pull its databases
    (incl. -wal/-shm so recent rows are visible), then extract text columns.
    Every failure degrades to a status note; /mem add is the manual mirror.
    """

    name = "小布记忆 xiaobu"
    kind = "memory"
    PACKAGES: ClassVar[list[str]] = [
        "com.heytap.speechassist",
        "com.coloros.speechassist",
        "com.oplus.speechassist",
    ]
    TEXT_COLS: ClassVar[tuple[str, ...]] = ("content", "text", "value", "title", "memory", "memo",
                 "remark", "word", "answer")

    def __init__(self, phone: Phone) -> None:
        super().__init__()
        self.phone = phone

    def _package(self) -> str | None:
        out = self.phone.sh("pm", "list", "packages", timeout=20)
        for pkg in self.PACKAGES:
            if f"package:{pkg}" in out:
                return pkg
        guess = [ln.split(":")[1] for ln in out.splitlines()
                 if "speechassist" in ln or "xiaobu" in ln]
        return guess[0] if guess else None

    def _pull_db(self, pkg: str, db: str) -> Path | None:
        local = CONFIG_DIR / "media" / f"xb_{db}"
        local.parent.mkdir(parents=True, exist_ok=True)
        got_main = False
        for suffix in ("", "-wal", "-shm"):
            data = self.phone.pull_bytes(
                f"su -c cat /data/data/{pkg}/databases/{db}{suffix}"
            )
            if suffix == "":
                got_main = bool(data)
            if data:
                (Path(str(local) + suffix)).write_bytes(data)
        return local if got_main else None

    def fetch(self) -> list[Item]:
        ok, note = self.phone.ensure()
        if not ok:
            raise SourceUnavailable(note)
        pkg = self._package()
        if not pkg:
            raise SourceUnavailable("未检测到小布助手（可用 /mem add 手动录入）")
        listing = self.phone.sh(
            "su", "-c", f"ls /data/data/{pkg}/databases", timeout=10
        )
        db_names = [ln.strip() for ln in listing.splitlines()
                    if ln.strip().endswith(".db")]
        if not db_names:
            raise SourceUnavailable("需要 root 读取小布数据库（可用 /mem add 手动录入）")
        items: list[Item] = []
        tables_seen: list[str] = []
        for db in db_names[:4]:
            local = self._pull_db(pkg, db)
            if local is None:
                continue
            try:
                con = sqlite3.connect(str(local))
                tables = [
                    r[0] for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ]
                for t in tables:
                    cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
                    text_cols = [c for c in cols
                                 if c.lower() in self.TEXT_COLS]
                    if not text_cols:
                        continue
                    tables_seen.append(t)
                    for row in con.execute(
                        f'SELECT "{text_cols[0]}" FROM "{t}"'
                    ):
                        txt = (row[0] or "").strip() if row else ""
                        if 2 <= len(txt) <= 500:
                            items.append(Item(
                                uid=f"xiaobu:{db}:{t}:"
                                    f"{hash(txt) & 0xffffffff:08x}",
                                source="xiaobu", kind="memory", title=txt,
                            ))
                con.close()
            except sqlite3.Error:
                continue
        note_t = ", ".join(dict.fromkeys(tables_seen))[:60]
        self.note = f"{len(items)} 条" + (f"（表: {note_t}）" if note_t else "")
        return items


class ManualSource(DataSource):
    """User-entered memories (/mem add) — the always-available mirror."""

    name = "手动记忆 manual"
    kind = "memory"

    def __init__(self, plugin: "PhoneSyncPlugin") -> None:
        super().__init__()
        self.plugin = plugin

    def fetch(self) -> list[Item]:
        self.ok = True
        entries = self.plugin.state.get("manual_memories", [])
        self.note = f"{len(entries)} 条"
        return [
            Item(uid=f"manual:{e['id']}", source="manual", kind="memory",
                 title=e["text"])
            for e in entries
        ]


# ── reminder planners ───────────────────────────────────────────


class RulePlanner:
    """Heuristics: morning events get a night-before sleep ping + alarm
    check, everything gets lead-time pings; all-day events get a
    day-before ping."""

    name = "rule"

    def plan(self, item: Item, now: datetime, ctx=None) -> list[Action]:
        if not item.start:
            return []
        start, lead = item.start, item.start - now
        acts: list[Action] = []

        if item.all_day:
            prev = (start - timedelta(days=1)).replace(
                hour=ALLDAY_REMIND_HOUR, minute=0)
            if prev > now:
                acts.append(Action(
                    at=prev, kind="wechat",
                    text=f"📅 明天全天有：「{item.title}」"
                         + (f"（{item.detail}）" if item.detail else ""),
                    desc=f"前一天 {prev:%H:%M} 微信预告",
                ))
            morning = start.replace(hour=8, minute=30)
            if morning > now:
                acts.append(Action(
                    at=morning, kind="wechat",
                    text=f"📅 今天全天：「{item.title}」",
                    desc="当天 08:30 微信提醒",
                ))
            return self._clean(acts, now)

        # lead-time pings
        if lead >= timedelta(hours=1):
            acts.append(Action(
                at=start - timedelta(hours=1), kind="wechat",
                text=f"⏰ 1 小时后（{start:%H:%M}）：{item.title}",
                desc="开始前 1 小时微信提醒",
            ))
        if lead >= timedelta(minutes=20):
            acts.append(Action(
                at=start - timedelta(minutes=15), kind="wechat",
                text=f"⏰ 15 分钟后（{start:%H:%M}）：{item.title}",
                desc="开始前 15 分钟微信提醒",
            ))
        elif lead > timedelta(minutes=2):
            acts.append(Action(
                at=now + timedelta(seconds=15), kind="wechat",
                text=f"⏰ 马上开始（{start:%H:%M}）：{item.title}",
                desc="已临近，立即提醒",
            ))

        # morning events ⇒ night-before routine (sleep ping + alarm confirm)
        if 5 <= start.hour < 11:
            target = start - timedelta(minutes=DEFAULT_WAKE_LEAD)
            night = (start - timedelta(days=1)).replace(
                hour=NIGHT_REMIND[0], minute=NIGHT_REMIND[1])
            if night > now:
                acts.append(Action(
                    at=night, kind="check_alarm",
                    target=(target.hour, target.minute),
                    label=f"起床：{item.title}",
                    text=f"🌙 明早 {start:%H:%M} 有「{item.title}」，早点休息～",
                    desc=f"早睡提醒 + 确认明早 {target:%H:%M} 闹钟",
                ))
            elif now < target:
                acts.append(Action(
                    at=target, kind="alarm", label=f"起床：{item.title}",
                    desc=f"手机闹钟 {target:%H:%M}",
                ))
        return self._clean(acts, now)

    @staticmethod
    def _clean(acts: list[Action], now: datetime) -> list[Action]:
        acts = [a for a in acts if a.at > now - timedelta(minutes=5)]
        acts.sort(key=lambda a: a.at)
        seen, out = set(), []
        for a in acts:
            key = (a.kind, a.at.strftime("%m%d%H%M"))
            if key not in seen:
                seen.add(key)
                out.append(a)
        return out[:MAX_ACTIONS_PER_EVENT]


class LLMPlanner:
    """Ask the AI agent for a reminder plan (JSON); falls back to rules."""

    name = "llm"

    def plan(self, item: Item, now: datetime, ctx=None) -> list[Action]:
        if ctx is None:
            return RulePlanner().plan(item, now)
        prompt = (
            f"现在是 {now.strftime('%Y-%m-%d %H:%M %A')}。"
            "为新日程制定提醒计划，只输出 JSON 数组，每项："
            '{"at": "YYYY-MM-DD HH:MM", "kind": "wechat|alarm|check_alarm",'
            ' "text": "提醒内容"}。\n'
            "kind 含义：wechat=到点发微信；alarm=立即在手机设置该时刻的闹钟"
            "（at=闹钟时刻）；check_alarm=到点检查手机闹钟并询问用户是否补设"
            "（适合早晨日程的前一晚）。\n"
            f"日程：「{item.title}」 {item.when()}"
            + (f" 地点:{item.detail}" if item.detail else "")
            + "。最多 4 条。"
        )
        try:
            resp = ctx.ask_agent(prompt)
            m = re.search(r"\[.*\]", resp, re.DOTALL)
            acts = []
            for d in json.loads(m.group(0)):
                at = datetime.strptime(d["at"], "%Y-%m-%d %H:%M")
                if at <= now or d.get("kind") not in (
                        "wechat", "alarm", "check_alarm"):
                    continue
                text = d.get("text", "")
                acts.append(Action(
                    at=at, kind=d["kind"], text=text, label=text,
                    desc=f"{at:%m-%d %H:%M} {d['kind']}: {text[:30]}",
                ))
            if acts:
                return RulePlanner._clean(acts, now)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            logger.warning("llm planner failed, falling back to rules: %s", e)
        return RulePlanner().plan(item, now)


_PLANNERS: dict[str, object] = {}


def register_planner(planner) -> None:
    _PLANNERS[planner.name] = planner


register_planner(RulePlanner())
register_planner(LLMPlanner())


# ── time parsing ────────────────────────────────────────────────

EXPLICIT_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
REL_DAY_RE = re.compile(r"(大后天|后天|明天|今天)")
WEEKDAY_RE = re.compile(r"(下?)周([一二三四五六日天])")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})|(\d{1,2})点(半|\d{1,2})?")
END_TIME_RE = re.compile(r"(?:到|-|—|~)\s*(\d{1,2}):(\d{2})")
PERIOD_RE = re.compile(r"(凌晨|早上|上午|中午|下午|傍晚|晚上)")


def _cut(text: str, m: re.Match) -> str:
    return (text[:m.start()] + " " + text[m.end():])


def parse_when(
    text: str, now: datetime
) -> tuple[str, datetime, datetime | None] | None:
    """Local fast-path parser for “标题 明天 8:00-9:40” style input.

    Returns (title, start, end|None), or None when unsure (caller falls
    back to the LLM parser). Handles 明天/后天/周X/下周X, HH:MM / H点[半|分],
    上午/下午 qualifiers, and an optional end time.
    """
    text = text.replace("：", ":").strip()
    date_part: datetime | None = None

    m = EXPLICIT_DATE_RE.search(text)
    if m:
        try:
            date_part = datetime(int(m.group(1)), int(m.group(2)),
                                 int(m.group(3)))
        except ValueError:
            return None
        text = _cut(text, m)

    m = REL_DAY_RE.search(text)
    if m and not date_part:
        offset = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}[m.group(1)]
        date_part = (now + timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        text = _cut(text, m)
    if not date_part:
        m = WEEKDAY_RE.search(text)
        if m:
            target = "一二三四五六日".index(m.group(2).replace("天", "日")) + 1
            delta = (target - now.isoweekday()) % 7 or 7
            if m.group(1):
                delta += 7
            date_part = (now + timedelta(days=delta)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            text = _cut(text, m)

    m = TIME_RE.search(text)
    if not m:
        return None
    if m.group(1):
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        h = int(m.group(3))
        mi = 30 if m.group(4) == "半" else int(m.group(4) or 0)
    period = PERIOD_RE.search(text[:m.start()])
    if period and h < 12 and (
            period.group(1) in ("下午", "傍晚", "晚上")
            or (period.group(1) == "中午" and h < 11)):
        h += 12
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    text = _cut(text, m)

    end = None
    m2 = END_TIME_RE.search(text)
    if m2:
        eh, em = int(m2.group(1)), int(m2.group(2))
        if 0 <= eh <= 23 and 0 <= em <= 59:
            end = (date_part or now).replace(hour=eh, minute=em,
                                             second=0, microsecond=0)
            text = _cut(text, m2)

    title = re.sub(r"\s+", " ", text).strip(" ,，。;；-—")
    if not title:
        title = "日程"
    base = date_part or now
    start = base.replace(hour=h, minute=mi, second=0, microsecond=0)
    if not date_part and start <= now:
        start += timedelta(days=1)
    if end and end <= start:
        end += timedelta(days=1)
    return title, start, end


def parse_when_llm(
    text: str, now: datetime, ctx: PluginContext
) -> tuple[str, datetime, datetime] | None:
    """LLM fallback for natural descriptions like '周五下午两节课后开会'."""
    prompt = (
        f"今天 {now.strftime('%Y-%m-%d %A %H:%M')}。"
        "把日程描述解析为一个 JSON 对象并只输出 JSON："
        '{"title": "...", "date": "YYYY-MM-DD", "start": "HH:MM", '
        '"end": "HH:MM 或 null"}。\n'
        f"描述：{text}"
    )
    try:
        resp = ctx.ask_agent(prompt)
        d = json.loads(re.search(r"\{.*\}", resp, re.DOTALL).group(0))
        start = datetime.strptime(f"{d['date']} {d['start']}", "%Y-%m-%d %H:%M")
        end = (
            datetime.strptime(f"{d['date']} {d['end']}", "%Y-%m-%d %H:%M")
            if d.get("end") else start + timedelta(hours=1)
        )
        if end <= start:
            end += timedelta(days=1)
        return d.get("title") or text[:20], start, end
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


# ── the plugin ──────────────────────────────────────────────────


class PhoneSyncPlugin(Plugin):
    name = "phonesync"
    description = "安卓日历/小布记忆同步 + 智能提醒规划 + 手机闹钟"
    commands: ClassVar[dict[str, str]] = {
        "/sync": "/sync [on|off|now|planner rule|llm|digest on|off [HH:MM]]"
                 " - 同步引擎设置（/sync 帮助 看详情，无参数看状态）",
        "/agenda": "/agenda [天数] - 未来日程（默认 7 天）",
        "/addevent": "/addevent <描述> - 新建日历日程并自动规划提醒",
        "/alarm": "/alarm [set HH:MM 标签 | del <id|all>] - 手机闹钟",
        "/mem": "/mem [add <text>|del <n>] - 小布/手动记忆（del 仅手动条目）",
        "/plan": "/plan <描述> - 预览提醒计划（不生效）",
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._syncing = False
        self._send = None            # captured send_text handle
        self._owner: str | None = None
        self.state: dict = {}
        self.phone = Phone()
        self.sources: list[DataSource] = []

    # ── lifecycle ──

    def on_start(self) -> None:
        self.state = self._load_state()
        for fn in _SOURCE_FACTORIES:
            try:
                self.sources.append(fn(self))
            except Exception as e:  # noqa: BLE001 — plugin isolation
                logger.error("source factory %s failed: %s", fn, e)
        if not any(isinstance(s, CalendarSource) for s in self.sources):
            self.sources.insert(0, CalendarSource(self.phone))
        if not any(isinstance(s, XiaobuSource) for s in self.sources):
            self.sources.append(XiaobuSource(self.phone))
        if not any(isinstance(s, ManualSource) for s in self.sources):
            self.sources.append(ManualSource(self))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="phone-sync")
        self._thread.start()
        logger.info("phonesync started (%d sources, %d known items)",
                    len(self.sources), len(self.state.get("items", {})))

    def on_stop(self) -> None:
        if self._stop:
            self._stop.set()
        self._save()

    # ── state ──

    @staticmethod
    def _load_state() -> dict:
        base = {
            "items": {},            # uid -> {title, when, start_ts, source…}
            "actions": [],          # planned reminders
            "announced_keys": [],   # announcement de-dup keys
            "manual_memories": [],  # [{"id","text","added"}]
            "xiaobu_memories": [],  # [{"uid","text"}]
            "pending_confirm": None,
            "settings": {"auto_sync": True, "planner": "rule",
                         "digest": False, "digest_time": "07:30"},
            "baselined": False,
            "last_sync": 0.0,
            "last_digest": "",
            "missed_log": [],
        }
        try:
            base.update(
                json.loads(STATE_FILE.read_text(encoding="utf-8"))
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return base

    def _save(self) -> None:
        try:
            with self._lock:
                snap = dict(self.state)
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps(snap, ensure_ascii=False, indent=1, default=str),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error("phonesync save failed: %s", e)

    def _capture(self, ctx: PluginContext) -> None:
        self._send = ctx.send_text
        self._owner = ctx.user_id

    def _push(self, text: str) -> None:
        """Proactive message to the owner (empty token, same as scheduler)."""
        if self._send and self._owner:
            try:
                self._send(self._owner, "", text)
            except Exception as e:  # noqa: BLE001
                logger.error("phonesync push failed: %s", e)

    # ── engine loop ──

    def _loop(self) -> None:
        while self._stop and not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                logger.error("phonesync tick error: %s", e)
            self._stop.wait(TICK_S)

    def _tick(self) -> None:
        now = datetime.now()
        self._fire_due(now)
        st = self.state.get("settings", {})
        if st.get("auto_sync", True) and \
                time.time() - self.state.get("last_sync", 0.0) >= SYNC_INTERVAL_S:
            self.sync_now(announce=True)
        self._maybe_digest(now)

    def _fire_due(self, now: datetime) -> None:
        due: list[dict] = []
        with self._lock:
            for rec in self.state.get("actions", []):
                if rec.get("state") != "pending":
                    continue
                at = _ts_to_dt(rec["at"])
                if at > now:
                    continue
                if (now - at).total_seconds() > MISSED_GRACE_S:
                    rec["state"] = "missed"
                    log = self.state.setdefault("missed_log", [])
                    log.append(f"{rec.get('desc') or rec.get('text', '')}"
                               f"（{at:%m-%d %H:%M}，错过）")
                    self.state["missed_log"] = log[-5:]
                else:
                    rec["state"] = "firing"
                    due.append(rec)
        for rec in due:
            self._execute(rec)
            with self._lock:
                rec["state"] = "done"
        with self._lock:
            self.state["actions"] = [
                r for r in self.state.get("actions", [])
                if r.get("state") in ("pending", "missed")
                or (r.get("state") == "done"
                    and _ts_to_dt(r["at"]) > now - timedelta(days=3))
            ]
            self._save()

    def _execute(self, rec: dict) -> None:
        kind = rec.get("kind")
        try:
            if kind == "wechat":
                self._push(rec.get("text") or rec.get("desc") or "提醒")
            elif kind == "alarm":
                at = _ts_to_dt(rec["at"])
                h, m = rec.get("target") or (at.hour, at.minute)
                ok = self.phone.ensure()[0] and self.phone.set_alarm(
                    h, m, rec.get("label") or "Kami")
                self._push(
                    f"⏰ 已在手机设置 {h:02d}:{m:02d} 闹钟「{rec.get('label')}」"
                    if ok else
                    f"⚠️ 手机闹钟设置失败（{rec.get('label')}）——请手动设置"
                )
            elif kind == "check_alarm":
                self._run_alarm_check(rec)
        except Exception as e:  # noqa: BLE001
            logger.error("phonesync execute %s failed: %s", kind, e)

    def _run_alarm_check(self, rec: dict) -> None:
        """Night ping: remind to sleep, verify tomorrow's alarm exists."""
        uid = rec.get("uid", "")
        item = self.state.get("items", {}).get(uid, {})
        title = item.get("title", "日程")
        when = item.get("when", "")
        t_h, t_m = rec.get("target") or self._wake_target(uid)
        label = rec.get("label") or f"起床：{title}"

        alarms = None
        if self.phone.ensure()[0]:
            alarms = self.phone.list_alarms()
        if alarms is None:
            self._set_pending(t_h, t_m, label, uid)
            self._push(fmt.block(
                "🌙", "睡前提醒",
                f"明早 {when} 有「{title}」",
                "⚠️ 读不到手机闹钟",
                f"回复 Y 由我设置 {t_h:02d}:{t_m:02d}，或睡前自行确认",
            ))
            return
        hits = self._alarms_covering(alarms, item.get("start_ts"))
        if hits:
            listing = "、".join(
                f"{a['h']:02d}:{a['m']:02d}"
                + (f"「{a['label']}」" if a["label"] else "")
                for a in hits[:3]
            )
            self._push(fmt.block(
                "🌙", "睡前提醒",
                f"明早 {when} 有「{title}」",
                f"✅ 闹钟已确认：{listing}",
                "早点休息，晚安 🌙",
            ))
        else:
            self._set_pending(t_h, t_m, label, uid)
            self._push(fmt.block(
                "🌙", "睡前提醒",
                f"明早 {when} 有「{title}」",
                "⏰ 手机上还没有明早的闹钟",
                f"回复 Y 设置 {t_h:02d}:{t_m:02d}「{label}」，或自行设置",
            ))

    @staticmethod
    def _alarms_covering(alarms: list[dict], start_ts) -> list[dict]:
        """Enabled alarms whose hh:mm falls in [start-90m, start-10m]."""
        if not start_ts:
            return []
        ev = _ts_to_dt(start_ts)
        lo = (ev - timedelta(minutes=90)).time()
        hi = (ev - timedelta(minutes=10)).time()
        hits = []
        for a in alarms:
            if not a.get("enabled") or a["h"] < 0:
                continue
            t = datetime.now().replace(hour=a["h"], minute=a["m"]).time()
            if lo <= t <= hi:
                hits.append(a)
        return hits

    def _wake_target(self, uid: str) -> tuple:
        st = self.state.get("items", {}).get(uid, {}).get("start_ts")
        if st:
            t = _ts_to_dt(st) - timedelta(minutes=DEFAULT_WAKE_LEAD)
            return (t.hour, t.minute)
        return (7, 15)

    def _set_pending(self, h: int, m: int, label: str, uid: str) -> None:
        with self._lock:
            self.state["pending_confirm"] = {
                "h": h, "m": m, "label": label, "uid": uid,
                "expire": time.time() + 2700,
            }
            self._save()

    # ── sync ──

    def sync_now(self, announce: bool = True) -> str:
        """Pull every source, diff, plan. Returns a short report."""
        if self._syncing:
            return "同步进行中…"
        self._syncing = True
        try:
            return self._sync_inner(announce)
        finally:
            self._syncing = False

    def _sync_inner(self, announce: bool) -> str:
        now = datetime.now()
        first_run = not self.state.get("baselined", False)
        lines: list[str] = []
        event_ok = False
        for src in self.sources:
            try:
                items = src.fetch()
                src.ok = True
                src.last_fetch = now
                if src.kind == "event":
                    event_ok = True
                    new_n = self._ingest_events(src, items, now, first_run)
                    src.note = f"{len(items)} 项在窗口内"
                    if new_n and not first_run:
                        lines.append(f"{src.name}: +{new_n} 新日程")
                else:
                    new_n = self._ingest_memories(src, items, announce)
                    if new_n and not first_run:
                        lines.append(f"{src.name}: +{new_n} 新记忆")
            except SourceUnavailable as e:
                src.ok = False
                src.note = str(e)
            except Exception as e:  # noqa: BLE001
                src.ok = False
                src.note = f"错误: {e}"
                logger.error("source %s failed: %s", src.name, e)
        with self._lock:
            self.state["last_sync"] = time.time()
            # baseline only counts once a calendar read actually succeeded,
            # otherwise the first online sync would announce old events
            if event_ok or self.state.get("baselined"):
                self.state["baselined"] = True
            self._prune(now)
            self._save()
        if first_run and announce and event_ok:
            n = len(self.state.get("items", {}))
            sections = [fmt.kv("监控", f"{n} 项日程，已按规则设置提醒")]
            nxt = self._next_event_line()
            if nxt:
                sections.append(fmt.kv("下一项", nxt))
            sections.append(fmt.footer("/agenda 查看 · /sync off 关闭"))
            self._push(fmt.block("📱", "手机日程同步已开启", *sections))
        return "\n".join(lines) or "（无变化）"

    def _ingest_events(
        self, src: DataSource, items: list[Item], now: datetime,
        first_run: bool,
    ) -> int:
        with self._lock:
            known: dict = self.state.setdefault("items", {})
            announced: set = set(self.state.setdefault("announced_keys", []))
        new_count = 0
        seen_uids: set[str] = set()
        for it in items:
            seen_uids.add(it.uid)
            future = bool(it.start and it.start > now + timedelta(minutes=5))
            if it.uid in known:
                known[it.uid]["title"] = it.title
                known[it.uid]["when"] = it.when()
                continue
            key = (f"{it.source}:{it.title}:{it.start:%m%d%H%M}"
                   if it.start else f"{it.source}:{it.title}")
            is_new_key = key not in announced
            known[it.uid] = {
                "title": it.title, "when": it.when(),
                "start_ts": it.start.timestamp() if it.start else 0,
                "source": it.source, "all_day": it.all_day,
                "detail": it.detail,
            }
            if not future:
                continue
            if first_run or not is_new_key:
                # existing schedule, or later occurrences of a known
                # series → plan silently
                announced.add(key)
                self._plan_for(it, now)
                new_count += 1
                continue
            # brand-new event → announce with the plan
            announced.add(key)
            new_count += 1
            self._plan_for(it, now)
            self._announce(it)
        self._detect_cancellations(seen_uids, now)
        with self._lock:
            self.state["announced_keys"] = list(announced)[-500:]
        return new_count

    def _detect_cancellations(self, seen_uids: set, now: datetime) -> None:
        cancelled: list[dict] = []
        with self._lock:
            known = self.state.get("items", {})
            for uid in [
                u for u, meta in known.items()
                if u.startswith("cal:")
                and meta.get("start_ts", 0) > now.timestamp() + 3600
                and u not in seen_uids
            ]:
                cancelled.append(known.pop(uid))
                self.state["actions"] = [
                    r for r in self.state.get("actions", [])
                    if r.get("uid") != uid or r.get("state") != "pending"
                ]
        for meta in cancelled:
            self._push(fmt.block(
                "❌", "日程已取消",
                f"{meta['title']} · {meta.get('when', '')}",
            ))

    def _ingest_memories(
        self, src: DataSource, items: list[Item], announce: bool
    ) -> int:
        if isinstance(src, ManualSource):
            return 0
        with self._lock:
            store = {
                m["uid"]: m["text"]
                for m in self.state.setdefault("xiaobu_memories", [])
            }
        new = 0
        for it in items:
            if it.uid in store:
                continue
            store[it.uid] = it.title
            new += 1
            if announce:
                self._push(f"🧠 小布新增记忆：{it.title[:100]}")
        with self._lock:
            self.state["xiaobu_memories"] = [
                {"uid": u, "text": t} for u, t in store.items()
            ][-200:]
            self._save()
        return new

    # ── planning ──

    def _planner(self):
        name = self.state.get("settings", {}).get("planner", "rule")
        return _PLANNERS.get(name, _PLANNERS["rule"])

    def _plan_for(self, item: Item, now: datetime,
                  ctx: PluginContext | None = None) -> list[dict]:
        actions = self._planner().plan(item, now, ctx)
        created: list[dict] = []
        with self._lock:
            have = {
                (r.get("kind"), r.get("at"), r.get("text", ""))
                for r in self.state.get("actions", [])
            }
            for a in actions:
                rec = {
                    "id": uuid.uuid4().hex[:4],
                    "uid": item.uid,
                    "kind": a.kind,
                    "at": a.at.timestamp(),
                    "text": a.text,
                    "label": a.label,
                    "target": list(a.target) if a.target else [],
                    "desc": a.desc or f"{a.at:%m-%d %H:%M} {a.kind}",
                    "state": "pending",
                }
                # global dedupe: the same step planned via a different uid
                # (e.g. manual-ev row + the real calendar row) must not
                # double-fire
                key = (a.kind, rec["at"], a.text)
                if a.kind in ("alarm", "check_alarm"):
                    key = (a.kind, rec["at"], "")
                if key in have:
                    continue
                have.add(key)
                self.state.setdefault("actions", []).append(rec)
                created.append(rec)
            self._save()
        return created

    def _announce(self, item: Item) -> None:
        now = datetime.now()
        lines = [f"{item.title}", f"🕘 {item.when()}"]
        if item.detail:
            lines[0] += f"（{item.detail}）"
        recs = sorted(self._pending_recs_for(item.uid), key=lambda r: r["at"])
        if recs:
            lines.append(fmt.section("提醒计划"))
            for n, r in enumerate(recs[:MAX_ACTIONS_PER_EVENT]):
                at = _ts_to_dt(r["at"])
                tag = {"wechat": "💬", "alarm": "⏰",
                       "check_alarm": "🌙"}.get(r["kind"], "•")
                day = ("今晚" if at.date() == now.date()
                       else "明早" if at.date()
                       == (now + timedelta(days=1)).date()
                       else at.strftime("%m-%d"))
                circ = fmt.CIRCLED[n] if n < len(fmt.CIRCLED) else f"{n + 1}."
                lines.append(
                    f"{circ} {day} {at:%H:%M} {tag} {r.get('desc') or ''}"
                )
        else:
            lines.append("（时间太近，未安排额外提醒）")
        self._push(fmt.block("📅", "检测到新日程", *lines))

    def _pending_recs_for(self, uid: str) -> list[dict]:
        with self._lock:
            return [
                r for r in self.state.get("actions", [])
                if r.get("uid") == uid and r.get("state") == "pending"
            ]

    def _prune(self, now: datetime) -> None:
        """Keep state small: drop old items and finished actions."""
        horizon = now - timedelta(days=7)
        items = self.state.get("items", {})
        self.state["items"] = {
            uid: m for uid, m in items.items()
            if not m.get("start_ts") or m["start_ts"] > horizon.timestamp()
        }
        self.state["actions"] = [
            r for r in self.state.get("actions", [])
            if r.get("state") == "pending"
            or _ts_to_dt(r["at"]) > now - timedelta(days=3)
        ]

    # ── digest ──

    def _maybe_digest(self, now: datetime) -> None:
        st = self.state.get("settings", {})
        if not st.get("digest", False):
            return
        hh, mm = map(int, st.get("digest_time", "07:30").split(":"))
        if (now.hour, now.minute) < (hh, mm):
            return
        today = now.strftime("%Y-%m-%d")
        if self.state.get("last_digest") == today:
            return
        with self._lock:
            self.state["last_digest"] = today
            self._save()
        evs = self._events_on(now)
        if evs:
            lines = [f"今天 {len(evs)} 项日程："]
            lines += [f"{fmt.CIRCLED[n]} {e.when().split(' ', 1)[-1]} {e.title}"
                      for n, e in enumerate(evs[:8])]
        else:
            lines = ["今天没有日程安排，自由的一天 🎉"]
        self._push(fmt.block("🌅", "今日早报", *lines))

    def _events_on(self, day: datetime) -> list[Item]:
        out = []
        for uid, m in self.state.get("items", {}).items():
            if not m.get("start_ts"):
                continue
            st = _ts_to_dt(m["start_ts"])
            if st.date() == day.date():
                out.append(Item(
                    uid=uid, source=m.get("source", "calendar"),
                    kind="event", title=m["title"], start=st,
                    all_day=m.get("all_day", False),
                    detail=m.get("detail", ""),
                ))
        out.sort(key=lambda i: i.start)
        return out

    def _iter_cached_items(self) -> list[Item]:
        out = []
        for uid, m in self.state.get("items", {}).items():
            st = _ts_to_dt(m["start_ts"]) if m.get("start_ts") else None
            out.append(Item(
                uid=uid, source=m.get("source", "calendar"),
                kind="event", title=m["title"], start=st,
                all_day=m.get("all_day", False), detail=m.get("detail", ""),
            ))
        return out

    def _next_event_line(self) -> str:
        evs = sorted(
            (i for i in self._iter_cached_items()
             if i.start and i.start > datetime.now()),
            key=lambda i: i.start,
        )
        return f"{evs[0].when()} {evs[0].title}" if evs else ""

    # ── commands ──

    def handle_command(self, cmd: str, args: str, ctx: PluginContext):
        self._capture(ctx)
        if cmd == "/sync":
            return self._cmd_sync(args)
        if cmd == "/agenda":
            return self._cmd_agenda(args)
        if cmd == "/addevent":
            return self._cmd_addevent(args, ctx)
        if cmd == "/alarm":
            return self._cmd_alarm(args)
        if cmd == "/mem":
            return self._cmd_mem(args)
        if cmd == "/plan":
            return self._cmd_plan(args, ctx)
        return None

    def _sync_help(self, st: dict) -> str:
        """Full subcommand reference for /sync 帮助 (and bad usage)."""
        digest = st.get("digest_time") if st.get("digest") else "off"
        return fmt.block(
            "⚙️", "/sync 同步引擎",
            fmt.kv("on", "开启自动同步（每 10 分钟）", 8),
            fmt.kv("off", "关闭自动同步", 8),
            fmt.kv("now", "立即同步一次", 8),
            fmt.kv("planner", f"提醒规划器 rule|llm（当前 {st.get('planner')}）", 8),
            fmt.kv("digest", f"每日早报 on|off [HH:MM]（当前 {digest}）", 8),
            fmt.kv("无参数", "显示引擎状态", 8),
            fmt.footer("/agenda 日程 · /plan 预览规划 · /addevent 建日程"),
        )

    def _cmd_sync(self, args: str) -> str:
        args = args.strip()
        with self._lock:
            st = self.state.setdefault("settings", {})
        if args in ("help", "帮助", "?"):
            return self._sync_help(st)
        if args == "on":
            st["auto_sync"] = True
            self._save()
            return "🔄 自动同步已开启（每 10 分钟）。"
        if args == "off":
            st["auto_sync"] = False
            self._save()
            return "⏸ 自动同步已关闭。/sync on 重新开启。"
        if args == "now":
            summary = self.sync_now(announce=True)
            return f"## 🔄 同步完成\n\n{summary}"
        if args.startswith("planner"):
            which = args.split()[-1] if args.split()[-1:] \
                and args.split()[-1] in _PLANNERS else ""
            if which:
                st["planner"] = which
                self._save()
                return f"🧭 提醒规划器 → {which}"
            return f"用法: /sync planner rule|llm（当前 {st.get('planner')}）"
        if args.startswith("digest"):
            parts = args.split()
            if len(parts) >= 2 and parts[1] in ("on", "off"):
                st["digest"] = parts[1] == "on"
                if len(parts) >= 3 and re.match(r"^\d{1,2}:\d{2}$", parts[2]):
                    st["digest_time"] = parts[2]
                self._save()
                return (f"🌅 每日早报 → {'开' if st['digest'] else '关'}"
                        f"（{st.get('digest_time')}）")
            return "用法: /sync digest on|off [HH:MM]"
        if args:
            return self._sync_help(st)

        # status
        try:
            online, note = self.phone.ensure()
        except Exception as e:  # noqa: BLE001
            online, note = False, f"adb 错误（{e}）"
        lines = [fmt.kv("手机", ("✅ " if online else "⚠️ ") + note),
                 fmt.section("数据源")]
        lines += ["  " + s.status() for s in self.sources]
        pending = sorted(
            (r for r in self.state.get("actions", [])
             if r.get("state") == "pending"),
            key=lambda r: r["at"],
        )
        lines.append(fmt.kv(
            "设置", f"同步 {'on' if st.get('auto_sync') else 'off'} · "
            f"规划 {st.get('planner')} · "
            f"早报 {st.get('digest_time') if st.get('digest') else 'off'}"
        ))
        if pending:
            lines.append(fmt.section("近期提醒"))
            for r in pending[:6]:
                at = _ts_to_dt(r["at"])
                tag = {"wechat": "💬", "alarm": "⏰",
                       "check_alarm": "🌙"}.get(r["kind"], "•")
                lines.append(f"  {tag} {at:%m-%d %H:%M} {r.get('desc', '')}")
        else:
            lines.append(fmt.kv("近期提醒", "无"))
        missed = self.state.get("missed_log", [])
        if missed:
            lines.append(fmt.section("错过（桥接停机期间）"))
            lines += [f"  ⚠️ {m}" for m in missed[-3:]]
        return fmt.block("📱", "手机生活助手", *lines)

    def _cmd_agenda(self, args: str) -> str:
        try:
            days = min(int(args or 7), HORIZON_DAYS)
        except ValueError:
            return "用法: /agenda [天数]"
        now = datetime.now()
        evs = sorted(
            (i for i in self._iter_cached_items()
             if i.start and now - timedelta(hours=1)
             <= i.start <= now + timedelta(days=days)),
            key=lambda i: i.start,
        )
        if not evs:
            age = ""
            if self.state.get("last_sync"):
                mins = int((time.time() - self.state["last_sync"]) // 60)
                age = f"（缓存 {mins} 分钟前）"
            return (f"📅 未来 {days} 天没有日程。{age}\n"
                    "/sync now 立即同步")
        lines = [f"共 {len(evs)} 项"]
        cur_date = None
        for i in evs:
            if i.start.date() != cur_date:
                cur_date = i.start.date()
                lines.append(
                    f"{i.start.strftime('%m-%d')} "
                    f"周{WEEK_CN[i.start.weekday()]}"
                )
            span = "全天" if i.all_day else i.start.strftime("%H:%M") + (
                f"-{i.end.strftime('%H:%M')}" if i.end else "")
            lines.append(f"   {span}  {i.title}"
                         + (f" @{i.detail}" if i.detail else ""))
        lines.append(fmt.footer("/sync now 立即同步"))
        return fmt.block("📅", f"未来 {days} 天", *lines)

    def _cmd_addevent(self, args: str, ctx: PluginContext) -> str:
        if not args:
            return ("用法: /addevent <描述>\n"
                    "例: /addevent 高数课 明天 8:00-9:40\n"
                    "    /addevent 组会 周五 14:00\n"
                    "（自然语言也行，交给 AI 解析，稍慢）")
        now = datetime.now()
        parsed = parse_when(args, now) or parse_when_llm(args, now, ctx)
        if parsed is None:
            return "没解析出时间。试试明确格式：/addevent 标题 明天 8:00-9:40"
        title, start, end = parsed
        ok, note = self.phone.create_event(title, start, end)
        if not ok:
            return f"❌ 建日程失败：{note}"
        when = Item(uid="", source="calendar", kind="event", title=title,
                    start=start, end=end).when()
        if "编辑页" in note:
            # provider write refused → plan under a local uid so reminders
            # exist even if the user skips saving on the phone
            item = Item(uid=f"manual-ev:{uuid.uuid4().hex[:6]}",
                        source="calendar", kind="event", title=title,
                        start=start, end=end)
            with self._lock:
                self.state.setdefault("items", {})[item.uid] = {
                    "title": title, "when": item.when(),
                    "start_ts": start.timestamp(), "source": "calendar",
                    "all_day": False, "detail": "",
                }
            recs = self._plan_for(item, now, ctx)
            return (fmt.block("📱", note,
                              fmt.kv("日程", f"「{title}」 {when}"),
                              fmt.kv("提醒", f"已规划 {len(recs)} 条"))
                    + "\n手机上保存后下次同步会合并。")
        # provider write succeeded → re-sync picks up the real row, plans
        # and announces with dedupe
        time.sleep(1.5)
        self.sync_now(announce=True)
        return (fmt.block("✅", "日程已创建",
                          fmt.kv("日程", f"「{title}」 {when}"),
                          fmt.kv("提醒", "已规划（详见同步通知）")))

    def _cmd_alarm(self, args: str) -> str:
        args = args.strip()
        if not self.phone.ensure()[0]:
            return "手机离线，无法管理闹钟。"
        if not args:
            alarms = self.phone.list_alarms()
            if alarms is None:
                return ("⚠️ 读不到系统闹钟库（厂商限制）。\n"
                        "/alarm set HH:MM 标签 直接设置")
            if not alarms:
                return "⏰ 手机上没有闹钟。"
            lines = [f"共 {len(alarms)} 个"]
            for a in alarms:
                mark = "🟢" if a["enabled"] else "⚪"
                lines.append(
                    f"{mark} [{a['id']}] {a['h']:02d}:{a['m']:02d}"
                    + (f" {a['label']}" if a["label"] else ""))
            lines.append(fmt.footer("/alarm del <id> 删除"))
            return fmt.block("⏰", "手机闹钟", *lines)
        parts = args.split(maxsplit=2)
        if parts[0] == "set":
            m = re.match(r"^(\d{1,2}):(\d{2})$",
                         parts[1]) if len(parts) > 1 else None
            if not m:
                return "用法: /alarm set HH:MM [标签]"
            h, mi = int(m.group(1)), int(m.group(2))
            if not (0 <= h <= 23 and 0 <= mi <= 59):
                return "时间不对（HH:MM）。"
            label = parts[2] if len(parts) > 2 else "Kami"
            return (
                f"✅ 已设置 {h:02d}:{mi:02d}「{label}」"
                if self.phone.set_alarm(h, mi, label)
                else "❌ 闹钟设置失败（SET_ALARM 未被时钟应用响应）"
            )
        if parts[0] == "del" and len(parts) > 1:
            target = parts[1]
            alarms = self.phone.list_alarms()
            if alarms is None:
                return "读不到闹钟库，无法删除——请在手机时钟应用操作。"
            if target == "all":
                n = sum(self.phone.delete_alarm(a["id"]) for a in alarms)
                return f"已删除 {n} 个闹钟。"
            if any(a["id"] == target for a in alarms):
                return ("✅ 已删除" if self.phone.delete_alarm(target)
                        else "❌ 删除失败")
            return f"找不到闹钟 [{target}]。/alarm 查看 id。"
        return "用法: /alarm | /alarm set HH:MM [标签] | /alarm del <id|all>"

    def _cmd_mem(self, args: str) -> str:
        args = args.strip()
        with self._lock:
            manual = list(self.state.setdefault("manual_memories", []))
            xiaobu = list(self.state.get("xiaobu_memories", []))
        if args.startswith("add "):
            text = args[4:].strip()
            if not text:
                return "用法: /mem add <内容>"
            with self._lock:
                manual.append({
                    "id": uuid.uuid4().hex[:6], "text": text,
                    "added": datetime.now().strftime("%m-%d %H:%M"),
                })
                self.state["manual_memories"] = manual[-200:]
                self._save()
            return f"📝 已记录（第 {len(self.state['manual_memories'])} 条）。"
        if args.startswith("del"):
            m = re.match(r"^del\s+(\d+)$", args)
            if not m:
                return "用法: /mem del <序号>（/mem 查看序号，仅手动条目）"
            idx = int(m.group(1)) - 1
            with self._lock:
                if 0 <= idx < len(manual):
                    gone = manual.pop(idx)
                    self.state["manual_memories"] = manual
                    self._save()
                    return f"已删除：{gone['text'][:50]}"
            return "序号超出范围（只能删手动条目）。"
        # list
        lines = []
        if xiaobu:
            lines.append(fmt.section(f"小布记忆 · {len(xiaobu)} 条（只读镜像）"))
            lines += [f"  {m['text'][:60]}" for m in xiaobu[-8:]]
        if manual:
            lines.append(fmt.section(f"手动记忆 · {len(manual)} 条"))
            first = max(1, len(manual) - 14)
            lines += [f"  {first + n}. {m['text'][:60]}"
                      for n, m in enumerate(manual[-15:])]
        if not lines:
            return ("🧠 还没有记忆。\n"
                    "/mem add <内容> 手动添加；小布记忆在手机 root 后自动同步。")
        lines.append(fmt.footer("/mem add <text> · /mem del <n>（仅手动）"))
        return fmt.block("🧠", "记忆库", *lines)

    def _cmd_plan(self, args: str, ctx: PluginContext) -> str:
        if not args:
            return "用法: /plan <描述>   例: /plan 高数课 明天 8:00"
        now = datetime.now()
        parsed = parse_when(args, now) or parse_when_llm(args, now, ctx)
        if parsed is None:
            return "没解析出时间。试试：/plan 标题 明天 8:00-9:40"
        title, start, end = parsed
        item = Item(uid="dryrun", source="calendar", kind="event",
                    title=title, start=start, end=end)
        actions = self._planner().plan(item, now, ctx)
        if not actions:
            return f"📋「{title}」 {item.when()}：时间太近，无提醒计划。"
        planner = self.state.get("settings", {}).get("planner")
        lines = [f"「{title}」 {item.when()}"]
        for n, a in enumerate(actions):
            circ = fmt.CIRCLED[n] if n < len(fmt.CIRCLED) else f"{n + 1}."
            lines.append(f"{circ} {a.at:%m-%d %H:%M} — {a.desc or a.text}")
        lines.append(fmt.footer("/addevent <描述> 创建真实日程"))
        return fmt.block("📋", f"提醒计划预览 · {planner} 规划器", *lines)

    # ── message hook ──

    def on_message(self, text: str, ctx: PluginContext) -> str | None:
        self._capture(ctx)
        t = text.strip().lower()
        with self._lock:
            pc = self.state.get("pending_confirm")
        if not pc:
            return None
        if time.time() > pc.get("expire", 0):
            with self._lock:
                self.state["pending_confirm"] = None
                self._save()
            return None
        if t in CONFIRM_WORDS:
            h, m, label = pc["h"], pc["m"], pc.get("label", "Kami")
            ok = self.phone.ensure()[0] and self.phone.set_alarm(h, m, label)
            with self._lock:
                self.state["pending_confirm"] = None
                self._save()
            return (
                f"✅ 已设置手机闹钟 {h:02d}:{m:02d}「{label}」，晚安！"
                if ok else
                "❌ 设置失败（手机离线或时钟未响应）——记得手动设置闹钟。"
            )
        if t in DECLINE_WORDS:
            with self._lock:
                self.state["pending_confirm"] = None
                self._save()
            return "好的，闹钟就交给你自己啦。"
        return None


def _ts_to_dt(ts) -> datetime:
    try:
        return datetime.fromtimestamp(float(ts or 0))
    except (ValueError, OSError, OverflowError):
        return datetime.now()


def register_source_factory(fn) -> None:
    """Public hook for other plugins to add data sources."""
    register_source(fn)
