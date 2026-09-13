#!/usr/bin/env python3
"""Morning-bomb alarm plugin.

/bomb [HH:MM] [count]  — arm a one-shot bombardment: at the target time the
plugin sends `count` messages ~1.2s apart (default 50 → about one minute).
/bomb off              — disarm a pending bombardment.
/bomb status           — show current state.

Delayed sends reuse the bound send_text handle with an empty context token,
the same pattern the scheduler's fire callback uses for proactive messages.
"""

import threading
from datetime import datetime, timedelta

from plugins import Plugin, PluginContext

DEFAULT_TIME = "07:00"
DEFAULT_COUNT = 50
INTERVAL = 1.2  # seconds between messages; 49 gaps ≈ 59s for the default

SPECIALS = {
    1: "☀️ 早上好！现在是叫醒服务时间——快起床！",
    10: "还不起？我开始认真了。",
    20: "紧急通报：你的被窝已被包围。",
    30: "轰到一半了哦，再赖床上午就没了。",
    40: "最后 10 条倒计时，且炸且珍惜。",
    49: "好啦好啦，这是倒数第二条……",
}


def _msg(i: int, count: int) -> str:
    if i in SPECIALS:
        return f"【{i}/{count}】{SPECIALS[i]}"
    bangs = "！" * min(1 + i // 10, 6)
    return f"【{i}/{count}】起床{bangs}"


class BombPlugin(Plugin):
    name = "bomb"
    description = "早晨消息轰炸闹钟（一次性）"
    commands = {
        "/bomb": "/bomb [HH:MM] [count] | off | status — 定时消息轰炸叫醒"
    }

    def __init__(self) -> None:
        self._stop: threading.Event | None = None
        self._info = ""

    def on_stop(self) -> None:
        # Reload/bridge exit disarms a pending timer so it never double-fires.
        if self._stop is not None:
            self._stop.set()
            self._stop = None
            self._info = ""

    def handle_command(self, cmd: str, args: str, ctx: PluginContext) -> str | None:
        args = args.strip()
        if args == "off":
            if self._stop is not None:
                self._stop.set()
                self._stop = None
                self._info = ""
                return "已解除轰炸布防。"
            return "当前没有布防中的轰炸。"
        if args == "status":
            return f"状态：{self._info or '未布防'}"

        parts = args.split()
        time_spec = parts[0] if parts else DEFAULT_TIME
        count = DEFAULT_COUNT
        if len(parts) > 1:
            try:
                count = max(1, min(int(parts[1]), 200))
            except ValueError:
                return "用法：/bomb [HH:MM] [count] | off | status"

        try:
            hh, mm = time_spec.split(":")
            target = datetime.now().replace(
                hour=int(hh), minute=int(mm), second=0, microsecond=0
            )
        except (ValueError, IndexError):
            return "时间格式不对，示例：/bomb 07:30"

        now = datetime.now()
        if target <= now:
            target += timedelta(days=1)
        delay = (target - now).total_seconds()

        if self._stop is not None:
            return f"已经布防了（{self._info}）。先 /bomb off 再重新设置。"

        stop = threading.Event()
        self._stop = stop
        self._info = f"{count} 条 @ {target.strftime('%m-%d %H:%M')}"
        threading.Thread(
            target=self._run,
            args=(ctx.user_id, ctx.send_text, stop, delay, count),
            daemon=True,
        ).start()
        return f"💣 已布防：明早 {target.strftime('%H:%M')} 轰炸 {count} 条（约1分钟）。/bomb off 可取消。"

    def _run(
        self,
        user_id: str,
        send_text,
        stop: threading.Event,
        delay: float,
        count: int,
    ) -> None:
        if stop.wait(delay):
            return  # disarmed before firing
        fails = 0
        for i in range(1, count + 1):
            if stop.is_set():
                return
            if not send_text(user_id, "", _msg(i, count)):
                fails += 1
                if fails >= 5:
                    send_text(user_id, "", "[bomb] 连续发送失败，已中止")
                    return
            else:
                fails = 0
            if i < count:
                stop.wait(INTERVAL)
        send_text(user_id, "", "轰完收工！快起床，别浪费这个上午 😤")
        self._stop = None
        self._info = ""
