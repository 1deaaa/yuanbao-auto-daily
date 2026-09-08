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
- ABI：系统为 x86_64，同时声明 `arm64-v8a`、`armeabi-v7a`、`armeabi`，当前通过 `libndk_translation.so` 提供 ARM 翻译。因此 ARM/ARM64 APK 通常可以尝试安装运行，但实际执行可能经过指令翻译；带有特殊原生库、反模拟器检测或依赖 Google/厂商硬件服务的应用仍需单独验证。
- 显示：`750x1333`，密度 `225`，适合在宿主机上以手机窗口使用，而不是占满 1440 高度的全屏窗口。
- 系统语言：`zh-CN`（简体中文）。
- 系统时区：`Asia/Shanghai`（北京时间，UTC+08:00）。
- 已安装应用：腾讯元宝 `com.tencent.hunyuan.app.chat`（当前官方版本名 `2.83.10`、版本号 `63888094`）、应用宝 `com.tencent.android.qqdownloader`、系统 WebView `com.android.webview` 等。
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

宿主机为 Ubuntu 26.04 LTS、KDE 6、纯 Wayland，CPU 为 AMD Ryzen 7 4800H，使用核显。选择 Waydroid 的原因是它直接使用 Linux 容器和宿主图形栈，启动开销较低，并能通过 ARM 二进制翻译层运行 ARM/ARM64 Android 包；本机当前采用 `libndk_translation.so`。

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

### 3.4 登录成功后历史和消息不可用：`fwmarkd/netd` 兼容修复

用户完成登录后，元宝主页可以显示但历史无法加载、发送消息无响应。此时 Android 网络面板仍显示默认网络已验证，宿主机和容器也能访问腾讯 HTTPS；继续对 Clash/TUN 做改动没有解释力。对元宝 TIM SDK 的连接日志和 Waydroid `netd` 行为进行对照后，定位到 `fwmarkd` 请求格式兼容问题：元宝原生 SDK 的 `ON_CONNECT` 请求带有额外 36 字节目标地址，旧属性未启用时容器内 `netd` 只接受 16 字节，因长度不符返回 `EBADMSG`。这会让元宝把登录后的连接误判为 `ERR_CONNECTION_FAILED`，表现为历史和发送功能一起失效。

本机将以下持久属性设为 `true`，并在重启后确认生成文件和 Android 运行时属性一致：

```text
ro.vendor.redirect_socket_calls=true
```

涉及的属性源和生成文件包括：

```text
/var/lib/waydroid/waydroid.cfg
/var/lib/waydroid/waydroid_base.prop
/var/lib/waydroid/waydroid.prop
/var/lib/waydroid/rootfs/vendor/waydroid.prop
```

修改前保留了备份：

```text
/var/lib/waydroid/waydroid.cfg.before-fwmark-20260905-010649
/var/lib/waydroid/waydroid_base.prop.before-fwmark-20260905-011023
```

每次修改属性后都必须完整重启容器和 Android 会话，再用当前动态地址检查：

```bash
waydroid session stop
sudo waydroid container stop
sudo waydroid container start
waydroid session start
adb connect <WAYDROID_IP>:5555
adb -s <WAYDROID_IP>:5555 shell getprop ro.vendor.redirect_socket_calls
```

修复后的实际日志证据为：

```text
ro.vendor.redirect_socket_calls=true
Connect to im server successfully
Login success
synchronize server complete
receive heartbeat response
```

随后已通过元宝发送测试消息并收到回复；修复后未再出现新的 `9508`、`ERR_CONNECTION_FAILED` 或 `EBADMSG`。ICMP ping 丢包不能单独证明 Android 断网，验证应优先使用 TCP/HTTPS、Android `VALIDATED` 状态和元宝 TIM 心跳。该开关属于 Waydroid 与应用/系统版本的兼容性处理，重新初始化镜像或升级 Android 后应重新抓取日志确认。

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
- 首步固定进入“我们”页的福利中心；模型报告每日问元宝和五个需要完成的重复任务，仅永久忽略“邀请新用户”；“使用推荐模板做同款”按推荐模板详情页自动填充、发送、等待的流程执行。
- 提问、写作使用预置英文测试主题；P 图和拍题使用本地脱敏图片。每次生成都必须调用 `wait_5s`，执行层会继续检查终止按钮。
- 每个子任务完成后先处理奖励弹窗，再回到福利中心重新报告进度；计数没有增加时不会假设成功。
- 当前兑换流程只定位“QQ超级会员3天卡”；先读取当前积分和商品价格，积分不足则不扣分，返回“我们”页后接受 `complete_task`。正常兑换仍要求明确的兑换成功证据、奖品记录、绑定账号使用成功证据，最后回到“我们”页才接受 `complete_task`。
- `.env` 解耦模型 ID、Base URL、密钥、运行时间、重试次数、冷却时间和视觉请求超时；每日用户级 systemd 定时器当前按 `Asia/Shanghai` 00:05 触发。
- `.yuanbao_daily_state.json` 只保存当天三天卡兑换状态（包括兑换中、已使用或目标商品积分不足），用于进程中断后的幂等恢复，不保存账号、令牌、验证码或截图。

