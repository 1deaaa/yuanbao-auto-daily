"""状态层：同日进度恢复、任务推进与强制任务策略。"""

from __future__ import annotations

from zoneinfo import ZoneInfo
from datetime import datetime

from .constants import (
    DAILY_ABILITY_RULE_NAME,
    DAILY_ABILITY_RULE_VERSION,
    DAILY_TASK_MODE_ABILITY_STATION,
    TASK_ENTRY_ROW_MARKERS,
    TASK_LABELS,
    TASK_MAX_PROGRESS,
    TASK_ORDER,
    TASK_STATE_SCHEMA,
    TASK_TARGET_COUNTS,
)
from .models import (
    Observation,
    ToolResult,
)
from .state import (
    read_daily_state,
    write_daily_state,
)
from .welfare import (
    has_complete_new_welfare_task_catalog,
    merge_supplemental_task_progress,
    parse_local_welfare_progress,
)


class WorkflowStateMixin:
    """按职责拆分的混入类；方法实现与原主类完全一致。"""

    def restore_state(self) -> None:
        """恢复新版当天进度；旧流程计数归档，不带入新任务目录。"""
        self.state_restored = False
        self.terminal_restore = False
        self.daily_ability_done = False
        self.daily_ability_evidence = None
        self.daily_ability_click_attempts = 0
        self.daily_ability_button_checks = 0
        self.daily_ability_verification_checks = 0
        self.daily_ability_experience_opened = False
        self.daily_ability_voice_submitted = False
        self.progress = {key: None for key in TASK_ORDER}
        self.task_targets = {}
        self.skipped_tasks.clear()
        self.confirmed_unavailable_tasks.clear()
        today = datetime.now(ZoneInfo(self.config.timezone_name)).date().isoformat()
        state = read_daily_state(self.config.state_path)
        if state.get("date") != today:
            return
        if (
            state.get("task_schema") != TASK_STATE_SCHEMA
            or state.get("daily_task_mode") != DAILY_TASK_MODE_ABILITY_STATION
        ):
            legacy_fields = {
                key: state[key]
                for key in (
                    "task_schema",
                    "daily_task_mode",
                    "daily_done",
                    "task_progress",
                    "skipped_tasks",
                )
                if key in state
            }
            if legacy_fields:
                state["legacy_task_state"] = legacy_fields
            state.pop("daily_done", None)
            state.update(
                {
                    "task_schema": TASK_STATE_SCHEMA,
                    "daily_task_mode": DAILY_TASK_MODE_ABILITY_STATION,
                    "daily_ability_done": False,
                    "task_progress": {},
                    "skipped_tasks": [],
                }
            )
            write_daily_state(self.config.state_path, state)
            return
        saved_ability_done = state.get("daily_ability_done")
        saved_ability_evidence = state.get("daily_ability_evidence")
        saved_ability_rule_version = state.get("daily_ability_rule_version")
        if isinstance(saved_ability_done, bool):
            evidence_valid = (
                saved_ability_done is not True
                or (
                    saved_ability_evidence == DAILY_ABILITY_RULE_NAME
                    and saved_ability_rule_version == DAILY_ABILITY_RULE_VERSION
                )
            )
            self.daily_ability_done = saved_ability_done and evidence_valid
            if self.daily_ability_done:
                self.daily_ability_evidence = saved_ability_evidence
            elif saved_ability_done:
                print(
                    "同日能力站完成状态缺少主轮播卡完成证据，"
                    "本轮将重新点击“去体验”"
                )
            self.state_restored = True
        saved_targets = state.get("task_targets")
        if isinstance(saved_targets, dict):
            self.task_targets = {
                key: value
                for key, value in saved_targets.items()
                if key in TASK_ORDER and isinstance(value, int) and value > 0
            }
        saved = state.get("task_progress")
        if not isinstance(saved, dict):
            return
        for key in TASK_ORDER:
            value = saved.get(key)
            if isinstance(value, int) and 0 <= value <= TASK_MAX_PROGRESS:
                self.progress[key] = value
                self.state_restored = True
        unavailable = state.get("confirmed_unavailable_tasks")
        if isinstance(unavailable, list):
            self.confirmed_unavailable_tasks = {
                value for value in unavailable
                if isinstance(value, str) and value in TASK_ORDER
            }
        skipped = state.get("skipped_tasks")
        if isinstance(skipped, list):
            restored_skips = {
                value for value in skipped if isinstance(value, str) and value in TASK_ORDER
            }
            self.skipped_tasks = (
                restored_skips & self.confirmed_unavailable_tasks
                if self.config.force_all_daily_tasks
                else restored_skips
            )
        if (
            "same_template" in self.skipped_tasks
            and "same_template" not in self.confirmed_unavailable_tasks
        ):
            # 旧版把做同款推荐卡固定排除；重新读取新版福利页前先作废该历史跳过。
            # 当前日期的“完整目录确认未提供”是新证据，应保留以免重启后再次空转。
            self.skipped_tasks.discard("same_template")
            self.progress["same_template"] = 0
        # 兑换已经完成或明确因积分不足跳过时，重启只需回到“我们”页收尾。
        # 不再重新打开兑换商城，避免重复扫描商品和无意义的页面竞态。
        if (
            self.daily_ability_done is True
            and all(
                (
                    self.progress.get(key) == self._task_target(key)
                    if self.config.force_all_daily_tasks
                    else key in self.skipped_tasks
                    or self.progress.get(key) == self._task_target(key)
                )
                for key in TASK_ORDER
            )
            and state.get("card") == self.config.card_name
            and state.get("exchange_status") in {"used", "unavailable"}
        ):
            if state["exchange_status"] == "used":
                self.executor.reward_use_confirmed = True
            else:
                self.executor.reward_unavailable = True
            self.terminal_restore = True


    def _persist_task_state(self) -> None:
        """保存当天进度并保留兑换阶段已经写入的字段。"""
        state = read_daily_state(self.config.state_path)
        today = datetime.now(ZoneInfo(self.config.timezone_name)).date().isoformat()
        if state.get("date") != today:
            state = {}
        state.update(
            {
                "date": today,
                "task_schema": TASK_STATE_SCHEMA,
                "daily_task_mode": DAILY_TASK_MODE_ABILITY_STATION,
                "daily_ability_done": self.daily_ability_done is True,
                "daily_ability_rule_version": (
                    DAILY_ABILITY_RULE_VERSION
                    if self.daily_ability_done
                    and self.daily_ability_evidence == DAILY_ABILITY_RULE_NAME
                    else None
                ),
                "daily_ability_evidence": self.daily_ability_evidence,
                "task_progress": {
                    key: self.progress.get(key) for key in TASK_ORDER
                },
                "task_targets": dict(self.task_targets),
                "skipped_tasks": sorted(self.skipped_tasks),
                "confirmed_unavailable_tasks": sorted(self.confirmed_unavailable_tasks),
            }
        )
        write_daily_state(self.config.state_path, state)


    def _merge_progress(
        self, values: dict[str, int], authoritative_keys: set[str] | None = None
    ) -> None:
        """普通更新单调增加；权威福利页快照可纠正过高或过低的恢复状态。

        福利页任务行自己的“已完成 x/3”是唯一权威来源：即使处于强制任务模式
        也必须以页面为准，否则会把实际没做的任务记成已完成（例如做同款被
        记成 3/3，页面却仍是 0/3），当天就再也不会补做。
        """
        for key, value in values.items():
            previous = self.progress.get(key)
            if authoritative_keys and key in authoritative_keys:
                self.progress[key] = value
            else:
                self.progress[key] = max(value, previous if previous is not None else 0)
        self._persist_task_state()


    def _select_next(self) -> None:
        self._clear_recovery("任务推进")
        # 切换任务时清掉上一轮留下的测试短语标记：否则拍题这类不需要
        # input_test_prompt 的任务会继承上一个任务的标记，发送校验把
        # 应用自己填好的拍照题干误判成“不是固定测试文本”而一直拒绝提交。
        self._reset_prompt_variant()
        if not self.daily_ability_done:
            self.target = None
            self.phase = "daily_ability"
            self.expected_count = None
            self.substate = {}
            return
        for key in TASK_ORDER:
            if key in self.skipped_tasks:
                continue
            count = self.progress.get(key)
            if count is None or count < self._task_target(key):
                self.target = key
                self.phase = "open_target"
                target = self._task_target(key)
                self.expected_count = (
                    min(target, (count or 0) + 1) if count is not None else 1
                )
                self.substate = {}
                return
        self.target = None
        self.expected_count = None
        self.phase = "open_exchange"
        self.substate = {}


    def _reset_prompt_variant(self) -> None:
        """清空执行器上的测试短语标记。"""
        setter = getattr(self.executor, "set_prompt_variant", None)
        if callable(setter):
            setter(None)

    def _task_target(self, key: str) -> int:
        """返回任务的目标次数；页面给出的口径优先于默认常量。"""
        return self.task_targets.get(key) or TASK_TARGET_COUNTS.get(key, 3)

    def _advance_after_task(self) -> None:
        """任务生成和奖励处理均成功后本地推进一次，避免重复请求视觉报告。"""
        if self.target in TASK_LABELS:
            current = self.progress.get(self.target) or 0
            target = self._task_target(self.target)
            self.progress[self.target] = min(target, current + 1)
        if self.target == "same_template":
            self.same_template_revealed = False
        self._persist_task_state()
        self._select_next()


    def _task_kind(self) -> str:
        return self.target or ""


    def reconcile_visible_progress(self, observation: Observation) -> bool:
        """当福利页已显示当前目标完成时，先同步进度并跳过重复入口点击。"""
        if (
            self.phase != "open_target"
            or self.target not in TASK_LABELS
            or self.expected_count is None
        ):
            return False
        report = parse_local_welfare_progress(observation)
        if report is None:
            return False
        report = self._merge_local_report(report, observation)
        visible_count = report.get(self.target)
        # expected_count 是下一轮要做的次数（例如 1/4），不是任务完成阈值。
        # 页面已经显示 3/4 时不能把它误判为“任务已完成”而反复对账；
        # 只有达到该任务页面实际目标次数才跳过入口。
        target_count = self._task_target(self.target)
        if not isinstance(visible_count, int) or visible_count < target_count:
            return False
        target_label = TASK_LABELS[self.target]
        result = self._parse_report(report, observation)
        if result.ok:
            print(f"福利页面已显示 {target_label} 完成，已同步进度并跳过重复点击")
        return result.ok


    def reconcile_return_to_welfare(self, observation: Observation) -> bool:
        """返回阶段观测到福利页时立即推进，抵抗 WebView 返回后的结果竞态。"""
        if self.phase != "return" or observation.stage != "welfare":
            return False
        self._advance_after_task()
        print("已观测到福利中心页面，完成返回阶段并推进下一任务")
        return True


    def _complete_task_assertions(self) -> ToolResult:
        if (
            self.daily_ability_done is not True
            or self.daily_ability_evidence != DAILY_ABILITY_RULE_NAME
        ):
            return ToolResult(False, "主轮播卡尚未由页面状态确认‘已完成’")
        incomplete = [
            TASK_LABELS[key]
            for key in TASK_ORDER
            if key not in self.skipped_tasks
            and self.progress.get(key) != self._task_target(key)
        ]
        if incomplete:
            return ToolResult(False, f"仍有未完成任务：{'、'.join(incomplete)}")
        reward_unavailable = bool(getattr(self.executor, "reward_unavailable", False))
        if not self.executor.reward_use_confirmed and not reward_unavailable:
            return ToolResult(False, "尚未确认 QQ 超级会员卡已对绑定账号使用成功")
        if self.phase != "complete":
            return ToolResult(False, "尚未完成元宝首页收尾导航")
        self.finished = True
        if reward_unavailable:
            return ToolResult(True, "每日任务已完成；QQ超级会员3天卡积分不足，已安全跳过兑换")
        return ToolResult(True, "每日任务与奖品使用均已完成，本轮进入下一次等待")


    def can_conclude_target_unavailable(self, observation: Observation) -> bool:
        """完整目录多次滚动仍不含当前任务时，确认页面没有提供该任务。"""
        if self.phase != "open_target" or self.target not in TASK_ENTRY_ROW_MARKERS:
            return False
        if observation.stage != "welfare":
            return False
        visible = [
            node.searchable
            for node in observation.nodes
            if node.enabled and node.bounds.area > 0
        ]
        target_markers = TASK_ENTRY_ROW_MARKERS[self.target]
        if any(any(marker.lower() in text for marker in target_markers) for text in visible):
            return False
        if self.config.force_all_daily_tasks:
            # 强制模式不能因失败跳过；只有本日首次报告已用完整目录确认任务
            # 缺席，且当前仍能读取完整目录时，才允许稳定滚动后记为“页面未提供”。
            if self.target not in self.confirmed_unavailable_tasks:
                return False
            report = merge_supplemental_task_progress(
                observation, parse_local_welfare_progress(observation)
            )
            return has_complete_new_welfare_task_catalog(observation, report)
        other_rows = 0
        for key, row_markers in TASK_ENTRY_ROW_MARKERS.items():
            if key == self.target:
                continue
            if any(
                any(marker.lower() in text for marker in row_markers)
                for text in visible
            ):
                other_rows += 1
        return other_rows >= 2 and any(
            marker in " ".join(visible) for marker in ("每日任务", "当日凌晨", "已完成")
        )


    def skip_unavailable_target(self, observation: Observation | None = None) -> bool:
        """仅将页面证实未提供的目标记入恢复状态；普通失败不可跳过。"""
        if self.target not in TASK_ORDER:
            return False
        if self.config.force_all_daily_tasks:
            if observation is None or not self.can_conclude_target_unavailable(observation):
                print("强制任务模式：未获得完整目录缺席证据，不跳过当前任务")
                return False
            self.confirmed_unavailable_tasks.add(self.target)
        target = self.target
        self.skipped_tasks.add(target)
        self._persist_task_state()
        self._select_next()
        print(f"当前福利任务目录已确认未提供 {TASK_LABELS[target]}，不再反复尝试入口")
        return True
