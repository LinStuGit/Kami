#!/usr/bin/env python3
"""Ticket system — track delegated tasks end to end.

Flow: a request that routes to a long-running model becomes a ticket.
The user gets (1) an immediate quick answer, (2) periodic progress reports
for long runs, (3) the final reply — each message labelled with the model
that produced it and the ticket id.
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import fmt

logger = logging.getLogger(__name__)

MONITOR_INTERVAL_S = 15
DEFAULT_PROGRESS_EVERY_S = 180


@dataclass
class Ticket:
    id: str
    user_id: str
    context_token: str
    text: str
    model_key: str
    model_name: str
    image_paths: list[Path] = field(default_factory=list)
    file_paths: list[Path] = field(default_factory=list)
    status: str = "queued"  # queued | running | done | failed | cancelled
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    result: str | None = None
    error: str | None = None
    progress_note: str = ""  # runners may set this for richer reports
    progress_every_s: int = DEFAULT_PROGRESS_EVERY_S
    last_report: float = 0.0
    chain: list[str] = field(default_factory=list)  # escalation order
    chain_pos: int = 0              # index into chain of the current model_key

    def __post_init__(self) -> None:
        if not self.chain:
            self.chain = [self.model_key]

    def elapsed_s(self) -> float:
        end = self.finished_at or time.time()
        start = self.started_at or self.created_at
        return max(0.0, end - start)

    def next_key(self) -> str | None:
        """Advance to the next model in the escalation chain.

        Returns the next model key, or None when the chain is exhausted.
        """
        if self.chain_pos + 1 >= len(self.chain):
            return None
        self.chain_pos += 1
        return self.chain[self.chain_pos]


class TicketManager:
    """Creates, tracks and reports tickets. Thread-safe."""

    def __init__(self) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()
        self._send_text = None  # (to_user, context_token, text) -> bool
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── wiring ──

    def bind(self, send_text) -> None:
        self._send_text = send_text

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run_forever, daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _send(self, ticket: Ticket, text: str) -> None:
        if self._send_text is None:
            return
        try:
            self._send_text(ticket.user_id, ticket.context_token, text)
        except Exception as e:
            logger.error("Ticket %s send failed: %s", ticket.id, e)

    def notify(self, ticket: Ticket, text: str) -> None:
        """Public: send a message tied to this ticket (result / progress)."""
        self._send(ticket, text)

    # ── lifecycle ──

    def create(
        self,
        user_id: str,
        context_token: str,
        text: str,
        model_key: str,
        model_name: str,
        image_paths: list[Path] | None = None,
        file_paths: list[Path] | None = None,
        progress_every_s: int = DEFAULT_PROGRESS_EVERY_S,
        chain: list[str] | None = None,
    ) -> Ticket:
        tid = f"T-{uuid.uuid4().hex[:4]}"
        t = Ticket(
            id=tid,
            user_id=user_id,
            context_token=context_token,
            text=text,
            model_key=model_key,
            model_name=model_name,
            image_paths=list(image_paths or []),
            file_paths=list(file_paths or []),
            progress_every_s=progress_every_s,
            chain=list(chain) if chain else [model_key],
        )
        with self._lock:
            self._tickets[tid] = t
        return t

    def get(self, tid: str) -> Ticket | None:
        with self._lock:
            return self._tickets.get(tid)

    def cancel(self, user_id: str, tid: str) -> str:
        t = self.get(tid)
        if t is None or t.user_id != user_id:
            return f"🔍 找不到工单 {tid}。"
        if t.status in ("done", "failed", "cancelled"):
            return f"ℹ️ 工单 {tid} 已结束（{t.status}），无需取消。"
        t.status = "cancelled"
        return (
            fmt.block(
                "🗑", f"工单已取消 · {tid}",
                fmt.kv("模型", t.model_name),
            )
            + "\n后台结果将被丢弃。"
        )

    def list_for(self, user_id: str, include_done: bool = False) -> str:
        with self._lock:
            tickets = [
                t for t in self._tickets.values() if t.user_id == user_id
            ]
        tickets = [t for t in tickets if include_done or t.status in
                   ("queued", "running")]
        if not tickets:
            return ("🎫 没有进行中的工单。\n"
                    "复杂任务会自动建工单，或用 /task 模型 消息 手动派发。")
        lines = [f"## 🎫 进行中工单 · {len(tickets)}", ""]
        for i, t in enumerate(sorted(tickets, key=lambda x: x.created_at)):
            brief = t.text[:36] + ("…" if len(t.text) > 36 else "")
            tier = f" ↥{t.chain_pos}" if t.chain_pos else ""
            lines.append(
                f"{fmt.CIRCLED[i]} {t.id} · {t.model_name}{tier} · "
                f"{fmt_elapsed(t.elapsed_s())}"
            )
            lines.append(f"   {brief}")
        lines.append(fmt.footer("/cancel T-xxxx 取消 · 完成的工单自动消失"))
        return "\n".join(lines)

    # ── reporting ──

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self._report_pass()
            except Exception as e:
                logger.error("Ticket monitor pass failed: %s", e)
            self._stop.wait(MONITOR_INTERVAL_S)

    def _report_pass(self) -> None:
        now = time.time()
        with self._lock:
            running = [
                t for t in self._tickets.values() if t.status == "running"
            ]
        for t in running:
            if now - t.last_report < t.progress_every_s:
                continue
            t.last_report = now
            note = f"\n{t.progress_note[:150]}" if t.progress_note else ""
            self._send(
                t,
                fmt.block(
                    "⏳", f"工单进行中 · {t.id}",
                    fmt.kv("模型", t.model_name),
                    fmt.kv("已执行", fmt_elapsed(t.elapsed_s())),
                )
                + (f"\n{note}" if note else "")
                + "\n" + fmt.footer(f"/cancel {t.id} 可取消"),
            )


def fmt_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
