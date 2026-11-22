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
    EXTRA_TASK_STEP_LIMIT,
    LOCAL_DETERMINISTIC_TOOLS,
    PACKAGE_NAME,
    PROGRESS_REFRESH_POLL_INTERVAL,
    PROGRESS_REFRESH_TIMEOUT,
    STABLE_MISSING_TASK_SCROLLS,
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
    is_daily_ability_experience_activity,
    is_daily_ability_experience_visible,
    is_welfare_activity,
    parse_local_welfare_progress,
)
from .workflow import Workflow
from .workflow_policy import stage_mismatch


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
        # 最近动作及结果会带给接管中的 VLM；本地动作的结果默认不回放，
        # 但页面变化时这些失败原因正是模型换策略所需的信息。
        self.recent_attempts: list[dict[str, Any]] = []

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
            except RetryLimitExceeded:
                raise
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
            if is_daily_ability_experience_activity(observation):
                # 奖励遮罩、WebView 异步路由或上一轮遗留的点击可能让主卡
                # 动作没有经过 _advance_daily_ability_opening，但当前 Activity
                # 已经是体验页。此时不能再次回福利页重开，应直接交给 VLM。
                workflow.daily_ability_experience_opened = True
                workflow.phase = "daily_ability_experience"
                print("能力站阶段已进入元宝体验 Activity，切换到体验处理")
        if workflow.phase == "daily_ability_experience":
            if getattr(self.executor, "pending_generation", False):
                workflow.daily_ability_experience_opened = True
            elif is_daily_ability_experience_visible(observation):
                workflow.daily_ability_experience_opened = True
            elif workflow.daily_ability_experience_opened:
                workflow.phase = "verify_daily_ability"
                print("体验操作已返回福利中心，进入主轮播卡完成状态核验")
        if workflow.phase == "report":
            if not is_welfare_activity(observation.activity):
                # 报告阶段只能读取福利 WebView。聊天首页可能残留上一页的
                # WebView/无障碍树，直接报告会反复使用旧 0/3 计数并耗尽步数。
                workflow.phase = "return"
                workflow.substate = {}
                workflow.progress_refresh_pending = False
                print("报告阶段当前不是福利 WebView，先回到返回导航阶段")
                return None
            if getattr(workflow, "progress_refresh_pending", False) is True:
                # 任务结果由服务端异步回写福利页；轮询目标计数通常比固定
                # sleep 更快，worst case 仍保留完整的稳定窗口。
                observation = self._await_progress_refresh(observation)
                workflow.progress_refresh_pending = False
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

    def _await_progress_refresh(self, observation: Observation) -> Observation:
        """等待福利页回写当前任务计数；计数更新后立即返回。"""
        target = self.workflow.target
        previous = self.workflow.progress.get(target) if target else None
        time.sleep(1.0)
        observation = self.executor.observe()
        if target is None:
            return observation
        deadline = time.monotonic() + PROGRESS_REFRESH_TIMEOUT
        while time.monotonic() < deadline:
            report = parse_local_welfare_progress(observation)
            current = report.get(target) if isinstance(report, dict) else None
            if isinstance(current, int) and (
                previous is None or current > previous
            ):
                return observation
            time.sleep(PROGRESS_REFRESH_POLL_INTERVAL)
            observation = self.executor.observe()
        print(
            f"福利页未在 {PROGRESS_REFRESH_TIMEOUT:.0f} 秒内回写 "
            f"{target} 计数，按当前页面继续对账"
        )
        return observation

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
        """决定本轮动作：本地可验证时走快路径，否则交给 VLM。"""
        workflow = self.workflow
        workflow.sync_takeover()

        drift = stage_mismatch(workflow.phase, workflow.target, observation)
        recovering = getattr(workflow, "recovering", False) is True
        takeover = getattr(workflow, "takeover", False) is True
        if drift and not recovering and not takeover:
            workflow.enter_takeover(drift)
            takeover = True
        if takeover:
            # 页面已经回到预期且本地步骤重新适用时，尽快退出接管以省请求。
            expected = workflow.expected_tool(observation)
            local_ready = (
                expected is None
                or expected not in LOCAL_DETERMINISTIC_TOOLS
                or workflow.deterministic_step_applicable(expected, observation)
            )
            if not drift and local_ready:
                workflow.exit_takeover("页面已恢复且本地步骤适用")
                takeover = False
                recovering = getattr(workflow, "recovering", False) is True

        if takeover:
            return self._select_takeover_action(observation, step)

        forced_tool = workflow.expected_tool(observation)
        context = workflow.context(observation)
        if recovering:
            context = f"{context}\n{workflow.recovery_hint()}"
            print(
                f"步骤 {step}：进入卡住恢复，VLM 自由决策 "
                f"（阶段={workflow.phase}，目标={workflow.target or '无'}）"
            )
        visual_navigation = workflow.requires_visual_navigation()
        # 本地语义已经能定位入口时不再请求视觉模型；本地入口失败后
        # visual_replan_attempts 会阻止下一轮继续复用同一个坏定位。
        local_action = (
            None if recovering else workflow.local_entry_action(observation)
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
        if workflow.phase == "daily_ability" and not recovering:
            return self._select_ability_action(observation, context)
        if (
            workflow.phase == "daily_ability_experience"
            and not recovering
            and forced_tool is None
        ):
            return self._select_experience_action(observation, context)
        if workflow.phase == "extra_tasks":
            return self._select_extra_task_action(observation, context)
        if forced_tool in LOCAL_DETERMINISTIC_TOOLS:
            # 本地配方只在页面仍符合预期时执行；一旦页面结构变化，
            # 立即交给 VLM，而不是反复跑同一个死脚本。
            if workflow.deterministic_step_applicable(forced_tool, observation):
                return Action(forced_tool, {}, None, False, visual_navigation, context)
            workflow.enter_takeover(
                f"本地步骤 {forced_tool} 不适用于当前页面（阶段={observation.stage}）"
            )
            return self._select_takeover_action(observation, step)
        if workflow.phase == "exchange_confirm":
            return self._select_exchange_confirm_action(observation, context)
        if forced_tool == "report_tasks":
            # 进度计数在无障碍树里是明确文本，本地能读到时直接采用；
            # 读不到才让视觉模型补充。模型此时也可以先滚动，而不是被
            # 锁死在 report_tasks 上等待不完整目录。
            local_report = workflow.local_task_report(observation)
            if local_report:
                print("任务进度由无障碍树直接读出，跳过视觉报告")
                return Action(
                    forced_tool, local_report, None, False, visual_navigation, context
                )
            return self._select_model_action(
                observation,
                context
                + "\n优先调用 report_tasks；只有当前截图已滚动到完整任务区时才报告。"
                "如果任务目录不完整，先 swipe/滚动；页面不对时可用导航工具先恢复，"
                "不要报告猜测的数字。",
                context,
            )
        if forced_tool == "verify_daily_ability":
            # 主卡状态是固定位置的明确文案，本地能读出时直接确认；
            # 读不到才让视觉模型补充。
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
            return self._select_model_action(
                observation,
                context
                + "\n优先调用 verify_daily_ability；主卡状态读不到时按 schema 报告‘未显示’。"
                "如果主卡不在视口，先滑动或返回福利页顶部，不要猜测状态。",
                context,
            )
        forced_context = context
        if forced_tool:
            forced_context += (
                f"\n优先调用 {forced_tool}；如果当前页面与阶段不符，先用导航工具"
                "恢复页面，再按最新截图继续。"
            )
        return self._select_model_action(observation, forced_context, context)

    def _select_model_action(
        self, observation: Observation, prompt_context: str, action_context: str
    ) -> Action:
        """让视觉模型在当前阶段的合理工具集内自主选择下一步。"""
        name, args, call_id = self.model.next_tool(
            observation,
            prompt_context,
            forced_tool=None,
            allowed_tools=self.workflow.preferred_model_tools(),
        )
        return Action(name, args, call_id, False, False, action_context)

    def _select_takeover_action(self, observation: Observation, step: int) -> Action:
        """接管模式：模型按最新截图连续多步自由决策。"""
        workflow = self.workflow
        if not workflow.reserve_takeover_step():
            raise RetryLimitExceeded(
                f"VLM 接管 {workflow.takeover_steps} 步后仍未恢复可执行流程",
                phase=workflow.phase,
                target=workflow.target or "",
                step=step,
                failures=self.failures,
                recent_action=self.recent_action,
                observation=observation,
            )
        print(
            f"步骤 {step}：VLM 接管决策 "
            f"（阶段={workflow.phase}，目标={workflow.target or '无'}，"
            f"第 {workflow.takeover_steps} 步）"
        )
        context = (
            f"{workflow.context(observation)}\n{workflow.takeover_hint()}\n"
            f"{self._recent_attempts_text()}"
        )
        name, args, call_id = self.model.next_tool(
            observation,
            context,
            forced_tool=None,
            allowed_tools=workflow.takeover_allowed_tools(),
        )
        return Action(name, args, call_id, False, False, context)

    def _select_exchange_confirm_action(
        self, observation: Observation, context: str
    ) -> Action:
        """兑换确认必须由模型核对商品名；不符时只允许返回。"""
        confirm_context = (
            f"{context}\n当前在兑换确认阶段。只有最新截图明确显示“QQ超级会员3天卡”时才能调用 "
            "confirm_exchange，并在 product_name 中逐字填写页面显示的商品名称，"
            "target_visible 必须为 true。若看到 QQ音乐/绿钻/黄钻或任何其它商品，"
            "或无法确认商品名称，必须调用 press_back 返回商城；"
            "绝对不要点击通用确认按钮，也不要尝试其他商品。"
        )
        name, args, call_id = self.model.next_tool(
            observation,
            confirm_context,
            forced_tool=None,
            allowed_tools=self.workflow.preferred_model_tools(),
        )
        return Action(name, args, call_id, False, False, context)

    def _recent_attempts_text(self) -> str:
        if not self.recent_attempts:
            return ""
        lines = ["最近动作与结果（本地动作失败原因供换策略参考）："]
        for item in self.recent_attempts[-6:]:
            outcome = "成功" if item["ok"] else "失败"
            lines.append(
                f"- {item['tool']} {outcome}：{str(item['message'])[:160]}"
            )
        return "\n".join(lines)

    def _select_visual_action(
        self, observation: Observation, context: str, visual_navigation: bool
    ) -> Action:
        """视觉导航阶段：入口、模板按钮和返回路径由模型按最新截图决定。"""
        workflow = self.workflow
        visual_context = (
            f"{context}\n本轮是视觉导航；优先依据最新截图选择 tap、swipe 或 press_back，"
            "也可以调用 open_welfare/return_to_welfare 走高层导航。禁止复用历史坐标。"
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
                "\n当前目标是福利中心的“做同款”任务：如果当前截图可见每日任务行右侧的“做同款”"
                "按钮，优先点击该按钮；如果任务行没有可用按钮，再点击任务行下方任意一张"
                "推荐模板卡片。卡片不在视口内时先滑动，工具返回的失败信息会列出当前可见卡片。"
            )
        if workflow.phase == "return":
            visual_context += (
                "当前目标是返回福利中心；优先按截图点击左上角抽屉入口，再点侧栏“福利中心”；"
                "只有没有抽屉入口时才尝试顶部返回链接或旧版“我们”入口。"
                "只有真正进入福利页后才算成功。"
            )
        allowed_visual_tools = workflow.preferred_model_tools()
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
        screenshot_completed = getattr(
            workflow.executor, "_top_daily_ability_completed_from_screenshot", None
        )
        if local_card_status is None and callable(screenshot_completed):
            if screenshot_completed(observation):
                workflow.daily_ability_done = True
                workflow.daily_ability_evidence = DAILY_ABILITY_RULE_NAME
                workflow._persist_task_state()
                workflow.phase = "report"
                print("截图确认主卡显示‘今日已完成’，本地直接确认能力站完成")
                raise _PhaseAdvanced()
        if local_card_status is None:
            ability_context = (
                f"{context}\n主卡固定位置的状态在无障碍树里读不到；"
                "请只依据最新截图判断：福利页顶部第一张主轮播卡（例如‘AI精讲’、‘AI录音笔’）"
                "右侧出现‘去提问’或‘去体验’时，必须传 button_visible=true、top_card_visible=true；"
                "如果主卡标题是‘AI录音笔/录音笔/语音’，再传 voice_card_visible=true；"
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
        visible = [
            node
            for node in observation.nodes
            if node.enabled and node.bounds.area > 0
        ]
        voice_card_visible = any(
            any(marker in node.searchable for marker in ("ai录音笔", "录音笔", "语音"))
            and node.bounds.center[1] <= self.executor.height * 0.30
            for node in visible
        )
        top_card_visible = voice_card_visible or any(
            node.text.strip() in {"去提问", "去体验", "已完成"}
            and node.bounds.center[0] >= self.executor.width * 0.65
            and node.bounds.center[1] <= self.executor.height * 0.45
            for node in visible
        )
        click_args: dict[str, Any] = {"button_visible": True}
        if top_card_visible:
            click_args["top_card_visible"] = True
        if voice_card_visible:
            click_args["voice_card_visible"] = True
        return Action("open_daily_ability", click_args, None)

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

    def _select_extra_task_action(
        self, observation: Observation, context: str
    ) -> Action:
        """新增任务巡查：模型可按最新截图连续尝试应用内任务。"""
        workflow = self.workflow
        workflow.extra_task_steps += 1
        if workflow.extra_task_steps > EXTRA_TASK_STEP_LIMIT:
            print(
                f"新增任务巡查达到 {EXTRA_TASK_STEP_LIMIT} 步预算，"
                "停止巡查并进入兑换阶段"
            )
            workflow.extra_tasks_done = True
            workflow.phase = "open_exchange"
            workflow.substate = {}
            raise _PhaseAdvanced()
        notes = "\n".join(workflow.extra_task_notes) or "尚无巡查记录"
        extra_context = (
            f"{context}\n{notes}\n"
            "固定每日任务已完成，现在巡查福利页中的新增任务。\n"
            "允许：在元宝应用内完成、无需外部账号/应用、不会对外产生影响的任务"
            "（浏览、观看、试用、签到、互动、问答等）。\n"
            "禁止并跳过：邀请好友、助力、拉新、组队；分享给他人；打开微信/QQ/浏览器"
            "或其它外部 App；下载安装；绑定、授权、支付、充值、实名等外部影响操作。\n"
            "如果当前不在福利页，先返回福利中心；看到可做任务入口就点击它并按页面完成，"
            "一次只做一个动作。完成一个任务后回到福利中心继续找下一个。\n"
            "没有更多可做任务时调用 report_extra_tasks 并设置 done=true；"
            "如果还要继续做，done=false 并在 remaining_tasks 列出剩余任务。"
            "excluded_tasks 填因邀请/外部影响而跳过的任务。"
        )
        name, args, call_id = self.model.next_tool(
            observation,
            extra_context,
            forced_tool=None,
            allowed_tools=workflow.preferred_model_tools(),
        )
        return Action(name, args, call_id, False, False, extra_context)

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
        # 恢复/接管动作本来就要在同一卡住画面上换手段重试，不能被
        # “同画面重复动作”拦截。
        if (getattr(workflow, "recovering", False) is True
            or getattr(workflow, "takeover", False) is True):
            result = workflow.dispatch(name, args, observation)
        elif repeated_action_is_stuck(
            name,
            signature,
            current_state,
            self.previous_action,
            self.previous_state,
        ):
            result = ToolResult(False, "页面状态未变化且重复同一动作，已拒绝循环点击")
        else:
            result = workflow.dispatch(name, args, observation)

        self.recent_attempts.append(
            {
                "tool": name,
                "ok": result.ok,
                "message": result.message,
                "stage": observation.stage,
            }
        )
        self.recent_attempts = self.recent_attempts[-8:]
        if result.ok:
            self.failures = 0
            workflow.reset_local_failure()
        else:
            self.failures += 1

        local_navigation_failure = False
        if action.use_local_action and not result.ok:
            # 本地入口/本地配方失败后，下一轮必须换手段，不能复用同一个
            # 已经证明无效的定位。
            self.previous_state, self.previous_action = None, None
            if action.visual_navigation:
                local_navigation_failure = True
                self.visual_replan_attempts += 1
                self.visual_replan_reason = result.message[:240]
                self.failures = 0
                print(
                    "本地语义入口未生效，下一轮改由视觉模型重新定位"
                    f"（{self.visual_replan_attempts}/{VISUAL_REPLAN_MAX_ATTEMPTS}）"
                )
                if self.visual_replan_attempts >= VISUAL_REPLAN_MAX_ATTEMPTS:
                    workflow.enter_takeover(
                        "本地语义入口连续失效："
                        f"{result.message[:200]}"
                    )
                    self.visual_replan_attempts = 0
            else:
                print("本地步骤未生效，下一轮交给视觉模型重新观察")
                workflow.note_local_failure(result.message)
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
        workflow.sync_takeover()
        if not result.ok:
            if (getattr(workflow, "takeover", False) is True
                or getattr(workflow, "recovering", False) is True):
                # 自由决策期间不按普通重试立即熔断；接管预算/一次性恢复
                # 自己控制失败边界。
                if self.config.retry_cooldown_seconds:
                    time.sleep(self.config.retry_cooldown_seconds)
                return False
            if (
                action.visual_navigation
                and name in VISUAL_NAVIGATION_TOOLS
                and not local_navigation_failure
            ):
                self.visual_replan_attempts += 1
                self.visual_replan_reason = result.message[:240]
                self.failures = 0
                self.previous_state, self.previous_action = None, None
                self.last_visual_action = None
                self.last_scroll_catalog_signature = None
                if self.visual_replan_attempts >= VISUAL_REPLAN_MAX_ATTEMPTS:
                    # 不再直接死亡：把连续多步控制权交给模型，允许它重新
                    # 找到入口或按新页面完成当前子任务。
                    workflow.enter_takeover(
                        "视觉导航连续未达目标："
                        f"{result.message[:200]}"
                    )
                    self.visual_replan_attempts = 0
                    self.failures = 0
                    return False
                print(
                    f"步骤 {step}：视觉导航未达目标，交回 VLMM 第 {self.visual_replan_attempts}/"
                    f"{VISUAL_REPLAN_MAX_ATTEMPTS} 次重规划"
                )
                if self.config.retry_cooldown_seconds:
                    time.sleep(self.config.retry_cooldown_seconds)
                return False
            if local_navigation_failure:
                if self.config.retry_cooldown_seconds:
                    time.sleep(self.config.retry_cooldown_seconds)
                return False
            if workflow.note_local_failure(result.message) is True:
                print(
                    f"步骤 {step}：本地步骤连续失败，已转 VLM 接管"
                    f"（阶段={workflow.phase}，目标={workflow.target or '无'}）"
                )
                if self.config.retry_cooldown_seconds:
                    time.sleep(self.config.retry_cooldown_seconds)
                return False
            if self.failures >= self.config.max_retries and workflow.should_enter_recovery():
                workflow.note_recovery_attempt()
                print(
                    f"步骤 {step}：连续 {self.failures} 次失败，转 VLM 自由决策恢复一次 "
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
