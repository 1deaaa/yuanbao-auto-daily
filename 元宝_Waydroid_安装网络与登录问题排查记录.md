# 腾讯元宝 Waydroid 安装、网络与登录问题排查记录

> 记录目的：保存本次在 Ubuntu KDE 6、纯 Wayland 主机上安装和调通腾讯元宝的完整过程，便于后续复现、维护和自动化测试环境迁移。
>
> 安全说明：本文已脱敏，不包含 sudo 密码、手机号、短信验证码、二维码内容、Cookie、Token、Clash 节点密码、UUID 或订阅内容。
>
> 使用说明：这是本机的证据和时间线，不是可直接复制的安装脚本。文中的用户名、私有 IP、绝对路径、应用版本和 4 GiB 限制只代表本机；面向新设备的通用步骤请先阅读 [从零安装与设备接入](docs/从零安装与设备接入.md)。

## 1. 最终状态

- 容器：Waydroid，状态为 `RUNNING`，Vendor type 为 `MAINLINE`。
- Android：13。
- 设备地址：由 `waydroid status` 动态获取（本文用 `<WAYDROID_IP>:5555` 表示）。
- 设备型号伪装：小米 13 对应标识 `2211133C`，设备代号 `fuxi`。
- ABI：系统为 x86_64，同时声明 `arm64-v8a`、`armeabi-v7a`、`armeabi`，通过 `libhoudini.so` 提供 ARM 翻译。因此 ARM/ARM64 APK 通常可以尝试安装运行，但实际执行可能经过指令翻译；带有特殊原生库、反模拟器检测或依赖 Google/厂商硬件服务的应用仍需单独验证。
- 显示：`750x1333`，密度 `225`，适合在宿主机上以手机窗口使用，而不是占满 1440 高度的全屏窗口。
- 系统语言：`zh-CN`（简体中文）。
- 系统时区：`Asia/Shanghai`（北京时间，UTC+08:00）。
- 已安装应用：腾讯元宝 `com.tencent.hunyuan.app.chat`（本次运行版本名 `2.79.10`、版本号 `61784970`）、应用宝 `com.tencent.android.qqdownloader`、系统 WebView `com.android.webview` 等。
- 桌面入口：
  - `~/.local/share/applications/waydroid.com.tencent.hunyuan.app.chat.desktop`（元宝）
  - `~/.local/share/applications/waydroid.com.tencent.android.qqdownloader.desktop`（应用宝）
- ADB：必须显式指定 `adb -s <WAYDROID_IP>:5555`。列表中的 `emulator-5554` 为离线无关设备，不能使用无目标 ADB 命令。

## 1.1 全流程时间线

以下时间均为本机/Android 日志时间，部分用户操作时间只能按日志前后关系定位：

| 时间 | 事件 | 结论或处理 |
| --- | --- | --- |
| 8 月 25 日 22:36 左右 | `android.hidl.allocator@1.0-service` 在 Waydroid 容器内以 `SIGABRT` 生成 coredump | 这是容器启动阶段的系统服务故障，不等同于元宝登录故障，也没有证据表明它是 OOM |
| 8 月 25 日晚间 | 处理 Waydroid 启动、ADB 离线和设备兼容性；最终使用 `anbox-binder`/`anbox-vndbinder`/`anbox-hwbinder`、AIDL3 协议，并打开 `auto_adb` | 容器恢复运行，ADB 通过容器 IP 连接；宿主机 KDE/Clash 全局设置未因该步骤改变 |
| 8 月 25 日 23:20 至 8 月 26 日 00:18 | 尝试调整 TBS 配置、X5 多进程/沙箱开关 | 每次尝试都保留了备份，但这些开关不是最终稳定解法 |
| 8 月 26 日 00:57 左右 | 重新生成 Waydroid 运行属性，确认小米机型、ARM 翻译和 ADB 配置 | 形成当前实例的设备基线 |
| 8 月 26 日 01:50 至 01:52 | 元宝登录页出现 X5 `privileged_process` 启动后 `SIGSYS`，主进程反复 `Recovering` | 定位到 X5/Waydroid 多进程兼容性，而不是单纯网络不通 |
| 8 月 26 日 02:08 至 02:18 | 尝试 `setting_forceUseSystemWebview=true`、`x5_disabled=true` | 元宝启动流程会覆盖 `tbs_extension.conf`，因此改动不持久；配置已备份 |
| 8 月 26 日 02:37 左右 | 写入元宝私有 `system_core_prefs.xml`，启用 `system_core_enabled=true` | 让元宝选择系统 WebView；这是最终有效的关键操作 |
| 8 月 26 日 03:03 至 03:07 | 日志显示 `com.android.webview` 启动，元宝依次进入登录页、WebActivity、绑定手机号页并回到 `YBHomeActivityV2` | 不再出现 X5 `SIGSYS` 循环；用户随后完成登录并确认成功 |
| 8 月 26 日 11:03 | 抓取最终 logcat 作为验证证据 | 后续日志仍未发现新的 X5 `privileged_process`/`SIGSYS` 登录循环 |