### 10.2 实测中发现并修复的问题

| 现象 | 根因 | 修复 |
| --- | --- | --- |
| 写作已生成但工具报告输入失败 | 压缩后的无障碍文本被截断，不能用于输入确认 | 改为读取完整节点文本，并禁止未确认输入时发送 |
| P 图模型在相册中连续点击 | 元宝实际使用 `RolePlayPickerActivity`，且文件名节点不是可点击节点 | 识别该 Activity，按 `auto-daily-test.png` 同边界父控件选择，并禁止相册内任意模型点击 |
| 奖励弹窗无法找到“开心收下” | 部分版本只暴露 `content-desc=button` 和积分金额 | 增加图片语义弹窗识别并自动收下 |
| 商品列表“兑换”后被误判为兑换成功 | 列表按钮先进入商品详情；详情说明文字包含“奖品记录” | 只有“兑换成功/恭喜兑换成功”等明确证据才通过，详情页先点击“立即兑换” |
| 成功弹窗证据被漏掉 | 关键 `content-desc` 位于压缩层级末尾 | 兑换和奖励断言读取完整节点集合 |
| 服务中断后可能重复兑换 | 兑换状态只在进程内存中 | 写入按日期隔离的 `pending/used` 状态并在重启时恢复 |
| 返回福利中心时 WebView 空层级 | 元宝的 `WebBrowserActivity` 偶发没有及时恢复任务页 | 执行层等待并重复入口点击；超时后重启元宝并重新导航，同时记录 Activity 和阶段 |
| 福利 WebView 空层级时“做同款”入口被连续拒绝 | 截图可见但无障碍树为空，执行层无法用节点语义校验视觉点击 | 仅在“做同款”福利入口且无障碍树为空时允许视觉模型点击；节点恢复后继续执行严格命中校验 |
| 视觉网关请求长时间无响应 | OpenAI 兼容客户端默认超时过长，任务会一直占用 Waydroid | `MODEL_REQUEST_TIMEOUT_SECONDS` 默认 90 秒，超时回到统一重试边界 |

### 10.3 2026-08-27 真实闭环证据

1. Waydroid `<WAYDROID_IP>:5555` 在线，最终元宝页面回到“我们”。
2. 福利中心最终观察到：问元宝 `3/3`、写作 `3/3`、P 图 `3/3`、拍题 `3/3`；该次旧版本实测尚未纳入“做同款”，因此只记录了当时被忽略的两项。当前版本已将“做同款”列为第五项任务。
3. 兑换弹窗出现 `恭喜兑换成功` 和目标商品 `QQ超级会员1天卡`；奖品记录中显示 `奖励已绑定`、绑定账号和 `立即使用`。
4. 使用后奖品记录显示 `已使用`，执行层设置 `reward_use_confirmed=true`，写入当天幂等状态。
5. 随后真实运行日志显示：`return_to_ours` 成功，`complete_task` 成功，消息为“每日任务与奖品使用均已完成，本轮进入下一次等待”。

### 10.4 2026-08-28 做同款流程与幂等恢复证据

1. 首次正式运行中，模型读取福利中心并将 `same_template` 从 `0/3` 完成到 `3/3`；执行层拒绝了一次不符合当前阶段的误点，模型随后自行修正。
2. 当天积分为 `16500`，商城没有 QQ 超级会员 1 天卡，3 天卡价格为 `30000`；因此未兑换商品，状态文件记录 `exchange_status=unavailable`。
3. 由于首次运行在兑换阶段被服务中断，重启后脚本重新启动 Waydroid，读取同日 `unavailable` 状态，不重复做同款或兑换，依次完成 `return_to_ours` 和 `complete_task`，服务退出码为 0。
4. 本次恢复运行结束后 `waydroid status` 为 `Session: STOPPED`，验证了“任务完成后关闭 Waydroid、下一轮再自动启动”的完整闭环。

