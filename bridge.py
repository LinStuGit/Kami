#!/usr/bin/env python3
"""Kami — WeChat bot ↔ Claude Code bridge.

Bridges WeChat ClawBot messages to Claude Code and other AI agent CLIs.
No OpenClaw needed — directly uses iLink API.

Usage:
    python bridge.py              # Login and start bridge (default: claude)
    python bridge.py -w /path     # Set working directory
    python bridge.py --logout     # Clear credentials
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ilink_client import ILinkClient
from llm_local import ask_local, ask_local_vision, is_local_up, is_vl_up
from memory_store import MemoryStore
from models import (
    GroupSpec,
    ModelSpec,
    all_models,
    describe_models,
    get_group,
    get_model,
    register_group,
    register_model,
)
from plugins import PluginManager
import fmt
import providers
from scheduler import Scheduler
from tickets import TicketManager, fmt_elapsed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # poll spam floods the daemon log

from paths import CONFIG_DIR
SESSION_FILE = CONFIG_DIR / "sessions.json"
MEDIA_DIR = CONFIG_DIR / "media"
PERSONA_FILE = CONFIG_DIR / "persona.json"
MODES_FILE = CONFIG_DIR / "modes.json"

# The bridge runs console-less (pythonw under the daemon); console children
# like claude.exe would each pop up a NEW console window without this flag.
_SILENT = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW

# Marker the agent can emit to deliver files to the user: [[file: /path]]
FILE_MARKER_RE = re.compile(r"\[\[file:\s*(.+?)\s*\]\]")

# Per-user state
_sessions: dict[str, str] = {}  # user_id -> session_id
_user_agent: dict[str, str] = {}  # user_id -> agent_key
_sessions_lock = threading.Lock()

# Runtime mutable working directory
_working_dir: str | None = None
_workdir_lock = threading.Lock()

_executor = ThreadPoolExecutor(max_workers=8)

# Live ILinkClient (set in run_bridge) for background workers that must send
# files (ticket [[file:]] delivery).
_bridge_client: ILinkClient | None = None

# OpenClaw-inspired subsystems
_memory = MemoryStore()
_scheduler = Scheduler()
_plugins = PluginManager()
_tickets = TicketManager()
_personas: dict[str, str] = {}  # user_id -> persona string
_user_modes: dict[str, str] = {}  # user_id -> auto | fast | pro


def _load_modes() -> None:
    try:
        _user_modes.update(json.loads(MODES_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _save_modes() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MODES_FILE.write_text(json.dumps(_user_modes, indent=2))


def _looks_simple(text: str) -> bool:
    """Heuristic: True = short/casual query fit for the local fast model.

    Conservative by design — anything ambiguous routes to Claude.
    """
    t = text.strip()
    if not t or len(t) > 120 or "\n" in t or "```" in t or "http" in t:
        return False
    low = t.lower()
    # Big-task markers always go to Claude
    heavy = (
        "写", "生成", "实现", "修复", "修一下", "调试", "分析", "重构", "开发",
        "创建", "搭建", "部署", "脚本", "代码", "程序", "论文", "报告", "文档",
        "项目", "读取", "发给我", "文件", "debug", "refactor", "implement",
        "build", "fix", "create", "generate", "write a", "code",
    )
    if any(k in low for k in heavy):
        return False
    simple = (
        "你好", "您好", "hi", "hello", "在吗", "谢谢", "感谢", "thanks",
        "你是谁", "几点", "今天星期", "天气", "翻译", "什么意思", "为什么",
        "是什么", "怎么读", "多少", "晚安", "早上好", "下午好", "再见",
        "help", "/",
    )
    if any(k in low for k in simple):
        return True
    # Short pure question
    if len(t) <= 40 and (t.endswith("?") or t.endswith("？")):
        return True
    return False


def call_local(message: str, user_id: str) -> str | None:
    """Fast reply from the local llama.cpp model. None = unavailable."""
    # Same context Claude gets: persona + shared MEMORY.md block, so both
    # paths answer from the same memory.
    parts = []
    persona = _personas.get(user_id, "")
    if persona:
        parts.append(f"Persona: {persona}")
    mem_ctx = _memory.get_context()
    if mem_ctx:
        parts.append(mem_ctx)
    system = "\n".join(parts) if parts else None
    return ask_local(message, system=system)


def call_local_vision(
    message: str, user_id: str, image_paths: list[Path]
) -> str | None:
    """Fast reply from the local Qwen3-VL model for image messages."""
    # Same context as call_local: persona + shared memory block.
    parts = []
    persona = _personas.get(user_id, "")
    if persona:
        parts.append(f"Persona: {persona}")
    mem_ctx = _memory.get_context()
    if mem_ctx:
        parts.append(mem_ctx)
    system = "\n".join(parts) if parts else None
    return ask_local_vision(message, image_paths, system=system)


# ── Model Registry Runners ──────────────────────────────────────
# Unified runner contract: (message, user_id, image_paths, file_paths) -> str


def _run_claude(message, user_id, image_paths=None, file_paths=None) -> str:
    with _workdir_lock:
        wd = _working_dir
    return call_agent(message, user_id, wd, image_paths, file_paths)


def _run_local(message, user_id, image_paths=None, file_paths=None) -> str:
    reply = call_local(message, user_id)
    return reply if reply is not None else "[本地模型不可用]"


def _run_vl(message, user_id, image_paths=None, file_paths=None) -> str:
    if not image_paths:
        return "[视觉模型需要图片输入]"
    reply = call_local_vision(message, user_id, image_paths)
    return reply if reply is not None else "[视觉模型不可用]"


def _register_builtin_models() -> None:
    """Seed the registry; plugins may register more via ctx.register_model()."""
    register_model(
        ModelSpec(
            key="claude",
            name="Claude Code",
            kind="agent",
            runner=_run_claude,
            supports_images=True,
            notes="full tool loop, runs as a ticket",
            icon="🤖",
            group="heavy",
        )
    )
    register_model(
        ModelSpec(
            key="local",
            name="Qwen3.5-2B",
            kind="text",
            runner=_run_local,
            notes="fast local text model",
            icon="⚡",
            group="quick",
        )
    )
    register_model(
        ModelSpec(
            key="vl",
            name="Qwen3-VL-4B",
            kind="vision",
            runner=_run_vl,
            supports_images=True,
            notes="fast local image understanding",
            icon="👁",
            group="vision",
        )
    )


def _register_remote_models() -> None:
    """Register allowlisted models from providers.json (e.g. paratera GLM).

    The provider config's model mapping is the allowlist — ids outside it
    are refused client-side in providers.py.
    """
    from llm_local import _image_data_url

    # Latency notes calibrated by bench (2026-09-14): see memory / logs.
    notes_map = {
        "GLM-4.5-Flash": "远程·思考型 (~10s)",
        "GLM-4-Flash": "远程·最快 (~2s)",
        "GLM-Z1-Flash": "远程·推理链 (~5s)",
        "GLM-4V-Flash": "远程·看图 (~1s)",
        "GLM-CogView3-Flash": "远程·文生图",
    }
    group_map = {
        "GLM-4.5-Flash": "reason",
        "GLM-4-Flash": "quick",
        "GLM-Z1-Flash": "reason",
        "GLM-4V-Flash": "vision",
        "GLM-CogView3-Flash": "draw",
    }

    def _remote_system(user_id: str) -> str:
        parts = ["用用户的语言简洁直接地回答。"]
        persona = _personas.get(user_id, "")
        if persona:
            parts.append(f"Persona: {persona}")
        mem_ctx = _memory.get_context()
        if mem_ctx:
            parts.append(mem_ctx)
        return "\n".join(parts)

    def make_runner(pname: str, upstream: str):
        def runner(message, user_id, image_paths=None, file_paths=None) -> str:
            content: str | list[dict] = message
            if image_paths:
                content = []
                for p in image_paths:
                    url = _image_data_url(Path(p))
                    if url:
                        content.append(
                            {"type": "image_url", "image_url": {"url": url}}
                        )
                content.append({"type": "text", "text": message})
            messages = [
                {"role": "system", "content": _remote_system(user_id)},
                {"role": "user", "content": content},
            ]
            reply = providers.chat_completion(pname, upstream, messages)
            return reply if reply else f"[{upstream} 无响应或出错，查看 bridge 日志]"

        return runner

    def make_imagegen_runner(pname: str, upstream: str):
        def runner(message, user_id, image_paths=None, file_paths=None) -> str:
            path = providers.generate_image(pname, upstream, message)
            if path is None:
                return (
                    f"[{upstream} 生成失败——若为 401 说明当前 key 无此模型权限]"
                )
            return f"已生成图片：\n[[file: {path}]]"

        return runner

    for pname, pcfg in providers.load_providers().items():
        if not pcfg.get("enabled", True):
            continue
        icon_map = {"GLM-4V-Flash": "👁", "GLM-CogView3-Flash": "🎨"}
        for key, upstream in pcfg.get("models", {}).items():
            if upstream == "GLM-4V-Flash":
                spec = ModelSpec(
                    key=key,
                    name=upstream,
                    kind="vision",
                    runner=make_runner(pname, upstream),
                    supports_images=True,
                    notes=notes_map.get(upstream, "远程模型"),
                    icon=icon_map.get(upstream, "✨"),
                    group=group_map.get(upstream, ""),
                )
            elif upstream == "GLM-CogView3-Flash":
                spec = ModelSpec(
                    key=key,
                    name=upstream,
                    kind="imagegen",
                    runner=make_imagegen_runner(pname, upstream),
                    notes=notes_map.get(upstream, "远程文生图"),
                    icon=icon_map.get(upstream, "✨"),
                    group=group_map.get(upstream, ""),
                )
            else:
                spec = ModelSpec(
                    key=key,
                    name=upstream,
                    kind="text",
                    runner=make_runner(pname, upstream),
                    notes=notes_map.get(upstream, "远程模型"),
                    icon=icon_map.get(upstream, "✨"),
                    group=group_map.get(upstream, ""),
                )
            register_model(spec)
            logger.info("Registered remote model: %s (%s)", key, upstream)


def _register_groups() -> None:
    """Specialty groups with tiered escalation chains for /task dispatch.

    Each chain tries its cheapest/most specialized model first and falls
    upward on failure, ending at the agent model as the universal tier.
    """
    register_group(GroupSpec(
        key="quick", name="秒答", icon="⚡",
        desc="快问快答、速查、翻译",
        chain=["local", "glm-4-flash", "glm-4.5-flash", "claude"],
    ))
    register_group(GroupSpec(
        key="reason", name="推理", icon="🧠",
        desc="数学、逻辑、多步分析",
        chain=["glm-z1-flash", "glm-4.5-flash", "claude"],
    ))
    register_group(GroupSpec(
        key="vision", name="视觉", icon="👁",
        desc="看图、识图、截图理解",
        chain=["vl", "glm-4v-flash", "claude"],
    ))
    register_group(GroupSpec(
        key="draw", name="绘图", icon="🎨",
        desc="文生图",
        chain=["glm-cogview3-flash", "claude"],
    ))
    register_group(GroupSpec(
        key="heavy", name="重活", icon="🤖",
        desc="编程、工具调用、长任务",
        chain=["claude"],
    ))


# ── Ticket Worker ───────────────────────────────────────────────


def _run_ticket_worker(tid: str) -> None:
    """Execute a ticket's assigned model and report the outcome to the user.

    Tiered dispatch: on a runner failure the ticket escalates to the next
    model in its chain until one succeeds or the chain is exhausted.
    """
    t = _tickets.get(tid)
    if t is None or t.status in ("cancelled", "running", "done"):
        return
    t.status = "running"
    t.started_at = time.time()
    t.last_report = time.time()
    while True:
        spec = get_model(t.model_key)
        try:
            if spec is None or spec.runner is None:
                raise RuntimeError(f"model '{t.model_key}' is not registered")
            result = spec.runner(t.text, t.user_id, t.image_paths, t.file_paths)
        except Exception as e:
            if t.status == "cancelled":
                return
            nxt = t.next_key()
            if nxt is None:
                t.status = "failed"
                t.error = str(e)
                t.finished_at = time.time()
                _tickets.notify(
                    t,
                    fmt.block(
                        "❌", f"工单失败 · {t.id}",
                        fmt.kv("模型", t.model_name),
                        fmt.kv("耗时", fmt_elapsed(t.elapsed_s())),
                    )
                    + f"\n{e}",
                )
                logger.error("Ticket %s failed: %s", tid, e)
                return
            # Escalate: same ticket, next tier up.
            prev_name = t.model_name
            nxt_spec = get_model(nxt)
            t.model_key = nxt
            t.model_name = nxt_spec.name if nxt_spec else nxt
            logger.warning(
                "Ticket %s escalating: %s -> %s (%s)",
                tid, prev_name, t.model_name, e,
            )
            _tickets.notify(
                t,
                fmt.block(
                    "⬆️", f"工单升级 · {t.id}",
                    fmt.kv("原因", str(e)[:80]),
                    fmt.kv("梯队", f"{prev_name} → {t.model_name}"),
                ),
            )
            continue

        t.result = result
        if t.status == "cancelled":
            return  # user cancelled mid-run — drop the result
        t.status = "done"
        t.finished_at = time.time()
        text_out = md_to_wechat(result)
        # Runners may emit [[file: path]] (e.g. image generation) — deliver.
        try:
            if _bridge_client is not None and FILE_MARKER_RE.search(text_out):
                text_out = _deliver_files(
                    _bridge_client, t.user_id, t.context_token, text_out
                )
        except Exception as e:
            logger.warning("Ticket %s file delivery failed: %s", tid, e)
        tier = f" ↥{t.chain_pos}" if t.chain_pos else ""
        _tickets.notify(
            t,
            fmt.block(
                "✅", f"工单完成 · {t.id}",
                fmt.kv("模型", t.model_name + tier),
                fmt.kv("耗时", fmt_elapsed(t.elapsed_s())),
            )
            + f"\n{text_out}",
        )
        logger.info("Ticket %s done (%d chars)", tid, len(result))
        try:
            _memory.log_conversation(f"[{tid}] {t.text[:150]}", result[:200])
        except Exception:
            pass
        return


def _quick_ack(
    text: str, model_name: str, tid: str, grp: str = ""
) -> str:
    """Fast first response: local model drafts an acknowledgment; never runs
    the task itself. Falls back to a static line when the local model is down.
    ``grp`` (e.g. "⚡ 秒答") marks a group dispatch that escalates on failure.
    """
    quick = ask_local(
        f"用户发来请求：「{text[:300]}」。\n"
        "不要执行这个任务！只用一两句话（中文）表示已收到，"
        "并用一句话说明准备怎么做。"
    )
    ack = quick or "收到，正在安排执行。"
    local_spec = get_model("local")
    local_name = local_spec.name if local_spec else "本地模型"
    route = (
        f"📨 工单 {tid} · {grp} · {model_name} 起步，失败自动逐级升级"
        if grp
        else f"📨 工单 {tid} → {model_name} 执行中，完成后带署名回复"
    )
    return (
        f"## ⚡ {local_name} · 秒答\n\n{ack}\n\n---\n"
        f"{route}\n"
        f"/cancel {tid} 取消 · /tickets 查看"
    )


# ── /help ───────────────────────────────────────────────────────

# (key, 名称, icon, 详情用法行)；索引行取每节前 3 行的首个词
HELP_SECTIONS: list[tuple[str, str, str, list[str]]] = [
    ("chat", "聊天", "💬", [
        "/new 新会话 · /reset 清空 · /sessions 列出",
        "/use 序号 切换 · /workdir 路径 换工作目录",
        "!开头 单条强制 Claude · 普通消息直接发",
    ]),
    ("model", "模型", "🧩", [
        "/models 分组与模型一览",
        "/task 分组|模型 消息 建工单（失败逐级升级）",
        "/ask 模型 消息 直问指定模型",
        "/mode auto|fast|pro 路由模式",
        "分组：⚡秒答 🧠推理 👁视觉 🎨绘图 🤖重活",
    ]),
    ("ticket", "工单", "🎫", [
        "/tickets 进行中工单 · /cancel T-xxxx 取消",
        "长任务后台执行 · 定期汇报 · 结果带模型署名",
    ]),
    ("memory", "记忆", "🧠", [
        "/remember 文本 记住 · /forget 词 忘掉",
        "/memory 全部记忆 · /search 词 搜历史",
        "/log 最近对话日志",
    ]),
    ("timer", "定时", "⏰", [
        "/remind 时间 内容 · /every 间隔 内容 循环",
        "/cron 分 时 日 月 周 内容 · /jobs 查看",
        "/cancel id 取消任务",
    ]),
    ("file", "文件", "📁", [
        "/send 路径 电脑文件发到微信 · /files 最近文件",
        "回复里写 [[file: 路径]] 也会自动发送",
    ]),
    ("sys", "系统", "📊", [
        "/status 状态 · /persona 人设 · /help [节名] 指南",
        "/plugins 插件列表 · /reload 重载",
        "/mdtest 微信 Markdown 渲染自检",
    ]),
]


def _help_index(plugins: list) -> list[str]:
    """One line per section — the compact /help front page."""
    lines = ["## 📖 Kami 指南", ""]
    for _key, name, icon, usages in HELP_SECTIONS:
        toks = [u.split()[0] for u in usages if u.startswith("/")]
        lines.append(f"{icon} {name}  {' · '.join(toks[:3])}")
    for p in plugins:
        cmds = [c.split()[0] for c in list(p.commands)[:4]]
        lines.append(f"🔌 {p.name}  {' · '.join(cmds)}")
    lines.append(fmt.footer("/help 节名 看详情 · /help 全部 展开所有"))
    return lines


def _help_detail(lines: list[str]) -> list[str]:
    """Shared tail for expanded /help pages."""
    lines.append(fmt.footer("/help 全部 看所有 · /help 回索引"))
    return lines


# ── Context-token persistence (for proactive pushes) ────────────

# ilink sendmessage rejects empty context_token (ret=-3), so proactive
# pushes (scheduler, /mdtest, …) reuse the latest token each user sent us.
CTX_TOKENS_FILE = CONFIG_DIR / "ctx_tokens.json"
_last_ctx: dict[str, str] = {}


def _load_ctx_tokens() -> None:
    try:
        _last_ctx.update(json.loads(CTX_TOKENS_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _remember_ctx(user_id: str, token: str) -> None:
    """Persist the newest live context_token per user (best-effort)."""
    if not token or _last_ctx.get(user_id) == token:
        return
    _last_ctx[user_id] = token
    try:
        CTX_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CTX_TOKENS_FILE.write_text(
            json.dumps(_last_ctx, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        try:
            os.chmod(CTX_TOKENS_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except OSError as e:
        logger.warning("Failed to persist context token: %s", e)


def _push_text(client: ILinkClient, user_id: str, text: str) -> bool:
    """Proactive push: stored live token first, empty token as fallback."""
    return client.send_text(user_id, _last_ctx.get(user_id, ""), text)


MD_PROBE = """## 二级标题
**加粗** · *斜体* · `行内代码` · ~~删除线~~
上一行后无空行接本行，行中**加粗**与`代码`测试