## 2. 环境与安装背景

宿主机为 Ubuntu 26.04 LTS、KDE 6、纯 Wayland，CPU 为 AMD Ryzen 7 4800H，使用核显。选择 Waydroid 的原因是它直接使用 Linux 容器和宿主图形栈，启动开销较低，并能通过 Houdini 兼容层运行 ARM/ARM64 Android 包。

安装和调试期间完成了以下基础工作：

1. 初始化并启动 Waydroid 容器与会话。
2. 开启可用的 ADB 调试入口，并确认容器 IP。
3. 调整为手机尺寸窗口，避免默认全屏影响操作。
4. 将设备属性伪装为常见小米机型，减少国内应用对设备环境的兼容性问题。
5. 设置简体中文和北京时间。
6. 安装应用宝，并保留 Waydroid 自动生成的桌面快捷方式。
7. 安装并启动腾讯元宝，包名为 `com.tencent.hunyuan.app.chat`。

### 2.1 本次真正起作用的关键操作

1. 以 Waydroid 作为 Android 运行环境，并启用 ARM/ARM64 翻译能力，使目标 ARM 包能在 x86_64 主机上运行。
2. 将 Waydroid 设备属性设为小米 13（`2211133C`/`fuxi`），把显示改为 `750x1333` 手机尺寸。
3. 启用容器内 ADB，并始终使用带目标地址的 ADB 命令。
4. 设置 `zh-CN` 和 `Asia/Shanghai`，固定测试环境的系统行为。
5. 用 DNS、网关、HTTP 返回码和 Clash 运行时规则逐层验证网络，而不是仅凭应用页面的错误文案判断。
6. 从 logcat 观察到腾讯 X5 `privileged_process` 的 `SIGSYS` 重启循环后，转向检查元宝的 WebView 选择逻辑。
7. 通过反编译确认 `system_core_enabled` 开关，向元宝私有 SharedPreferences 写入该值，让元宝使用系统 WebView。
8. 用修复前后进程类型、WebView provider、Activity 跳转和用户实际登录结果进行闭环验证。

### 2.2 Waydroid 最初无法启动：容器系统服务与 ADB

启动早期曾出现：

```text
android.hidl.allocator@1.0-service
Signal: 6 (ABRT)
Control Group: /lxc.payload.waydroid
```

这对应 Waydroid 容器内的 HIDL allocator 服务 coredump。它发生在元宝登录问题之前，不能直接当作元宝崩溃，也没有证据显示这是宿主机内存耗尽。处理过程包括：

- 使用 Waydroid 可用的 `anbox-binder`、`anbox-vndbinder`、`anbox-hwbinder` 设备节点。
- 使用 AIDL3 binder/service-manager 协议。
- 打开 Waydroid 的 `auto_adb`，重启容器和会话后重新确认 ADB。
- 对容器 IP 变化和 `emulator-5554` 离线设备做显式区分。

当前配置文件为 `/var/lib/waydroid/waydroid.cfg`，相关临时对照文件保存在 `/tmp/waydroid.cfg.binder-fix` 和 `/tmp/waydroid.cfg.auto-adb-fix`。这些修改作用域是 Waydroid 实例本身，不是宿主机的全局网络配置。

关键设备属性来自当前实例：

```text
ro.product.brand=Xiaomi
ro.product.manufacturer=Xiaomi
ro.product.model=2211133C
ro.product.device=fuxi
ro.build.display.id=TKQ1.221114.001 V14.0.31.0.TMCCNXM
ro.dalvik.vm.native.bridge=libhoudini.so
ro.ndk_translation.version=0.2.3
```

## 3. 第一次网络故障：现象与排查

### 3.1 用户看到的现象