### 10.5 验证命令

```bash
python3 -m unittest -v test_元宝每日任务.py
python3 -m py_compile 元宝每日任务.py test_元宝每日任务.py
systemctl --user list-timers yuanbao-daily.timer
adb -s <WAYDROID_IP>:5555 get-state
```

本次验证结果为 34 项单元测试通过、Python 编译通过、定时器启用且下一次触发时间为北京时间次日 00:05。

### 10.6 2026-09-01 定时任务未完成原因

1. 定时器在北京时间 `00:05:38` 正常触发，Waydroid 获得 `192.168.240.112:5555`，不是定时器、网络或容器启动失败。
2. 五次服务尝试均在固定首步 `open_welfare` 失败，错误为“福利中心没有加载出任务页面”；服务按 90 秒重试，`00:17:04` 达到 `StartLimitBurst=5` 后停止。
3. 重新做同版本冷启动时，元宝首先显示“元宝新版本”遮罩。遮罩的功能说明含“拍题”等文字，旧阶段识别器因此误判为拍题页；版本遮罩又遮住底部“我们”导航，固定导航等待结束后报福利中心未加载。
4. 执行层现在识别 `com.tencent.hunyuan.app.chat:id/upgrade_dialog` 和 `:id/skip`，自动点击跳过版本提示，并把该遮罩优先归类为应用阻塞层，避免再次被功能说明误判。
5. 另发现 Waydroid `Container: FROZEN` 仍可能保留 IP 和 ADB `device` 状态，导致 shell 命令超时。自动设备发现现在会先解冻，并等待 `sys.boot_completed=1` 与短 shell 探针成功后才进入元宝。

这次故障没有发现宿主机 OOM、腾讯网络不可用或 HIDL allocator 崩溃证据；直接诱因是元宝冷启动版本提示未被处理。

6. 当天下午从 systemd 链路补跑时，首轮在 45 分钟服务超时前完成了大部分任务，但仍停在拍题重试阶段；随后自动重试从福利中心实际计数继续，没有重复已完成任务。
7. 自动重试读到五项任务均为 `3/3` 后，模型曾连续三次点击错误位置；由于福利 WebView 当时无障碍树为空，执行层按原规则拒绝了这些视觉点击并停止本轮。加入“空树福利入口视觉点击”边界测试并重新启动后，`18:31:45` 读到五项全 `3/3`（同款计数已在此前异步到账，因此该次没有再次依赖空树入口点击），`18:33:13` 只点击 `QQ超级会员3天卡`，`18:34:10` 确认绑定账号使用成功，`18:35:06` 服务以退出码 0 完成并关闭 Waydroid。

### 10.7 当前兑换策略

从当前版本起兑换目标固定为 `QQ超级会员3天卡`。执行层只扫描该商品，读取当前积分和商品价格后再决定是否点击；积分不足会记录当天 `unavailable` 状态、返回“我们”页并完成任务，不会改兑 1 天卡或任何其它商品。历史实测中出现的 1 天卡记录属于旧策略，不代表当前行为。

### 10.8 2026-09-02 启动失败复现与修复验证

