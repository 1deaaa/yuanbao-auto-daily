"""编排层入口：阶段状态机、任务推进与完成断言。

具体实现按职责拆在混入类中：

- ``workflow_state`` 同日进度恢复与任务推进；
- ``workflow_entries`` 任务行语义与入口定位；
- ``workflow_guards`` 动作前置条件、阶段白名单与已知误触的处理；
- ``workflow_reports`` 福利页进度、兑换读数与能力站核验；
- ``workflow_recovery`` 卡住恢复、语义救援与阶段白名单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .constants import (
    DAILY_ABILITY_EXPERIENCE_ACTION_MARKERS,
    DAILY_ABILITY_EXPERIENCE_TOOLS,
    DAILY_ABILITY_RULE_NAME,
    DAILY_ABILITY_RULE_VERSION,
    DAILY_ABILITY_TOOLS,
    DAILY_TASK_MODE_ABILITY_STATION,
    FORCED_DAILY_TASKS,
    IMAGE_TASKS,
    LOCAL_DETERMINISTIC_TOOLS,
    NEW_WELFARE_DAILY_TASKS,
    TASK_LABELS,
    TASK_ORDER,
    TASK_STATE_SCHEMA,
    TASK_STEPS,
    TEXT_TASKS,
    VISUAL_EXCHANGE_TOOLS,
    VISUAL_NAVIGATION_TOOLS,
)
from .errors import AgentError, ManualActionRequired
from .executor import ToolExecutor
from .models import Node, Observation, ToolResult
from .state import read_daily_state, write_daily_state
from .ui import find_node, find_semantic_node, node_interaction_bounds
from .welfare import (
    has_authoritative_local_task_catalog,
    has_complete_new_welfare_task_catalog,
    has_visible_exchange_product,
    is_new_welfare_page,
    merge_supplemental_task_progress,
    parse_local_welfare_progress,
)
from .workflow_ability import WorkflowAbilityMixin
from .workflow_entries import WorkflowEntriesMixin
from .workflow_guards import WorkflowGuardsMixin
from .workflow_recovery import WorkflowRecoveryMixin
from .workflow_reports import WorkflowReportsMixin
from .workflow_state import WorkflowStateMixin


@dataclass
class Workflow(
    WorkflowStateMixin,
    WorkflowAbilityMixin,
    WorkflowEntriesMixin,
    WorkflowGuardsMixin,
    WorkflowReportsMixin,
    WorkflowRecoveryMixin,
):
    """组合各编排能力；对外只暴露 ``dispatch`` 工具入口。"""

    config: Config
    executor: ToolExecutor
    daily_ability_done: bool = False
    daily_ability_evidence: str | None = None
    daily_ability_click_attempts: int = 0
    daily_ability_button_checks: int = 0
    daily_ability_verification_checks: int = 0
    daily_ability_experience_opened: bool = False
    daily_ability_voice_submitted: bool = False
    daily_ability_experience_steps: int = 0
    task_targets: dict[str, int] = field(default_factory=dict)
    progress: dict[str, int | None] = field(default_factory=lambda: {key: None for key in TASK_ORDER})
    phase: str = "navigate_welfare"
    target: str | None = None
    substate: dict[str, bool] = field(default_factory=dict)
    expected_count: int | None = None
    finished: bool = False
    entry_scroll_attempts: dict[str, int] = field(default_factory=dict)
    state_restored: bool = False
    skipped_tasks: set[str] = field(default_factory=set)
    confirmed_unavailable_tasks: set[str] = field(default_factory=set)
    same_template_revealed: bool = False
    terminal_restore: bool = False
    recovering: bool = False
    recover_phase: str | None = None
    recover_target: str | None = None
    recover_attempts: int = 0

    def dispatch(self, name: str, args: dict[str, Any], observation: Observation) -> ToolResult:
        """执行一个工具动作；前置条件与阶段约束由守卫层逐条判定。"""
        dismissed = self._dismiss_reward_if_needed(name, observation)
        if dismissed is not None:
            return dismissed
        # 卡住恢复期间必须先允许模型处理遮挡层、返回键或重新定位；如果先套用
        # 正常阶段白名单，verify_daily_ability 会把恢复动作直接拦截，恢复机制
        # 实际上无法工作。
        allowlist_error = self._guard_phase_allowlist(name)
        if allowlist_error is not None:
            return allowlist_error
        if self.recovering:
            return self._dispatch_recovery(name, args, observation)
        if (
            self.phase == "navigate_welfare"
            and name == "report_tasks"
            and observation.stage != "welfare"
        ):
            return ToolResult(
                False,
                "尚未进入福利中心，不能报告任务进度",
                {"visual_replan": True},
            )
        exchange_error = self._guard_exchange_navigation(name, args, observation)
        if exchange_error is not None:
            return exchange_error
        if self.phase == "open_exchange" and name == "report_exchange":
            return self._parse_exchange_report(args, observation)
        phase_error = self._valid_phase(name)
        if phase_error:
            return ToolResult(False, phase_error)
        if name == "continue_daily_ability":
            result = self._continue_daily_ability(args, observation)
            if not result.ok:
                return result
            return self._after_successful_action(name, args, result, observation)
        experience_error = self._guard_daily_ability_experience(
            name, args, observation
        )
        if experience_error is not None:
            return experience_error
        activity_error = self._guard_activity_page(name, observation)
        if activity_error is not None:
            return activity_error
        picker_error = self._guard_picker_stage(name, observation)
        if picker_error is not None:
            return picker_error
        entry_error = self._guard_task_entry_tap(name, args, observation)
        if entry_error is not None:
            return entry_error
        same_template_error = self._guard_same_template(name, args, observation)
        if same_template_error is not None:
            return same_template_error
        precondition_error = self._guard_task_preconditions(name, observation)
        if precondition_error is not None:
            return precondition_error
        if name == "report_tasks":
            return self._parse_report(
                self._merge_local_report(args, observation), observation
            )
        if name == "verify_daily_ability":
            result = self._parse_daily_ability_verification(args, observation)
            if not result.ok:
                return result
            return self._after_successful_action(name, args, result, observation)
        if name == "complete_task":
            return self._complete_task_assertions()
        if name == "input_test_prompt":
            set_variant = getattr(self.executor, "set_prompt_variant", None)
            if callable(set_variant):
                set_variant(self._prompt_variant())
        result = self._execute_observed(name, args, observation)
        if (
            self.phase == "return"
            and name in {"tap", "press_back"}
            and result.data.get("stage") != "welfare"
        ):
            # 返回入口可能被前景控件遮挡，甚至在执行器报告失败时也要尝试
            # 当前页面的语义导航；成功回到福利页后由本地状态统一推进。
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
                        "返回入口未响应，已按当前页面语义导航回福利中心",
                        {"stage": "welfare"},
                    )
        if not result.ok:
            return result
        if self.phase == "open_exchange" and name == "tap" and result.data.get("stage") == "welfare":
            return ToolResult(
                False,
                "兑换商城入口点击后仍停留在福利页，请依据最新截图重新定位",
                {"stage": "welfare", "visual_replan": True},
            )
        return self._after_successful_action(name, args, result, observation)

    def _after_successful_action(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        observation: Observation | None = None,
    ) -> ToolResult:
        """统一推进正常动作和卡住恢复动作后的工作流状态。"""
        if (
            self.phase == "return"
            and name in VISUAL_NAVIGATION_TOOLS
            and result.data.get("stage") != "welfare"
        ):
            return ToolResult(
                False,
                "视觉导航尚未进入福利中心；请根据最新截图优先打开左上角抽屉并点击“福利中心”，"
                "仅当没有抽屉入口时才尝试旧版“我们”页路径",
                {"stage": result.data.get("stage", ""), "visual_replan": True},
            )
        if self.phase == "open_target" and name in {"tap", "press_back"}:
            if not self._target_entry_ready(result):
                # 点击后没有当前目标页证据，不能把残留 WebView、活动页或错误
                # 首页推进成 perform；下一轮把最新截图重新交给 VLMM。
                stage = result.data.get("stage", "")
                return ToolResult(
                    False,
                    f"任务入口点击后未确认目标页（当前阶段={stage or '未知'}），请依据最新截图换策略",
                    {
                        "stage": stage,
                        "visual_replan": True,
                    },
                )
            self.phase = "perform"
            self.substate = {}
            if self.target == "same_template" and name == "tap":
                self.substate["entry_clicked"] = True
                activity = str(result.data.get("activity", "")).lower()
                if "aitemplatedetailactivity" in activity:
                    # 入口卡片直接打开模板详情时，模板已经被选中，下一步应发送。
                    self.substate["template_clicked"] = True
            return result
        if self.phase == "navigate_welfare" and (
            name == "open_welfare" or result.data.get("stage") == "welfare"
        ):
            if self.terminal_restore:
                # 先读一次福利页任务目录再决定是否直接收尾。
                self.phase = "report"
            elif self.daily_ability_done:
                self.phase = "report"
            else:
                self.phase = "daily_ability"
            # 恢复状态只能提供已完成进度，不能替代当前页面目录；启动视觉
            # 导航可能经历改版、任务移除或页面滚动，必须再次报告当前目录。
        elif self.phase in {
            "daily_ability",
            "verify_daily_ability",
            "daily_ability_experience",
            "daily_ability_wait",
            "verify_daily_ability_wait",
        }:
            return self._advance_daily_ability_phase(name, args, result)
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
        elif self.phase == "return" and (
            name == "return_to_welfare" or result.data.get("stage") == "welfare"
        ):
            self._advance_after_task()
        elif self.phase == "open_exchange" and name == "tap":
            stage = result.data.get("stage", "")
            if stage != "exchange":
                self.substate.pop("entry_clicked", None)
                return ToolResult(
                    False,
                    "兑换商城入口点击后未进入兑换商城，请依据最新截图重新定位",
                    {"stage": stage, "visual_replan": True},
                )
            # 只有执行层确认当前页面已经是兑换商城，下一轮才允许模型报告
            # 积分和目标商品；误回“我们”、活动页或未知页不能推进兑换阶段。
            self.substate["entry_clicked"] = True
        elif self.phase == "open_exchange" and name == "open_exchange":
            # 保留旧调用方的兼容路径；正常运行时入口已由 VLMM 点击。
            self.phase = "redeem"
        elif self.phase == "redeem" and name == "redeem_qq_card":
            if (
                result.data.get("reward_use_confirmed")
                or self.executor.reward_use_confirmed
                or result.data.get("reward_unavailable")
                or getattr(self.executor, "reward_unavailable", False)
            ):
                self.phase = "return_home"
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
                self.phase = "return_home"
        elif self.phase == "return_home" and name == "return_to_home":
            self.phase = "complete"
        return result


    def _execute_observed(
        self, name: str, args: dict[str, Any], observation: Observation
    ) -> ToolResult:
        """对需要严格对应当前截图的动作，复用模型本轮看到的同一份观测。"""
        if name == "open_daily_ability":
            opener = getattr(self.executor, "_open_daily_ability", None)
            if callable(opener):
                return opener(args, observation)
        if name == "continue_daily_ability":
            return self._continue_daily_ability(args, observation)
        return self.executor.execute(name, args)


    @staticmethod
    def _continue_daily_ability(
        args: dict[str, Any], observation: Observation
    ) -> ToolResult:
        """接管当前已打开的能力浮层，不重复点击福利卡片。"""
        if observation.stage != "welfare":
            return ToolResult(False, "已打开体验页面的恢复入口仅适用于福利 WebView")
        if args.get("experience_visible") is not True:
            return ToolResult(False, "只有最新截图明确显示体验浮层时才能继续接管")
        return ToolResult(
            True,
            "当前截图显示能力体验已经打开，已交给视觉模型继续处理",
            {
                "stage": observation.stage,
                "ability_station_clicked": True,
                "ability_station_evidence": "existing_experience_detected",
            },
        )


    def _correct_daily_ability_experience_tap(
        self, args: dict[str, Any], observation: Observation
    ) -> tuple[int, int] | None:
        """把接近浮层行动按钮的视觉点校正到本轮无障碍边界。"""
        try:
            x, y = int(args["x"]), int(args["y"])
            width, height = int(self.executor.width), int(self.executor.height)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if width <= 0 or height <= 0:
            return None
        candidates = []
        for node in observation.nodes:
            bounds = node.bounds if node.bounds.area > 0 else node.parent_bounds
            searchable = node.searchable
            if (
                not node.enabled
                or bounds.area <= 0
                or not any(marker in searchable for marker in DAILY_ABILITY_EXPERIENCE_ACTION_MARKERS)
            ):
                continue
            center_x, center_y = bounds.center
            max_distance = max(120, round(height * 0.10))
            if (
                bounds.left - 65 <= x <= bounds.right + 65
                and abs(y - center_y) <= max_distance
            ):
                candidates.append(
                    ((x - center_x) ** 2 + (y - center_y) ** 2, (center_x, center_y))
                )
        return min(candidates, default=(0, None), key=lambda item: item[0])[1]
