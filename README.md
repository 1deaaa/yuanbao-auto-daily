# 元宝每日任务

这是一个使用本地视觉大模型，通过 Android 截图、无障碍层级和 ADB 操作腾讯元宝的自动化示例。模型每轮只提出一个白名单动作；Python 编排器负责阶段、前置条件、重试、任务计数、兑换证据和完成断言。

[![测试](https://github.com/1deaaa/yuanbao-auto-daily/actions/workflows/test.yml/badge.svg)](https://github.com/1deaaa/yuanbao-auto-daily/actions/workflows/test.yml)

## 设备支持

Waydroid 不是上层代理的硬依赖。项目使用标准 ADB 设备接口：

| 配置 | 设备 | Waydroid 依赖 |
| --- | --- | --- |
| `ANDROID_DEVICE=auto` | Linux Wayland 上的 Waydroid | 自动启动/停止 Waydroid |
| `ANDROID_DEVICE=emulator-5554` | Android Studio、MuMu 等本地模拟器 | 无 |
| `ANDROID_DEVICE=<USB_SERIAL>` | 已授权 USB 真机 | 无 |
| `ANDROID_DEVICE=<主机>:5555` | ADB TCP 设备或远程模拟器 | 无 |

显式设备模式只调用 `adb -s <设备>`，并用 `adb shell am start` 启动应用，不执行任何 Waydroid 命令。当前上层任务规则针对腾讯元宝；设备适配层可以复用于其他 Android 应用，但其他应用需要自行编写页面阶段和成功断言。

## 快速开始

完整的跨主机教程见：[从零安装与设备接入](docs/从零安装与设备接入.md)。其中包含：

- Ubuntu Wayland 安装 Waydroid、初始化镜像和动态 ADB 连接；
- ARM/ARM64 APK 翻译层、分辨率、简中和北京时间设置；
- 可选的小米 13 设备属性伪装；
- 本次 HIDL allocator、X5/TBS `SIGSYS`、网络误判和 OOM 的诊断过程；
- Windows 模拟器、USB 真机和其他 ADB 设备的运行方式；
- systemd 定时器、验证清单、安全和许可证边界。

Linux/Waydroid 最小运行步骤：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
.venv/bin/python 元宝每日任务.py --once
```

编辑 `.env`：

```dotenv
MODEL_ID=gemini-3.7-flash
BASE_URL=http://localhost:7860/v1
LOCAL_LLM_API_KEY=请填入本地端点密钥
ANDROID_DEVICE=auto
STOP_WAYDROID_AFTER_RUN=true
TEST_IMAGE_PATH=测试题目.png
PROMPT_PATH=每日任务提示词.md
STATE_PATH=.yuanbao_daily_state.json
```

Windows PowerShell 或其他 ADB 主机只需把 `ANDROID_DEVICE` 改成在线设备序列号，使用对应的 Python 虚拟环境运行同一个脚本；不需要安装 Waydroid。设备必须已经安装 `com.tencent.hunyuan.app.chat` 并完成 ADB 授权。

## 定时运行

`yuanbao-daily.service` 和 `yuanbao-daily.timer` 是 Linux 用户级 systemd 示例，默认按 `Asia/Shanghai` 每天 05:00 运行。服务单元按 `~/auto-daily` 和项目内 `.venv` 编写；如果仓库放在其他目录，请在复制前修改单元中的三处路径。`ANDROID_DEVICE=auto` 时任务成功后停止 Waydroid；显式 ADB 设备模式不会关闭外部模拟器或真机。

```bash
mkdir -p ~/.config/systemd/user
cp yuanbao-daily.service yuanbao-daily.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now yuanbao-daily.timer
```

## 文档索引

- [从零安装与设备接入](docs/从零安装与设备接入.md)：面向新开发者的迁移教程。
- [Waydroid 安装、网络与登录排查记录](元宝_Waydroid_安装网络与登录问题排查记录.md)：本机 Ubuntu KDE/Wayland 的问题证据和时间线，属于案例记录。
- [视觉安卓代理调研](视觉安卓代理调研.md)：操控方案比较、视觉代理边界和后续演进方向。
- [每日任务提示词](每日任务提示词.md)：模型工具协议和元宝任务流程。

## 使用边界

本项目只适用于你拥有或明确获授权测试的 Android 设备、账号和应用。它不会读取或绕过短信验证码、二维码、Cookie 或访问令牌，也不会代替人工完成登录。腾讯元宝、Waydroid、Android 镜像、ARM 翻译库和第三方模拟器均由各自权利人维护；使用前请遵守相应服务条款、软件许可证和当地法律。公开问题报告中不要附带账号、验证码、登录截图或模型密钥。

## 开发检查

```bash
.venv/bin/python -m unittest -v test_元宝每日任务.py
.venv/bin/python -m py_compile 元宝每日任务.py 视觉安卓代理.py test_元宝每日任务.py
```

## 安全和许可提示

不要提交 `.env`、模型密钥、手机号、验证码、Cookie、Token、真实登录日志或含个人信息的截图。ARM 翻译库、GApps、目标 APK 和第三方模拟器由各自项目或厂商授权，本仓库不重新分发。设备伪装和应用私有 WebView 偏好只应用于你有权测试的设备和账号，并且可能随应用更新失效。

本项目代码采用 [MIT 许可证](LICENSE)。第三方组件和目标应用不包含在本许可证授权范围内。