1. 当天 `00:05:38` 定时器正常触发，但服务连续五次在首步 `open_welfare` 失败，`00:21:23` 出现 `Start request repeated too quickly`。回看同版本冷启动截图和层级后确认，直接诱因是元宝“元宝新版本”更新遮罩：遮罩说明文字包含“拍题”，旧阶段识别器误判为拍题页，同时遮住底部“我们”入口。
2. 执行层现在在每次首屏观测中优先处理 Android 兼容性提示和元宝更新遮罩，识别 `upgrade_dialog`/`skip` 后自动跳过；阶段识别也把该遮罩固定归类为 `app`，不会再次被其中的功能说明误判为业务任务页。应用启动还会等待 Splash 窗口消失，并从干净的元宝进程开始。
3. `waydroid status` 偶尔会报告 `Session: RUNNING / Container: FROZEN`，普通用户执行 `waydroid container unfreeze` 会因 root 权限或 `/var/lib/waydroid/waydroid.log` 权限失败。运行时现在自动停止并重新启动用户会话，随后重新发现动态 ADB 地址；真实测试已确认从 `FROZEN` 恢复到 `RUNNING`，ADB `192.168.240.112:5555` 在线。
4. 已知元宝首次协议页不需要模型猜测：脚本默认只在 `HYLoginMainActivity` 中找到可点击的精确“同意并继续”时自动确认；按钮缺失或 `AUTO_ACCEPT_PROTOCOL=false` 时才保留人工阻塞。手机号、验证码、微信/QQ 扫码登录和 ANR 仍属于人工前置条件。
5. Android 系统兼容性提示也不需要模型猜测：启动阶段优先识别标题为“Android 系统”、类型为 `SYSTEM_ERROR` 且处于可见状态的系统窗口。无障碍树可用时点击精确的“确定”节点；无障碍树因容器冻结暂时为空时，从窗口边界计算右下按钮中心后点击，避免把该弹窗误交给模型。
6. 登录或 ANR 等人工阻塞由脚本提升为 `ManualActionRequired`，保留 Waydroid 现场并返回退出码 `75`；服务单元的 `RestartPreventExitStatus=75` 会阻止重复拉起同一阻塞页面。此前协议页人工停止的真实记录对应旧版本逻辑。
7. 另外修正会话所有权：任务首步失败时只停止本轮新启动的 Waydroid 会话，不会停止用户预先打开的会话；任务成功仍按 `STOP_WAYDROID_AFTER_RUN=true` 回收 Waydroid。
8. 本轮验证通过 47 项单元测试、Python 编译检查和 `git diff --check`；测试结束后请确认 `waydroid status`，下次定时触发时间为北京时间 `00:05`。

### 10.9 2026-09-03 最新 ARM64 版本与转译后端对照

1. 从腾讯应用宝官方详情页取得 `com.tencent.hunyuan.app.chat` 的 `2.83.10` 单 APK。该官方包只有 `lib/arm64-v8a`，没有 `x86` 或 `x86_64` 原生库；此前的第三方 XAPK 虽然同为 `2.83.10`，却带有 Google Play `PairIP LicenseActivity`，侧载到 VANILLA Waydroid 后会因缺少 `com.android.vending.licensing.ILicensingService` 反复授权失败。
2. 使用 `adb install -r` 覆盖安装官方包，版本更新为 `2.83.10`，应用数据目录 inode 未变，没有执行 `pm clear`。安装后需用 `pm enable --user 0 com.tencent.hunyuan.app.chat` 恢复用户启用状态；当前画面为手机/扫码登录页，登录仍需人工完成。
3. 在同一台机器、同一设备属性和同一 APK 下做冷启动对照：`libhoudini.so` 于 `21:16:39` 启动，`am start -W` 超时，随后持续出现 `TuringFD` 的 `Long monitor contention`，Splash 未正常退出。
4. 先备份 `/var/lib/waydroid/waydroid.cfg`、`waydroid_base.prop`、`waydroid.prop` 和 system 覆盖层，再按 Android 13 配置安装 `libndk_translation`。当前生效属性为 `ro.dalvik.vm.native.bridge=libndk_translation.so`，版本为 `0.2.3`。
5. 切换后于 `21:24:01` 冷启动同一官方包，日志显示 `Initialized NDK translation (aarch64), version 0.2.3`；`am start -W` 返回 `Status: ok`、`LaunchState: COLD`、`TotalTime: 1937`，并记录 `Displayed ... HYLoginMainActivity: +1s937ms`。手工关闭一次 Android 系统兼容性提示后，截图和无障碍层级均显示完整登录页，未再出现 Houdini 下的启动锁死。

结论：在本机 AMD x86_64 Waydroid 上，元宝最新官方 ARM64 包可以通过 `libndk_translation` 完成稳定冷启动；当前推荐后端改为 `libndk_translation`，而不是 `libhoudini`。这只证明启动和登录页兼容，登录后的 WebView、任务页面和后续原生能力仍需在人工登录后继续验证。转译层和应用版本升级都可能改变结果，升级前应保留配置备份。

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

### 11.3 Waydroid 常驻服务文件句柄耗尽

2026-08-28 11:54 的一次冷启动在脚本逻辑之前失败，日志为 `OSError: [Errno 24] Too many open files`。排查发现 `/usr/bin/waydroid container start` 常驻进程自前一晚启动后一直未重启，约持有 1021 个文件描述符，软上限为 1024；其中大量是 `eventpoll`。这是 Waydroid 26.04 容器管理器在多次会话启动/停止后累积选择器句柄的已知形态，和元宝网络、ADB 或模型无关。