- 元宝首页（未登录状态）可以打开。
- 手机号登录点击下一步后长时间加载。
- 微信扫码二维码有时加载不出来。
- QQ 扫码能够成功，但最后仍卡在登录页面。
- 曾出现 `https://cdn-hybrid-prod.hunyuan.tencent.com/page/captcha.html` 的 `net::ERR_INTERNET_DISCONNECTED`。
- 验证码曾提示失效，容易误判为手机号或网络完全不可用。

### 3.2 宿主机与容器网络检查

检查了 Waydroid 的 DHCP、网关和 DNS。当前容器通过 Waydroid 私有网桥获取网络，实例地址由 DHCP 动态分配。同时检查 Mihomo/Clash Verge 的运行时配置：

- 配置文件：`~/.local/share/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml`
- TUN 接口：`Meta`
- fake-ip 网段：`198.18.0.0/16`
- `tencent.com`、`qq.com`、`qcloud.com` 等规则均为 `DIRECT`。
- 实际连接检查中，`quic.yuanbao.tencent.com`、`access1.tpns.tencent.com`、`otheve.beacon.qq.com` 分别按所属腾讯域名规则走 `DIRECT`。

宿主机和 Waydroid 内曾对元宝主页、验证码页面做 HTTP 访问，均能获得 HTTP 200。由此得到两个结论：

1. Waydroid 的基础网络、DNS 和中国大陆腾讯域名访问是可用的。
2. `Tun + DIRECT` 并不等于“没有网络”；TUN 负责接管流量，随后按规则直连是正常行为。不能只因为看到 TUN 就判定 Clash 拦截了腾讯域名。

因此，`ERR_INTERNET_DISCONNECTED` 是某个登录页面/渲染进程当时的网络状态表现，但不是“腾讯域名被错误送入代理”这一类规则问题。继续抓取应用进程日志后，故障方向发生了变化。

### 3.3 网络问题的最终处理结论

本次没有通过关闭 Clash TUN、把腾讯域名强制送代理、替换宿主机 DNS 或修改宿主机防火墙来“碰运气”解决。网络部分的有效操作是确认链路事实：Waydroid 能拿到 DHCP、网关和 DNS，腾讯主页/验证码页能返回 HTTP 200，Mihomo 对匹配到的腾讯域名执行 `DIRECT`。在此基础上，登录失败改由 WebView 进程日志继续定位；切换元宝到系统 WebView 后，验证码页、扫码回调和登录跳转恢复正常。

因此，网络问题的可复现解决方案是“保留现有 TUN + 规则直连，排除错误网络归因，再修复 X5 渲染兼容性”，而不是继续扩大网络配置改动范围。

## 4. 第二次故障定位：登录无限转圈的真实原因

### 4.1 原始日志证据

在原始登录日志 `/tmp/yb-login-logcat-0150.txt` 中，反复出现以下顺序：

```text
MultiProcess: Recovering https://yuanbao.tencent.com/e/login?...
ActivityManager: Start proc ... com.tencent.hunyuan.app.chat:privileged_process0/1/2
ActivityManager: Process ...:privileged_processN ... has died
ActivityManager: Scheduling restart of crashed service ... ChildProcessService$PrivilegedN
```

相关 X5 Chromium 子进程启动后很快退出，退出信号为 `SIGSYS (31, Bad system call)`。主进程并未直接崩溃，而是不断重新恢复登录 URL，所以界面表现为登录成功后无限转圈、返回登录页或无响应。

这解释了为什么：

- 未登录首页可以显示：它不一定触发相同的 X5 多进程路径。
- 验证码能够发送并接收：网络请求本身已经能走通。
- 手机号、微信、QQ 多种登录方式都可能在最后一步卡住：它们最终都要进入同一套 Web/X5 登录回调链路。
- 宿主机出现过 Chromium scope 被 OOM 杀死，并不能解释最初的稳定复现；原始登录阶段更直接的证据是 X5 子进程 `SIGSYS` 循环。最终修复也没有依赖扩大宿主机内存。

### 4.2 兼容性根因

腾讯元宝内置腾讯 X5/TBS WebView，并会启动多个 `privileged_process` Chromium 子进程。Waydroid 的容器/内核兼容层对该多进程调用路径不完全兼容，导致子进程触发非法系统调用（`SIGSYS`），主进程随后重复恢复登录页面。

通过反编译 TBS 代码确认：

- `/tmp/tbs-jadx/sources/com/tencent/tbs/tbsshell/WebCoreProxy.java` 中的 `canUseX5()` 会检查 `system_core_prefs.xml`。
- 偏好项 `system_core_enabled=true` 可使元宝选择系统 WebView 路径，而不是 X5 路径。

