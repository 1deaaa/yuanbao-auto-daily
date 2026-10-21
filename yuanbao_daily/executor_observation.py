"""观测层：截图、无障碍树、页面阶段与系统弹窗处理。"""

from __future__ import annotations

from typing import Any
from typing import Any, Iterable
from dataclasses import dataclass, field, replace
import time

from .constants import (
    DAILY_ABILITY_VERIFY_PREPARE_TIMEOUT,
    PACKAGE_NAME,
    STARTUP_OBSERVE_POLL_INTERVAL,
    STARTUP_OBSERVE_TIMEOUT,
    UI_DUMP_ATTEMPTS,
    UI_DUMP_TIMEOUT,
    WELFARE_ACTIVITY_MARKERS,
    WEBVIEW_UI_DUMP_ATTEMPTS,
    WEBVIEW_UI_DUMP_RETRY_DELAY,
    WEBVIEW_UI_DUMP_TIMEOUT,
    WELFARE_TASK_CATALOG_SCROLL_MAX_ATTEMPTS,
    WELFARE_TASK_LIST_SCROLL_END_RATIO,
    WELFARE_TASK_LIST_SCROLL_START_RATIO,
)
from .errors import (
    AgentError,
    AppUnresponsiveError,
    ManualActionRequired,
)
from .models import (
    Node,
    Observation,
    ToolResult,
)
from .ui import (
    compact_ui,
    find_node,
    node_interaction_bounds,
    parse_nodes,
)
from .welfare import (
    daily_ability_card_status,
    detect_stage,
    has_complete_new_welfare_task_catalog,
    is_daily_ability_experience_visible,
    is_new_welfare_page,
    parse_local_welfare_progress,
)


