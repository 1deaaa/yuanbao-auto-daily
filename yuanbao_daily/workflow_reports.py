"""报告层：福利页进度解析、兑换读数与能力站完成核验。"""

from __future__ import annotations

from typing import Any
import time

from .constants import (
    FORCED_DAILY_TASKS,
    NEW_WELFARE_DAILY_TASKS,
    TASK_LABELS,
    TASK_ORDER,
    TASK_TARGET_COUNTS,
)
from .models import (
    Observation,
    ToolResult,
)
from .welfare import (
    has_authoritative_local_task_catalog,
    parse_question_card_target,
    has_complete_new_welfare_task_catalog,
    is_new_welfare_page,
    merge_supplemental_task_progress,
    parse_local_welfare_progress,
)


class WorkflowReportsMixin:
    """按职责拆分的混入类；方法实现与原主类完全一致。"""

    def local_task_report(self, observation: Observation) -> dict[str, Any] | None:
        """从无障碍树直接读出任务计数；读不到时返回 None。

        福利页的任务行会把 ``已完成 x/3`` 作为独立文本节点暴露，精度高于
        视觉模型读截图；只有本地读不出时才需要模型补充。
        """
        local_report = merge_supplemental_task_progress(
            observation, parse_local_welfare_progress(observation)
        )
        if local_report is None:
            return None
        report: dict[str, Any] = {}
        for key in TASK_ORDER:
            value = local_report.get(key)
            if isinstance(value, int):
                report[key] = value
        available = local_report.get("available_tasks")
        if isinstance(available, list):
            report["available_tasks"] = list(available)
        return report

    @staticmethod
    def _merge_local_report(
        args: dict[str, Any], observation: Observation
    ) -> dict[str, Any]:
        """以福利页当前任务计数校准视觉报告。"""
        new_welfare_page = is_new_welfare_page(observation)
        local_report = merge_supplemental_task_progress(
            observation, parse_local_welfare_progress(observation)
        )
        if local_report is None:
            if new_welfare_page:
                # 空树时保留新版固定三项；模型只有在当前截图确有进度时才能
                # 另外报告做同款，不能仅凭推荐模板按钮把它加入目录。
                merged = dict(args)
                merged["available_tasks"] = list(NEW_WELFARE_DAILY_TASKS)
                reported_available = args.get("available_tasks")
                if (
                    not observation.nodes
                    and isinstance(reported_available, list)
                    and "same_template" in reported_available
                    and isinstance(args.get("same_template"), int)
                ):
                    merged["available_tasks"].append("same_template")
                return merged
            return args
        merged = dict(args)
        local_available = local_report.get("available_tasks")
        reported_available = merged.get("available_tasks")
        if new_welfare_page:
            merged["available_tasks"] = list(NEW_WELFARE_DAILY_TASKS)
            merged["question"] = 0
            if isinstance(local_report.get("same_template"), int):
                merged["available_tasks"].append("same_template")
                merged["same_template"] = local_report["same_template"]
            else:
                merged["same_template"] = 0
        elif (
            isinstance(local_available, list)
            and has_authoritative_local_task_catalog(observation, local_report)
        ):
            # “趣问元宝”等活动卡不属于每日任务；已锚定任务区时，目录按本地行精确取值。
            merged["available_tasks"] = list(dict.fromkeys(local_available))
        elif (
            isinstance(local_available, list)
            and isinstance(reported_available, list)
            and all(value in TASK_ORDER for value in reported_available)
        ):
            merged["available_tasks"] = list(
                dict.fromkeys((*reported_available, *local_available))
            )
        elif "available_tasks" not in merged and isinstance(local_available, list):
            merged["available_tasks"] = local_available
        if has_complete_new_welfare_task_catalog(observation, local_report):
            for key in NEW_WELFARE_DAILY_TASKS:
                merged[key] = local_report[key]
            if isinstance(local_report.get("same_template"), int):
                merged["same_template"] = local_report["same_template"]
        elif not new_welfare_page:
            for key in TASK_ORDER:
                value = local_report.get(key)
                if isinstance(value, int):
                    merged[key] = value
        return merged


    def _parse_report(
        self, args: dict[str, Any], observation: Observation | None = None
    ) -> ToolResult:
        try:
            values = {}
            for key in TASK_ORDER:
                previous = self.progress.get(key)
                value = args.get(key, previous if previous is not None else 0)
                values[key] = int(value)
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(False, "report_tasks 缺少固定进度字段")
        if any(
            value < 0 or value > TASK_TARGET_COUNTS.get(key, 3)
            for key, value in values.items()
        ):
            return ToolResult(False, "任务进度超出该任务的合理次数范围")
        available = args.get("available_tasks")
        if available is not None:
            if not isinstance(available, list) or any(
                not isinstance(value, str) or value not in TASK_ORDER for value in available
            ):
                return ToolResult(False, "available_tasks 必须是任务目录中的字符串数组")
        if observation is not None and self.phase == "report":
            # 趣问元宝的目标次数写在卡片进度里（(0/3) 或 (0/4)），每天可能变化，
            # 以页面为准，不能写死。
            question_target = parse_question_card_target(observation)
            if question_target is not None:
                self.task_targets["question"] = question_target
        if self.config.force_all_daily_tasks:
            # 视觉报告仍用于读取当前可见进度；任务目录只决定入口可见性，不能
            # 在强制模式下将趣问元宝或做同款写入 skipped_tasks。
            available = list(FORCED_DAILY_TASKS)
            self.skipped_tasks.clear()
        authoritative_keys: set[str] = set()
        if observation is not None and self.phase == "report" and observation.stage == "welfare":
            local_report = merge_supplemental_task_progress(
                observation, parse_local_welfare_progress(observation)
            )
            new_welfare_page = is_new_welfare_page(observation)
            complete_catalog = has_complete_new_welfare_task_catalog(
                observation, local_report
            )
            empty_catalog_scrolled = (
                new_welfare_page
                and not observation.nodes
                and getattr(self.executor, "_welfare_task_catalog_scrolled", False)
            )
            if new_welfare_page and not complete_catalog and not empty_catalog_scrolled:
                return ToolResult(
                    False,
                    "新版每日任务目录尚未完整显示；先滚动到写作、拍题和P图任务及其进度后再报告",
                    {"stage": "welfare", "visual_replan": True},
                )
            if complete_catalog and local_report is not None:
                authoritative_keys = {
                    key
                    for key in NEW_WELFARE_DAILY_TASKS
                    if isinstance(local_report.get(key), int)
                }
                if isinstance(local_report.get("same_template"), int):
                    authoritative_keys.add("same_template")
            elif empty_catalog_scrolled:
                authoritative_keys = set(NEW_WELFARE_DAILY_TASKS)
                if local_report is not None and isinstance(
                    local_report.get("same_template"), int
                ):
                    authoritative_keys.add("same_template")
        if available is not None:
            self.skipped_tasks = set(TASK_ORDER) - set(available)
        merged_values = {}
        for key, value in values.items():
            if key in authoritative_keys:
                merged_values[key] = value
            elif key in self.skipped_tasks:
                merged_values[key] = self.progress[key] or 0
            else:
                merged_values[key] = max(value, self.progress[key] or 0)
        if self.target and self.expected_count is not None and self.target in TASK_LABELS:
            if self.target in self.skipped_tasks:
                self._merge_progress(merged_values, authoritative_keys)
                self._select_next()
                return ToolResult(True, f"当前页面未提供任务 {TASK_LABELS[self.target]}，已跳过旧入口")
            current = merged_values[self.target]
            if current < self.expected_count:
                self._merge_progress(merged_values, authoritative_keys)
                self.phase = "open_target"
                return ToolResult(
                    False,
                    f"任务 {TASK_LABELS[self.target]} 当前仍为 {current}/"
                    f"{self.expected_count}，必须重新点击入口完成下一次",
                )
        self._merge_progress(merged_values, authoritative_keys)
        if self.terminal_restore:
            # 收尾快路径只能由福利页的任务计数确认：本地状态可能把实际没做的
            # 任务记成已完成（例如做同款记成 3/3，页面仍是 0/3），此时必须
            # 继续把任务补做完，不能直接收尾。
            undone = [
                key
                for key in sorted(authoritative_keys)
                if merged_values.get(key, 0) < TASK_TARGET_COUNTS.get(key, 3)
            ]
            if undone:
                self.terminal_restore = False
                self.executor.reward_unavailable = False
                self.executor.reward_use_confirmed = False
                print(
                    "福利页显示任务尚未完成："
                    + "、".join(TASK_LABELS[key] for key in undone)
                    + "；继续执行每日任务"
                )
            else:
                self.phase = "return_home"
                return ToolResult(True, "福利页已确认全部任务完成，直接收尾")
        self._select_next()
        return ToolResult(True, "已更新福利中心任务进度")


    def _parse_exchange_report(
        self, args: dict[str, Any], observation: Observation
    ) -> ToolResult:
        """接受 VLMM 对空层级兑换页的当前截图读数，并只处理积分不足分支。"""
        if args.get("page") != "exchange":
            return ToolResult(False, "只有确认当前截图是兑换商城时才能报告兑换页")
        try:
            points = int(args["points"])
            cost = int(args["cost"])
        except (KeyError, TypeError, ValueError):
            return ToolResult(False, "兑换页报告必须包含当前积分和目标卡价格")
        if points < 0 or cost <= 0:
            return ToolResult(False, "兑换页报告中的积分和价格必须为正数或零")
        if args.get("target_visible") is not True:
            return ToolResult(False, "必须先在当前截图中看到 QQ 超级会员 3 天卡及其兑换价格")
        # 模型读数可能把 35500 误读成 5500；如果当前无障碍观测仍能直接
        # 读到顶部积分，页面证据优先，避免把可兑换订单错误标记为积分不足。
        read_points = getattr(self.executor, "_read_exchange_points", None)
        if callable(read_points):
            observed_points = read_points(observation)
            if observed_points is not None:
                points = observed_points
        points_verified = observed_points is not None if callable(read_points) else False
        if points < cost and not points_verified:
            # 福利 WebView 会间歇性返回空树。积分不足属于跳过兑换的关键分支，
            # 必须主动重读页面；仍无直接积分证据时不能按模型低读数写状态。
            for _ in range(3):
                time.sleep(1)
                refreshed = getattr(self.executor, "observe", lambda: None)()
                if refreshed is None or refreshed.stage != "exchange":
                    continue
                if callable(read_points):
                    observed_points = read_points(refreshed)
                    if observed_points is not None:
                        points = observed_points
                        points_verified = True
                        break
            if not points_verified:
                return ToolResult(
                    False,
                    "模型报告积分不足，但当前兑换页没有可直接核实的积分；拒绝写入跳过状态，请重新观察",
                    {"stage": "exchange", "visual_replan": True},
                )
        self.executor.reward_card_name = self.config.card_name
        setter = getattr(self.executor, "set_visual_exchange_report", None)
        if callable(setter):
            setter(points, cost, verified=points_verified)
        else:
            # 兼容没有显式 setter 的测试替身或旧执行器。
            setattr(self.executor, "visual_exchange_points", points)
            setattr(self.executor, "visual_exchange_cost", cost)
            setattr(self.executor, "visual_exchange_points_verified", points_verified)
        if points < cost:
            self.executor.reward_unavailable = True
            write_state = getattr(self.executor, "_write_state", None)
            if callable(write_state):
                write_state("unavailable")
            self.substate["exchange_reported"] = True
            self.phase = "return_home"
            return ToolResult(
                True,
                f"目标商品“{self.config.card_name}”需要 {cost} 积分，当前仅 {points}，已安全跳过兑换",
                {
                    "stage": "exchange",
                    "reward_unavailable": True,
                    "points": points,
                    "cost": cost,
                },
            )
        self.substate["exchange_reported"] = True
        self.substate["visual_points"] = points
        self.substate["visual_cost"] = cost
        self.phase = "redeem"
        return ToolResult(
            True,
            f"已确认兑换商城和目标商品，当前积分 {points}，目标价格 {cost}",
            {"stage": "exchange", "points": points, "cost": cost},
        )


    @staticmethod
    def _parse_daily_ability_verification(
        args: dict[str, Any], observation: Observation
    ) -> ToolResult:
        """只接受当前福利页主轮播卡固定位置的明确完成文案。"""
        if observation.stage != "welfare":
            return ToolResult(
                False,
                "能力站完成状态核验要求当前停留在福利中心",
                {"stage": observation.stage},
            )
        status = args.get("card_status")
        if status not in {"已完成", "去体验", "未显示"}:
            return ToolResult(False, "状态核验必须按当前截图报告卡片状态")
        if status == "未显示":
            return ToolResult(
                True,
                "主轮播卡按钮状态当前不可见；未写入完成状态，稍后重新核验",
                {"stage": "welfare", "card_status": status},
            )
        if status == "已完成":
            return ToolResult(
                True,
                "当前截图已确认主轮播卡显示‘已完成’",
                {"stage": "welfare", "card_status": status},
            )
        return ToolResult(
            True,
            "主轮播卡仍显示‘去体验’，未确认每日大任务完成",
            {"stage": "welfare", "card_status": status},
        )
