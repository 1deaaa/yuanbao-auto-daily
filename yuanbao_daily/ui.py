"""无障碍树解析与节点查找；只做结构化读取，不含业务判断。"""
from pathlib import Path
import base64
import hashlib
import json
import re
import struct
import zlib
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from .constants import RETRYABLE_SAME_STATE_TOOLS
from .models import (
    Bounds,
    Node,
)

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
# 入口缓存只在当前运行期间有效，不写入磁盘。布局变化或语义校验失败时，
# cached_action 会返回 None，下一步仍由视觉模型重新定位。
# “做同款”模板列表也在 WebView 活动里渲染，包含活动规则/参与方式等活动页
# 语义；用它定位任务行会误触活动横幅，因此语义入口不使用该标题。
    # 先列右侧行动按钮，再列任务标题。新版 WebView 只把这些文本节点
    # 暴露出来且 clickable=false；点击行动按钮的中心比点击标题稳定。
# 福利 WebView 有时只暴露任务标题，不暴露右侧行动按钮；有时又把整行
# 暴露成一个覆盖整行的 TextView。两种情况下都应以任务行的纵坐标为锚点，
# 点击右侧行动列，而不是点击标题节点中心。
# 福利页改版后每日任务区顶部插入了签到卡与邀请/创作横幅，旧固定行坐标
# 已整体下移，继续盲点会误触“已完成”等非入口行。本字典保留为空作为扩展点；
# 入口一律走语义或视觉定位，不再使用固定行坐标。
        # 环境变量优先于 .env，便于 systemd、容器和临时测试覆盖配置。
            # 只自动确认元宝首次协议页；登录、验证码和扫码仍需人工完成。
            # 兑换目标固定为三天卡，避免配置或模型误选其它商品。
        # KDE Wayland/XWayland 对置顶和强制聚焦的支持取决于窗口管理器；
        # 这些增强失败时仍必须进入 mainloop，让报告窗口保持常驻。
        # 没有图形会话或 Tk 后端异常时，不能覆盖原始任务错误。
    # WebView/Compose 重绘期间，文字节点偶尔暂时变成 [0,0][0,0]。
    # 保留最近的可见父级区域，让语义校验仍能约束视觉点击范围。


def node_interaction_bounds(node: Node) -> Bounds:
    """获取语义节点可用于点击校验的区域，不改变原始无障碍边界。"""
    return node.interaction_bounds


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

    def walk(
        element: ET.Element,
        parent_bounds: Bounds | None = None,
        parent_clickable: bool = False,
    ) -> None:
        attrs = element.attrib
        bounds = parse_bounds(attrs.get("bounds", ""))
        result.append(
            Node(
                text=attrs.get("text", ""),
                content_desc=attrs.get("content-desc", ""),
                resource_id=attrs.get("resource-id", ""),
                class_name=attrs.get("class", ""),
                clickable=attrs.get("clickable") == "true",
                enabled=attrs.get("enabled", "true") == "true",
                bounds=bounds,
                parent_bounds=parent_bounds,
                parent_clickable=parent_clickable,
            )
        )
        next_parent_bounds = bounds if bounds.area > 0 else parent_bounds
        next_parent_clickable = (
            attrs.get("clickable") == "true"
            if bounds.area > 0
            else parent_clickable
        )
        for child in element:
            if child.tag == "node":
                walk(child, next_parent_bounds, next_parent_clickable)

    for child in root:
        if child.tag == "node":
            walk(child)
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
        if node.bounds.area == 0 and node.parent_bounds is not None:
            attrs["parent-bounds"] = (
                f"[{node.parent_bounds.left},{node.parent_bounds.top}]"
                f"[{node.parent_bounds.right},{node.parent_bounds.bottom}]"
            )
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


def find_semantic_node(
    nodes: Iterable[Node],
    *needles: str,
    clickable_only: bool = False,
    exact: bool = False,
) -> Node | None:
    """查找带有可见父级区域的语义节点，专供 WebView 点击校验使用。"""
    candidates = [
        node
        for node in nodes
        if node.enabled and node_interaction_bounds(node).area > 0
    ]
    if clickable_only:
        candidates = [node for node in candidates if node.clickable or node.parent_clickable]
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


def encode_adb_input_text(value: str) -> str:
    """编码 adb shell input text 的空格和井号，避免被远端 shell 截断。"""
    return value.replace("%", "%25").replace(" ", "%s").replace("#", r"\#")


def normalize_tool_arguments(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """兼容 OpenAI 兼容端点把整数参数错误编码成数组的情况。"""

    normalized = dict(args)

    def scalar(value: Any) -> Any:
        while isinstance(value, list) and len(value) == 1:
            value = value[0]
        return value

    integer_fields = {
        "tap": ("x", "y"),
        "long_press": ("x", "y", "duration_ms"),
        "swipe": ("x1", "y1", "x2", "y2", "duration_ms"),
        "open_daily_ability": ("x", "y"),
        "report_tasks": (
            "question",
            "writing",
            "image",
            "photo_question",
            "same_template",
        ),
    }.get(name, ())

    # Qwen 兼容端点偶尔把起点/终点作为 [x, y] 写进 x1/x2，
    # 有时同时填充 y1/y2，有时完全省略 y1/y2；按点坐标优先还原，
    # 避免把列表传给 int() 耗尽一次视觉重规划。
    if name == "tap":
        x_value = normalized.get("x")
        y_value = normalized.get("y")
        if isinstance(x_value, list) and len(x_value) >= 2:
            normalized["x"] = scalar(x_value[0])
            normalized["y"] = scalar(x_value[1])
        elif isinstance(y_value, list) and len(y_value) >= 2:
            normalized["x"] = scalar(y_value[0])
            normalized["y"] = scalar(y_value[1])
    if name == "swipe":
        for x_key, y_key in (("x1", "y1"), ("x2", "y2")):
            x_value = normalized.get(x_key)
            if isinstance(x_value, list) and len(x_value) >= 2:
                normalized[x_key] = scalar(x_value[0])
                normalized[y_key] = scalar(x_value[1])

    for key in integer_fields:
        if key in normalized:
            normalized[key] = scalar(normalized[key])
    return normalized
