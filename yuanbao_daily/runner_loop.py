"""每日任务主循环：按阶段推进动作、执行工具并处理失败与恢复。

本模块只回答三件事：这一轮页面处于什么状态（阶段对账）、这一步该做什么
（动作选择）、做完之后如何记账（结果处理）。页面语义、工具契约和设备操作
分别由 workflow、tools、executor 承担，避免主循环变成大杂烩。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any

from .config import Config
from .constants import (
    DAILY_ABILITY_EXPERIENCE_TOOLS,
    DAILY_ABILITY_RULE_NAME,
    DAILY_ABILITY_TOOLS,
    LOCAL_DETERMINISTIC_TOOLS,
    PACKAGE_NAME,
    STABLE_MISSING_TASK_SCROLLS,
    VISUAL_EXCHANGE_TOOLS,
    VISUAL_NAVIGATION_TOOLS,
    VISUAL_REPLAN_MAX_ATTEMPTS,
)
from .errors import AgentError, ModelAccessError, RetryLimitExceeded
from .executor import ToolExecutor
from .models import Observation, ToolResult
from .ui import (
    action_signature,
    repeated_action_is_stuck,
    state_signature,
)
from .vision import VisionModel
from .welfare import (
    daily_ability_card_status,
    is_daily_ability_experience_visible,
)
from .workflow import Workflow


@dataclass
class Action:
    """本轮选定的动作及其来源。"""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    use_local_action: bool = False
    visual_navigation: bool = False
    context: str = ""


class TaskLoop:
    """每日任务主循环。"""

    def __init__(
        self,
        config: Config,
        executor: ToolExecutor,
        workflow: Workflow,
        model: VisionModel,
    ):
        self.config = config
        self.executor = executor
        self.workflow = workflow
        self.model = model
        self.failures = 0
        self.previous_state: str | None = None
        self.previous_action: str | None = None
        self.recent_action: dict[str, Any] | None = None
        self.visual_replan_attempts = 0
        self.visual_replan_phase: tuple[str, str | None] | None = None
        self.visual_replan_reason = ""
        self.stable_missing_scrolls = 0
        self.last_scroll_catalog_signature: tuple[tuple[Any, ...], ...] | None = None
        self.last_visual_action: str | None = None

    def run(self) -> bool:
        """执行每日任务主循环；返回 True 表示本轮已全部完成。"""
        for step in range(1, self.config.max_steps + 1):
            try:
                reconciled = self._reconcile_phase(step)
            except AgentError as exc:
                # WebView 重绘期间 uiautomator 可能暂时没生成 XML 文件；
                # 这是观测瞬态失败，不应直接终止整轮任务。
                self.failures += 1
                print(
                    f"步骤 {step}：页面观测失败（{str(exc)[:160]}），"
                    "将在下一步重新观测"
                )
                if self.failures >= self.config.max_retries:
                    raise RetryLimitExceeded(
                        f"页面观测连续失败 {self.failures} 次，达到最大重试次数",
                        phase=self.workflow.phase,
                        target=self.workflow.target or "",
                        step=step,
                        failures=self.failures,
                        recent_action=self.recent_action,
                    ) from exc
                if self.config.retry_cooldown_seconds:
                    time.sleep(self.config.retry_cooldown_seconds)
                continue
            if reconciled is None:
                continue
            observation, current_state, catalog_signature = reconciled
            try:
                action = self._select_action(observation, step)
            except _PhaseAdvanced:
                # 阶段在选择动作时已经切换（例如主卡本地确认完成），本轮结束。
                continue
            except ModelAccessError as exc:
                self.failures += 1
                raise RetryLimitExceeded(
                    f"视觉模型服务不可用：{str(exc)[:300]}",
                    phase=self.workflow.phase,
                    target=self.workflow.target or "",
                    step=step,
                    failures=self.failures,
                    recent_action=self.recent_action,
                    observation=observation,
                ) from exc
            except AgentError as exc:
                self.failures += 1
                print(f"步骤 {step}：模型动作解析失败（{str(exc)[:160]}）")
                # 模型调用失败同样先给一次 VLM 恢复机会：很可能是页面未到位或
                # 瞬态问题，恢复提示会让模型重新观察最新截图再决策。
                if self.failures >= self.config.max_retries and self.workflow.should_enter_recovery():
                    self.workflow.note_recovery_attempt()
                    print(
                        f"步骤 {step}：模型调用连续 {self.failures} 次失败，熔断前转 VLM 自由决策恢复一次 "
                        f"（阶段={self.workflow.phase}，目标={self.workflow.target or '无'}）"
                    )
                    if self.config.retry_cooldown_seconds:
                        time.sleep(self.config.retry_cooldown_seconds)
                    continue
                if self.failures >= self.config.max_retries:
                    raise RetryLimitExceeded(
                        f"视觉模型动作连续失败 {self.failures} 次，达到最大重试次数",
                        phase=self.workflow.phase,
                        target=self.workflow.target or "",
                        step=step,
                        failures=self.failures,
                        recent_action=self.recent_action,
                        observation=observation,
                    ) from exc
                if self.config.retry_cooldown_seconds:
                    time.sleep(self.config.retry_cooldown_seconds)
                continue
            if self._apply_result(
                step, observation, current_state, catalog_signature, action
            ):
                return True
        raise RetryLimitExceeded(
            f"达到单轮最大步骤数 {self.config.max_steps}，未完成每日任务",
            phase=self.workflow.phase,
            target=self.workflow.target or "",
            step=self.config.max_steps,
            failures=self.failures,
            recent_action=self.recent_action,
            observation=observation,
        )

    def _reconcile_phase(
        self, step: int
    ) -> tuple[Observation, str, tuple[tuple[Any, ...], ...]] | None:
        """观测页面并做阶段级对账；返回 None 表示本步已处理完。"""
        workflow = self.workflow
        observation = self.executor.observe()
        if workflow.phase == "daily_ability":
            local_card_status = daily_ability_card_status(observation)
            if local_card_status == "已完成":
                workflow.daily_ability_done = True
                workflow.daily_ability_evidence = DAILY_ABILITY_RULE_NAME
                workflow._persist_task_state()
                workflow.phase = "report"
                print("本地已确认主轮播卡固定区域显示‘已完成’，跳过重复体验")
                return None
        if workflow.phase == "daily_ability_experience":
            if getattr(self.executor, "pending_generation", False):
                workflow.daily_ability_experience_opened = True
            elif is_daily_ability_experience_visible(observation):
                workflow.daily_ability_experience_opened = True
            elif workflow.daily_ability_experience_opened:
                workflow.phase = "verify_daily_ability"
                print("体验操作已返回福利中心，进入主轮播卡完成状态核验")
        if workflow.phase == "report":
            prepare_report = getattr(
                self.executor, "prepare_welfare_task_report", None
            )
            if callable(prepare_report):
                observation = prepare_report(observation)
        if workflow.phase in {"daily_ability", "verify_daily_ability"}:
            prepare_card = getattr(
                self.executor, "prepare_daily_ability_card", None
            )
            if callable(prepare_card):
                observation = prepare_card(observation)
        current_state = state_signature(
            observation.ui, observation.image, observation.activity
        )
        catalog_signature = workflow.task_catalog_signature(observation)
        phase_key = (workflow.phase, workflow.target)
        if phase_key != self.visual_replan_phase:
            self.visual_replan_attempts = 0
            self.visual_replan_reason = ""
            self.stable_missing_scrolls = 0
            self.last_scroll_catalog_signature = None
            self.last_visual_action = None
            self.visual_replan_phase = phase_key
        elif self.last_visual_action == "swipe":
            if workflow.can_conclude_target_unavailable(observation):
                # WebView 滚动后常有一两次空无障碍树；只要最新完整目录仍
                # 明确没有目标，就累计稳定缺失次数，不让瞬态空树重新开始。
                self.stable_missing_scrolls += 1
                if catalog_signature:
                    self.last_scroll_catalog_signature = catalog_signature
            elif observation.nodes:
                # 目录已经完整暴露但目标重新出现或页面语义改变，重新观察。
                self.stable_missing_scrolls = 0
        if (
            self.stable_missing_scrolls >= STABLE_MISSING_TASK_SCROLLS
            and workflow.can_conclude_target_unavailable(observation)
        ):
            workflow.skip_unavailable_target(observation)
            self._reset_progress(current_state)
            return None
        if workflow.reconcile_return_to_welfare(observation):
            self._reset_progress(current_state)
            return None
        if workflow.reconcile_visible_progress(observation):
            self._reset_progress(current_state)
            print(f"步骤 {step}：已根据福利页面的可见进度恢复当前任务")
            return None
        return observation, current_state, catalog_signature

    def _reset_progress(self, current_state: str) -> None:
        """对账成功后清空失败与重规划计数。"""
        self.failures = 0
        self.visual_replan_attempts = 0
        self.visual_replan_reason = ""
        self.stable_missing_scrolls = 0
        self.last_scroll_catalog_signature = None
        self.last_visual_action = None
        self.previous_state, self.previous_action = current_state, None

    def _select_action(self, observation: Observation, step: int) -> Action:
        """决定本轮要执行的动作。"""
        workflow = self.workflow
        # 本地状态已明确的阶段/子步骤强制唯一工具；入口点击等仍由模型定位。
        # 卡住恢复期间不强制工具，让 VLM 看最新截图自由决策。
        forced_tool = workflow.expected_tool(observation)
        context = workflow.context(observation)
        if workflow.recovering:
            context = f"{context}\n{workflow.recovery_hint()}"
            print(
                f"步骤 {step}：进入卡住恢复，VLM 自由决策 "
                f"（阶段={workflow.phase}，目标={workflow.target or '无'}）"
            )
        visual_navigation = workflow.requires_visual_navigation()
        # 本地语义已经能定位入口时不再请求视觉模型：模型给出的坐标
        # 随后也会被同一套语义校正改写，等于白花一次请求。
        local_action = (
            None if workflow.recovering else workflow.local_entry_action(observation)
        )
        use_local_action = local_action is not None and (
            not visual_navigation or self.visual_replan_attempts == 0
        )
        if use_local_action:
            name, args = local_action
            return Action(name, args, None, True, visual_navigation, context)
        if visual_navigation:
            return self._select_visual_action(
                observation, context, visual_navigation
            )
        if workflow.phase == "daily_ability" and not workflow.recovering:
            return self._select_ability_action(observation, context)
        if (
            workflow.phase == "daily_ability_experience"
            and not workflow.recovering
            and forced_tool is None
        ):
            return self._select_experience_action(observation, context)
        if forced_tool in LOCAL_DETERMINISTIC_TOOLS:
            # 输入、发送、等待、返回和兑换都有可验证的本地前置/后置条件；
            # 直接执行可以显著减少重复视觉请求，同时把结果写入同轮历史。
            return Action(forced_tool, {}, None, False, visual_navigation, context)
        if forced_tool == "report_tasks":
            # 进度计数在无障碍树里是明确文本，本地能读到时直接采用；
            # 只有读不到（空树或字段缺失）才让 VLMM 从最新截图补充，
            # 避免把可确定的读数交给概率性模型。
            local_report = workflow.local_task_report(observation)
            if local_report:
                print("任务进度由无障碍树直接读出，跳过视觉报告")
                return Action(
                    forced_tool, local_report, None, False, visual_navigation, context
                )
            name, args, call_id = self.model.next_tool(
                observation,
                f"{context}\n本阶段唯一允许调用 {forced_tool}；不得返回滑动、点击或其它工具。",
                forced_tool=forced_tool,
                allowed_tools=frozenset({forced_tool}),
            )
            return Action(name, args, call_id, False, visual_navigation, context)
        if forced_tool == "verify_daily_ability":
            # 主卡状态是固定位置的明确文案，本地能读出时直接确认；
            # 读不到才让 VLMM 从截图补充。
            local_card_status = daily_ability_card_status(observation)
            if local_card_status in {"已完成", "去体验"}:
                print(f"主轮播卡状态本地读出：{local_card_status}")
                return Action(
                    forced_tool,
                    {"card_status": local_card_status},
                    None,
                    False,
                    visual_navigation,
                    context,
                )
            forced_context = context + (
                f"\n本阶段唯一允许调用 {forced_tool}；"
                "主卡固定位置若读不到，请按 schema 报告‘未显示’，不得改用其它工具。"
            )
            name, args, call_id = self.model.next_tool(
                observation,
                forced_context,
                forced_tool=forced_tool,
                allowed_tools=frozenset({forced_tool}),
            )
            return Action(name, args, call_id, False, visual_navigation, context)
        forced_context = context
        if forced_tool:
            forced_context += (
                f"\n本阶段唯一允许调用 {forced_tool}；若目标卡或完成状态未显示，"
                "仍须使用该工具按 schema 报告未显示，不得改用其它工具。"
            )
        name, args, call_id = self.model.next_tool(
            observation,
            forced_context,
            forced_tool=forced_tool,
            allowed_tools=(frozenset({forced_tool}) if forced_tool else None),
        )
        return Action(name, args, call_id, False, visual_navigation, context)

    def _select_visual_action(
        self, observation: Observation, context: str, visual_navigation: bool
    ) -> Action:
        """视觉导航阶段：入口、模板按钮和返回路径由模型按最新截图决定。"""
        workflow = self.workflow
        visual_context = (
            f"{context}\n本轮是视觉导航；只能从 tap、swipe、press_back 中选择，"
            "禁止再次调用 open_welfare 或使用历史坐标。"
        )
        if self.visual_replan_attempts:
            visual_context += (
                "上一视觉动作没有达到目标，失败原因是："
                f"{self.visual_replan_reason or '页面状态未按预期变化'}。"
                "请只依据这次最新截图重新规划，禁止重复相同动作或相同位置；"
                "目标不可见时先滑动，浮层遮挡时先关闭，点击后必须等待并确认页面阶段变化。"
            )
        if workflow.phase == "open_target" and workflow.target == "same_template":
            visual_context += (
                "\n当前目标是福利中心的“做同款”任务：该任务行下方的一张推荐模板卡片"
                "就是入口，标题行右侧没有可点按钮。请点击任意一张模板卡片；"
                "工具返回的失败信息里会列出当前可点击的卡片及其坐标，"
                "卡片不在视口内时先滑动。"
            )
        if workflow.phase == "return":
            visual_context += (
                "当前目标是返回福利中心；优先按截图点击左上角抽屉入口，再点侧栏“福利中心”；"
                "只有没有抽屉入口时才尝试顶部返回链接或旧版“我们”入口。"
                "只有真正进入福利页后才算成功。"
            )
        allowed_visual_tools = VISUAL_NAVIGATION_TOOLS
        if workflow.phase == "open_exchange" and workflow.substate.get("entry_clicked"):
            allowed_visual_tools = VISUAL_EXCHANGE_TOOLS
        semantic_recovery_needed = bool(self.visual_replan_attempts)
        if workflow.phase == "navigate_welfare":
            semantic_recovery_needed = semantic_recovery_needed or (
                observation.stage == "ours"
                or (
                    bool(observation.activity)
                    and PACKAGE_NAME not in observation.activity.lower()
                )
            )
        semantic_action = (
            workflow.semantic_visual_recovery_action(observation)
            if semantic_recovery_needed
            else None
        )
        if semantic_action is not None:
            # 只使用当前无障碍树刚刚暴露的唯一语义控件；它不是历史
            # 坐标缓存，页面改版后仍会由 VLMM 或新语义重新决定。
            name, args = semantic_action
            return Action(name, args, None, False, visual_navigation, context)
        name, args, call_id = self.model.next_tool(
            observation,
            visual_context,
            forced_tool=None,
            allowed_tools=allowed_visual_tools,
        )
        return Action(name, args, call_id, False, visual_navigation, context)

    def _select_ability_action(self, observation: Observation, context: str) -> Action:
        """能力站阶段：主卡状态能本地读出时直接点击，否则问模型。"""
        workflow = self.workflow
        local_card_status = daily_ability_card_status(observation)
        if local_card_status == "已完成":
            workflow.daily_ability_done = True
            workflow.daily_ability_evidence = DAILY_ABILITY_RULE_NAME
            workflow._persist_task_state()
            workflow.phase = "report"
            print("主轮播卡固定区域显示‘已完成’，本地直接确认完成")
            # 阶段已经切换，本轮不再执行动作；由下一轮重新观测。
            raise _PhaseAdvanced()
        if local_card_status is None:
            ability_context = (
                f"{context}\n主卡固定位置的状态在无障碍树里读不到；"
                "请只依据最新截图判断：显示‘去体验’时传 button_visible=true，"
                "已显示‘已完成’时传 button_visible=false 并调用 continue_daily_ability "
                "把当前页面交给模型处理。不要识别标题/颜色，也不要滑动换卡。"
            )
            name, args, call_id = self.model.next_tool(
                observation,
                ability_context,
                allowed_tools=DAILY_ABILITY_TOOLS,
            )
            return Action(name, args, call_id)
        print(f"主轮播卡本地读出状态：{local_card_status}，直接点击")
        return Action("open_daily_ability", {"button_visible": True}, None)

    def _select_experience_action(
        self, observation: Observation, context: str
    ) -> Action:
        """能力体验阶段：语音控件由本地语义定位，其余交给模型。"""
        workflow = self.workflow
        experience_context = (
            f"{context}\n你接手了主轮播卡当前打开的体验页面。按页面实际要求完成互动，"
            "只使用当前截图中可见的控件。"
            "如果页面要求语音输入（出现‘按住说话’一类控件），调用 hold_to_speak 并设置时长，"
            "程序会按住该控件并注入短测试语音，随后你再调用 wait_5s 等待结果；"
            "不要用 long_press 去猜语音控件的坐标。"
            "体验可能只是同一福利 WebView 中的浮层（例如出现‘去对话’按钮），此时仍要按截图继续操作，"
            "不能因为阶段名称是 welfare 就直接核验或返回。完成后再返回福利中心，不要打开其它福利卡片。"
        )
        voice_action = workflow.daily_ability_voice_action(observation)
        if voice_action is not None:
            name, args = voice_action
            print("能力体验检测到‘按住说话’，执行本地必做语音长按")
            return Action(name, args, None)
        name, args, call_id = self.model.next_tool(
            observation,
            experience_context,
            allowed_tools=DAILY_ABILITY_EXPERIENCE_TOOLS,
        )
        return Action(name, args, call_id)

    def _apply_result(
        self,
        step: int,
        observation: Observation,
        current_state: str,
        catalog_signature: tuple[tuple[Any, ...], ...],
        action: Action,
    ) -> bool:
        """执行本轮动作并记账；返回 True 表示整轮已完成。"""
        workflow = self.workflow
        name, args, call_id = action.name, action.args, action.call_id
        self.recent_action = {"tool": name, "arguments": args}
        signature = action_signature(name, args)
        # 卡住恢复动作不受“同画面重复”拦截：恢复本就是在同一卡住画面上
        # 换手段再试一次（换坐标重 tap / swipe / press_back），拦截它就等于
        # 堵死恢复。恢复只允许一次，后续仍受正常计数与熔断保护。
        if workflow.recovering:
            result = workflow.dispatch(name, args, observation)
            self.failures = self.failures + 1 if not result.ok else 0
            self.previous_state, self.previous_action = current_state, signature
        elif repeated_action_is_stuck(
            name,
            signature,
            current_state,
            self.previous_action,
            self.previous_state,
        ):
            self.failures += 1
            result = ToolResult(False, "页面状态未变化且重复同一动作，已拒绝循环点击")
        else:
            result = workflow.dispatch(name, args, observation)
            self.failures = self.failures + 1 if not result.ok else 0
        if action.use_local_action and not result.ok:
            # 本地入口失败后允许下一轮换用视觉模型重新定位；否则重复保护
            # 会把这次失败误判成同一动作的循环点击。
            self.previous_state, self.previous_action = None, None
            print("本地语义入口未生效，下一轮改由视觉模型重新定位")
        else:
            self.previous_state, self.previous_action = current_state, signature
        if call_id is None:
            self.model.record_local_tool_result(
                name, args, observation, action.context, result
            )
        else:
            self.model.record_tool_result(call_id, result)
        safe_args = (
            json.dumps(args, ensure_ascii=False, sort_keys=True)
            if name in {"report_tasks", "tap", "swipe"}
            else ""
        )
        source = (
            "本地语义"
            if action.use_local_action
            else ("本地" if call_id is None else "视觉模型")
        )
        print(
            f"步骤 {step}：工具={name} 来源={source} {safe_args} "
            f"结果={'成功' if result.ok else '失败'}；{result.message[:180]}"
        )
        if action.visual_navigation and result.ok:
            self.visual_replan_attempts = 0
            self.visual_replan_reason = ""
            if name == "swipe" and workflow.phase == "open_target":
                self.last_scroll_catalog_signature = catalog_signature
                self.last_visual_action = "swipe"
            else:
                self.stable_missing_scrolls = 0
                self.last_scroll_catalog_signature = None
                self.last_visual_action = None
        if workflow.finished:
            return True
        # 视觉导航失败是感知/规划问题，不应和 ADB、输入或兑换错误共用
        # MAX_RETRIES。把失败原因带回下一轮最新截图，让 VLMM 自己换策略。
        if (
            action.visual_navigation
            and name in VISUAL_NAVIGATION_TOOLS
            and not result.ok
        ):
            self.visual_replan_attempts += 1
            self.visual_replan_reason = result.message[:240]
            self.failures = 0
            self.previous_state, self.previous_action = None, None
            self.last_visual_action = None
            self.last_scroll_catalog_signature = None
            if self.visual_replan_attempts >= VISUAL_REPLAN_MAX_ATTEMPTS:
                raise RetryLimitExceeded(
                    f"视觉导航连续重规划 {self.visual_replan_attempts} 次仍未达到目标",
                    phase=workflow.phase,
                    target=workflow.target or "",
                    step=step,
                    failures=self.visual_replan_attempts,
                    recent_action={
                        **(self.recent_action or {}),
                        "result": result.message[:500],
                    },
                    observation=observation,
                )
            print(
                f"步骤 {step}：视觉导航未达目标，交回 VLMM 第 {self.visual_replan_attempts}/"
                f"{VISUAL_REPLAN_MAX_ATTEMPTS} 次重规划"
            )
            if self.config.retry_cooldown_seconds:
                time.sleep(self.config.retry_cooldown_seconds)
            return False
        if not result.ok:
            # 熔断前先给 VLM 一次自由决策恢复机会：意外弹窗、页面未到位等
            # 未被程序覆盖的情况，让模型看最新截图自己决定（关弹窗/滑动/
            # 重 tap/返回等），而不是连续失败几次就直接死。同一卡点只恢复
            # 一次；恢复动作本身的成败仍计入 failures，失败则正常熔断。
            if self.failures >= self.config.max_retries and workflow.should_enter_recovery():
                workflow.note_recovery_attempt()
                print(
                    f"步骤 {step}：连续 {self.failures} 次失败，熔断前转 VLM 自由决策恢复一次 "
                    f"（阶段={workflow.phase}，目标={workflow.target or '无'}）"
                )
                if self.config.retry_cooldown_seconds:
                    time.sleep(self.config.retry_cooldown_seconds)
                return False
            if self.failures >= self.config.max_retries:
                raise RetryLimitExceeded(
                    f"连续 {self.failures} 次工具/状态失败，达到最大重试次数",
                    phase=workflow.phase,
                    target=workflow.target or "",
                    step=step,
                    failures=self.failures,
                    recent_action={
                        **(self.recent_action or {}),
                        "result": result.message[:500],
                    },
                    observation=observation,
                )
            if self.config.retry_cooldown_seconds:
                time.sleep(self.config.retry_cooldown_seconds)
        return False


class _PhaseAdvanced(Exception):
    """阶段在选择动作时已经切换，本轮不再执行动作。"""


def run_task_loop(
    config: Config,
    executor: ToolExecutor,
    workflow: Workflow,
    model: VisionModel,
) -> bool:
    """执行每日任务主循环；返回 True 表示本轮已全部完成。"""
    return TaskLoop(config, executor, workflow, model).run()
