# 元宝每日任务

这是一个使用本地视觉大模型，通过 Android 截图、无障碍层级和 ADB 操作腾讯元宝的自动化示例。模型每轮只提出一个白名单动作；Python 编排器负责阶段、前置条件、重试、任务计数、兑换证据和完成断言。

每日任务流程采用“首次判断、确定性执行”的策略：新的一天首次读取任务进度时由完整无障碍层级直接解析，层级不完整或语义不明确时才请求视觉模型；输入、发送、等待生成、选图、领奖、返回、兑换和完成断言等确定性动作由执行器完成。子任务完成并通过生成/奖励后置条件后，返回福利中心时由本地编排器递增计数，不再为每一次重复任务重新请求同一张页面的视觉判断；同一轮内重复任务还会缓存已验证入口坐标，并在页面语义或区域校验失败时自动回退视觉定位。这样减少同质化视觉请求，同时保留首次进度、未知入口、推荐模板选择和异常页面的模型判断。本地动作遇到 WebView 重绘或输入法瞬态失败时允许有限重试，视觉坐标点击仍会拦截同画面重复动作。

同一次 `run_once`（通常是当天的一轮任务）会保留压缩后的观测、动作和结果历史；通用原型也采用同样的回合内历史布局。两条路径都只发送最新截图，不把历史图片或账号信息写入请求上下文，并保持静态提示和工具定义不变，以便支持前缀缓存的模型复用更长上文。

状态文件只保存当天的任务计数、能力站每日大任务完成标志和三天卡兑换状态。普通视觉报告按同日单调合并；新版福利页只有在完整每日任务目录及三项计数均可由当前页面层级核实时，才允许用页面实数纠正旧状态。无障碍树为空时，必须先滚动到任务区，再依据最新截图报告，避免沿用过期进度或把推荐卡当成每日任务。服务重启可从中断任务继续。

## 模型介入边界

视觉模型只负责“看得见才能决定”的部分，其余全部交给本地代码，避免用概率性判断覆盖可确定的证据：

| 场景 | 决策方 | 理由 |
| --- | --- | --- |
| 福利页任务计数 `x/3` | 本地无障碍树 | 计数是明确文本节点，模型读截图反而更差 |
| 主轮播卡“去体验/已完成” | 本地固定区域 | 二元状态固定在同一位置 |
| 收尾快路径是否成立 | 本地（以福利页计数为准） | 本地状态可能把没做的任务记成已完成 |
| 语音输入控件定位 | 本地语义定位 | 控件位置多变，由模型猜坐标会误长按 |
| 推荐模板卡片选择 | 视觉模型 | 卡片数量与位置每天变化 |
| 任务入口、抽屉、兑换入口、活动页 | 视觉模型 | 页面改版频繁，固定坐标必然过期 |
| 卡住恢复 | 视觉模型自由决策 | 异常页面无法穷举，需要模型看截图换策略 |
| 积分与商品价格 | 本地可读时本地优先 | 兑换分支容不得读数错误 |

