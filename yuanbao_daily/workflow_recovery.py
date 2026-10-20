"""恢复层：卡住恢复提示、语义救援动作与阶段白名单。"""

from __future__ import annotations

from typing import Any

from .constants import (
    DAILY_ABILITY_EXPERIENCE_TOOLS,
    DAILY_ABILITY_TOOLS,
    IMAGE_TASKS,
    PACKAGE_NAME,
    TASK_ENTRY_MARKERS,
    TASK_LABELS,
    TASK_ORDER,
    TASK_STEPS,
    TEXT_TASKS,
)
from .models import (
    Node,
    Observation,
    ToolResult,
)
from .ui import (
    find_node,
    find_voice_press_point,
    node_interaction_bounds,
)


class WorkflowRecoveryMixin:
    """按职责拆分的混入类；方法实现与原主类完全一致。"""

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
        """记录一次 VLM 自由决策恢复尝试。

        同一入口已连续失败，恢复时必须让 VLM 按最新截图重新定位；否则恢复
        动作会复用同一个坏坐标，再次失败。
        """
        self.recovering = True
        self.recover_phase = self.phase
        self.recover_target = self.target
        self.recover_attempts += 1


    def finish_recovery(self, reason: str) -> None:
        """恢复动作执行后即退出恢复态；成败由后续步骤的 failures 计数判定。"""
        self.recovering = False
        print(f"卡住恢复动作已执行（{reason}），回到正常流程观察效果")


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


    def requires_visual_navigation(self) -> bool:
        """所有易变页面入口都由 VLMM 按当前截图导航，避免复用旧坐标。"""
        return self.phase in {"navigate_welfare", "open_target", "open_exchange"} or (
            self.phase == "perform"
            and self.target == "same_template"
            and self.substate.get("entry_clicked")
            and not self.substate.get("template_clicked")
        )


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
        # 做同款直接提交模板自带的内容：模板详情页已经填好了模板提示词和
        # 参考图，改写输入框会丢掉模板信息，福利页的“做同款 x/3”不会计数。
        if not self.substate.get("sent"):
            # 拍题确认后有些版本会自动提交；有发送按钮时仍必须显式发送。
            if kind == "photo_question" and self.substate.get("confirmed"):
                if self.executor._find_send(observation) is None:
                    return "wait_5s"
            return "send_message"
        if not self.substate.get("waited"):
            return "wait_5s"
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


    def daily_ability_voice_action(
        self, observation: Observation
    ) -> tuple[str, dict[str, Any]] | None:
        """在能力体验明确要求语音输入时返回本地必做的语音动作。

        这里只表达“按住说话”的意图，不计算坐标：控件位置由执行层按当前
        无障碍语义定位并注入短测试语音，避免编排层与执行层各写一套定位规则。
        """
        if (
            self.phase != "daily_ability_experience"
            or self.daily_ability_voice_submitted
            or getattr(self.executor, "pending_generation", False)
        ):
            return None
        if find_voice_press_point(observation.nodes) is None:
            return None
        return "hold_to_speak", {"duration_ms": 2500}


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
