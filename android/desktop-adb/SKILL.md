---
name: android-control
description: Control the user's Android phone over ADB from Claude Code - screenshots, taps, text input, app management, file push/pull, logcat. Use when the user asks to operate, check, debug or automate anything on their phone, e.g. "帮我看下手机屏幕", "在手机上打开微信", "点一下那个按钮", "手机装个apk", "看下手机日志".
---

# Android control via ADB

The phone connects over network ADB (wireless debugging). Everything is one
`adb` invocation away - no MCP server needed.

## Connecting

```bash
adb devices                                  # already connected?
adb connect <IP>:<PORT>                      # port shows in 开发者选项-无线调试
adb mdns services                            # discover if port changed (Android 11+)
```

The wireless-debugging port changes every session. If `connect` times out,
ask the user for the new IP:port from 开发者选项 → 无线调试. Pairing (first
time only) uses "使用配对码配对设备": `adb pair <IP>:<PAIR_PORT> <code>`.

Multi-device: always pass `-s <IP:PORT>` when more than one device is attached.

## The core loop: look → decide → act

```bash
adb -s S exec-out screencap -p > /tmp/phone.png     # screenshot
```

Then **Read /tmp/phone.png** - screenshots render as images you can see.
Decide what to tap, then:

```bash
adb -s S shell input tap <x> <y>            # tap
adb -s S shell input swipe x1 y1 x2 y2 300  # swipe (300ms)
adb -s S shell input keyevent 4             # BACK (3=HOME, 26=POWER)
adb -s S shell input text hello             # ASCII text only
```

Repeat screenshot after each action to verify the result. Never chain more
than one blind action - always look between steps.

## Precise element coordinates

When a screenshot is ambiguous, dump the view hierarchy:

```bash
adb -s S shell uiautomator dump /sdcard/ui.xml
adb -s S pull /sdcard/ui.xml /tmp/ui.xml
```

Grep `/tmp/ui.xml` for `text="..."` / `resource-id="..."` and use the
`bounds="[x1,y1][x2,y2]"` center point for `input tap`.

## Text input (Chinese)

`input text` cannot send non-ASCII. Options: use the clipboard route
(`adb shell am broadcast -a clipper.set -e text '中文'` needs the Clipper
app), or an ADBKeyboard build:

```bash
adb -s S shell ime set com.android.adbk/.ADBKeyboard   # enable first
adb -s S shell am broadcast -a ADB_INPUT_TEXT --es msg '你好'
```

Fallback: focus the field, then `input keyevent` each keycode for ASCII.

## Apps & packages

```bash
adb -s S shell pm list packages | grep -i wechat
adb -s S shell monkey -p com.tencent.mm -c android.intent.category.LAUNCHER 1   # launch app
adb -s S shell am force-stop com.tencent.mm
adb -s S install -r app.apk                  # install/replace
adb -s S shell dumpsys package com.x | grep versionName
```

## Files

```bash
adb -s S push local.zip /sdcard/Download/
adb -s S pull /sdcard/DCIM/Camera/xxx.jpg ./
```

## Diagnostics

```bash
adb -s S shell getprop ro.build.version.release
adb -s S shell dumpsys battery               # level, charging state
adb -s S logcat -d -t 200                    # recent log buffer (-d = dump & exit)
adb -s S shell dumpsys activity top | head -40
```

## Safety

- ADB is full phone control. Only connect to the user's own devices, and
  confirm before `uninstall`, `pm clear`, factory-reset-ish or destructive
  shell commands.
- `adb disconnect` when done to drop the tunnel.
