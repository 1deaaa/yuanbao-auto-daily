"""编排层：阶段状态机、任务推进与完成断言。"""
from pathlib import Path
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .constants import (
    ACTIVITY_PAGE_MARKERS,
    DAILY_ABILITY_BUTTON_CHECK_LIMIT,
    DAILY_ABILITY_EXPERIENCE_ACTION_MARKERS,
    DAILY_ABILITY_EXPERIENCE_TOOLS,
    DAILY_ABILITY_RULE_NAME,
    DAILY_ABILITY_RULE_VERSION,
    DAILY_ABILITY_TOOLS,
    DAILY_TASK_MODE_ABILITY_STATION,
    DEFAULT_CARD_NAME,
    FORCED_DAILY_TASKS,
    IMAGE_TASKS,
    LOCAL_DETERMINISTIC_TOOLS,
    NEW_WELFARE_DAILY_TASKS,
    PACKAGE_NAME,
    TASK_ENTRY_ACTION_MARKERS,
    TASK_ENTRY_MARKERS,
    TASK_ENTRY_ROW_MARKERS,
    TASK_ENTRY_SCROLL_MAX_ATTEMPTS,
    TASK_ENTRY_ACTION_X_RATIO,
    TASK_LABELS,
    TASK_ORDER,
    TASK_STATE_SCHEMA,
    TASK_STEPS,
    TEXT_TASKS,
    VISUAL_EXCHANGE_TOOLS,
    VISUAL_NAVIGATION_TOOLS,
    DAILY_ABILITY_VERIFY_CHECK_LIMIT,
)
from .errors import (
    AgentError,
    ManualActionRequired,
)
from .executor import ToolExecutor
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
    find_semantic_node,
    node_interaction_bounds,
)
from .welfare import (
    has_authoritative_local_task_catalog,
    has_complete_new_welfare_task_catalog,
    has_visible_exchange_product,
    is_new_welfare_page,
    merge_supplemental_task_progress,
    parse_local_welfare_progress,
)