## 5. 最终修复：仅对元宝切换到系统 WebView

最终生效的改动是创建元宝私有 SharedPreferences 文件：

```text
$HOME/.local/share/waydroid/data/data/com.tencent.hunyuan.app.chat/shared_prefs/system_core_prefs.xml
```

文件内容（不含任何账号数据）：

```xml
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <boolean name="system_core_enabled" value="true" />
</map>
```

该文件设置为元宝应用 UID/GID `10127:10127`、模式 `660`，只影响元宝自己的数据目录，没有修改宿主机全局代理、Waydroid 持久系统属性或 Clash 配置。原有备份集中在：

```text
/var/lib/waydroid/compat-backups/
```

### 5.1 曾尝试但不是最终方案的改动

之前尝试在 TBS 配置中加入：

```text
setting_forceUseSystemWebview=true
x5_disabled=true
```

但元宝启动流程会重写/覆盖 `tbs_extension.conf`，因此该方式不稳定，不能作为最终修复依据。对应原始文件已保存在备份目录，便于审计。

### 5.2 修复后验证

修复后的日志 `/tmp/yb-final-logcat-20260826-110342.log` 显示：

- 加载的是 `com.android.webview 146.0.7680.153`。
- 出现 `WebViewFactory: Loading com.android.webview`。
- 不再出现新的腾讯 X5 `privileged_process0/1/2` + `SIGSYS` 循环。
- 元宝可以创建 `HYLoginMainActivity`、`HYPhoneLoginActivity`、`WebActivity`。
- 登录流程随后进入绑定手机号页，再回到 `YBHomeActivityV2`，用户确认已经登录成功。

最终日志中的典型顺序（时间为 Android 日志时间）：

```text
03:03:56  org.lineageos.jelly 启动系统 Chromium/WebView
03:03:56  元宝 HYPhoneLoginActivity 创建
03:04:38  元宝 WebActivity 创建
03:05:24  返回 HYLoginMainActivity / 登录相关页面
03:06:55  进入 BindPhoneNumActivity
03:07:04  再次创建 WebActivity
03:07:10  回到 YBHomeActivityV2
```

这些 Activity 切换是登录链路和绑定流程的正常证据；日志中的少量窗口焦点、缺失厂商 Provider、WebView sandbox cgroup 警告没有阻止登录。

## 6. 重要问题、误判与经验

| 现象 | 实际判断 | 处理方式 |
| --- | --- | --- |
| 验证码失效、验证码页报断网 | 需要区分页面瞬时网络状态与应用渲染进程故障 | 先检查 DNS、网关、HTTP 200 和 Clash 规则，再看应用进程日志 |
| 腾讯域名规则显示 `DIRECT` 但仍卡登录 | `DIRECT` 是 TUN 接管后的直连结果，不代表 TUN 本身错误 | 不反复修改 Clash 全局规则，避免引入新的网络变量 |
| 首页能打开，登录回调卡住 | 首页与登录回调使用的渲染路径可能不同 | 对比登录前后的 Activity、WebView 和子进程日志 |
| 元宝无响应 | 原始故障主要是 X5 子进程 `SIGSYS`，不是元宝主进程直接崩溃 | 改用系统 WebView |
| 宿主机 Chromium scope 被 OOM 杀掉 | 属于一次独立的内存压力事件，不能替代对 X5 崩溃证据的分析 | 记录为风险，后续自动化时限制并发、及时回收 WebView/截图进程 |
| ADB 偶尔提示设备离线 | Waydroid IP 会随容器重启变化，且存在无关的 `emulator-5554` | 每次先 `waydroid status`，再显式指定当前 `IP:5555` |

## 7. 复现与回滚参考

### 7.1 启动与检查

```bash
waydroid status
adb devices
adb -s <WAYDROID_IP>:5555 shell getprop ro.product.model
adb -s <WAYDROID_IP>:5555 shell wm size
adb -s <WAYDROID_IP>:5555 shell settings get system system_locales
adb -s <WAYDROID_IP>:5555 shell getprop persist.sys.timezone
waydroid app launch com.tencent.hunyuan.app.chat
```

如果容器重启后 IP 改变，把上面命令中的地址替换为 `waydroid status` 输出的新地址；不要对离线的 `emulator-5554` 执行无目标命令。

### 7.2 检查系统 WebView 是否仍被使用

