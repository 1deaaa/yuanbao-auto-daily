#!/usr/bin/env python3
"""使用视觉大模型和受限工具完成元宝每日任务。

程序将观测、动作执行、任务计数、等待生成、奖励兑换和完成断言分开，
模型只能调用本文件声明的工具，不能执行任意宿主机命令。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from openai import APIError, BadRequestError, OpenAI


ROOT = Path(__file__).resolve().parent
DEFAULT_DEVICE = "auto"
DEFAULT_BASE_URL = "http://localhost:7860/v1"
DEFAULT_MODEL = "gemini-3.7-flash"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_RUN_TIME = "05:00"
DEFAULT_TEST_PROMPT = "healthy habits"
DEFAULT_TEST_IMAGE = str(ROOT / "测试题目.png")
PACKAGE_NAME = "com.tencent.hunyuan.app.chat"
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_COOLDOWN = 10
DEFAULT_MAX_STEPS = 260
DEFAULT_GENERATION_TIMEOUT = 180
DEFAULT_CARD_NAME = "QQ超级会员1天卡"
DEFAULT_FALLBACK_CARD_NAME = "QQ超级会员3天卡"
DEFAULT_STATE_PATH = ROOT / ".yuanbao_daily_state.json"
DEFAULT_STOP_WAYDROID_AFTER_RUN = True
WAYDROID_START_TIMEOUT = 90
WAYDROID_STOP_TIMEOUT = 45
WAYDROID_POLL_INTERVAL = 1.0
APP_RESOLVE_TIMEOUT = 45
APP_RESOLVE_POLL_INTERVAL = 1.0
OURS_NAV_TIMEOUT = 30
OURS_NAV_POLL_INTERVAL = 1.0
WELFARE_LOAD_TIMEOUT = 15
WELFARE_LOAD_POLL_INTERVAL = 1.5
MAX_HISTORY_SUMMARY_CHARS = 1200
MAX_HISTORY_RESULT_CHARS = 1200
IGNORED_TASKS = ("邀请新用户",)


TASK_LABELS = {
    "question": "问元宝问题",
    "writing": "使用写作能力",
    "image": "使用P图能力",
    "photo_question": "使用拍题能力",
    "same_template": "使用推荐模板做同款",
}
TASK_ORDER = tuple(TASK_LABELS)


class AgentError(RuntimeError):
    """代理可以报告给模型、并由外层重试的错误。"""


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise AgentError(f"配置 {name} 必须是整数") from exc
    if value < minimum:
        raise AgentError(f"配置 {name} 不能小于 {minimum}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise AgentError(f"配置 {name} 必须是布尔值")


def _project_path(value: str) -> Path:
    """解析配置路径；相对路径统一相对于项目目录，便于跨主机运行。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def parse_run_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        raise AgentError(f"每日运行时间格式错误，应为 HH:MM：{value!r}")
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class Config:
    model_id: str
    base_url: str
    api_key: str
    device: str
    stop_waydroid_after_run: bool
    run_time: str
    timezone_name: str
    max_retries: int
    retry_cooldown_seconds: int
    max_steps: int
    generation_timeout_seconds: int
    test_prompt: str
    test_image_path: Path
    card_name: str
    fallback_card_name: str
    prompt_path: Path
    state_path: Path

    @classmethod
    def from_env(cls, env_path: Path = ROOT / ".env") -> "Config":
        # 环境变量优先于 .env，便于 systemd、容器和临时测试覆盖配置。
        if env_path.exists():
            load_dotenv(env_path, override=False)
        model_id = _first_env("MODEL_ID", "LOCAL_LLM_MODEL", default=DEFAULT_MODEL)
        base_url = _first_env("BASE_URL", "LOCAL_LLM_BASE_URL", default=DEFAULT_BASE_URL).rstrip("/")
        api_key = _first_env("API_KEY", "LOCAL_LLM_API_KEY")
        if not api_key:
            raise AgentError("缺少 API_KEY，请在 .env 或环境变量中配置")
        run_time = _first_env("DAILY_RUN_TIME", default=DEFAULT_RUN_TIME)
        parse_run_time(run_time)
        timezone_name = _first_env("TIMEZONE", "DAILY_TIMEZONE", default=DEFAULT_TIMEZONE)
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise AgentError(f"找不到时区：{timezone_name}") from exc
        test_image = _project_path(_first_env("TEST_IMAGE_PATH", default=DEFAULT_TEST_IMAGE))
        prompt_path = _project_path(_first_env("PROMPT_PATH", default="每日任务提示词.md"))
        state_path = _project_path(_first_env("STATE_PATH", default=".yuanbao_daily_state.json"))
        config = cls(
            model_id=model_id,
            base_url=base_url,
            api_key=api_key,
            device=_first_env("ANDROID_DEVICE", default=DEFAULT_DEVICE),
            stop_waydroid_after_run=_env_bool(
                "STOP_WAYDROID_AFTER_RUN", DEFAULT_STOP_WAYDROID_AFTER_RUN
            ),
            run_time=run_time,
            timezone_name=timezone_name,
            max_retries=_env_int("MAX_RETRIES", DEFAULT_MAX_RETRIES, minimum=1),
            retry_cooldown_seconds=_env_int(
                "RETRY_COOLDOWN_SECONDS", DEFAULT_RETRY_COOLDOWN, minimum=0
            ),
            max_steps=_env_int("MAX_STEPS", DEFAULT_MAX_STEPS, minimum=1),
            generation_timeout_seconds=_env_int(
                "GENERATION_TIMEOUT_SECONDS", DEFAULT_GENERATION_TIMEOUT, minimum=5
            ),
            test_prompt=_first_env("TEST_PROMPT", default=DEFAULT_TEST_PROMPT),
            test_image_path=test_image,
            card_name=_first_env("EXCHANGE_PRODUCT", default=DEFAULT_CARD_NAME),
            fallback_card_name=_first_env(
                "EXCHANGE_FALLBACK_PRODUCT", default=DEFAULT_FALLBACK_CARD_NAME
            ),
            prompt_path=prompt_path,
            state_path=state_path,
        )
        if not config.test_prompt or len(config.test_prompt) > 200:
            raise AgentError("TEST_PROMPT 为空或过长")
        if not config.test_image_path.is_file():
            raise AgentError(f"测试图片不存在：{config.test_image_path}")
        return config


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2


@dataclass(frozen=True)
class Node:
    text: str
    content_desc: str
    resource_id: str
    class_name: str
    clickable: bool
    enabled: bool
    bounds: Bounds

    @property
    def searchable(self) -> str:
        return f"{self.text} {self.content_desc} {self.resource_id}".lower()


def parse_bounds(value: str) -> Bounds:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not match:
        return Bounds(0, 0, 0, 0)
    return Bounds(*(int(item) for item in match.groups()))


def parse_nodes(xml: str) -> list[Node]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    result: list[Node] = []
    for element in root.iter("node"):
        attrs = element.attrib
        result.append(
            Node(
                text=attrs.get("text", ""),
                content_desc=attrs.get("content-desc", ""),
                resource_id=attrs.get("resource-id", ""),
                class_name=attrs.get("class", ""),
                clickable=attrs.get("clickable") == "true",
                enabled=attrs.get("enabled", "true") == "true",
                bounds=parse_bounds(attrs.get("bounds", "")),
            )
        )
    return result


