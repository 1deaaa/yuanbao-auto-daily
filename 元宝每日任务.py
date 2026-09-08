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
import struct
import subprocess
import sys
import time
import zlib
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
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_REASONING_EFFORT = "high"
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_RUN_TIME = "00:05"
DEFAULT_TEST_PROMPT = "healthy habits"
DEFAULT_TEST_IMAGE = str(ROOT / "测试题目.png")
PACKAGE_NAME = "com.tencent.hunyuan.app.chat"
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_COOLDOWN = 10
DEFAULT_MAX_STEPS = 260
DEFAULT_GENERATION_TIMEOUT = 180
DEFAULT_MODEL_REQUEST_TIMEOUT = 180
DEFAULT_CARD_NAME = "QQ超级会员3天卡"
DEFAULT_STATE_PATH = ROOT / ".yuanbao_daily_state.json"
DEFAULT_STOP_WAYDROID_AFTER_RUN = True
DEFAULT_AUTO_ACCEPT_PROTOCOL = True
# 这些工具的动作和成功条件完全由本地执行器掌握，不需要为每一步再请求视觉模型。
LOCAL_DETERMINISTIC_TOOLS = frozenset(
    {
        "input_test_prompt",
        "send_message",
        "wait_5s",
        "select_local_image",
        "confirm_image",
        "claim_reward",
        "return_to_welfare",
        "open_exchange",
        "redeem_qq_card",
        "confirm_exchange",
        "open_prize_records",
        "use_bound_reward",
        "confirm_reward_use",
        "return_to_ours",
        "complete_task",
    }
)
# 页面重绘、输入法和奖励遮罩都可能让一次本地动作暂时失败；这些动作自身
# 有前置/后置校验，允许在同一画面上按 MAX_RETRIES 有界重试。视觉坐标点击
# 仍然严格拒绝同画面重复，避免模型坐标漂移造成连续误点。
RETRYABLE_SAME_STATE_TOOLS = LOCAL_DETERMINISTIC_TOOLS | {"wait_5s"}
WAYDROID_START_TIMEOUT = 90
WAYDROID_READY_TIMEOUT = 45
WAYDROID_STOP_TIMEOUT = 45
WAYDROID_POLL_INTERVAL = 1.0
APP_RESOLVE_TIMEOUT = 45
APP_RESOLVE_POLL_INTERVAL = 1.0
APP_READY_TIMEOUT = 90
APP_READY_POLL_INTERVAL = 1.0
STARTUP_OBSERVE_TIMEOUT = 45
STARTUP_OBSERVE_POLL_INTERVAL = 1.0
UI_DUMP_ATTEMPTS = 3
UI_DUMP_TIMEOUT = 8
UI_DUMP_RETRY_DELAY = 0.5
UI_DUMP_READ_ATTEMPTS = 4
UI_DUMP_READ_DELAY = 0.25
# 福利中心是 Chromium WebView，未压缩层级在重绘期间容易拖住 uiautomator。
# Android 的 uiautomator dump 从 API 18 起支持该选项，统一使用压缩树即可保留
# 文本、资源 ID 和边界等本项目所需字段，同时显著减少 ADB 传输量。
UI_DUMP_OPTIONS = ("--compressed",)
WEBVIEW_ACTIVITY_MARKER = "webbrowseractivity"
WEBVIEW_UI_DUMP_ATTEMPTS = 1
WEBVIEW_UI_DUMP_TIMEOUT = 3
ANDROID_WARNING_CHECK_INTERVAL = 3.0
ANDROID_WARNING_WINDOW_TITLE = "Android 系统"
ANDROID_WARNING_WINDOW_TYPE = "ty=SYSTEM_ERROR"
ANDROID_WARNING_WINDOW_FRAME = re.compile(
    r"^\s*Frames:.*?frame=\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
    re.MULTILINE,
)
APPSTORE_PACKAGE_NAME = "com.tencent.android.qqdownloader"
APP_ANR_WINDOW_MARKERS = ("Application Not Responding", "没有响应", "无响应")
ADB_TRANSPORT_FAILURE_MARKERS = (
    "adb 超时",
    "adb: device offline",
    "device offline",
    "cannot connect to",
    "transport endpoint is not connected",
    "broken pipe",
)
OURS_NAV_TIMEOUT = 30
OURS_NAV_POLL_INTERVAL = 1.0
OURS_FALLBACK_DELAY = 8.0
# 750x1333 实测首页底部“我们”入口的归一化位置；只用于已确认的首页空层级。
OURS_FALLBACK_X_RATIO = 0.852
OURS_FALLBACK_Y_RATIO = 0.963
WELFARE_LOAD_TIMEOUT = 30
WELFARE_LOAD_POLL_INTERVAL = 1.5
WELFARE_WEBVIEW_READY_DELAY = 3.0
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


def read_daily_state(path: Path) -> dict[str, Any]:
    """读取可恢复的当天状态；文件损坏时按空状态处理。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_daily_state(path: Path, state: dict[str, Any]) -> None:
    """原子写入非敏感状态，避免进程中断留下半个 JSON。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        # 状态文件只是恢复优化，不能阻断当前已经完成的页面操作。
        print(f"状态文件写入失败：{type(exc).__name__}")


