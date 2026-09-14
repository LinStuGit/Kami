<p align="center">
  <h1 align="center">Kami</h1>
  <p align="center">
    <strong>微信 ClawBot 直连 Claude Code，无需 OpenClaw</strong>
  </p>
  <p align="center">
    用微信操控你电脑上的 Claude Code — 随时随地，掌上编程
  </p>
  <p align="center">
    <a href="#quick-start">快速开始</a> |
    <a href="#features">功能特性</a> |
    <a href="#commands">命令参考</a> |
    <a href="#architecture">架构设计</a>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.11+-blue?logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
    <img src="https://img.shields.io/badge/dependencies-3-brightgreen" alt="Deps">
    <img src="https://img.shields.io/badge/telemetry-zero-critical" alt="No Telemetry">
    <img src="https://img.shields.io/badge/lines-~2000-informational" alt="LOC">
  </p>
</p>

---

## Why Kami?

你是否想过：**通勤路上用手机让 Claude Code 帮你改 bug？**

Kami 用 **~2000 行 Python**，直接把微信 ClawBot 和 Claude Code CLI 连在一起。轻量、安全、开箱即用。

<p align="center">
  <img src="assets/flow.png" alt="Kami 架构流程" width="700">
</p>

### 亮点

- **全媒体输入** — 文字、语音、图片，发给 ClawBot 就能用
- **持久记忆** — Claude 记得你之前说过的话
- **定时任务** — 提醒、巡检、自动汇报
- **Session 管理** — 手机上切换电脑里的编程会话
- **多 Agent** — Claude Code / Codex / Gemini / Aider 一键切换
- **零遥测** — 所有数据留在你本地，~2000 行代码完全可审计

---

<a id="features"></a>

## Features

### 1. 文字 / 语音 / 图片 — 全媒体输入

```
文字消息 ──→ 直接发送给 Claude Code
语音消息 ──→ 微信自动转文字 → 发送给 Claude Code
图片消息 ──→ CDN 下载 + AES-128 解密 → Claude Code 读图理解
文件附件 ──→ 下载保存 → 告知 Claude Code 文件路径
```

发一张代码截图，Claude 就能帮你分析和修改。发一段语音描述 bug，Claude 就开始修。

### 2. 持久记忆系统 (OpenClaw-inspired)

```
/remember 项目用 React + TypeScript
/remember 数据库密码在 .env 里
/memory                              ← 查看所有记忆
/search TypeScript                   ← 搜索记忆和近3天对话
```

记忆存储为 Markdown 文件，**每次对话自动注入**给 Claude 作为上下文。再也不用重复说明项目背景。

### 3. 定时任务 (OpenClaw-inspired)

```
/remind 17:00 提交代码           ← 下午5点提醒
/remind in 30m 检查部署           ← 30分钟后提醒
/every 2h !检查服务器状态         ← 每2小时让 Claude 检查并汇报
/cron 0 9 * * 1-5 早报           ← 工作日早上9点
/jobs                             ← 查看所有任务
```

`!` 前缀的消息会经过 Claude Code 处理后再发给你 — 真正的**自动化巡检**。

### 4. Session 管理 — 掌控电脑上的所有会话

```
/sessions                          ← 列出电脑上所有 Claude Code sessions
/use 3                             ← 切换到第3个 session
/new                               ← 开始全新会话
/workdir ~/projects/my-app         ← 运行时切换工作目录
```

通勤时继续之前在办公室开始的编程会话。

### 5. 多 Agent 支持

```
/agent                             ← 查看可用 agent
/agent claude                      ← Claude Code (默认)
/agent codex                       ← OpenAI Codex CLI
/agent gemini                      ← Google Gemini CLI
/agent aider                       ← Aider
```

一键切换 AI 引擎，对比不同 agent 的效果。

### 6. 人设系统

```
/persona 你是一个资深全栈工程师，偏好 TypeScript
/persona 你是 IC 设计专家，熟悉 Cadence 工具链
```

为每个用户设置独立的 Claude 行为风格，持久化保存。

### 7. 文件收发

```
接收：微信发文件 ──→ 自动下载到 media/ ──→ Claude 直接读取处理
发送：让 Claude 生成文件并在回复里写 [[file: /绝对路径]] ──→ 自动通过微信发送
手动：/send <本地路径>      ← 把电脑上的任意文件发到微信
查看：/files                ← 列出最近收到的 10 个文件
```

发送链路与官方 openclaw-weixin 插件一致：`getuploadurl` 预签名 → AES-128-ECB 加密 → CDN 上传 → `sendmessage` file_item。上限 100 MB。

### 8. 双模型路由 — 快问本地小模型，大任务 Claude

```
/mode auto        ← 默认：寒暄/短问题 → 本地模型（~1.5s），复杂任务 → Claude
/mode fast        ← 全部走本地模型
/mode pro         ← 全部走 Claude
!消息前缀          ← 单条强制走 Claude
```

