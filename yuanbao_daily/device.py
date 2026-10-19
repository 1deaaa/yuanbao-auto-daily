"""ADB 设备通道与 Waydroid 会话生命周期管理。"""

from __future__ import annotations

import contextlib
from xml.etree import ElementTree as ET
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from .constants import (
    ADB_TRANSPORT_FAILURE_MARKERS,
    ANDROID_WARNING_CHECK_INTERVAL,
    ANDROID_WARNING_WINDOW_FRAME,
    ANDROID_WARNING_WINDOW_TITLE,
    ANDROID_WARNING_WINDOW_TYPE,
    APP_ANR_WINDOW_MARKERS,
    APP_READY_POLL_INTERVAL,
    APP_READY_TIMEOUT,
    APP_RESOLVE_POLL_INTERVAL,
    APP_RESOLVE_TIMEOUT,
    APPSTORE_PACKAGE_NAME,
    PACKAGE_NAME,
    UI_DUMP_ATTEMPTS,
    UI_DUMP_OPTIONS,
    UI_DUMP_READ_ATTEMPTS,
    UI_DUMP_READ_DELAY,
    UI_DUMP_RETRY_DELAY,
    UI_DUMP_TIMEOUT,
    WAYDROID_POLL_INTERVAL,
    WAYDROID_READY_TIMEOUT,
    WAYDROID_START_TIMEOUT,
    WAYDROID_STOP_TIMEOUT,
)
from .errors import (
    AgentError,
    AppUnresponsiveError,
    ManualActionRequired,
)
from .models import Bounds
from .ui import (
    find_node,
    parse_nodes,
)

def is_adb_transport_failure(error: BaseException) -> bool:
    """判断是否应丢弃本轮 Waydroid 会话并让 systemd 从干净实例重试。"""
    text = str(error).lower()
    return any(marker in text for marker in ADB_TRANSPORT_FAILURE_MARKERS)


