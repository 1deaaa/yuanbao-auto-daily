"""兑换层：积分与价格读取、兑换确认、奖品记录与绑定账号使用。"""

from __future__ import annotations

from typing import Any
import re
import time

from .constants import (
    WEBVIEW_ACTIVITY_MARKER,
    WELFARE_REWARD_BUTTON_Y_RATIO,
)
from .models import (
    Node,
    Observation,
    ToolResult,
)
from .state import (
    read_daily_state,
    write_daily_state,
)
from .ui import (
    find_node,
    png_pixel,
)
from .welfare import (
    has_visible_exchange_product,
)


class ExecutorExchangeMixin:
    """按职责拆分的混入类；方法实现与原主类完全一致。"""

    def _open_exchange(self) -> ToolResult:
        observation = self._go_to_welfare()
        # 奖励弹窗的绿色“开心收下”在部分版本只有图片语义，没有可点击文本。
        # 兑换前先收下残留奖励，避免弹窗拦截兑换商城入口。
        self._dismiss_reward_popup(observation)
        for attempt in range(5):
            observation = self.observe()
            # 点进的可能是奖品记录/积分明细等相邻 WebView：只要积分余额可读，
            # 直接核对积分与价格，积分不足就安全跳过，不必强求 exchange 阶段。
            points, cost = self._read_points_and_cost(observation)
            if points is not None and cost is not None and points < cost:
                self.reward_card_name = self.config.card_name
                self.reward_unavailable = True
                self._write_state("unavailable")
                return self._result(
                    True,
                    f"目标商品“{self.config.card_name}”需要 {cost} 积分，当前仅 {points}，已安全跳过兑换",
                    stage=observation.stage,
                    reward_unavailable=True,
                    points=points,
                    cost=cost,
                )
            if observation.stage == "exchange" and (
                has_visible_exchange_product(observation, self.config.card_name)
                or not observation.nodes
            ):
                self._last_exchange_observation = observation
                return self._result(True, "已进入兑换商城", stage=observation.stage)
            # 福利页顶部“兑换商城”是 TextView 文案节点（不可点击），不要用
            # clickable_only 查找；节点离屏或零边界时先把页面滚回顶部，
            # 不再使用与布局绑定的固定坐标。
            mall = find_node(observation.nodes, "兑换商城", exact=True)
            if mall is None:
                mall = find_node(observation.nodes, "兑换商城")
            if mall is not None and mall.bounds.area > 0:
                self._tap_node(mall)
                time.sleep(3)
                after = self.observe()
                if after.stage == "exchange" and (
                    has_visible_exchange_product(after, self.config.card_name)
                    or not after.nodes
                ):
                    self._last_exchange_observation = after
                    return self._result(True, "已进入兑换商城", stage=after.stage)
                points, cost = self._read_points_and_cost(after)
                if points is not None and cost is not None and points < cost:
                    self.reward_card_name = self.config.card_name
                    self.reward_unavailable = True
                    self._write_state("unavailable")
                    return self._result(
                        True,
                        f"目标商品“{self.config.card_name}”需要 {cost} 积分，当前仅 {points}，已安全跳过兑换",
                        stage=after.stage,
                        reward_unavailable=True,
                        points=points,
                        cost=cost,
                    )
            elif observation.stage == "welfare":
                if not any(node.bounds.area > 0 for node in observation.nodes):
                    # WebView dump 超时时截图仍可能是福利页；空层级没有足够
                    # 证据证明入口离屏，先等待下一次观测，禁止用空数据滑动页面。
                    time.sleep(1)
                    continue
                # 手指向下滑动，让页面回到顶部；下一轮仍按最新无障碍树找入口。
                self.device.input(
                    "swipe",
                    str(self.width // 2),
                    str(round(self.height * 0.28)),
                    str(self.width // 2),
                    str(round(self.height * 0.86)),
                    "500",
                )
                time.sleep(1)
        return self._result(False, "兑换商城没有加载")


    def _read_points_and_cost(
        self, observation: Observation
    ) -> tuple[int | None, int | None]:
        """在积分明细/奖品记录等相邻页直接读取余额与 3 天卡价格。

        失败报告证明：积分不足时页面可能落在 prize_records 而不是 exchange，
        但余额（顶部纯数字）与卡价（“兑换QQ超级会员3天卡”附近的 -30000）
        依然可读。读不到任一值时返回 None，调用方继续走正常兑换流程。
        """
        points: int | None = None
        for node in observation.nodes:
            if node.bounds.area <= 0 or node.bounds.top > 160:
                continue
            value = node.text.strip().replace(",", "")
            match = re.fullmatch(r"(\d+)(?:\s*积分)?", value)
            if match is None:
                continue
            points = min(points, int(match.group(1))) if points is not None else int(match.group(1))
        if points is None:
            return None, None
        cost: int | None = None
        card_index: int | None = None
        ordered = list(observation.nodes)
        for index, node in enumerate(ordered):
            label = f"{node.text} {node.content_desc}".replace("\n", "")
            if "超级会员" in label and "天卡" in label:
                card_index = index
                break
        if card_index is None:
            return points, None
        for node in ordered[card_index : card_index + 12]:
            for token in re.findall(r"-?\d[\d,]*", f"{node.text} {node.content_desc}"):
                try:
                    number = int(token.replace(",", ""))
                except ValueError:
                    continue
                magnitude = abs(number)
                if 1000 <= magnitude <= 1000000:
                    cost = magnitude if cost is None else min(cost, magnitude)
        if cost is None:
            return points, None
        return points, cost


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
        if (
            observation.stage == "welfare"
            or WEBVIEW_ACTIVITY_MARKER in observation.activity.lower()
        ):
            x = self.width // 2
            y = round(self.height * WELFARE_REWARD_BUTTON_Y_RATIO)
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
        point_labels = [
            node
            for node in observation.nodes
            if node.bounds.area > 0 and "我的积分" in node.searchable
        ]
        numeric_nodes: list[tuple[Node, int]] = []
        for node in observation.nodes:
            # 新版兑换页把“我的积分”固定在 y=128 左右；保留标题栏下方
            # 的固定积分栏，不能用旧版 y=120 的边界误删真实积分。
            if node.bounds.area <= 0 or node.bounds.top > 220:
                continue
            value = node.text.strip().replace(",", "")
            match = re.fullmatch(r"(\d+)(?:\s*积分)?", value)
            if match is None:
                continue
            numeric_nodes.append((node, int(match.group(1))))
            item = (node.bounds.left, int(match.group(1)))
            if "积分" in value:
                labelled.append(item)
            else:
                plain.append(item)
        if point_labels:
            nearby = [
                (abs(node.bounds.left - label.bounds.right), value)
                for label in point_labels
                for node, value in numeric_nodes
                if label.bounds.top - 20 <= node.bounds.top <= label.bounds.bottom + 20
                and node.bounds.left >= label.bounds.left
            ]
            if nearby:
                return min(nearby, key=lambda item: item[0])[1]
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
            if result.data.get("reward_unavailable"):
                return result
            # WebView 路由完成后紧接着的一次 dump 可能短暂复用旧福利层级；
            # 沿用刚由 _open_exchange 确认过的商城观测，避免在错误页面滚动。
            observation = getattr(self, "_last_exchange_observation", None)
            if observation is None or observation.stage != "exchange":
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
                "已从同日状态恢复：QQ超级会员3天卡积分不足，已安全跳过兑换",
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
        if points is None:
            if getattr(self, "visual_exchange_points_verified", False):
                points = self.visual_exchange_points
        if cost is None:
            cost = self.visual_exchange_cost
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
                f"目标商品“{self.config.card_name}”需要 {cost} 积分，当前仅 {points}，已安全跳过兑换",
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


    def set_visual_exchange_report(
        self, points: int, cost: int, verified: bool = False
    ) -> None:
        """保存本轮兑换截图已确认的积分和目标卡价格。"""
        self.visual_exchange_points = points
        self.visual_exchange_cost = cost
        self.visual_exchange_points_verified = verified
