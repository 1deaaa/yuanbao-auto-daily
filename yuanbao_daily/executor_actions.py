"""工具分派层：每个受限工具一个独立处理方法。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
import time

from .constants import (
    WRITING_SUBMIT_VERIFY_ATTEMPTS,
    WRITING_SUBMIT_VERIFY_INTERVAL,
)
from .errors import (
    AgentError,
)
from .models import (
    Observation,
    ToolResult,
)
from .ui import find_voice_press_point
from .welfare import is_welfare_activity


class ExecutorActionsMixin:
    """按职责拆分的混入类；方法实现与原主类完全一致。"""

    # 工具名 -> 处理方法；新增工具只需实现同名处理方法。
    _TOOL_HANDLERS = {
        "tap": "_do_tap",
        "hold_to_speak": "_do_hold_to_speak",
        "long_press": "_do_long_press",
        "swipe": "_do_swipe",
        "press_back": "_do_press_back",
        "open_welfare": "_do_open_welfare",
        "open_daily_ability": "_do_open_daily_ability",
        "input_test_prompt": "_do_input_test_prompt",
        "send_message": "_do_send_message",
        "wait_5s": "_do_wait_5s",
        "select_local_image": "_do_select_local_image",
        "confirm_image": "_do_confirm_image",
        "claim_reward": "_do_claim_reward",
        "return_to_welfare": "_do_return_to_welfare",
        "open_exchange": "_do_open_exchange",
        "redeem_qq_card": "_do_redeem_qq_card",
        "confirm_exchange": "_do_confirm_exchange",
        "open_prize_records": "_do_open_prize_records",
        "use_bound_reward": "_do_use_bound_reward",
        "confirm_reward_use": "_do_confirm_reward_use",
        "return_to_home": "_do_return_to_home",
        "report_tasks": "_do_report_tasks",
        "complete_task": "_do_complete_task",
    }

    def _do_tap(self, args: dict[str, Any]) -> ToolResult:
        ability_experience_action = bool(
            getattr(self, "_daily_ability_experience_navigation", False)
        )
        self._daily_ability_experience_navigation = False
        x, y = int(args["x"]), int(args["y"])
        if x >= self.width or y >= self.height or x < 0 or y < 0:
            raise AgentError(f"坐标越界：{x},{y}")
        before_tap = self._initial_observation()
        experience_navigation_expected = ability_experience_action and self._daily_ability_navigation_button_visible(
            before_tap
        )
        covered = self._foreground_label_covering_tap(
            before_tap, x, y, self.width, self.height
        )
        if covered is not None:
            return self._result(
                False,
                f"视觉目标被前景交互层（{covered}）遮挡，请先根据最新截图关闭浮层",
                stage=before_tap.stage,
                visual_replan=True,
            )
        if ability_experience_action:
            resolved_x, resolved_y = x, y
        else:
            resolved_x, resolved_y = self._resolve_tap_point(before_tap, x, y)
        if (resolved_x, resolved_y) != (x, y):
            args.update({"x": resolved_x, "y": resolved_y})
            x, y = resolved_x, resolved_y
            print("视觉点击落在文字节点，已提升到当前层级的可点击父控件")
        was_welfare_context = self._welfare_context and not ability_experience_action
        # 任务入口点击后，下一次观测必须重新判定真实页面。若继续保留
        # 福利上下文，WebView 的空/滞后无障碍树会把目标页伪装成福利页，
        # 后续状态机就会在错误页面上重规划。
        if was_welfare_context:
            self._welfare_context = False
        self._tap_point(x, y)
        if experience_navigation_expected:
            after_tap = self._observe_after_daily_ability_action(
                before_tap, navigation_expected=True
            )
        else:
            time.sleep(0.8)
            after_tap = self.observe()
        # 福利页任务行动按钮是 WebView 的异步路由入口。短暂仍显示福利页
        # 不代表点击失败，必须给路由和新 Activity 一个有界加载窗口。
        if was_welfare_context:
            after_tap = self._observe_after_entry_tap()
        stage = after_tap.stage
        if stage == "welfare" and was_welfare_context:
            # 福利 WebView 的旧层级可能在第一次点击后继续返回，导致入口循环。
            # 清掉福利上下文后按本轮已验证的原始入口位置重试一次，让下一次
            # 观测按真实 Activity/聊天层级判断；最多一次，避免重复误点。
            self._welfare_context = False
            time.sleep(0.5)
            self._tap_point(x, y)
            time.sleep(1.0)
            after_retry = self.observe()
            if after_retry.stage != "welfare":
                after_tap = after_retry
                stage = after_retry.stage
        if stage != "welfare":
            self._welfare_context = False
        if stage == "welfare" and was_welfare_context:
            return self._result(
                False,
                "任务入口点击后仍停留在福利中心，已等待异步导航仍未切换页面",
                stage=stage,
                visual_replan=True,
            )
        return self._result(
            True,
            "已点击指定位置",
            stage=stage,
            activity=after_tap.activity,
            entry_evidence=self._entry_evidence(after_tap),
        )


    def _do_hold_to_speak(self, args: dict[str, Any]) -> ToolResult:
        """按住语音输入控件并注入短测试语音。

        控件位置由本地按“按住说话”语义定位，模型只负责表达意图，避免它在
        多变布局上猜错长按对象而误触其它控件。
        """
        observation = self._initial_observation()
        point = find_voice_press_point(observation.nodes)
        if point is None:
            return self._result(
                False,
                "当前页面没有找到语音输入控件，请根据最新截图确认页面要求",
                stage=observation.stage,
                visual_replan=True,
            )
        duration = max(600, min(10000, int(args.get("duration_ms", 2500))))
        return self._press_control(point[0], point[1], duration, inject_audio=True)

    def _do_long_press(self, args: dict[str, Any]) -> ToolResult:
        x, y = int(args["x"]), int(args["y"])
        duration = max(600, min(10000, int(args.get("duration_ms", 2000))))
        return self._press_control(
            x,
            y,
            duration,
            inject_audio=args.get("_daily_ability_voice") is True,
        )

    def _press_control(
        self,
        x: int,
        y: int,
        duration: int,
        inject_audio: bool = False,
    ) -> ToolResult:
        """长按页面控件；语音场景额外注入一段短测试语音。"""
        observation = self._initial_observation()
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise AgentError(f"长按坐标越界：{x},{y}")
        covered = self._foreground_label_covering_tap(
            observation, x, y, self.width, self.height
        )
        if covered is not None:
            return self._result(
                False,
                f"长按目标被前景交互层（{covered}）遮挡，请先处理遮挡层",
                stage=observation.stage,
                visual_replan=True,
            )
        audio_session = None
        if inject_audio:
            audio_session = self._start_test_audio()
            if audio_session is not None:
                time.sleep(0.15)
        try:
            self.device.input(
                "swipe", str(x), str(y), str(x), str(y), str(duration)
            )
        finally:
            self._finish_test_audio(audio_session)
        self.pending_generation = True
        time.sleep(0.5)
        after = self.observe()
        return self._result(
            True,
            "已长按并释放页面控件，下一步调用 wait_5s 等待结果",
            stage=after.stage,
            audio_injected=audio_session is not None,
        )


    def _do_swipe(self, args: dict[str, Any]) -> ToolResult:
        values = [int(args[key]) for key in ("x1", "y1", "x2", "y2")]
        if any(value < 0 for value in values):
            raise AgentError("滑动坐标不能为负数")
        start = (min(values[0], self.width - 1), min(values[1], self.height - 1))
        end = (min(values[2], self.width - 1), min(values[3], self.height - 1))
        duration = max(100, min(2000, int(args.get("duration_ms", 500))))
        self.device.input("swipe", str(start[0]), str(start[1]), str(end[0]), str(end[1]), str(duration))
        time.sleep(1)
        return self._result(True, "已完成滑动", stage=self.observe().stage)


    def _do_press_back(self, args: dict[str, Any]) -> ToolResult:
        self.device.input("keyevent", "KEYCODE_BACK")
        time.sleep(1)
        after_back = self.observe()
        return self._result(
            True,
            "已返回上一页",
            stage=after_back.stage,
            activity=after_back.activity,
            entry_evidence=self._entry_evidence(after_back),
        )


    def _do_open_welfare(self, args: dict[str, Any]) -> ToolResult:
        after = self._go_to_welfare()
        return self._result(True, "已进入福利中心", stage=after.stage)


    def _do_open_daily_ability(self, args: dict[str, Any]) -> ToolResult:
        return self._open_daily_ability(args, self._initial_observation())


    def _do_input_test_prompt(self, args: dict[str, Any]) -> ToolResult:
        return self._input_fixed_prompt(self._initial_observation())


    def _do_send_message(self, args: dict[str, Any]) -> ToolResult:
        observation = self._initial_observation()
        screenshot_chat_fallback = False
        keyboard_shown = getattr(self.device, "keyboard_shown", None)
        if callable(keyboard_shown) and keyboard_shown():
            if self.prompt_variant:
                # 趣问页面按返回键收起键盘时，元宝会把附件底部面板展开；
                # 保留键盘，由 _find_send 按当前输入框相对位置定位箭头。
                pass
            else:
                # 写作页等其它页面仍先收起键盘，再按当前层级重新定位提交控件。
                self.device.input("keyevent", "KEYCODE_BACK")
                time.sleep(0.4)
                observation = self.observe()
        # 附件面板可能在输入文本完成后再次出现；发送动作必须在当前层级
        # 重新收起它，不能只依赖 input_test_prompt 阶段的早期清理。
        attachment_prepared = False
        if not attachment_prepared and self._dismiss_chat_attachment_sheet(observation):
            observation = self.observe()
            if self._chat_attachment_sheet_visible(observation):
                # 关闭动画结束后面板可能又弹回一次；在最终发送前再拖动当前
                # 横杆，避免把一次过渡帧当成已经关闭。
                observation = self._drag_chat_attachment_sheet_down(observation)
                if self._chat_attachment_sheet_visible(observation):
                    return self._result(
                        False,
                        "聊天页附件面板仍遮挡发送入口，已停止本次点击，请重新观察后再发送",
                        stage=observation.stage,
                    )
        if self.prompt_variant and observation.stage != "writing":
            # 附件面板关闭后可能仍报告为 app/chat，但无障碍树没有恢复输入框；
            # 只要截图确认已经是正常聊天页，就统一走发送箭头截图兜底。
            if self._find_input(observation) is None:
                marker_nodes = self._chat_attachment_marker_nodes(observation)
                visual_state = self._chat_attachment_sheet_visual_state(
                    observation, marker_nodes
                )
                if visual_state is False:
                    screenshot_chat_fallback = True
                else:
                    return self._result(
                        False,
                        "当前仍未确认聊天输入层，已停止发送并等待页面重新稳定",
                        stage=observation.stage,
                    )
        if observation.stage in {"chat", "writing"}:
            input_node = self._find_input(observation)
            input_text = input_node.text.strip() if input_node is not None else ""
            variant = getattr(self, "prompt_variant", None)
            if observation.stage == "writing" or variant:
                expected_prompt = self._fixed_prompt().strip()
                # 发送动作可能在 Compose 重绘前已经提交成功，下一次观测却
                # 只看到空输入框或占位状态。若当前会话已经出现本轮问题，
                # 直接把它视为已发送，禁止再次点击发送按钮。
                sent_message_visible = any(
                    node.bounds.area > 0
                    and node.text.strip() == expected_prompt
                    and not node.class_name.lower().endswith("edittext")
                    for node in observation.nodes
                )
                if sent_message_visible:
                    self.pending_generation = True
                    return self._result(
                        True,
                        "当前会话已出现本轮提问消息，确认发送完成；下一步必须调用 wait_5s",
                        stage=observation.stage,
                    )
                if input_text != expected_prompt:
                    return self._result(
                        False,
                        "当前输入框内容与本轮固定测试文本不一致，已拒绝提交",
                        stage=observation.stage,
                    )
            elif not input_text:
                return self._result(False, "输入框为空，必须先调用 input_test_prompt")
        # 截图兜底表示无障碍树仍包含面板/缩略图节点；这些残留小图标可能
        # 被 _find_send 误判为箭头，直接使用本轮聊天截图对应的位置。
        button = None if screenshot_chat_fallback else self._find_send(observation)
        if button is None:
            if observation.stage == "writing":
                return self._result(
                    False,
                    "写作表单中未找到相对输入框定位的提交控件，未执行点击",
                    stage=observation.stage,
                )
            input_node = self._find_input(observation)
            if screenshot_chat_fallback:
                fallback_x = round(self.width * 0.926)
                keyboard_visible = getattr(self.device, "keyboard_shown", None)
                keyboard_visible = bool(keyboard_visible()) if callable(keyboard_visible) else False
                fallback_y = round(self.height * (0.531 if keyboard_visible else 0.896))
                self._tap_point(fallback_x, fallback_y)
                self.pending_generation = True
                return self._result(
                    True,
                    "无障碍树滞后，已按最新聊天截图点击发送箭头，下一步必须调用 wait_5s",
                    stage=observation.stage,
                )
            if input_node is not None:
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
        if observation.stage == "writing":
            after = self.observe()
            variant = getattr(self, "prompt_variant", None)
            for _ in range(WRITING_SUBMIT_VERIFY_ATTEMPTS):
                marker_visible = not variant or any(
                    variant in node.text for node in after.nodes
                )
                if after.stage == "chat" and marker_visible:
                    break
                time.sleep(WRITING_SUBMIT_VERIFY_INTERVAL)
                after = self.observe()
            marker_visible = not variant or any(
                variant in node.text for node in after.nodes
            )
            if after.stage != "chat" or not marker_visible:
                return self._result(
                    False,
                    "写作表单提交后未观察到本轮消息进入会话，未标记为已发送",
                    stage=after.stage,
                )
            self.pending_generation = True
            return self._result(
                True,
                "已确认本轮写作消息进入会话，下一步必须调用 wait_5s",
                stage=after.stage,
            )
        if self.prompt_variant:
            # 聊天页的附件面板/滞后无障碍树可能让点击发送箭头变成空操作。
            # 只有当前输入框已经清空（或最新树已明确进入生成态）才标记发送成功，
            # 否则返回失败让外层先收起面板后重试，不能把未提交消息计入任务。
            after = self.observe()
            retried_send = False
            expected_prompt = self._fixed_prompt().strip()
            for attempt in range(WRITING_SUBMIT_VERIFY_ATTEMPTS):
                if not self._stale_welfare_tree(after):
                    after_input = self._find_input(after)
                    input_cleared = (
                        after_input is not None and not after_input.text.strip()
                    )
                    sent_message_visible = any(
                        node.bounds.area > 0
                        and node.text.strip() == expected_prompt
                        and not node.class_name.lower().endswith("edittext")
                        for node in after.nodes
                    )
                    if input_cleared or sent_message_visible:
                        self.pending_generation = True
                        return self._result(
                            True,
                            "已确认本轮提问消息进入会话，下一步必须调用 wait_5s",
                            stage=after.stage,
                        )
                if not retried_send and attempt == 0:
                    # Compose 发送箭头偶尔在首个点击后仍保留输入内容；重新按
                    # 当前树定位一次，避免把未提交的问题误标为成功。
                    if self._dismiss_chat_attachment_sheet(after):
                        after = self.observe()
                        if self._chat_attachment_sheet_visible(after):
                            return self._result(
                                False,
                                "聊天页附件面板仍遮挡发送入口，已停止重试点击",
                                stage=after.stage,
                            )
                    retry_button = self._find_send(after)
                    if retry_button is not None:
                        self._tap_node(retry_button)
                    retried_send = True
                time.sleep(WRITING_SUBMIT_VERIFY_INTERVAL)
                after = self.observe()
            return self._result(
                False,
                "发送后固定测试文本仍停留在输入框，未确认消息提交；先处理附件面板后重试",
                stage=after.stage,
            )
        self.pending_generation = True
        return self._result(True, "已发送，下一步必须调用 wait_5s", stage=self.observe().stage)


    def _do_wait_5s(self, args: dict[str, Any]) -> ToolResult:
        return self._wait_generation()


    def _do_select_local_image(self, args: dict[str, Any]) -> ToolResult:
        return self._ensure_picker_image()


    def _do_confirm_image(self, args: dict[str, Any]) -> ToolResult:
        reselected_from_camera = False
        for attempt in range(3):
            if self._tap_text_button("确认", "使用", "完成"):
                return self._result(True, "已确认图片", stage=self.observe().stage)
            observation = self.observe()
            if (
                not reselected_from_camera
                and observation.stage == "photo_question"
                and "cameracaptureactivity" in observation.activity.lower()
            ):
                # 部分新版拍题页第一次从相册选图只更新左下角缩略图，
                # 仍停留在相机页；再次从当前页的相册入口选图后才会
                # 进入 CameraResultActivity 并暴露“确认”按钮。
                reselected_from_camera = True
                reselection = self._ensure_picker_image()
                if not reselection.ok:
                    return reselection
            elif attempt < 2:
                time.sleep(1)
        return self._result(False, "没有找到图片确认按钮")


    def _do_claim_reward(self, args: dict[str, Any]) -> ToolResult:
        for attempt in range(3):
            observation = self._initial_observation() if attempt == 0 else self.observe()
            if self._dismiss_reward_popup(observation):
                return self._result(True, "已收下任务奖励", stage=self.observe().stage)
            if attempt < 2:
                time.sleep(2)
        return self._result(True, "未发现奖励弹窗，视为无需领取", stage=self.observe().stage)


    def _do_return_to_welfare(self, args: dict[str, Any]) -> ToolResult:
        def normalize_empty_webview(observation: Observation) -> Observation:
            if (
                observation.stage in {"", "unknown", "app", "chat"}
                and is_welfare_activity(observation.activity)
            ):
                return replace(observation, stage="welfare")
            return observation

        # 返回后的 WebView 可能先只有 Activity、截图而没有可访问树；导航函数
        # 已经完成福利入口路径，此时先接受空树，下一步再等目录异步渲染，避免
        # 把真实福利页误报成仍在聊天首页。
        after = normalize_empty_webview(self._go_to_welfare())
        # 奖励遮罩可能在生成完成数秒后才注入 WebView；返回福利页时
        # 再做一次有界本地收尾，避免下一任务的视觉定位看到奖励弹窗。
        for attempt in range(3):
            if self._dismiss_reward_popup(after):
                after = normalize_empty_webview(self.observe())
                break
            if attempt < 2:
                time.sleep(2)
                after = normalize_empty_webview(self.observe())
        if after.stage != "welfare" or not is_welfare_activity(after.activity):
            return self._result(
                False,
                "未确认已回到福利中心，当前仍是聊天或其它页面；不会推进任务报告",
                stage=after.stage,
                activity=after.activity,
                visual_replan=True,
            )
        return self._result(
            True,
            "已返回福利中心并刷新任务页面",
            stage=after.stage,
            activity=after.activity,
        )


    def _do_open_exchange(self, args: dict[str, Any]) -> ToolResult:
        return self._open_exchange()


    def _do_redeem_qq_card(self, args: dict[str, Any]) -> ToolResult:
        return self._redeem_card()


    def _do_confirm_exchange(self, args: dict[str, Any]) -> ToolResult:
        if not self.exchange_target_clicked:
            return self._result(False, "尚未定位并点击目标商品，拒绝确认兑换")
        observation = self._initial_observation()
        target_visible, foreign_visible = self.exchange_product_evidence(observation)
        reported_name = str(args.get("product_name") or "").strip()
        model_verified = bool(reported_name) and self.product_name_matches(reported_name)
        if reported_name and not model_verified:
            after = self._leave_exchange_detail()
            return self._result(
                False,
                f"确认页商品“{reported_name}”不是目标卡，已退出确认页，未执行兑换",
                stage=after.stage,
                visual_replan=True,
            )
        if foreign_visible and not target_visible:
            after = self._leave_exchange_detail()
            return self._result(
                False,
                "确认页出现其它会员/音乐商品且没有目标卡，已退出确认页，未执行兑换",
                stage=after.stage,
                visual_replan=True,
            )
        if not (
            target_visible
            or model_verified
            or getattr(self, "exchange_target_confirmed", False)
        ):
            return self._result(
                False,
                "无法确认当前确认页属于 QQ超级会员3天卡，拒绝盲点确认",
                stage=observation.stage,
                visual_replan=True,
            )
        # 商品列表的“兑换”会先进入详情页；先点击详情页的“立即兑换”。
        clicked = self._tap_text_button("立即兑换")
        after = self.observe()
        target_after, foreign_after = self.exchange_product_evidence(after)
        if foreign_after and not target_after:
            after = self._leave_exchange_detail()
            return self._result(
                False,
                "确认过程中页面出现了其它会员/音乐商品，已退出，未执行兑换",
                stage=after.stage,
                visual_replan=True,
            )
        # 某些版本在详情页后还会出现二次确认弹窗。只有目标商品仍有
        # 本地/模型证据时才允许点击确认，且不再使用泛化的“确认”兜底。
        if self._has_evidence(after, "确认兑换", "确定兑换"):
            if not (target_after or model_verified or self.exchange_target_confirmed):
                after = self._leave_exchange_detail()
                return self._result(
                    False,
                    "确认弹窗没有目标商品证据，已退出，未执行兑换",
                    stage=after.stage,
                    visual_replan=True,
                )
            clicked = self._tap_text_button("确认兑换", "确定兑换") or clicked
            after = self.observe()
            target_final, foreign_final = self.exchange_product_evidence(after)
            if foreign_final and not target_final:
                after = self._leave_exchange_detail()
                return self._result(
                    False,
                    "确认后页面显示的是其它会员/音乐商品，已退出，未记为兑换成功",
                    stage=after.stage,
                    visual_replan=True,
                )
        explicit_success = self._has_exchange_success(after)
        debit = self._exchange_debit_evidence(after)
        if not clicked and not explicit_success and debit is None:
            return self._result(False, "没有兑换确认弹窗或成功提示", stage=after.stage)
        if not explicit_success and debit is None:
            return self._result(False, "兑换后没有发现明确成功证据", stage=after.stage)
        if not explicit_success and debit is not None:
            before_points, cost, after_points = debit
            self.exchange_confirmed = True
            self._write_state("pending")
            return self._result(
                True,
                f"已确认兑换成功：积分从 {before_points} 扣减 {cost}，余额 {after_points}",
                stage=after.stage,
                points_before=before_points,
                cost=cost,
                points_after=after_points,
            )
        self.exchange_confirmed = True
        self._write_state("pending")
        return self._result(True, "已确认兑换且发现成功证据", stage=after.stage)


    def _exchange_debit_evidence(
        self, observation: Observation
    ) -> tuple[int, int, int] | None:
        """返回“兑换前积分 - 目标价格 = 当前积分”的账务证据。"""
        before_points = getattr(self, "visual_exchange_points", None)
        cost = getattr(self, "visual_exchange_cost", None)
        after_points = self._read_exchange_points(observation)
        if (
            isinstance(before_points, int)
            and isinstance(cost, int)
            and isinstance(after_points, int)
            and before_points >= cost
            and after_points == before_points - cost
        ):
            return before_points, cost, after_points
        return None


    def _do_open_prize_records(self, args: dict[str, Any]) -> ToolResult:
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


    def _do_use_bound_reward(self, args: dict[str, Any]) -> ToolResult:
        if not self.prize_records_opened:
            return self._result(False, "尚未打开奖品记录")
        button = self._find_reward_use_button(self.observe())
        if button is None:
            return self._result(False, "奖品记录中没有找到目标卡片的使用按钮")
        self._tap_node(button)
        time.sleep(2)
        self.reward_use_clicked = True
        return self._result(True, "已点击绑定账号使用入口", stage=self.observe().stage)


    def _do_confirm_reward_use(self, args: dict[str, Any]) -> ToolResult:
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


    def _do_return_to_home(self, args: dict[str, Any]) -> ToolResult:
        after = self._go_to_ours()
        destination = "新版聊天首页" if after.stage == "chat" else "旧版“我们”页面"
        return self._result(True, f"已返回元宝{destination}", stage=after.stage)


    def _do_report_tasks(self, args: dict[str, Any]) -> ToolResult:
        return self._result(True, "已记录模型观察到的任务进度")


    def _do_complete_task(self, args: dict[str, Any]) -> ToolResult:
        return self._result(True, "完成工具调用已收到")
