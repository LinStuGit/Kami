# Kami Reporter — 独立 IP 上报 APK

不依赖 Termux 的独立小 app：手机**网络一变化就向 PC 上报新 IP**，
PC 端（`control_server.py` 的 `/api/adb/report`）随即 `adb connect`，
从此 adb 无线连接不再需要 Termux 里的 `adb-report.sh` 常驻循环。

## 行为

- `ConnectivityManager.NetworkCallback` 监听网络变化（3 秒防抖），
  变化即上报当前 Wi-Fi IPv4 + 无线调试端口
- 5 分钟心跳兜底（PC 离线错过的变化会被补报）
- 上报失败按 10s→30s→60s→5min 退避重试
- 开机自启（`BOOT_COMPLETED`），前台服务常驻通知栏
- 只报 `wlan*` 接口的 IPv4（移动网络下 PC 本来就不可达，不污染端点文件）

## Shizuku 加固（推荐，服务长期有效）

授权 Shizuku 后服务在下次心跳自动执行（shell uid，无需 root）：

| 命令 | 作用 |
| --- | --- |
| `dumpsys deviceidle whitelist +<pkg>` | 加入电池优化白名单，Doze 不杀 |
| `cmd appops set <pkg> RUN_ANY_IN_BACKGROUND allow` | 解除后台限制 |
| `cmd appops set <pkg> RUN_IN_BACKGROUND allow` | 同上（旧版本） |
| `settings put global adb_wifi_enabled 1` | 无线调试被关时自动重新打开 |

没有 Shizuku 时可用 app 内「电池优化豁免」按钮兜底。

## 安装

1. GitHub Actions 构建：`android-apk` 工作流的 `Kami-ipreport-apk`
   artifact；本地 `gradle assembleDebug -p ipreport`
2. 安装后打开 app：
   - **PC 地址**：预填 `http://59.66.31.61:8800`，按实际改
   - **Token**：PC 上 `cat ~/.config/kami/control_token` 复制过来
   - 点「保存并启动」→「电池优化豁免」
3. 有 Shizuku 的话：先启动 Shizuku APP，再点「授权 Shizuku」

上报成功后通知栏显示 `已上报 ip:port`，PC 端 `adb devices` 出现手机。

## 服务端契约

`POST /api/adb/report`（`X-Token` 或 `?t=` 鉴权）：

```json
{"ip": "192.168.1.23", "port": "39921"}
```

`port` 可为空字符串（无线调试关闭时）：PC 只更新
`~/.config/kami/adb_endpoint.json`，由 adb 插件看门狗扫描端口。