class Device:
    """封装带明确 serial 的 ADB，绝不使用无目标 ADB。"""

    def __init__(self, serial: str):
        self.serial = serial

    @classmethod
    def discover(cls, configured: str) -> "Device":
        serial = configured.strip()
        if serial and serial.lower() != "auto":
            device = cls(serial)
            device.connect()
            return device
        try:
            status = subprocess.run(
                ["waydroid", "status"], capture_output=True, text=True, timeout=15, check=True
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"无法读取 Waydroid 状态：{type(exc).__name__}") from exc
        match = re.search(r"IP address:\s*(\d+\.\d+\.\d+\.\d+)", status)
        if not match:
            raise AgentError("Waydroid 没有提供容器 IP，请先启动容器")
        device = cls(f"{match.group(1)}:5555")
        device.connect()
        return device

    def connect(self) -> None:
        try:
            # USB 序列号和本地模拟器序列号已经由 ADB 管理；只有网络设备需要先执行 connect。
            if ":" in self.serial:
                subprocess.run(["adb", "connect", self.serial], capture_output=True, timeout=15, check=False)
            state = self.adb("get-state", timeout=15).decode("utf-8", errors="replace").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"ADB 连接失败：{type(exc).__name__}") from exc
        if state != "device":
            raise AgentError(f"ADB 设备状态不是 device：{state or '空'}")

    def wait_ready(
        self, timeout: float = WAYDROID_READY_TIMEOUT, poll_interval: float = WAYDROID_POLL_INTERVAL
    ) -> None:
        """等待 Android 完成启动，并确认 shell 不再只是表面在线。"""
        deadline = time.monotonic() + timeout
        last_error: AgentError | None = None
        while time.monotonic() < deadline:
            try:
                boot_completed = self.adb(
                    "shell", "getprop", "sys.boot_completed", timeout=5
                ).decode("utf-8", errors="replace").strip()
                if boot_completed == "1":
                    # 冻结的 Waydroid 也可能返回 get-state=device；再做一次短 shell 探针。
                    self.adb("shell", "echo", "auto-daily-ready", timeout=5)
                    return
            except AgentError as exc:
                last_error = exc
            time.sleep(poll_interval)
        detail = f"；{last_error}" if last_error else ""
        raise AgentError(f"Android 尚未完成启动或 ADB shell 无响应（等待 {timeout:.0f} 秒{detail}）")

    def wait_network_ready(
        self, timeout: float = 30.0, poll_interval: float = 1.0
    ) -> None:
        """等待 Android 默认网络真正可用，避免登录后把瞬态网络状态交给任务流。"""
        deadline = time.monotonic() + timeout
        last_detail = "未发现已验证的默认网络"
        while time.monotonic() < deadline:
            try:
                raw = self.adb("shell", "dumpsys", "connectivity", timeout=10)
                text = raw.decode("utf-8", errors="replace")
                active = re.search(
                    r"(?m)^\s*Active default network:\s*(\d+)\s*$", text
                )
                network_text = ""
                if active is not None:
                    # dumpsys 将活动网络编号和能力拆在 Current Networks 的
                    # NetworkAgentInfo 中，不能只检查“Active default network”一行。
                    network_id = active.group(1)
                    agent = re.search(
                        rf"(?ms)^\s*NetworkAgentInfo\{{network\{{{re.escape(network_id)}\}}.*?"
                        r"(?=^\s*NetworkAgentInfo\{|^\s*Inactivity Timers:|\Z)",
                        text,
                    )
                    if agent is not None:
                        network_text = agent.group(0)
                    else:
                        # 精简实现或测试端点可能把 Capabilities 直接放在活动网络段。
                        active_block = re.search(
                            r"(?ms)^\s*Active default network:.*?(?=^\s*Current network preferences:|\Z)",
                            text,
                        )
                        network_text = active_block.group(0) if active_block else active.group(0)
                else:
                    network_text = text
                if "VALIDATED" in network_text and "INTERNET" in network_text:
                    return
                if active is not None:
                    last_detail = active.group(0).strip()[:160]
            except AgentError as exc:
                last_detail = str(exc)[:160]
            time.sleep(poll_interval)
        raise AgentError(f"Android 默认网络未就绪（等待 {timeout:.0f} 秒；{last_detail}）")

    def adb(self, *args: str, timeout: float = 20, check: bool = True) -> bytes:
        command = ["adb", "-s", self.serial, *args]
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"ADB 超时：{args[0] if args else 'unknown'}") from exc
        except OSError as exc:
            raise AgentError(f"ADB 启动失败：{type(exc).__name__}") from exc
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise AgentError(f"ADB 命令失败：{args[0] if args else 'unknown'} {stderr[:180]}")
        return result.stdout

    def input(self, *args: str) -> None:
        self.adb("shell", "input", *args, timeout=15)

    def keyboard_shown(self) -> bool:
        """检测软键盘是否显示；查询失败时按未显示处理，避免误发返回键。"""
        try:
            output = self.adb(
                "shell", "dumpsys", "input_method", timeout=10
            ).decode("utf-8", errors="replace")
        except AgentError:
            return False
        return "mInputShown=true" in output

    def keep_awake(self) -> str:
        """临时阻止后台 Waydroid 因屏幕超时冻结，并返回原始设置。"""
        previous = self.adb(
            "shell", "settings", "get", "global", "stay_on_while_plugged_in", timeout=10
        ).decode("utf-8", errors="replace").strip()
        self.adb("shell", "svc", "power", "stayon", "true", timeout=10)
        return previous

    def restore_keep_awake(self, previous: str) -> None:
        """恢复 keep_awake 修改过的 Android 全局设置。"""
        self.adb("shell", "svc", "power", "stayon", "false", timeout=10)
        if previous in {"", "null", "None"}:
            self.adb("shell", "settings", "delete", "global", "stay_on_while_plugged_in", timeout=10)
        else:
            self.adb(
                "shell",
                "settings",
                "put",
                "global",
                "stay_on_while_plugged_in",
                previous,
                timeout=10,
            )

    def screenshot(self) -> bytes:
        image = self.adb("exec-out", "screencap", "-p", timeout=20)
        if not image.startswith(b"\x89PNG"):
            raise AgentError("ADB 截图不是有效 PNG")
        return image

    def ui_dump(
        self,
        *,
        attempts: int = UI_DUMP_ATTEMPTS,
        timeout: float = UI_DUMP_TIMEOUT,
    ) -> str:
        remote = "/sdcard/auto_daily_ui.xml"
        last_detail = "未知错误"
        attempts = max(1, int(attempts))
        timeout = max(1.0, float(timeout))
        for attempt in range(1, attempts + 1):
            try:
                # 先删除旧文件，避免 uiautomator 返回空根节点时误读上一轮层级。
                self.adb("shell", "rm", "-f", remote, timeout=5)
                dump_output = self.adb(
                    "shell",
                    "uiautomator",
                    "dump",
                    *UI_DUMP_OPTIONS,
                    remote,
                    timeout=timeout,
                ).decode("utf-8", errors="replace").strip()
                if "error:" in dump_output.lower() or "null root" in dump_output.lower():
                    last_detail = dump_output[:180] or "空根节点"
                else:
                    xml = ""
                    read_error: AgentError | None = None
                    # uiautomator 偶尔先返回“已写入”再真正创建文件，短暂重读即可恢复。
                    for read_attempt in range(UI_DUMP_READ_ATTEMPTS):
                        try:
                            xml = self.adb("shell", "cat", remote, timeout=10).decode(
                                "utf-8", errors="replace"
                            )
                            read_error = None
                            break
                        except AgentError as exc:
                            read_error = exc
                            if read_attempt + 1 < UI_DUMP_READ_ATTEMPTS:
                                time.sleep(UI_DUMP_READ_DELAY)
                    if read_error is not None:
                        raise read_error
                    try:
                        root = ET.fromstring(xml)
                    except ET.ParseError:
                        root = None
                    if root is not None and root.tag == "hierarchy":
                        return xml
                    last_detail = "返回内容不是有效 hierarchy"
            except AgentError as exc:
                last_detail = str(exc)[:180]
            if attempt < UI_DUMP_ATTEMPTS:
                time.sleep(UI_DUMP_RETRY_DELAY)
        raise AgentError(f"Android 无障碍树不可用（已重试 {attempts} 次）：{last_detail}")

    @staticmethod
    def _android_warning_bounds(windows: str) -> Bounds | None:
        """从窗口管理器输出中找出可见的 Android 系统错误弹窗。"""
        blocks = re.split(r"(?=^\s*Window #\d+\s+Window\{)", windows, flags=re.MULTILINE)
        for block in blocks:
            if ANDROID_WARNING_WINDOW_TITLE not in block:
                continue
            if ANDROID_WARNING_WINDOW_TYPE not in block:
                continue
            if "isVisible=true" not in block and "isOnScreen=true" not in block:
                continue
            match = ANDROID_WARNING_WINDOW_FRAME.search(block)
            if match is None:
                continue
            bounds = Bounds(*(int(item) for item in match.groups()))
            if bounds.area > 0:
                return bounds
        return None

    @staticmethod
    def _android_warning_tap_point(bounds: Bounds) -> tuple[int, int]:
        """按 Android Material 单按钮弹窗的相对布局计算“确定”按钮中心。"""
        # 按钮通常位于弹窗右下角，使用比例可适配不同分辨率和窗口缩放。
        x = bounds.right - max(1, round(bounds.width * 0.105))
        y = bounds.bottom - max(1, round(bounds.height * 0.235))
        return x, y

    def dismiss_android_warning(self, windows: str | None = None) -> bool:
        """关闭已知的 Android 系统兼容性提示，避免它阻塞应用首屏。"""
        # wait_app_ready 已经拿到了窗口快照；先用快照处理，避免容器短暂冻结时
        # 再等待数轮 uiautomator 超时。
        if windows is not None:
            bounds = self._android_warning_bounds(windows)
            if bounds is not None:
                x, y = self._android_warning_tap_point(bounds)
                self.input("tap", str(x), str(y))
                print("已自动关闭 Android 系统兼容性提示")
                time.sleep(0.5)
                return True
        try:
            nodes = parse_nodes(self.ui_dump())
        except AgentError:
            nodes = []
        warning = find_node(nodes, "您的设备内部出现了问题")
        button = find_node(nodes, "确定", clickable_only=True, exact=True)
        if warning is not None and button is not None:
            self.input("tap", str(button.bounds.center[0]), str(button.bounds.center[1]))
            print("已自动关闭 Android 系统兼容性提示")
            time.sleep(0.5)
            return True

        # 容器冻结或系统弹窗切换瞬间，uiautomator 可能返回空根节点；此时
        # 只接受带有精确标题、SYSTEM_ERROR 类型和可见边界的系统窗口，避免误点业务页面。
        if windows is None:
            try:
                windows = self.adb(
                    "shell", "dumpsys", "window", "windows", timeout=10
                ).decode("utf-8", errors="replace")
            except AgentError:
                return False
        bounds = self._android_warning_bounds(windows)
        if bounds is None:
            return False
        x, y = self._android_warning_tap_point(bounds)
        self.input("tap", str(x), str(y))
        print("已自动关闭 Android 系统兼容性提示")
        time.sleep(0.5)
        return True

    def app_not_responding(self, package_name: str = PACKAGE_NAME) -> bool:
        """读取 ActivityManager 的进程状态，识别输入分发器已经标记的 ANR。"""
        raw = self.adb("shell", "dumpsys", "activity", "processes", timeout=15)
        text = raw.decode("utf-8", errors="replace")
        blocks = re.split(r"(?=^\s*\*APP\*\s+UID\s+)", text, flags=re.MULTILINE)
        for block in blocks:
            if package_name not in block:
                continue
            if re.search(r"\bmNotResponding\s*=\s*true\b", block):
                return True
        return False

    def wait_app_ready(
        self,
        package_name: str = PACKAGE_NAME,
        timeout: float = APP_READY_TIMEOUT,
        poll_interval: float = APP_READY_POLL_INTERVAL,
    ) -> None:
        """等待应用移除 Android 启动窗口，避免把 Splash 或旧层级交给模型。"""
        deadline = time.monotonic() + timeout
        last_detail = "未知状态"
        appstore_stop_attempted = False
        warning_dismissed = False
        last_warning_check_at = float("-inf")
        while time.monotonic() < deadline:
            try:
                now = time.monotonic()
                activity = self.activity()
                windows = self.adb(
                    "shell", "dumpsys", "window", "windows", timeout=10
                ).decode("utf-8", errors="replace")
                if any(marker.lower() in windows.lower() for marker in APP_ANR_WINDOW_MARKERS) or self.app_not_responding(package_name):
                    raise AppUnresponsiveError(
                        f"{package_name} 启动阶段出现应用无响应（ANR），准备重新启动"
                    )
                splash_present = bool(
                    re.search(rf"\b{re.escape(package_name)}/SplashScreen\b", windows)
                )
                last_detail = (
                    f"Activity={activity[:100] or '未知'}，"
                    f"启动窗口={'仍存在' if splash_present else '未发现'}"
                )
                # 系统兼容性弹窗可能在 Activity 启动十几秒后才出现；在成功关闭前每轮检查，
                # 不能用“一次检查”标志把延迟出现的弹窗永久漏掉。
                if (
                    not warning_dismissed
                    and (not splash_present or now - last_warning_check_at >= ANDROID_WARNING_CHECK_INTERVAL)
                ):
                    last_warning_check_at = now
                    if self.dismiss_android_warning(windows):
                        warning_dismissed = True
                        continue
                if (
                    splash_present
                    and activity.startswith(APPSTORE_PACKAGE_NAME + "/")
                    and not appstore_stop_attempted
                ):
                    # 应用宝的桌面小窗可能压在元宝 Splash 上；每日任务不依赖它，关闭后再让元宝接管前台。
                    self.adb(
                        "shell", "am", "force-stop", APPSTORE_PACKAGE_NAME, timeout=20
                    )
                    appstore_stop_attempted = True
                    time.sleep(0.5)
                    continue
                if activity.startswith(package_name + "/") and not splash_present:
                    return
            except (ManualActionRequired, AppUnresponsiveError):
                # 人工登录和 ANR 都不能降级为普通超时；外层分别等待用户或重启会话。
                raise
            except AgentError as exc:
                last_detail = str(exc)[:180]
            time.sleep(poll_interval)
        # 启动窗口超时通常意味着 ARM64 转译进程仍附着在旧实例；继续复用
        # 当前会话只会让下一次 Intent 再次被 ActivityManager 拒绝。交给外层
        # 的 AppUnresponsiveError 触发一次完整 Waydroid 会话重建。
        raise AppUnresponsiveError(f"应用启动未完成（等待 {timeout:.0f} 秒；{last_detail}）")

    def activity(self) -> str:
        raw = self.adb("shell", "dumpsys", "activity", "activities", timeout=20)
        text = raw.decode("utf-8", errors="replace")
        match = re.search(r"topResumedActivity=.*?\s([\w.$]+/[\w.$]+)", text)
        return match.group(1) if match else text[:160]

    def display_size(self) -> tuple[int, int]:
        raw = self.adb("shell", "wm", "size", timeout=10).decode("utf-8", errors="replace")
        match = re.search(r"(\d+)x(\d+)", raw)
        if not match:
            raise AgentError(f"无法读取屏幕尺寸：{raw[:100]}")
        return int(match.group(1)), int(match.group(2))

    def push_image(self, local_path: Path, remote_path: str = "/sdcard/Pictures/auto-daily-test.png") -> None:
        self.adb("shell", "mkdir", "-p", "/sdcard/Pictures", timeout=15)
        self.adb("push", str(local_path), remote_path, timeout=30)
        # PictureSelector 按媒体更新时间排序；触摸文件后再通知扫描器，确保本轮图片排在最前。
        self.adb("shell", "touch", remote_path, timeout=15)
        self.adb(
            "shell",
            "am",
            "broadcast",
            "-a",
            "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d",
            f"file://{remote_path}",
            timeout=15,
        )

    def launch_app(self, package_name: str = PACKAGE_NAME) -> None:
        """通过标准 Android Intent 启动应用，不依赖 Waydroid 命令。"""
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_name):
            raise AgentError(f"应用包名格式错误：{package_name!r}")
        if package_name == PACKAGE_NAME:
            # 应用宝的桌面小窗会抢占元宝启动层，并在 4GB 实例中诱发 ANR；每日任务不依赖它。
            self.adb("shell", "am", "force-stop", APPSTORE_PACKAGE_NAME, timeout=20)
            # 元宝使用 ARM64 兼容层时，force-stop 可能留下旧进程记录，导致新 Activity
            # 被系统判定为“附着到旧进程”并永远停在 Splash。正常启动只发 Intent；
            # 故障恢复路径会在确认 ANR 或启动超时后显式清理进程。
        deadline = time.monotonic() + APP_RESOLVE_TIMEOUT
        resolved_activity: str | None = None
        last_output = ""
        last_error: AgentError | None = None
        try:
            while time.monotonic() < deadline:
                try:
                    resolved = self.adb(
                        "shell",
                        "cmd",
                        "package",
                        "resolve-activity",
                        "--brief",
                        "-a",
                        "android.intent.action.MAIN",
                        "-c",
                        "android.intent.category.LAUNCHER",
                        package_name,
                        timeout=20,
                    )
                    last_output = resolved.decode("utf-8", errors="replace")
                except AgentError as exc:
                    last_error = exc
                    time.sleep(APP_RESOLVE_POLL_INTERVAL)
                    continue
                match = re.search(
                    r"(?m)^([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)\s*$",
                    last_output,
                )
                if match is not None and match.group(1).startswith(package_name + "/"):
                    resolved_activity = match.group(1)
                    break
                time.sleep(APP_RESOLVE_POLL_INTERVAL)
            if resolved_activity is None:
                detail = last_output.strip()[:180] or str(last_error or "无输出")
                raise AgentError(
                    f"应用没有可启动的 MAIN/LAUNCHER Activity：{package_name}（等待 {APP_RESOLVE_TIMEOUT} 秒后仍未就绪；{detail}）"
                )
            self.adb("shell", "am", "start", "-W", "-n", resolved_activity, timeout=30)
        except AgentError as exc:
            raise AgentError(f"无法通过 ADB 启动应用 {package_name}：{exc}") from exc
        time.sleep(2)


