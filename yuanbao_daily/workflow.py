"""编排层入口：阶段状态机、任务推进与完成断言。

具体实现按职责拆在混入类中：

- ``workflow_state`` 同日进度恢复与任务推进；
- ``workflow_entries`` 任务行语义与入口定位；
- ``workflow_guards`` 动作前置条件、阶段白名单与已知误触的处理；
- ``workflow_reports`` 福利页进度、兑换读数与能力站核验；
- ``workflow_recovery`` 语义救援与阶段白名单；
- ``workflow_policy`` 本地快路径适用条件与模型工具边界；
- ``workflow_takeover`` 页面漂移后的持续 VLM 接管。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import inspect
import uuid

from .config import Config
from .constants import (
    DAILY_ABILITY_EXPERIENCE_ACTION_MARKERS,
    IMAGE_TASKS,
    TASK_ORDER,
    TEXT_TASKS,
    VISUAL_NAVIGATION_TOOLS,
)
from .errors import AgentError, ManualActionRequired
from .executor import ToolExecutor
from .models import Observation, ToolResult
from .welfare import (
    is_welfare_activity,
)
from .workflow_ability import WorkflowAbilityMixin
from .workflow_entries import WorkflowEntriesMixin
from .workflow_guards import WorkflowGuardsMixin
from .workflow_recovery import WorkflowRecoveryMixin
from .workflow_reports import WorkflowReportsMixin
from .workflow_state import WorkflowStateMixin
from .workflow_takeover import WorkflowTakeoverMixin


@dataclass
class Workflow(
    WorkflowStateMixin,
    WorkflowAbilityMixin,
    WorkflowEntriesMixin,
    WorkflowGuardsMixin,
    WorkflowReportsMixin,
    WorkflowRecoveryMixin,
    WorkflowTakeoverMixin,
):
    """组合各编排能力；对外只暴露 ``dispatch`` 工具入口。"""

    config: Config
    executor: ToolExecutor
    daily_ability_done: bool = False
    daily_ability_evidence: str | None = None
    daily_ability_unavailable: bool = False
    daily_ability_unavailable_evidence: str | None = None
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
    prompt_retry_serial: int = 0
    prompt_session_nonce: str = field(
        default_factory=lambda: uuid.uuid4().hex[:8]
    )
    progress_refresh_pending: bool = False
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
    takeover: bool = False
    takeover_phase: str | None = None
    takeover_target: str | None = None
    takeover_reason: str = ""
    takeover_steps: int = 0
    local_failure_streak: int = 0
    last_local_failure: str = ""
    extra_task_steps: int = 0
    extra_task_reports: int = 0
    extra_tasks_done: bool = False
    extra_task_notes: list[str] = field(default_factory=list)

    def dispatch(self, name: str, args: dict[str, Any], observation: Observation) -> ToolResult:
        """执行一个工具动作；前置条件与阶段约束由守卫层逐条判定。"""
        dismissed = self._dismiss_reward_if_needed(name, observation)
        if dismissed is not None:
            return dismissed
        # 不可逆的兑换确认即使处于接管模式也必须先核对商品身份。
        confirm_error = self._guard_confirm_exchange(name, args, observation)
        if confirm_error is not None:
            return confirm_error
        extra_safety_error = self._guard_extra_task_safety(
            name, args, observation
        )
        if extra_safety_error is not None:
            return extra_safety_error
        # 卡住恢复/接管期间必须先允许模型处理遮挡层、返回键或重新定位；
        # 如果先套用正常阶段白名单，恢复动作会被直接拦截。
        allowlist_error = self._guard_phase_allowlist(name)
        if allowlist_error is not None:
            return allowlist_error
        if self.takeover:
            return self._dispatch_freeform(name, args, observation)
        if self.recovering:
            return self._dispatch_freeform(name, args, observation)
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
        if name == "report_extra_tasks":
            if self.phase != "extra_tasks":
                return ToolResult(
                    False, "report_extra_tasks 只在新增任务巡查阶段使用"
                )
            return self._parse_extra_tasks_report(args, observation)
        if name == "select_local_image":
            set_context = getattr(self.executor, "set_task_context", None)
            if callable(set_context):
                set_context(self.target, self.expected_count)
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
            report_args = (
                args
                if self.config.force_all_daily_tasks
                else self._merge_local_report(args, observation)
            )
            return self._parse_report(
                report_args, observation
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
            # 输入动作必须幂等：页面可能已经保留上一轮成功填入的固定问题，
            # 但状态迁移在重绘/恢复时尚未写入 input。直接确认现有文本，避免
            # 在同一聊天页重复输入并耗尽整轮步骤。
            if self.phase == "perform" and self.target in TEXT_TASKS:
                find_input = getattr(self.executor, "_find_input", None)
                fixed_prompt = getattr(self.executor, "_fixed_prompt", None)
                if callable(find_input) and callable(fixed_prompt):
                    input_node = find_input(observation)
                    expected = str(fixed_prompt()).strip()
                    if input_node is not None and input_node.text.strip() == expected:
                        result = ToolResult(
                            True,
                            "当前输入框已是本轮固定测试文本，直接确认输入完成",
                            {"stage": observation.stage, "input_confirmed": True},
                        )
                        return self._after_successful_action(
                            name, args, result, observation
                        )
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
        if (
            result.data.get("stage") == "welfare"
            and name in {"return_to_welfare", "open_welfare"}
            and self.phase
            in {
                "open_target",
                "perform",
                "claim",
                "redeem",
                "prize_records",
                "use_reward",
                "use_confirm",
            }
        ):
            # 接管时模型可能直接放弃当前子流程回到福利中心。先重新读取
            # 页面计数，再决定是补做当前任务还是进入下一阶段，不能继续
            # 在旧阶段按旧子步骤失败。
            if self.phase in {"redeem", "prize_records", "use_reward", "use_confirm"}:
                self.executor.exchange_target_clicked = False
                self.executor.exchange_target_confirmed = False
            self.phase = "report"
            self.substate = {}
            self.progress_refresh_pending = True
            return result
        if (
            self.phase == "open_target"
            and self.target in IMAGE_TASKS
            and result.data.get("stage") in {"picker", "photo_preview"}
        ):
            # 系统选图/相机预览已经是图片任务目标页；忽略点击结果中可能残留的
            # 聊天 entry_evidence，直接进入本地选图步骤。
            self.phase = "perform"
            self.substate = {}
            return result
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
            if (
                self.target == "question"
                and name == "tap"
                and self._tap_hits_question_card(args, observation)
            ):
                # 新版趣问推荐问题卡点击后即提交一次提问；不能再把卡片内容
                # 覆盖成固定测试文本，否则福利页不会增加趣问计数。
                self.substate["question_card_clicked"] = True
                self.substate["sent"] = True
                return result
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
            elif self.daily_ability_done or self.daily_ability_unavailable:
                # 同日状态已确认能力站入口未由福利页提供时，重启后直接恢复
                # 固定任务报告，不重复执行已穷尽的入口检查。
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
            if (
                ("stage" in result.data and result.data.get("stage") != "welfare")
                or (
                    "activity" in result.data
                    and not is_welfare_activity(str(result.data.get("activity", "")))
                )
            ):
                return ToolResult(
                    False,
                    "返回动作未确认福利 WebView，保持返回阶段继续导航",
                    {"stage": result.data.get("stage", ""), "visual_replan": True},
                )
            # 返回福利页只证明导航成功，不能证明任务计数已经增加。
            # 元宝会出现消息已生成但福利页计数尚未回写、甚至提交失败不计数的情况；
            # 先回到 report 重新读取当前页面，只有页面实数确认后才推进任务。
            self.phase = "report"
            self.substate = {}
            self.progress_refresh_pending = True
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
        elif self.phase == "exchange_confirm" and name in {
            "press_back",
            "return_to_welfare",
            "open_exchange",
        }:
            # 确认页不是目标商品时由模型或本地守卫退回；回到商城后重新
            # 定位目标卡，回到福利页则重新打开商城。
            self.executor.exchange_target_clicked = False
            self.executor.exchange_target_confirmed = False
            stage = result.data.get("stage")
            if name == "open_exchange" and stage == "exchange":
                self.phase = "redeem"
            else:
                self.phase = "open_exchange" if stage == "welfare" else "redeem"
            self.substate = {}
        elif self.phase == "exchange_back" and name in {
            "press_back",
            "return_to_welfare",
            "open_exchange",
        }:
            self.executor.exchange_target_clicked = False
            self.executor.exchange_target_confirmed = False
            self.phase = (
                "open_exchange"
                if result.data.get("stage") == "welfare"
                else "redeem"
            )
            self.substate = {}
        elif self.phase == "prize_records" and name == "open_prize_records":
            self.phase = "use_reward"
        elif self.phase == "use_reward" and name == "use_bound_reward":
            self.phase = "use_confirm"
        elif self.phase == "use_confirm" and name == "confirm_reward_use":
            if self.executor.reward_use_confirmed:
                self.phase = "return_home"
        elif self.phase == "use_confirm" and name == "press_back":
            self.executor.reward_use_clicked = False
            self.phase = "use_reward"
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
        return self._run_executor(name, args, observation)

    def _run_executor(
        self, name: str, args: dict[str, Any], observation: Observation
    ) -> ToolResult:
        """把本轮已经截取的观测传给执行器，避免同一动作再 dump 一次页面。"""
        execute = self.executor.execute
        try:
            parameters = inspect.signature(execute).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "observation" in parameters:
            return execute(name, args, observation=observation)
        return execute(name, args)


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