class ExecutorObservationMixin:
    """按职责拆分的混入类；方法实现与原主类完全一致。"""

    def observe(self) -> Observation:
        # Waydroid/Android 某些实例首次启动应用时会弹出系统兼容性提示；
        # 该提示不是业务状态，若不关闭会遮住底部导航并阻断固定的首步导航。
        for _ in range(2):
            # WebView 页面持续重绘时，uiautomator 可能一直等不到 idle state。
            # 福利页探针给足完成一次压缩树 dump 的时间；仍失败时保留截图回退，
            # 不能把页面渲染竞态误判成 ADB 故障。
            activity = self.device.activity()
            webview = any(
                marker in activity.lower() for marker in WELFARE_ACTIVITY_MARKERS
            )
            ui_xml = ""
            nodes: tuple[Node, ...] = ()
            probe_count = (
                2
                if webview and getattr(self, "_welfare_context", False)
                else 1
            )
            for probe in range(probe_count):
                try:
                    ui_xml = self.device.ui_dump(
                        attempts=WEBVIEW_UI_DUMP_ATTEMPTS if webview else UI_DUMP_ATTEMPTS,
                        timeout=WEBVIEW_UI_DUMP_TIMEOUT if webview else UI_DUMP_TIMEOUT,
                    )
                except AgentError:
                    if not webview:
                        raise
                    if probe + 1 < probe_count:
                        print("福利 WebView 无障碍树暂不可用，等待后重试")
                        time.sleep(WEBVIEW_UI_DUMP_RETRY_DELAY)
                        continue
                    print("福利 WebView 无障碍树未及时空闲，使用截图观测")
                    image = self.device.screenshot()
                    return Observation(image, "", "", (), activity, "welfare")
                nodes = tuple(parse_nodes(ui_xml))
                if not (
                    webview
                    and getattr(self, "_welfare_context", False)
                    and not any(node.text.strip() for node in nodes)
                ):
                    break
                self.normalize_welfare_scroll()
                if probe + 1 < probe_count:
                    print("福利 WebView 空层级，等待任务目录渲染后重试")
                    time.sleep(WEBVIEW_UI_DUMP_RETRY_DELAY)
                    continue
                image = self.device.screenshot()
                self._welfare_context = True
                return Observation(image, ui_xml, "", (), activity, "welfare")
            if self._dismiss_android_warning(nodes):
                continue
            if self._dismiss_upgrade_prompt(nodes):
                continue
            if self._dismiss_protocol_prompt(nodes, activity):
                continue
            # dump 可能等待数秒；截图放在 dump 之后，避免把旧页面图片和新层级
            # 拼进同一次视觉请求，尤其是福利页返回后的 WebView 重绘阶段。
            image = self.device.screenshot()
            stage = detect_stage(nodes, activity)
            # WebView 返回福利中心后，Android 无障碍桥偶尔仍暴露上一页的聊天
            # 节点；截图已经是当前页面，不能让这棵滞后树把福利页误报为 app/chat。
            # 仅在本轮此前已确认福利上下文且 Activity 仍是 WebView 时启用，
            # 具体入口位置和页面变化仍由 VLMM 根据最新截图判断。
            if (
                webview
                and getattr(self, "_welfare_context", False)
                and stage in {"app", "chat", "unknown"}
            ):
                stage = "welfare"
            if stage == "welfare":
                self._welfare_context = True
            return Observation(image, ui_xml, compact_ui(ui_xml), nodes, activity, stage)
        image = self.device.screenshot()
        activity = self.device.activity()
        return Observation(image, ui_xml, compact_ui(ui_xml), nodes, activity, detect_stage(nodes, activity))


    def observe_startup(
        self,
        timeout: float = STARTUP_OBSERVE_TIMEOUT,
        poll_interval: float = STARTUP_OBSERVE_POLL_INTERVAL,
    ) -> Observation:
        """等待首屏稳定后再交给工作流，避免把过渡层或旧层级当成业务页面。"""
        deadline = time.monotonic() + timeout
        last_error: AgentError | None = None
        while time.monotonic() < deadline:
            try:
                check_responsive = getattr(self.device, "app_not_responding", None)
                if callable(check_responsive) and check_responsive(PACKAGE_NAME):
                    raise AppUnresponsiveError("元宝首屏进程无响应（ANR）")
                observation = self.observe()
                if callable(check_responsive) and check_responsive(PACKAGE_NAME):
                    raise AppUnresponsiveError("元宝首屏点击后进程无响应（ANR）")
            except ManualActionRequired:
                raise
            except AppUnresponsiveError:
                raise
            except AgentError as exc:
                last_error = exc
                time.sleep(poll_interval)
                continue

            blocker = self._manual_blocker(observation)
            if blocker is not None:
                # 协议页已尝试自动确认；若页面短暂未切换，继续有界重试，
                # 只有超时后才按启动失败交给会话恢复逻辑。
                if (
                    "协议" in blocker
                    and getattr(self.config, "auto_accept_protocol", False)
                ):
                    last_error = AgentError(blocker)
                    time.sleep(poll_interval)
                    continue
                raise ManualActionRequired(
                    f"{blocker}（Activity={observation.activity[:120] or '未知'}）"
                )
            return observation
        detail = str(last_error)[:180] if last_error else "首屏没有稳定业务页面"
        raise AgentError(f"首屏观测未完成（等待 {timeout:.0f} 秒；{detail}）")


    def _dismiss_android_warning(self, nodes: Iterable[Node]) -> bool:
        """只关闭已知的 Android 系统兼容性提示，不替模型处理业务弹窗。"""
        warning = find_node(nodes, "您的设备内部出现了问题")
        if warning is None:
            return False
        button = find_node(nodes, "确定", clickable_only=True, exact=True)
        if button is None:
            return False
        self._tap_node(button)
        time.sleep(0.5)
        return True


    def _dismiss_upgrade_prompt(self, nodes: Iterable[Node]) -> bool:
        """关闭元宝冷启动时的版本更新遮罩，避免遮住底部导航。"""
        dialog = find_node(nodes, f"{PACKAGE_NAME}:id/upgrade_dialog")
        title = find_node(nodes, "元宝新版本", exact=True)
        if dialog is None and title is None:
            return False
        skip = find_node(nodes, f"{PACKAGE_NAME}:id/skip", clickable_only=True)
        if skip is None:
            return False
        self._tap_node(skip)
        time.sleep(0.8)
        return True


    @staticmethod
    def _is_protocol_prompt(nodes: Iterable[Node], activity: str) -> bool:
        """只识别元宝首次协议页，不把普通登录页的隐私链接当成协议弹窗。"""
        if "hyloginmainactivity" not in activity.lower():
            return False
        return find_node(nodes, "同意并继续", exact=True) is not None


    def _dismiss_protocol_prompt(self, nodes: Iterable[Node], activity: str) -> bool:
        """精确自动确认元宝协议；按钮缺失时交由人工阻塞保护处理。"""
        if not getattr(self.config, "auto_accept_protocol", False):
            return False
        if not self._is_protocol_prompt(nodes, activity):
            return False
        # Compose 常把 clickable 标记放在无文字的父 View 上，文字节点本身为不可点击；
        # 精确命中文案后点击其中心仍落在父按钮范围内，不能因此把协议页误判为人工阻塞。
        button = find_node(nodes, "同意并继续", clickable_only=True, exact=True)
        if button is None:
            button = find_node(nodes, "同意并继续", exact=True)
        if button is None:
            return False
        self._tap_node(button)
        time.sleep(1.0)
        return True


    @staticmethod
    def _manual_blocker(observation: Observation) -> str | None:
        """识别必须由用户处理的协议或登录页面，避免无意义地重启重试。"""
        labels = " ".join(
            f"{node.text} {node.content_desc}" for node in observation.nodes
        ).lower()
        if ExecutorObservationMixin._is_protocol_prompt(observation.nodes, observation.activity):
            return "元宝协议页未能自动确认，请先人工点击“同意并继续”"
        if any(
            marker in labels
            for marker in ("手机号登录", "微信登录", "qq登录", "验证码登录", "获取验证码")
        ):
            return "元宝当前未登录，请先人工完成手机号、微信或 QQ 登录"
        if any(marker in labels for marker in ("应用无响应", "application not responding")):
            return "元宝出现应用无响应窗口，请先关闭该窗口并检查应用状态"
        return None


    def _result(self, ok: bool, message: str, **data: Any) -> ToolResult:
        return ToolResult(ok, message, data)


    @staticmethod
    def _foreground_label_covering_tap(
        observation: Observation, x: int, y: int, width: int, height: int
    ) -> str | None:
        """识别被前景交互层覆盖的文字目标，让模型先处理遮挡层。

        Android 的无障碍树可能同时保留底层回答卡片和前景 Compose 弹层。两者
        的文字边界会重叠，直接按最小可点击父节点取中心反而可能选中弹层中的
        另一个选项。这里仅在同一点存在多个不同文字、且存在多个重叠交互容器
        时暂停点击；具体关闭方式仍由 VLMM 根据截图选择。
        """
        screen_area = max(1, width * height)
        labels = [
            node
            for node in observation.nodes
            if node.enabled
            and node.bounds.area > 0
            and (node.text.strip() or node.content_desc.strip())
            and node.bounds.left <= x <= node.bounds.right
            and node.bounds.top <= y <= node.bounds.bottom
        ]
        distinct = []
        for node in labels:
            value = node.searchable.strip()
            if value and value not in distinct:
                distinct.append(value)
        if len(distinct) < 2:
            return None
        containers = [
            node
            for node in observation.nodes
            if node.enabled
            and node.clickable
            and node.bounds.area > 0
            and node.bounds.area <= round(screen_area * 0.55)
            and node.bounds.left <= x <= node.bounds.right
            and node.bounds.top <= y <= node.bounds.bottom
        ]
        if len(containers) < 2:
            return None
        # 层级中较晚出现的文字通常属于前景 Compose 弹层；只返回语义，
        # 不在执行层猜测应该点击哪个弹层按钮。
        for node in reversed(labels):
            value = node.searchable.strip()
            if value and value != distinct[0]:
                return value[:80]
        return None


    def _resolve_tap_point(
        self, observation: Observation, x: int, y: int
    ) -> tuple[int, int]:
        """将文字节点上的视觉点击提升到当前层级的可点击父控件。

        Compose/WebView 经常把文字和点击手势拆成两个节点。视觉模型给出的
        点通常已经语义正确，但直接点文字中心可能被标题栏或装饰层吞掉；只
        在当前点确实落在一个非可点击文字节点内时做父控件提升，不凭历史布局
        猜测其它位置，也不改变模型对页面目标的判断。
        """
        if not observation.nodes:
            return x, y
        labels = [
            node
            for node in observation.nodes
            if node.enabled
            and not node.clickable
            and node.bounds.area > 0
            and (node.text.strip() or node.content_desc.strip())
            and node.bounds.left <= x <= node.bounds.right
            and node.bounds.top <= y <= node.bounds.bottom
        ]
        if not labels:
            return x, y
        label = min(labels, key=lambda node: node.bounds.area)
        screen_area = max(1, self.width * self.height)
        parents = [
            node
            for node in observation.nodes
            if node.enabled
            and node.clickable
            and node.bounds.area >= label.bounds.area
            and node.bounds.area <= round(screen_area * 0.55)
            and node.bounds.left <= x <= node.bounds.right
            and node.bounds.top <= y <= node.bounds.bottom
        ]
        if not parents:
            return x, y
        parent = min(parents, key=lambda node: node.bounds.area)
        return parent.bounds.center


    def _collapse_expanded_input(self, observation: Observation) -> bool:
        """收起聊天页的全屏输入浮层，避免它吞掉底部导航点击。"""
        collapse = find_node(observation.nodes, "收起输入框", exact=True)
        if collapse is None:
            # 新版写作页的顶部“快速回答/模型”选择器也会复用
            # session_input_arrow_down 资源 ID，但它位于左上角，点击会反复打开
            # 模型菜单，永远无法进入下面的键盘收起和底部导航。资源 ID 只能作为
            # 位置受限的后备证据；真正的输入浮层收起按钮位于右侧，或直接暴露
            # “收起输入框”语义。
            candidates = [
                node
                for node in observation.nodes
                if node.enabled
                and node.clickable
                and node.bounds.area > 0
                and node.resource_id.endswith("session_input_arrow_down")
                and node.bounds.center[0] >= self.width * 0.65
            ]
            collapse = min(candidates, key=lambda node: node.bounds.area, default=None)
        if collapse is None:
            return False
        self._tap_node(collapse)
        return True


    def _hide_soft_keyboard(self) -> bool:
        """软键盘会盖住底部导航并吞掉“我们”的点击；显示时先按返回键收起。"""
        checker = getattr(self.device, "keyboard_shown", None)
        if not callable(checker) or not checker():
            return False
        self.device.input("keyevent", "KEYCODE_BACK")
        return True


    def _dismiss_chat_attachment_sheet(self, observation: Observation) -> bool:
        """仅当聊天页附件面板的多个专属入口同时可见时收起该面板。"""
        if observation.stage not in {"chat", "app"}:
            return False
        visible_text = " ".join(
            node.searchable
            for node in observation.nodes
            if node.enabled and node.bounds.area > 0
        )
        markers = ("进入相册", "联网搜索", "添加文件")
        if sum(marker in visible_text for marker in markers) < 2:
            return False
        self.device.input("keyevent", "KEYCODE_BACK")
        time.sleep(0.4)
        print("已收起聊天页附件面板，继续福利中心导航")
        return True


    def _entry_evidence(self, observation: Observation) -> dict[str, Any]:
        """提取点击后可验证的页面证据，防止残留层级推进错误阶段。"""
        visible_text = " ".join(
            node.searchable for node in observation.nodes if node.bounds.area > 0
        )
        return {
            "activity": observation.activity,
            "stage": observation.stage,
            "input": self._find_input(observation) is not None,
            "send": self._find_send(observation) is not None,
            "image": any(
                marker in visible_text
                for marker in ("智能p图", "上传图片", "选择图片", "图片编辑")
            ),
            "photo_question": any(
                marker in visible_text
                for marker in ("拍题", "拍照答题", "相册答题", "一次框选一道题")
            ),
            "template": any(
                marker in visible_text for marker in ("做同款", "推荐模板", "同款")
            )
            or any(
                node.enabled
                and node.clickable
                and node.bounds.width >= 300
                and node.bounds.height >= 300
                for node in observation.nodes
            ),
            "daily_ability_experience": is_daily_ability_experience_visible(observation),
        }


    def normalize_welfare_scroll(self) -> None:
        """仅在福利 WebView 明确需要回到顶部时调用；调用方必须已有成功 dump。

        注意：uiautomator dump 超时本身不代表滚动位置错误。旧代码在 dump
        超时也做标准化滑动，把任务列表推离顶部，反而造成后续入口定位漂移。
        dump 失败时只截图返回，不做任何滑动。
        """
        if getattr(self, "_welfare_scroll_normalized", False):
            return
        self.device.input(
            "swipe",
            str(self.width // 2),
            str(round(self.height * 0.38)),
            str(self.width // 2),
            str(round(self.height * 0.86)),
            "500",
        )
        time.sleep(1.2)
        self._welfare_scroll_normalized = True
        print("福利 WebView 空层级，已将任务列表滚动位置标准化")


    def prepare_daily_ability_card(self, observation: Observation) -> Observation:
        """读主轮播卡状态前，把福利页滚回顶部并等待状态可读。

        福利页会记住滚动位置：停在每日任务列表时主轮播卡不在视口，WebView
        刚返回时又常有几秒空层级。此时直接点击或核验只会得到“状态不可见”，
        模型反复空转也无法推进。
        """
        if observation.stage != "welfare":
            return observation
        deadline = time.monotonic() + DAILY_ABILITY_VERIFY_PREPARE_TIMEOUT
        while time.monotonic() < deadline:
            if daily_ability_card_status(observation) in {"已完成", "去体验"}:
                return observation
            if observation.nodes:
                # 层级可读但主卡不在视口：滚回顶部再读一次。
                self.reset_welfare_scroll()
                self.normalize_welfare_scroll()
                observation = self.observe()
                continue
            time.sleep(1.0)
            observation = self.observe()
        return observation

    def reset_welfare_scroll(self) -> None:
        """福利页每次重新打开后都重新校准滚动位置。"""
        self._welfare_scroll_normalized = False
        self._welfare_task_catalog_scrolled = False
        supplemental = getattr(self, "_welfare_supplemental_task_progress", None)
        if isinstance(supplemental, dict):
            supplemental.clear()


    def prepare_welfare_task_report(self, observation: Observation) -> Observation:
        """报告前确认新版每日任务目录，并保留滚出视口的做同款进度。"""
        if observation.stage != "welfare":
            return observation
        if not isinstance(
            getattr(self, "_welfare_supplemental_task_progress", None), dict
        ):
            self._welfare_supplemental_task_progress = {}
        local_report = parse_local_welfare_progress(observation)
        if local_report is not None and isinstance(local_report.get("same_template"), int):
            self._welfare_supplemental_task_progress["same_template"] = local_report[
                "same_template"
            ]

        def attach_supplemental_progress(current: Observation) -> Observation:
            if current.stage != "welfare":
                return current
            current_report = parse_local_welfare_progress(current)
            if current_report is not None and isinstance(
                current_report.get("same_template"), int
            ):
                self._welfare_supplemental_task_progress["same_template"] = (
                    current_report["same_template"]
                )
            if not self._welfare_supplemental_task_progress:
                return current
            supplemental = tuple(
                sorted(self._welfare_supplemental_task_progress.items())
            )
            if current.supplemental_task_progress == supplemental:
                return current
            return replace(
                current,
                supplemental_task_progress=supplemental,
            )

        observation = attach_supplemental_progress(observation)
        new_welfare_page = is_new_welfare_page(observation)

        def reveal_same_template_progress(current: Observation) -> Observation:
            """补一次滚动以读到每日任务区末尾的“做同款”行。

            “做同款”排在写作、拍题、P图之后，通常还在屏幕下方；报告看不到它的
            真实计数时只能按本地缓存判断，可能把实际没做的任务当成已完成。
            """
            if "same_template" in self._welfare_supplemental_task_progress:
                return current
            self.device.input(
                "swipe",
                str(self.width // 2),
                str(round(self.height * WELFARE_TASK_LIST_SCROLL_START_RATIO)),
                str(self.width // 2),
                str(round(self.height * WELFARE_TASK_LIST_SCROLL_END_RATIO)),
                "500",
            )
            time.sleep(1)
            return attach_supplemental_progress(self.observe())

        if new_welfare_page:
            if has_complete_new_welfare_task_catalog(observation, local_report):
                self._welfare_task_catalog_scrolled = True
                return reveal_same_template_progress(observation)
            if (
                not observation.nodes
                and getattr(self, "_welfare_task_catalog_scrolled", False)
            ):
                return observation
        elif (
            local_report is not None
            or getattr(self, "_welfare_task_catalog_scrolled", False)
        ):
            return observation

        if not getattr(self, "_welfare_scroll_normalized", False):
            self.normalize_welfare_scroll()
        for attempt in range(WELFARE_TASK_CATALOG_SCROLL_MAX_ATTEMPTS):
            self.device.input(
                "swipe",
                str(self.width // 2),
                str(round(self.height * WELFARE_TASK_LIST_SCROLL_START_RATIO)),
                str(self.width // 2),
                str(round(self.height * WELFARE_TASK_LIST_SCROLL_END_RATIO)),
                "500",
            )
            time.sleep(1)
            observation = attach_supplemental_progress(self.observe())
            local_report = parse_local_welfare_progress(observation)
            if new_welfare_page:
                if has_complete_new_welfare_task_catalog(observation, local_report):
                    self._welfare_task_catalog_scrolled = True
                    return reveal_same_template_progress(observation)
                if not observation.nodes:
                    # 层级为空时，只有完成目录滚动后才允许模型读取当前截图。
                    self._welfare_task_catalog_scrolled = True
                    return observation
            else:
                self._welfare_task_catalog_scrolled = True
                return observation
            if observation.stage != "welfare":
                raise AgentError("滚动福利任务目录时离开了福利中心")
            print(
                "新版福利每日任务目录尚不完整，继续滚动定位"
                f"（{attempt + 1}/{WELFARE_TASK_CATALOG_SCROLL_MAX_ATTEMPTS}）"
            )
        raise AgentError("滚动后仍无法从福利页面读取写作、拍题和P图三项每日任务")


    def _tap_node(self, node: Node) -> None:
        x, y = node_interaction_bounds(node).center
        self._tap_point(x, y)


    def _tap_point(self, x: int, y: int) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise AgentError(f"坐标越界：{x},{y}")
        self.device.input("tap", str(x), str(y))