class WaydroidRuntime:
    """管理一次任务所需的 Waydroid 会话生命周期。"""

    def __init__(
        self,
        start_timeout: float = WAYDROID_START_TIMEOUT,
        stop_timeout: float = WAYDROID_STOP_TIMEOUT,
        poll_interval: float = WAYDROID_POLL_INTERVAL,
    ):
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self.poll_interval = poll_interval
        self._session_process: Any | None = None
        self._session_started = False
        self.managed = False

    @property
    def session_started(self) -> bool:
        """返回本轮是否由运行时启动过用户会话。"""
        return self._session_started

    @staticmethod
    def _status() -> str:
        try:
            result = subprocess.run(
                ["waydroid", "status"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"无法读取 Waydroid 状态：{type(exc).__name__}") from exc
        return result.stdout

    @staticmethod
    def _extract_ip(status: str) -> str | None:
        match = re.search(r"^IP address:\s*(\d+\.\d+\.\d+\.\d+)\s*$", status, re.MULTILINE)
        return match.group(1) if match else None

    @staticmethod
    def _session_running(status: str) -> bool:
        return bool(re.search(r"^Session:\s*RUNNING\s*$", status, re.MULTILINE))

    @staticmethod
    def _container_frozen(status: str) -> bool:
        return bool(re.search(r"^Container:\s*FROZEN\s*$", status, re.MULTILINE))

    @staticmethod
    def _fully_stopped(status: str) -> bool:
        # Waydroid 没有用户会话时只输出 Session: STOPPED，不会输出 Container 行。
        # 只有明确报告容器仍在运行时才需要继续等待；缺少该行代表会话已退出。
        if not re.search(r"^Session:\s*STOPPED\s*$", status, re.MULTILINE):
            return False
        container = re.search(r"^Container:\s*(\S+)\s*$", status, re.MULTILINE)
        return container is None or container.group(1) == "STOPPED"

    def _start_session(self) -> None:
        try:
            self._session_process = subprocess.Popen(
                ["waydroid", "session", "start"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise AgentError(f"无法启动 Waydroid 会话：{type(exc).__name__}") from exc

    @staticmethod
    def launch_app(device: Device, package_name: str = PACKAGE_NAME) -> None:
        """通过 Waydroid 应用入口启动应用，登记活动应用并避免后台会话自动冻结。"""
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_name):
            raise AgentError(f"应用包名格式错误：{package_name!r}")
        if package_name == PACKAGE_NAME:
            # 应用宝的小窗可能抢占启动层；每日任务不依赖它，可以安全关闭。
            device.adb("shell", "am", "force-stop", APPSTORE_PACKAGE_NAME, timeout=20)
        # 不在正常路径 force-stop 元宝。ARM64 兼容层有时会留下无法回收的旧进程，
        # 随后的启动会卡在 Splash；ToolExecutor._restart_app 仅在恢复路径先清理。
        try:
            result = subprocess.run(
                ["waydroid", "app", "launch", package_name],
                capture_output=True,
                text=True,
                timeout=APP_RESOLVE_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"Waydroid 应用启动超时：{package_name}") from exc
        except OSError as exc:
            raise AgentError(f"无法调用 Waydroid 应用入口：{type(exc).__name__}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:180]
            raise AgentError(f"Waydroid 应用启动失败：{detail or '无输出'}")
        time.sleep(2)

    @staticmethod
    def _unfreeze_container() -> None:
        try:
            result = subprocess.run(
                ["waydroid", "container", "unfreeze"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"无法解冻 Waydroid 容器：{type(exc).__name__}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:180]
            raise AgentError(f"Waydroid 容器解冻失败：{detail or '无输出'}")

    def _recover_frozen_container(self) -> None:
        """优先解冻；普通用户无权操作容器时，重启一次用户会话作为后备。"""
        try:
            self._unfreeze_container()
            return
        except AgentError as exc:
            print(f"Waydroid 容器解冻不可用（{str(exc)[:140]}），正在重启用户会话")
        if not self.stop():
            raise AgentError("Waydroid 容器冻结且无法停止用户会话")
        self._start_session()
        self._session_started = True

    def discover_device(self, configured: str) -> Device:
        """必要时启动会话，并等待容器 IP 与 ADB 都可用。"""
        value = configured.strip()
        if value and value.lower() != "auto":
            return Device.discover(value)

        self.managed = True
        status = self._status()
        frozen_recovery_attempted = False
        if self._container_frozen(status):
            print("Waydroid 容器当前已冻结，正在解冻")
            self._recover_frozen_container()
            frozen_recovery_attempted = True
            status = self._status()
        if (
            not self._extract_ip(status)
            and not self._session_running(status)
            and not self._session_started
        ):
            self._start_session()
            self._session_started = True

        try:
            deadline = time.monotonic() + self.start_timeout
            last_error: AgentError | None = None
            unfreeze_attempted = frozen_recovery_attempted
            while time.monotonic() < deadline:
                status = self._status()
                if self._container_frozen(status) and not unfreeze_attempted:
                    unfreeze_attempted = True
                    print("Waydroid 容器在启动等待期间被冻结，正在解冻")
                    self._recover_frozen_container()
                    continue
                ip = self._extract_ip(status)
                if ip:
                    device = Device(f"{ip}:5555")
                    try:
                        device.connect()
                        remaining = max(1.0, deadline - time.monotonic())
                        device.wait_ready(timeout=min(WAYDROID_READY_TIMEOUT, remaining))
                        return device
                    except AgentError as exc:
                        last_error = exc

                if self._session_process is not None and self._session_process.poll() is not None:
                    if not self._session_running(status):
                        break
                time.sleep(self.poll_interval)

            if last_error is not None:
                raise AgentError(f"Waydroid 启动后 ADB 未就绪：{last_error}") from last_error
            raise AgentError("Waydroid 启动超时，未获得容器 IP")
        except AgentError:
            # 冷启动失败时清理本轮新建的会话，允许 systemd 的下一次重试从干净状态开始。
            if self._session_started:
                print("Waydroid 冷启动失败，正在清理半启动会话")
                self.stop()
            raise

    def stop(self) -> bool:
        """停止用户会话及其容器，并等待状态变为 STOPPED。"""
        try:
            result = subprocess.run(
                ["waydroid", "session", "stop"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"关闭 Waydroid 失败：{type(exc).__name__}")
            return False

        deadline = time.monotonic() + self.stop_timeout
        stopped = False
        while time.monotonic() < deadline:
            try:
                status = self._status()
            except AgentError:
                break
            if self._fully_stopped(status):
                stopped = True
                break
            time.sleep(self.poll_interval)

        if self._session_process is not None:
            try:
                self._session_process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                with contextlib.suppress(OSError):
                    self._session_process.terminate()
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    self._session_process.wait(timeout=2)
            self._session_process = None
        self._session_started = False

        if result.returncode != 0:
            return False
        return stopped