def compact_ui(xml: str, limit: int = 14000) -> str:
    """只把可操作或有文本的节点发送给模型，避免泄露无关层级。"""
    lines: list[str] = []
    for node in parse_nodes(xml):
        attrs: dict[str, Any] = {
            "text": node.text,
            "content-desc": node.content_desc,
            "resource-id": node.resource_id,
            "class": node.class_name,
            "clickable": node.clickable,
            "enabled": node.enabled,
            "bounds": f"[{node.bounds.left},{node.bounds.top}][{node.bounds.right},{node.bounds.bottom}]",
        }
        if node.text or node.content_desc or node.resource_id or node.clickable:
            lines.append(json.dumps(attrs, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)[:limit]


def find_node(
    nodes: Iterable[Node],
    *needles: str,
    clickable_only: bool = False,
    exact: bool = False,
) -> Node | None:
    candidates = [node for node in nodes if node.enabled and node.bounds.area > 0]
    if clickable_only:
        candidates = [node for node in candidates if node.clickable]
    for needle in needles:
        wanted = needle.lower()
        for node in candidates:
            fields = (node.text, node.content_desc, node.resource_id)
            if exact and any(field == needle for field in fields):
                return node
            if not exact and wanted in node.searchable:
                return node
    return None


def has_visible_text(nodes: Iterable[Node], *needles: str) -> bool:
    haystack = " ".join(node.searchable for node in nodes)
    return all(needle.lower() in haystack for needle in needles)


def action_signature(name: str, args: dict[str, Any]) -> str:
    return json.dumps({"tool": name, "arguments": args}, ensure_ascii=False, sort_keys=True)


def state_signature(ui: str, image: bytes, activity: str) -> str:
    material = f"{activity}\n{ui}" if ui else activity + hashlib.sha256(image).hexdigest()
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


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

    def ui_dump(self) -> str:
        remote = "/sdcard/auto_daily_ui.xml"
        self.adb("shell", "uiautomator", "dump", remote, timeout=20)
        return self.adb("shell", "cat", remote, timeout=10).decode("utf-8", errors="replace")

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
            self.adb("shell", "am", "start", "-n", resolved_activity, timeout=30)
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

    def discover_device(self, configured: str) -> Device:
        """必要时启动会话，并等待容器 IP 与 ADB 都可用。"""
        value = configured.strip()
        if value and value.lower() != "auto":
            return Device.discover(value)

        self.managed = True
        status = self._status()
        if not self._extract_ip(status) and not self._session_running(status):
            self._start_session()
            self._session_started = True

        try:
            deadline = time.monotonic() + self.start_timeout
            last_error: AgentError | None = None
            while time.monotonic() < deadline:
                status = self._status()
                ip = self._extract_ip(status)
                if ip:
                    device = Device(f"{ip}:5555")
                    try:
                        device.connect()
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


@dataclass(frozen=True)
class Observation:
    image: bytes
    ui_xml: str
    ui: str
    nodes: tuple[Node, ...]
    activity: str
    stage: str


def detect_stage(nodes: Iterable[Node], activity: str) -> str:
    node_list = list(nodes)
    text = " ".join(node.searchable for node in node_list)
    activity_lower = activity.lower()
    if (
        "documentsui" in activity_lower
        or "com.google.android.documentsui" in text
        or "pictureselector" in activity_lower
        or "roleplaypickeractivity" in activity_lower
        or any(marker in text for marker in ("相机胶卷", "本地相册", "最近项目"))
    ):
        return "picker"
    if "cameraresultactivity" in activity_lower or "一次框选一道题" in text:
        return "photo_preview"
    if "奖品记录" in text and "兑换商城" not in text:
        return "prize_records"
    if any(
        marker in text
        for marker in ("每日问元宝得积分", "去写作", "去p图", "去拍题", "问元宝任意问题累计")
    ):
        return "welfare"
    if "兑换商城" in text or any(
        marker in text for marker in ("qq超级会员1天卡", "qq超级会员3天卡")
    ):
        return "exchange"
    # “我们”页的抽屉中有福利中心和任务；聊天页虽然也有底部导航，但没有这组内容。
    if "福利中心" in text and "任务" in text:
        return "ours"
    if any(value in text for value in ("选择写作类型", "ai写作", "请输入你要写的主题")):
        return "writing"
    if any(value in text for value in ("智能p图", "上传图片", "p图能力")):
        return "image"
    if any(value in text for value in ("拍题", "拍照答题", "相册答题")):
        return "photo_question"
    if any(node.resource_id.endswith("edConversationInput") for node in node_list):
        return "chat"
    if PACKAGE_NAME in activity:
        return "app"
    return "unknown"


@dataclass
class ToolResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class ToolExecutor:
    """执行层只提供白名单工具，并维护生成等待和奖品使用证据。"""

    def __init__(self, config: Config, device: Device):
        self.config = config
        self.device = device
        self.width, self.height = device.display_size()
        self.today = datetime.now(ZoneInfo(config.timezone_name)).date().isoformat()
        self.pending_generation = False
        self.reward_use_confirmed = False
        self.image_pushed = False
        self.exchange_target_clicked = False
        self.exchange_confirmed = False
        self.prize_records_opened = False
        self.reward_use_clicked = False
        self.reward_card_name = config.card_name
        self.reward_unavailable = False

    def _read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.config.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_state(self, exchange_status: str, card_name: str | None = None) -> None:
        """保存仅用于同日恢复的非敏感状态，不写入账号、令牌或截图。"""
        path = self.config.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "date": self.today,
                        "card": card_name or self.reward_card_name,
                        "exchange_status": exchange_status,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temporary.replace(path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            # 状态文件是恢复优化，不能因目录权限问题阻断已经成功的兑换。
            print(f"状态文件写入失败：{type(exc).__name__}")

    def _today_exchange_status(self, card_name: str | None = None) -> str | None:
        state = self._read_state()
        expected_card = card_name or self.reward_card_name
        if state.get("date") != self.today or state.get("card") != expected_card:
            return None
        status = state.get("exchange_status")
        return status if status in {"pending", "used", "unavailable"} else None

    def _dismiss_android_warning(self, nodes: Iterable[Node]) -> bool:
        """只关闭已知的 Android 系统兼容性提示，不替模型处理业务弹窗。"""
        warning = find_node(nodes, "您的设备内部出现了问题")
        if warning is None:
            return False
        button = find_node(nodes, "确定", clickable_only=True, exact=True)
        if button is None:
            return False
        self._tap_node(button)
        time.sleep(0.5)
        return True

    def observe(self) -> Observation:
        # Waydroid/Android 某些实例首次启动应用时会弹出系统兼容性提示；
        # 该提示不是业务状态，若不关闭会遮住底部导航并阻断固定的首步导航。
        for _ in range(2):
            image = self.device.screenshot()
            ui_xml = self.device.ui_dump()
            nodes = tuple(parse_nodes(ui_xml))
            if self._dismiss_android_warning(nodes):
                continue
            activity = self.device.activity()
            return Observation(image, ui_xml, compact_ui(ui_xml), nodes, activity, detect_stage(nodes, activity))
        activity = self.device.activity()
        return Observation(image, ui_xml, compact_ui(ui_xml), nodes, activity, detect_stage(nodes, activity))

    def _result(self, ok: bool, message: str, **data: Any) -> ToolResult:
        return ToolResult(ok, message, data)

    def _tap_node(self, node: Node) -> None:
        x, y = node.bounds.center
        self._tap_point(x, y)

    def _tap_point(self, x: int, y: int) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise AgentError(f"坐标越界：{x},{y}")
        self.device.input("tap", str(x), str(y))

    def _guard_generation(self, tool_name: str) -> ToolResult | None:
        if self.pending_generation and tool_name != "wait_5s":
            return self._result(False, "消息仍可能在生成，必须先调用 wait_5s 等待右下角终止按钮消失")
        return None

    def _find_input(self, observation: Observation) -> Node | None:
        preferred = find_node(observation.nodes, "edConversationInput", clickable_only=False)
        if preferred is not None:
            return preferred
        # 写作页的 Compose 输入框有时没有资源 ID，使用可编辑节点作为后备。
        candidates = [
            node
            for node in observation.nodes
            if node.enabled
            and node.bounds.area > 0
            and (
                node.class_name.lower().endswith("edittext")
                or "输入" in node.content_desc
                or "主题" in node.content_desc
            )
        ]
        return max(candidates, key=lambda node: node.bounds.area, default=None)

    def _find_send(self, observation: Observation) -> Node | None:
        nodes = [node for node in observation.nodes if node.enabled and node.bounds.area > 0]
        preferred = [
            node
            for node in nodes
            if node.clickable
            and (
                "发送" in node.text
                or "发送" in node.content_desc
                or "send" in node.content_desc.lower()
                or (
                    (rid := node.resource_id.lower())
                    and (
                        "send" in rid
                        or "submit" in rid
                        or "arrow_up" in rid
                        or "arrowup" in rid
                        or ("ic_up" in rid and "upload" not in rid)
                    )
                )
            )
        ]
        if preferred:
            return max(preferred, key=lambda node: node.bounds.area)
        # Compose 版本的绿色上箭头有时没有文字和资源 ID，只暴露为右下角可点击图标。
        # 仅在底部区域寻找较小控件，避免误点页面上的其它大按钮。
        bottom_right = [
            node
            for node in nodes
            if node.clickable
            and node.bounds.top >= self.height * 0.72
            and node.bounds.center[0] >= self.width * 0.72
            and node.bounds.area <= 40_000
            and not any(
                marker in f"{node.text} {node.content_desc}".lower()
                for marker in ("上传", "相册", "键盘", "收起", "关闭")
            )
        ]
        return min(
            bottom_right,
            key=lambda node: (
                abs(node.bounds.center[0] - (self.width - 60))
                + abs(node.bounds.center[1] - (self.height - 60)),
                -node.bounds.area,
            ),
            default=None,
        )

    def _input_fixed_prompt(self, observation: Observation) -> ToolResult:
        if observation.stage == "ours":
            ours = find_node(observation.nodes, "问元宝", exact=True)
            if ours is not None:
                self._tap_node(ours)
            else:
                self._tap_point(111, self.height - 43)
            time.sleep(1.5)
            observation = self.observe()
        input_node = self._find_input(observation)
        if input_node is None:
            fallback_y = 755 if observation.stage == "writing" else self.height - 100
            self._tap_point(self.width // 2, fallback_y)
        else:
            self._tap_node(input_node)
        value = self.config.test_prompt
        if any(ord(char) > 127 for char in value):
            return self._result(False, "固定测试文本含中文，当前设备未配置可靠中文输入；请在 TEST_PROMPT 中使用英文")
        # 输入框可能已经保留上一次尝试的内容；先检查完整节点文本，避免重复追加。
        if input_node is not None and value in input_node.text:
            return self._result(True, "固定测试文本已在输入框中", stage=observation.stage)
        escaped = value.replace("%", "%25").replace(" ", "%s")
        self.device.input("text", escaped)
        time.sleep(0.5)
        after = self.observe()
        # compact_ui 为了控制模型上下文会截断，不能用它判断输入是否成功。
        input_texts = [node.text for node in after.nodes if node.text]
        if not any(value in text for text in input_texts):
            return self._result(False, "固定测试文本没有出现在输入框")
        return self._result(True, "已通过固定工具输入测试文本", stage=after.stage)

    def _wait_generation(self) -> ToolResult:
        time.sleep(5)
        deadline = time.monotonic() + self.config.generation_timeout_seconds
        checks = 0
        while True:
            checks += 1
            try:
                observation = self.observe()
            except AgentError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(5)
                continue
            busy = False
            for node in observation.nodes:
                label = f"{node.text} {node.content_desc}".lower()
                if "停止生成" in label or "停止回答" in label or "stop generating" in label:
                    busy = True
                    break
                if (
                    node.bounds.area > 1000
                    and "fl_send_stop" in node.resource_id.lower()
                    and "发送" not in label
                ):
                    busy = True
                    break
            if not busy:
                self.pending_generation = False
                return self._result(True, f"已等待生成完成（检查 {checks} 次）", stage=observation.stage)
            if time.monotonic() >= deadline:
                return self._result(False, "等待生成超时，右下角终止按钮仍存在")
            time.sleep(5)

    def _go_to_ours(self) -> Observation:
        """等待首页导航就绪后进入“我们”，避免冷启动时过早返回或误按。"""
        deadline = time.monotonic() + OURS_NAV_TIMEOUT
        while time.monotonic() < deadline:
            observation = self.observe()
            if observation.stage == "ours":
                return observation
            ours_node = find_node(observation.nodes, "我们", exact=True)
            if ours_node is not None:
                # 不假设固定分辨率；部分设备的底部导航高度和横向边距不同。
                self._tap_node(ours_node)
                time.sleep(1.5)
                continue
            # 应用冷启动或 WebView 首次恢复时层级可能暂时为空。
            # 先给首页最多约 8 秒加载时间，避免 BACK 把尚未就绪的应用退出。
            elapsed = OURS_NAV_TIMEOUT - max(0.0, deadline - time.monotonic())
            if elapsed >= 8:
                self.device.input("keyevent", "KEYCODE_BACK")
                time.sleep(1.2)
            else:
                time.sleep(OURS_NAV_POLL_INTERVAL)
        observation = self.observe()
        if observation.stage != "ours":
            raise AgentError("无法回到元宝“我们”页面")
        return observation

    def _go_to_welfare(self) -> Observation:
        observation = self._go_to_ours()
        welfare_node = find_node(observation.nodes, "福利中心", exact=True)
        if welfare_node is None:
            self._tap_point(640, 486)
        else:
            self._tap_node(welfare_node)
        # WebView 从详情页返回时偶尔需要数秒恢复层级；在执行层内等待，避免把临时加载状态交给模型。
        deadline = time.monotonic() + WELFARE_LOAD_TIMEOUT
        while time.monotonic() < deadline:
            observation = self.observe()
            if observation.stage == "welfare":
                return observation
            time.sleep(WELFARE_LOAD_POLL_INTERVAL)
        raise AgentError("福利中心没有加载出任务页面")

    def _ensure_picker_image(self) -> ToolResult:
        if not self.image_pushed:
            self.device.push_image(self.config.test_image_path)
            self.image_pushed = True
            time.sleep(1)
        observation = self.observe()
        for attempt in range(3):
            if observation.stage == "picker":
                break
            candidates = [
                node
                for node in observation.nodes
                if node.clickable
                and node.bounds.area > 0
                and any(word in node.searchable for word in ("相册", "图片", "上传", "image", "gallery"))
            ]
            if candidates:
                self._tap_node(candidates[0])
            else:
                # 元宝拍题页和 P 图页都把本地图片入口放在输入区左侧。
                self._tap_point(58, self.height - 125)
            time.sleep(2 + attempt)
            observation = self.observe()
        if observation.stage != "picker":
            return self._result(False, "没有进入系统图片选择器")
        permission = find_node(observation.nodes, "允许", "Allow", clickable_only=True)
        if permission is not None:
            self._tap_node(permission)
            time.sleep(1)
            observation = self.observe()
        # PictureSelector 的文件名通常是不可点击的 content-desc，点击同边界的父 View
        # 可以稳定选中脚本刚推送的测试图片，而不是随机选到旧照片。
        filename = "auto-daily-test.png"
        label = find_node(observation.nodes, filename)
        if label is not None:
            matching = [
                node
                for node in observation.nodes
                if node.clickable and node.enabled and node.bounds == label.bounds
            ]
            if matching:
                self._tap_node(matching[0])
                time.sleep(2)
                return self._result(True, "已选择本地脱敏测试图片", stage=self.observe().stage)
        image_nodes = [
            node
            for node in observation.nodes
            if node.enabled
            and node.bounds.area > 1500
            and node.bounds.top > 80
            and node.class_name.lower().endswith("imageview")
        ]
        if not image_nodes:
            image_nodes = [
                node
                for node in observation.nodes
                if node.enabled and node.clickable and node.bounds.area > 1500 and node.bounds.top > 80
            ]
        if not image_nodes:
            self._tap_point(110, 220)
        else:
            self._tap_node(sorted(image_nodes, key=lambda node: (node.bounds.top, node.bounds.left))[0])
        time.sleep(2)
        return self._result(True, "已选择本地脱敏测试图片", stage=self.observe().stage)

    def _open_exchange(self) -> ToolResult:
        observation = self._go_to_welfare()
        # 奖励弹窗的绿色“开心收下”在部分版本只有图片语义，没有可点击文本。
        # 兑换前先收下残留奖励，避免弹窗拦截兑换商城入口。
        self._dismiss_reward_popup(observation)
        observation = self.observe()
        mall = find_node(observation.nodes, "兑换商城", exact=True)
        if mall is None:
            self._tap_point(110, 233)
        else:
            self._tap_node(mall)
        time.sleep(3)
        after = self.observe()
        if after.stage != "exchange":
            return self._result(False, "兑换商城没有加载")
        return self._result(True, "已进入兑换商城", stage=after.stage)

    def _dismiss_reward_popup(self, observation: Observation | None = None) -> bool:
        observation = observation or self.observe()
        reward_amount = any(
            re.fullmatch(r"\d+\s*积分", node.text.strip())
            and node.bounds.top > self.height // 2
            for node in observation.nodes
        )
        button = find_node(observation.nodes, "button", exact=True)
        if not reward_amount or button is None:
            return False
        self._tap_node(button)
        time.sleep(2)
        return True

    def _find_card_and_button(
        self, observation: Observation, card_name: str | None = None
    ) -> tuple[Node, Node] | None:
        wanted_card = card_name or self.reward_card_name
        product = find_node(observation.nodes, wanted_card, exact=True)
        if product is None:
            product = find_node(observation.nodes, wanted_card)
        if product is None:
            return None
        buttons = [
            node
            for node in observation.nodes
            if node.enabled
            and node.bounds.area > 0
            and (node.text == "兑换" or node.content_desc == "兑换")
        ]
        if not buttons:
            return None
        px, py = product.bounds.center
        same_card = [
            node
            for node in buttons
            if abs(node.bounds.center[0] - px) < 220 and node.bounds.center[1] >= product.bounds.bottom
        ]
        if not same_card:
            # 商品卡片可能仍在视口边缘，不能把别的商品按钮误配给它。
            return None
        return product, min(same_card, key=lambda node: abs(node.bounds.center[1] - py))

    def _read_exchange_points(self, observation: Observation) -> int | None:
        """读取兑换页顶部的当前积分，无法确认时返回 None。"""
        labelled: list[tuple[int, int]] = []
        plain: list[tuple[int, int]] = []
        for node in observation.nodes:
            if node.bounds.area <= 0 or node.bounds.top > 120:
                continue
            value = node.text.strip().replace(",", "")
            match = re.fullmatch(r"(\d+)(?:\s*积分)?", value)
            if match is None:
                continue
            item = (node.bounds.left, int(match.group(1)))
            if "积分" in value:
                labelled.append(item)
            else:
                plain.append(item)
        candidates = labelled or plain
        if not candidates:
            return None
        # 积分数字紧邻“我的积分：”，通常是顶部最靠左的纯数字节点。
        return min(candidates, key=lambda item: item[0])[1]

    def _read_product_cost(self, observation: Observation, product: Node) -> int | None:
        """读取目标商品卡片下方的积分价格。"""
        px, py = product.bounds.center
        candidates = []
        for node in observation.nodes:
            if node.bounds.area <= 0:
                continue
            value = node.text.strip().replace(",", "")
            match = re.fullmatch(r"(\d+)(?:\s*积分)?", value)
            if match is None:
                continue
            nx, ny = node.bounds.center
            if ny < product.bounds.bottom or ny > product.bounds.bottom + 130:
                continue
            if abs(nx - px) > 180:
                continue
            candidates.append((abs(ny - product.bounds.bottom), int(match.group(1))))
        return min(candidates, default=(0, None), key=lambda item: item[0])[1]

    def _exchange_to_top(self, observation: Observation) -> Observation:
        """把兑换商城列表复位到顶部，供备用商品从同一滚动起点重新扫描。"""
        top_button = find_node(observation.nodes, "回到顶部", clickable_only=True)
        if top_button is not None:
            self._tap_node(top_button)
            time.sleep(1)
            return self.observe()
        # 某些版本不暴露“回到顶部”按钮，反向滑动若干次也能回到首屏。
        for _ in range(8):
            self.device.input(
                "swipe",
                str(self.width // 2),
                "450",
                str(self.width // 2),
                "1150",
                "500",
            )
            time.sleep(0.6)
            observation = self.observe()
        return observation

    def _recover_exchange_state(self, observation: Observation) -> str | None:
        """从同日状态文件恢复兑换流程，避免服务重启后重复扣积分。"""
        candidates = [self.config.card_name]
        if self.config.fallback_card_name and self.config.fallback_card_name not in candidates:
            candidates.append(self.config.fallback_card_name)
        for card_name in candidates:
            status = self._today_exchange_status(card_name)
            if status is None:
                continue
            self.reward_card_name = card_name
            self.exchange_confirmed = True
            if status == "used":
                self.prize_records_opened = True
                self.reward_use_clicked = True
                self.reward_use_confirmed = True
                return status
            if status == "unavailable":
                self.reward_unavailable = True
                return status
            return "pending"
        return None

    def _redeem_card(self) -> ToolResult:
        observation = self.observe()
        if observation.stage != "exchange":
            result = self._open_exchange()
            if not result.ok:
                return result
            observation = self.observe()
        self.reward_card_name = self.config.card_name
        self.reward_unavailable = False
        recovered = self._recover_exchange_state(observation)
        if recovered == "used":
            return self._result(
                True,
                "已从同日状态恢复：QQ超级会员卡已对绑定账号使用成功",
                stage=observation.stage,
                reward_use_confirmed=True,
            )
        if recovered == "pending":
            return self._result(
                True,
                "已从同日状态恢复：QQ超级会员卡已兑换，下一步打开奖品记录",
                stage=observation.stage,
                prize_records_opened=False,
            )
        if recovered == "unavailable":
            return self._result(
                True,
                "已从同日状态恢复：备用兑换商品积分不足，跳过兑换并返回“我们”页",
                stage=observation.stage,
                reward_unavailable=True,
            )
        if self._has_exchange_success(observation):
            self.exchange_confirmed = True
            self._write_state("pending")
            return self._result(
                True,
                "当前页面已有明确兑换成功弹窗，跳过重复兑换",
                stage=observation.stage,
                prize_records_opened=False,
            )
        found = self._find_card_and_button(observation, self.reward_card_name)
        for _ in range(6):
            if found:
                break
            self.device.input("swipe", str(self.width // 2), "1150", str(self.width // 2), "450", "500")
            time.sleep(1)
            observation = self.observe()
            found = self._find_card_and_button(observation, self.reward_card_name)
        if not found and self.config.fallback_card_name:
            # 1 天卡下架时继续扫描 3 天卡；只有读到积分和价格后才允许决定是否跳过。
            fallback = self.config.fallback_card_name
            observation = self._exchange_to_top(observation)
            self.reward_card_name = fallback
            found = self._find_card_and_button(observation, fallback)
            for _ in range(6):
                if found:
                    break
                self.device.input("swipe", str(self.width // 2), "1150", str(self.width // 2), "450", "500")
                time.sleep(1)
                observation = self.observe()
                found = self._find_card_and_button(observation, fallback)
            if found:
                product, _ = found
                points = self._read_exchange_points(observation)
                cost = self._read_product_cost(observation, product)
                if points is None or cost is None:
                    return self._result(
                        False,
                        f"已找到备用商品“{fallback}”，但无法确认积分或价格，拒绝盲目兑换",
                        stage=observation.stage,
                    )
                if points < cost:
                    self.reward_unavailable = True
                    self._write_state("unavailable", fallback)
                    return self._result(
                        True,
                        f"未找到“{self.config.card_name}”；备用商品“{fallback}”需要 {cost} 积分，当前仅 {points}，跳过兑换并返回“我们”页",
                        stage=observation.stage,
                        reward_unavailable=True,
                        points=points,
                        cost=cost,
                    )
        if not found:
            self.reward_card_name = self.config.card_name
            return self._result(False, f"没有找到目标商品：{self.config.card_name}，备用商品也不可用")
        product, button = found
        self._tap_node(button)
        time.sleep(2)
        self.exchange_target_clicked = True
        return self._result(True, f"已点击目标商品“{self.reward_card_name}”的兑换按钮", stage=self.observe().stage)

    def _find_reward_use_button(self, observation: Observation) -> Node | None:
        product = find_node(observation.nodes, self.reward_card_name)
        if product is None:
            return None
        buttons = [
            node
            for node in observation.nodes
            if node.enabled
            and node.bounds.area > 0
            and node.text in {"立即使用", "去使用", "使用"}
        ]
        if not buttons:
            return None
        px, py = product.bounds.center
        same_card = [
            node
            for node in buttons
            if abs(node.bounds.center[0] - px) < 260
            and node.bounds.center[1] >= product.bounds.top - 30
        ]
        return min(same_card or buttons, key=lambda node: abs(node.bounds.center[1] - py))

    @staticmethod
    def _has_evidence(observation: Observation, *markers: str) -> bool:
        content = " ".join(
            [observation.ui]
            + [f"{node.text} {node.content_desc}" for node in observation.nodes]
        ).lower()
        return any(marker.lower() in content for marker in markers)

    @staticmethod
    def _has_exchange_success(observation: Observation) -> bool:
        """兑换详情页也会显示“奖品记录”说明，成功必须有更强的页面证据。"""
        content = " ".join(
            [observation.ui]
            + [f"{node.text} {node.content_desc}" for node in observation.nodes]
        ).lower()
        if "兑换成功" in content or "恭喜兑换成功" in content:
            return True
        return "已兑换" in content and ("奖品记录" in content or "前往" in content)

    def _has_reward_use_success(self, observation: Observation) -> bool:
        """只接受目标卡片附近的使用状态，避免把顶部“已使用”筛选标签当成结果。"""
        reward_card_name = getattr(self, "reward_card_name", self.config.card_name)
        card_nodes = [
            node
            for node in observation.nodes
            if reward_card_name.lower() in node.searchable and node.bounds.area > 0
        ]
        for card in card_nodes:
            nearby_status = [
                node
                for node in observation.nodes
                if node.text in {"已使用", "已生效"}
                and node.bounds.top >= card.bounds.bottom - 5
                and node.bounds.top <= card.bounds.bottom + 180
                and node.bounds.right >= card.bounds.left
                and node.bounds.left <= card.bounds.right
            ]
            if nearby_status:
                return True
        content = " ".join(f"{node.text} {node.content_desc}" for node in observation.nodes)
        return "奖励已绑定" in content and "QQ账号" in content

    def _tap_text_button(self, *labels: str) -> bool:
        observation = self.observe()
        node = find_node(observation.nodes, *labels, clickable_only=True)
        if node is None:
            node = find_node(observation.nodes, *labels)
        if node is None:
            return False
        self._tap_node(node)
        time.sleep(2)
        return True

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        guarded = self._guard_generation(name)
        if guarded is not None:
            return guarded
        try:
            if name == "tap":
                x, y = int(args["x"]), int(args["y"])
                if x >= self.width or y >= self.height or x < 0 or y < 0:
                    raise AgentError(f"坐标越界：{x},{y}")
                self._tap_point(x, y)
                time.sleep(0.8)
                return self._result(True, "已点击指定位置", stage=self.observe().stage)
            if name == "swipe":
                values = [int(args[key]) for key in ("x1", "y1", "x2", "y2")]
                if any(value < 0 for value in values):
                    raise AgentError("滑动坐标不能为负数")
                start = (min(values[0], self.width - 1), min(values[1], self.height - 1))
                end = (min(values[2], self.width - 1), min(values[3], self.height - 1))
                duration = max(100, min(2000, int(args.get("duration_ms", 500))))
                self.device.input("swipe", str(start[0]), str(start[1]), str(end[0]), str(end[1]), str(duration))
                time.sleep(1)
                return self._result(True, "已完成滑动", stage=self.observe().stage)
            if name == "press_back":
                self.device.input("keyevent", "KEYCODE_BACK")
                time.sleep(1)
                return self._result(True, "已返回上一页", stage=self.observe().stage)
            if name == "open_welfare":
                after = self._go_to_welfare()
                return self._result(True, "已进入福利中心", stage=after.stage)
            if name == "input_test_prompt":
                return self._input_fixed_prompt(self.observe())
            if name == "send_message":
                observation = self.observe()
                if observation.stage in {"chat", "writing"}:
                    input_node = self._find_input(observation)
                    input_text = input_node.text.strip() if input_node is not None else ""
                    if not input_text:
                        return self._result(False, "输入框为空，必须先调用 input_test_prompt")
                button = self._find_send(observation)
                if button is None:
                    input_node = self._find_input(observation)
                    if observation.stage == "writing":
                        fallback_y = 754
                    elif input_node is not None:
                        # 输入框通常延伸到屏幕底部；发送箭头位于其右下角，而不是输入框中心。
                        fallback_y = min(
                            self.height - 45,
                            max(self.height // 2, input_node.bounds.bottom - 35),
                        )
                    else:
                        fallback_y = self.height - 60
                    self._tap_point(self.width - 60, fallback_y)
                else:
                    self._tap_node(button)
                self.pending_generation = True
                return self._result(True, "已发送，下一步必须调用 wait_5s", stage=self.observe().stage)
            if name == "wait_5s":
                return self._wait_generation()
            if name == "select_local_image":
                return self._ensure_picker_image()
            if name == "confirm_image":
                if not self._tap_text_button("确认", "使用", "完成"):
                    return self._result(False, "没有找到图片确认按钮")
                return self._result(True, "已确认图片", stage=self.observe().stage)
            if name == "claim_reward":
                if self._tap_text_button("开心收下") or self._tap_text_button("收下"):
                    return self._result(True, "已收下任务奖励", stage=self.observe().stage)
                if self._dismiss_reward_popup():
                    return self._result(True, "已收下任务奖励", stage=self.observe().stage)
                return self._result(True, "未发现奖励弹窗，视为无需领取", stage=self.observe().stage)
            if name == "return_to_welfare":
                after = self._go_to_welfare()
                return self._result(True, "已返回福利中心并刷新任务页面", stage=after.stage)
            if name == "open_exchange":
                return self._open_exchange()
            if name == "redeem_qq_card":
                return self._redeem_card()
            if name == "confirm_exchange":
                if not self.exchange_target_clicked:
                    return self._result(False, "尚未定位并点击目标商品，拒绝确认兑换")
                # 商品列表的“兑换”会先进入详情页；先点击详情页的“立即兑换”。
                clicked = self._tap_text_button("立即兑换")
                after = self.observe()
                # 某些版本在详情页后还会出现二次确认弹窗。
                if self._has_evidence(after, "确认兑换", "确定兑换"):
                    clicked = self._tap_text_button("确认兑换", "确定兑换", "确认") or clicked
                    after = self.observe()
                if not clicked and not self._has_exchange_success(after):
                    return self._result(False, "没有兑换确认弹窗或成功提示", stage=after.stage)
                if not self._has_exchange_success(after):
                    return self._result(False, "兑换后没有发现明确成功证据", stage=after.stage)
                self.exchange_confirmed = True
                self._write_state("pending")
                return self._result(True, "已确认兑换且发现成功证据", stage=after.stage)
            if name == "open_prize_records":
                if not self.exchange_confirmed:
                    return self._result(False, "兑换尚未确认成功")
                clicked = self._tap_text_button("奖品记录", "前往奖品记录")
                after = self.observe()
                if not clicked and after.stage != "prize_records":
                    self._tap_point(self.width - 90, 148)
                    time.sleep(2)
                    after = self.observe()
                if after.stage != "prize_records" and not self._has_evidence(after, "奖品记录"):
                    return self._result(False, "没有进入奖品记录页面", stage=after.stage)
                self.prize_records_opened = True
                return self._result(True, "已打开奖品记录", stage=after.stage)
            if name == "use_bound_reward":
                if not self.prize_records_opened:
                    return self._result(False, "尚未打开奖品记录")
                button = self._find_reward_use_button(self.observe())
                if button is None:
                    return self._result(False, "奖品记录中没有找到目标卡片的使用按钮")
                self._tap_node(button)
                time.sleep(2)
                self.reward_use_clicked = True
                return self._result(True, "已点击绑定账号使用入口", stage=self.observe().stage)
            if name == "confirm_reward_use":
                if not self.reward_use_clicked:
                    return self._result(False, "尚未点击目标卡片的绑定账号使用入口")
                self._tap_text_button("立即使用", "确认使用", "确定", "确认")
                time.sleep(2)
                after = self.observe()
                evidence = self._has_reward_use_success(after)
                self.reward_use_confirmed = evidence
                if not evidence:
                    return self._result(False, "尚未发现绑定账号使用成功的页面证据", stage=after.stage)
                self._write_state("used")
                return self._result(True, "已确认卡片对绑定账号使用成功", stage=after.stage)
            if name == "return_to_ours":
                after = self._go_to_ours()
                return self._result(True, "已返回“我们”页面", stage=after.stage)
            if name == "report_tasks":
                return self._result(True, "已记录模型观察到的任务进度")
            if name == "complete_task":
                return self._result(True, "完成工具调用已收到")
            return self._result(False, f"未知工具：{name}")
        except (KeyError, TypeError, ValueError) as exc:
            return self._result(False, f"工具参数错误：{type(exc).__name__}")
        except AgentError as exc:
            return self._result(False, str(exc)[:240])


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "open_welfare",
            "description": "自动回到元宝的“我们”页并打开福利中心。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_tasks",
            "description": "只读报告福利中心截图中可见的任务进度；必须使用这些固定字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "daily_done": {"type": "boolean"},
                    "question": {"type": "integer", "minimum": 0, "maximum": 3},
                    "writing": {"type": "integer", "minimum": 0, "maximum": 3},
                    "image": {"type": "integer", "minimum": 0, "maximum": 3},
                    "photo_question": {"type": "integer", "minimum": 0, "maximum": 3},
                    "same_template": {"type": "integer", "minimum": 0, "maximum": 3},
                },
                "required": [
                    "daily_done",
                    "question",
                    "writing",
                    "image",
                    "photo_question",
                    "same_template",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tap",
            "description": "点击当前截图中的一个可见位置。只能点击屏幕内坐标。",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "swipe",
            "description": "在当前屏幕内滑动以查看被遮挡的控件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "x2": {"type": "integer"},
                    "y2": {"type": "integer"},
                    "duration_ms": {"type": "integer"},
                },
                "required": ["x1", "y1", "x2", "y2"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_back",
            "description": "返回上一页；只在当前页面没有更明确的返回控件时使用。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "input_test_prompt",
            "description": "输入预置的最简测试文本，不要自行传入文本；提问和写作必须使用它。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "点击当前页面发送按钮。发送后必须立刻调用 wait_5s。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_5s",
            "description": "等待至少 5 秒，并继续检查右下角终止按钮，直到 AI 输出完成或超时。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_local_image",
            "description": "选择程序准备好的本地脱敏测试图片；P 图和拍题都用这个工具。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_image",
            "description": "确认刚才选择的图片，主要用于拍题流程。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claim_reward",
            "description": "如果出现“开心收下”奖励弹窗就点击收下；没有弹窗也可以调用。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "return_to_welfare",
            "description": "完成一次子任务后返回福利中心并刷新任务计数。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_exchange",
            "description": "从福利中心打开兑换商城。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "redeem_qq_card",
            "description": "优先定位配置指定的 QQ 超级会员 1 天卡；若页面没有该商品则查找配置的 3 天卡，积分不足时安全跳过兑换。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_exchange",
            "description": "确认兑换弹窗；只有目标商品已被定位后才能调用。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_prize_records",
            "description": "按兑换成功提示打开绿色的“前往奖品记录”。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_bound_reward",
            "description": "在奖品记录中定位目标卡并点击立即使用绑定账号。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_reward_use",
            "description": "确认绑定账号使用卡片，并要求页面出现成功证据。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "return_to_ours",
            "description": "兑换和使用成功后回到元宝“我们”页面。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "只有所有任务和奖品使用断言都通过后才能调用，调用后本轮结束并等待下一次计划运行。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


def load_prompt(path: Path) -> str:
    if not path.is_file():
        raise AgentError(f"提示词文件不存在：{path}")
    return path.read_text(encoding="utf-8")


class VisionModel:
    """调用 OpenAI Chat Completions，兼容支持或不支持 tools 的 v1 端点。"""

    def __init__(self, config: Config):
        self.config = config
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url)
        self.tools_supported = True
        # 系统提示必须在同一轮请求中完全一致；动态状态放到最新 user 消息末尾。
        # 端点不支持 tools 时也沿用同一份静态协议，避免降级请求改变前缀。
        self.system_prompt = (
            load_prompt(config.prompt_path).rstrip()
            + '\n\n协议兼容说明：若当前端点不支持 tools，请只输出'
            + ' {"tool":"工具名","arguments":{}}；支持 tools 时仍只调用一个工具。'
        )
        self.history: list[dict[str, Any]] = []
        self._pending_turn: dict[str, Any] | None = None
        self._turn_number = 0

    def _request(self, messages: list[dict[str, Any]]) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": messages,
        }
        if self.tools_supported:
            kwargs["tools"] = TOOL_DEFINITIONS
            kwargs["tool_choice"] = "auto"
        try:
            return self.client.chat.completions.create(**kwargs)
        except (BadRequestError, APIError) as exc:
            detail = str(exc).lower()
            tools_unsupported = any(
                marker in detail
                for marker in (
                    "does not support tools",
                    "unsupported tools",
                    "unknown field: tools",
                    "unrecognized field: tools",
                    "tools is not supported",
                    "function calling is not supported",
                )
            )
            if self.tools_supported and isinstance(exc, BadRequestError) and tools_unsupported:
                # 一些 OpenAI 兼容端点实现了视觉输入却没有 function tools，降级为严格 JSON。
                self.tools_supported = False
                return self.client.chat.completions.create(model=self.config.model_id, messages=messages)
            raise exc

    def _with_retries(self, operation: Callable[[], Any], label: str) -> Any:
        last: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                return operation()
            except Exception as exc:  # 上游兼容端点的异常类型不统一，统一进入重试边界。
                last = exc
                if attempt >= self.config.max_retries:
                    break
                print(f"{label}失败（第 {attempt}/{self.config.max_retries} 次），{self.config.retry_cooldown_seconds} 秒后重试")
                time.sleep(self.config.retry_cooldown_seconds)
        raise AgentError(f"{label}连续失败：{type(last).__name__ if last else '未知错误'}") from last

    @staticmethod
    def _result_json(result: ToolResult) -> str:
        payload = {"ok": result.ok, "message": result.message, **result.data}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[
            :MAX_HISTORY_RESULT_CHARS
        ]

    @staticmethod
    def _action_json(name: str, args: dict[str, Any]) -> str:
        return json.dumps(
            {"tool": name, "arguments": args},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _history_summary(context: str, observation: Observation, turn_number: int) -> str:
        # 历史只保留编排状态和页面定位信息；截图、完整 UI 和可能的用户输入不回放。
        summary = (
            f"历史回合 {turn_number} 的观测摘要（仅作上下文，当前页面以最新截图为准）：\n"
            f"编排状态：{context}\n"
            f"Activity：{observation.activity or '(未知)'}\n"
            f"页面阶段：{observation.stage or '(未知)'}"
        )
        return summary[:MAX_HISTORY_SUMMARY_CHARS]

    @staticmethod
    def _current_observation(observation: Observation, context: str) -> dict[str, Any]:
        user_text = (
            "当前屏幕观测（这是本轮唯一的最新页面依据）：\n"
            f"编排状态：{context}\n"
            f"当前 Activity：{observation.activity or '(未知)'}\n"
            f"当前页面阶段：{observation.stage or '(未知)'}\n"
            "以下无障碍层级只作辅助，截图是最终依据：\n"
            f"{observation.ui or '(无可用层级信息)'}"
        )
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(observation.image).decode("ascii"),
                        "detail": "high",
                    },
                },
            ],
        }

    def next_tool(self, observation: Observation, context: str) -> tuple[str, dict[str, Any], str]:
        user_message = self._current_observation(observation, context)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *self.history,
            user_message,
        ]
        response = self._with_retries(lambda: self._request(messages), "视觉模型调用")
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if len(tool_calls) > 1:
            raise AgentError("模型一次返回了多个工具调用，已拒绝以保持单步状态可验证")
        if tool_calls:
            call = tool_calls[0]
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise AgentError("模型工具参数不是有效 JSON") from exc
            if not isinstance(args, dict):
                raise AgentError("模型工具参数必须是 JSON 对象")
            self._pending_turn = {
                "context": context,
                "observation": observation,
                "name": call.function.name,
                "args": args,
            }
            return call.function.name, args, call.id
        content = message.content or ""
        action = self._parse_json_tool(content)
        call_id = "fallback-current"
        self._pending_turn = {
            "context": context,
            "observation": observation,
            "name": action[0],
            "args": action[1],
        }
        return action[0], action[1], call_id

    @staticmethod
    def _parse_json_tool(content: str) -> tuple[str, dict[str, Any]]:
        candidate = content.strip()
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise AgentError(f"模型没有返回工具调用 JSON：{content[:160]!r}") from exc
        if "tool" in value:
            name = value["tool"]
            args = value.get("arguments", {})
        elif value.get("action") == "tap":
            name, args = "tap", {"x": value.get("x"), "y": value.get("y")}
        elif value.get("action") == "swipe":
            name, args = "swipe", value
        elif value.get("action") == "wait":
            name, args = "wait_5s", {}
        elif value.get("action") == "done":
            name, args = "complete_task", {}
        elif value.get("action") == "type":
            name, args = "input_test_prompt", {}
        else:
            raise AgentError("模型 JSON 没有可识别的工具")
        if not isinstance(name, str) or not isinstance(args, dict):
            raise AgentError("模型工具名或参数类型错误")
        return name, args

    def record_tool_result(self, call_id: str, result: ToolResult) -> None:
        pending = self._pending_turn
        if pending is None:
            return
        self._turn_number += 1
        observation = pending["observation"]
        self.history.extend(
            [
                {
                    "role": "user",
                    "content": self._history_summary(
                        pending["context"], observation, self._turn_number
                    ),
                },
                {
                    "role": "assistant",
                    "content": self._action_json(pending["name"], pending["args"]),
                },
                {
                    "role": "user",
                    "content": (
                        "编排器已执行上一动作，结果如下；下一步必须以最新屏幕观测为准：\n"
                        + self._result_json(result)
                    ),
                },
            ]
        )
        self._pending_turn = None


@dataclass
class Workflow:
    config: Config
    executor: ToolExecutor
    daily_done: bool | None = None
    progress: dict[str, int | None] = field(default_factory=lambda: {key: None for key in TASK_ORDER})
    phase: str = "navigate_welfare"
    target: str | None = None
    substate: dict[str, bool] = field(default_factory=dict)
    expected_count: int | None = None
    stale_reports: int = 0
    finished: bool = False

    def context(self, observation: Observation) -> str:
        progress = ", ".join(
            f"{TASK_LABELS[key]}={self.progress.get(key) if self.progress.get(key) is not None else '?'} / 3"
            for key in TASK_ORDER
        )
        return (
            f"阶段={self.phase}; 当前目标={self.target or '无'}; 今日问元宝已完成={self.daily_done}; "
            f"任务进度={progress}; 奖励使用证据={self.executor.reward_use_confirmed}"
        )

    def _parse_report(self, args: dict[str, Any]) -> ToolResult:
        try:
            daily_done = bool(args["daily_done"])
            values = {key: int(args[key]) for key in TASK_ORDER}
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(False, "report_tasks 缺少固定进度字段")
        if any(value < 0 or value > 3 for value in values.values()):
            return ToolResult(False, "任务进度必须在 0 到 3 之间")
        if self.target and self.expected_count is not None and self.target in TASK_LABELS:
            current = values[self.target]
            if current < self.expected_count:
                self.stale_reports += 1
                self.progress.update(values)
                self.daily_done = daily_done
                self.phase = "open_target"
                return ToolResult(
                    False,
                    f"任务 {TASK_LABELS[self.target]} 当前仍为 {current}/3，必须重新点击入口完成下一次",
                )
        self.progress.update(values)
        self.daily_done = daily_done
        self.stale_reports = 0
        self._select_next()
        return ToolResult(True, "已更新福利中心任务进度")

    def _select_next(self) -> None:
        if not self.daily_done:
            self.target = "daily_question"
            self.phase = "open_target"
            self.expected_count = None
            self.substate = {}
            return
        for key in TASK_ORDER:
            count = self.progress.get(key)
            if count is None or count < 3:
                self.target = key
                self.phase = "open_target"
                self.expected_count = min(3, (count or 0) + 1) if count is not None else 1
                self.substate = {}
                return
        self.target = None
        self.expected_count = None
        self.phase = "open_exchange"
        self.substate = {}

    def _task_kind(self) -> str:
        return self.target or ""

    def _perform_complete(self) -> bool:
        kind = self._task_kind()
        if kind in {"daily_question", "question", "writing"}:
            return all(self.substate.get(name, False) for name in ("input", "sent", "waited"))
        if kind == "image":
            return all(self.substate.get(name, False) for name in ("image", "sent", "waited"))
        if kind == "photo_question":
            return all(self.substate.get(name, False) for name in ("image", "confirmed", "waited"))
        if kind == "same_template":
            return all(
                self.substate.get(name, False)
                for name in ("entry_clicked", "template_clicked", "sent", "waited")
            )
        return False

    def _valid_phase(self, name: str) -> str | None:
        allowed: dict[str, set[str]] = {
            "navigate_welfare": {"open_welfare"},
            "report": {"report_tasks"},
            "open_target": {"tap", "swipe", "press_back"},
            "perform": {
                "tap",
                "swipe",
                "press_back",
                "input_test_prompt",
                "send_message",
                "wait_5s",
                "select_local_image",
                "confirm_image",
            },
            "claim": {"claim_reward"},
            "return": {"return_to_welfare"},
            "open_exchange": {"open_exchange"},
            "redeem": {"redeem_qq_card"},
            "exchange_confirm": {"confirm_exchange"},
            "prize_records": {"open_prize_records"},
            "use_reward": {"use_bound_reward"},
            "use_confirm": {"confirm_reward_use"},
            "return_ours": {"return_to_ours"},
            "complete": {"complete_task"},
        }
        if name not in allowed.get(self.phase, set()):
            return f"当前阶段 {self.phase} 不应调用 {name}"
        return None

    @staticmethod
    def _ignored_welfare_tap(args: dict[str, Any], observation: Observation) -> bool:
        """在福利中心拦截唯一明确忽略的邀请任务整行点击。"""
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        for node in observation.nodes:
            label = node.searchable
            if not any(marker in label for marker in ("邀请新用户", "去邀请")):
                continue
            if node.bounds.top - 55 <= y <= node.bounds.bottom + 55:
                return True
        return False

    @staticmethod
    def _tap_hits_label(args: dict[str, Any], observation: Observation, *markers: str) -> bool:
        """判断模型点击是否落在当前观测中带指定语义的控件附近。"""
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        wanted = tuple(marker.lower() for marker in markers)
        for node in observation.nodes:
            if not node.enabled or node.bounds.area <= 0:
                continue
            if not any(marker in node.searchable for marker in wanted):
                continue
            if (
                node.bounds.left - 45 <= x <= node.bounds.right + 45
                and node.bounds.top - 45 <= y <= node.bounds.bottom + 45
            ):
                return True
        return False

    def _complete_task_assertions(self) -> ToolResult:
        if self.daily_done is not True:
            return ToolResult(False, "每日问元宝尚未确认完成")
        incomplete = [TASK_LABELS[key] for key in TASK_ORDER if self.progress.get(key) != 3]
        if incomplete:
            return ToolResult(False, f"仍有未完成任务：{'、'.join(incomplete)}")
        reward_unavailable = bool(getattr(self.executor, "reward_unavailable", False))
        if not self.executor.reward_use_confirmed and not reward_unavailable:
            return ToolResult(False, "尚未确认 QQ 超级会员卡已对绑定账号使用成功")
        if self.phase != "complete":
            return ToolResult(False, "尚未回到“我们”页面")
        self.finished = True
        if reward_unavailable:
            return ToolResult(True, "每日任务已完成；备用兑换商品积分不足，已返回“我们”页面")
        return ToolResult(True, "每日任务与奖品使用均已完成，本轮进入下一次等待")

    def dispatch(self, name: str, args: dict[str, Any], observation: Observation) -> ToolResult:
        phase_error = self._valid_phase(name)
        if phase_error:
            return ToolResult(False, phase_error)
        if (
            name in {"tap", "swipe", "press_back"}
            and self.target in {"image", "photo_question"}
            and observation.stage == "picker"
        ):
            return ToolResult(False, "当前已在系统图片选择器，必须调用 select_local_image 选择测试图片")
        if name == "tap" and observation.stage == "welfare" and self._ignored_welfare_tap(args, observation):
            return ToolResult(False, "已拒绝点击“邀请新用户”任务")
        if name == "tap" and self.target == "same_template":
            if self.phase == "open_target" and not self._tap_hits_label(args, observation, "做同款"):
                return ToolResult(False, "请点击福利中心中“做同款”任务入口")
            if self.phase == "perform":
                if self.substate.get("template_clicked", False):
                    return ToolResult(False, "推荐模板已经点击，下一步应直接发送并等待生成")
                if not self.substate.get("entry_clicked", False):
                    if observation.stage != "welfare" or not self._tap_hits_label(args, observation, "做同款"):
                        return ToolResult(False, "请先点击福利中心中“做同款”任务入口")
                elif not self._tap_hits_label(args, observation, "做同款"):
                    return ToolResult(False, "请在推荐模板页点击任意一个“做同款”按钮")
        if name == "send_message" and self.target in {"daily_question", "question", "writing"}:
            if not self.substate.get("input", False):
                return ToolResult(False, "当前提问/写作任务尚未确认固定测试文本，必须先调用 input_test_prompt")
        if name == "send_message" and self.target == "image":
            if not self.substate.get("image", False):
                return ToolResult(False, "当前 P 图任务尚未确认图片，必须先调用 select_local_image")
        if name == "send_message" and self.target == "same_template":
            if not self.substate.get("template_clicked", False):
                return ToolResult(False, "尚未点击推荐模板，不能发送")
            if (
                self.executor._find_input(observation) is None
                and self.executor._find_send(observation) is None
                and "aitemplatedetailactivity" not in observation.activity.lower()
                and observation.stage not in {"app", "chat"}
            ):
                return ToolResult(False, "模板详情页尚未加载输入框或发送控件，请重新观察后再发送")
        if name == "confirm_image" and self.target in {"image", "photo_question"}:
            if not self.substate.get("image", False):
                return ToolResult(False, "尚未选择图片，不能确认图片")
        if name == "wait_5s" and self.target in {
            "daily_question",
            "question",
            "writing",
            "image",
            "photo_question",
            "same_template",
        }:
            if not self.substate.get("sent", False):
                if self.target == "photo_question" and self.substate.get("confirmed", False):
                    # 拍题有的版本确认图片后自动提交；若当前版本仍显示发送按钮，不能跳过。
                    if self.executor._find_send(observation) is not None:
                        return ToolResult(False, "拍题页面仍有发送按钮，必须先调用 send_message")
                else:
                    return ToolResult(False, "当前任务尚未发送，必须先调用 send_message")
        if name == "report_tasks":
            result = self._parse_report(args)
            return result
        if name == "complete_task":
            return self._complete_task_assertions()
        result = self.executor.execute(name, args)
        if not result.ok:
            return result
        if self.phase == "navigate_welfare" and name == "open_welfare":
            self.phase = "report"
        elif self.phase == "open_target" and name in {"tap", "swipe", "press_back"}:
            self.phase = "perform"
            self.substate = {}
            if self.target == "same_template" and name == "tap":
                self.substate["entry_clicked"] = True
        elif self.phase == "perform":
            if name == "input_test_prompt":
                self.substate["input"] = True
            elif name == "select_local_image":
                self.substate["image"] = True
            elif name == "confirm_image":
                self.substate["confirmed"] = True
            elif name == "tap" and self.target == "same_template":
                if not self.substate.get("entry_clicked", False):
                    self.substate["entry_clicked"] = True
                else:
                    self.substate["template_clicked"] = True
            elif name == "send_message":
                self.substate["sent"] = True
            elif name == "wait_5s":
                self.substate["waited"] = True
            if self._perform_complete():
                self.phase = "claim"
        elif self.phase == "claim" and name == "claim_reward":
            self.phase = "return"
        elif self.phase == "return" and name == "return_to_welfare":
            self.phase = "report"
        elif self.phase == "open_exchange" and name == "open_exchange":
            self.phase = "redeem"
        elif self.phase == "redeem" and name == "redeem_qq_card":
            if (
                result.data.get("reward_use_confirmed")
                or self.executor.reward_use_confirmed
                or result.data.get("reward_unavailable")
                or getattr(self.executor, "reward_unavailable", False)
            ):
                self.phase = "return_ours"
            elif result.data.get("prize_records_opened") is False:
                self.phase = "prize_records"
            else:
                self.phase = "exchange_confirm"
        elif self.phase == "exchange_confirm" and name == "confirm_exchange":
            self.phase = "prize_records"
        elif self.phase == "prize_records" and name == "open_prize_records":
            self.phase = "use_reward"
        elif self.phase == "use_reward" and name == "use_bound_reward":
            self.phase = "use_confirm"
        elif self.phase == "use_confirm" and name == "confirm_reward_use":
            if self.executor.reward_use_confirmed:
                self.phase = "return_ours"
        elif self.phase == "return_ours" and name == "return_to_ours":
            self.phase = "complete"
        return result


def run_once(config: Config) -> None:
    runtime = WaydroidRuntime()
    completed = False
    device: Device | None = None
    previous_stay_awake: str | None = None
    try:
        device = runtime.discover_device(config.device)
        print(f"设备={device.serial} 模型={config.model_id} 端点={config.base_url}")
        if runtime.managed:
            # 后台会话没有可见窗口，Android 屏幕超时会通过 Waydroid hardware 服务冻结容器。
            previous_stay_awake = device.keep_awake()
        device.launch_app(PACKAGE_NAME)
        executor = ToolExecutor(config, device)
        workflow = Workflow(config, executor)
        model = VisionModel(config)
        # 首步由本地编排器固定执行，避免模型在尚未建立福利中心状态时直接报告或点击任务。
        bootstrap = workflow.dispatch("open_welfare", {}, executor.observe())
        print(f"步骤 0：工具=open_welfare 结果={'成功' if bootstrap.ok else '失败'}；{bootstrap.message[:180]}")
        if not bootstrap.ok:
            raise AgentError(bootstrap.message)
        failures = 0
        previous_state: str | None = None
        previous_action: str | None = None
        for step in range(1, config.max_steps + 1):
            observation = executor.observe()
            current_state = state_signature(observation.ui, observation.image, observation.activity)
            try:
                name, args, call_id = model.next_tool(observation, workflow.context(observation))
            except AgentError as exc:
                failures += 1
                print(f"步骤 {step}：模型动作解析失败（{str(exc)[:160]}）")
                if failures >= config.max_retries:
                    raise
                time.sleep(config.retry_cooldown_seconds)
                continue
            signature = action_signature(name, args)
            if signature == previous_action and current_state == previous_state and name not in {"wait_5s"}:
                failures += 1
                result = ToolResult(False, "页面状态未变化且重复同一动作，已拒绝循环点击")
            else:
                result = workflow.dispatch(name, args, observation)
                failures = failures + 1 if not result.ok else 0
            previous_state, previous_action = current_state, signature
            model.record_tool_result(call_id, result)
            safe_args = "" if name not in {"report_tasks"} else json.dumps(args, ensure_ascii=False, sort_keys=True)
            print(f"步骤 {step}：工具={name} {safe_args} 结果={'成功' if result.ok else '失败'}；{result.message[:180]}")
            if workflow.finished:
                completed = True
                return
            if not result.ok:
                if failures >= config.max_retries:
                    raise AgentError(f"连续 {failures} 次工具/状态失败，停止本轮")
                if config.retry_cooldown_seconds:
                    time.sleep(config.retry_cooldown_seconds)
        raise AgentError(f"达到单轮最大步骤数 {config.max_steps}，未完成每日任务")
    finally:
        if device is not None and previous_stay_awake is not None:
            with contextlib.suppress(AgentError):
                device.restore_keep_awake(previous_stay_awake)
        if completed and config.stop_waydroid_after_run and runtime.managed:
            print("每日任务已完成，正在关闭 Waydroid 以释放内存")
            if not runtime.stop():
                print("警告：Waydroid 未确认完全停止，下次运行前仍会再次尝试启动")


def next_run_at(config: Config, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(config.timezone_name)
    if now is None:
        current = datetime.now(tz)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=tz)
    else:
        current = now.astimezone(tz)
    hour, minute = parse_run_time(config.run_time)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return target


def run_daemon(config: Config) -> None:
    tz = ZoneInfo(config.timezone_name)
    while True:
        target = next_run_at(config)
        seconds = max(0.5, (target - datetime.now(tz)).total_seconds())
        print(f"下一次运行：{target.isoformat()}")
        time.sleep(seconds)
        started = datetime.now(tz)
        print(f"开始每日任务：{started.isoformat()}")
        try:
            run_once(config)
            print("本轮完成，进入下一轮等待")
        except Exception as exc:
            print(f"本轮失败：{type(exc).__name__}；下一次计划时间仍按配置计算")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="元宝每日任务视觉代理")
    parser.add_argument("--once", action="store_true", help="立即运行一轮")
    parser.add_argument("--daemon", action="store_true", help="按 .env 中的北京时间运行时间持续调度")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)
    try:
        config = Config.from_env(args.env_file)
        if args.daemon:
            run_daemon(config)
        else:
            run_once(config)
        return 0
    except KeyboardInterrupt:
        print("已中止。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"失败：{type(exc).__name__}；{str(exc)[:300]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