本地模型由守护进程托管 `llama-server`（127.0.0.1:8899，OpenAI 兼容接口，GPU 全量 offload）；
本地不可用时自动回退 Claude。启发式路由偏保守——含任务动词（写/生成/修复/分析…）的消息一律走 Claude。

**视觉路由**：守护进程同时托管 **Qwen3-VL-4B**（127.0.0.1:8188，带 mmproj 投影器）。
auto/fast 模式下，微信发来的图片自动由本地 VL 模型识别回答（~2s）；图片配文字若含任务动词仍升级 Claude；
VL 不可用时回退 Claude 读图。`/status` 可查看两个本地模型的健康状态。

### 9. 工单系统 — 先快答、再委派、完成汇报

```
你：帮我写个爬虫脚本
[Qwen3.5-2B | 快速应答]            ← 秒回：本地模型先给思路
已创建工单 T-a1b2，委派给 Claude Code 执行…
⏳ [Claude Code | T-a1b2] 仍在执行（已 3m00s）   ← 长任务每 3 分钟汇报
✅ [Claude Code | T-a1b2] 完成（耗时 5m12s）：…   ← 完成回复，署名模型
```

- 每一条模型回复都标注来源模型（+工单号）；快问直答不建工单
- `/tickets` 查看进行中的工单，`/cancel T-xxxx` 取消，`/models` 查看已注册模型
- **注册新模型**：插件里 `ctx.register_model(ModelSpec(key=…, name=…, runner=…))` 即接入路由与工单体系（见 `models.py`）
- **远程提供商**：`providers.json`（0600）配置 OpenAI 兼容端点（默认 paratera/GLM 全家桶），`models` 映射即白名单——未列出的模型一律拒绝。微信 `/ask glm-4-flash 你好` 直接问指定模型；文生图模型（CogView）的产出会作为文件发回微信

---

<a id="quick-start"></a>

## Quick Start

### 前置条件

- Python 3.11+
- 微信 8.0.70+（已启用 ClawBot 插件）
- Claude Code CLI（`npm install -g @anthropic-ai/claude-code`）

### 安装

```bash
git clone https://github.com/allenhuang0/Kami.git
cd Kami
pip install -r requirements.txt
```

### 首次运行

```bash
python bridge.py
```

终端会显示二维码 — 用微信扫码即可。登录后自动开始监听消息。

### 后续运行

```bash
python bridge.py                    # 自动恢复登录
python bridge.py -w ~/my-project    # 指定 Claude Code 工作目录
python bridge.py --login            # 重新扫码登录
python bridge.py --logout           # 清除登录凭据
```

---

<a id="commands"></a>

## Commands

<details>
<summary><strong>完整命令参考（点击展开）</strong></summary>

### Session

| 命令 | 功能 |
|------|------|
| `/sessions` | 列出电脑上 Claude Code sessions |
| `/use <n>` | 切换到第 n 个 session |
| `/new` | 开始新会话 |
| `/reset` | 清除当前 session |
| `/workdir <path>` | 运行时切换工作目录 |

### Agent

| 命令 | 功能 |
|------|------|
| `/agent` | 列出可用 agent |
| `/agent <name>` | 切换 agent（claude/codex/gemini/aider）|

### Memory

| 命令 | 功能 |
|------|------|
| `/remember <text>` | 保存长期记忆 |
| `/forget <keyword>` | 删除匹配的记忆 |
| `/memory` | 查看所有记忆 |
| `/search <query>` | 搜索记忆和近期对话 |
| `/log` | 查看今天的对话日志 |

### Persona

| 命令 | 功能 |
|------|------|
| `/persona` | 查看当前人设 |
| `/persona <desc>` | 设置人设 |

### Schedule

| 命令 | 功能 |
|------|------|
| `/remind <time> <msg>` | 设置提醒（`17:00`, `in 30m`）|
| `/every <interval> <msg>` | 间隔重复（`30m`, `2h`, `1d`）|
| `/cron <expr> <msg>` | cron 表达式 |
| `/jobs` | 查看所有任务 |
| `/cancel <id>` | 取消任务 |

### Other

| 命令 | 功能 |
|------|------|
| `/status` | 查看 bridge 状态 |
| `/help` | 显示帮助 |

</details>

---

<a id="architecture"></a>

## Architecture

```
Kami/
  bridge.py          # 主桥接器：消息路由、Agent 调用、命令分发
  ilink_client.py    # iLink Bot API 客户端：登录、轮询、发送
  memory_store.py    # 持久记忆系统（Markdown 文件存储）
  scheduler.py       # 定时任务调度器（cron/interval/reminder）
  requirements.txt   # 仅 3 个依赖：httpx, qrcode, cryptography
```

### 数据存储

所有数据存储在本地 `~/.config/kami/`，无云端同步：

| 文件 | 用途 | 权限 |
|------|------|------|
| `token.json` | 微信登录凭据 | 0600 |
| `sessions.json` | Claude session 映射 | 0600 |
| `persona.json` | 人设配置 | 0600 |
| `jobs.json` | 定时任务 | 0600 |
| `cursor.dat` | 消息同步游标 | 0600 |
| `memory/MEMORY.md` | 长期记忆 | - |
| `memory/YYYY-MM-DD.md` | 每日对话日志 | - |