```bash
adb -s <WAYDROID_IP>:5555 shell dumpsys webviewupdate
adb -s <WAYDROID_IP>:5555 logcat -d -s WebViewFactory ActivityManager MultiProcess
```

预期应看到 `com.android.webview`，且不应看到新的元宝 `privileged_process0/1/2` `SIGSYS` 循环。

### 7.3 回滚元宝私有开关

回滚前先停止元宝并保留现文件备份。由于该目录受应用 UID 保护，使用 root 权限执行：

```bash
sudo cp -a \
  "$HOME/.local/share/waydroid/data/data/com.tencent.hunyuan.app.chat/shared_prefs/system_core_prefs.xml" \
  "$HOME/.local/share/waydroid/data/data/com.tencent.hunyuan.app.chat/shared_prefs/system_core_prefs.xml.before-rollback"
sudo rm \
  "$HOME/.local/share/waydroid/data/data/com.tencent.hunyuan.app.chat/shared_prefs/system_core_prefs.xml"
```

然后重启 Waydroid 和元宝，确认是否恢复原始 TBS 行为。若要恢复最终修复，只需把备份文件放回原路径，并保持应用 UID/GID 与 `660` 权限。

## 8. 证据文件索引

以下文件保留在本机 `/tmp` 或 Waydroid 数据目录中，未将原始日志直接复制进本文，以避免把会话参数写入长期文档：

- `/tmp/yb-login-logcat-0150.txt`：修复前登录阶段，包含 X5 子进程退出/恢复循环。
- `/tmp/yb-before-force-system-webview-20260826-0210.txt`：修复前后尝试过程的日志。
- `/tmp/yb-system-core-full-20260826-0242.log`：写入系统 WebView 开关后的完整启动日志。
- `/tmp/yb-final-logcat-20260826-110342.log`：最终验证日志。
- `/tmp/yb-system-core-login.png`、`/tmp/yb-jelly-login.png`：登录页面截图。
- `/var/lib/waydroid/compat-backups/`：TBS/X5 相关尝试的配置备份。
- `/tmp/tbs-jadx/sources/com/tencent/tbs/tbsshell/WebCoreProxy.java`：确认 `system_core_enabled` 读取逻辑的反编译源码。

## 9. 后续自动化测试建议

后续开发“视觉 AI + 自然语言工作流 + Android 点击测试”程序时，建议把本次经验固化为启动前检查：

1. 自动发现 Waydroid 当前 IP，并固定 ADB 目标。
2. 检查屏幕尺寸、语言、时区和设备型号是否符合测试基线。
3. 检查 WebView provider，优先确认被测应用没有在容器中启动已知不兼容的 X5 多进程。
4. 每次测试前保存 `logcat` 起始标记，按应用包名过滤，单独记录 Activity、WebView、网络和进程退出事件。
5. 限制截图/浏览器并发，测试结束后回收无用 Activity，避免宿主机内存压力再次杀掉 Chromium scope。
6. 继续保留“网络层验证”和“渲染/进程层验证”两条独立证据链，避免把所有登录失败都归因于代理或 DNS。

## 10. 元宝每日任务自动化落地与实测

### 10.1 实现内容

- `元宝每日任务.py` 使用本地 OpenAI 兼容视觉端点，接收当前截图和精简无障碍层级，每轮只允许一个白名单工具。
- 首步固定进入“我们”页的福利中心；模型只报告四个需要完成的任务，永久忽略“邀请新用户”和“使用推荐模板做同款”。
- 提问、写作使用预置英文测试主题；P 图和拍题使用本地脱敏图片。每次生成都必须调用 `wait_5s`，执行层会继续检查终止按钮。
- 每个子任务完成后先处理奖励弹窗，再回到福利中心重新报告进度；计数没有增加时不会假设成功。
- 兑换流程只允许“QQ超级会员1天卡”，要求明确的兑换成功证据、奖品记录、绑定账号使用成功证据，最后回到“我们”页才接受 `complete_task`。
- `.env` 解耦模型 ID、Base URL、密钥、运行时间、重试次数和冷却时间；每日用户级 systemd 定时器按 `Asia/Shanghai` 05:00 触发。
- `.yuanbao_daily_state.json` 只保存当天商品兑换状态，用于进程中断后的幂等恢复，不保存账号、令牌、验证码或截图。

### 10.2 实测中发现并修复的问题

