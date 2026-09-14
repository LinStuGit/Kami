# Kami on Android

在安卓手机上跑整套 Kami：微信桥接 + Claude Code，带手机端控制台
（终端日志流 + 快捷按钮）、桌面小工具和 Shizuku 提权操作。

> **不想要 Termux？** 只需要"手机换网即向 PC 上报新 IP"的话，装独立
> 的 [`ipreport/`](../ipreport/) APK 即可（Shizuku 加固保活，
> 替代 `phone-daemon/adb-report.sh` 的常驻循环）。

## 架构

```
┌─ 手机 ────────────────────────────────────────────┐
│  Kami APK (WebView 壳 + Shizuku)               │
│    └─ 加载 http://127.0.0.1:8800 (control_server)  │
│  Termux (运行时：node + python)                     │
│    ├─ control_server.py  控制台 UI + API :8800      │
│    ├─ daemon.py          守护 bridge.py             │
│    ├─ bridge.py          微信 ClawBot <-> Claude    │
│    └─ claude CLI         npm 全局安装 (node)        │
│  Termux:Widget   桌面快捷按钮（启动/停止/状态/控制台）│
│  Termux:Boot     开机自启 + 唤醒锁                  │
│  Shizuku         提权：电池白名单/后台白名单/开微信   │
└───────────────────────────────────────────────────┘
```

- **Node 与 Python 环境**由 Termux 提供（`pkg install python nodejs-lts`），
  部署脚本一键装好；Claude Code CLI 通过 npm 安装。
- **UI** 即 `webui/index.html`：APK 内或任意手机浏览器打开
  `http://127.0.0.1:8800` 均可使用。

## 安装步骤

1. **安装应用**（F-Droid 版，别用 Play 商店的旧版）：
   - Termux、Termux:Widget、Termux:Boot（后两个可选但推荐）
   - [Shizuku](https://shizuku.rikka.app/)（可选，提权用）
2. **导入仓库并部署**：把本仓库放进手机（`git clone` 或 U 盘拷贝），
   Termux 里执行：
   ```bash
   cd ~ && bash /path/to/repo/android/setup_termux.sh [git远程地址]
   ```
   脚本会装依赖、装 claude CLI、注册快捷方式与自启、拉起控制台。
3. **首次微信登录**（需要看二维码，只能在 Termux 终端里做一次）：
   ```bash
   cd ~/weclaude && python bridge.py --login
   ```
4. **Shizuku**（可选）：启动 Shizuku APP（无线调试配对）→
   Shizuku 里「在终端应用中使用 rish」导出到 Termux，放进 `$PREFIX/bin`；
   APK 的 Shizuku 按钮即可用。
5. **APK**：push 后 GitHub Actions 自动构建（`android-apk` workflow），
   在 Actions 页下载 `Kami-debug-apk` 安装；或本地
   `gradle assembleDebug -p android/app`。不想装 APK 的话，浏览器开
   `http://127.0.0.1:8800` 就是同样的控制台。

## 安全模型

- 控制台所有写操作都要令牌（首次启动生成于
  `~/.config/kami/control_token`）。
- 任意 shell 仅限本机回环（自己手机上操作自己）；局域网其他设备
  只能看状态。
- Shizuku 权限等价 adb shell（非 root），且逐条由你在 UI 上点出来。

## 已知限制

- llama.cpp 本地快模型手机端默认不开（APK 构建 gguf 体积大、发热明显）；
  auto 路由会发现本地模型不在线，全部消息回落 Claude，功能不受影响。
- 微信 ClawBot 要求微信保持在线；配合 Shizuku 的电池/后台白名单和
  唤醒锁，Termux 的存活率会高很多。
- 首次扫码登录必须在 Termux 终端（QR 需要真实终端显示）。
