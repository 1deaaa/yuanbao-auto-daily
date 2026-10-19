"""福利页语义：任务目录、进度解析与页面阶段判定。"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable

from .constants import (
    ACTIVITY_PAGE_MARKERS,
    DAILY_ABILITY_EXPERIENCE_OVERLAY_MARKERS,
    DEFAULT_CARD_NAME,
    LEGACY_DAILY_QUESTION_MARKERS,
    NEW_WELFARE_DAILY_TASKS,
    PACKAGE_NAME,
    TASK_ENTRY_ROW_MARKERS,
    WEBVIEW_ACTIVITY_MARKER,
)
from .models import (
    Node,
    Observation,
)
from .ui import find_node

def is_daily_ability_experience_visible(observation: Observation) -> bool:
    """识别能力体验页或仍覆盖在福利页上的体验浮层。"""
    if observation.stage != "welfare":
        return True
    for node in observation.nodes:
        searchable = node.searchable
        interaction_bounds = node.interaction_bounds
        if (
            interaction_bounds.area > 0
            and any(marker in searchable for marker in DAILY_ABILITY_EXPERIENCE_OVERLAY_MARKERS)
        ):
            return True
        if (
            "去对话" in searchable
            and interaction_bounds.area > 0
            and interaction_bounds.top >= 300
        ):
            return True
    return False


def daily_ability_card_status(observation: Observation) -> str | None:
    """从主轮播卡固定按钮区域读取已完成/去体验状态。"""
    visible = [node for node in observation.nodes if node.enabled and node.bounds.area > 0]
    width = max(900, max((node.bounds.right for node in visible), default=0))
    height = max(1600, max((node.bounds.bottom for node in visible), default=0))
    candidates = [
        node
        for node in visible
        if node.text.strip() in {"已完成", "去体验"}
        and width * 0.48 <= node.bounds.center[0] <= width * 0.78
        and height * 0.45 <= node.bounds.center[1] <= height * 0.65
    ]
    if not candidates:
        return None
    return min(
        (node.text.strip() for node in candidates),
        key=lambda value: 0 if value == "已完成" else 1,
    )


def has_visible_exchange_product(
    observation: Observation, card_name: str = DEFAULT_CARD_NAME
) -> bool:
    """只用当前可见节点确认兑换页是否展示了目标商品。"""
    markers = tuple(
        dict.fromkeys(
            marker.lower()
            for marker in (card_name, DEFAULT_CARD_NAME, "超级会员3天卡")
            if marker
        )
    )
    return any(
        node.bounds.area > 0
        and any(marker in node.searchable for marker in markers)
        for node in observation.nodes
    )


def is_new_welfare_webview(nodes: Iterable[Node], activity: str) -> bool:
    """识别 2.88.0 福利 WebView 的页面外壳，而不是把积分栏当商城。"""
    activity_lower = activity.lower()
    if WEBVIEW_ACTIVITY_MARKER not in activity_lower:
        return False
    visible = [node for node in nodes if node.bounds.area > 0]
    root_ids = {node.resource_id for node in visible}
    if not {"hunyuan-bot", "__next"}.intersection(root_ids):
        return False
    text = " ".join(node.searchable for node in visible)
    return any(
        marker in text
        for marker in (
            "去体验",
            "已连续打卡",
            "第1天",
            "每日任务",
            "当日凌晨0点更新任务",
            "使用写作能力",
            "使用拍题能力",
            "使用p图能力",
            "做同款模板",
        )
    )


def parse_local_welfare_progress(observation: Observation) -> dict[str, Any] | None:
    """从新版福利层级读取任务目录与计数，兼容裁切到零面积的 WebView 节点。"""
    if observation.stage != "welfare" or not observation.nodes:
        return None
    nodes = tuple(observation.nodes)
    searchable_nodes = tuple(
        node for node in nodes if node.text.strip() or node.content_desc.strip()
    )
    progress: dict[str, int] = {}
    available: list[str] = []
    count_pattern = re.compile(r"(?:已完成|完成)?\s*([0-3])\s*/\s*3")
    for key, markers in TASK_ENTRY_ROW_MARKERS.items():
        label_nodes = [
            node
            for node in searchable_nodes
            if any(marker.lower() in node.searchable for marker in markers)
        ]
        if not label_nodes:
            continue
        # WebView 常将任务标题和“已完成 x/3”分别暴露为兄弟节点；先查整行
        # 文本本身，避免零边界标题借用整页父容器后串到前一项的计数。
        for label in label_nodes:
            match = count_pattern.search(label.searchable)
            if match is not None:
                progress[key] = int(match.group(1))
                break
        if key in progress:
            available.append(key)
            continue
        for label in label_nodes:
            label_bounds = label.bounds
            nearby_counts = []
            if label_bounds.area <= 0:
                continue
            for candidate in searchable_nodes:
                candidate_match = count_pattern.search(candidate.searchable)
                if candidate_match is None:
                    continue
                candidate_bounds = candidate.bounds
                if candidate_bounds.area <= 0 or label_bounds.area <= 0:
                    continue
                if (
                    abs(candidate_bounds.center[0] - label_bounds.center[0]) <= 420
                    and label_bounds.top - 20
                    <= candidate_bounds.top
                    <= label_bounds.bottom + 150
                ):
                    nearby_counts.append(
                        (
                            abs(candidate_bounds.center[1] - label_bounds.center[1]),
                            candidate_match,
                        )
                    )
            if nearby_counts:
                _, match = min(nearby_counts, key=lambda item: item[0])
                progress[key] = int(match.group(1))
                break
        # “做同款”入口只有在当前福利页明确显示累计进度时才是每日任务；
        # 单独出现的推荐模板按钮不加入任务目录。
        if key != "same_template" or key in progress:
            available.append(key)
    if not progress and not available:
        return None
    return {"available_tasks": available, **progress}


def merge_supplemental_task_progress(
    observation: Observation, report: dict[str, Any] | None
) -> dict[str, Any] | None:
    """合并滚动福利页前已从当前画面读取的做同款进度。"""
    supplemental = dict(observation.supplemental_task_progress)
    count = supplemental.get("same_template")
    if not isinstance(count, int) or not 0 <= count <= 3:
        return report
    merged = dict(report or {"available_tasks": []})
    available = merged.get("available_tasks")
    if not isinstance(available, list):
        available = []
    merged["available_tasks"] = list(
        dict.fromkeys((*available, "same_template"))
    )
    merged["same_template"] = count
    return merged


def is_new_welfare_page(observation: Observation) -> bool:
    """识别新版福利 WebView，避免把旧版问题列表套用新版目录约束。"""
    if (
        observation.stage != "welfare"
        or WEBVIEW_ACTIVITY_MARKER not in observation.activity.lower()
    ):
        return False
    if not observation.nodes or is_new_welfare_webview(
        observation.nodes, observation.activity
    ):
        return True
    text = " ".join(node.searchable for node in observation.nodes)
    return "当日凌晨0点更新任务" in text or any(
        marker in text
        for marker in ("使用拍题能力", "使用p图能力", "邀2名新用户得奖励")
    )


def has_complete_new_welfare_task_catalog(
    observation: Observation,
    local_report: dict[str, Any] | None = None,
) -> bool:
    """只有标题、三项任务及各自计数都在当前福利层级中时才认定目录完整。"""
    if not is_new_welfare_page(observation):
        return False
    if local_report is None:
        local_report = parse_local_welfare_progress(observation)
    if local_report is None:
        return False
    available = local_report.get("available_tasks")
    if not isinstance(available, list):
        return False
    if not all(
        key in available and isinstance(local_report.get(key), int)
        for key in NEW_WELFARE_DAILY_TASKS
    ):
        return False
    return any("当日凌晨0点更新任务" in node.searchable for node in observation.nodes)


def has_authoritative_local_task_catalog(
    observation: Observation, local_report: dict[str, Any]
) -> bool:
    """确认本地任务行属于新版福利页的完整每日任务区。"""
    available = local_report.get("available_tasks")
    if not isinstance(available, list) or len(set(available)) < 2:
        return False
    return any(
        "当日凌晨0点更新任务" in node.searchable
        for node in observation.nodes
    )


def detect_stage(nodes: Iterable[Node], activity: str) -> str:
    node_list = list(nodes)
    # WebView 重绘期间会保留上一页的零边界节点（例如不可见的“兑换商城”），
    # 这些节点不能覆盖当前截图中真实可见的福利任务语义。
    visible_nodes = [node for node in node_list if node.bounds.area > 0]
    text = " ".join(node.searchable for node in visible_nodes)
    activity_lower = activity.lower()
    task_row_markers = tuple(
        marker.lower()
        for markers in TASK_ENTRY_ROW_MARKERS.values()
        for marker in markers
    )
    task_row_count = sum(
        any(marker in node.searchable for marker in task_row_markers)
        for node in visible_nodes
    )
    has_welfare_catalog = task_row_count >= 2 or (
        task_row_count >= 1 and "当日凌晨0点更新任务" in text
    ) or any(marker.lower() in text for marker in LEGACY_DAILY_QUESTION_MARKERS)
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
    # 福利页“做同款”等任务入口会打开站内活动 WebView；它们有明确的活动语义
    # （活动规则/参与方式/做同款），不能当成提问/写作输入页，否则本地输入会在
    # 错误页面上耗尽重试次数。节点为空但 Activity 明确是元宝 WebView 时也视为
    # 活动页候选，交给 _close_activity_page 按返回键关闭。
    if "webbrowseractivity" in activity_lower and any(
        marker in text for marker in ACTIVITY_PAGE_MARKERS
    ) and not has_welfare_catalog:
        return "activity"
    # 新版活动专题页可能只暴露右侧“规则/记录”和专题内容，不再提供旧的
    # “活动规则/参与方式”文案。它仍然是福利 WebView 内的嵌套活动页，不能
    # 当作福利任务列表，否则兑换阶段会在专题卡片上盲点。
    exact_visible = {
        node.text.strip()
        for node in visible_nodes
        if node.text.strip()
    }
    if is_new_welfare_webview(visible_nodes, activity):
        # 新版页面的积分栏也带“兑换商城”，但能力站/任务目录才是福利页的
        # 正向证据。真实商城没有这些能力站或每日任务语义时再进入 exchange。
        return "welfare"
    if (
        "webbrowseractivity" in activity_lower
        and {"规则", "记录"}.issubset(exact_visible)
        and "兑换商城" not in text
        and not any(
            marker.lower() in text
            for marker in LEGACY_DAILY_QUESTION_MARKERS
        )
        and not has_welfare_catalog
        and "话题提问" in text
    ):
        return "activity"
    # 福利任务目录会直接展示“使用拍题能力/使用P图能力”等能力名称，已完成
    # 的行还会隐藏“去拍题/去P图”按钮。只按能力关键词判断会把福利页误判成
    # 具体能力页，随后本地执行器会在错误页面上调用图片选择器。任务目录标题
    # 或多条任务行同时出现时，优先保留福利页语义；真正进入能力页后不会出现
    # 这些完整的任务行标题。
    if task_row_count >= 2 or (
        task_row_count >= 1 and "当日凌晨0点更新任务" in text
    ):
        return "welfare"
    if any(
        marker in text
        for marker in (
            "每日问元宝得积分",
            "去提问",
            "去写作",
            "去p图",
            "去拍题",
            "问元宝任意问题累计",
        )
    ):
        return "welfare"
    if "兑换商城" in text or "qq超级会员3天卡" in text:
        return "exchange"
    # “我们”页的抽屉中有精确的“福利中心”入口和“任务”菜单；聊天首页的福利横幅
    # （“元宝福利中心”“做任务、得积分、兑福利”）必须排除，否则会被误判为“我们”页。
    if (
        find_node(node_list, "福利中心", exact=True) is not None
        and find_node(node_list, "任务", exact=True) is not None
    ):
        return "ours"
    if any(value in text for value in ("选择写作类型", "ai写作", "请输入你要写的主题")):
        return "writing"
    if any(value in text for value in ("上传图片", "选择图片进行编辑", "图片编辑")):
        return "image"
    if any(value in text for value in ("拍照答题", "相册答题", "请上传题目", "框选题目")):
        return "photo_question"
    if (
        "home.v2." in activity_lower
        and any(node.resource_id.endswith("conversation_list_layout") for node in node_list)
    ) or any(node.resource_id.endswith("edConversationInput") for node in node_list):
        return "chat"
    # “智能P图”“拍题答疑”也会作为聊天页快捷入口常驻；只有没有聊天容器时，
    # 这些宽泛能力词才用于兜底识别具体能力页。
    if any(value in text for value in ("智能p图", "p图能力")):
        return "image"
    if "拍题" in text:
        return "photo_question"
    if PACKAGE_NAME in activity:
        return "app"
    return "unknown"