@dataclass
class Workflow:
    config: Config
    executor: ToolExecutor
    daily_ability_done: bool = False
    daily_ability_evidence: str | None = None
    daily_ability_click_attempts: int = 0
    daily_ability_button_checks: int = 0
    daily_ability_verification_checks: int = 0
    daily_ability_experience_opened: bool = False
    daily_ability_voice_submitted: bool = False
    progress: dict[str, int | None] = field(default_factory=lambda: {key: None for key in TASK_ORDER})
    phase: str = "navigate_welfare"
    target: str | None = None
    substate: dict[str, bool] = field(default_factory=dict)
    expected_count: int | None = None
    stale_reports: int = 0
    finished: bool = False
    entry_points: dict[str, tuple[float, float]] = field(default_factory=dict)
    # report_tasks 使用完整层级选出的入口只消费一次。福利 WebView 下一轮
    # 可能暂时返回空树，但紧接着的截图仍是同一页面，此时应沿用这次已验证的
    # 坐标，避免把顶部签到卡或空白区域交给视觉模型猜测。
    primed_entry_points: set[str] = field(default_factory=set)
    entry_scroll_attempts: dict[str, int] = field(default_factory=dict)
    entry_failures: int = 0
    state_restored: bool = False
    fixed_entry_disabled: set[str] = field(default_factory=set)
    # 任务目录由福利页视觉报告决定；页面改版移除某项任务时，本轮不再盲点
    # 历史入口。该集合只用于当前日期的恢复，不替代页面上的完成证据。
    skipped_tasks: set[str] = field(default_factory=set)
    same_template_revealed: bool = False
    terminal_restore: bool = False
    # 卡住恢复（VLM 自由决策）状态：同一卡点只恢复一次，避免无限循环。
    # recover_phase/recover_target 记录恢复开始时的阶段与目标；恢复成功或
    # 阶段/目标变化后清零，允许新的卡点再次恢复。
    recovering: bool = False
    recover_phase: str | None = None
    recover_target: str | None = None
    recover_attempts: int = 0

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
        self.skipped_tasks.clear()
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
        saved = state.get("task_progress")
        if not isinstance(saved, dict):
            return
        for key in TASK_ORDER:
            value = saved.get(key)
            if isinstance(value, int) and 0 <= value <= 3:
                self.progress[key] = value
                self.state_restored = True
        skipped = state.get("skipped_tasks")
        if isinstance(skipped, list):
            self.skipped_tasks = {
                value for value in skipped if isinstance(value, str) and value in TASK_ORDER
            }
        if self.config.force_all_daily_tasks:
            # 强制任务模式不接受历史跳过名单；失败只能保留未完成进度并重新定位。
            self.skipped_tasks.clear()
        if "same_template" in self.skipped_tasks:
            # 旧版把做同款推荐卡固定排除；重新读取新版福利页前先作废该跳过结果。
            self.skipped_tasks.discard("same_template")
            self.progress["same_template"] = 0
        # 兑换已经完成或明确因积分不足跳过时，重启只需回到“我们”页收尾。
        # 不再重新打开兑换商城，避免重复扫描商品和无意义的页面竞态。
        if (
            self.daily_ability_done is True
            and all(
                (
                    self.progress.get(key) == 3
                    if self.config.force_all_daily_tasks
                    else key in self.skipped_tasks or self.progress.get(key) == 3
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
                "skipped_tasks": sorted(self.skipped_tasks),
            }
        )
        write_daily_state(self.config.state_path, state)

    def _merge_progress(
        self, values: dict[str, int], authoritative_keys: set[str] | None = None
    ) -> None:
        """普通更新单调增加；首次权威福利页快照可纠正过高的恢复状态。"""
        for key, value in values.items():
            previous = self.progress.get(key)
            if (
                authoritative_keys
                and key in authoritative_keys
                and not self.config.force_all_daily_tasks
            ):
                self.progress[key] = value
            else:
                self.progress[key] = max(value, previous if previous is not None else 0)
        self._persist_task_state()

    def recovery_hint(self) -> str:
        """卡住恢复时给 VLM 的自由决策提示：说明卡点，不限定具体工具。"""
        label = TASK_LABELS.get(self.target or "", self.target or "无")
        return (
            "注意：常规流程已在此卡住（同一阶段连续失败），现进入一次智能恢复："
            f"阶段={self.phase}，当前目标={label}。"
            "请只看最新截图自由决策，不要沿用之前的坐标：若目标不在视口内先滑动；"
            "若有意外弹窗请先关闭；若入口位置不对请重新定位；若已在正确页面请继续正常步骤。"
            "只允许一个工具调用。"
        )

    def requires_visual_navigation(self) -> bool:
        """所有易变页面入口都由 VLMM 按当前截图导航，避免复用旧坐标。"""
        return self.phase in {"navigate_welfare", "open_target", "open_exchange"} or (
            self.phase == "perform"
            and self.target == "same_template"
            and self.substate.get("entry_clicked")
            and not self.substate.get("template_clicked")
        )

    def _welfare_exchange_entry_node(self, observation: Observation) -> Node | None:
        """识别兑换阶段中仍停留在福利页的“兑换商城”入口。

        福利页和真实商城都可能暴露“兑换商城”文案，不能只按阶段字符串判断。
        当前无障碍树若没有可见目标商品，则该文案只能作为福利页入口处理；真实
        商城至少需要在当前视口暴露目标商品，随后才允许进入商品/兑换流程。
        """
        if observation.stage not in {"welfare", "exchange"}:
            return None
        config = getattr(self.executor, "config", None)
        card_name = getattr(config, "card_name", DEFAULT_CARD_NAME)
        if has_visible_exchange_product(observation, card_name):
            return None
        node = find_semantic_node(observation.nodes, "兑换商城", exact=True)
        if node is None:
            node = find_semantic_node(observation.nodes, "兑换商城")
        return node

    def semantic_visual_recovery_action(
        self, observation: Observation
    ) -> tuple[str, dict[str, int]] | None:
        """视觉重规划失败后，使用当前层级明确暴露的语义控件救援一次。

        这不是页面坐标后备：节点边界和文案都来自本轮观测。只有返回福利中心
        或兑换商城这类唯一语义入口被无障碍树明确暴露时才启用，其他变化仍交给
        VLMM 根据截图判断。
        """
        if self.phase == "return":
            if observation.stage != "welfare":
                # 返回福利中心不需要视觉模型选择易被顶部控件遮挡的坐标；
                # 执行器会按当前 Activity 和无障碍语义，经“我们”页重新打开福利中心。
                ours = find_node(observation.nodes, "我们", exact=True)
                if ours is not None:
                    print("返回阶段使用当前页面语义导航回福利中心")
                    return "return_to_welfare", {}
            finder = getattr(self.executor, "_welfare_return_node", None)
            node = finder(observation) if callable(finder) else None
            if node is not None:
                point_finder = getattr(self.executor, "_welfare_return_tap_point", None)
                if callable(point_finder):
                    point = point_finder(
                        observation,
                        node,
                        getattr(self.executor, "width", 0),
                        getattr(self.executor, "height", 0),
                    )
                else:
                    point = node_interaction_bounds(node).center
                if point != (0, 0) or node_interaction_bounds(node).area > 0:
                    print("视觉重规划失败，使用当前观测中的“返回福利中心”语义控件")
                    return "tap", {"x": point[0], "y": point[1]}
        if self.phase == "navigate_welfare":
            activity_lower = observation.activity.lower()
            if PACKAGE_NAME not in activity_lower and observation.activity:
                # 视觉模型误触系统浏览器或其它自由窗口时，返回键是与布局无关的
                # 通用恢复动作；回到元宝后再继续视觉识别福利入口。
                print("当前前台不是元宝页面，使用返回键关闭外部窗口")
                return "press_back", {}
            return_finder = getattr(self.executor, "_welfare_return_node", None)
            return_node = return_finder(observation) if callable(return_finder) else None
            if return_node is not None:
                # 启动页可能残留上一次任务的“返回福利中心”入口；这是当前层级
                # 明确的语义信号，直接复用执行器的本地导航，避免视觉模型反复
                # 点击顶部控件或旧坐标。
                print("启动导航发现“返回福利中心”，使用当前页面语义导航")
                return "return_to_welfare", {}
            finder = getattr(self.executor, "_welfare_entry_click_node", None)
            node = finder(observation) if callable(finder) else None
            if node is not None:
                bounds = node_interaction_bounds(node)
                if bounds.area > 0:
                    print("使用当前观测中的“福利中心”语义控件进入福利页")
                    # 入口节点可能同时被标题、按钮和顶部悬浮层暴露；直接
                    # 交给 open_welfare 让执行器按当前层级点击，绕过视觉点击的
                    # 前景遮挡判定，仍然不复用历史坐标。
                    return "open_welfare", {}
        if self.phase == "open_exchange":
            node = self._welfare_exchange_entry_node(observation)
            if node is not None:
                bounds = node_interaction_bounds(node)
                if bounds.area > 0:
                    print("视觉重规划失败，使用当前观测中的福利页“兑换商城”语义入口")
                    return "tap", {"x": bounds.center[0], "y": bounds.center[1]}
        return None

    def context(self, observation: Observation) -> str:
        if self.phase == "report":
            progress = "待从当前福利页重新读取；忽略恢复状态中的旧计数"
        else:
            progress = ", ".join(
                f"{TASK_LABELS[key]}={self.progress.get(key) if self.progress.get(key) is not None else '?'} / 3"
                for key in TASK_ORDER
            )
        substate = ", ".join(
            name for name, completed in self.substate.items() if completed
        ) or "无"
        coordinate_hint = ""
        if self.phase == "daily_ability":
            coordinate_hint = (
                "能力卡标题、颜色和具体功能会轮换，不作筛选；只点击‘元宝能力站’主轮播卡固定位置、"
                "当前显示‘去体验’的按钮，忽略页面顶部轮播里的同名按钮。若当前截图已显示刚打开的体验浮层，"
                "调用 continue_daily_ability 将其交给模型，不要重复点击卡片。"
            )
        elif self.phase == "daily_ability_experience":
            coordinate_hint = (
                "刚才已点击主轮播卡‘去体验’。当前能力内容每次可能不同，请只依据最新截图完成页面要求的互动；"
                "如果浮层遮罩下仍能看到‘去体验’，那是背景按钮，不能重复点击；应点击浮层正面的操作入口。"
                "完成后返回福利中心。不要再切换或点击福利页的其它卡片。"
            )
        elif self.phase == "verify_daily_ability":
            coordinate_hint = (
                "只检查元宝能力站主轮播卡固定位置的当前按钮状态，不识别标题或颜色；"
                "只有最新截图明确显示‘已完成’才算成功。仍显示‘去体验’或只是打开过体验页都不算完成。"
            )
        if self.phase == "open_target" and self.target in TASK_ENTRY_MARKERS:
            width = getattr(self.executor, "width", 0)
            height = getattr(self.executor, "height", 0)
            coordinate_hint = (
                f"屏幕原始像素为 {width}x{height}；当前目标按钮位于福利中心任务列表，"
                "不要点击顶部积分卡的同名按钮。"
            )
        if self.phase == "open_exchange":
            if self.substate.get("entry_clicked"):
                coordinate_hint = (
                    "兑换入口已经点击；请根据最新截图确认是否已进入兑换商城。"
                    "若截图明确显示商城，调用 report_exchange，填写当前可见积分、QQ超级会员3天卡价格和 target_visible=true；"
                    "若仍是福利页，重新定位并点击兑换商城，目标不在视口内先滑动。"
                )
            else:
                coordinate_hint = (
                    "请根据最新截图点击福利页中的兑换商城入口；无障碍层级为空时仍以截图为准，"
                    "不要调用 open_exchange，也不要猜测历史坐标。"
                )
        return (
            f"阶段={self.phase}; 能力站每日大任务已完成={self.daily_ability_done}; "
            f"强制执行全部任务={self.config.force_all_daily_tasks}; "
            f"当前目标={self.target or '无'}; "
            f"任务进度={progress}; 当前子步骤已完成={substate}; "
            f"本轮页面未提供的任务={','.join(sorted(self.skipped_tasks)) or '无'}; "
            f"期望计数={self.expected_count if self.expected_count is not None else '无'}; "
            f"奖励使用证据={self.executor.reward_use_confirmed}; {coordinate_hint}"
        )

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
        if any(value < 0 or value > 3 for value in values.values()):
            return ToolResult(False, "任务进度必须在 0 到 3 之间")
        available = args.get("available_tasks")
        if available is not None:
            if not isinstance(available, list) or any(
                not isinstance(value, str) or value not in TASK_ORDER for value in available
            ):
                return ToolResult(False, "available_tasks 必须是任务目录中的字符串数组")
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
                self.stale_reports = 0
                self._select_next()
                return ToolResult(True, f"当前页面未提供任务 {TASK_LABELS[self.target]}，已跳过旧入口")
            current = merged_values[self.target]
            if current < self.expected_count:
                self.stale_reports += 1
                self._merge_progress(merged_values, authoritative_keys)
                self.phase = "open_target"
                return ToolResult(
                    False,
                    f"任务 {TASK_LABELS[self.target]} 当前仍为 {current}/3，必须重新点击入口完成下一次",
                )
        self._merge_progress(merged_values, authoritative_keys)
        self.stale_reports = 0
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
        if not isinstance(visible_count, int) or visible_count < self.expected_count:
            return False
        target_label = TASK_LABELS[self.target]
        result = self._parse_report(report, observation)
        if result.ok:
            self._prime_entry_point(observation)
            print(f"福利页面已显示 {target_label} 完成，已同步进度并跳过重复点击")
        return result.ok

    def reconcile_return_to_welfare(self, observation: Observation) -> bool:
        """返回阶段观测到福利页时立即推进，抵抗 WebView 返回后的结果竞态。"""
        if self.phase != "return" or observation.stage != "welfare":
            return False
        self._advance_after_task()
        print("已观测到福利中心页面，完成返回阶段并推进下一任务")
        return True

    def _select_next(self) -> None:
        self._clear_recovery("任务推进")
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
            if count is None or count < 3:
                self.target = key
                self.phase = "open_target"
                self.expected_count = min(3, (count or 0) + 1) if count is not None else 1
                self.substate = {}
                return
        self.target = None
        self.expected_count = None
        self.phase = "open_exchange"
        self.substate = {}

    def _clear_recovery(self, reason: str = "") -> None:
        """恢复结束（成功推进或切到新卡点）后清零，允许后续新卡点再次恢复。"""
        if self.recovering or self.recover_attempts:
            detail = f"（{reason}）" if reason else ""
            print(f"卡住恢复结束{detail}，清理恢复状态")
        self.recovering = False
        self.recover_phase = None
        self.recover_target = None
        self.recover_attempts = 0

    def should_enter_recovery(self) -> bool:
        """同一阶段+目标只恢复一次；切到新阶段/目标后允许再次恢复。"""
        if self.recover_phase != self.phase or self.recover_target != self.target:
            if self.recovering or self.recover_attempts:
                self._clear_recovery("阶段或目标已变化")
        if self.recovering:
            return False
        if self.recover_attempts > 0:
            return False
        return True

    def note_recovery_attempt(self) -> None:
        """记录一次 VLM 自由决策恢复尝试，并作废当前入口缓存。

        同一入口坐标已连续失败，恢复时必须让 VLM 按最新截图重新定位；
        否则恢复动作会复用同一个坏坐标，再次失败。
        """
        self.recovering = True
        self.recover_phase = self.phase
        self.recover_target = self.target
        self.recover_attempts += 1
        key = self._entry_cache_key()
        if key is not None and key in self.entry_points:
            self.entry_points.pop(key, None)
            print(f"卡住恢复：已作废入口缓存 {key}，VLM 将按最新截图重新定位")

    def finish_recovery(self, reason: str) -> None:
        """恢复动作执行后即退出恢复态；成败由后续步骤的 failures 计数判定。"""
        self.recovering = False
        print(f"卡住恢复动作已执行（{reason}），回到正常流程观察效果")

    def _advance_after_task(self) -> None:
        """任务生成和奖励处理均成功后本地推进一次，避免重复请求视觉报告。"""
        if self.target in TASK_LABELS:
            current = self.progress.get(self.target) or 0
            self.progress[self.target] = min(3, current + 1)
        if self.target == "same_template":
            self.entry_points.pop("same_template", None)
            self.entry_points.pop("same_template:template", None)
            self.same_template_revealed = False
        self._persist_task_state()
        self._select_next()

    def _task_kind(self) -> str:
        return self.target or ""

    @staticmethod
    def task_catalog_signature(observation: Observation) -> tuple[tuple[Any, ...], ...]:
        """提取可见任务目录的稳定签名，排除上方奖励轮播等无关动态区域。"""
        markers = tuple(
            marker
            for values in TASK_ENTRY_ROW_MARKERS.values()
            for marker in values
        )
        rows = []
        for node in observation.nodes:
            if node.bounds.area <= 0 or not node.enabled:
                continue
            if any(marker.lower() in node.searchable for marker in markers):
                rows.append(
                    (
                        node.searchable,
                        node.bounds.left,
                        node.bounds.top,
                        node.bounds.right,
                        node.bounds.bottom,
                    )
                )
        return tuple(sorted(rows))

    def can_conclude_target_unavailable(self, observation: Observation) -> bool:
        """确认目录已渲染但当前目标语义不存在，供有界滚动收敛使用。"""
        if self.config.force_all_daily_tasks:
            return False
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

    def skip_unavailable_target(self) -> bool:
        """将已确认从当前任务目录移除的目标记入恢复状态并选择下一阶段。"""
        if self.config.force_all_daily_tasks:
            print("强制任务模式：不跳过当前任务，继续交给 VLM 重新定位")
            return False
        if self.target not in TASK_ORDER:
            return False
        target = self.target
        self.skipped_tasks.add(target)
        self._persist_task_state()
        self._select_next()
        print(f"当前福利任务目录未提供 {TASK_LABELS[target]}，已跳过旧任务入口")
        return True

    def _entry_cache_key(self) -> str | None:
        """返回当前可以复用的入口类型；未知阶段不使用坐标缓存。"""
        if self.phase == "open_target" and self.target in TASK_ENTRY_MARKERS:
            return self.target
        if (
            self.phase == "perform"
            and self.target == "same_template"
            and self.substate.get("entry_clicked")
            and not self.substate.get("template_clicked")
        ):
            return "same_template:template"
        return None

    @staticmethod
    def _usable_entry_node(node: Node) -> bool:
        """过滤邀请任务和活动页语义，避免把相邻卡片当成当前任务行。"""
        return bool(
            node.enabled
            and node_interaction_bounds(node).area > 0
            and "邀请新用户" not in node.searchable
            and not any(
                marker in node.searchable.lower() for marker in ACTIVITY_PAGE_MARKERS
            )
        )

    @staticmethod
    def _matches_entry_marker(node: Node, marker: str) -> bool:
        wanted = marker.lower()
        return any(field == marker for field in (node.text, node.content_desc, node.resource_id)) or wanted in node.searchable

    def _task_entry_point(self, observation: Observation) -> tuple[int, int, int] | None:
        """返回任务入口的右侧点击点和纵向容差。

        福利 WebView 的无障碍实现并不稳定：有时只暴露任务标题，有时暴露
        “去写作”等行动文案，还有时把整行标题作为一个宽大的 TextView。
        先用任务标题确定行，再把横坐标放到右侧行动列，能覆盖这三种层级。
        """
        if (
            self.phase != "open_target"
            or self.target not in TASK_ENTRY_MARKERS
            or observation.stage != "welfare"
            or not observation.nodes
        ):
            return None
        width = int(getattr(self.executor, "width", 0))
        height = int(getattr(self.executor, "height", 0))
        nodes = [node for node in observation.nodes if self._usable_entry_node(node)]
        if width <= 0:
            width = max((node_interaction_bounds(node).right for node in nodes), default=0)
        if height <= 0:
            height = max((node_interaction_bounds(node).bottom for node in nodes), default=0)
        if width <= 0 or height <= 0:
            return None
        action_markers = TASK_ENTRY_ACTION_MARKERS.get(self.target, ())
        row_markers = TASK_ENTRY_ROW_MARKERS.get(self.target, ())
        actions = [
            node
            for node in nodes
            if any(self._matches_entry_marker(node, marker) for marker in action_markers)
        ]
        row_candidates: list[tuple[int, Node]] = []
        max_row_height = 260
        for node in nodes:
            bounds = node_interaction_bounds(node)
            if bounds.height > max_row_height:
                continue
            for marker_index, marker in enumerate(row_markers):
                if self._matches_entry_marker(node, marker):
                    exact = any(
                        field == marker
                        for field in (node.text, node.content_desc, node.resource_id)
                    )
                    row_candidates.append((marker_index * 2 + (0 if exact else 1), node))
                    break

        row = min(
            row_candidates,
            key=lambda item: (
                item[0],
                node_interaction_bounds(item[1]).top,
                node_interaction_bounds(item[1]).area,
            ),
            default=(0, None),
        )[1]
        # 新版做同款没有独立的“做同款”文字按钮，任务入口就是推荐模板卡片。
        # 在已有任务行且未暴露行动文案时，优先返回首张可点击卡片中心；否则
        # 会把模型坐标校正到标题行右侧，连续点击同一个无效位置。
        if self.target == "same_template" and not actions and row is not None:
            row_bounds = node_interaction_bounds(row)
            cards = [
                node
                for node in nodes
                if node.clickable
                and node.enabled
                and node.bounds.area >= 30000
                and node.bounds.top >= row_bounds.bottom
            ]
            if cards:
                card = min(cards, key=lambda node: (node.bounds.top, node.bounds.left))
                return card.bounds.center[0], card.bounds.center[1], max(90, card.bounds.height // 3)
        action: Node | None = None
        if actions and row is not None:
            nearby = [
                node
                for node in actions
                if node is not row
                and abs(
                    node_interaction_bounds(node).center[1]
                    - node_interaction_bounds(row).center[1]
                )
                <= max(180, node_interaction_bounds(row).height + 80)
            ]
            if nearby:
                action = min(
                    nearby,
                    key=lambda node: (
                        abs(
                            node_interaction_bounds(node).center[1]
                            - node_interaction_bounds(row).center[1]
                        ),
                        -node_interaction_bounds(node).center[0],
                    ),
                )
        if action is None and actions and row is None:
            # “去提问”可能同时出现在顶部签到卡和任务列表；没有任务标题时
            # 选择屏幕中更靠下的行动项，避免点击顶部卡片。
            action = max(
                actions,
                key=lambda node: (
                    node_interaction_bounds(node).center[1],
                    node_interaction_bounds(node).center[0],
                ),
            )
        if action is None and row is None:
            return None

        anchor = action or row
        assert anchor is not None
        anchor_bounds = node_interaction_bounds(anchor)
        x, y = anchor_bounds.center
        action_bounds = node_interaction_bounds(action) if action is not None else None
        row_bounds = node_interaction_bounds(row) if row is not None else None
        # 只有右侧的小行动节点才直接使用其中心；整行节点的中心通常落在
        # x=450 左右，必须改用右侧行动列。没有行动节点时同样只借用行的 y。
        if (
            action is not None
            and action_bounds is not None
            and action_bounds.width <= round(width * 0.35)
            and action_bounds.center[0] >= round(width * 0.62)
        ):
            x = action_bounds.center[0]
        else:
            x = round(width * TASK_ENTRY_ACTION_X_RATIO)
        anchor_height = max(
            row_bounds.height if row_bounds is not None else 0,
            action_bounds.height if action_bounds is not None else 0,
        )
        tolerance = max(70, min(180, anchor_height // 2 + 55))
        return x, y, tolerance

    def _semantic_entry_action(
        self, observation: Observation
    ) -> tuple[str, dict[str, int]] | None:
        """完整无障碍层级可读时直接按任务语义找入口，失败再交给视觉模型。"""
        if (
            self.phase != "open_target"
            or self.target not in TASK_ENTRY_MARKERS
            or observation.stage != "welfare"
            or not observation.nodes
        ):
            return None
        point = self._task_entry_point(observation)
        if point is None:
            return None
        self.entry_scroll_attempts.pop(self.target, None)
        # 语义入口失败后不再无条件复用：同一坐标已失败时必须换入口，
        # 否则语义定位会反复给出同一个点不中的坐标，耗尽重试次数。
        cached = self._entry_cache_key()
        if cached is not None and cached in self.entry_points:
            return None
        x, y, _ = point
        print(f"使用无障碍语义定位任务入口：{self.target}")
        return "tap", {"x": x, "y": y}

    def _offscreen_entry_scroll_action(
        self, observation: Observation
    ) -> tuple[str, dict[str, int]] | None:
        """目标行明确存在但尚无屏幕边界时，有界滚动福利列表使其进入视口。"""
        if (
            self.phase != "open_target"
            or self.target not in TASK_ENTRY_ROW_MARKERS
            or observation.stage != "welfare"
            or not observation.nodes
        ):
            return None
        markers = TASK_ENTRY_ROW_MARKERS[self.target]
        if not any(
            node.bounds.area == 0
            and any(self._matches_entry_marker(node, marker) for marker in markers)
            for node in observation.nodes
        ):
            return None
        attempts = self.entry_scroll_attempts.get(self.target, 0)
        if attempts >= TASK_ENTRY_SCROLL_MAX_ATTEMPTS:
            return None
        width = int(getattr(self.executor, "width", 0))
        height = int(getattr(self.executor, "height", 0))
        if width <= 0 or height <= 0:
            return None
        self.entry_scroll_attempts[self.target] = attempts + 1
        print(f"福利任务行在视口外，滚动以显示入口：{self.target}（{attempts + 1}/{TASK_ENTRY_SCROLL_MAX_ATTEMPTS}）")
        start_ratio, end_ratio = (
            (0.38, 0.84) if self.target == "same_template" else (0.84, 0.38)
        )
        return "swipe", {
            "x1": width // 2,
            "y1": round(height * start_ratio),
            "x2": width // 2,
            "y2": round(height * end_ratio),
            "duration_ms": 500,
        }

    def _tap_matches_task_entry(self, args: dict[str, Any], observation: Observation) -> bool:
        point = self._task_entry_point(observation)
        if point is None:
            return False
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        target_x, target_y, tolerance = point
        width = int(getattr(self.executor, "width", 0))
        return (
            abs(x - target_x) <= max(55, round(width * 0.10))
            and abs(y - target_y) <= tolerance
        )

    def _correct_task_entry_tap(self, args: dict[str, Any], observation: Observation) -> bool:
        """用当前语义行校正视觉点击；无语义时完全交给 VLMM。"""
        point = self._task_entry_point(observation)
        if point is None:
            # 无障碍树没有目标语义时完全交给 VLMM，避免本地规则阻断页面改版。
            return True
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        target_x, target_y, tolerance = point
        width = int(getattr(self.executor, "width", 0))
        if abs(y - target_y) > tolerance:
            # 当前无障碍树已经给出了目标行，视觉模型可能受截图缩放影响把点
            # 落在相邻卡片；使用本轮语义行校正，而不是让旧布局规则阻断页面改版。
            args.update({"x": target_x, "y": target_y})
            print(f"模型点击偏离目标任务行，已按本轮语义重新定位：{self.target}")
            return True
        if abs(x - target_x) > max(55, round(width * 0.10)):
            args.update({"x": target_x, "y": target_y})
            print(f"模型点击落在任务行中部，已按本轮语义校正行动位置：{self.target}")
        return True

    def _correct_exchange_entry_tap(self, args: dict[str, Any], observation: Observation) -> bool:
        """校验兑换入口点击；空 WebView 层级完全交给截图视觉判断。"""
        if observation.stage not in {"welfare", "exchange"}:
            return False
        entry = self._welfare_exchange_entry_node(observation)
        if entry is None and observation.stage == "welfare" and not observation.nodes:
            return True
        if entry is None:
            return False
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        bounds = node_interaction_bounds(entry)
        if bounds.area <= 0:
            # 没有任何可见父级时只剩截图证据，完全交给 VLMM；不能用历史坐标
            # 猜测入口位置。
            return True
        if not (
            bounds.left - 70 <= x <= bounds.right + 70
            and bounds.top - 70 <= y <= bounds.bottom + 70
        ):
            # 父级区域只是当前页面的语义约束，不把父级中心当作固定入口坐标；
            # 视觉模型仍负责在本轮截图内选点，下一次观测会重新规划。
            return False
        return True

    def _remember_entry_point(self, name: str, args: dict[str, Any]) -> None:
        """保存一次已成功点击的入口，使用归一化坐标适配同一运行中的尺寸。"""
        if name != "tap":
            return
        key = self._entry_cache_key()
        if key is None:
            return
        try:
            x, y = int(args["x"]), int(args["y"])
            width, height = int(self.executor.width), int(self.executor.height)
        except (KeyError, TypeError, ValueError, AttributeError):
            return
        if width <= 0 or height <= 0 or not (0 <= x < width and 0 <= y < height):
            return
        self.entry_points[key] = (x / width, y / height)

    def _prime_entry_point(self, observation: Observation) -> None:
        """用刚完成的稳定福利层级预存当前任务入口，覆盖下一轮空树竞态。"""
        action = self._semantic_entry_action(observation)
        if action is None:
            return
        self._remember_entry_point(*action)
        key = self._entry_cache_key()
        if key is not None and key in self.entry_points:
            self.primed_entry_points.add(key)

    def cached_action(self, observation: Observation) -> tuple[str, dict[str, int]] | None:
        """在页面仍匹配时复用已验证入口，否则让视觉模型重新定位。"""
        if (
            self.phase == "open_target"
            and self.target == "same_template"
            and not self.same_template_revealed
            and observation.stage == "welfare"
            and not observation.nodes
        ):
            self.same_template_revealed = True
            self.entry_points.pop("same_template", None)
            width, height = int(self.executor.width), int(self.executor.height)
            if width > 0 and height > 0:
                # 做同款模板卡位于每日任务列表上方；从下方目录返回时向下滑动手指，
                # 让上方卡片重新进入视口。
                return "swipe", {
                    "x1": width // 2,
                    "y1": round(height * 0.38),
                    "x2": width // 2,
                    "y2": round(height * 0.84),
                    "duration_ms": 500,
                }
        if (
            self.phase == "perform"
            and self.target == "same_template"
            and self.substate.get("entry_clicked")
            and not self.substate.get("template_clicked")
        ):
            key = self._entry_cache_key()
            if key is not None and key in self.entry_points:
                width, height = int(self.executor.width), int(self.executor.height)
                if width > 0 and height > 0:
                    ratio_x, ratio_y = self.entry_points[key]
                    cached_args = {
                        "x": round(width * ratio_x),
                        "y": round(height * ratio_y),
                    }
                    if observation.stage == "app" and (
                        self._tap_hits_label(cached_args, observation, "做同款")
                        or self._tap_hits_template_card(cached_args, observation)
                    ):
                        return "tap", cached_args
                if observation.stage == "app":
                    self.entry_points.pop(key, None)
            button = find_node(observation.nodes, "做同款", exact=True)
            if button is None:
                button = find_node(observation.nodes, "做同款")
            if button is not None and button.bounds.area > 0:
                print("使用无障碍语义定位推荐模板按钮")
                return "tap", {"x": button.bounds.center[0], "y": button.bounds.center[1]}
            fallback = self._template_card_fallback(observation)
            if fallback is not None:
                print("使用首张可点击推荐模板卡片继续做同款任务")
                return "tap", {"x": fallback[0], "y": fallback[1]}
        semantic_action = self._semantic_entry_action(observation)
        if semantic_action is not None:
            return semantic_action
        scroll_action = self._offscreen_entry_scroll_action(observation)
        if scroll_action is not None:
            return scroll_action
        key = self._entry_cache_key()
        if key is None or key not in self.entry_points:
            return None
        width, height = int(self.executor.width), int(self.executor.height)
        if width <= 0 or height <= 0:
            return None
        ratio_x, ratio_y = self.entry_points[key]
        args = {"x": round(width * ratio_x), "y": round(height * ratio_y)}
        if key == "same_template:template":
            if observation.stage != "app":
                return None
            activity_lower = observation.activity.lower()
            if "aitemplatedetailactivity" in activity_lower:
                return None
            if not observation.nodes:
                if "home.v2" not in activity_lower and "webbrowseractivity" not in activity_lower:
                    return None
                return "tap", args
            if self._tap_hits_label(args, observation, "做同款"):
                return "tap", args
            if self._tap_hits_template_card(args, observation):
                return "tap", args
            return None
        if observation.stage != "welfare":
            return None
        if self._ignored_welfare_tap(args, observation):
            return None
        if not observation.nodes:
            # 只有 report_tasks 刚用完整层级确认过的坐标才允许跨过一次空树；
            # 普通历史缓存仍然禁止复用，避免福利页滚动后误点旧任务行。
            if key in self.primed_entry_points:
                self.primed_entry_points.discard(key)
                print(f"福利 WebView 层级暂空，沿用已确认的任务入口坐标：{key}")
                return "tap", args
            return None
        markers = TASK_ENTRY_MARKERS.get(key, ())
        if markers:
            # 优先要求任务专属标题；只有当前版本完全不暴露标题时，才退回通用按钮文字。
            specific = markers
            searchable = " ".join(node.searchable for node in observation.nodes)
            if any(marker.lower() in searchable for marker in specific):
                if self._tap_hits_label(args, observation, *specific) or self._tap_matches_task_entry(
                    args, observation
                ):
                    return "tap", args
                return None
            if self._tap_hits_label(args, observation, *markers) or self._tap_matches_task_entry(
                args, observation
            ):
                return "tap", args
        return None

    def invalidate_cached_action(self) -> None:
        """丢弃当前入口缓存，让下一轮重新请求视觉定位。"""
        key = self._entry_cache_key()
        if key == "same_template":
            self.fixed_entry_disabled.add(key)
        if key is not None:
            self.entry_points.pop(key, None)

    def expected_tool(self, observation: Observation) -> str | None:
        """返回本地状态已经确定的下一步工具，入口坐标仍交给模型定位。"""
        # 卡住恢复期间不强制任何工具，让 VLM 根据恢复提示自由决策
        # （tap/swipe/press_back/input/... 均可），这正是智能恢复的意义。
        if self.recovering:
            return None
        if (
            self.phase == "daily_ability_experience"
            and getattr(self.executor, "pending_generation", False)
        ):
            return "wait_5s"
        if (
            self.phase == "daily_ability_experience"
            and self.daily_ability_voice_submitted
            and observation.stage in {"chat", "app"}
        ):
            return "return_to_welfare"
        fixed_phases = {
            "daily_ability_wait": "wait_5s",
            "verify_daily_ability": "verify_daily_ability",
            "verify_daily_ability_wait": "wait_5s",
            "report": "report_tasks",
            # 报告后目标已经由本地状态确定；只允许模型在最新截图中找入口坐标。
            # 若保留 auto，视觉模型可能沿用上一子任务的 claim_reward，导致连续失败熔断。
            "open_target": "tap",
            "claim": "claim_reward",
            "return": "return_to_welfare",
            "open_exchange": (
                "report_exchange"
                if self.substate.get("entry_clicked")
                else "tap"
            ),
            "redeem": "redeem_qq_card",
            "exchange_confirm": "confirm_exchange",
            "prize_records": "open_prize_records",
            "use_reward": "use_bound_reward",
            "use_confirm": "confirm_reward_use",
            "return_home": "return_to_home",
            "complete": "complete_task",
        }
        if self.phase in fixed_phases:
            return fixed_phases[self.phase]
        if self.phase != "perform":
            return None

        kind = self._task_kind()
        if kind in TEXT_TASKS and not self.substate.get("input"):
            return "input_test_prompt"
        if kind in IMAGE_TASKS and not self.substate.get("image"):
            return "select_local_image"
        if kind == "photo_question" and not self.substate.get("confirmed"):
            return "confirm_image"
        if kind == "same_template" and not self.substate.get("template_clicked"):
            return "tap"
        if not self.substate.get("sent"):
            # 拍题确认后有些版本会自动提交；有发送按钮时仍必须显式发送。
            if kind == "photo_question" and self.substate.get("confirmed"):
                if self.executor._find_send(observation) is None:
                    return "wait_5s"
            return "send_message"
        if not self.substate.get("waited"):
            return "wait_5s"
        return None

    def _perform_complete(self) -> bool:
        kind = self._task_kind()
        required = TASK_STEPS.get(kind)
        return bool(required) and all(self.substate.get(name, False) for name in required)

    def _prompt_variant(self) -> str | None:
        """为文字任务生成可恢复的唯一轮次标记，避免元宝去重相同问题。"""
        if self.target not in TEXT_TASKS:
            return None
        count = self.expected_count or 1
        return f"{self.target.replace('_', '-')}-{count}"

    @staticmethod
    def _daily_ability_voice_press_point(
        observation: Observation,
    ) -> tuple[int, int] | None:
        """定位能力体验聊天页的“按住说话”控件。"""
        candidates = []
        for node in observation.nodes:
            if "按住" not in node.searchable or "说话" not in node.searchable:
                continue
            bounds = node.interaction_bounds
            if node.enabled and bounds.area > 0:
                candidates.append((bounds.area, bounds.center))
        return min(candidates, default=(0, None), key=lambda item: item[0])[1]

    def daily_ability_voice_action(
        self, observation: Observation
    ) -> tuple[str, dict[str, Any]] | None:
        """在能力体验明确要求语音输入时返回本地必做的长按动作。"""
        if (
            self.phase != "daily_ability_experience"
            or self.daily_ability_voice_submitted
            or getattr(self.executor, "pending_generation", False)
        ):
            return None
        point = self._daily_ability_voice_press_point(observation)
        if point is None:
            return None
        return "long_press", {
            "x": point[0],
            "y": point[1],
            "duration_ms": 2500,
            "_daily_ability_voice": True,
        }

    def _valid_phase(self, name: str) -> str | None:
        allowed: dict[str, set[str]] = {
            "navigate_welfare": {
                "open_welfare",
                "report_tasks",
                "return_to_welfare",
                "tap",
                "swipe",
                "press_back",
            },
            "report": {"report_tasks"},
            "daily_ability": DAILY_ABILITY_TOOLS,
            "daily_ability_experience": DAILY_ABILITY_EXPERIENCE_TOOLS,
            "daily_ability_wait": {"wait_5s"},
            "verify_daily_ability": {"verify_daily_ability"},
            "verify_daily_ability_wait": {"wait_5s"},
            "open_target": {"tap", "swipe", "press_back"},
            "perform": {
                "tap",
                "swipe",
                "press_back",
                "input_test_prompt",
                "send_message",
                "wait_5s",
                "select_local_image",
                "confirm_image",
            },
            "claim": {"claim_reward"},
            "return": {"return_to_welfare", "tap", "swipe", "press_back"},
            "open_exchange": {
                "open_exchange",
                "tap",
                "swipe",
                "press_back",
                "report_exchange",
            },
            "redeem": {"redeem_qq_card"},
            "exchange_confirm": {"confirm_exchange"},
            "prize_records": {"open_prize_records"},
            "use_reward": {"use_bound_reward"},
            "use_confirm": {"confirm_reward_use"},
            "return_home": {"return_to_home"},
            "complete": {"complete_task"},
        }
        if name not in allowed.get(self.phase, set()):
            return f"当前阶段 {self.phase} 不应调用 {name}"
        return None

    @staticmethod
    def _ignored_welfare_tap(args: dict[str, Any], observation: Observation) -> bool:
        """在福利中心拦截唯一明确忽略的邀请任务整行点击。"""
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        for node in observation.nodes:
            label = node.searchable
            if not any(marker in label for marker in ("邀请新用户", "去邀请")):
                continue
            if node.bounds.top - 55 <= y <= node.bounds.bottom + 55:
                return True
        return False

    @staticmethod
    def _tap_hits_label(
        args: dict[str, Any],
        observation: Observation,
        *markers: str,
        allow_empty_welfare: bool = False,
    ) -> bool:
        """判断模型点击是否落在当前观测中带指定语义的控件附近。"""
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        # 福利 WebView 偶发只返回空无障碍树；此时截图仍可交给视觉模型定位入口。
        if allow_empty_welfare and observation.stage == "welfare" and not observation.nodes:
            return True
        wanted = tuple(marker.lower() for marker in markers)
        for node in observation.nodes:
            if not node.enabled or node.bounds.area <= 0:
                continue
            if not any(marker in node.searchable for marker in wanted):
                continue
            if (
                node.bounds.left - 45 <= x <= node.bounds.right + 45
                and node.bounds.top - 45 <= y <= node.bounds.bottom + 45
            ):
                return True
        return False

    @staticmethod
    def _tap_hits_template_card(args: dict[str, Any], observation: Observation) -> bool:
        """推荐模板页文字尚未注入无障碍树时，只放行可点击卡片区域。"""
        try:
            x, y = int(args["x"]), int(args["y"])
        except (KeyError, TypeError, ValueError):
            return False
        # 模板 WebView 首次渲染常只有两张大卡片的 clickable 父 View；
        # 顶部分类和导航也可能是 clickable，但高度不足以成为模板卡片。
        for node in observation.nodes:
            bounds = node.bounds
            if (
                node.enabled
                and node.clickable
                and bounds.width >= 300
                and bounds.height >= 300
                and bounds.top >= 700
                and bounds.left <= x <= bounds.right
                and bounds.top <= y <= bounds.bottom
            ):
                return True
        return False

    def _template_card_fallback(self, observation: Observation) -> tuple[int, int] | None:
        """模型坐标无效时，从已确认的大型可点击卡片中取首张中心。"""
        activity_lower = observation.activity.lower()
        home_activity = PACKAGE_NAME in activity_lower and "home.v2" in activity_lower
        if observation.stage != "app" and not home_activity:
            return None
        candidates: list[Node] = []
        for node in observation.nodes:
            bounds = node.bounds
            if (
                node.enabled
                and node.clickable
                and bounds.width >= 300
                and bounds.height >= 300
                and bounds.top >= 700
                and bounds.area > 0
            ):
                candidates.append(node)
        if not candidates:
            # WebView 层级偶发为空，但 Activity 仍明确是元宝创建首页；
            # 该布局的首张推荐卡中心可按屏幕比例计算，未知页面不走此分支。
            if not home_activity:
                return None
            width = getattr(self, "width", getattr(self.executor, "width", 0))
            height = getattr(self, "height", getattr(self.executor, "height", 0))
            if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
                return None
            return round(width * 0.26), round(height * 0.78)
        card = min(candidates, key=lambda node: (node.bounds.top, node.bounds.left))
        return card.bounds.center

    def _complete_task_assertions(self) -> ToolResult:
        if (
            self.daily_ability_done is not True
            or self.daily_ability_evidence != DAILY_ABILITY_RULE_NAME
        ):
            return ToolResult(False, "主轮播卡尚未由页面状态确认‘已完成’")
        incomplete = [
            TASK_LABELS[key]
            for key in TASK_ORDER
            if key not in self.skipped_tasks and self.progress.get(key) != 3
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

    def _target_entry_ready(self, result: ToolResult) -> bool:
        """确认视觉点击已经到达当前任务页，而不是接受残留层级。"""
        evidence = result.data.get("entry_evidence")
        if not isinstance(evidence, dict):
            # 假执行器和旧兼容调用没有附带证据时保留原有行为；真实执行器
            # 总会提供 entry_evidence，因此不会把缺失证据当作成功。
            return result.data.get("stage") not in {
                "welfare",
                "activity",
                "exchange",
                "ours",
                "prize_records",
                "unknown",
            }
        stage = str(evidence.get("stage") or result.data.get("stage") or "")
        activity = str(evidence.get("activity") or result.data.get("activity") or "")
        activity_lower = activity.lower()
        if stage in {"welfare", "activity", "exchange", "ours", "prize_records", "unknown"}:
            return False
        target = self.target
        if target in TEXT_TASKS:
            # 福利 WebView 的旧树有时会残留聊天节点；输入框必须是当前截图
            # 观测到的可见控件，才能进入输入阶段。
            return bool(evidence.get("input"))
        if target == "image":
            # P 图入口的新版流程会直接打开系统图片选择器；选择图片是下一步
            # 本地业务动作，因此 picker 本身就是有效的目标页证据。
            return stage in {"image", "picker"} or bool(evidence.get("image"))
        if target == "photo_question":
            return stage in {"photo_question", "photo_preview", "picker"} or bool(
                evidence.get("photo_question")
            )
        if target == "same_template":
            if bool(evidence.get("template")):
                return True
            return stage == "app" and "webbrowseractivity" not in activity_lower
        return False

    def _after_successful_action(
        self, name: str, args: dict[str, Any], result: ToolResult
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
                self.entry_failures += 1
                stage = result.data.get("stage", "")
                return ToolResult(
                    False,
                    f"任务入口点击后未确认目标页（当前阶段={stage or '未知'}），请依据最新截图换策略",
                    {
                        "stage": stage,
                        "visual_replan": True,
                    },
                )
            self.entry_failures = 0
            self.phase = "perform"
            self.substate = {}
            if self.target == "same_template" and name == "tap":
                self.substate["entry_clicked"] = True
                activity = str(result.data.get("activity", "")).lower()
                if "aitemplatedetailactivity" in activity:
                    # 入口卡片直接打开模板详情时，模板已经被选中，下一步应发送。
                    self.substate["template_clicked"] = True
            return result
        # 只缓存已经由执行器接受的点击；随后阶段会改变，因此必须在状态转移前记录。
        self._remember_entry_point(name, args)
        if self.phase == "navigate_welfare" and (
            name == "open_welfare" or result.data.get("stage") == "welfare"
        ):
            if self.terminal_restore:
                self.phase = "return_home"
            elif self.daily_ability_done:
                self.phase = "report"
            else:
                self.phase = "daily_ability"
            # 恢复状态只能提供已完成进度，不能替代当前页面目录；启动视觉
            # 导航可能经历改版、任务移除或页面滚动，必须再次报告当前目录。
        elif self.phase == "daily_ability" and name in {
            "open_daily_ability",
            "continue_daily_ability",
        }:
            if (
                name == "open_daily_ability"
                and result.data.get("ability_card_visible") is False
            ):
                self.daily_ability_button_checks += 1
                if self.daily_ability_button_checks > DAILY_ABILITY_BUTTON_CHECK_LIMIT:
                    return ToolResult(
                        False,
                        "多次检查仍未确认主轮播卡‘去体验’按钮，未点击其它卡片",
                        {"stage": result.data.get("stage", ""), "visual_replan": True},
                    )
                self.phase = "daily_ability_wait"
                return result
            if result.data.get("ability_station_clicked") is not True:
                return ToolResult(
                    False,
                    "主轮播卡‘去体验’没有得到点击证据",
                    {"stage": result.data.get("stage", ""), "visual_replan": True},
                )
            self.daily_ability_click_attempts += 1
            self.daily_ability_button_checks = 0
            self.daily_ability_verification_checks = 0
            # 主卡点击证据成立后，体验可能仍在同一福利 WebView 的浮层中；先
            # 记录“体验已启动”，但完成状态仍必须等返回后的主卡“已完成”证据。
            self.daily_ability_experience_opened = name == "open_daily_ability" or (
                result.data.get("stage") != "welfare"
            )
            self.daily_ability_voice_submitted = False
            self.phase = "daily_ability_experience"
        elif self.phase == "verify_daily_ability" and name == "verify_daily_ability":
            card_status = result.data.get("card_status")
            if card_status == "已完成":
                self.daily_ability_done = True
                self.daily_ability_evidence = DAILY_ABILITY_RULE_NAME
                self._persist_task_state()
                self.phase = "report"
            elif card_status == "去体验":
                return ToolResult(
                    False,
                    "模型处理体验页面后主轮播卡仍显示‘去体验’，未将大任务记为完成",
                    {"stage": "welfare", "visual_replan": True},
                )
            else:
                self.daily_ability_verification_checks += 1
                if self.daily_ability_verification_checks > DAILY_ABILITY_VERIFY_CHECK_LIMIT:
                    return ToolResult(
                        False,
                        "未能读取主轮播卡状态并核验‘已完成’，未写入完成状态",
                        {"stage": "welfare", "visual_replan": True},
                    )
                self.phase = "verify_daily_ability_wait"
        elif self.phase == "daily_ability_experience":
            if name == "long_press" and args.get("_daily_ability_voice") is True:
                self.daily_ability_voice_submitted = True
            evidence = result.data.get("entry_evidence")
            experience_visible = isinstance(evidence, dict) and evidence.get(
                "daily_ability_experience"
            ) is True
            experience_visible = experience_visible or result.data.get(
                "daily_ability_experience"
            ) is True
            if getattr(self.executor, "pending_generation", False):
                self.daily_ability_experience_opened = True
            elif result.data.get("stage") != "welfare" or experience_visible:
                self.daily_ability_experience_opened = True
            elif self.daily_ability_experience_opened or name == "press_back":
                self.phase = "verify_daily_ability"
        elif self.phase == "daily_ability_wait" and name == "wait_5s":
            self.phase = "daily_ability"
        elif self.phase == "verify_daily_ability_wait" and name == "wait_5s":
            self.phase = "verify_daily_ability"
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

    def dispatch(self, name: str, args: dict[str, Any], observation: Observation) -> ToolResult:
        if (
            self.phase in {"report", "open_target", "open_exchange"}
            and observation.stage == "welfare"
        ):
            dismiss_reward = getattr(self.executor, "_dismiss_reward_popup", None)
            if callable(dismiss_reward) and dismiss_reward(observation):
                return ToolResult(
                    True,
                    "已收下延迟出现的任务奖励；本次业务动作未执行，请根据最新页面继续",
                    {"stage": "welfare", "reward_dismissed": True},
                )
        # 卡住恢复期间必须先允许模型处理遮挡层、返回键或重新定位；如果先套用
        # 正常阶段白名单，verify_daily_ability 会把恢复动作直接拦截，恢复机制
        # 实际上无法工作。
        if not self.recovering and self.phase == "daily_ability":
            if name not in DAILY_ABILITY_TOOLS:
                return ToolResult(False, "当前阶段只允许点击主轮播卡固定位置的‘去体验’")
        elif (
            not self.recovering
            and self.phase == "verify_daily_ability"
            and name != "verify_daily_ability"
        ):
            return ToolResult(False, "体验页面处理后必须先核验主轮播卡是否显示‘已完成’")
        # 卡住恢复期间 VLM 自由决策：跳过阶段白名单（恢复动作本就是打破常规的
        # 关弹窗/返回/重定位），只保留真正危险动作的人工确认逻辑。成败由外层
        # failures 计数判定，成功则退出恢复态回到正常流程。
        if self.recovering:
            if name == "report_tasks":
                result = self._parse_report(
                    self._merge_local_report(args, observation), observation
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
                    if fallback_observation is not None and fallback_observation.stage == "welfare":
                        result = ToolResult(
                            True,
                            "视觉恢复未触发入口，已按当前页面语义导航回福利中心",
                            {"stage": "welfare"},
                        )
            self.finish_recovery(f"{name} 执行成功")
            if name in {"report_tasks", "complete_task"}:
                return result
            return self._after_successful_action(name, args, result)
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
        if (
            self.phase == "open_exchange"
            and observation.stage not in {"welfare", "exchange"}
        ):
            # 兑换导航误触后可能落到“我们”页或其它活动页。先让 VLMM 按当前
            # 截图把页面带回福利中心；此时不能校验兑换入口坐标，也不能报告商城。
            if name == "report_exchange":
                return ToolResult(
                    False,
                    "当前不在兑换商城，必须先根据最新截图回到福利中心",
                    {"stage": observation.stage, "visual_replan": True},
                )
            if name in VISUAL_NAVIGATION_TOOLS:
                result = self.executor.execute(name, args)
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
        if self.phase == "open_exchange" and name == "report_exchange":
            return self._parse_exchange_report(args, observation)
        phase_error = self._valid_phase(name)
        if phase_error:
            return ToolResult(False, phase_error)
        if name == "continue_daily_ability":
            result = self._continue_daily_ability(args, observation)
            if not result.ok:
                return result
            return self._after_successful_action(name, args, result)
        if (
            self.phase == "daily_ability_experience"
            and name == "tap"
            and observation.stage == "welfare"
            and self._tap_hits_label(args, observation, "去体验")
        ):
            return ToolResult(
                False,
                "主卡‘去体验’已经点击；当前浮层仍覆盖它，请点击浮层自身的体验入口，不要重复点击背景卡片",
                {"stage": observation.stage, "visual_replan": True},
            )
        if self.phase == "daily_ability_experience" and name == "tap":
            corrected = self._correct_daily_ability_experience_tap(args, observation)
            if corrected is not None and corrected != (args.get("x"), args.get("y")):
                args.update({"x": corrected[0], "y": corrected[1]})
                print("体验入口坐标按当前无障碍按钮边界校正")
            prepare = getattr(
                self.executor, "prepare_daily_ability_experience_navigation", None
            )
            if callable(prepare):
                prepare()
        # 入口点击已确认打开福利活动页（站内活动 WebView）：这是已知误触，
        # 直接关回任务列表并作废当前入口，让视觉模型重新定位真正的入口。
        # 同一 phase/progress 下的重复短路仍会触发全局 stuck 保护并计入有界失败，
        # 不会无限循环；关不回时返回阶段信息，便于外层重试或上报。
        if (
            observation.stage == "activity"
            and self.phase != "daily_ability_experience"
            and name in {"tap", "swipe", "press_back", "input_test_prompt"}
        ):
            after = self.executor._close_activity_page(observation)
            self.fixed_entry_disabled.add(self.target or "")
            self.entry_points.pop(self.target or "", None)
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
        if name == "tap" and observation.stage == "welfare" and self._ignored_welfare_tap(args, observation):
            return ToolResult(False, "已拒绝点击“邀请新用户”任务", {"visual_replan": True})
        if name == "tap" and self.target == "same_template":
            if self.phase == "open_target" and not self._tap_hits_label(
                args, observation, "做同款", allow_empty_welfare=True
            ) and not self._tap_matches_task_entry(args, observation):
                # 新版推荐模板卡片本身就是做同款任务入口，允许按任务行语义
                # 校正到卡片中心，不要求存在独立“做同款”文字节点。
                if observation.stage == "welfare" and not observation.nodes:
                    pass
                else:
                    return ToolResult(
                        False,
                        "请点击福利中心中“做同款”任务入口或推荐模板卡片",
                        {"visual_replan": True},
                    )
            if self.phase == "open_target" and not self._tap_hits_label(
                args, observation, "做同款", allow_empty_welfare=True
            ):
                # WebView 的文字节点和图片节点经常分离，模型坐标可能落在同一
                # 行的图片上。已有明确“做同款”文字时直接校正到文字中心，避免
                # 把可确定的定位误判成模型失败。
                entry_button = find_node(observation.nodes, "做同款", exact=True)
                if entry_button is None:
                    entry_button = find_node(observation.nodes, "做同款")
                if entry_button is not None:
                    args.update(
                        {"x": entry_button.bounds.center[0], "y": entry_button.bounds.center[1]}
                    )
                elif not (
                    (observation.stage == "welfare" and not observation.nodes)
                    or self._tap_matches_task_entry(args, observation)
                ):
                    return ToolResult(
                        False,
                        "请点击福利中心中“做同款”任务入口"
                        f"（阶段={observation.stage or '未知'}，Activity={observation.activity[:100] or '未知'}）",
                        {"visual_replan": True},
                    )
            if self.phase == "perform":
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
                elif not self._tap_hits_label(args, observation, "做同款"):
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
                    elif not (
                        observation.stage == "app"
                        and self._tap_hits_template_card(args, observation)
                    ):
                        fallback = self._template_card_fallback(observation)
                        if fallback is None:
                            return ToolResult(False, "请在推荐模板页点击任意一个“做同款”按钮")
                        # 元宝新版把“做同款”文字放在 WebView 卡片的图片层，
                        # 无效坐标无法通过无障碍文字校验；只在大型卡片已确认时取首张中心。
                        args.update({"x": fallback[0], "y": fallback[1]})
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
        if name == "report_tasks":
            result = self._parse_report(
                self._merge_local_report(args, observation), observation
            )
            if result.ok:
                self._prime_entry_point(observation)
            return result
        if name == "verify_daily_ability":
            result = self._parse_daily_ability_verification(args, observation)
            if not result.ok:
                return result
            return self._after_successful_action(name, args, result)
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
        return self._after_successful_action(name, args, result)