### 三级标题（前有空行）

- 无序一
- 无序二
  - 嵌套项
- 列表内 **加粗** 测试

1. 有序一
2. 有序二

> 引用块：Kami 渲染探测

```python
print("hello kami")
```

[链接](https://example.com)

| 列A | 列B |
| --- | --- |
| 甲 | 2 |
| 乙 | 10 |

**行首加粗:** 模仿日志样式
- [x] 已完成任务
- [ ] 未完成任务"""


def _load_personas() -> None:
    try:
        _personas.update(json.loads(PERSONA_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _save_personas() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PERSONA_FILE.write_text(json.dumps(_personas, indent=2, ensure_ascii=False))
    try:
        os.chmod(PERSONA_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ── Agent Definitions ───────────────────────────────────────────

AGENTS: dict[str, dict] = {
    "claude": {
        "name": "Claude Code",
        "binary": "claude",
        "build_cmd": lambda msg, sid: _build_claude_cmd(msg, sid),
        "use_stdin": True,
        "parse_output": lambda stdout, uid: _parse_claude_output(stdout, uid),
    },
    "codex": {
        "name": "Codex CLI",
        "binary": "codex",
        "build_cmd": lambda msg, sid: ["codex", "-q", msg],
        "use_stdin": False,
        "parse_output": lambda stdout, uid: stdout.strip() or "[No response]",
    },
    "gemini": {
        "name": "Gemini CLI",
        "binary": "gemini",
        "build_cmd": lambda msg, sid: ["gemini", "-p", msg],
        "use_stdin": False,
        "parse_output": lambda stdout, uid: stdout.strip() or "[No response]",
    },
    "aider": {
        "name": "Aider",
        "binary": "aider",
        "build_cmd": lambda msg, sid: ["aider", "--message", msg, "--yes"],
        "use_stdin": False,
        "parse_output": lambda stdout, uid: stdout.strip() or "[No response]",
    },
}


# Tools Claude may use without an approval prompt. Headless (-p) runs have no
# permission-prompt channel, so unlisted tools are auto-denied — which is why
# the session reported "no file read / command execution tools". Scoped
# allowlist instead of --dangerously-skip-permissions: only these are granted.
# MCP tools are appended at startup (mcp__<server> rules, see _detect_mcp_servers).
AGENT_ALLOWED_TOOLS = ",".join(
    [
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "NotebookEdit",
        "Bash",
        "Task",
        "TodoWrite",
        "WebFetch",
        "WebSearch",
    ]
)


# ── MCP Discovery ───────────────────────────────────────────────

_mcp_servers: list[str] = []  # filled once at startup via `claude mcp list`

# Attaching MCP servers costs ~40s per session (SolidWorks proxy cold start),
# so sessions start WITHOUT them and attach on demand when the request looks
# like it needs them. "mcp" in the message always forces attachment.
MCP_KEYWORDS = (
    "mcp", "solidworks", "trilium",
    "cad", "零件", "装配体", "工程图", "三维图", "建模",
    "笔记", "笔记本",
)


def _wants_mcp(message: str) -> bool:
    low = message.lower()
    return any(k in low for k in MCP_KEYWORDS)


def _detect_mcp_servers() -> list[str]:
    """Names of configured MCP servers, for mcp__<name> allowlist rules.

    `claude mcp list` prints lines like `name: target (type) - status`;
    user/project-scope servers configured in Claude Code are picked up
    automatically, so `claude mcp add` is all it takes to expose a new one
    to WeChat.
    """
    binary = _find_binary("claude")
    if not binary:
        return []
    try:
        result = subprocess.run(
            [binary, "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=90,
            **_SILENT,
        )
        names: list[str] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if ":" not in line or line.startswith("Checking"):
                continue
            name = line.split(":", 1)[0].strip()
            if name and " " not in name and re.match(r"^[\w.-]+$", name):
                names.append(name)
        return names
    except Exception as e:
        logger.warning("MCP server detection failed: %s", e)
        return []


def _build_claude_cmd(
    message: str, session_id: str | None, use_mcp: bool | None = None
) -> list[str]:
    """Build Claude Code CLI command.

    use_mcp=None auto-decides from the message: attaching MCP servers costs
    ~40s per session (SolidWorks proxy cold start), so they are only loaded
    when the request looks like it needs them.
    """
    attach = _wants_mcp(message) if use_mcp is None else use_mcp
    allowed = AGENT_ALLOWED_TOOLS
    if attach and _mcp_servers:
        allowed += "," + ",".join(f"mcp__{s}" for s in _mcp_servers)
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--allowedTools",
        allowed,
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    return cmd


def _get_user_agent(user_id: str) -> str:
    return _user_agent.get(user_id, "claude")


# ── Binary Resolution ───────────────────────────────────────────

# The VS Code extension dir name embeds a version that changes on
# auto-update, e.g. anthropic.claude-code-2.1.270-win32-x64 — match
# the prefix and pick the highest version.
_CLAUDE_EXT_RE = re.compile(
    r"^anthropic\.claude-code-(\d+(?:\.\d+)*)-", re.IGNORECASE
)
_CLAUDE_EXT_CACHE_TTL = 60  # seconds; short so auto-updates are picked up
_claude_ext_cache: tuple[float, str | None] | None = None


def _pick_claude_from_roots(roots: list[Path]) -> str | None:
    """Scan extension roots for the newest bundled claude binary."""
    exe_name = "claude.exe" if os.name == "nt" else "claude"
    best: tuple[tuple[int, ...], Path] | None = None
    for ext_root in roots:
        if not ext_root.is_dir():
            continue
        for entry in ext_root.iterdir():
            m = _CLAUDE_EXT_RE.match(entry.name)
            if not m or not entry.is_dir():
                continue
            exe = entry / "resources" / "native-binary" / exe_name
            if not exe.is_file():
                continue
            version = tuple(int(p) for p in m.group(1).split("."))
            if best is None or version > best[0]:
                best = (version, exe)
    return str(best[1]) if best else None


def _find_vscode_claude() -> str | None:
    """Locate the VS Code extension's native claude binary (with TTL cache)."""
    global _claude_ext_cache
    now = time.monotonic()
    if _claude_ext_cache and now - _claude_ext_cache[0] < _CLAUDE_EXT_CACHE_TTL:
        cached = _claude_ext_cache[1]
        # Drop the cache early if the extension updated and the dir is gone.
        if cached is None or Path(cached).is_file():
            return cached
        _claude_ext_cache = None

    roots = [
        Path.home() / ".vscode" / "extensions",
        Path.home() / ".vscode-insiders" / "extensions",
    ]
    result = _pick_claude_from_roots(roots)
    _claude_ext_cache = (now, result)
    if result:
        logger.info("Using VS Code claude binary: %s", result)
    return result


def _find_binary(name: str) -> str | None:
    """Resolve a CLI binary: prefer the VS Code bundled claude (native exe,
    auto-updated), fall back to PATH."""
    if name == "claude":
        vscode = _find_vscode_claude()
        if vscode:
            return vscode
    return shutil.which(name)


# ── Session Persistence ─────────────────────────────────────────


def _load_sessions() -> None:
    try:
        with _sessions_lock:
            _sessions.update(json.loads(SESSION_FILE.read_text()))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load sessions: %s", e)


def _save_sessions() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _sessions_lock:
        SESSION_FILE.write_text(json.dumps(_sessions, indent=2))
    try:
        os.chmod(SESSION_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ── Markdown → WeChat-friendly Markdown ─────────────────────────

_TASK_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[( |x|X)\]\s*(.*)$")
_HR_RE = re.compile(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_HEADING_RE = re.compile(r"\s*#{1,6}\s")


def md_to_wechat(text: str) -> str:
    """Pass model markdown through to WeChat's renderer, repairing only
    what it can't show: task lists (→ ✅/⬜ glyphs) and headings/rules
    missing their blank line above. Tables render natively — untouched."""
    out: list[str] = []
    lines = text.replace("\r\n", "\n").split("\n")
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue
        m = _TASK_RE.match(line)
        if m:
            box = "✅" if m.group(2).lower() == "x" else "⬜"
            out.append(f"{m.group(1)}{box} {m.group(3)}".rstrip())
            i += 1
            continue
        # Headings/rules need a blank line above or they stay raw (and a
        # bare --- would underline the previous line setext-style).
        if ((_HEADING_RE.match(line) or _HR_RE.match(line))
                and out and out[-1].strip()):
            out.append("")
        out.append(line)
        i += 1
    return "\n".join(out).strip()


# ── Continuous Typing Indicator ─────────────────────────────────


def _typing_loop(
    client: ILinkClient,
    to_user: str,
    context_token: str,
    stop_event: threading.Event,
) -> None:
    """Send typing indicator every 5 seconds until stop_event is set."""
    while not stop_event.is_set():
        try:
            client.send_typing(to_user, context_token)
        except Exception:
            break
        stop_event.wait(5)


# ── Claude Code Output Parsing ──────────────────────────────────


def _parse_claude_output(stdout: str, user_id: str) -> str:
    """Parse Claude CLI JSON output, extract text and session_id."""
    if not stdout.strip():
        return "[No response from Claude Code]"

    try:
        data = json.loads(stdout)
        session_id = data.get("session_id")
        if session_id:
            with _sessions_lock:
                _sessions[user_id] = session_id
            _save_sessions()

        result_text = data.get("result", "")
        if not result_text:
            result_text = data.get("text", data.get("content", str(data)))
        return result_text if result_text else "[Empty response]"
    except json.JSONDecodeError:
        # NDJSON fallback (streaming output)
        lines = stdout.strip().splitlines()
        text_parts = []
        for line in lines:
            try:
                obj = json.loads(line)
                if obj.get("type") == "result":
                    sid = obj.get("session_id")
                    if sid:
                        with _sessions_lock:
                            _sessions[user_id] = sid
                        _save_sessions()
                    text_parts.append(obj.get("result", ""))
                elif obj.get("type") == "assistant" and "content" in obj:
                    for block in obj["content"]:
                        if block.get("type") == "text":
                            text_parts.append(block["text"])
            except (json.JSONDecodeError, TypeError, KeyError):
                text_parts.append(line)
        return "\n".join(text_parts) if text_parts else stdout.strip()


# ── Agent Invocation ────────────────────────────────────────────


def call_agent(
    message: str,
    user_id: str,
    working_dir: str | None = None,
    image_paths: list[Path] | None = None,
    file_paths: list[Path] | None = None,
) -> str:
    """Call the user's selected AI agent CLI and return the response."""
    agent_key = _get_user_agent(user_id)
    agent = AGENTS.get(agent_key)
    if not agent:
        return f"[Unknown agent: {agent_key}]"

    binary = _find_binary(agent["binary"])
    if not binary:
        return f"[{agent['name']} not found. Install it first.]"

    with _sessions_lock:
        session_id = _sessions.get(user_id) if agent_key == "claude" else None

    if agent_key == "claude":
        logger.info("Claude session: %s", session_id[:12] if session_id else "new")

    # Append media instructions so Claude knows what arrived
    if image_paths:
        paths_str = ", ".join(str(p) for p in image_paths)
        img_note = (
            f"\n\nThe user sent {len(image_paths)} image(s). "
            f"Use the Read tool to view: {paths_str}\n"
            f"Describe what you see and respond to the user's message."
        )
        message += img_note
    if file_paths:
        files_str = "\n".join(f"  - {p.name}: {p}" for p in file_paths)
        file_note = (
            f"\n\nThe user sent {len(file_paths)} file(s):\n{files_str}\n"
            f"Use the Read tool (or Bash) to inspect them and respond accordingly."
        )
        message += file_note

    cmd = agent["build_cmd"](message, session_id)
    # Replace binary name with full path
    cmd[0] = binary

    # Build system prompt with memory + persona + file protocol
    if agent_key == "claude":
        sys_parts = []
        persona = _personas.get(user_id, "")
        if persona:
            sys_parts.append(f"Persona: {persona}")
        mem_ctx = _memory.get_context()
        if mem_ctx:
            sys_parts.append(mem_ctx)
        sys_parts.append(
            "To deliver a file to the user, save it anywhere on disk and include "
            "[[file: /absolute/path]] in your reply. The bridge sends it via WeChat "
            "and strips the marker. Multiple files = multiple markers."
        )
        if sys_parts:
            cmd.extend(["--append-system-prompt", "\n".join(sys_parts)])

    # Pass user message via stdin (clean, no context mixing)
    stdin_data = None
    if agent["use_stdin"]:
        stdin_data = message

    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=1800,
            cwd=working_dir,
            **_SILENT,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Claude session expired -> retry
            if (
                agent_key == "claude"
                and session_id
                and ("session" in stderr.lower() and "not found" in stderr.lower())
            ):
                logger.warning("Session expired, starting fresh.")
                with _sessions_lock:
                    _sessions.pop(user_id, None)
                _save_sessions()
                retry_cmd = _build_claude_cmd(message, None)
                retry_cmd[0] = binary
                # Re-add system prompt context
                sys_parts = []
                persona = _personas.get(user_id, "")
                if persona:
                    sys_parts.append(f"Persona: {persona}")
                mem_ctx = _memory.get_context()
                if mem_ctx:
                    sys_parts.append(mem_ctx)
                if sys_parts:
                    retry_cmd.extend(["--append-system-prompt", "\n".join(sys_parts)])
                result = subprocess.run(
                    retry_cmd,
                    input=stdin_data,
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    cwd=working_dir,
                    **_SILENT,
                )

            if result.returncode != 0:
                logger.error("%s error (exit %d)", agent["name"], result.returncode)
                if stderr:
                    logger.error("%s stderr: %s", agent["name"], stderr[:500])
                return f"[{agent['name']} error. Check bridge logs for details.]"

        return agent["parse_output"](result.stdout, user_id)

    except subprocess.TimeoutExpired:
        return f"[{agent['name']} timed out after 5 minutes]"
    except FileNotFoundError:
        return f"[{agent['name']} CLI not found]"


# ── Image Handling ──────────────────────────────────────────────


def _handle_media(
    client: ILinkClient, message: dict
) -> tuple[list[Path], list[Path]]:
    """Download images/files from message. Returns (image_paths, file_paths)."""
    media_items = client.extract_media(message)
    images: list[Path] = []
    files: list[Path] = []
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    for item in media_items:
        if item["type"] == "image" and (
            item.get("cdn_url") or item.get("encrypt_query_param")
        ):
            ext = ".jpg"
            path = MEDIA_DIR / f"img_{uuid.uuid4().hex[:12]}{ext}"
            if client.download_media(
                item.get("cdn_url", ""),
                item.get("aes_key", ""),
                path,
                encrypt_query_param=item.get("encrypt_query_param", ""),
            ):
                images.append(path)
                logger.info(
                    "Downloaded image: %s (%d bytes)", path.name, path.stat().st_size
                )
        elif item["type"] == "file" and (
            item.get("cdn_url") or item.get("encrypt_query_param")
        ):
            raw_name = item.get("filename", f"file_{int(time.time())}")
            safe_name = Path(raw_name).name  # strip path components
            if not safe_name or safe_name.startswith("."):
                safe_name = f"file_{int(time.time() * 1000)}"
            path = MEDIA_DIR / safe_name
            # Avoid clobbering an earlier file with the same name
            if path.exists():
                path = path.with_stem(f"{path.stem}_{uuid.uuid4().hex[:6]}")
            if client.download_media(
                item.get("cdn_url", ""),
                item.get("aes_key", ""),
                path,
                encrypt_query_param=item.get("encrypt_query_param", ""),
            ):
                expected_md5 = item.get("md5", "")
                if expected_md5:
                    actual = hashlib.md5(path.read_bytes()).hexdigest()
                    if actual != expected_md5:
                        logger.warning(
                            "File md5 mismatch for %s (got %s, want %s)",
                            path.name,
                            actual,
                            expected_md5,
                        )
                files.append(path)
                logger.info(
                    "Downloaded file: %s (%d bytes)",
                    path.name,
                    path.stat().st_size,
                )

    return images, files


def _deliver_files(
    client: ILinkClient,
    from_user: str,
    context_token: str,
    response: str,
) -> str:
    """Send every [[file: path]] the agent emitted; return cleaned text."""
    sent: list[str] = []
    missing: list[str] = []

    def _replace(match: re.Match) -> str:
        raw = match.group(1).strip().strip("'\"")
        path = Path(raw).expanduser()
        if path.is_file():
            if client.send_file(from_user, context_token, path):
                sent.append(path.name)
                return ""
            return f"[file failed: {path.name}]"
        missing.append(raw)
        return f"[file not found: {raw}]"

    cleaned = FILE_MARKER_RE.sub(_replace, response)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    if sent:
        logger.info("Delivered %d file(s) to %s", len(sent), from_user[:16])
    return cleaned


# ── Session Management ──────────────────────────────────────────


def list_claude_sessions(working_dir: str | None = None) -> str:
    """List recent Claude Code sessions."""
    binary = _find_binary("claude")
    if not binary:
        return "[Claude Code CLI not found]"
    try:
        result = subprocess.run(
            [binary, "sessions", "list", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=working_dir,
            **_SILENT,
        )
        if result.returncode != 0:
            return "[Failed to list sessions]"

        sessions = json.loads(result.stdout) if result.stdout.strip() else []
        if not sessions:
            return "🗂 没有活跃会话。/new 开始新会话。"

        shown = sessions[:10]
        lines = [f"## 🗂 Claude 会话 · {len(shown)}", ""]
        for i, s in enumerate(shown, 1):
            sid = s.get("id", s.get("session_id", "?"))
            summary = s.get("summary", s.get("name", ""))[:36]
            ts = (s.get("updated_at", s.get("timestamp", "")) or "")[:19]
            ts_s = ts[5:16].replace("T", " ") if len(ts) >= 16 else ts
            lines.append(f"{fmt.CIRCLED[i - 1]} [{sid[:8]}] {summary}")
            if ts_s:
                lines.append(f"   {ts_s}")
        lines.append(fmt.footer("/use 序号 切换 · /new 新会话"))
        return "\n".join(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return "[Failed to list sessions]"


def pick_session(choice: str, user_id: str, working_dir: str | None = None) -> str:
    """Switch to a session by number or ID."""
    binary = _find_binary("claude")
    if not binary:
        return "[Claude Code CLI not found]"
    try:
        result = subprocess.run(
            [binary, "sessions", "list", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=working_dir,
            **_SILENT,
        )
        sessions = json.loads(result.stdout) if result.stdout.strip() else []
    except Exception:
        return "[Failed to list sessions]"

    if not sessions:
        return "No sessions available."

    target = None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(sessions):
            target = sessions[idx]
    except ValueError:
        for s in sessions:
            sid = s.get("id", s.get("session_id", ""))
            if sid.startswith(choice):
                target = s
                break

    if not target:
        return f"Session '{choice}' not found. Use /sessions to list."

    session_id = target.get("id", target.get("session_id", ""))
    summary = target.get("summary", target.get("name", ""))[:40]
    with _sessions_lock:
        _sessions[user_id] = session_id
    _save_sessions()
    return f"✅ 已切换会话 [{session_id[:8]}] {summary}"


# ── Message Handler ─────────────────────────────────────────────


def handle_message(
    client: ILinkClient,
    msg: dict,
) -> None:
    """Handle a single incoming WeChat message (runs in thread)."""
    global _working_dir
    from_user = msg.get("from_user_id", "unknown")
    context_token = msg.get("context_token", "")
    _remember_ctx(from_user, context_token)

    try:
        text = client.extract_text(msg) or ""

        # Download images/files (best-effort)
        image_paths: list[Path] = []
        file_paths: list[Path] = []
        try:
            image_paths, file_paths = _handle_media(client, msg)
        except Exception as e:
            logger.warning("Media download failed: %s", e)

        # Handle voice messages
        try:
            for item in client.extract_media(msg):
                if item["type"] == "voice" and item.get("text"):
                    text = (text + "\n" + item["text"]) if text else item["text"]
        except Exception as e:
            logger.warning("Voice extraction failed: %s", e)

        if not text.strip() and not image_paths and not file_paths:
            return

        # Debug: log raw item_list types
        raw_items = msg.get("item_list", [])
        item_types = [i.get("type") for i in raw_items]
        if any(t != 1 for t in item_types):
            logger.info(
                "Raw item_list types: %s, keys: %s",
                item_types,
                [list(i.keys()) for i in raw_items],
            )

        logger.info(
            "Message from %s (%d chars, %d images, %d files)",
            from_user[:16],
            len(text),
            len(image_paths),
            len(file_paths),
        )

        cmd = text.strip()
        cmd_lower = cmd.lower()

        with _workdir_lock:
            working_dir = _working_dir

        ctx = _plugins._make_ctx(from_user, context_token, working_dir)

        # ── Plugin commands (may override built-ins) ──
        plugin_reply = _plugins.dispatch_command(cmd, ctx)
        if plugin_reply is not None:
            client.send_text(from_user, context_token, plugin_reply)
            return

        # ── Special commands ──
        if cmd_lower in ("/reset", "/clear"):
            with _sessions_lock:
                _sessions.pop(from_user, None)
            _save_sessions()
            client.send_text(from_user, context_token, "🗑 会话已清空。")
            return

        if cmd_lower == "/status":
            with _sessions_lock:
                sid = _sessions.get(from_user, "")[:8] or "未设"
            agent_name = AGENTS.get(_get_user_agent(from_user), {}).get("name", "?")
            mode = _user_modes.get(from_user, "auto")
            client.send_text(
                from_user,
                context_token,
                fmt.block(
                    "📊", "Kami 状态",
                    fmt.kv("模型", agent_name),
                    fmt.kv("会话", sid),
                    fmt.kv("模式", mode),
                    fmt.kv("本地", ("⚡ up" if is_local_up() else "⚡ down")
                           + f"  ·  👁 {'up' if is_vl_up() else 'down'}"),
                    fmt.kv("目录", working_dir or "(默认)"),
                ),
            )
            return

        if cmd_lower == "/sessions":
            client.send_text(
                from_user, context_token, list_claude_sessions(working_dir)
            )
            return

        if cmd_lower.startswith("/use "):
            client.send_text(
                from_user,
                context_token,
                pick_session(cmd[5:].strip(), from_user, working_dir),
            )
            return

        if cmd_lower == "/new":
            with _sessions_lock:
                _sessions.pop(from_user, None)
            _save_sessions()
            client.send_text(from_user, context_token, "✨ 新会话已开始。")
            return

        if cmd_lower.startswith("/workdir"):
            parts = cmd.split(maxsplit=1)
            if len(parts) < 2:
                client.send_text(
                    from_user,
                    context_token,
                    f"📂 当前目录：{working_dir or '(默认)'}\n"
                    "用法：/workdir /path/to/project",
                )
                return
            new_path = Path(parts[1].strip()).expanduser()
            if not new_path.is_dir():
                client.send_text(
                    from_user, context_token,
                    f"🔍 目录不存在：{parts[1].strip()}",
                )
                return
            with _workdir_lock:
                _working_dir = str(new_path)
            client.send_text(
                from_user, context_token, f"📂 工作目录 → {new_path}"
            )
            return

        if cmd_lower in ("/agent", "/agents"):
            current = _get_user_agent(from_user)
            lines = ["## 🤖 可用 Agent", ""]
            for key, agent in AGENTS.items():
                installed = bool(_find_binary(agent["binary"]))
                marker = " ←当前" if key == current else ""
                lines.append(
                    f"{'🟢' if installed else '⚪'} {key}  {agent['name']}{marker}"
                    if installed else
                    f"⚪ {key}  {agent['name']}（未安装）"
                )
            lines.append(fmt.footer("/agent 名称 切换"))
            client.send_text(from_user, context_token, "\n".join(lines))
            return

        if cmd_lower.startswith("/agent "):
            agent_key = cmd[7:].strip().lower()
            if agent_key not in AGENTS:
                client.send_text(
                    from_user,
                    context_token,
                    f"🔍 没有 {agent_key} 这个 agent。\n"
                    f"可选：{' / '.join(AGENTS)}",
                )
                return
            agent = AGENTS[agent_key]
            if not _find_binary(agent["binary"]):
                client.send_text(
                    from_user,
                    context_token,
                    f"⚪ {agent['name']} 未安装（缺 {agent['binary']}）",
                )
                return
            _user_agent[from_user] = agent_key
            with _sessions_lock:
                _sessions.pop(from_user, None)
            _save_sessions()
            client.send_text(
                from_user, context_token, f"✅ 已切换 → {agent['name']}"
            )
            return

        # ── Memory commands ──
        if cmd_lower.startswith("/remember "):
            content = cmd[10:].strip()
            client.send_text(from_user, context_token, _memory.remember(content))
            return

        if cmd_lower.startswith("/forget "):
            keyword = cmd[8:].strip()
            client.send_text(from_user, context_token, _memory.forget(keyword))
            return

        if cmd_lower == "/memory":
            client.send_text(from_user, context_token, _memory.list_memories())
            return

        if cmd_lower.startswith("/search "):
            query = cmd[8:].strip()
            client.send_text(from_user, context_token, _memory.search(query))
            return

        if cmd_lower == "/log":
            client.send_text(
                from_user, context_token,
                md_to_wechat(_memory.get_today_log()),
            )
            return

        if cmd_lower == "/mdtest":
            # Markdown rendering probe: shows what the WeChat client
            # renders vs echoes raw.
            client.send_text(from_user, context_token, MD_PROBE)
            return

        # ── Persona commands ──
        if cmd_lower == "/mode":
            m = _user_modes.get(from_user, "auto")
            local_ok = is_local_up()
            mode_desc = {
                "auto": "简单 → 本地 ⚡ · 复杂 → Claude 🤖",
                "fast": "全部走本地模型 ⚡",
                "pro": "全部走 Claude 🤖",
            }[m]
            client.send_text(
                from_user,
                context_token,
                fmt.block(
                    "🔀", f"路由模式 · {m}",
                    mode_desc,
                    fmt.kv("本地", "⚡ up" if local_ok else "⚡ down"),
                    fmt.kv("视觉", "👁 up" if is_vl_up() else "👁 down"),
                    fmt.footer("/mode auto | fast | pro",
                               "!开头 单条强制 Claude"),
                ),
            )
            return

        if cmd_lower.startswith("/mode "):
            m = cmd[6:].strip().lower()
            if m in ("auto", "fast", "pro"):
                _user_modes[from_user] = m
                _save_modes()
                client.send_text(from_user, context_token, f"🔀 模式 → {m}")
            else:
                client.send_text(
                    from_user,
                    context_token,
                    "用法：/mode auto | fast | pro",
                )
            return

        if cmd_lower == "/persona":
            p = _personas.get(from_user, "")
            client.send_text(
                from_user,
                context_token,
                fmt.block(
                    "🎭", "当前人设",
                    p or "（未设置，模型用默认风格回答）",
                    fmt.footer("/persona 描述 设置"),
                ),
            )
            return

        if cmd_lower.startswith("/persona "):
            persona_text = cmd[9:].strip()
            _personas[from_user] = persona_text
            _save_personas()
            client.send_text(
                from_user, context_token, f"🎭 人设已更新：{persona_text[:80]}"
            )
            return

        # ── Scheduler commands ──
        if cmd_lower.startswith("/remind "):
            parts = cmd[8:].strip().split(maxsplit=1)
            if len(parts) < 2:
                client.send_text(
                    from_user,
                    context_token,
                    "用法：/remind 时间 内容\n例：/remind 17:00 提交代码",
                )
                return
            client.send_text(
                from_user,
                context_token,
                _scheduler.add_reminder(from_user, parts[0], parts[1]),
            )
            return

        if cmd_lower.startswith("/every "):
            parts = cmd[7:].strip().split(maxsplit=1)
            if len(parts) < 2:
                client.send_text(
                    from_user,
                    context_token,
                    "用法：/every 间隔 内容\n例：/every 30m 检查服务器（!开头经 Claude）",
                )
                return
            run_claude = parts[1].startswith("!")
            msg = parts[1][1:].strip() if run_claude else parts[1]
            client.send_text(
                from_user,
                context_token,
                _scheduler.add_interval(from_user, parts[0], msg, run_claude),
            )
            return

        if cmd_lower.startswith("/cron "):
            # /cron 0 9 * * 1-5 Good morning
            cron_parts = cmd[6:].strip().split(maxsplit=5)
            if len(cron_parts) < 6:
                client.send_text(
                    from_user,
                    context_token,
                    "用法：/cron 分 时 日 月 周 内容\n例：/cron 0 9 * * 1-5 早报",
                )
                return
            cron_expr = " ".join(cron_parts[:5])
            msg = cron_parts[5]
            run_claude = msg.startswith("!")
            if run_claude:
                msg = msg[1:].strip()
            client.send_text(
                from_user,
                context_token,
                _scheduler.add_cron(from_user, cron_expr, msg, run_claude),
            )
            return

        if cmd_lower == "/jobs":
            client.send_text(from_user, context_token, _scheduler.list_jobs(from_user))
            return

        if cmd_lower.startswith("/task "):
            # Ticket flow for ANY registered model OR specialty group:
            # quick ack → background run with progress reports → labelled
            # result. A group ticket escalates up its chain on failure.
            parts = cmd[6:].strip().split(maxsplit=1)
            target = parts[0] if parts else ""
            spec = get_model(target)
            grp = None if spec else get_group(target.lower())
            if len(parts) < 2 or (spec is None and grp is None):
                client.send_text(
                    from_user,
                    context_token,
                    "用法：/task 模型|分组 消息\n"
                    "分组见 /models（如 quick 秒答 · reason 推理 · vision 看图）",
                )
                return
            msg = parts[1]
            if grp is not None:
                chain = [k for k in grp.chain if get_model(k)]
                if not chain:
                    client.send_text(
                        from_user, context_token,
                        f"⚠️ 分组 {grp.name} 里没有可用模型。/models 查看",
                    )
                    return
                head = get_model(chain[0])
                ticket = _tickets.create(
                    from_user, context_token, msg, chain[0],
                    head.name if head else chain[0], chain=chain,
                )
                client.send_text(
                    from_user, context_token,
                    _quick_ack(msg, head.name if head else chain[0],
                               ticket.id, grp=f"{grp.icon} {grp.name}"),
                )
                logger.info(
                    "Ticket %s created (%s -> group %s, chain %s)",
                    ticket.id, from_user[:16], grp.key, "→".join(chain),
                )
            else:
                ticket = _tickets.create(
                    from_user, context_token, msg, spec.key, spec.name
                )
                client.send_text(
                    from_user, context_token,
                    _quick_ack(msg, spec.name, ticket.id),
                )
                logger.info("Ticket %s created (%s -> %s)",
                            ticket.id, from_user[:16], spec.key)
            _executor.submit(_run_ticket_worker, ticket.id)
            return

        if cmd_lower == "/models":
            client.send_text(from_user, context_token, describe_models())
            return

        if cmd_lower.startswith("/ask "):
            parts = cmd[5:].strip().split(maxsplit=1)
            spec = get_model(parts[0]) if parts else None
            if spec is None or spec.runner is None or len(parts) < 2:
                client.send_text(
                    from_user,
                    context_token,
                    "用法：/ask 模型 消息\n可用模型见 /models",
                )
                return
            msg = parts[1]
            stop_typing = threading.Event()
            typing_thread = threading.Thread(
                target=_typing_loop,
                args=(client, from_user, context_token, stop_typing),
                daemon=True,
            )
            typing_thread.start()
            try:
                result = spec.runner(msg, from_user, [], [])
                result = md_to_wechat(result)
                result = _deliver_files(client, from_user, context_token, result)
            finally:
                stop_typing.set()
                typing_thread.join(timeout=1)
            client.send_text(
                from_user, context_token,
                fmt.tag_reply(spec.icon, spec.name, result),
            )
            try:
                _memory.log_conversation(f"/ask {spec.key}: {msg[:150]}", result[:200])
            except Exception:
                pass
            return

        if cmd_lower == "/tickets":
            client.send_text(from_user, context_token, _tickets.list_for(from_user))
            return

        if cmd_lower.startswith("/cancel "):
            job_id = cmd[8:].strip()
            if job_id.lower().startswith("t-"):
                client.send_text(
                    from_user, context_token, _tickets.cancel(from_user, job_id)
                )
                return
            client.send_text(
                from_user, context_token, _scheduler.cancel_job(from_user, job_id)
            )
            return

        if cmd_lower.startswith("/send "):
            raw = cmd[6:].strip().strip("'\"")
            path = Path(raw).expanduser()
            if not path.is_file():
                client.send_text(
                    from_user, context_token, f"File not found: {path}"
                )
                return
            stop_typing = threading.Event()
            stop_typing.set()
            if client.send_file(
                from_user, context_token, path, caption=f"[Sent] {path.name}"
            ):
                logger.info("Sent %s to %s", path.name, from_user[:16])
            else:
                client.send_text(
                    from_user,
                    context_token,
                    f"[Failed to send {path.name} — check bridge logs]",
                )
            return

        if cmd_lower == "/files":
            MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            entries = sorted(
                MEDIA_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
            )[:10]
            if not entries:
                client.send_text(
                    from_user, context_token, "📁 还没收到过文件。"
                )
                return
            lines = [f"## 📁 最近文件 · {len(entries)}", ""]
            for i, p in enumerate(entries):
                size_kb = p.stat().st_size / 1024
                age = time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))
                lines.append(f"{fmt.CIRCLED[i]} {p.name}")
                lines.append(f"   {size_kb:.0f} KB · {age}")
            lines.append(fmt.footer("发给 Claude 处理，或 /send 路径"))
            client.send_text(from_user, context_token, "\n".join(lines))
            return

        if cmd_lower == "/plugins":
            client.send_text(from_user, context_token, _plugins.list_plugins())
            return

        if cmd_lower == "/reload":
            client.send_text(
                from_user,
                context_token,
                f"Reloaded {_plugins.reload()} plugin(s).",
            )
            return

        if cmd_lower == "/help" or cmd_lower.startswith("/help "):
            arg = cmd[5:].strip()
            with _plugins._lock:
                loaded = [p for p in _plugins._plugins if p.commands]

            if not arg:
                lines = _help_index(loaded)
            elif arg.lower() in ("全部", "all"):
                lines = ["## 📖 Kami 指南 · 全部", ""]
                for _key, name, icon, usages in HELP_SECTIONS:
                    lines.append(fmt.section(f"{icon} {name}"))
                    lines += [f" {u}" for u in usages]
                for p in loaded:
                    lines.append(fmt.section(f"🔌 {p.name}"))
                    lines += [f" {h}" for h in p.commands.values()]
                lines.append(fmt.footer("普通消息直接发给当前模型"))
            else:
                a = arg.lower()
                sec = next(
                    ((icon, name, usages) for _k, name, icon, usages
                     in HELP_SECTIONS
                     if a == _k or a == name or name.startswith(arg)),
                    None,
                )
                plug = next(
                    (p for p in loaded
                     if len(a) >= 2
                     and (a == p.name.lower() or a in p.name.lower())),
                    None,
                )
                if sec:
                    icon, name, usages = sec
                    lines = _help_detail(
                        [f"## {icon} {name}", ""]
                        + [f" {u}" for u in usages]
                    )
                elif plug:
                    lines = _help_detail(
                        [f"## 🔌 {plug.name}", ""]
                        + ([f" {plug.description}"] if plug.description else [])
                        + [f" {h}" for h in plug.commands.values()]
                    )
                else:
                    lines = _help_index(loaded)
                    lines.append(f"🔍 没有这个节：{arg}")
            client.send_text(from_user, context_token, "\n".join(lines))
            return

        # ── Plugin message hook (short-circuit before the agent) ──
        plugin_reply = _plugins.dispatch_message(text, ctx)
        if plugin_reply is not None:
            client.send_text(from_user, context_token, plugin_reply)
            logger.info("Plugin replied to %s", from_user[:16])
            return

        # ── Routing: local fast model vs Claude ──
        force_claude = text.startswith("!")
        if force_claude:
            text = text[1:].lstrip()
            if not text:
                return
        mode = _user_modes.get(from_user, "auto")
        use_local = (
            not force_claude
            and not image_paths
            and (mode == "fast" or (mode == "auto" and _looks_simple(text)))
        )
        # Images go to the local Qwen3-VL unless the text is a heavy task
        # or the user forced Claude — those keep the full tool loop.
        use_vl = (
            not force_claude
            and bool(image_paths)
            and mode in ("auto", "fast")
            and (mode == "fast" or not text or _looks_simple(text))
        )
        route = "vl" if use_vl else ("local" if use_local else "claude")
        logger.info("Routing %s -> %s (mode=%s)", from_user[:16], route, mode)

        # ── Claude route → ticket flow ──
        # Quick answer first (local ack), delegate, report when done.
        # Trivial chats in pro/forced mode still answer directly (no ticket).
        if route == "claude" and (
            image_paths or file_paths or not _looks_simple(text)
        ):
            spec = get_model("claude")
            mname = spec.name if spec else "Claude Code"
            ticket = _tickets.create(
                from_user,
                context_token,
                text,
                "claude",
                mname,
                image_paths=image_paths,
                file_paths=file_paths,
            )
            client.send_text(
                from_user,
                context_token,
                _quick_ack(text, mname, ticket.id),
            )
            logger.info("Ticket %s created (%s)", ticket.id, from_user[:16])
            _executor.submit(_run_ticket_worker, ticket.id)
            return

        # ── Direct fast paths (local text / local vision / trivial chat) ──
        stop_typing = threading.Event()
        typing_thread = threading.Thread(
            target=_typing_loop,
            args=(client, from_user, context_token, stop_typing),
            daemon=True,
        )
        typing_thread.start()

        try:
            if use_vl:
                response = call_local_vision(
                    text or "请描述这张图片的内容", from_user, image_paths
                )
                if response is not None:
                    response = _plugins.transform_response(text, response, ctx)
            elif use_local:
                response = call_local(text, from_user)
                if response is not None:
                    response = _plugins.transform_response(text, response, ctx)
            else:
                response = None
            if response is None:
                # Pro mode, forced !, or local model down
                response = call_agent(
                    text, from_user, working_dir, image_paths, file_paths
                )
                route = "claude"
                response = _plugins.transform_response(text, response, ctx)
            route_spec = get_model(route)
            tag = route_spec.name if route_spec else route
            response = md_to_wechat(response)
            response = _deliver_files(client, from_user, context_token, response)
        finally:
            stop_typing.set()
            typing_thread.join(timeout=1)

        reply_icon = route_spec.icon if route_spec else "🤖"
        client.send_text(
            from_user, context_token,
            fmt.tag_reply(reply_icon, tag, response),
        )
        logger.info(
            "Replied to %s via %s (%d chars)", from_user[:16], tag, len(response)
        )

        # Log conversation to daily memory
        try:
            _memory.log_conversation(text[:200], response[:200])
        except Exception:
            pass

    except Exception as e:
        logger.error("handle_message error: %s", e, exc_info=True)
        try:
            client.send_text(
                from_user, context_token, "[Internal error, please try again]"
            )
        except Exception:
            pass


# ── Main Bridge Loop ────────────────────────────────────────────


def run_bridge(working_dir: str | None = None) -> None:
    """Main bridge loop: poll WeChat -> call agent -> reply."""
    global _working_dir, _bridge_client
    _working_dir = working_dir

    client = ILinkClient()

    if not client.is_logged_in:
        print("No saved login found. Starting QR code login...\n")
        client.login()

    _bridge_client = client

    _load_sessions()
    _load_personas()
    _load_ctx_tokens()
    _load_modes()

    # Scheduler callback: send message (and optionally run Claude) when job fires
    def _on_job_fire(user_id: str, message: str, run_claude: bool) -> None:
        try:
            if run_claude:
                response = call_agent(message, user_id, _working_dir, None)
                response = md_to_wechat(response)
                text = f"[Scheduled] {response}"
            else:
                text = f"[Reminder] {message}"
            if not _push_text(client, user_id, text):
                logger.warning(
                    "Failed to deliver scheduled message to %s", user_id[:16]
                )
        except Exception as e:
            logger.error("Scheduler callback error: %s", e)

    _scheduler.set_callback(_on_job_fire)
    _scheduler.start()

    # Ebbinghaus-style memory consolidation (old logs -> summaries -> gone)
    _memory.start_maintenance()

    # Wire plugin system to bridge capabilities, then load plugins/
    _register_builtin_models()
    _register_remote_models()
    _register_groups()

    # Detect MCP servers so their tools pass the headless allowlist
    global _mcp_servers
    _mcp_servers = _detect_mcp_servers()
    logger.info("MCP servers: %s", _mcp_servers or "none")

    _tickets.bind(send_text=lambda u, c, t: client.send_text(u, c, t))
    _tickets.start()

    _plugins.bind(
        send_text=lambda u, c, t: client.send_text(u, c, t),
        send_file_fn=lambda u, c, p: client.send_file(u, c, Path(p)),
        call_agent=call_agent,
        memory=_memory,
        scheduler=_scheduler,
        register_model_fn=register_model,
    )
    plugin_count = _plugins.load()

    print("\n=== Kami ===")
    print(f"Working directory: {working_dir or '(default)'}")
    print(f"Default agent: {AGENTS['claude']['name']}")
    print(f"Memory: {_memory.get_context()[:30] or '(empty)'}...")
    print(f"Scheduled jobs: {len(_scheduler._jobs)}")
    print(f"Plugins: {_plugins.count}")
    print("Listening for WeChat messages... (Ctrl+C to stop)\n")

    consecutive_errors = 0
    max_consecutive_errors = 10

    try:
        while True:
            try:
                messages = client.poll_messages()
                consecutive_errors = 0

                for msg in messages:
                    _executor.submit(handle_message, client, msg)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                consecutive_errors += 1
                logger.error(
                    "Poll error (%d/%d): %s",
                    consecutive_errors,
                    max_consecutive_errors,
                    e,
                )
                err_str = str(e).lower()
                if "401" in err_str or "unauthorized" in err_str:
                    logger.warning("Token may have expired. Re-login with --login")

                if consecutive_errors >= max_consecutive_errors:
                    logger.critical("Too many consecutive errors, stopping.")
                    break
                time.sleep(min(2**consecutive_errors, 30))

    except KeyboardInterrupt:
        print("\nStopping bridge...")
    finally:
        _save_sessions()
        _plugins.unload()
        _tickets.stop()
        _scheduler.stop()
        _executor.shutdown(wait=False)
        client.close()
        print("Bridge stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat ClawBot <-> AI Agent bridge")
    parser.add_argument("--logout", action="store_true", help="Clear login credentials")
    parser.add_argument("--login", action="store_true", help="Force re-login")
    parser.add_argument(
        "--workdir", "-w", type=str, default=None, help="Working directory"
    )
    args = parser.parse_args()

    if args.logout:
        client = ILinkClient()
        client.logout()
        client.close()
        print("Logged out.")
        return

    if args.login:
        client = ILinkClient()
        client.logout()
        client.login()
        client.close()
        print("Login complete.")
        return

    run_bridge(working_dir=args.workdir)


if __name__ == "__main__":
    main()
