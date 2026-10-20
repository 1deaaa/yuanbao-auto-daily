"""工具分派层：每个受限工具一个独立处理方法。"""

from __future__ import annotations

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
    ToolResult,
)
from .ui import find_voice_press_point, png_pixel


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
        before_tap = self.observe()
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
        observation = self.observe()
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
        observation = self.observe()
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
        return self._open_daily_ability(args, self.observe())


    def _do_input_test_prompt(self, args: dict[str, Any]) -> ToolResult:
        return self._input_fixed_prompt(self.observe())


    def _do_send_message(self, args: dict[str, Any]) -> ToolResult:
        observation = self.observe()
        if observation.stage in {"chat", "writing"}:
            input_node = self._find_input(observation)
            input_text = input_node.text.strip() if input_node is not None else ""
            variant = getattr(self, "prompt_variant", None)
            if observation.stage == "writing" or variant:
                if input_text != self._fixed_prompt().strip():
                    return self._result(
                        False,
                        "当前输入框内容与本轮固定测试文本不一致，已拒绝提交",
                        stage=observation.stage,
                    )
            elif not input_text:
                return self._result(False, "输入框为空，必须先调用 input_test_prompt")
        button = self._find_send(observation)
        if button is None:
            if observation.stage == "writing":
                return self._result(
                    False,
                    "写作表单中未找到相对输入框定位的提交控件，未执行点击",
                    stage=observation.stage,
                )
            input_node = self._find_input(observation)
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
            observation = self.observe()
            if self._dismiss_reward_popup(observation):
                return self._result(True, "已收下任务奖励", stage=self.observe().stage)
            if attempt < 2:
                time.sleep(2)
        return self._result(True, "未发现奖励弹窗，视为无需领取", stage=self.observe().stage)


    def _do_return_to_welfare(self, args: dict[str, Any]) -> ToolResult:
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


    def _do_open_exchange(self, args: dict[str, Any]) -> ToolResult:
        return self._open_exchange()


    def _do_redeem_qq_card(self, args: dict[str, Any]) -> ToolResult:
        return self._redeem_card()


    def _do_confirm_exchange(self, args: dict[str, Any]) -> ToolResult:
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
