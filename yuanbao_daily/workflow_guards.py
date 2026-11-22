"""分发守卫层：动作前置条件、阶段白名单与已知误触的处理。

``Workflow.dispatch`` 只负责编排顺序，具体“为什么这次点击被拒绝”集中
放在这里，便于逐条阅读与单独测试。
"""

from __future__ import annotations

from typing import Any

from .constants import (
    EXTRA_TASK_FORBIDDEN_MARKERS,
    PACKAGE_NAME,
    TASK_STEPS,
    TEXT_TASKS,
    VISUAL_NAVIGATION_TOOLS,
)
from .errors import AgentError, ManualActionRequired
from .models import Observation, ToolResult
from .ui import find_node
from .welfare import is_welfare_activity
from .workflow_policy import PHASE_ALLOWED_TOOLS


class WorkflowGuardsMixin:
    """按职责拆分的混入类；方法实现与原主类完全一致。"""

    def _dismiss_reward_if_needed(
        self, name: str, observation: Observation
    ) -> ToolResult | None:
        """福利页出现延迟奖励弹窗时先收下，避免它挡住业务动作。"""
        if (
            self.phase
            not in {
                "report",
                "open_target",
                "open_exchange",
                "daily_ability",
                "daily_ability_wait",
                "verify_daily_ability",
                "verify_daily_ability_wait",
            }
            or observation.stage != "welfare"
        ):
            return None
        dismiss_reward = getattr(self.executor, "_dismiss_reward_popup", None)
        if callable(dismiss_reward) and dismiss_reward(observation):
            # 奖励遮罩关闭动画期间，WebView 无障碍树可能短暂被识别为 app；
            # 保留福利上下文，让下一次观测按 WelfareWebActivity 归一回 welfare。
            setattr(self.executor, "_welfare_context", True)
            return ToolResult(
                True,
                "已收下延迟出现的任务奖励；本次业务动作未执行，请根据最新页面继续",
                {"stage": "welfare", "reward_dismissed": True},
            )
        return None

    def _dispatch_freeform(
        self, name: str, args: dict[str, Any], observation: Observation
    ) -> ToolResult:
        """恢复/接管期间 VLM 自由决策：跳过阶段白名单与常规前置条件。"""
        if name == "report_tasks":
            report_args = (
                args
                if self.config.force_all_daily_tasks
                else self._merge_local_report(args, observation)
            )
            result = self._parse_report(
                report_args, observation
            )
        elif name == "verify_daily_ability":
            result = self._parse_daily_ability_verification(args, observation)
        elif name == "report_exchange":
            result = self._parse_exchange_report(args, observation)
        elif name == "complete_task":
            result = self._complete_task_assertions()
        else:
            result = self._execute_observed(name, args, observation)
        if not result.ok:
            return result
        if (
            self.phase == "return"
            and name in VISUAL_NAVIGATION_TOOLS
            and result.data.get("stage") != "welfare"
        ):
            semantic_navigation = getattr(self.executor, "_go_to_welfare", None)
            if callable(semantic_navigation):
                try:
                    fallback_observation = semantic_navigation()
                except ManualActionRequired:
                    raise
                except AgentError:
                    fallback_observation = None
                if (
                    fallback_observation is not None
                    and fallback_observation.stage == "welfare"
                ):
                    result = ToolResult(
                        True,
                        "视觉恢复未触发入口，已按当前页面语义导航回福利中心",
                        {"stage": "welfare"},
                    )
        if self.recovering:
            self.finish_recovery(f"{name} 执行成功")
        if name in {"report_tasks", "complete_task"}:
            return result
        return self._after_successful_action(name, args, result)

    def _guard_confirm_exchange(
        self, name: str, args: dict[str, Any], observation: Observation
    ) -> ToolResult | None:
        """兑换确认门禁：商品身份不明或不是目标卡时绝不点击确认。"""
        if self.phase != "exchange_confirm" or name != "confirm_exchange":
            return None
        if args.get("target_visible") is not True:
            return self._reject_exchange_confirm(
                "视觉模型没有确认当前确认页显示目标卡"
            )
        product_name = str(args.get("product_name") or "").strip()
        matcher = getattr(self.executor, "product_name_matches", None)
        if not product_name or not callable(matcher) or not matcher(product_name):
            return self._reject_exchange_confirm(
                f"确认页商品“{product_name or '未知'}”不是 {self.config.card_name}"
            )
        evidence = getattr(self.executor, "exchange_product_evidence", None)
        if callable(evidence):
            target_visible, foreign_visible = evidence(observation)
            if foreign_visible and not target_visible:
                return self._reject_exchange_confirm(
                    "当前页面出现其它会员/绿钻商品，未看到目标卡"
                )
        args["_verified_target_name"] = self.config.card_name
        return None

    def _reject_exchange_confirm(self, reason: str) -> ToolResult:
        """安全退出确认页，回到商城重新选择；不写入任何兑换成功状态。"""
        print(f"拒绝兑换确认：{reason}")
        self.executor.exchange_target_clicked = False
        self.executor.exchange_target_confirmed = False
        self.phase = "exchange_back"
        self.substate = {}
        return ToolResult(
            False,
            f"拒绝确认兑换：{reason}。必须先返回商城重新核对目标商品",
            {"visual_replan": True, "exchange_rejected": True},
        )

    def _guard_extra_task_safety(
        self, name: str, args: dict[str, Any], observation: Observation
    ) -> ToolResult | None:
        """新增任务只做应用内、无外部影响的动作。"""
        if self.phase != "extra_tasks":
            return None
        if name in {
            "press_back",
            "return_to_welfare",
            "open_welfare",
            "report_extra_tasks",
        }:
            return None
        activity = observation.activity or ""
        activity_lower = activity.lower()
        if (
            activity
            and PACKAGE_NAME not in activity_lower
            and not is_welfare_activity(activity_lower)
            and observation.stage not in {"picker", "photo_preview"}
        ):
            return ToolResult(
                False,
                "新增任务不允许打开外部应用或产生外部影响；请先返回元宝福利中心",
                {"stage": observation.stage, "visual_replan": True},
            )
        if observation.stage == "exchange":
            # 新增任务巡查永远不进入兑换流程；兑换只由固定流程完成后执行，
            # 且目标只有 QQ超级会员3天卡。
            return ToolResult(
                False,
                "新增任务巡查不进入兑换流程；请返回福利中心继续巡查或调用 report_extra_tasks",
                {"stage": observation.stage, "visual_replan": True},
            )
        if name == "tap":
            try:
                x, y = int(args["x"]), int(args["y"])
            except (KeyError, TypeError, ValueError):
                return None
            for node in observation.nodes:
                if not node.enabled or node.bounds.area <= 0:
                    continue
                exchange_label = any(
                    marker in node.searchable
                    for marker in ("兑换商城", "立即兑换", "确认兑换", "确定兑换")
                ) or node.text.strip() == "兑换"
                if not exchange_label and not any(
                    marker in node.searchable
                    for marker in EXTRA_TASK_FORBIDDEN_MARKERS
                ):
                    continue
                bounds = node.interaction_bounds
                if (
                    bounds.left - 45 <= x <= bounds.right + 45
                    and bounds.top - 45 <= y <= bounds.bottom + 45
                ):
                    return ToolResult(
                        False,
                        "该点击涉及兑换、邀请好友、分享、绑定或外部影响，"
                        "新增任务巡查已拒绝执行",
                        {"stage": observation.stage, "visual_replan": True},
                    )
        return None

    def _guard_phase_allowlist(self, name: str) -> ToolResult | None:
        """能力站与核验阶段的最小工具集；接管/恢复期间不限制。"""
        if self.recovering or self.takeover:
            return None
        if self.phase == "daily_ability":
            allowed = PHASE_ALLOWED_TOOLS["daily_ability"]
            if name not in allowed:
                return ToolResult(
                    False,
                    "当前阶段只允许点击主轮播卡的体验入口，或先用导航工具处理页面",
                )
        elif self.phase == "verify_daily_ability":
            allowed = PHASE_ALLOWED_TOOLS["verify_daily_ability"]
            if name not in allowed:
                return ToolResult(
                    False,
                    "必须先核验主轮播卡是否显示‘已完成’，或先用导航工具回到福利页",
                )
        return None

    def _guard_exchange_navigation(
        self, name: str, args: dict[str, Any], observation: Observation
    ) -> ToolResult | None:
        """兑换导航偏离福利中心时的纠正与入口校验。"""
        if self.phase == "open_exchange" and observation.stage not in {"welfare", "exchange"}:
            # 兑换导航误触后可能落到“我们”页或其它活动页。先让 VLMM 按当前
            # 截图把页面带回福利中心；此时不能校验兑换入口坐标，也不能报告商城。
            if name == "report_exchange":
                return ToolResult(
                    False,
                    "当前不在兑换商城，必须先根据最新截图回到福利中心",
                    {"stage": observation.stage, "visual_replan": True},
                )
            if name in VISUAL_NAVIGATION_TOOLS:
                result = self._run_executor(name, args, observation)
                if not result.ok:
                    return result
                stage = result.data.get("stage", "")
                if stage == "welfare":
                    self.substate.pop("entry_clicked", None)
                    return ToolResult(
                        True,
                        "已回到福利中心，请重新定位兑换商城入口",
                        {"stage": stage},
                    )
                return ToolResult(
                    False,
                    "当前仍未回到福利中心，请依据最新截图换策略",
                    {"stage": stage, "visual_replan": True},
                )
        if self.phase == "open_exchange" and name == "tap":
            if not self._correct_exchange_entry_tap(args, observation):
                return ToolResult(
                    False,
                    "当前点击没有落在兑换商城入口；请根据最新截图重新定位",
                    {"visual_replan": True},
                )
            prepare = getattr(self.executor, "prepare_visual_exchange_navigation", None)
            if callable(prepare):
                prepare()
        return None

    def _guard_daily_ability_experience(
        self, name: str, args: dict[str, Any], observation: Observation
    ) -> ToolResult | None:
        """能力体验页的入口校正，避免重复点击背景主卡。"""
        if self.phase != "daily_ability_experience":
            return None
        if (
            name == "tap"
            and observation.stage == "welfare"
            and self._tap_hits_label(args, observation, "去体验")
        ):
            return ToolResult(
                False,
                "主卡‘去体验’已经点击；当前浮层仍覆盖它，请点击浮层自身的体验入口，不要重复点击背景卡片",
                {"stage": observation.stage, "visual_replan": True},
            )
        if name == "tap":
            corrected = self._correct_daily_ability_experience_tap(args, observation)
            if corrected is not None and corrected != (args.get("x"), args.get("y")):
                args.update({"x": corrected[0], "y": corrected[1]})
                print("体验入口坐标按当前无障碍按钮边界校正")
            prepare = getattr(
                self.executor, "prepare_daily_ability_experience_navigation", None
            )
            if callable(prepare):
                prepare()
        return None

    def _guard_activity_page(
        self, name: str, observation: Observation
    ) -> ToolResult | None:
        """任务入口误开福利活动页时直接关回任务列表。"""
        if (
            observation.stage != "activity"
            or self.phase == "daily_ability_experience"
            or name not in {"tap", "swipe", "press_back", "input_test_prompt"}
        ):
            return None
        after = self.executor._close_activity_page(observation)
        if after.stage == "welfare":
            return ToolResult(
                False,
                "任务入口打开了福利活动页，已返回任务列表，请重新定位入口",
                {"stage": "welfare", "visual_replan": True},
            )
        return ToolResult(
            False,
            f"任务入口打开了福利活动页，返回后仍未回到任务列表（阶段={after.stage or '未知'}）",
            {"stage": after.stage or "", "visual_replan": True},
        )

    def _guard_picker_stage(self, name: str, observation: Observation) -> ToolResult | None:
        """图片任务已经进入系统选择器时必须改用选图工具。"""
        if (
            name in {"tap", "swipe", "press_back"}
            and self.target in {"image", "photo_question"}
            and observation.stage == "picker"
        ):
            return ToolResult(
                False,
                "当前已在系统图片选择器，必须调用 select_local_image 选择测试图片",
                {"visual_replan": True},
            )
        return None

    def _guard_task_entry_tap(
        self, name: str, args: dict[str, Any], observation: Observation
    ) -> ToolResult | None:
        """任务入口点击必须落在目标行，且不能点到被忽略的邀请任务。"""
        if (
            name == "tap"
            and self.phase == "open_target"
            and observation.stage == "welfare"
            and not self._correct_task_entry_tap(args, observation)
        ):
            return ToolResult(
                False,
                "当前点击没有落在目标任务行；请根据最新截图重新定位，目标不在视口内时先滑动",
                {"visual_replan": True},
            )
        if (
            name == "tap"
            and observation.stage == "welfare"
            and self._ignored_welfare_tap(args, observation)
        ):
            return ToolResult(False, "已拒绝点击“邀请新用户”任务", {"visual_replan": True})
        return None

    def _guard_same_template(
        self, name: str, args: dict[str, Any], observation: Observation
    ) -> ToolResult | None:
        """做同款的入口与模板按钮校验。"""
        if name != "tap" or self.target != "same_template":
            return None
        if self.phase == "open_target":
            # 福利页的推荐模板卡片本身就是做同款入口：模型可以自由选择
            # 任意一张卡片，点击落空时再按当前可见卡片校正。
            return self._resolve_same_template_tap(args, observation)
        if self.phase != "perform":
            return None
        if self.substate.get("template_clicked", False):
            return ToolResult(False, "推荐模板已经点击，下一步应直接发送并等待生成")
        if not self.substate.get("entry_clicked", False):
            if observation.stage != "welfare" or not self._tap_hits_label(
                args, observation, "做同款", allow_empty_welfare=True
            ):
                return ToolResult(
                    False,
                    "请先点击福利中心中“做同款”任务入口",
                    {"visual_replan": True},
                )
            return None
        if self._tap_hits_label(args, observation, "做同款"):
            return None
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
            return None
        if observation.stage == "app" and self._tap_hits_template_card(args, observation):
            return None
        fallback = self._template_card_fallback(observation)
        if fallback is None:
            return ToolResult(False, "请在推荐模板页点击任意一个“做同款”按钮")
        # 元宝新版把“做同款”文字放在 WebView 卡片的图片层，
        # 无效坐标无法通过无障碍文字校验；只在大型卡片已确认时取首张中心。
        args.update({"x": fallback[0], "y": fallback[1]})
        return None

    def _guard_task_preconditions(
        self, name: str, observation: Observation
    ) -> ToolResult | None:
        """输入、发送、确认和等待的本地前置条件。"""
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
        return None