def png_pixel(image: bytes, x: int, y: int) -> tuple[int, int, int, int] | None:
    """读取 PNG 的一个像素；只支持本项目截图使用的 8 位 RGB/RGBA 格式。"""
    if not image.startswith(b"\x89PNG\r\n\x1a\n") or x < 0 or y < 0:
        return None
    try:
        offset = 8
        width = height = color_type = bit_depth = None
        compressed = bytearray()
        while offset + 12 <= len(image):
            length = struct.unpack(">I", image[offset : offset + 4])[0]
            kind = image[offset + 4 : offset + 8]
            body_start = offset + 8
            body_end = body_start + length
            if body_end + 4 > len(image):
                return None
            body = image[body_start:body_end]
            offset = body_end + 4
            if kind == b"IHDR":
                width, height, bit_depth, color_type, _, _, _ = struct.unpack(
                    ">IIBBBBB", body
                )
            elif kind == b"IDAT":
                compressed.extend(body)
            elif kind == b"IEND":
                break
        if (
            width is None
            or height is None
            or bit_depth != 8
            or color_type not in {2, 6}
            or x >= width
            or y >= height
        ):
            return None
        channels = 4 if color_type == 6 else 3
        stride = width * channels
        raw = zlib.decompress(bytes(compressed))
        previous = bytearray(stride)
        cursor = 0
        for row_index in range(y + 1):
            filter_type = raw[cursor]
            cursor += 1
            current = bytearray(raw[cursor : cursor + stride])
            cursor += stride
            for index in range(stride):
                left = current[index - channels] if index >= channels else 0
                above = previous[index]
                upper_left = previous[index - channels] if index >= channels else 0
                if filter_type == 1:
                    current[index] = (current[index] + left) & 255
                elif filter_type == 2:
                    current[index] = (current[index] + above) & 255
                elif filter_type == 3:
                    current[index] = (current[index] + ((left + above) // 2)) & 255
                elif filter_type == 4:
                    estimate = left + above - upper_left
                    distance_left = abs(estimate - left)
                    distance_above = abs(estimate - above)
                    distance_upper_left = abs(estimate - upper_left)
                    predictor = (
                        left
                        if distance_left <= distance_above
                        and distance_left <= distance_upper_left
                        else (
                            above
                            if distance_above <= distance_upper_left
                            else upper_left
                        )
                    )
                    current[index] = (current[index] + predictor) & 255
                elif filter_type != 0:
                    return None
            previous = current
        start = x * channels
        values = tuple(current[start : start + channels])
        return values if channels == 4 else (*values, 255)
    except (IndexError, struct.error, ValueError, zlib.error):
        return None

# 任务的固定子步骤集中维护。入口坐标和任务进度仍交给视觉模型，
# 进入具体能力页后的动作由本地执行器按这里的配方稳定完成。
TASK_STEPS: dict[str, tuple[str, ...]] = {
    "daily_question": ("input", "sent", "waited"),
    "question": ("input", "sent", "waited"),
    "writing": ("input", "sent", "waited"),
    "image": ("image", "sent", "waited"),
    "photo_question": ("image", "confirmed", "waited"),
    "same_template": ("entry_clicked", "template_clicked", "sent", "waited"),
}
TEXT_TASKS = frozenset({"daily_question", "question", "writing"})
IMAGE_TASKS = frozenset({"image", "photo_question"})

# 入口缓存只在当前运行期间有效，不写入磁盘。布局变化或语义校验失败时，
# cached_action 会返回 None，下一步仍由视觉模型重新定位。
TASK_ENTRY_MARKERS: dict[str, tuple[str, ...]] = {
    "daily_question": ("每日问元宝", "去提问", "问元宝"),
    "question": ("问元宝任意问题", "去提问", "提问"),
    "writing": ("去写作", "写作"),
    "image": ("去p图", "p图", "智能p图"),
    "photo_question": ("去拍题", "拍题", "拍照答题"),
    "same_template": ("做同款",),
}

# 本机 900x1600 竖屏福利 WebView 的固定任务行按钮位置。只在无障碍树为空、
# Activity 明确是福利 WebView 且处于已验证的竖屏比例时启用；其它分辨率仍交给模型定位。
WELFARE_FIXED_ENTRY_RATIOS: dict[str, tuple[float, float]] = {
    "daily_question": (0.895, 0.271),
    "question": (0.895, 0.665),
    "writing": (0.895, 0.751),
    "image": (0.895, 0.836),
    "photo_question": (0.895, 0.922),
}
# 本机福利页向上滚动一次后，“做同款”任务按钮稳定出现在第五行。
# 只在已执行该次滚动、无障碍树为空且手机纵向比例时启用。
SAME_TEMPLATE_FIXED_ENTRY_RATIOS = (0.895, 0.846)
class AgentError(RuntimeError):
    """代理可以报告给模型、并由外层重试的错误。"""


class ManualActionRequired(AgentError):
    """需要用户先完成登录、验证码或扫码，不能靠自动重试解决。"""


class AppUnresponsiveError(AgentError):
    """应用启动阶段无响应，可以通过重启应用或 Waydroid 会话恢复。"""


def is_adb_transport_failure(error: BaseException) -> bool:
    """判断是否应丢弃本轮 Waydroid 会话并让 systemd 从干净实例重试。"""
    text = str(error).lower()
    return any(marker in text for marker in ADB_TRANSPORT_FAILURE_MARKERS)


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


def _env_choice(name: str, default: str, choices: set[str] | frozenset[str]) -> str:
    value = _first_env(name, default=default).lower()
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise AgentError(f"配置 {name} 必须是以下值之一：{allowed}")
    return value


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
    reasoning_effort: str
    base_url: str
    api_key: str
    device: str
    stop_waydroid_after_run: bool
    auto_accept_protocol: bool
    run_time: str
    timezone_name: str
    max_retries: int
    retry_cooldown_seconds: int
    max_steps: int
    generation_timeout_seconds: int
    model_request_timeout_seconds: int
    test_prompt: str
    test_image_path: Path
    card_name: str
    prompt_path: Path
    state_path: Path

    @classmethod
    def from_env(cls, env_path: Path = ROOT / ".env") -> "Config":
        # 环境变量优先于 .env，便于 systemd、容器和临时测试覆盖配置。
        if env_path.exists():
            load_dotenv(env_path, override=False)
        model_id = _first_env("MODEL_ID", "LOCAL_LLM_MODEL", default=DEFAULT_MODEL)
        reasoning_effort = _env_choice(
            "REASONING_EFFORT", DEFAULT_REASONING_EFFORT, REASONING_EFFORTS
        )
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
            reasoning_effort=reasoning_effort,
            base_url=base_url,
            api_key=api_key,
            device=_first_env("ANDROID_DEVICE", default=DEFAULT_DEVICE),
            stop_waydroid_after_run=_env_bool(
                "STOP_WAYDROID_AFTER_RUN", DEFAULT_STOP_WAYDROID_AFTER_RUN
            ),
            # 只自动确认元宝首次协议页；登录、验证码和扫码仍需人工完成。
            auto_accept_protocol=_env_bool(
                "AUTO_ACCEPT_PROTOCOL", DEFAULT_AUTO_ACCEPT_PROTOCOL
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
            model_request_timeout_seconds=_env_int(
                "MODEL_REQUEST_TIMEOUT_SECONDS", DEFAULT_MODEL_REQUEST_TIMEOUT, minimum=5
            ),
            test_prompt=_first_env("TEST_PROMPT", default=DEFAULT_TEST_PROMPT),
            test_image_path=test_image,
            # 兑换目标固定为三天卡，避免配置或模型误选其它商品。
            card_name=DEFAULT_CARD_NAME,
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


def repeated_action_is_stuck(
    name: str,
    signature: str,
    state: str,
    previous_action: str | None,
    previous_state: str | None,
) -> bool:
    """只拦截视觉重复点击；本地幂等动作交给外层有界重试。"""
    return (
        signature == previous_action
        and state == previous_state
        and name not in RETRYABLE_SAME_STATE_TOOLS
    )


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


@dataclass(frozen=True)
class Observation:
    image: bytes
    ui_xml: str
    ui: str
    nodes: tuple[Node, ...]
    activity: str
    stage: str


def parse_local_welfare_progress(observation: Observation) -> dict[str, Any] | None:
    """从完整福利层级读取进度；层级不完整时返回 None 交给视觉模型。"""
    if observation.stage != "welfare" or not observation.nodes:
        return None
    markers = {
        "question": ("问元宝问题", "问元宝任意问题"),
        "writing": ("使用写作能力", "去写作"),
        "image": ("使用p图能力", "去p图"),
        "photo_question": ("使用拍题能力", "去拍题"),
        "same_template": ("使用推荐模板做同款", "做同款"),
    }
    nodes = tuple(observation.nodes)
    progress: dict[str, int] = {}
    for key, wanted_markers in markers.items():
        label_nodes = [
            node
            for node in nodes
            if any(marker.lower() in node.searchable for marker in wanted_markers)
        ]
        if not label_nodes:
            return None
        for label in label_nodes:
            nearby = [
                node
                for node in nodes
                if abs(node.bounds.center[0] - label.bounds.center[0]) <= 420
                and label.bounds.top - 20 <= node.bounds.top <= label.bounds.bottom + 150
            ]
            text = " ".join(node.searchable for node in (label, *nearby))
            match = re.search(r"(?:已完成|完成)?\s*([0-3])\s*/\s*3", text)
            if match is not None:
                progress[key] = int(match.group(1))
                break
        if key not in progress:
            return None
    all_text = " ".join(node.searchable for node in nodes)
    daily_done = "今日已完成" in all_text or progress["question"] >= 3
    return {"daily_done": daily_done, **progress}


def detect_stage(nodes: Iterable[Node], activity: str) -> str:
    node_list = list(nodes)
    text = " ".join(node.searchable for node in node_list)
    activity_lower = activity.lower()
    # 更新遮罩里的功能说明也会出现“拍题”等词，必须先标为应用阻塞层。
    if any(
        node.resource_id.endswith(":id/upgrade_dialog")
        or node.resource_id.endswith(":id/skip")
        for node in node_list
    ):
        return "app"
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
    if "兑换商城" in text or "qq超级会员3天卡" in text:
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

    def __init__(
        self,
        config: Config,
        device: Device,
        app_launcher: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.device = device
        self.app_launcher = app_launcher or device.launch_app
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
        # 元宝的每日任务会忽略完全相同的问题；工作流在每个任务轮次设置
        # 一个短的本地变体标记，避免把相同测试文本重复提交成无效动作。
        self.prompt_variant: str | None = None
        self._welfare_scroll_normalized = False
        self._welfare_context = False

    def set_prompt_variant(self, variant: str | None) -> None:
        """设置当前任务轮次的唯一文本后缀，不把它交给视觉模型决定。"""
        self.prompt_variant = variant or None

    def reset_welfare_scroll(self) -> None:
        """福利页每次重新打开后都重新校准滚动位置。"""
        self._welfare_scroll_normalized = False

    def normalize_welfare_scroll(self) -> None:
        """无障碍树为空时把福利任务列表拉回顶部，保证固定行坐标可复用。"""
        if getattr(self, "_welfare_scroll_normalized", False):
            return
        self.device.input(
            "swipe",
            str(self.width // 2),
            str(round(self.height * 0.38)),
            str(self.width // 2),
            str(round(self.height * 0.86)),
            "500",
        )
        time.sleep(1.2)
        self._welfare_scroll_normalized = True
        print("福利 WebView 空层级，已将任务列表滚动位置标准化")

    def _read_state(self) -> dict[str, Any]:
        return read_daily_state(self.config.state_path)

    def _write_state(self, exchange_status: str, card_name: str | None = None) -> None:
        """保存仅用于同日恢复的非敏感状态，不写入账号、令牌或截图。"""
        path = self.config.state_path
        state = self._read_state()
        if state.get("date") != self.today:
            state = {}
        state.update(
            {
                "date": self.today,
                "card": card_name or self.reward_card_name,
                "exchange_status": exchange_status,
            }
        )
        write_daily_state(path, state)

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

    def _dismiss_upgrade_prompt(self, nodes: Iterable[Node]) -> bool:
        """关闭元宝冷启动时的版本更新遮罩，避免遮住底部导航。"""
        dialog = find_node(nodes, f"{PACKAGE_NAME}:id/upgrade_dialog")
        title = find_node(nodes, "元宝新版本", exact=True)
        if dialog is None and title is None:
            return False
        skip = find_node(nodes, f"{PACKAGE_NAME}:id/skip", clickable_only=True)
        if skip is None:
            return False
        self._tap_node(skip)
        time.sleep(0.8)
        return True

    @staticmethod
    def _is_protocol_prompt(nodes: Iterable[Node], activity: str) -> bool:
        """只识别元宝首次协议页，不把普通登录页的隐私链接当成协议弹窗。"""
        if "hyloginmainactivity" not in activity.lower():
            return False
        return find_node(nodes, "同意并继续", exact=True) is not None

    def _dismiss_protocol_prompt(self, nodes: Iterable[Node], activity: str) -> bool:
        """精确自动确认元宝协议；按钮缺失时交由人工阻塞保护处理。"""
        if not getattr(self.config, "auto_accept_protocol", False):
            return False
        if not self._is_protocol_prompt(nodes, activity):
            return False
        # Compose 常把 clickable 标记放在无文字的父 View 上，文字节点本身为不可点击；
        # 精确命中文案后点击其中心仍落在父按钮范围内，不能因此把协议页误判为人工阻塞。
        button = find_node(nodes, "同意并继续", clickable_only=True, exact=True)
        if button is None:
            button = find_node(nodes, "同意并继续", exact=True)
        if button is None:
            return False
        self._tap_node(button)
        time.sleep(1.0)
        return True

    @staticmethod
    def _manual_blocker(observation: Observation) -> str | None:
        """识别必须由用户处理的协议或登录页面，避免无意义地重启重试。"""
        labels = " ".join(
            f"{node.text} {node.content_desc}" for node in observation.nodes
        ).lower()
        if ToolExecutor._is_protocol_prompt(observation.nodes, observation.activity):
            return "元宝协议页未能自动确认，请先人工点击“同意并继续”"
        if any(
            marker in labels
            for marker in ("手机号登录", "微信登录", "qq登录", "验证码登录", "获取验证码")
        ):
            return "元宝当前未登录，请先人工完成手机号、微信或 QQ 登录"
        if any(marker in labels for marker in ("应用无响应", "application not responding")):
            return "元宝出现应用无响应窗口，请先关闭该窗口并检查应用状态"
        return None

    def observe(self) -> Observation:
        # Waydroid/Android 某些实例首次启动应用时会弹出系统兼容性提示；
        # 该提示不是业务状态，若不关闭会遮住底部导航并阻断固定的首步导航。
        for _ in range(2):
            # WebView 页面持续重绘时，uiautomator 可能一直等不到 idle state。
            # 先读取 Activity，已确认是福利 WebView 时只做一次短探针；失败后
            # 仍可用截图交给视觉模型，不能把页面渲染竞态误判成 ADB 故障。
            activity = self.device.activity()
            webview = WEBVIEW_ACTIVITY_MARKER in activity.lower()
            try:
                ui_xml = self.device.ui_dump(
                    attempts=WEBVIEW_UI_DUMP_ATTEMPTS if webview else UI_DUMP_ATTEMPTS,
                    timeout=WEBVIEW_UI_DUMP_TIMEOUT if webview else UI_DUMP_TIMEOUT,
                )
            except AgentError:
                if not webview:
                    raise
                print("福利 WebView 无障碍树未及时空闲，使用截图观测")
                if getattr(self, "_welfare_context", False):
                    self.normalize_welfare_scroll()
                image = self.device.screenshot()
                return Observation(image, "", "", (), activity, "welfare")
            nodes = tuple(parse_nodes(ui_xml))
            if self._dismiss_android_warning(nodes):
                continue
            if self._dismiss_upgrade_prompt(nodes):
                continue
            if self._dismiss_protocol_prompt(nodes, activity):
                continue
            if webview and not nodes and getattr(self, "_welfare_context", False):
                self.normalize_welfare_scroll()
                image = self.device.screenshot()
                return Observation(image, ui_xml, "", (), activity, "welfare")
            # dump 可能等待数秒；截图放在 dump 之后，避免把旧页面图片和新层级
            # 拼进同一次视觉请求，尤其是福利页返回后的 WebView 重绘阶段。
            image = self.device.screenshot()
            return Observation(image, ui_xml, compact_ui(ui_xml), nodes, activity, detect_stage(nodes, activity))
        image = self.device.screenshot()
        activity = self.device.activity()
        return Observation(image, ui_xml, compact_ui(ui_xml), nodes, activity, detect_stage(nodes, activity))

    def observe_startup(
        self,
        timeout: float = STARTUP_OBSERVE_TIMEOUT,
        poll_interval: float = STARTUP_OBSERVE_POLL_INTERVAL,
    ) -> Observation:
        """等待首屏稳定后再交给工作流，避免把过渡层或旧层级当成业务页面。"""
        deadline = time.monotonic() + timeout
        last_error: AgentError | None = None
        while time.monotonic() < deadline:
            try:
                check_responsive = getattr(self.device, "app_not_responding", None)
                if callable(check_responsive) and check_responsive(PACKAGE_NAME):
                    raise AppUnresponsiveError("元宝首屏进程无响应（ANR）")
                observation = self.observe()
                if callable(check_responsive) and check_responsive(PACKAGE_NAME):
                    raise AppUnresponsiveError("元宝首屏点击后进程无响应（ANR）")
            except ManualActionRequired:
                raise
            except AppUnresponsiveError:
                raise
            except AgentError as exc:
                last_error = exc
                time.sleep(poll_interval)
                continue

            blocker = self._manual_blocker(observation)
            if blocker is not None:
                # 协议页已尝试自动确认；若页面短暂未切换，继续有界重试，
                # 只有超时后才按启动失败交给会话恢复逻辑。
                if (
                    "协议" in blocker
                    and getattr(self.config, "auto_accept_protocol", False)
                ):
                    last_error = AgentError(blocker)
                    time.sleep(poll_interval)
                    continue
                raise ManualActionRequired(
                    f"{blocker}（Activity={observation.activity[:120] or '未知'}）"
                )
            return observation
        detail = str(last_error)[:180] if last_error else "首屏没有稳定业务页面"
        raise AgentError(f"首屏观测未完成（等待 {timeout:.0f} 秒；{detail}）")

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

        # 任务入口点击后，写作页和聊天页都可能先显示过渡层；先等待输入控件，
        # 并在写作类型选择页保留默认“通用”，避免把页面竞态交给外层重试。
        deadline = time.monotonic() + 12
        input_node: Node | None = None
        while time.monotonic() < deadline:
            input_node = self._find_input(observation)
            if input_node is not None:
                break
            if observation.stage == "writing":
                generic = find_node(observation.nodes, "通用", exact=True)
                if generic is not None:
                    self._tap_node(generic)
                    time.sleep(1)
                    observation = self.observe()
                    continue
                # Compose 写作页偶尔没有可访问输入节点，点击主题区域可唤起输入框。
                self._tap_point(self.width // 2, min(self.height - 100, 755))
            elif observation.stage in {"chat", "app", "unknown"}:
                self._tap_point(self.width // 2, self.height - 100)
            time.sleep(0.8)
            observation = self.observe()
        if input_node is None:
            return self._result(False, "等待输入框超时，当前页面尚未进入提问/写作输入状态")

        value = self.config.test_prompt
        if self.prompt_variant:
            value = f"{value} {self.prompt_variant}"
        if any(ord(char) > 127 for char in value):
            return self._result(False, "固定测试文本含中文，当前设备未配置可靠中文输入；请在 TEST_PROMPT 中使用英文")
        # 输入框可能已经保留上一次尝试的内容；先检查完整节点文本，避免重复追加。
        if input_node is not None and value in input_node.text:
            return self._result(True, "固定测试文本已在输入框中", stage=observation.stage)
        escaped = value.replace("%", "%25").replace(" ", "%s")
        # 输入法切换和 WebView 重绘偶尔会吞掉第一次 input text；在同一个工具内
        # 有界重试，并重新聚焦输入框，避免外层把一次瞬态失败升级为整轮熔断。
        for attempt in range(3):
            if attempt:
                refreshed = self.observe()
                refreshed_input = self._find_input(refreshed)
                if refreshed_input is not None:
                    input_node = refreshed_input
                    self._tap_node(input_node)
                else:
                    self._tap_point(self.width // 2, self.height - 100)
                time.sleep(0.5)
            self.device.input("text", escaped)
            check_deadline = time.monotonic() + 2.5
            while time.monotonic() < check_deadline:
                after = self.observe()
                # compact_ui 为了控制模型上下文会截断，不能用它判断输入是否成功。
                input_texts = [node.text for node in after.nodes if node.text]
                if any(value in text for text in input_texts):
                    return self._result(True, "已通过固定工具输入测试文本", stage=after.stage)
                time.sleep(0.4)
        return self._result(False, "固定测试文本没有出现在输入框")

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

    def _navigate_to_ours_until(self, deadline: float) -> Observation:
        """在一个有界时间窗内完成返回和导航；两次恢复都复用这套逻辑。"""
        back_sent = False
        last_back_elapsed: float | None = None
        fallback_tapped = False
        last_observation: Observation | None = None
        last_error: AgentError | None = None
        while time.monotonic() < deadline:
            try:
                observation = self.observe()
            except ManualActionRequired:
                raise
            except AgentError as exc:
                last_error = exc
                time.sleep(OURS_NAV_POLL_INTERVAL)
                continue
            last_observation = observation
            blocker = self._manual_blocker(observation)
            if blocker is not None:
                raise ManualActionRequired(f"{blocker}（Activity={observation.activity[:120] or '未知'}）")
            if observation.stage == "ours":
                return observation
            ours_node = find_node(observation.nodes, "我们", exact=True)
            if ours_node is not None:
                # 不假设固定分辨率；部分设备的底部导航高度和横向边距不同。
                self._tap_node(ours_node)
                time.sleep(1.5)
                continue
            # 应用冷启动或 WebView 首次恢复时层级可能暂时为空；聊天首页不要因暂时空层级退出。
            elapsed = OURS_NAV_TIMEOUT - max(0.0, deadline - time.monotonic())
            activity_lower = observation.activity.lower()
            # 已确认是元宝首页但无障碍树为空时，最多按一次归一化的“我们”位置。
            # 不对福利页、登录页或未知 Activity 盲点，且由 OURS_FALLBACK_DELAY 限制等待时间。
            if (
                not fallback_tapped
                and elapsed >= OURS_FALLBACK_DELAY
                and not observation.nodes
                and PACKAGE_NAME in activity_lower
                and "home.v2." in activity_lower
                and self.height > self.width
            ):
                x = round(self.width * OURS_FALLBACK_X_RATIO)
                y = round(self.height * OURS_FALLBACK_Y_RATIO)
                self._tap_point(x, y)
                fallback_tapped = True
                time.sleep(1.2)
                continue
            # 元宝的模板详情、写作和拍题 Activity 都可能被统一归类为 app；
            # 只要不是 home.v2 首页，就说明当前仍在应用内部嵌套页，可以安全返回。
            nested_app = PACKAGE_NAME in activity_lower and "home.v2" not in activity_lower
            clearly_nested = observation.stage in {
                "writing",
                "image",
                "photo_question",
                "photo_preview",
                "picker",
            } or nested_app or (
                "home.v2" not in activity_lower and observation.stage not in {"app", "unknown"}
            )
            if (
                elapsed >= 12
                and clearly_nested
                and (last_back_elapsed is None or elapsed - last_back_elapsed >= 5)
            ):
                self.device.input("keyevent", "KEYCODE_BACK")
                back_sent = True
                last_back_elapsed = elapsed
                time.sleep(1.2)
            else:
                time.sleep(OURS_NAV_POLL_INTERVAL)
        detail = ""
        if last_observation is not None:
            detail = (
                f"阶段={last_observation.stage or '未知'}，"
                f"Activity={last_observation.activity[:120] or '未知'}"
            )
        elif last_error is not None:
            detail = str(last_error)[:180]
        raise AgentError(f"导航等待超时（{detail or '没有有效页面观测'}）")

    def _restart_app(self) -> None:
        """优先复用现有进程恢复首页，仅在确认失败后清理元宝进程。"""
        # 正常运行中的元宝从详情页返回时，直接发送 MAIN Intent 可以避免
        # ARM64 兼容层在 force-stop 后留下旧进程记录并卡在 Splash。
        try:
            self.device.launch_app(PACKAGE_NAME)
            self.device.wait_app_ready(PACKAGE_NAME)
            return
        except AgentError as direct_error:
            print(f"元宝直接恢复失败，准备清理进程重试：{str(direct_error)[:160]}")
        self.device.adb("shell", "am", "force-stop", PACKAGE_NAME, timeout=20)
        time.sleep(1)
        self.app_launcher(PACKAGE_NAME)
        self.device.wait_app_ready(PACKAGE_NAME)

    def _go_to_ours(self) -> Observation:
        """等待首页导航就绪后进入“我们”，最多重启元宝一次。"""
        last_error: AgentError | None = None
        for attempt in range(2):
            try:
                return self._navigate_to_ours_until(time.monotonic() + OURS_NAV_TIMEOUT)
            except ManualActionRequired:
                raise
            except AgentError as exc:
                last_error = exc
                if attempt == 0:
                    print(f"元宝首页导航失败，正在重启元宝后重试：{str(exc)[:180]}")
                    try:
                        self._restart_app()
                    except ManualActionRequired:
                        raise
                    except AgentError as restart_error:
                        raise AgentError(f"无法回到元宝“我们”页面，应用重启失败：{restart_error}") from restart_error
        raise AgentError(f"无法回到元宝“我们”页面：{last_error or '未知错误'}")

    def _go_to_welfare(self) -> Observation:
        self.reset_welfare_scroll()
        self._welfare_context = True
        last_observation: Observation | None = None
        for attempt in range(2):
            observation = self._go_to_ours()
            last_observation = observation
            # 入口本身也可能在抽屉恢复期间暂时缺失；最多等待几秒后再使用已知布局坐标。
            entry_deadline = time.monotonic() + min(8, WELFARE_LOAD_TIMEOUT)
            fallback_tapped = False
            last_entry_tap_at: float | None = None
            while time.monotonic() < entry_deadline:
                if observation.stage == "welfare":
                    return observation
                webview_observation = self._welfare_webview_observation(
                    observation, last_entry_tap_at
                )
                if webview_observation is not None:
                    print("福利中心 WebView 已打开，无障碍树暂时为空，交由模型确认")
                    return webview_observation
                welfare_node = find_node(observation.nodes, "福利中心", exact=True)
                if welfare_node is not None and last_entry_tap_at is None:
                    self._tap_node(welfare_node)
                    last_entry_tap_at = time.monotonic()
                elif (
                    not fallback_tapped
                    and observation.stage == "ours"
                    and time.monotonic() + 1 >= entry_deadline
                ):
                    # 仅在已识别的“我们”页使用历史布局后备，未知页面不得盲点。
                    self._tap_point(640, 486)
                    fallback_tapped = True
                    last_entry_tap_at = time.monotonic()
                time.sleep(OURS_NAV_POLL_INTERVAL)
                observation = self.observe()
                last_observation = observation
            # WebView 从详情页返回时偶尔需要数十秒恢复层级；在执行层内等待，避免把临时加载状态交给模型。
            deadline = time.monotonic() + WELFARE_LOAD_TIMEOUT
            while time.monotonic() < deadline:
                observation = self.observe()
                last_observation = observation
                if observation.stage == "welfare":
                    return observation
                webview_observation = self._welfare_webview_observation(
                    observation, last_entry_tap_at
                )
                if webview_observation is not None:
                    print("福利中心 WebView 已打开，无障碍树暂时为空，交由模型确认")
                    return webview_observation
                # 如果抽屉仍停留在“我们”页，重复点击入口，覆盖第一次点击未被 WebView 接收的竞态。
                if observation.stage == "ours":
                    welfare_node = find_node(observation.nodes, "福利中心", exact=True)
                    now = time.monotonic()
                    if welfare_node is not None and (
                        last_entry_tap_at is None or now - last_entry_tap_at >= 5
                    ):
                        self._tap_node(welfare_node)
                        last_entry_tap_at = now
                time.sleep(WELFARE_LOAD_POLL_INTERVAL)
            if attempt == 0:
                print("福利中心页面加载超时，正在重启元宝并重试")
                self._restart_app()
        detail = ""
        if last_observation is not None:
            detail = f"（阶段={last_observation.stage or '未知'}，Activity={last_observation.activity[:120] or '未知'}）"
        raise AgentError(f"福利中心没有加载出任务页面{detail}")

    @staticmethod
    def _welfare_webview_observation(
        observation: Observation, entry_started_at: float | None
    ) -> Observation | None:
        """福利页 WebView 偶发没有无障碍文本时，等待渲染后交给视觉模型确认。"""
        if entry_started_at is None:
            return None
        if "webbrowseractivity" not in observation.activity.lower():
            return None
        if time.monotonic() - entry_started_at < WELFARE_WEBVIEW_READY_DELAY:
            return None
        return Observation(
            observation.image,
            observation.ui_xml,
            observation.ui,
            observation.nodes,
            observation.activity,
            "welfare",
        )

    def _ensure_picker_image(self) -> ToolResult:
        if not self.image_pushed:
            self.device.push_image(self.config.test_image_path)
            self.image_pushed = True
            time.sleep(1)
        observation = self.observe()
        # P 图和拍题的图片入口并不完全一致：拍题有时先进入相机预览，
        # 再由左下角相册入口切到 PictureSelector。所有重试都限制在已知业务页内。
        entry_markers = (
            "相册",
            "相册答题",
            "相片",
            "图片",
            "上传",
            "选择图片",
            "从相册",
            "gallery",
            "image",
        )
        fallback_points = (
            (58, self.height - 125),
            (58, self.height - 80),
            (110, self.height - 125),
            (self.width // 8, round(self.height * 0.82)),
        )
        for attempt in range(6):
            if observation.stage == "picker":
                break
            candidates = [
                node
                for node in observation.nodes
                if node.clickable
                and node.bounds.area > 0
                and any(word in node.searchable for word in entry_markers)
            ]
            if candidates:
                # 优先点击最靠近输入区左侧的图片入口，避免误触顶部说明文本。
                self._tap_node(
                    min(
                        candidates,
                        key=lambda node: (
                            abs(node.bounds.center[0] - self.width * 0.12)
                            + abs(node.bounds.center[1] - self.height * 0.86),
                            -node.bounds.area,
                        ),
                    )
                )
            elif observation.stage == "photo_preview":
                # 相机预览没有相册文字时，先返回拍题页，再由下一轮选择入口。
                self.device.input("keyevent", "KEYCODE_BACK")
            else:
                # 元宝拍题页和 P 图页都把本地图片入口放在输入区左侧；不同版本
                # 的底部栏高度略有差异，依次尝试有限的相邻坐标。
                x, y = fallback_points[min(attempt, len(fallback_points) - 1)]
                self._tap_point(x, y)
            time.sleep(min(5, 2 + attempt))
            observation = self.observe()
        if observation.stage != "picker":
            return self._result(
                False,
                f"没有进入系统图片选择器（阶段={observation.stage or '未知'}，Activity={observation.activity[:100] or '未知'}）",
            )
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
        text_button = find_node(observation.nodes, "开心收下", "收下")
        if text_button is not None:
            self._tap_node(text_button)
            time.sleep(2)
            return True
        button = find_node(observation.nodes, "button", exact=True)
        if reward_amount and button is not None:
            self._tap_node(button)
            time.sleep(2)
            return True
        # WebView 奖励遮罩常完全不进入无障碍树。当前手机布局中，
        # 绿色“开心收下”按钮中心是可验证的亮绿色像素，普通任务行该位置为深色。
        if observation.stage == "welfare" and not observation.nodes:
            x, y = self.width // 2, round(self.height * 0.647)
            pixel = png_pixel(observation.image, x, y)
            if (
                pixel is not None
                and pixel[1] >= 160
                and pixel[1] >= pixel[0] * 2
                and pixel[1] >= pixel[2] * 1.25
            ):
                self._tap_point(x, y)
                time.sleep(2)
                return True
        return False

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

    def _recover_exchange_state(self, observation: Observation) -> str | None:
        """从同日状态文件恢复兑换流程，避免服务重启后重复扣积分。"""
        status = self._today_exchange_status(self.config.card_name)
        if status is None:
            return None
        self.reward_card_name = self.config.card_name
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
                "已从同日状态恢复：QQ超级会员3天卡积分不足，跳过兑换并返回“我们”页",
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
        if not found:
            return self._result(False, f"没有找到目标商品：{self.config.card_name}")
        product, button = found
        points = self._read_exchange_points(observation)
        cost = self._read_product_cost(observation, product)
        if points is None or cost is None:
            return self._result(
                False,
                f"已找到目标商品“{self.config.card_name}”，但无法确认积分或价格，拒绝盲目兑换",
                stage=observation.stage,
            )
        if points < cost:
            self.reward_unavailable = True
            self._write_state("unavailable")
            return self._result(
                True,
                f"目标商品“{self.config.card_name}”需要 {cost} 积分，当前仅 {points}，跳过兑换并返回“我们”页",
                stage=observation.stage,
                reward_unavailable=True,
                points=points,
                cost=cost,
            )
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
            and any(
                label in {"立即使用", "去使用", "使用"}
                for label in (node.text.strip(), node.content_desc.strip())
            )
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
                stage = self.observe().stage
                if stage != "welfare":
                    self._welfare_context = False
                return self._result(True, "已点击指定位置", stage=stage)
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
                for attempt in range(3):
                    observation = self.observe()
                    if self._dismiss_reward_popup(observation):
                        return self._result(True, "已收下任务奖励", stage=self.observe().stage)
                    if attempt < 2:
                        time.sleep(2)
                return self._result(True, "未发现奖励弹窗，视为无需领取", stage=self.observe().stage)
            if name == "return_to_welfare":
                after = self._go_to_welfare()
                # 奖励遮罩可能在生成完成数秒后才注入 WebView；返回福利页时
                # 再做一次有界本地收尾，避免下一任务的视觉定位看到奖励弹窗。
                for attempt in range(3):
                    if self._dismiss_reward_popup(after):
                        after = self.observe()
                        break
                    if attempt < 2:
                        time.sleep(2)
                        after = self.observe()
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
        except ManualActionRequired:
            # 登录和协议确认不能靠重试解决，也不能被降级成普通工具失败。
            raise
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
            "description": "只定位 QQ 超级会员 3 天卡；读取当前积分和商品价格，积分不足时安全跳过兑换。",
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
        # 视觉网关无响应时必须回到统一重试边界，不能无限占用 Waydroid 会话。
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.model_request_timeout_seconds,
        )
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

    def _request(self, messages: list[dict[str, Any]], forced_tool: str | None = None) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": messages,
            # Chat Completions 规范中的 reasoning_effort 是请求体顶层字段。
            # 使用 extra_body 让 openai>=1.40 的旧客户端也能把它原样透传给兼容网关。
            "extra_body": {"reasoning_effort": self.config.reasoning_effort},
        }
        if self.tools_supported:
            kwargs["tools"] = TOOL_DEFINITIONS
            # 仅在编排器发现阶段错误后强制该阶段唯一工具；正常请求保持 auto，避免改变缓存前缀。
            kwargs["tool_choice"] = (
                {
                    "type": "function",
                    "function": {"name": forced_tool},
                }
                if forced_tool
                else "auto"
            )
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
                return self.client.chat.completions.create(
                    model=self.config.model_id,
                    messages=messages,
                    extra_body={"reasoning_effort": self.config.reasoning_effort},
                )
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

    def next_tool(
        self,
        observation: Observation,
        context: str,
        forced_tool: str | None = None,
    ) -> tuple[str, dict[str, Any], str]:
        user_message = self._current_observation(observation, context)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *self.history,
            user_message,
        ]
        response = self._with_retries(
            lambda: self._request(messages, forced_tool=forced_tool), "视觉模型调用"
        )
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
        self._append_history(
            pending["context"],
            pending["observation"],
            pending["name"],
            pending["args"],
            result,
        )
        self._pending_turn = None

    def record_local_tool_result(
        self,
        name: str,
        args: dict[str, Any],
        observation: Observation,
        context: str,
        result: ToolResult,
    ) -> None:
        """本地工具只更新 Workflow，不把可推导的流水账塞回模型上下文。"""
        # 参数保留在接口中，便于调试调用方；页面和子状态会在下一次视觉请求中
        # 通过最新截图及 Workflow.context 提供，不重复消耗输入 token。
        del name, args, observation, context, result

    def _append_history(
        self,
        context: str,
        observation: Observation,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
    ) -> None:
        self._turn_number += 1
        self.history.extend(
            [
                {
                    "role": "user",
                    "content": self._history_summary(
                        context, observation, self._turn_number
                    ),
                },
                {
                    "role": "assistant",
                    "content": self._action_json(name, args),
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
    entry_points: dict[str, tuple[float, float]] = field(default_factory=dict)
    entry_failures: int = 0
    state_restored: bool = False
    fixed_entry_disabled: set[str] = field(default_factory=set)
    same_template_revealed: bool = False
    terminal_restore: bool = False

    def restore_state(self) -> None:
        """恢复当天已确认的进度；旧日期只保留兑换状态的清理语义。"""
        self.state_restored = False
        self.terminal_restore = False
        today = datetime.now(ZoneInfo(self.config.timezone_name)).date().isoformat()
        state = read_daily_state(self.config.state_path)
        if state.get("date") != today:
            return
        saved_daily_done = state.get("daily_done")
        if isinstance(saved_daily_done, bool):
            self.daily_done = saved_daily_done
            self.state_restored = True
        saved = state.get("task_progress")
        if not isinstance(saved, dict):
            return
        for key in TASK_ORDER:
            value = saved.get(key)
            if isinstance(value, int) and 0 <= value <= 3:
                self.progress[key] = value
                self.state_restored = True
        # 兑换已经完成或明确因积分不足跳过时，重启只需回到“我们”页收尾。
        # 不再重新打开兑换商城，避免重复扫描商品和无意义的页面竞态。
        if (
            self.daily_done is True
            and all(self.progress.get(key) == 3 for key in TASK_ORDER)
            and state.get("card") == self.config.card_name
            and state.get("exchange_status") in {"used", "unavailable"}
        ):
            if state["exchange_status"] == "used":
                self.executor.reward_use_confirmed = True
            else:
                self.executor.reward_unavailable = True
            self.terminal_restore = True

    def _persist_task_state(self) -> None:
        """保存当天进度并保留兑换阶段已经写入的字段。"""
        state = read_daily_state(self.config.state_path)
        today = datetime.now(ZoneInfo(self.config.timezone_name)).date().isoformat()
        if state.get("date") != today:
            state = {}
        state.update(
            {
                "date": today,
                "daily_done": self.daily_done is True,
                "task_progress": {
                    key: self.progress.get(key) for key in TASK_ORDER
                },
            }
        )
        write_daily_state(self.config.state_path, state)

    def _merge_progress(self, daily_done: bool, values: dict[str, int]) -> None:
        """同一天的进度只允许单调增加，抵抗 WebView/视觉模型瞬时误读。"""
        self.daily_done = bool(self.daily_done is True or daily_done)
        for key, value in values.items():
            previous = self.progress.get(key)
            self.progress[key] = max(value, previous if previous is not None else 0)
        self._persist_task_state()

    def context(self, observation: Observation) -> str:
        progress = ", ".join(
            f"{TASK_LABELS[key]}={self.progress.get(key) if self.progress.get(key) is not None else '?'} / 3"
            for key in TASK_ORDER
        )
        substate = ", ".join(
            name for name, completed in self.substate.items() if completed
        ) or "无"
        coordinate_hint = ""
        if self.phase == "open_target" and self.target in WELFARE_FIXED_ENTRY_RATIOS:
            width = getattr(self.executor, "width", 0)
            height = getattr(self.executor, "height", 0)
            coordinate_hint = (
                f"屏幕原始像素为 {width}x{height}；当前目标按钮位于福利中心任务列表，"
                "不要点击顶部积分卡的同名按钮。"
            )
        return (
            f"阶段={self.phase}; 当前目标={self.target or '无'}; 今日问元宝已完成={self.daily_done}; "
            f"任务进度={progress}; 当前子步骤已完成={substate}; "
            f"期望计数={self.expected_count if self.expected_count is not None else '无'}; "
            f"奖励使用证据={self.executor.reward_use_confirmed}; {coordinate_hint}"
        )

    def _parse_report(self, args: dict[str, Any]) -> ToolResult:
        try:
            daily_done = bool(args["daily_done"])
            values = {key: int(args[key]) for key in TASK_ORDER}
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(False, "report_tasks 缺少固定进度字段")
        if any(value < 0 or value > 3 for value in values.values()):
            return ToolResult(False, "任务进度必须在 0 到 3 之间")
        merged_daily_done = bool(
            self.daily_done is True or daily_done or values["question"] >= 3
        )
        merged_values = {
            key: max(value, self.progress[key] if self.progress[key] is not None else 0)
            for key, value in values.items()
        }
        if self.target and self.expected_count is not None and self.target in TASK_LABELS:
            current = merged_values[self.target]
            if current < self.expected_count:
                self.stale_reports += 1
                self._merge_progress(merged_daily_done, merged_values)
                self.phase = "open_target"
                return ToolResult(
                    False,
                    f"任务 {TASK_LABELS[self.target]} 当前仍为 {current}/3，必须重新点击入口完成下一次",
                )
        self._merge_progress(merged_daily_done, merged_values)
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

    def _advance_after_task(self) -> None:
        """任务生成和奖励处理均成功后本地推进一次，避免重复请求视觉报告。"""
        if self.target == "daily_question":
            self.daily_done = True
        elif self.target in TASK_LABELS:
            current = self.progress.get(self.target) or 0
            self.progress[self.target] = min(3, current + 1)
        if self.target == "same_template":
            self.entry_points.pop("same_template", None)
            self.entry_points.pop("same_template:template", None)
            self.same_template_revealed = False
        self._persist_task_state()
        self._select_next()

    def _task_kind(self) -> str:
        return self.target or ""

    def _entry_cache_key(self) -> str | None:
        """返回当前可以复用的入口类型；未知阶段不使用坐标缓存。"""
        if self.phase == "open_target" and self.target in TASK_ENTRY_MARKERS:
            return self.target
        if (
            self.phase == "perform"
            and self.target == "same_template"
            and self.substate.get("entry_clicked")
            and not self.substate.get("template_clicked")
        ):
            return "same_template:template"
        return None

    def _semantic_entry_action(
        self, observation: Observation
    ) -> tuple[str, dict[str, int]] | None:
        """完整无障碍层级可读时直接按任务语义找入口，失败再交给视觉模型。"""
        if (
            self.phase != "open_target"
            or self.target not in TASK_ENTRY_MARKERS
            or self.target == "same_template"
            or observation.stage != "welfare"
            or not observation.nodes
            or self.target in self.entry_points
        ):
            return None
        markers = TASK_ENTRY_MARKERS[self.target]
        specific = tuple(
            marker
            for marker in markers
            if marker not in {"去提问", "提问", "写作", "p图", "拍题"}
        )
        candidates: list[tuple[int, int, Node]] = []
        for marker_index, marker in enumerate((*specific, *markers)):
            wanted = marker.lower()
            for node in observation.nodes:
                if (
                    not node.enabled
                    or node.bounds.area <= 0
                    or "邀请新用户" in node.searchable
                ):
                    continue
                fields = (node.text, node.content_desc, node.resource_id)
                exact = any(field == marker for field in fields)
                if not (exact or wanted in node.searchable):
                    continue
                # 优先专属文案和可点击节点；宽泛文案只在专属文案缺失时使用。
                specificity = 0 if marker in specific else 1
                clickable_penalty = 0 if node.clickable else 1
                candidates.append(
                    (
                        specificity * 10 + clickable_penalty,
                        -node.bounds.top,
                        node,
                    )
                )
            if candidates and marker in specific:
                break
        if not candidates:
            return None
        _, _, node = min(candidates, key=lambda item: (item[0], item[1]))
        x, y = node.bounds.center
        print(f"使用无障碍语义定位任务入口：{self.target}")
        return "tap", {"x": x, "y": y}

    def _remember_entry_point(self, name: str, args: dict[str, Any]) -> None:
        """保存一次已成功点击的入口，使用归一化坐标适配同一运行中的尺寸。"""
        if name != "tap":
            return
        key = self._entry_cache_key()
        if key is None:
            return
        try:
            x, y = int(args["x"]), int(args["y"])
            width, height = int(self.executor.width), int(self.executor.height)
        except (KeyError, TypeError, ValueError, AttributeError):
            return
        if width <= 0 or height <= 0 or not (0 <= x < width and 0 <= y < height):
            return
        self.entry_points[key] = (x / width, y / height)

    def cached_action(self, observation: Observation) -> tuple[str, dict[str, int]] | None:
        """在页面仍匹配时复用已验证入口，否则让视觉模型重新定位。"""
        if (
            self.phase == "open_target"
            and self.target == "same_template"
            and not self.same_template_revealed
            and observation.stage == "welfare"
            and not observation.nodes
        ):
            self.same_template_revealed = True
            self.entry_points.pop("same_template", None)
            width, height = int(self.executor.width), int(self.executor.height)
            if width > 0 and height > 0:
                return "swipe", {
                    "x1": width // 2,
                    "y1": round(height * 0.84),
                    "x2": width // 2,
                    "y2": round(height * 0.38),
                    "duration_ms": 500,
                }
        if (
            self.phase == "open_target"
            and self.target == "same_template"
            and self.same_template_revealed
            and self.target not in self.fixed_entry_disabled
            and self.target not in self.entry_points
            and observation.stage == "welfare"
            and not observation.nodes
        ):
            width, height = int(self.executor.width), int(self.executor.height)
            if width > 0 and height > 0 and 1.55 <= height / width <= 2.05:
                ratio_x, ratio_y = SAME_TEMPLATE_FIXED_ENTRY_RATIOS
                print("使用本机福利布局固定“做同款”入口坐标")
                return "tap", {"x": round(width * ratio_x), "y": round(height * ratio_y)}
        if (
            self.phase == "open_target"
            and self.target in WELFARE_FIXED_ENTRY_RATIOS
            and self.target not in self.fixed_entry_disabled
            and self.target not in self.entry_points
            and observation.stage == "welfare"
            and not observation.nodes
            and "webbrowseractivity" in observation.activity.lower()
        ):
            width, height = int(self.executor.width), int(self.executor.height)
            if width > 0 and height > 0 and 1.55 <= height / width <= 2.05:
                ratio_x, ratio_y = WELFARE_FIXED_ENTRY_RATIOS[self.target]
                print(f"使用本机福利布局固定入口坐标：{self.target}")
                return "tap", {"x": round(width * ratio_x), "y": round(height * ratio_y)}
        semantic_action = self._semantic_entry_action(observation)
        if semantic_action is not None:
            return semantic_action
        key = self._entry_cache_key()
        if key is None or key not in self.entry_points:
            return None
        width, height = int(self.executor.width), int(self.executor.height)
        if width <= 0 or height <= 0:
            return None
        ratio_x, ratio_y = self.entry_points[key]
        args = {"x": round(width * ratio_x), "y": round(height * ratio_y)}
        if key == "same_template:template":
            if observation.stage != "app":
                return None
            activity_lower = observation.activity.lower()
            if "aitemplatedetailactivity" in activity_lower:
                return None
            if not observation.nodes:
                if "home.v2" not in activity_lower and "webbrowseractivity" not in activity_lower:
                    return None
                return "tap", args
            if self._tap_hits_label(args, observation, "做同款"):
                return "tap", args
            if self._tap_hits_template_card(args, observation):
                return "tap", args
            return None
        if observation.stage != "welfare":
            return None
        if self._ignored_welfare_tap(args, observation):
            return None
        if not observation.nodes:
            return "tap", args
        markers = TASK_ENTRY_MARKERS.get(key, ())
        if markers:
            # 优先要求任务专属标题；只有当前版本完全不暴露标题时，才退回通用按钮文字。
            specific = tuple(
                marker for marker in markers if marker not in {"去提问", "提问", "写作", "p图", "拍题"}
            )
            searchable = " ".join(node.searchable for node in observation.nodes)
            if any(marker.lower() in searchable for marker in specific):
                return ("tap", args) if self._tap_hits_label(args, observation, *specific) else None
            if self._tap_hits_label(args, observation, *markers):
                return "tap", args
        return None

    def invalidate_cached_action(self) -> None:
        """丢弃当前入口缓存，让下一轮重新请求视觉定位。"""
        key = self._entry_cache_key()
        if key in WELFARE_FIXED_ENTRY_RATIOS or key == "same_template":
            self.fixed_entry_disabled.add(key)
        if key is not None:
            self.entry_points.pop(key, None)

    def expected_tool(self, observation: Observation) -> str | None:
        """返回本地状态已经确定的下一步工具，入口坐标仍交给模型定位。"""
        fixed_phases = {
            "report": "report_tasks",
            # 报告后目标已经由本地状态确定；只允许模型在最新截图中找入口坐标。
            # 若保留 auto，视觉模型可能沿用上一子任务的 claim_reward，导致连续失败熔断。
            "open_target": "tap",
            "claim": "claim_reward",
            "return": "return_to_welfare",
            "open_exchange": "open_exchange",
            "redeem": "redeem_qq_card",
            "exchange_confirm": "confirm_exchange",
            "prize_records": "open_prize_records",
            "use_reward": "use_bound_reward",
            "use_confirm": "confirm_reward_use",
            "return_ours": "return_to_ours",
            "complete": "complete_task",
        }
        if self.phase in fixed_phases:
            return fixed_phases[self.phase]
        if self.phase != "perform":
            return None

        kind = self._task_kind()
        if kind in TEXT_TASKS and not self.substate.get("input"):
            return "input_test_prompt"
        if kind in IMAGE_TASKS and not self.substate.get("image"):
            return "select_local_image"
        if kind == "photo_question" and not self.substate.get("confirmed"):
            return "confirm_image"
        if kind == "same_template" and not self.substate.get("template_clicked"):
            return "tap"
        if not self.substate.get("sent"):
            # 拍题确认后有些版本会自动提交；有发送按钮时仍必须显式发送。
            if kind == "photo_question" and self.substate.get("confirmed"):
                if self.executor._find_send(observation) is None:
                    return "wait_5s"
            return "send_message"
        if not self.substate.get("waited"):
            return "wait_5s"
        return None

    def _perform_complete(self) -> bool:
        kind = self._task_kind()
        required = TASK_STEPS.get(kind)
        return bool(required) and all(self.substate.get(name, False) for name in required)

    def _prompt_variant(self) -> str | None:
        """为文字任务生成可恢复的唯一轮次标记，避免元宝去重相同问题。"""
        if self.target not in TEXT_TASKS:
            return None
        count = self.expected_count or 1
        return f"{self.target.replace('_', '-')}-{count}"

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
    def _tap_hits_label(
        args: dict[str, Any],
        observation: Observation,
        *markers: str,
        allow_empty_welfare: bool = False,
    ) -> bool:
        """判断模型点击是否落在当前观测中带指定语义的控件附近。"""
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        # 福利 WebView 偶发只返回空无障碍树；此时截图仍可交给视觉模型定位入口。
        if allow_empty_welfare and observation.stage == "welfare" and not observation.nodes:
            return True
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

    @staticmethod
    def _tap_hits_template_card(args: dict[str, Any], observation: Observation) -> bool:
        """推荐模板页文字尚未注入无障碍树时，只放行可点击卡片区域。"""
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        # 模板 WebView 首次渲染常只有两张大卡片的 clickable 父 View；
        # 顶部分类和导航也可能是 clickable，但高度不足以成为模板卡片。
        for node in observation.nodes:
            bounds = node.bounds
            if (
                node.enabled
                and node.clickable
                and bounds.width >= 300
                and bounds.height >= 300
                and bounds.top >= 700
                and bounds.left <= x <= bounds.right
                and bounds.top <= y <= bounds.bottom
            ):
                return True
        return False

    def _template_card_fallback(self, observation: Observation) -> tuple[int, int] | None:
        """模型坐标无效时，从已确认的大型可点击卡片中取首张中心。"""
        activity_lower = observation.activity.lower()
        home_activity = PACKAGE_NAME in activity_lower and "home.v2" in activity_lower
        if observation.stage != "app" and not home_activity:
            return None
        candidates: list[Node] = []
        for node in observation.nodes:
            bounds = node.bounds
            if (
                node.enabled
                and node.clickable
                and bounds.width >= 300
                and bounds.height >= 300
                and bounds.top >= 700
                and bounds.area > 0
            ):
                candidates.append(node)
        if not candidates:
            # WebView 层级偶发为空，但 Activity 仍明确是元宝创建首页；
            # 该布局的首张推荐卡中心可按屏幕比例计算，未知页面不走此分支。
            if not home_activity:
                return None
            width = getattr(self, "width", getattr(self.executor, "width", 0))
            height = getattr(self, "height", getattr(self.executor, "height", 0))
            if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
                return None
            return round(width * 0.26), round(height * 0.78)
        card = min(candidates, key=lambda node: (node.bounds.top, node.bounds.left))
        return card.bounds.center

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
            return ToolResult(True, "每日任务已完成；QQ超级会员3天卡积分不足，已返回“我们”页面")
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
            if self.phase == "open_target" and not self._tap_hits_label(
                args, observation, "做同款", allow_empty_welfare=True
            ):
                # WebView 的文字节点和图片节点经常分离，模型坐标可能落在同一
                # 行的图片上。已有明确“做同款”文字时直接校正到文字中心，避免
                # 把可确定的定位误判成模型失败。
                entry_button = find_node(observation.nodes, "做同款", exact=True)
                if entry_button is None:
                    entry_button = find_node(observation.nodes, "做同款")
                if entry_button is not None:
                    args.update(
                        {"x": entry_button.bounds.center[0], "y": entry_button.bounds.center[1]}
                    )
                elif not (observation.stage == "welfare" and not observation.nodes):
                    return ToolResult(
                        False,
                        "请点击福利中心中“做同款”任务入口"
                        f"（阶段={observation.stage or '未知'}，Activity={observation.activity[:100] or '未知'}）",
                    )
            if self.phase == "perform":
                if self.substate.get("template_clicked", False):
                    return ToolResult(False, "推荐模板已经点击，下一步应直接发送并等待生成")
                if not self.substate.get("entry_clicked", False):
                    if observation.stage != "welfare" or not self._tap_hits_label(
                        args, observation, "做同款", allow_empty_welfare=True
                    ):
                        return ToolResult(False, "请先点击福利中心中“做同款”任务入口")
                elif not self._tap_hits_label(args, observation, "做同款"):
                    # 模板按钮的文字节点常被 Compose 单独暴露，模型可能点到卡片图片；
                    # 有文字时改用文字中心；首次渲染没有文字时，仅放行卡片父 View 内的点击。
                    template_button = find_node(observation.nodes, "做同款", exact=True)
                    if template_button is not None:
                        args.update(
                            {
                                "x": template_button.bounds.center[0],
                                "y": template_button.bounds.center[1],
                            }
                        )
                    elif not (
                        observation.stage == "app"
                        and self._tap_hits_template_card(args, observation)
                    ):
                        fallback = self._template_card_fallback(observation)
                        if fallback is None:
                            return ToolResult(False, "请在推荐模板页点击任意一个“做同款”按钮")
                        # 元宝新版把“做同款”文字放在 WebView 卡片的图片层，
                        # 无效坐标无法通过无障碍文字校验；只在大型卡片已确认时取首张中心。
                        args.update({"x": fallback[0], "y": fallback[1]})
        if name == "send_message" and self.target in TEXT_TASKS:
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
        if name == "wait_5s" and self.target in TASK_STEPS:
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
        if name == "input_test_prompt":
            set_variant = getattr(self.executor, "set_prompt_variant", None)
            if callable(set_variant):
                set_variant(self._prompt_variant())
        result = self.executor.execute(name, args)
        if not result.ok:
            return result
        if (
            name == "tap"
            and self.phase == "open_target"
            and result.data.get("stage") == "welfare"
        ):
            # 福利 WebView 的截图可用但层级可能滞后；入口点击若在短暂等待后仍
            # 没有离开福利中心，不能把它记成成功，否则下一步本地输入会在错误
            # 页面上耗尽重试次数。下一轮保留最新截图交给模型重新定位。
            self.entry_failures += 1
            return ToolResult(False, "任务入口点击后仍停留在福利中心，请根据最新截图重新定位入口")
        # 只缓存已经由执行器接受的点击；随后阶段会改变，因此必须在状态转移前记录。
        self._remember_entry_point(name, args)
        if self.phase == "navigate_welfare" and name == "open_welfare":
            self.phase = "report"
            if self.state_restored:
                if self.terminal_restore:
                    self.phase = "return_ours"
                else:
                    self._select_next()
        elif self.phase == "open_target" and name in {"tap", "press_back"}:
            self.entry_failures = 0
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
            self._advance_after_task()
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
    startup_ready = False
    manual_action_required = False
    cleanup_after_transport_failure = False
    device: Device | None = None
    executor: ToolExecutor | None = None
    workflow: Workflow | None = None
    model: VisionModel | None = None
    previous_stay_awake: str | None = None
    startup_attempt = 0
    try:
        # 启动阶段最多恢复一次。所有后续任务步骤仍由同一套工作流和模型历史处理。
        while True:
            try:
                device = runtime.discover_device(config.device)
                print(f"设备={device.serial} 模型={config.model_id} 端点={config.base_url}")
                if runtime.managed:
                    # 后台会话没有可见窗口，Android 屏幕超时会通过 Waydroid hardware 服务冻结容器。
                    previous_stay_awake = device.keep_awake()
                if runtime.managed:
                    # Waydroid 的 app launch 会登记活动应用；直接 am start 会让 suspend_action=freeze
                    # 把后台会话冻结，随后所有 ADB 操作都会超时。
                    runtime.launch_app(device, PACKAGE_NAME)
                    app_launcher = lambda package_name: runtime.launch_app(device, package_name)
                else:
                    app_launcher = device.launch_app
                    device.launch_app(PACKAGE_NAME)
                # am start 返回只代表 Intent 已提交；必须等启动窗口消失，防止读取旧无障碍树。
                device.wait_app_ready(PACKAGE_NAME)
                # 登录态页面会同时依赖 HTTPS 和 TIM 长连接；先确认 Android 默认网络已验证，
                # 避免把刚启动时的瞬态断网误报成登录态失效或交给视觉模型反复点击。
                device.wait_network_ready()
                executor = ToolExecutor(config, device, app_launcher=app_launcher)
                workflow = Workflow(config, executor)
                workflow.restore_state()
                model = VisionModel(config)
                # 首步由本地编排器固定执行，避免模型在尚未建立福利中心状态时直接报告或点击任务。
                startup_observation = executor.observe_startup()
                bootstrap = workflow.dispatch("open_welfare", {}, startup_observation)
                print(f"步骤 0：工具=open_welfare 结果={'成功' if bootstrap.ok else '失败'}；{bootstrap.message[:180]}")
                if not bootstrap.ok:
                    raise AgentError(bootstrap.message)
                startup_ready = True
                break
            except ManualActionRequired:
                raise
            except AgentError as exc:
                if startup_attempt >= 1 or not runtime.managed:
                    raise
                startup_attempt += 1
                print(f"启动阶段失败，正在执行第 {startup_attempt} 次恢复：{str(exc)[:180]}")
                if device is not None and previous_stay_awake is not None:
                    with contextlib.suppress(AgentError):
                        device.restore_keep_awake(previous_stay_awake)
                previous_stay_awake = None
                if runtime.session_started or isinstance(exc, AppUnresponsiveError):
                    if not runtime.stop():
                        raise AgentError("启动失败后无法停止 Waydroid 会话") from exc
                elif device is not None:
                    # 会话由用户或其它服务管理时不停止会话，只清理元宝坏进程再重试一次。
                    with contextlib.suppress(AgentError):
                        device.adb("shell", "am", "force-stop", PACKAGE_NAME, timeout=20)
                device = None
                executor = None
                workflow = None
                model = None
        failures = 0
        previous_state: str | None = None
        previous_action: str | None = None
        for step in range(1, config.max_steps + 1):
            observation = executor.observe()
            current_state = state_signature(observation.ui, observation.image, observation.activity)
            try:
                # 本地状态已明确的阶段/子步骤强制唯一工具；入口点击等仍由模型定位。
                forced_tool = workflow.expected_tool(observation)
                context = workflow.context(observation)
                cached_action = workflow.cached_action(observation)
                if cached_action is not None:
                    # 同一任务的第 2、3 次通常复用同一入口；页面语义变化时
                    # cached_action 会失效，下面仍会回到视觉模型定位。
                    name, args = cached_action
                    call_id = None
                    used_cached_action = True
                elif forced_tool in LOCAL_DETERMINISTIC_TOOLS:
                    # 输入、发送、等待、返回和兑换都有可验证的本地前置/后置条件；
                    # 直接执行可以显著减少重复视觉请求，同时把结果写入同轮历史。
                    name, args, call_id = forced_tool, {}, None
                    used_cached_action = False
                elif forced_tool == "report_tasks":
                    local_report = parse_local_welfare_progress(observation)
                    if local_report is not None:
                        name, args, call_id = "report_tasks", local_report, None
                        used_cached_action = False
                    else:
                        name, args, call_id = model.next_tool(
                            observation,
                            context,
                            forced_tool=forced_tool,
                        )
                        used_cached_action = False
                else:
                    name, args, call_id = model.next_tool(
                        observation,
                        context,
                        forced_tool=forced_tool,
                    )
                    used_cached_action = False
            except AgentError as exc:
                failures += 1
                print(f"步骤 {step}：模型动作解析失败（{str(exc)[:160]}）")
                if failures >= config.max_retries:
                    raise
                time.sleep(config.retry_cooldown_seconds)
                continue
            signature = action_signature(name, args)
            if repeated_action_is_stuck(
                name,
                signature,
                current_state,
                previous_action,
                previous_state,
            ):
                failures += 1
                result = ToolResult(False, "页面状态未变化且重复同一动作，已拒绝循环点击")
            else:
                result = workflow.dispatch(name, args, observation)
                failures = failures + 1 if not result.ok else 0
            if used_cached_action and not result.ok:
                workflow.invalidate_cached_action()
                # 缓存失败后允许下一轮重新执行同名的视觉点击；否则循环保护会把
                # 这次恢复误判成模型重复点击。
                previous_state, previous_action = None, None
                print("缓存入口校验失败，已清除坐标并交给视觉模型重新定位")
            else:
                previous_state, previous_action = current_state, signature
            if call_id is None:
                model.record_local_tool_result(name, args, observation, context, result)
            else:
                model.record_tool_result(call_id, result)
            safe_args = (
                json.dumps(args, ensure_ascii=False, sort_keys=True)
                if name in {"report_tasks", "tap", "swipe"}
                else ""
            )
            source = "缓存" if used_cached_action else ("本地" if call_id is None else "视觉模型")
            print(
                f"步骤 {step}：工具={name} 来源={source} {safe_args} "
                f"结果={'成功' if result.ok else '失败'}；{result.message[:180]}"
            )
            if workflow.finished:
                completed = True
                return
            if not result.ok:
                if failures >= config.max_retries:
                    raise AgentError(f"连续 {failures} 次工具/状态失败，停止本轮")
                if config.retry_cooldown_seconds:
                    time.sleep(config.retry_cooldown_seconds)
        raise AgentError(f"达到单轮最大步骤数 {config.max_steps}，未完成每日任务")
    except ManualActionRequired:
        # 保留协议、登录或 ANR 现场供用户处理；退出码 75 会阻止 systemd 自动重启。
        manual_action_required = True
        raise
    except AgentError as exc:
        if is_adb_transport_failure(exc):
            cleanup_after_transport_failure = True
            print("检测到 ADB/Waydroid 传输故障，本轮结束前清理会话并交给 systemd 重试")
        raise
    finally:
        if (
            device is not None
            and previous_stay_awake is not None
            and not manual_action_required
        ):
            with contextlib.suppress(AgentError):
                device.restore_keep_awake(previous_stay_awake)
        elif manual_action_required:
            # 保持现场可见且避免 Waydroid 立即冻结，待用户处理协议/登录/ANR 后下次成功运行再恢复设置。
            print("已保留 Waydroid 人工处理现场")
        if config.stop_waydroid_after_run and runtime.managed and (
            completed
            or (
                not startup_ready
                and not manual_action_required
                and runtime.session_started
            )
            or (cleanup_after_transport_failure and runtime.session_started)
        ):
            if completed:
                print("每日任务已完成，正在关闭 Waydroid 以释放内存")
            elif cleanup_after_transport_failure:
                print("ADB/Waydroid 传输故障，正在关闭本轮会话以便干净重试")
            else:
                # 首步导航失败时清理本轮会话，避免 systemd 重试继续复用卡死页面。
                print("每日任务尚未进入福利中心，正在关闭 Waydroid 以清理启动现场")
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
    except ManualActionRequired as exc:
        print(f"需要人工处理：{exc}", file=sys.stderr)
        # 75 由 systemd 的 RestartPreventExitStatus 识别，避免重复拉起同一阻塞页面。
        return 75
    except Exception as exc:
        print(f"失败：{type(exc).__name__}；{str(exc)[:300]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
