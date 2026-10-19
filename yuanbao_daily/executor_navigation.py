"""导航层：聊天首页、抽屉、福利入口与返回路径。"""

from __future__ import annotations

import time

from .constants import (
    ENTRY_NAVIGATION_POLL_INTERVAL,
    ENTRY_NAVIGATION_TIMEOUT,
    OURS_FALLBACK_DELAY,
    OURS_FALLBACK_X_RATIO,
    OURS_FALLBACK_Y_RATIO,
    OURS_NAV_POLL_INTERVAL,
    OURS_NAV_TIMEOUT,
    PACKAGE_NAME,
    WELFARE_ENTRY_FALLBACK_X_RATIO,
    WELFARE_ENTRY_FALLBACK_Y_RATIO,
    WELFARE_LOAD_POLL_INTERVAL,
    WELFARE_LOAD_TIMEOUT,
    WELFARE_RETURN_LINK_FALLBACK_DELAY,
    WELFARE_WEBVIEW_READY_DELAY,
)
from .errors import (
    AgentError,
    ManualActionRequired,
)
from .models import (
    Bounds,
    Node,
    Observation,
)
from .ui import (
    find_node,
    node_interaction_bounds,
)


class ExecutorNavigationMixin:
    """按职责拆分的混入类；方法实现与原主类完全一致。"""

    def _navigate_to_ours_until(self, deadline: float) -> Observation:
        """兼容旧版“我们”页；新版聊天首页是等价的有效落点。"""
        back_sent = False
        last_back_elapsed: float | None = None
        fallback_tapped = False
        ours_tap_attempts = 0
        last_ours_node: Node | None = None
        last_observation: Observation | None = None
        last_error: AgentError | None = None
        while time.monotonic() < deadline:
            try:
                observation = self.observe()
            except ManualActionRequired:
                raise
            except AgentError as exc:
                last_error = exc
                time.sleep(OURS_NAV_POLL_INTERVAL)
                continue
            last_observation = observation
            blocker = self._manual_blocker(observation)
            if blocker is not None:
                raise ManualActionRequired(f"{blocker}（Activity={observation.activity[:120] or '未知'}）")
            if self._is_new_chat_home(observation):
                return observation
            if observation.stage == "ours":
                return observation
            # 新版聊天页发送后可能停留在全屏展开输入浮层，该浮层会吞掉底部
            # “我们”的点击；先收起输入框再继续导航。
            if self._collapse_expanded_input(observation):
                time.sleep(1.0)
                continue
            ours_node = find_node(observation.nodes, "我们", exact=True)
            if ours_node is not None:
                last_ours_node = ours_node
            navigation_node = ours_node or (
                last_ours_node
                if observation.stage == "chat"
                and PACKAGE_NAME in observation.activity.lower()
                else None
            )
            if navigation_node is not None:
                # 软键盘会盖住底部导航，点击“我们”会落在键盘上被吞掉；
                # 确认键盘显示时先按返回键收起，再在下一轮点击。
                if self._hide_soft_keyboard():
                    ours_tap_attempts = 0
                    time.sleep(1.0)
                    continue
                # Compose 版本的图标节点可能不标记 clickable，第一次中心点击
                # 没有切换页面时，第二次点击当前层级暴露的底部可点击区域。
                if ours_tap_attempts == 0:
                    self._tap_node(navigation_node)
                else:
                    x, y = self._bottom_navigation_tap_point(
                        observation, navigation_node, self.width, self.height
                    )
                    self._tap_point(x, y)
                ours_tap_attempts += 1
                time.sleep(1.5)
                continue
            elapsed = OURS_NAV_TIMEOUT - max(0.0, deadline - time.monotonic())
            back_button_visible = any(
                node.enabled
                and node.bounds.area > 0
                and (
                    node.resource_id.endswith("android:id/back")
                    or node.content_desc.strip().lower() in {"back", "返回上一页"}
                )
                for node in observation.nodes
            )
            if (
                observation.stage == "chat"
                and "home.v2." in observation.activity.lower()
                and back_button_visible
                and not back_sent
            ):
                self.device.input("keyevent", "KEYCODE_BACK")
                back_sent = True
                last_back_elapsed = elapsed
                time.sleep(1.2)
                continue
            if observation.stage != "chat":
                ours_tap_attempts = 0
                last_ours_node = None
            # 应用冷启动或 WebView 首次恢复时层级可能暂时为空；聊天首页不要因暂时空层级退出。
            activity_lower = observation.activity.lower()
            # 已确认是元宝首页但无障碍树为空时，最多按一次归一化的“我们”位置。
            # 不对福利页、登录页或未知 Activity 盲点，且由 OURS_FALLBACK_DELAY 限制等待时间。
            if (
                not fallback_tapped
                and elapsed >= OURS_FALLBACK_DELAY
                and not observation.nodes
                and PACKAGE_NAME in activity_lower
                and "home.v2." in activity_lower
                and self.height > self.width
            ):
                x = round(self.width * OURS_FALLBACK_X_RATIO)
                y = round(self.height * OURS_FALLBACK_Y_RATIO)
                self._tap_point(x, y)
                fallback_tapped = True
                time.sleep(1.2)
                continue
            # 元宝的模板详情、写作和拍题 Activity 都可能被统一归类为 app；
            # 只要不是 home.v2 首页，就说明当前仍在应用内部嵌套页，可以安全返回。
            # 福利活动 WebView 也是应用内部嵌套页；它不会自己回到任务页，
            # 保留人工可读的关闭路径，供返回逻辑按返回键处理。
            nested_app = PACKAGE_NAME in activity_lower and "home.v2" not in activity_lower
            clearly_nested = observation.stage in {
                "writing",
                "image",
                "photo_question",
                "photo_preview",
                "picker",
                "activity",
            } or nested_app or (
                "home.v2" not in activity_lower and observation.stage not in {"app", "unknown"}
            )
            if (
                elapsed >= 12
                and clearly_nested
                and (last_back_elapsed is None or elapsed - last_back_elapsed >= 5)
            ):
                self.device.input("keyevent", "KEYCODE_BACK")
                back_sent = True
                last_back_elapsed = elapsed
                time.sleep(1.2)
            else:
                time.sleep(OURS_NAV_POLL_INTERVAL)
        detail = ""
        if last_observation is not None:
            detail = (
                f"阶段={last_observation.stage or '未知'}，"
                f"Activity={last_observation.activity[:120] or '未知'}"
            )
        elif last_error is not None:
            detail = str(last_error)[:180]
        raise AgentError(f"导航等待超时（{detail or '没有有效页面观测'}）")


    def _restart_app(self) -> None:
        """优先复用现有进程恢复首页，仅在确认失败后清理元宝进程。"""
        # 正常运行中的元宝从详情页返回时，直接发送 MAIN Intent 可以避免
        # ARM64 兼容层在 force-stop 后留下旧进程记录并卡在 Splash。
        try:
            self.device.launch_app(PACKAGE_NAME)
            self.device.wait_app_ready(PACKAGE_NAME)
            return
        except AgentError as direct_error:
            print(f"元宝直接恢复失败，准备清理进程重试：{str(direct_error)[:160]}")
        self.device.adb("shell", "am", "force-stop", PACKAGE_NAME, timeout=20)
        time.sleep(1)
        self.app_launcher(PACKAGE_NAME)
        self.device.wait_app_ready(PACKAGE_NAME)


    def _go_to_ours(self) -> Observation:
        """等待元宝首页导航就绪；兼容旧版“我们”页，最多重启一次。"""
        last_error: AgentError | None = None
        for attempt in range(2):
            try:
                return self._navigate_to_ours_until(time.monotonic() + OURS_NAV_TIMEOUT)
            except ManualActionRequired:
                raise
            except AgentError as exc:
                last_error = exc
                if attempt == 0:
                    print(f"元宝首页导航失败，正在重启元宝后重试：{str(exc)[:180]}")
                    try:
                        self._restart_app()
                    except ManualActionRequired:
                        raise
                    except AgentError as restart_error:
                        raise AgentError(f"无法回到元宝主页，应用重启失败：{restart_error}") from restart_error
        raise AgentError(f"无法回到元宝主页：{last_error or '未知错误'}")


    @staticmethod
    def _welfare_entry_node(observation: Observation) -> Node | None:
        """定位福利入口，兼容“福利中心”“元宝福利中心”“[icon] 福利中心”等文案。"""
        exact = find_node(observation.nodes, "福利中心", exact=True)
        if exact is not None:
            return exact
        # 新版聊天首页把入口改成了带图标前缀的横幅文案；只有首页允许模糊匹配，
        # 避免在聊天记录或其它嵌套页面误点同名文本。横幅上有两个含“福利中心”
        # 的节点，取面积最小的行动按钮，而不是整张横幅标题。
        if "home.v2" not in observation.activity.lower():
            return None
        candidates = [
            node
            for node in observation.nodes
            if node.enabled
            and node.bounds.area > 0
            and "福利中心" in node.searchable
            and not any(
                marker in node.searchable for marker in ("返回福利中心", "回到福利中心")
            )
        ]
        return min(candidates, key=lambda node: node.bounds.area, default=None)


    @staticmethod
    def _welfare_return_node(observation: Observation) -> Node | None:
        """定位任务完成后首页中的“返回福利中心”语义控件及其可点击父节点。"""
        labels = [
            node
            for node in observation.nodes
            if node.enabled
            and node.bounds.area > 0
            and any(marker in node.searchable for marker in ("返回福利中心", "回到福利中心"))
        ]
        if not labels:
            return None
        label = min(labels, key=lambda node: node.bounds.area)
        if label.clickable:
            return label
        if label.parent_clickable and label.parent_bounds is not None:
            direct_parents = [
                node
                for node in observation.nodes
                if node.enabled
                and node.clickable
                and node.bounds == label.parent_bounds
            ]
            if direct_parents:
                return min(direct_parents, key=lambda node: node.bounds.area)
            return label
        lx, ly = label.bounds.center
        parents = [
            node
            for node in observation.nodes
            if node.enabled
            and node.clickable
            and node.bounds.area >= label.bounds.area
            and node.bounds.left <= lx <= node.bounds.right
            and node.bounds.top <= ly <= node.bounds.bottom
        ]
        return min(parents, key=lambda node: node.bounds.area, default=label)


    @staticmethod
    def _welfare_return_tap_point(
        observation: Observation, node: Node, width: int, height: int
    ) -> tuple[int, int]:
        """在返回按钮与其它顶部控件重叠时选择未被覆盖的区域。"""
        bounds = node_interaction_bounds(node)
        center = bounds.center

        def contains(outer: Bounds, inner: Bounds) -> bool:
            return (
                outer != inner
                and outer.left <= inner.left
                and outer.top <= inner.top
                and outer.right >= inner.right
                and outer.bottom >= inner.bottom
            )

        competing = [
            candidate
            for candidate in observation.nodes
            if candidate.enabled
            and candidate.clickable
            and candidate.bounds.area > 0
            and candidate.bounds != bounds
            and candidate.bounds.area <= round(max(1, width * height) * 0.55)
            # 聊天消息容器、RecyclerView 等可点击祖先会覆盖整个顶部区域，
            # 但它们不是前景浮层；只把与返回按钮相交的兄弟控件视为遮挡。
            and not contains(candidate.bounds, bounds)
            and candidate.bounds.left < bounds.right
            and candidate.bounds.right > bounds.left
            and candidate.bounds.top < bounds.bottom
            and candidate.bounds.bottom > bounds.top
        ]
        if not competing:
            return center
        candidates = (
            (bounds.right - 15, center[1]),
            (bounds.left + 15, center[1]),
            center,
        )
        for point in candidates:
            x, y = point
            if (
                bounds.left <= x < bounds.right
                and bounds.top <= y < bounds.bottom
                and not any(
                    candidate.bounds.left <= x < candidate.bounds.right
                    and candidate.bounds.top <= y < candidate.bounds.bottom
                    for candidate in competing
                )
            ):
                return point
        return center


    @staticmethod
    def _welfare_entry_click_node(observation: Observation) -> Node | None:
        """把福利文案提升到当前层级中真正可点击的父节点。"""
        label = ExecutorNavigationMixin._welfare_entry_node(observation)
        if label is None or label.clickable:
            return label
        lx, ly = label.bounds.center
        parents = [
            node
            for node in observation.nodes
            if node.enabled
            and node.clickable
            and node.bounds.area >= label.bounds.area
            and node.bounds.left <= lx <= node.bounds.right
            and node.bounds.top <= ly <= node.bounds.bottom
        ]
        return min(parents, key=lambda node: node.bounds.area, default=label)


    @staticmethod
    def _welfare_drawer_toggle_node(observation: Observation) -> Node | None:
        """定位新版聊天首页无障碍树中的抽屉入口。"""
        candidates = [
            node
            for node in observation.nodes
            if node.enabled
            and node.clickable
            and node.bounds.area > 0
            and (
                node.resource_id.rsplit("/", 1)[-1] == "ic_navigation_show"
                or node.content_desc.strip() == "抽屉页入口"
            )
        ]
        return min(candidates, key=lambda node: node.bounds.area, default=None)


    def _go_to_welfare(self) -> Observation:
        self.reset_welfare_scroll()
        self._welfare_context = True
        last_observation: Observation | None = None
        for attempt in range(2):
            observation = self.observe()
            if self._dismiss_chat_attachment_sheet(observation):
                observation = self.observe()
            if self._hide_soft_keyboard():
                time.sleep(0.4)
                observation = self.observe()
            if observation.stage == "activity":
                # 福利专题页和任务列表共用 WebView Activity；先按页面语义返回，
                # 避免把专题页交给“我们”页导航逻辑反复重启应用。
                observation = self._close_activity_page(observation)
                if observation.stage == "welfare":
                    return observation
            # 新版聊天首页把福利入口做成了横幅；当前页已有可用入口时不再强制
            # 绕行“我们”页，避免抽屉改版后启动流程整体失败。
            if (
                observation.stage != "welfare"
                and self._welfare_entry_node(observation) is None
                and self._welfare_return_node(observation) is None
                and self._welfare_drawer_toggle_node(observation) is None
            ):
                observation = self._go_to_ours()
            last_observation = observation
            # 入口本身也可能在抽屉恢复期间暂时缺失；最多等待几秒后再使用已知布局坐标。
            entry_deadline = time.monotonic() + min(8, WELFARE_LOAD_TIMEOUT)
            fallback_tapped = False
            return_link_fallback_attempted = False
            last_entry_tap_at: float | None = None
            drawer_tap_attempts = 0
            last_drawer_tap_at: float | None = None
            while time.monotonic() < entry_deadline:
                if observation.stage == "welfare":
                    return observation
                webview_observation = self._welfare_webview_observation(
                    observation, last_entry_tap_at
                )
                if webview_observation is not None:
                    print("福利中心 WebView 已打开，无障碍树暂时为空，交由模型确认")
                    return webview_observation
                # 新版优先走左上角抽屉；只有没有抽屉控件的旧版页面才尝试
                # “返回福利中心”消息入口及“我们”页兼容路径。
                if (
                    not return_link_fallback_attempted
                    and last_entry_tap_at is not None
                    and time.monotonic() - last_entry_tap_at
                    >= WELFARE_RETURN_LINK_FALLBACK_DELAY
                    and observation.stage == "chat"
                    and PACKAGE_NAME in observation.activity.lower()
                    and self._welfare_return_node(observation) is not None
                    and self._welfare_drawer_toggle_node(observation) is None
                ):
                    return_link_fallback_attempted = True
                    try:
                        observation = self._go_to_ours()
                        last_observation = observation
                        last_entry_tap_at = None
                        continue
                    except AgentError as exc:
                        print(f"返回福利中心链接无响应，旧版“我们”导航失败：{str(exc)[:160]}")
                entry_node = self._welfare_entry_click_node(observation)
                drawer_node = self._welfare_drawer_toggle_node(observation)
                return_node = self._welfare_return_node(observation)
                now = time.monotonic()
                if entry_node is not None and last_entry_tap_at is None:
                    self._tap_node(entry_node)
                    last_entry_tap_at = time.monotonic()
                elif drawer_node is not None and drawer_tap_attempts < 2 and (
                    last_drawer_tap_at is None or now - last_drawer_tap_at >= 5
                ):
                    self._tap_node(drawer_node)
                    drawer_tap_attempts += 1
                    last_drawer_tap_at = now
                    last_entry_tap_at = None
                    print("新版聊天页未直接暴露福利入口，改从抽屉打开福利中心")
                elif return_node is not None and last_entry_tap_at is None:
                    x, y = self._welfare_return_tap_point(
                        observation, return_node, self.width, self.height
                    )
                    self._tap_point(x, y)
                    last_entry_tap_at = now
                elif (
                    not fallback_tapped
                    and observation.stage == "ours"
                    and time.monotonic() + 1 >= entry_deadline
                ):
                    # 仅在已识别的“我们”页使用历史布局后备，未知页面不得盲点。
                    self._tap_point(
                        round(self.width * WELFARE_ENTRY_FALLBACK_X_RATIO),
                        round(self.height * WELFARE_ENTRY_FALLBACK_Y_RATIO),
                    )
                    fallback_tapped = True
                    last_entry_tap_at = time.monotonic()
                time.sleep(OURS_NAV_POLL_INTERVAL)
                observation = self.observe()
                last_observation = observation
            # WebView 从详情页返回时偶尔需要数十秒恢复层级；在执行层内等待，避免把临时加载状态交给模型。
            deadline = time.monotonic() + WELFARE_LOAD_TIMEOUT
            while time.monotonic() < deadline:
                observation = self.observe()
                last_observation = observation
                if observation.stage == "welfare":
                    return observation
                webview_observation = self._welfare_webview_observation(
                    observation, last_entry_tap_at
                )
                if webview_observation is not None:
                    print("福利中心 WebView 已打开，无障碍树暂时为空，交由模型确认")
                    return webview_observation
                # 入口仍在当前页时重复点击，覆盖第一次点击未被 WebView 接收的竞态。
                entry_node = self._welfare_entry_click_node(observation)
                drawer_node = self._welfare_drawer_toggle_node(observation)
                return_node = self._welfare_return_node(observation)
                now = time.monotonic()
                if entry_node is not None and (
                    last_entry_tap_at is None or now - last_entry_tap_at >= 5
                ):
                    self._tap_node(entry_node)
                    last_entry_tap_at = now
                elif drawer_node is not None and drawer_tap_attempts < 2 and (
                    last_drawer_tap_at is None or now - last_drawer_tap_at >= 5
                ):
                    self._tap_node(drawer_node)
                    drawer_tap_attempts += 1
                    last_drawer_tap_at = now
                    last_entry_tap_at = None
                    print("福利侧栏尚未打开，重试新版抽屉入口")
                elif return_node is not None and (
                    last_entry_tap_at is None or now - last_entry_tap_at >= 5
                ):
                    x, y = self._welfare_return_tap_point(
                        observation, return_node, self.width, self.height
                    )
                    self._tap_point(x, y)
                    last_entry_tap_at = now
                time.sleep(WELFARE_LOAD_POLL_INTERVAL)
            if attempt == 0:
                print("福利中心页面加载超时，正在重启元宝并重试")
                self._restart_app()
        detail = ""
        if last_observation is not None:
            detail = f"（阶段={last_observation.stage or '未知'}，Activity={last_observation.activity[:120] or '未知'}）"
        raise AgentError(f"福利中心没有加载出任务页面{detail}")


    @staticmethod
    def _welfare_webview_observation(
        observation: Observation, entry_started_at: float | None
    ) -> Observation | None:
        """福利页 WebView 偶发没有无障碍文本时，等待渲染后交给视觉模型确认。"""
        if entry_started_at is None:
            return None
        if "webbrowseractivity" not in observation.activity.lower():
            return None
        if time.monotonic() - entry_started_at < WELFARE_WEBVIEW_READY_DELAY:
            return None
        return Observation(
            observation.image,
            observation.ui_xml,
            observation.ui,
            observation.nodes,
            observation.activity,
            "welfare",
        )


    @staticmethod
    def _bottom_navigation_tap_point(
        observation: Observation, node: Node, width: int, height: int
    ) -> tuple[int, int]:
        """从当前层级计算底部导航的第二触点，避开不可点击的图标子节点。"""
        bounds = node_interaction_bounds(node)
        x, y = bounds.center
        candidates = [
            candidate
            for candidate in observation.nodes
            if candidate.enabled
            and candidate.bounds.area > 0
            # 桌面窗口模式下应用内容可能只占屏幕的一部分；“我们”文字或
            # 底部导航容器通常位于图标下方，不能只按整屏高度筛选。
            and candidate.bounds.top >= bounds.top + round(bounds.height * 0.55)
            and candidate.bounds.bottom > bounds.bottom
            and candidate.bounds.left <= x <= candidate.bounds.right
            and candidate.bounds.right > x
        ]
        if candidates:
            target = min(candidates, key=lambda candidate: candidate.bounds.area)
            point = (x, target.bounds.center[1])
            if point != (x, y) and 0 <= point[0] < width and 0 <= point[1] < height:
                return point
        # 无障碍树没有暴露底部容器时，仍优先点图标下方的窗口内区域；此前
        # 使用 max(..., height-20) 会在桌面窗口模式把触点推到应用窗口之外。
        fallback_y = min(height - 1, bounds.bottom + max(1, round(bounds.height * 0.4)))
        return x, fallback_y


    @staticmethod
    def _is_new_chat_home(observation: Observation) -> bool:
        """只把明确暴露新版抽屉、且没有旧版“我们”入口的主 Activity 视为首页。"""
        activity_lower = observation.activity.lower()
        return (
            observation.stage in {"chat", "app"}
            and PACKAGE_NAME in activity_lower
            and "home.v2.ybhomeactivityv2" in activity_lower
            and find_node(observation.nodes, "我们", exact=True) is None
            and ExecutorNavigationMixin._welfare_drawer_toggle_node(observation) is not None
        )


    def _observe_after_entry_tap(self) -> Observation:
        """等待福利入口真正离开 WebView，覆盖点击后异步导航竞态。

        注意：福利 WebView 持续重绘时 dump 可能连续超时；旧代码在每次超时后
        都做标准化滑动，把任务列表推离顶部。滑动只应在至少一次成功 dump 之后
        仍停留福利页时执行，dump 超时只等待重试。
        """
        deadline = time.monotonic() + ENTRY_NAVIGATION_TIMEOUT
        dumped_once = False
        latest = self.observe()
        if latest.stage != "welfare":
            # 第一次观测可能正好落在 Activity 切换中；连续两次仍是非福利页
            # 才确认已经离开任务列表，避免旧无障碍树的瞬态阶段误推进本地状态。
            non_welfare_observations = 1
            while time.monotonic() < deadline:
                time.sleep(ENTRY_NAVIGATION_POLL_INTERVAL)
                latest = self.observe()
                if latest.stage == "welfare":
                    non_welfare_observations = 0
                    continue
                non_welfare_observations += 1
                if non_welfare_observations >= 2:
                    self._welfare_context = False
                    return latest
            return latest
        if latest.nodes:
            dumped_once = True
        while time.monotonic() < deadline:
            time.sleep(ENTRY_NAVIGATION_POLL_INTERVAL)
            latest = self.observe()
            if latest.stage != "welfare":
                self._welfare_context = False
                return latest
            if latest.nodes:
                if not dumped_once:
                    dumped_once = True
                elif getattr(self, "_welfare_context", False):
                    # 已确认仍在任务列表且层级可读，位置才需要标准化。
                    self.normalize_welfare_scroll()
        return latest


    def _close_activity_page(self, observation: Observation) -> Observation:
        """关闭误触打开的福利活动页，回到任务列表；只在 WebView 活动页生效。"""
        if observation.stage != "activity":
            return observation
        close = find_node(observation.nodes, "返回", clickable_only=True) or find_node(
            observation.nodes, "返回"
        )
        if close is None:
            close = find_node(observation.nodes, "关闭", clickable_only=True)
        if close is not None:
            self._tap_node(close)
        else:
            self.device.input("keyevent", "KEYCODE_BACK")
        time.sleep(1.5)
        after = self.observe()
        # 活动详情可能有多层路由；最多再退一次，避免陷在活动栈里反复等待。
        if after.stage == "activity":
            self.device.input("keyevent", "KEYCODE_BACK")
            time.sleep(1.5)
            after = self.observe()
        self._welfare_context = True
        return after


    def prepare_visual_exchange_navigation(self) -> None:
        """让视觉点击后的下一次观测不再把空 WebView 强行判成福利页。"""
        # 福利页空层级需要保留上下文，避免普通任务入口误点；兑换入口已经
        # 由截图明确选中，点击后必须允许 Activity/截图重新决定真实页面阶段。
        self._welfare_context = False


    def prepare_daily_ability_experience_navigation(self) -> None:
        """将体验浮层的点击按当前真实页面重新判定，避免福利页路由等待误拦截。"""
        self._welfare_context = False
        self._daily_ability_experience_navigation = True