| 现象 | 根因 | 修复 |
| --- | --- | --- |
| 写作已生成但工具报告输入失败 | 压缩后的无障碍文本被截断，不能用于输入确认 | 改为读取完整节点文本，并禁止未确认输入时发送 |
| P 图模型在相册中连续点击 | 元宝实际使用 `RolePlayPickerActivity`，且文件名节点不是可点击节点 | 识别该 Activity，按 `auto-daily-test.png` 同边界父控件选择，并禁止相册内任意模型点击 |
| 奖励弹窗无法找到“开心收下” | 部分版本只暴露 `content-desc=button` 和积分金额 | 增加图片语义弹窗识别并自动收下 |
| 商品列表“兑换”后被误判为兑换成功 | 列表按钮先进入商品详情；详情说明文字包含“奖品记录” | 只有“兑换成功/恭喜兑换成功”等明确证据才通过，详情页先点击“立即兑换” |
| 成功弹窗证据被漏掉 | 关键 `content-desc` 位于压缩层级末尾 | 兑换和奖励断言读取完整节点集合 |
| 服务中断后可能重复兑换 | 兑换状态只在进程内存中 | 写入按日期隔离的 `pending/used` 状态并在重启时恢复 |

### 10.3 2026-08-27 真实闭环证据

1. Waydroid `<WAYDROID_IP>:5555` 在线，最终元宝页面回到“我们”。
2. 福利中心最终观察到：问元宝 `3/3`、写作 `3/3`、P 图 `3/3`、拍题 `3/3`；两项永久忽略任务未被点击。
3. 兑换弹窗出现 `恭喜兑换成功` 和目标商品 `QQ超级会员1天卡`；奖品记录中显示 `奖励已绑定`、绑定账号和 `立即使用`。
4. 使用后奖品记录显示 `已使用`，执行层设置 `reward_use_confirmed=true`，写入当天幂等状态。
5. 随后真实运行日志显示：`return_to_ours` 成功，`complete_task` 成功，消息为“每日任务与奖品使用均已完成，本轮进入下一次等待”。

### 10.4 验证命令

```bash
python3 -m unittest -v test_元宝每日任务.py
python3 -m py_compile 元宝每日任务.py test_元宝每日任务.py
systemctl --user list-timers yuanbao-daily.timer
adb -s <WAYDROID_IP>:5555 get-state
```

本次验证结果为 17 项单元测试通过、Python 编译通过、定时器启用且下一次触发时间为北京时间次日 05:00。

## 11. 4GB 内存限制与每日任务生命周期

### 11.1 内存限制

Waydroid Android 进程位于独立的 LXC cgroup `/sys/fs/cgroup/lxc.payload.waydroid`，只给 `waydroid-container.service` 设置 systemd `MemoryMax` 不会限制 Android 进程。因此在 `/var/lib/waydroid/lxc/waydroid/config` 写入：

```text
lxc.cgroup2.memory.max = 4294967296
```

原配置备份为 `/var/lib/waydroid/lxc/waydroid/config.before-memory-limit-20260827`。重启后通过 `memory.max=4294967296` 确认上限生效；`memory.events` 的 `oom_kill` 为 0 时表示尚未发生 cgroup OOM。当前实例常驻内存接近 4GB，单独运行元宝基本可用，但同时运行应用宝或其他 Android 应用时余量偏紧。

### 11.2 每日任务自动启动和回收

`元宝每日任务.py` 现在由 `WaydroidRuntime` 管理一轮任务：

1. `ANDROID_DEVICE=auto` 时检查 `waydroid status`；会话停止则后台启动 `waydroid session start`。
2. 轮询当前容器 IP，使用 `IP:5555` 连接 ADB，确认设备状态为 `device` 后，再等待目标应用的 MAIN/LAUNCHER Activity 可解析，才启动元宝并开始视觉模型操作；这是冷启动时包管理器晚于 ADB 就绪的竞态保护。
3. 只有 `complete_task` 的全部任务、兑换和绑定使用断言成功后，才调用 `waydroid session stop`，并等待 `Session: STOPPED`；Waydroid 在没有用户会话时会省略 `Container` 行，若仍输出该行则必须也是 `Container: STOPPED`，随后才结束本轮，确保 Android payload 已退出并释放内存。
4. 模型或工具失败时不自动关闭现场，便于保留截图、日志和登录状态排查；下次运行会再次尝试启动/连接。

默认配置 `STOP_WAYDROID_AFTER_RUN=true`。如需调试期间保持图形界面，可临时设为 `false`；调试结束应恢复为 `true`。
