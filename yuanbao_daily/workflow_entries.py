"""入口定位层：任务行语义、坐标缓存与视觉点击校正。"""

from __future__ import annotations

from typing import Any

from .constants import (
    ACTIVITY_PAGE_MARKERS,
    DEFAULT_CARD_NAME,
    TASK_ENTRY_ACTION_MARKERS,
    TASK_ENTRY_ACTION_X_RATIO,
    TASK_ENTRY_MARKERS,
    TASK_ENTRY_ROW_MARKERS,
    TASK_ENTRY_SCROLL_MAX_ATTEMPTS,
)
from .models import (
    Node,
    Observation,
)
from .ui import (
    find_node,
    find_semantic_node,
    node_interaction_bounds,
)
from .welfare import (
    has_visible_exchange_product,
)


class WorkflowEntriesMixin:
    """按职责拆分的混入类；方法实现与原主类完全一致。"""

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


    @staticmethod
    def _usable_entry_node(node: Node) -> bool:
        """过滤邀请任务和活动页语义，避免把相邻卡片当成当前任务行。"""
        return bool(
            node.enabled
            and node_interaction_bounds(node).area > 0
            and "邀请新用户" not in node.searchable
            and not any(
                marker in node.searchable.lower() for marker in ACTIVITY_PAGE_MARKERS
            )
        )


    @staticmethod
    def _matches_entry_marker(node: Node, marker: str) -> bool:
        wanted = marker.lower()
        return any(field == marker for field in (node.text, node.content_desc, node.resource_id)) or wanted in node.searchable


    def _task_entry_point(self, observation: Observation) -> tuple[int, int, int] | None:
        """返回任务入口的右侧点击点和纵向容差。

        福利 WebView 的无障碍实现并不稳定：有时只暴露任务标题，有时暴露
        “去写作”等行动文案，还有时把整行标题作为一个宽大的 TextView。
        先用任务标题确定行，再把横坐标放到右侧行动列，能覆盖这三种层级。
        """
        if (
            self.phase != "open_target"
            or self.target not in TASK_ENTRY_MARKERS
            or observation.stage != "welfare"
            or not observation.nodes
        ):
            return None
        width = int(getattr(self.executor, "width", 0))
        height = int(getattr(self.executor, "height", 0))
        nodes = [node for node in observation.nodes if self._usable_entry_node(node)]
        if width <= 0:
            width = max((node_interaction_bounds(node).right for node in nodes), default=0)
        if height <= 0:
            height = max((node_interaction_bounds(node).bottom for node in nodes), default=0)
        if width <= 0 or height <= 0:
            return None
        action_markers = TASK_ENTRY_ACTION_MARKERS.get(self.target, ())
        row_markers = TASK_ENTRY_ROW_MARKERS.get(self.target, ())
        actions = [
            node
            for node in nodes
            if any(self._matches_entry_marker(node, marker) for marker in action_markers)
        ]
        row_candidates: list[tuple[int, Node]] = []
        max_row_height = 260
        for node in nodes:
            bounds = node_interaction_bounds(node)
            if bounds.height > max_row_height:
                continue
            for marker_index, marker in enumerate(row_markers):
                if self._matches_entry_marker(node, marker):
                    exact = any(
                        field == marker
                        for field in (node.text, node.content_desc, node.resource_id)
                    )
                    row_candidates.append((marker_index * 2 + (0 if exact else 1), node))
                    break

        row = min(
            row_candidates,
            key=lambda item: (
                item[0],
                node_interaction_bounds(item[1]).top,
                node_interaction_bounds(item[1]).area,
            ),
            default=(0, None),
        )[1]
        # 新版做同款没有独立的“做同款”文字按钮，任务入口就是推荐模板卡片。
        # 在已有任务行且未暴露行动文案时，优先返回首张可点击卡片中心；否则
        # 会把模型坐标校正到标题行右侧，连续点击同一个无效位置。
        if self.target == "same_template" and not actions and row is not None:
            row_bounds = node_interaction_bounds(row)
            cards = [
                node
                for node in nodes
                if node.clickable
                and node.enabled
                and node.bounds.area >= 30000
                and node.bounds.top >= row_bounds.bottom
            ]
            if cards:
                card = min(cards, key=lambda node: (node.bounds.top, node.bounds.left))
                return card.bounds.center[0], card.bounds.center[1], max(90, card.bounds.height // 3)
        action: Node | None = None
        if actions and row is not None:
            nearby = [
                node
                for node in actions
                if node is not row
                and abs(
                    node_interaction_bounds(node).center[1]
                    - node_interaction_bounds(row).center[1]
                )
                <= max(180, node_interaction_bounds(row).height + 80)
            ]
            if nearby:
                action = min(
                    nearby,
                    key=lambda node: (
                        abs(
                            node_interaction_bounds(node).center[1]
                            - node_interaction_bounds(row).center[1]
                        ),
                        -node_interaction_bounds(node).center[0],
                    ),
                )
        if action is None and actions and row is None:
            # “去提问”可能同时出现在顶部签到卡和任务列表；没有任务标题时
            # 选择屏幕中更靠下的行动项，避免点击顶部卡片。
            action = max(
                actions,
                key=lambda node: (
                    node_interaction_bounds(node).center[1],
                    node_interaction_bounds(node).center[0],
                ),
            )
        if action is None and row is None:
            return None

        anchor = action or row
        assert anchor is not None
        anchor_bounds = node_interaction_bounds(anchor)
        x, y = anchor_bounds.center
        action_bounds = node_interaction_bounds(action) if action is not None else None
        row_bounds = node_interaction_bounds(row) if row is not None else None
        # 只有右侧的小行动节点才直接使用其中心；整行节点的中心通常落在
        # x=450 左右，必须改用右侧行动列。没有行动节点时同样只借用行的 y。
        if (
            action is not None
            and action_bounds is not None
            and action_bounds.width <= round(width * 0.35)
            and action_bounds.center[0] >= round(width * 0.62)
        ):
            x = action_bounds.center[0]
        else:
            x = round(width * TASK_ENTRY_ACTION_X_RATIO)
        anchor_height = max(
            row_bounds.height if row_bounds is not None else 0,
            action_bounds.height if action_bounds is not None else 0,
        )
        tolerance = max(70, min(180, anchor_height // 2 + 55))
        return x, y, tolerance


    def _semantic_entry_action(
        self, observation: Observation
    ) -> tuple[str, dict[str, int]] | None:
        """完整无障碍层级可读时直接按任务语义找入口，失败再交给视觉模型。"""
        if (
            self.phase != "open_target"
            or self.target not in TASK_ENTRY_MARKERS
            or observation.stage != "welfare"
            or not observation.nodes
        ):
            return None
        point = self._task_entry_point(observation)
        if point is None:
            return None
        self.entry_scroll_attempts.pop(self.target, None)
        # 语义入口失败后不再无条件复用：同一坐标已失败时必须换入口，
        # 否则语义定位会反复给出同一个点不中的坐标，耗尽重试次数。
        cached = self._entry_cache_key()
        if cached is not None and cached in self.entry_points:
            return None
        x, y, _ = point
        print(f"使用无障碍语义定位任务入口：{self.target}")
        return "tap", {"x": x, "y": y}


    def _offscreen_entry_scroll_action(
        self, observation: Observation
    ) -> tuple[str, dict[str, int]] | None:
        """目标行明确存在但尚无屏幕边界时，有界滚动福利列表使其进入视口。"""
        if (
            self.phase != "open_target"
            or self.target not in TASK_ENTRY_ROW_MARKERS
            or observation.stage != "welfare"
            or not observation.nodes
        ):
            return None
        markers = TASK_ENTRY_ROW_MARKERS[self.target]
        if not any(
            node.bounds.area == 0
            and any(self._matches_entry_marker(node, marker) for marker in markers)
            for node in observation.nodes
        ):
            return None
        attempts = self.entry_scroll_attempts.get(self.target, 0)
        if attempts >= TASK_ENTRY_SCROLL_MAX_ATTEMPTS:
            return None
        width = int(getattr(self.executor, "width", 0))
        height = int(getattr(self.executor, "height", 0))
        if width <= 0 or height <= 0:
            return None
        self.entry_scroll_attempts[self.target] = attempts + 1
        print(f"福利任务行在视口外，滚动以显示入口：{self.target}（{attempts + 1}/{TASK_ENTRY_SCROLL_MAX_ATTEMPTS}）")
        start_ratio, end_ratio = (
            (0.38, 0.84) if self.target == "same_template" else (0.84, 0.38)
        )
        return "swipe", {
            "x1": width // 2,
            "y1": round(height * start_ratio),
            "x2": width // 2,
            "y2": round(height * end_ratio),
            "duration_ms": 500,
        }


    def _tap_matches_task_entry(self, args: dict[str, Any], observation: Observation) -> bool:
        point = self._task_entry_point(observation)
        if point is None:
            return False
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        target_x, target_y, tolerance = point
        width = int(getattr(self.executor, "width", 0))
        return (
            abs(x - target_x) <= max(55, round(width * 0.10))
            and abs(y - target_y) <= tolerance
        )


    def _correct_task_entry_tap(self, args: dict[str, Any], observation: Observation) -> bool:
        """用当前语义行校正视觉点击；无语义时完全交给 VLMM。"""
        point = self._task_entry_point(observation)
        if point is None:
            # 无障碍树没有目标语义时完全交给 VLMM，避免本地规则阻断页面改版。
            return True
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        target_x, target_y, tolerance = point
        width = int(getattr(self.executor, "width", 0))
        if abs(y - target_y) > tolerance:
            # 当前无障碍树已经给出了目标行，视觉模型可能受截图缩放影响把点
            # 落在相邻卡片；使用本轮语义行校正，而不是让旧布局规则阻断页面改版。
            args.update({"x": target_x, "y": target_y})
            print(f"模型点击偏离目标任务行，已按本轮语义重新定位：{self.target}")
            return True
        if abs(x - target_x) > max(55, round(width * 0.10)):
            args.update({"x": target_x, "y": target_y})
            print(f"模型点击落在任务行中部，已按本轮语义校正行动位置：{self.target}")
        return True


    def _correct_exchange_entry_tap(self, args: dict[str, Any], observation: Observation) -> bool:
        """校验兑换入口点击；空 WebView 层级完全交给截图视觉判断。"""
        if observation.stage not in {"welfare", "exchange"}:
            return False
        entry = self._welfare_exchange_entry_node(observation)
        if entry is None and observation.stage == "welfare" and not observation.nodes:
            return True
        if entry is None:
            return False
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        bounds = node_interaction_bounds(entry)
        if bounds.area <= 0:
            # 没有任何可见父级时只剩截图证据，完全交给 VLMM；不能用历史坐标
            # 猜测入口位置。
            return True
        if not (
            bounds.left - 70 <= x <= bounds.right + 70
            and bounds.top - 70 <= y <= bounds.bottom + 70
        ):
            # 父级区域只是当前页面的语义约束，不把父级中心当作固定入口坐标；
            # 视觉模型仍负责在本轮截图内选点，下一次观测会重新规划。
            return False
        return True


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


    def _prime_entry_point(self, observation: Observation) -> None:
        """用刚完成的稳定福利层级预存当前任务入口，覆盖下一轮空树竞态。"""
        action = self._semantic_entry_action(observation)
        if action is None:
            return
        self._remember_entry_point(*action)
        key = self._entry_cache_key()
        if key is not None and key in self.entry_points:
            self.primed_entry_points.add(key)


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
                # 做同款模板卡位于每日任务列表上方；从下方目录返回时向下滑动手指，
                # 让上方卡片重新进入视口。
                return "swipe", {
                    "x1": width // 2,
                    "y1": round(height * 0.38),
                    "x2": width // 2,
                    "y2": round(height * 0.84),
                    "duration_ms": 500,
                }
        if (
            self.phase == "perform"
            and self.target == "same_template"
            and self.substate.get("entry_clicked")
            and not self.substate.get("template_clicked")
        ):
            key = self._entry_cache_key()
            if key is not None and key in self.entry_points:
                width, height = int(self.executor.width), int(self.executor.height)
                if width > 0 and height > 0:
                    ratio_x, ratio_y = self.entry_points[key]
                    cached_args = {
                        "x": round(width * ratio_x),
                        "y": round(height * ratio_y),
                    }
                    if observation.stage == "app" and (
                        self._tap_hits_label(cached_args, observation, "做同款")
                        or self._tap_hits_template_card(cached_args, observation)
                    ):
                        return "tap", cached_args
                if observation.stage == "app":
                    self.entry_points.pop(key, None)
            button = find_node(observation.nodes, "做同款", exact=True)
            if button is None:
                button = find_node(observation.nodes, "做同款")
            if button is not None and button.bounds.area > 0:
                print("使用无障碍语义定位推荐模板按钮")
                return "tap", {"x": button.bounds.center[0], "y": button.bounds.center[1]}
            fallback = self._template_card_fallback(observation)
            if fallback is not None:
                print("使用首张可点击推荐模板卡片继续做同款任务")
                return "tap", {"x": fallback[0], "y": fallback[1]}
        semantic_action = self._semantic_entry_action(observation)
        if semantic_action is not None:
            return semantic_action
        scroll_action = self._offscreen_entry_scroll_action(observation)
        if scroll_action is not None:
            return scroll_action
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
            # 只有 report_tasks 刚用完整层级确认过的坐标才允许跨过一次空树；
            # 普通历史缓存仍然禁止复用，避免福利页滚动后误点旧任务行。
            if key in self.primed_entry_points:
                self.primed_entry_points.discard(key)
                print(f"福利 WebView 层级暂空，沿用已确认的任务入口坐标：{key}")
                return "tap", args
            return None
        markers = TASK_ENTRY_MARKERS.get(key, ())
        if markers:
            # 优先要求任务专属标题；只有当前版本完全不暴露标题时，才退回通用按钮文字。
            specific = markers
            searchable = " ".join(node.searchable for node in observation.nodes)
            if any(marker.lower() in searchable for marker in specific):
                if self._tap_hits_label(args, observation, *specific) or self._tap_matches_task_entry(
                    args, observation
                ):
                    return "tap", args
                return None
            if self._tap_hits_label(args, observation, *markers) or self._tap_matches_task_entry(
                args, observation
            ):
                return "tap", args
        return None


    def invalidate_cached_action(self) -> None:
        """丢弃当前入口缓存，让下一轮重新请求视觉定位。"""
        key = self._entry_cache_key()
        if key == "same_template":
            self.fixed_entry_disabled.add(key)
        if key is not None:
            self.entry_points.pop(key, None)


    def _welfare_exchange_entry_node(self, observation: Observation) -> Node | None:
        """识别兑换阶段中仍停留在福利页的“兑换商城”入口。

        福利页和真实商城都可能暴露“兑换商城”文案，不能只按阶段字符串判断。
        当前无障碍树若没有可见目标商品，则该文案只能作为福利页入口处理；真实
        商城至少需要在当前视口暴露目标商品，随后才允许进入商品/兑换流程。
        """
        if observation.stage not in {"welfare", "exchange"}:
            return None
        config = getattr(self.executor, "config", None)
        card_name = getattr(config, "card_name", DEFAULT_CARD_NAME)
        if has_visible_exchange_product(observation, card_name):
            return None
        node = find_semantic_node(observation.nodes, "兑换商城", exact=True)
        if node is None:
            node = find_semantic_node(observation.nodes, "兑换商城")
        return node


    @staticmethod
    def task_catalog_signature(observation: Observation) -> tuple[tuple[Any, ...], ...]:
        """提取可见任务目录的稳定签名，排除上方奖励轮播等无关动态区域。"""
        markers = tuple(
            marker
            for values in TASK_ENTRY_ROW_MARKERS.values()
            for marker in values
        )
        rows = []
        for node in observation.nodes:
            if node.bounds.area <= 0 or not node.enabled:
                continue
            if any(marker.lower() in node.searchable for marker in markers):
                rows.append(
                    (
                        node.searchable,
                        node.bounds.left,
                        node.bounds.top,
                        node.bounds.right,
                        node.bounds.bottom,
                    )
                )
        return tuple(sorted(rows))
