#!/usr/bin/env python3
"""用 OpenAI 兼容视觉端点驱动 Android/Waydroid 的最小安全原型。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from openai import OpenAI


DEFAULT_DEVICE = os.environ.get("ANDROID_DEVICE", "")
DEFAULT_BASE_URL = "http://localhost:7860/v1"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_REASONING_EFFORT = "high"
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
PACKAGE_NAME = "com.tencent.hunyuan.app.chat"
ALLOWED_ACTIONS = {"tap", "swipe", "type", "key", "wait", "done"}
MAX_STEPS = 20
HISTORY_SUMMARY_LIMIT = 500
NUMERIC_KEYCODES = {
    "4": "KEYCODE_BACK",
    "61": "KEYCODE_TAB",
    "66": "KEYCODE_ENTER",
    "67": "KEYCODE_DEL",
}

SYSTEM_PROMPT = (
    "你是 Android 无障碍操作代理。每次只返回一个 JSON 对象，不要 markdown，不要解释。\n"
    "允许 action: tap(x,y), swipe(x1,y1,x2,y2,duration_ms), type(text), "
    "key(keycode), wait(seconds), done。坐标是当前截图的像素坐标，屏幕左上角为 0,0。\n"
    "必须优先使用截图中可见文字/控件；不要猜测屏幕外坐标。状态没有变化时不要重复同一动作。\n"
    "福利中心的‘去提问’点击后可能关闭网页并返回‘我们’页；此时要点击底部‘问元宝’进入输入页。\n"
    "在‘我们’页，‘福利中心’按钮位于截图约 (640,486)，不要把上方‘录音’卡片当成福利中心。\n"
    "在福利中心，顶部‘每日问元宝得积分’区域的‘去提问’约为 (670,361)；返回‘我们’后，底部‘问元宝’约为 (111,1290)。\n"
    "只有确认消息列表出现用户发送的数字 1 且输入框恢复占位符后，才返回 "
    '{"action":"done","reason":"..."}。'
)


@dataclass(frozen=True)
class Device:
    serial: str

    def adb(self, *args: str, timeout: float = 15.0, check: bool = True) -> bytes:
        command = ["adb", "-s", self.serial, *args]
        result = subprocess.run(command, capture_output=True, timeout=timeout)
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ADB 命令失败: {' '.join(command)}\n{stderr}")
        return result.stdout

    def screenshot(self) -> bytes:
        return self.adb("exec-out", "screencap", "-p", timeout=20)

    def ui_dump(self) -> str:
        remote = "/sdcard/auto_daily_window.xml"
        self.adb("shell", "uiautomator", "dump", remote, timeout=20)
        raw = self.adb("shell", "cat", remote, timeout=10)
        return raw.decode("utf-8", errors="replace")

    def display_size(self) -> tuple[int, int]:
        output = self.adb("shell", "wm", "size", timeout=10).decode("utf-8", errors="replace")
        match = re.search(r"(\d+)x(\d+)", output)
        if not match:
            raise RuntimeError(f"无法读取屏幕尺寸: {output!r}")
        return int(match.group(1)), int(match.group(2))

    def input(self, *args: str, timeout: float = 15.0) -> None:
        self.adb("shell", "input", *args, timeout=timeout)


def compact_ui(xml: str, limit: int = 12000) -> str:
    """只保留文本、描述、资源 ID 和边界，避免把无关层级大量送入模型。"""
    lines: list[str] = []
    for node in re.findall(r"<node\b[^>]*/?>", xml):
        attrs = {}
        for key in ("text", "content-desc", "resource-id", "class", "clickable", "enabled", "bounds"):
            match = re.search(rf'{key}="([^"]*)"', node)
            if match and match.group(1):
                attrs[key] = match.group(1)
        if attrs.get("text") or attrs.get("content-desc") or attrs.get("resource-id") or attrs.get("clickable") == "true":
            lines.append(json.dumps(attrs, ensure_ascii=False, separators=(",", ":")))
    result = "\n".join(lines)
    return result[:limit]


def state_signature(ui: str, image: bytes) -> str:
    """为当前观测生成短期指纹，不把截图写入磁盘。"""
    material = ui if ui else hashlib.sha256(image).hexdigest()
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def action_signature(action: dict[str, Any]) -> str:
    """忽略 JSON 字段顺序，便于检测模型重复返回同一个动作。"""
    return json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def screen_stage(ui: str) -> str:
    """根据无障碍文本给模型一个粗粒度页面阶段提示。"""
    if "每日问元宝得积分" in ui or "兑换商城" in ui:
        return "福利中心"
    if "edConversationInput" in ui:
        return "问元宝"
    if '"text":"福利中心"' in ui and '"text":"任务"' in ui:
        return "我们"
    return "未知"


def _bounds_position(bounds: str) -> tuple[int, int] | None:
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def has_send_evidence(ui: str, goal: str) -> bool:
    """确认“输入 1 并发送”已经产生用户消息，而不是只点击了发送按钮。"""
    if "发送" not in goal or "1" not in goal:
        return True

    input_reset = False
    user_message = False
    for line in ui.splitlines():
        try:
            node = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            node.get("resource-id", "").endswith("edConversationInput")
            and node.get("class") == "android.widget.EditText"
            and node.get("text") in {"发消息或按住说话...", ""}
        ):
            input_reset = True
        if node.get("text") == "1" and node.get("class") == "android.widget.TextView":
            position = _bounds_position(node.get("bounds", ""))
            # 元宝的用户消息靠右，避免把左侧模型回答中的“1”当成用户输入。
            if position is not None and position[0] >= 500 and position[1] < 600:
                user_message = True
    return input_reset and user_message


def encode_image(image: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


def append_history(
    history: list[dict[str, Any]], step: int, ui: str, action: dict[str, Any]
) -> None:
    """归档已完成回合的最小文本信息，不把旧截图或完整层级带入下一请求。"""
    stage = screen_stage(ui)
    summary = (
        f"历史回合 {step} 的观测摘要（无截图，当前页面以最新截图为准）："
        f"页面阶段={stage}"
    )[:HISTORY_SUMMARY_LIMIT]
    history.extend(
        [
            {"role": "user", "content": summary},
            {"role": "assistant", "content": action_signature(action)},
            {
                "role": "user",
                "content": "编排器已执行上一动作；下一步必须以最新屏幕观测为准。",
            },
        ]
    )


def parse_action(text: str) -> dict[str, Any]:
    """解析模型输出，允许模型包裹在 markdown JSON 代码块中。"""
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    try:
        action = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型没有返回有效 JSON: {text[:500]!r}") from exc
    if not isinstance(action, dict) or action.get("action") not in ALLOWED_ACTIONS:
        raise ValueError(f"不允许的动作: {action!r}")
    return action


def normalize_point(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    """兼容视觉模型按 1000 像素宽的缩放画布返回坐标。"""
    if 0 <= x < width and 0 <= y < height:
        return x, y
    if 0 <= x <= 1000 and 0 <= y <= 2000:
        scale = width / 1000
        converted = round(x * scale), round(y * scale)
        if 0 <= converted[0] < width and 0 <= converted[1] < height:
            return converted
    raise ValueError(f"坐标越界: {(x, y)}，屏幕为 {width}x{height}")


def validate_and_execute(device: Device, action: dict[str, Any], width: int, height: int) -> None:
    kind = action["action"]
    if kind == "tap":
        x, y = int(action["x"]), int(action["y"])
        x, y = normalize_point(x, y, width, height)
        device.input("tap", str(x), str(y))
    elif kind == "swipe":
        values = [int(action[key]) for key in ("x1", "y1", "x2", "y2")]
        start = normalize_point(values[0], values[1], width, height)
        end = normalize_point(values[2], values[3], width, height)
        values = [start[0], start[1], end[0], end[1]]
        duration = max(100, min(2000, int(action.get("duration_ms", 350))))
        device.input("swipe", *(str(value) for value in values), str(duration))
    elif kind == "type":
        value = str(action.get("text", ""))
        if not value or len(value) > 500 or any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("输入文本为空、过长或含有不安全控制字符")
        # adb input text 对空格和百分号有特殊语义，使用逐字 Unicode 输入回退。
        escaped = value.replace("%", "%25").replace(" ", "%s").replace("'", "\\'")
        device.input("text", escaped)
    elif kind == "key":
        keycode = str(action.get("keycode", ""))
        keycode = NUMERIC_KEYCODES.get(keycode, keycode)
        if keycode not in {"KEYCODE_ENTER", "KEYCODE_BACK", "KEYCODE_TAB", "KEYCODE_DEL"}:
            raise ValueError(f"不允许的按键: {keycode}")
        device.input("keyevent", keycode)
    elif kind == "wait":
        seconds = max(0.1, min(5.0, float(action.get("seconds", 1.0))))
        time.sleep(seconds)
    elif kind == "done":
        return


def build_client() -> OpenAI:
    api_key = os.environ.get("LOCAL_LLM_API_KEY")
    if not api_key:
        raise RuntimeError("请通过环境变量 LOCAL_LLM_API_KEY 提供本地端点密钥")
    return OpenAI(
        api_key=api_key,
        base_url=os.environ.get("LOCAL_LLM_BASE_URL", DEFAULT_BASE_URL),
    )


def ask_model(
    client: OpenAI,
    model: str,
    image: bytes,
    ui: str,
    goal: str,
    size: tuple[int, int],
    history: list[dict[str, Any]] | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    width, height = size
    history = history or []
    stage = screen_stage(ui)
    user_text = (
        f"目标：{goal}\n当前屏幕尺寸：{width}x{height}\n"
        f"根据层级推断的页面阶段：{stage}\n"
        "以下是压缩后的无障碍层级，仅作为辅助，截图是最终依据：\n"
        f"{ui or '(无可用层级信息)'}"
    )
    response = client.chat.completions.create(
        model=model,
        # 通过 extra_body 让旧版 OpenAI 客户端也把规范字段放在请求顶层。
        extra_body={
            "reasoning_effort": _configured_reasoning_effort(reasoning_effort),
        },
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": encode_image(image), "detail": "high"}},
                ],
            },
        ],
    )
    content = response.choices[0].message.content or ""
    return parse_action(content)


def _configured_reasoning_effort(value: str | None = None) -> str:
    effort = (value or os.environ.get("REASONING_EFFORT") or DEFAULT_REASONING_EFFORT).strip().lower()
    if effort not in REASONING_EFFORTS:
        allowed = ", ".join(sorted(REASONING_EFFORTS))
        raise RuntimeError(f"REASONING_EFFORT 必须是以下值之一：{allowed}")
    return effort


def main() -> int:
    parser = argparse.ArgumentParser(description="视觉模型驱动 Android 的最小原型")
    parser.add_argument("--device", default=os.environ.get("ANDROID_DEVICE", DEFAULT_DEVICE))
    parser.add_argument("--model", default=os.environ.get("LOCAL_LLM_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS)
    parser.add_argument(
        "--goal",
        default="从元宝主页进入‘我们’，打开‘福利中心’，点击‘问元宝’，输入数字 1 并发送。完成发送后停止。",
    )
    args = parser.parse_args()

    if not args.device:
        raise RuntimeError("请通过 --device 或 ANDROID_DEVICE 指定在线 ADB 设备")
    device = Device(args.device)
    client = build_client()
    width, height = device.display_size()
    print(f"设备={args.device} 屏幕={width}x{height} 模型={args.model}")

    history: list[dict[str, Any]] = []
    previous_state: str | None = None
    previous_action_key: str | None = None
    repeated_observation_count = 0

    for step in range(1, max(1, min(args.max_steps, MAX_STEPS)) + 1):
        image = device.screenshot()
        ui = compact_ui(device.ui_dump())
        action = ask_model(
            client,
            args.model,
            image,
            ui,
            args.goal,
            (width, height),
            history,
        )
        print(f"步骤 {step}: {json.dumps(action, ensure_ascii=False)}")

        current_state = state_signature(ui, image)
        current_action_key = action_signature(action)
        if (
            action["action"] != "wait"
            and current_state == previous_state
            and current_action_key == previous_action_key
        ):
            repeated_observation_count += 1
        else:
            repeated_observation_count = 0
        if repeated_observation_count >= 2:
            raise RuntimeError("页面状态未变化且模型重复同一动作，已熔断以避免循环点击")

        validate_and_execute(device, action, width, height)
        if action["action"] == "done":
            final_ui = compact_ui(device.ui_dump())
            if not has_send_evidence(final_ui, args.goal):
                raise RuntimeError("模型提前报告完成，但未发现已发送的数字 1；拒绝误报")
            print("模型报告目标已完成。")
            return 0
        previous_state = current_state
        previous_action_key = current_action_key
        append_history(history, step, ui, action)
        time.sleep(0.8)

    raise RuntimeError("达到最大步骤数，未收到 done")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已中止。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"失败: {exc}", file=sys.stderr)
        raise SystemExit(1)