新增专用工具 `hold_to_speak`：模型只表达“按住说话”的意图，程序按“按住说话”语义定位控件、按住期间通过 `pactl` 临时把一段 1.7 秒测试语音（`语音测试.wav`）注入默认麦克风，结束后恢复原音源，避免模型在多变布局上猜错长按对象。

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
MODEL_ID=gpt-6-luna
BASE_URL=https://www.cctq.ai/v1
LOCAL_LLM_API_KEY=请填入兼容端点密钥
REASONING_EFFORT=max
ANDROID_DEVICE=auto
STOP_WAYDROID_AFTER_RUN=true
# 仅精确确认元宝首次协议页；登录、验证码和微信/QQ 扫码仍需人工完成
AUTO_ACCEPT_PROTOCOL=true
MODEL_REQUEST_TIMEOUT_SECONDS=180
TEST_IMAGE_PATH=测试题目.png
PROMPT_PATH=每日任务提示词.md
STATE_PATH=.yuanbao_daily_state.json
```

每日任务的目标次数按福利页实际口径执行：写作、拍题、P图和做同款各 3 次，趣问元宝卡片的进度是 `(x/4)`，需要问满 4 次才能领到积分；福利页的计数是唯一权威来源，本地状态不会把没做的任务记成已完成，收尾快路径也必须先由页面计数确认。

新版大任务只点击元宝能力站主轮播卡固定位置显示的“去体验”，不识别卡片标题或颜色；点击后由视觉模型处理卡片对应的体验流程，回福利中心确认主卡明确显示“已完成”才记为完成。启用 `FORCE_ALL_DAILY_TASKS=true` 时，趣问元宝、写作、拍题、P图、做同款五类业务都必须执行到目标进度，不能因一次失败、空树或暂时缺少进度文案而跳过；多变入口和模板仍由视觉模型按最新截图决策。新版聊天页从左上角抽屉进入福利中心；兑换只定位 QQ 超级会员 3 天卡，先读取当前积分和商品价格，积分不足则不扣分并安全完成本轮。任务完成后的导航同时兼容新版聊天首页和旧版“我们”页。

Windows PowerShell 或其他 ADB 主机只需把 `ANDROID_DEVICE` 改成在线设备序列号，使用对应的 Python 虚拟环境运行同一个脚本；不需要安装 Waydroid。设备必须已经安装 `com.tencent.hunyuan.app.chat` 并完成 ADB 授权。

## Waydroid 窗口尺寸

元宝按 `sw600dp` 布局（窗口宽 1012 像素），显示区宽度不足时右侧会被裁掉——
福利页的“兑换商城”、模板卡片的“做同款”按钮和聊天页最右侧的模式入口都会缺失，
视觉模型因此看不到入口。当前配置让显示区正好容纳应用宽度：

```bash
waydroid prop set persist.waydroid.width 720      # 720 × 1.5 = 1080 像素宽
waydroid prop set persist.waydroid.height 1067    # 1067 × 1.5 = 1600 像素高
waydroid prop set persist.waydroid.multi_windows false   # 应用铺满显示区
waydroid session stop && waydroid session start
```

校验方式：`adb shell wm size` 应输出 `Physical size: 1080x1600`，
`dumpsys activity top` 里元宝窗口的 `mBounds` 宽度不应超过显示区宽度。

## 定时运行

`yuanbao-daily.service` 和 `yuanbao-daily.timer` 是 Linux 用户级 systemd 示例，默认按 `Asia/Shanghai` 每天 00:05 运行。服务单元按 `~/auto-daily` 和项目内 `.venv` 编写；如果仓库放在其他目录，请在复制前修改单元中的三处路径。普通失败会自动重试最多 5 次；启动时的 Android 系统兼容性提示和元宝协议页会在精确匹配后自动确认，登录、验证码、扫码或 ANR 等人工阻塞以退出码 75 结束，不会重复拉起同一页面。脚本内部达到最大重试次数时以退出码 76 结束，并弹出置顶 Tkinter 失败报告窗口，展示北京时间、阶段、任务、最近动作、状态文件和完整堆栈，必须手动关闭；`RestartPreventExitStatus=75 76` 会阻止窗口关闭后被 systemd 立即重复拉起。报告会隐藏 API 密钥、手机号和令牌。服务启动超时设为无限，保证窗口可以一直等待人工处理。将 `AUTO_ACCEPT_PROTOCOL=false` 可关闭协议自动确认。`ANDROID_DEVICE=auto` 时任务成功后停止 Waydroid；显式 ADB 设备模式不会关闭外部模拟器或真机。

```bash
mkdir -p ~/.config/systemd/user
cp yuanbao-daily.service yuanbao-daily.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now yuanbao-daily.timer
```

Waydroid 路线还可以安装 `ops/` 中的 root 级恢复配置。它会在容器管理器异常退出时由 systemd 拉起，每 10 分钟检查空闲实例的文件句柄，并在每天 23:50、仅当 Android 容器未运行时重启一次管理器，避免句柄泄漏影响次日 00:05 冷启动：

```bash
sudo install -Dm755 ops/waydroid-container-healthcheck.sh /usr/local/sbin/waydroid-container-healthcheck
sudo install -Dm644 ops/waydroid-container.service.d/recovery.conf /etc/systemd/system/waydroid-container.service.d/recovery.conf
sudo install -Dm644 ops/waydroid-container-watchdog.service ops/waydroid-container-daily-reset.service /etc/systemd/system/
sudo install -Dm644 ops/waydroid-container-watchdog.timer ops/waydroid-container-daily-reset.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now waydroid-container-watchdog.timer waydroid-container-daily-reset.timer
mkdir -p ~/.config/systemd/user/yuanbao-daily.service.d
install -Dm644 ops/yuanbao-daily-recovery.conf ~/.config/systemd/user/yuanbao-daily.service.d/recovery.conf
systemctl --user daemon-reload
```

健康检查阈值默认为空闲管理器 800 个文件句柄；可通过 `WAYDROID_FD_THRESHOLD` 调整。检查脚本不会在 `Container: RUNNING` 时重启实例。

## 文档索引

- [从零安装与设备接入](docs/从零安装与设备接入.md)：面向新开发者的迁移教程。
- [Waydroid 安装、网络与登录排查记录](元宝_Waydroid_安装网络与登录问题排查记录.md)：本机 Ubuntu KDE/Wayland 的问题证据和时间线，属于案例记录。
- [视觉安卓代理调研](视觉安卓代理调研.md)：操控方案比较、视觉代理边界和后续演进方向。
- [每日任务提示词](每日任务提示词.md)：模型工具协议和元宝任务流程。
- [请求前缀缓存与上下文](docs/请求前缀缓存与上下文.md)：同轮历史消息、请求抓包和 token 前缀验证。

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
