"""入口定位层：任务行语义、坐标缓存与视觉点击校正。"""

from __future__ import annotations

from typing import Any

from .constants import (
    ACTIVITY_PAGE_MARKERS,
    DEFAULT_CARD_NAME,
    LEGACY_TASK_ENTRY_ACTION_X_RATIO,
    TASK_ENTRY_ACTION_MARKERS,
    TASK_ENTRY_ACTION_X_RATIO,
    TASK_ENTRY_MARKERS,
    TASK_ENTRY_ROW_MARKERS,
    TASK_ENTRY_SCROLL_MAX_ATTEMPTS,
)
from .models import (
    Node,
    Observation,
    ToolResult,
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
            if self.target == "same_template" and row is not None:
                # 模板卡片内部也可能带“做同款”字样，但它属于卡片而不是任务行
                # 的行动按钮；只接受与任务行同一水平带的行动节点。
                row_bounds = node_interaction_bounds(row)
                band = max(40, row_bounds.height)
                nearby = [
                    node
                    for node in nearby
                    if row_bounds.top - band
                    <= node_interaction_bounds(node).center[1]
                    <= row_bounds.bottom + band
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
        # 新版做同款没有独立的“做同款”文字按钮，任务入口就是推荐模板卡片。
        # 只有在任务行没有可用行动节点时才回退到卡片：页面残留的零边界
        # “做同款”文案不算可用入口，否则会把点击推到任务行右侧的空白处，
        # 页面毫无反应，模型只能反复点同一个无效坐标。
        if self.target == "same_template" and row is not None and not self._usable_action(action):
            cards = self._template_cards(observation, row)
            if cards:
                card = cards[0]
                return card.bounds.center[0], card.bounds.center[1], max(90, card.bounds.height // 3)
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
        elif row_bounds is not None and row_bounds.right >= round(width * 0.9):
            # 旧版整行任务行：标题节点铺满整行，行动按钮贴在最右侧的固定列。
            x = round(width * LEGACY_TASK_ENTRY_ACTION_X_RATIO)
        else:
            # 新版卡片任务行：标题是短节点，行动按钮固定在卡片右侧行动列。
            x = round(width * TASK_ENTRY_ACTION_X_RATIO)
        anchor_height = max(
            row_bounds.height if row_bounds is not None else 0,
            action_bounds.height if action_bounds is not None else 0,
        )
        tolerance = max(70, min(180, anchor_height // 2 + 55))
        return x, y, tolerance


    @staticmethod
    def _usable_action(action: Node | None) -> bool:
        """判断行动节点是否真的可以点击。

        WebView 改版后常留下零边界的“做同款”文案节点：它能通过文案匹配，
        但没有可点击区域，用它做入口只会把点击推到空白处。
        """
        return action is not None and action.bounds.area > 0


    def _template_cards(
        self, observation: Observation, row: Node | None = None
    ) -> list[Node]:
        """列出“做同款”任务行下方的推荐模板卡片。

        卡片本身是做同款任务的入口，具体选哪一张由视觉模型决定；这里只按
        当前无障碍层级给出可点击区域，避免模型在空白处反复点击。
        """
        if row is None:
            row = self._same_template_row(observation)
        if row is None:
            return []
        floor = node_interaction_bounds(row).bottom
        cards = [
            node
            for node in observation.nodes
            if node.enabled
            and node.clickable
            and node.bounds.area >= 30000
            and node.bounds.top >= floor
        ]
        return sorted(cards, key=lambda node: (node.bounds.top, node.bounds.left))


    def _same_template_row(self, observation: Observation) -> Node | None:
        """返回“做同款”任务行节点；文案变化时返回 None 交由视觉模型判断。"""
        markers = TASK_ENTRY_ROW_MARKERS.get("same_template", ())
        for node in observation.nodes:
            if not self._usable_entry_node(node):
                continue
            bounds = node_interaction_bounds(node)
            if bounds.height > 260:
                continue
            if any(self._matches_entry_marker(node, marker) for marker in markers):
                return node
        return None


    def _tap_hits_welfare_template_card(
        self, args: dict[str, Any], observation: Observation
    ) -> bool:
        """判断点击是否落在福利页的任一推荐模板卡片上。"""
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        for node in self._template_cards(observation):
            bounds = node.bounds
            if bounds.left <= x <= bounds.right and bounds.top <= y <= bounds.bottom:
                return True
        return False


    def template_card_hint(self, observation: Observation) -> str:
        """给视觉模型列出当前可见的模板卡片，供其在被拒后重新选择。"""
        cards = self._template_cards(observation)
        if not cards:
            return ""
        names = []
        for node in cards[:6]:
            label = (node.content_desc or node.text or "").strip()
            center = node.bounds.center
            names.append(f"{label or '模板卡片'}({center[0]},{center[1]})")
        return "当前福利页可见的推荐模板卡片：" + "、".join(names)

    def _same_template_row_action(self, observation: Observation) -> Node | None:
        """返回“做同款”任务行同一水平带内的“做同款”按钮。

        任务标题“使用推荐模板做同款”本身也含“做同款”，必须排除，否则会把
        点击校正到不可点的标题上。
        """
        row = self._same_template_row(observation)
        if row is None:
            return None
        row_bounds = node_interaction_bounds(row)
        band = max(40, row_bounds.height)
        for node in observation.nodes:
            if node is row or not node.enabled or node.bounds.area <= 0:
                continue
            if not any(
                field.strip() == "做同款" for field in (node.text, node.content_desc)
            ):
                continue
            if (
                row_bounds.top - band <= node.bounds.center[1] <= row_bounds.bottom + band
            ):
                return node
        return None


    def _tap_hits_same_template_label(
        self, args: dict[str, Any], observation: Observation
    ) -> bool:
        """判断点击是否落在真正的“做同款”按钮上（任务标题不算）。"""
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        row = self._same_template_row(observation)
        for node in observation.nodes:
            if node is row or not node.enabled or node.bounds.area <= 0:
                continue
            if "做同款" not in node.searchable:
                continue
            if (
                node.bounds.left - 45 <= x <= node.bounds.right + 45
                and node.bounds.top - 45 <= y <= node.bounds.bottom + 45
            ):
                return True
        return False


    def _resolve_same_template_tap(
        self, args: dict[str, Any], observation: Observation
    ) -> ToolResult | None:
        """校验并校正福利页“做同款”入口点击。

        返回 None 表示点击有效可以继续执行；否则返回带可操作提示的失败结果，
        让视觉模型知道自己应该点哪一张模板卡片。
        """
        if self._tap_hits_same_template_label(args, observation):
            return None
        if self._tap_hits_welfare_template_card(args, observation):
            return None
        if self._tap_matches_task_entry(args, observation):
            return None
        if observation.stage == "welfare" and not observation.nodes:
            # 空层级时没有本地依据，完全交给视觉模型按截图判断。
            return None
        row_action = self._same_template_row_action(observation)
        if row_action is not None:
            args.update(
                {"x": row_action.bounds.center[0], "y": row_action.bounds.center[1]}
            )
            return None
        cards = self._template_cards(observation)
        if cards:
            card = cards[0]
            args.update({"x": card.bounds.center[0], "y": card.bounds.center[1]})
            print("模型点击未落在模板卡片，已按当前可见的推荐模板卡片重新定位")
            return None
        hint = self.template_card_hint(observation)
        return ToolResult(
            False,
            "请点击福利中心中“做同款”任务入口或推荐模板卡片"
            + (f"；{hint}" if hint else ""),
            {"visual_replan": True},
        )

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
        if self.target == "same_template" and self._tap_hits_welfare_template_card(
            args, observation
        ):
            # 模型已经选中了某张模板卡片：选哪一张属于视觉决策，不改写坐标。
            return True
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


    def local_entry_action(
        self, observation: Observation
    ) -> tuple[str, dict[str, int]] | None:
        """返回当前页面可以直接执行的本地动作；None 表示需要视觉模型决策。

        任务行、行动按钮和推荐模板按钮都是稳定语义，本地定位比“让模型先点、
        再被本地改写”更准，也省一次视觉请求；只有读不到语义时才交给模型。
        """
        if self.phase == "open_target" and self.target == "same_template":
            # 做同款的入口是任意一张推荐模板卡片，选哪一张属于视觉决策，
            # 仍由模型按最新截图挑选；这里只负责把离屏的任务行带回视口。
            scroll_action = self._offscreen_entry_scroll_action(observation)
            if scroll_action is not None:
                return scroll_action
            if (
                not self.same_template_revealed
                and observation.stage == "welfare"
                and not observation.nodes
            ):
                self.same_template_revealed = True
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
            return None
        if (
            self.phase == "perform"
            and self.target == "same_template"
            and self.substate.get("entry_clicked")
            and not self.substate.get("template_clicked")
        ):
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
        return self._offscreen_entry_scroll_action(observation)


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