### 安全设计

- HTTPS 全链路加密（iLink 官方 API）
- BaseURL 白名单验证（仅 `*.weixin.qq.com`）
- 文件路径遍历防护
- 线程池并发控制（最大 8 workers）
- 内部错误不暴露给微信用户
- **零遥测、零数据收集、零第三方请求**

---

## Auto-Start (macOS)

<details>
<summary>设置开机自启（点击展开）</summary>

```bash
cat > ~/Library/LaunchAgents/com.weclaude.bridge.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.weclaude.bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>python3</string>
        <string>/path/to/Kami/bridge.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/Kami</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
EOF

# 修改上面的路径后加载
launchctl load ~/Library/LaunchAgents/com.weclaude.bridge.plist
```

</details>

---

## FAQ

<details>
<summary><strong>Q: 和 OpenClaw 有什么区别？</strong></summary>

OpenClaw 是完整的 AI 助手平台（200K+ 行代码），Kami 只做一件事：把微信消息转发给 Claude Code。更轻量、更专注、更易审计。
</details>

<details>
<summary><strong>Q: Session 显示 "Ended" 是 bug 吗？</strong></summary>

不是。`claude -p` 模式每次调用后进程退出，session 标记为 Ended。但通过 `--resume`，对话上下文完整保留。Kami 自动管理这个过程。
</details>

<details>
<summary><strong>Q: 支持群聊吗？</strong></summary>

目前只支持 ClawBot 私聊。群聊支持计划在未来版本中加入。
</details>

<details>
<summary><strong>Q: 数据安全吗？</strong></summary>

所有数据存储在你本地电脑上，不经过任何第三方服务器。代码完全开源，~2000 行可以完整审计。
</details>

---

## Daemon (Windows)

用 `daemon.py` 把 bridge 跑成后台守护进程：崩溃自动拉起（指数退避）、日志落盘（`logs/weclaude.log`，5MB 轮转）。同时守护 **CC Switch**：每 30 秒检测一次，发现没在运行就自动打开（你手动打开的实例不会被干扰）。

```bash
python daemon.py login              # 首次：扫码登录（前台，需真实终端）
python daemon.py start              # 后台启动守护进程（bridge + CC Switch）
python daemon.py status             # 查看状态（daemon / bridge / ccswitch）
python daemon.py restart -w ~/proj  # 重启并指定 Claude 工作目录
python daemon.py stop               # 停止
python daemon.py foreground         # 前台运行 supervisor（调试用）
python daemon.py start --no-ccswitch        # 不管理 CC Switch
python daemon.py start --ccswitch PATH\to\cc-switch.exe  # 指定 CC Switch 路径
python daemon.py install-autostart  # Windows 登录时自动启动（schtasks）
python daemon.py uninstall-autostart
```

崩溃重启策略：bridge 连续崩溃时间隔按 2^n 秒退避（上限 120s），稳定运行 10 分钟后重置；CC Switch 只负责「没运行就打开」，不强制重启。

---

## Plugins

把 `.py` 文件放进 `plugins/` 目录即可扩展功能，启动时自动加载（`_` 开头的文件跳过）。参考实现见 `plugins/example.py`。

```python
# plugins/my_plugin.py
from plugins import Plugin, PluginContext

class MyPlugin(Plugin):
    name = "my"
    description = "我的插件"
    commands = {"/hi": "/hi - 打招呼"}

    def handle_command(self, cmd, args, ctx):
        return f"你好！{args}"

    def on_message(self, text, ctx):
        # 返回 str 则直接回复并拦截；返回 None 继续走 AI
        return None

    def on_response(self, text, response, ctx):
        # 改写 AI 回复后再发给用户
        return response
```

| 钩子 | 时机 | 返回值 |
|------|------|--------|
| `on_start` / `on_stop` | bridge 启动 / 退出 | — |
| `handle_command(cmd, args, ctx)` | 用户输入命中 `commands` 里注册的斜杠命令（优先于内置命令） | 回复文本或 None |
| `on_message(text, ctx)` | 消息即将发给 AI 之前 | 回复文本（拦截）或 None |
| `on_response(text, resp, ctx)` | AI 回复发给用户之前 | 改写后的文本 |

`ctx` 提供 `reply(text)`（回复当前用户）、`send_file(path)`（发文件给当前用户）、`ask_agent(msg)`（调 AI）、`memory`、`scheduler`、`user_id`、`working_dir`。单个插件抛异常不影响 bridge 运行。

微信端命令：`/plugins` 查看已加载插件，`/reload` 热重载。

---

## Contributing

欢迎 PR！项目结构简单，~2000 行 Python，容易上手。

```bash
git clone https://github.com/allenhuang0/Kami.git
cd Kami
pip install -r requirements.txt
ruff check .
python -m py_compile bridge.py
```

---

## License

[MIT](LICENSE)

---

<p align="center">
  <strong>Kami</strong> — 把 Claude Code 装进微信
</p>