处理步骤：

1. 使用 `sudo systemctl restart waydroid-container.service` 清理旧的容器管理进程；确认新进程初始文件描述符很少且 Waydroid 为 `Session: STOPPED`。
2. 在 `/etc/systemd/system/waydroid-container.service.d/limits.conf` 写入以下 drop-in，并执行 `sudo systemctl daemon-reload`。这样即使未来再次出现句柄累积，也不会在第二次会话启动时立即触发 1024 上限：

```ini
[Service]
LimitNOFILE=524288
```

3. 重新运行 `systemctl --user start yuanbao-daily.service`，确认日志依次出现 `report_tasks`、状态恢复、`complete_task` 和 `Finished ... status=0/SUCCESS`；任务结束后 `waydroid status` 应为 `Session: STOPPED`。

该 drop-in 属于宿主机 Waydroid 配置，不在项目仓库内维护；升级 Waydroid 后若再次观察到文件句柄增长，应先检查该文件是否仍存在以及 `systemctl show waydroid-container.service -p LimitNOFILESoft` 是否为 `524288`。

### 11.4 每日定时启动的恢复层

仅有 `Persistent=true` 只能补跑关机或睡眠期间错过的触发，不能处理“任务服务启动后失败”。因此用户级 `yuanbao-daily.service` 增加 `Restart=on-failure`、90 秒间隔和 2 小时内最多 5 次启动；任务脚本在 Waydroid 冷启动拿不到 IP 时会清理本轮新建的半启动会话，使下一次重试从干净状态开始。

Waydroid root 服务同时使用 `ops/waydroid-container.service.d/recovery.conf` 的崩溃自动重启。`ops/waydroid-container-watchdog.timer` 每 10 分钟检查空闲容器管理器的文件句柄，默认达到 800 个才重启；`ops/waydroid-container-daily-reset.timer` 每天北京时间 23:50 只在 `Container` 非 RUNNING 时重启管理器，为次日 00:05 任务预先清理上一轮留下的句柄。两项检查都不会主动停止正在运行的 Android 容器。

这套机制可以自动恢复已知的启动竞态、Waydroid 管理器崩溃和句柄泄漏；不能保证上游应用改版、外网不可用、本地模型网关故障、宿主机掉电或整体 OOM 时任务一定成功。此类异常仍需查看 `journalctl --user -u yuanbao-daily.service` 和 `journalctl -u waydroid-container.service`。

### 11.5 2026-09-07 任务中途停止与请求精简

1. `00:05` 的首轮服务正常启动，前置任务可以执行；第一次中断发生在写作任务的 `input_test_prompt`。页面正在加载或 WebView 重绘时，固定文本没有及时出现在输入框，连续三次外层重试触发防循环熔断。systemd 随后按 `Restart=on-failure` 重试，最终因 `StartLimitBurst=5` 停止。日志没有内存不足、网络断开或 Waydroid HIDL 崩溃证据。
2. 补跑时发现第二个独立问题：拍题入口有时先进入相机预览，原有图片选择器判断只接受相册页面，导致 `select_local_image` 连续失败。现在支持相册、图片、上传等入口语义、相机预览返回和有限备用坐标，并在进入选择器后只选脚本推送的脱敏图片。
3. 随后“做同款”任务曾因福利 WebView 空无障碍树拒绝模型坐标；增加了空树福利入口的有界放行、文字节点校正和推荐卡片父控件回退。最终补跑从已有计数恢复，`23:50:25` 读到五项任务全部 `3/3`，`23:51:07` 读取到三天卡需要 `30000` 积分而当前仅 `20000`，安全跳过兑换，`23:51:34` `complete_task` 成功，服务退出码为 0，随后 Waydroid 正常停止。

本轮同时把工作流收敛为“视觉定位一次、本地重复执行”的边界：

| 环节 | 执行方 | 仍保留的校验 |
| --- | --- | --- |
| 任务进度读取 | 视觉模型 | 福利中心截图和字段范围校验 |
| 每种任务首次入口定位 | 视觉模型 | 屏幕边界、忽略“邀请新用户”、阶段校验 |
| 同一任务第 2/3 次入口 | 本地坐标缓存 | 当前页面阶段、文字邻近或已知大卡片区域校验；失败立即清缓存并回退模型 |
| 推荐模板首次卡片定位 | 视觉模型 | `做同款`文字或可点击大卡片校验 |
| 推荐模板后续卡片 | 本地坐标缓存 | `app`阶段及文字/卡片区域校验 |
| 输入、发送、生成等待、选图、确认、领奖、返回、兑换和完成断言 | 本地执行器 | 每个动作都有前置状态和页面结果证据 |
| 登录、协议、验证码、扫码、未知异常 | 人工或视觉模型判断 | 不通过无意义重试掩盖人工阻塞 |

缓存只在单次进程中存在，不写入状态文件，因此不会把旧分辨率或旧版本布局带到下一天。固定任务子步骤集中在 `TASK_STEPS` 配方表中，输入类、图片类和做同款流程共用同一套阶段编排，减少散落的重复分支。

这项优化不改变必要的智能判断：模型仍负责“当前任务做到几次”“当前入口在哪里”“推荐模板卡片在哪里”和“页面是否出现未知状态”。在布局稳定的情况下，第二、三次重复任务及做同款卡片不再各自请求一次视觉模型；布局变化时会自动恢复原有模型路径。

### 11.6 2026-09-08 中途停止复盘与最终补跑

本次中断的直接原因不是宿主机内存、ADB 网络或 Waydroid 崩溃，而是工作流状态被一次视觉误读覆盖：

1. 福利截图实际显示“问元宝问题 3/3”和顶部“今日已完成”，模型报告却返回 daily_done=false；旧编排器于是重新进入已完成的每日问入口，固定坐标连续点击无效后触发三次失败熔断。
2. 另一次重试把已经确认的 3/3 报成 2/3。旧状态没有持久化，也没有同日单调合并，服务重启后会重新执行已完成任务。
3. 现在状态文件保存 date、daily_done、五项 task_progress 和三天卡 exchange_status。同一天的计数只取最大值，问题计数达到 3 时自动推导今日任务已完成；兑换写入会保留任务进度，服务重启从最近确认的任务继续。
4. 子任务的输入、发送、生成等待、选图、确认、奖励、返回福利中心和兑换均由本地执行器完成。返回福利中心且所有本地后置条件成功后，当前任务计数在本地递增，不再对同一张福利截图重复请求视觉模型；完整无障碍层级可直接解析五项计数时也跳过模型报告。
5. 元宝奖励遮罩有时在 claim_reward 返回数秒后才注入 WebView，且无障碍树为空。执行器现在等待领取，并在返回福利中心时再次检查；对 900x1600 手机布局使用亮绿色按钮像素探针，确认是奖励遮罩才点击，不会盲点任务行。
6. “做同款”任务行可能位于屏幕下方。执行器先向上滚动一次并清除旧坐标缓存，入口点击后必须离开福利页才推进阶段；滚动后本机已验证坐标可用，模板卡片和未知页面仍交给视觉模型。
7. 完整无障碍层级恢复后，普通任务入口按专属文案直接定位；只有层级为空、入口语义不明确或布局发生变化时才请求视觉模型。输入、选图、领奖、返回等本地幂等动作允许按 `MAX_RETRIES` 有界重试，视觉坐标点击仍严格拦截同画面重复，避免 WebView 瞬态重绘再次把任务提前熔断。

2026-09-08 实际补跑记录：从 问元宝问题=3/3、写作=0/3、P图=0/3、拍题=0/3、做同款=0/3 恢复；完成写作、P图、拍题和做同款各 3 次。任务奖励到账后积分为 31500，读取到 QQ 超级会员 3 天卡价格 30000，完成兑换、奖品记录和绑定账号使用确认；complete_task 成功，状态文件记录 exchange_status=used，随后 Waydroid 为 Session: STOPPED。

这次补跑说明需要保留的视觉判断只有：首次或异常时读取进度、定位当前任务入口、定位推荐模板卡片，以及判断未知页面。所有同质重复动作已经收敛到同一套本地阶段配方；页面布局变化时，入口语义校验失败会清除缓存并回退视觉模型。

验证结果：`py_compile` 通过，`test_元宝每日任务.py` 共 78 项通过，`git diff --check` 通过；补跑状态文件记录五项任务均为 `3/3`、三天卡已使用，Waydroid 为 `Session: STOPPED`。随后沿现有 systemd 链路做了终态恢复验证：读取当天终态、跳过兑换、返回“我们”、`complete_task` 成功，服务退出码为 0，Waydroid 再次停止。此前 00:05 首轮的失败记录仍保留在 `journalctl --user -u yuanbao-daily.service`，不能把它改写成首轮成功。
