"""执行层：把受限工具动作真正落到设备上，并维护页面证据。"""
import os
import re
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import contextlib
import shutil
import subprocess
import time
from typing import Any, Callable, Iterable

from .config import Config
from .constants import (
    APPSTORE_PACKAGE_NAME,
    DAILY_ABILITY_BUTTON_X_RATIO,
    DAILY_ABILITY_BUTTON_Y_RATIO,
    DAILY_ABILITY_CARD_MAX_Y_RATIO,
    DAILY_ABILITY_CARD_MIN_Y_RATIO,
    DAILY_ABILITY_EXPERIENCE_ACTION_MARKERS,
    DAILY_ABILITY_EXPERIENCE_NAVIGATION_MARKERS,
    DAILY_ABILITY_EXPERIENCE_NAVIGATION_POLL_INTERVAL,
    DAILY_ABILITY_EXPERIENCE_NAVIGATION_TIMEOUT,
    ENTRY_NAVIGATION_POLL_INTERVAL,
    ENTRY_NAVIGATION_TIMEOUT,
    OURS_FALLBACK_DELAY,
    OURS_FALLBACK_X_RATIO,
    OURS_FALLBACK_Y_RATIO,
    OURS_NAV_POLL_INTERVAL,
    OURS_NAV_TIMEOUT,
    PACKAGE_NAME,
    STARTUP_OBSERVE_POLL_INTERVAL,
    STARTUP_OBSERVE_TIMEOUT,
    TASK_ENTRY_ACTION_X_RATIO,
    UI_DUMP_ATTEMPTS,
    UI_DUMP_TIMEOUT,
    WEBVIEW_ACTIVITY_MARKER,
    WEBVIEW_UI_DUMP_ATTEMPTS,
    WEBVIEW_UI_DUMP_RETRY_DELAY,
    WEBVIEW_UI_DUMP_TIMEOUT,
    WELFARE_ENTRY_FALLBACK_X_RATIO,
    WELFARE_ENTRY_FALLBACK_Y_RATIO,
    WELFARE_LOAD_POLL_INTERVAL,
    WELFARE_LOAD_TIMEOUT,
    WELFARE_RETURN_LINK_FALLBACK_DELAY,
    WELFARE_REWARD_BUTTON_Y_RATIO,
    WELFARE_TASK_CATALOG_SCROLL_MAX_ATTEMPTS,
    WELFARE_TASK_LIST_SCROLL_END_RATIO,
    WELFARE_TASK_LIST_SCROLL_START_RATIO,
    WELFARE_WEBVIEW_READY_DELAY,
    WRITING_SUBMIT_VERIFY_ATTEMPTS,
    WRITING_SUBMIT_VERIFY_INTERVAL,
)
from .device import Device
from .errors import (
    AgentError,
    AppUnresponsiveError,
    ManualActionRequired,
)
from .models import (
    Bounds,
    Node,
    Observation,
    ToolResult,
)
from .state import (
    read_daily_state,
    write_daily_state,
)
from .ui import (
    compact_ui,
    encode_adb_input_text,
    find_node,
    node_interaction_bounds,
    parse_nodes,
    png_pixel,
)
from .welfare import (
    detect_stage,
    has_complete_new_welfare_task_catalog,
    has_visible_exchange_product,
    is_daily_ability_experience_visible,
    is_new_welfare_page,
    parse_local_welfare_progress,
)

class ToolExecutor:
    """执行层只提供白名单工具，并维护生成等待和奖品使用证据。"""

    def __init__(
        self,
        config: Config,
        device: Device,
        app_launcher: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.device = device
        self.app_launcher = app_launcher or device.launch_app
        self.width, self.height = device.display_size()
        self.today = datetime.now(ZoneInfo(config.timezone_name)).date().isoformat()
        self.pending_generation = False
        self.reward_use_confirmed = False
        self.image_pushed = False
        self.exchange_target_clicked = False
        self.exchange_confirmed = False
        self.prize_records_opened = False
        self.reward_use_clicked = False
        self.reward_card_name = config.card_name
        self.reward_unavailable = False
        # 兑换页滚动后可能暂时只暴露商品卡片；保留本轮兑换截图已确认的
        # 积分和价格，供执行器在无障碍树缺字段时兜底。
        self.visual_exchange_points: int | None = None
        self.visual_exchange_cost: int | None = None
        self._last_exchange_observation: Observation | None = None
        # 元宝的每日任务会忽略完全相同的问题；工作流在每个任务轮次设置
        # 一个短的本地变体标记，避免把相同测试文本重复提交成无效动作。
        self.prompt_variant: str | None = None
        self._welfare_scroll_normalized = False
        self._welfare_task_catalog_scrolled = False
        self._welfare_context = False
        self._welfare_supplemental_task_progress: dict[str, int] = {}
        self._daily_ability_experience_navigation = False

    def _start_test_audio(self) -> tuple[str, str, str, subprocess.Popen[bytes]] | None:
        """把短测试音频临时路由到默认麦克风的监视源。"""
        audio_path = getattr(self.config, "test_audio_path", None)
        pactl = shutil.which("pactl")
        paplay = shutil.which("paplay")
        if not isinstance(audio_path, Path) or not audio_path.is_file():
            print(f"语音测试文件不存在，跳过音频注入：{audio_path}")
            return None
        if pactl is None or paplay is None:
            print("当前系统缺少 pactl/paplay，跳过音频注入")
            return None
        try:
            default_source = subprocess.run(
                [pactl, "get-default-source"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
            sink_name = f"auto_daily_voice_{os.getpid()}"
            loaded = subprocess.run(
                [
                    pactl,
                    "load-module",
                    "module-null-sink",
                    f"sink_name={sink_name}",
                    "sink_properties=device.description=AutoDailyVoice",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            module_id = loaded.stdout.strip()
            if loaded.returncode != 0 or not module_id:
                print("无法创建临时音频回环，继续使用当前麦克风")
                return None
            changed = subprocess.run(
                [pactl, "set-default-source", f"{sink_name}.monitor"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if changed.returncode != 0:
                subprocess.run([pactl, "unload-module", module_id], check=False)
                print("无法切换临时麦克风源，继续使用当前麦克风")
                return None
            process = subprocess.Popen(
                [paplay, "--device", sink_name, str(audio_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return default_source, module_id, sink_name, process
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"语音测试音频注入失败，继续使用当前麦克风：{type(exc).__name__}")
            return None

    @staticmethod
    def _finish_test_audio(
        session: tuple[str, str, str, subprocess.Popen[bytes]] | None,
    ) -> None:
        """恢复默认音频源并卸载本轮临时回环。"""
        if session is None:
            return
        default_source, module_id, _sink_name, process = session
        pactl = shutil.which("pactl")
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            process.wait(timeout=5)
        if pactl is None:
            return
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if default_source:
                subprocess.run(
                    [pactl, "set-default-source", default_source],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            subprocess.run(
                [pactl, "unload-module", module_id],
                capture_output=True,
                timeout=5,
                check=False,
            )

    def set_prompt_variant(self, variant: str | None) -> None:
        """设置当前任务轮次的唯一文本后缀，不把它交给视觉模型决定。"""
        self.prompt_variant = variant or None

    def set_visual_exchange_report(
        self, points: int, cost: int, verified: bool = False
    ) -> None:
        """保存本轮兑换截图已确认的积分和目标卡价格。"""
        self.visual_exchange_points = points
        self.visual_exchange_cost = cost
        self.visual_exchange_points_verified = verified

    def reset_welfare_scroll(self) -> None:
        """福利页每次重新打开后都重新校准滚动位置。"""
        self._welfare_scroll_normalized = False
        self._welfare_task_catalog_scrolled = False
        supplemental = getattr(self, "_welfare_supplemental_task_progress", None)
        if isinstance(supplemental, dict):
            supplemental.clear()

    def prepare_visual_exchange_navigation(self) -> None:
        """让视觉点击后的下一次观测不再把空 WebView 强行判成福利页。"""
        # 福利页空层级需要保留上下文，避免普通任务入口误点；兑换入口已经
        # 由截图明确选中，点击后必须允许 Activity/截图重新决定真实页面阶段。
        self._welfare_context = False

    def prepare_daily_ability_experience_navigation(self) -> None:
        """将体验浮层的点击按当前真实页面重新判定，避免福利页路由等待误拦截。"""
        self._welfare_context = False
        self._daily_ability_experience_navigation = True

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
        if new_welfare_page:
            if has_complete_new_welfare_task_catalog(observation, local_report):
                self._welfare_task_catalog_scrolled = True
                return observation
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
                    return observation
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

    def _daily_ability_click_point(
        self, args: dict[str, Any], observation: Observation
    ) -> tuple[int, int] | None:
        """定位固定主卡槽位中的“去体验”，忽略轮播内容和卡片颜色。"""
        if observation.stage != "welfare" or self.width <= 0 or self.height <= 0:
            return None
        if args.get("button_visible") is not True:
            return None

        target_x = round(self.width * DAILY_ABILITY_BUTTON_X_RATIO)
        target_y = round(self.height * DAILY_ABILITY_BUTTON_Y_RATIO)
        button_nodes = [
            node
            for node in observation.nodes
            if node.enabled
            and node.text.strip() == "去体验"
            and node.bounds.area > 0
        ]
        main_card_buttons = [
            node
            for node in button_nodes
            if self.width * 0.48 <= node.bounds.center[0] <= self.width * 0.78
            and self.height * DAILY_ABILITY_CARD_MIN_Y_RATIO
            <= node.bounds.center[1]
            <= self.height * DAILY_ABILITY_CARD_MAX_Y_RATIO
        ]
        if main_card_buttons:
            button = min(
                main_card_buttons,
                key=lambda node: (
                    (node.bounds.center[0] - target_x) ** 2
                    + (node.bounds.center[1] - target_y) ** 2
                ),
            )
            return button.bounds.center
        if button_nodes:
            # 当前树已给出“去体验”边界但只在顶部轮播等其它区域时，禁止回退
            # 到主卡固定点，以免误点同名入口。
            return None

        # WebView 偶发不提供按钮无障碍节点；此时按钮位置固定，模型必须先从
        # 当前截图确认主卡槽位显示“去体验”，执行器不采纳模型提供的坐标。
        return target_x, target_y

    def _open_daily_ability(
        self, args: dict[str, Any], observation: Observation
    ) -> ToolResult:
        """点击主轮播卡固定槽位的“去体验”，随后把体验页面交给视觉模型。"""
        self._welfare_context = True
        if observation.stage != "welfare":
            return self._result(
                False,
                f"主卡体验按钮要求福利页，当前阶段={observation.stage or '未知'}",
                stage=observation.stage,
                visual_replan=True,
            )
        if args.get("button_visible") is not True:
            return self._result(
                True,
                "主轮播卡固定位置当前未确认显示‘去体验’，未点击",
                stage=observation.stage,
                ability_card_visible=False,
            )
        point = self._daily_ability_click_point(args, observation)
        if point is None:
            return self._result(
                False,
                "当前可见的‘去体验’不在主轮播卡固定位置，未点击",
                stage=observation.stage,
                visual_replan=True,
            )
        print(f"点击主轮播卡固定位置的‘去体验’（x={point[0]}, y={point[1]}）")
        self._tap_point(*point)
        time.sleep(1.2)
        after = self.observe()
        return self._result(
            True,
            "已点击主轮播卡‘去体验’，将当前体验页面交给视觉模型处理",
            stage=after.stage,
            ability_station_clicked=True,
            ability_station_evidence="main_carousel_experience_clicked",
            experience_opened=after.stage != "welfare",
        )

    def _read_state(self) -> dict[str, Any]:
        return read_daily_state(self.config.state_path)

    def _write_state(self, exchange_status: str, card_name: str | None = None) -> None:
        """保存仅用于同日恢复的非敏感状态，不写入账号、令牌或截图。"""
        path = self.config.state_path
        state = self._read_state()
        if state.get("date") != self.today:
            state = {}
        state.update(
            {
                "date": self.today,
                "card": card_name or self.reward_card_name,
                "exchange_status": exchange_status,
            }
        )
        write_daily_state(path, state)

    def _today_exchange_status(self, card_name: str | None = None) -> str | None:
        state = self._read_state()
        expected_card = card_name or self.reward_card_name
        if state.get("date") != self.today or state.get("card") != expected_card:
            return None
        status = state.get("exchange_status")
        return status if status in {"pending", "used", "unavailable"} else None

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
        if ToolExecutor._is_protocol_prompt(observation.nodes, observation.activity):
            return "元宝协议页未能自动确认，请先人工点击“同意并继续”"
        if any(
            marker in labels
            for marker in ("手机号登录", "微信登录", "qq登录", "验证码登录", "获取验证码")
        ):
            return "元宝当前未登录，请先人工完成手机号、微信或 QQ 登录"
        if any(marker in labels for marker in ("应用无响应", "application not responding")):
            return "元宝出现应用无响应窗口，请先关闭该窗口并检查应用状态"
        return None

    def observe(self) -> Observation:
        # Waydroid/Android 某些实例首次启动应用时会弹出系统兼容性提示；
        # 该提示不是业务状态，若不关闭会遮住底部导航并阻断固定的首步导航。
        for _ in range(2):
            # WebView 页面持续重绘时，uiautomator 可能一直等不到 idle state。
            # 福利页探针给足完成一次压缩树 dump 的时间；仍失败时保留截图回退，
            # 不能把页面渲染竞态误判成 ADB 故障。
            activity = self.device.activity()
            webview = WEBVIEW_ACTIVITY_MARKER in activity.lower()
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

    def _result(self, ok: bool, message: str, **data: Any) -> ToolResult:
        return ToolResult(ok, message, data)

    def _tap_node(self, node: Node) -> None:
        x, y = node_interaction_bounds(node).center
        self._tap_point(x, y)

    def _tap_point(self, x: int, y: int) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise AgentError(f"坐标越界：{x},{y}")
        self.device.input("tap", str(x), str(y))

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

    @staticmethod
    def _daily_ability_navigation_button_visible(observation: Observation) -> bool:
        """判断当前体验浮层是否有可能触发异步 Activity 导航。"""
        return any(
            any(marker in node.searchable for marker in DAILY_ABILITY_EXPERIENCE_NAVIGATION_MARKERS)
            for node in observation.nodes
        )

    def _observe_after_daily_ability_action(
        self, before: Observation, navigation_expected: bool
    ) -> Observation:
        """等待能力浮层的导航按钮真正切出 WebView，避免点击旧页面。"""
        latest = self.observe()
        if not navigation_expected:
            return latest
        if latest.activity != before.activity or latest.stage != "welfare":
            return latest
        deadline = time.monotonic() + DAILY_ABILITY_EXPERIENCE_NAVIGATION_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(DAILY_ABILITY_EXPERIENCE_NAVIGATION_POLL_INTERVAL)
            latest = self.observe()
            if latest.activity != before.activity or latest.stage != "welfare":
                return latest
        return latest

    def _guard_generation(self, tool_name: str) -> ToolResult | None:
        if self.pending_generation and tool_name != "wait_5s":
            return self._result(False, "消息仍可能在生成，必须先调用 wait_5s 等待右下角终止按钮消失")
        return None

    def _find_input(self, observation: Observation) -> Node | None:
        preferred = find_node(observation.nodes, "edConversationInput", clickable_only=False)
        if preferred is not None:
            return preferred
        # 写作页的 Compose 输入框有时没有资源 ID，使用可编辑节点作为后备。
        candidates = [
            node
            for node in observation.nodes
            if node.enabled
            and node.bounds.area > 0
            and (
                node.class_name.lower().endswith("edittext")
                or "输入" in node.content_desc
                or "主题" in node.content_desc
            )
        ]
        return max(candidates, key=lambda node: node.bounds.area, default=None)

    def _fixed_prompt(self) -> str:
        value = self.config.test_prompt
        variant = getattr(self, "prompt_variant", None)
        return f"{value} {variant}" if variant else value

    def _find_send(self, observation: Observation) -> Node | None:
        nodes = [node for node in observation.nodes if node.enabled and node.bounds.area > 0]
        preferred = [
            node
            for node in nodes
            if node.clickable
            and (
                "发送" in node.text
                or "发送" in node.content_desc
                or "send" in node.content_desc.lower()
                or (
                    (rid := node.resource_id.lower())
                    and (
                        "send" in rid
                        or "submit" in rid
                        or "arrow_up" in rid
                        or "arrowup" in rid
                        or ("ic_up" in rid and "upload" not in rid)
                    )
                )
            )
        ]
        if preferred:
            return max(preferred, key=lambda node: node.bounds.area)
        input_node = self._find_input(observation)
        if input_node is not None:
            input_bounds = node_interaction_bounds(input_node)
            anchored = [
                node
                for node in nodes
                if node.clickable
                and node is not input_node
                and node.bounds.area <= 40_000
                and node.bounds.left >= input_bounds.right - 48
                and node.bounds.right >= input_bounds.right - 4
                and min(node.bounds.bottom, input_bounds.bottom)
                - max(node.bounds.top, input_bounds.top)
                >= min(20, input_bounds.height // 2)
                and not any(
                    marker in f"{node.text} {node.content_desc}".lower()
                    for marker in ("上传", "相册", "键盘", "收起", "关闭")
                )
            ]
            if anchored:
                return min(
                    anchored,
                    key=lambda node: (
                        abs(node.bounds.center[1] - input_bounds.center[1]),
                        -node.bounds.area,
                    ),
                )
            return None
        # Compose 版本的绿色上箭头有时没有文字和资源 ID，只暴露为右下角可点击图标。
        # 没有可访问输入框的页面才按屏幕右下区域兜底。
        bottom_right = [
            node
            for node in nodes
            if node.clickable
            and node.bounds.top >= self.height * 0.72
            and node.bounds.center[0] >= self.width * 0.72
            and node.bounds.area <= 40_000
            and not any(
                marker in f"{node.text} {node.content_desc}".lower()
                for marker in ("上传", "相册", "键盘", "收起", "关闭")
            )
        ]
        return min(
            bottom_right,
            key=lambda node: (
                abs(node.bounds.center[0] - (self.width - 60))
                + abs(node.bounds.center[1] - (self.height - 60)),
                -node.bounds.area,
            ),
            default=None,
        )

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

    def _input_fixed_prompt(self, observation: Observation) -> ToolResult:
        if observation.stage == "activity":
            # 福利入口可能误触活动横幅；input 阶段先关回福利中心，让外层重新
            # 定位真正的任务入口，而不是在活动页上等待输入框直到熔断。
            after = self._close_activity_page(observation)
            if after.stage == "welfare":
                return self._result(False, "当前是福利活动页，已返回任务列表，请重新点击任务入口")
            return self._result(
                False,
                f"当前是福利活动页，返回后仍未回到任务列表（阶段={after.stage or '未知'}），请重新观察后再试",
            )
        if observation.stage == "ours":
            ours = find_node(observation.nodes, "问元宝", exact=True)
            if ours is not None:
                self._tap_node(ours)
            else:
                self._tap_point(111, self.height - 43)
            time.sleep(1.5)
            observation = self.observe()

        # 任务入口点击后，写作页和聊天页都可能先显示过渡层；先等待输入控件，
        # 并在写作类型选择页保留默认“通用”，避免把页面竞态交给外层重试。
        deadline = time.monotonic() + 12
        input_node: Node | None = None
        while time.monotonic() < deadline:
            input_node = self._find_input(observation)
            if input_node is not None:
                break
            if observation.stage == "writing":
                generic = find_node(observation.nodes, "通用", exact=True)
                if generic is not None:
                    self._tap_node(generic)
                    time.sleep(1)
                    observation = self.observe()
                    continue
                # Compose 写作页偶尔没有可访问输入节点，点击主题区域可唤起输入框。
                self._tap_point(self.width // 2, min(self.height - 100, 755))
            elif observation.stage in {"chat", "app", "unknown"}:
                self._tap_point(self.width // 2, self.height - 100)
            time.sleep(0.8)
            observation = self.observe()
        if input_node is None:
            return self._result(False, "等待输入框超时，当前页面尚未进入提问/写作输入状态")

        value = self._fixed_prompt()
        if any(ord(char) > 127 for char in value):
            return self._result(False, "固定测试文本含中文，当前设备未配置可靠中文输入；请在 TEST_PROMPT 中使用英文")
        escaped = encode_adb_input_text(value)
        # 写作/聊天页面会复用草稿框；每轮必须替换旧内容，不能将任务标记追加到上一轮。
        for attempt in range(3):
            if attempt:
                refreshed = self.observe()
                refreshed_input = self._find_input(refreshed)
                if refreshed_input is None:
                    return self._result(False, "重试输入时输入框已消失")
                observation = refreshed
                input_node = refreshed_input
            current_text = input_node.text.strip()
            if current_text == value:
                return self._result(True, "固定测试文本已准确填入当前输入框", stage=observation.stage)
            self._tap_node(input_node)
            if current_text:
                self.device.input("keycombination", "113", "29")
                self.device.input("keyevent", "KEYCODE_DEL")
                time.sleep(0.2)
                cleared = self.observe()
                cleared_input = self._find_input(cleared)
                if cleared_input is None or cleared_input.text.strip():
                    return self._result(False, "旧输入内容未能清空，已中止本轮以避免拼接发送")
                observation = cleared
                input_node = cleared_input
            self.device.input("text", escaped)
            check_deadline = time.monotonic() + 2.5
            while time.monotonic() < check_deadline:
                after = self.observe()
                after_input = self._find_input(after)
                if after_input is not None and after_input.text.strip() == value:
                    return self._result(True, "已在当前输入框中准确填入固定测试文本", stage=after.stage)
                time.sleep(0.4)
        return self._result(False, "固定测试文本没有出现在输入框")

    def _wait_generation(self) -> ToolResult:
        time.sleep(5)
        deadline = time.monotonic() + self.config.generation_timeout_seconds
        checks = 0
        while True:
            checks += 1
            try:
                observation = self.observe()
            except AgentError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(5)
                continue
            busy = False
            for node in observation.nodes:
                label = f"{node.text} {node.content_desc}".lower()
                if "停止生成" in label or "停止回答" in label or "stop generating" in label:
                    busy = True
                    break
                if (
                    node.bounds.area > 1000
                    and "fl_send_stop" in node.resource_id.lower()
                    and "发送" not in label
                ):
                    busy = True
                    break
            if not busy:
                self.pending_generation = False
                return self._result(
                    True,
                    f"已等待生成完成（检查 {checks} 次）",
                    stage=observation.stage,
                    daily_ability_experience=is_daily_ability_experience_visible(observation),
                )
            if time.monotonic() >= deadline:
                return self._result(False, "等待生成超时，右下角终止按钮仍存在")
            time.sleep(5)

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
            and ToolExecutor._welfare_drawer_toggle_node(observation) is not None
        )

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
        label = ToolExecutor._welfare_entry_node(observation)
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
    def _picker_image_node(nodes: Iterable[Node]) -> Node | None:
        """从系统相册层级选出首个图片卡片容器，而不是图片子节点。"""
        visible = [node for node in nodes if node.enabled and node.bounds.area > 1500]
        # PictureSelector 的网格卡片是可点击的 RelativeLayout；顶部相册栏也
        # 可能可点击，但高度明显小于图片卡片，按尺寸和位置排除它。
        cards = [
            node
            for node in visible
            if node.clickable
            and node.bounds.top > 120
            and node.bounds.width >= 100
            and node.bounds.height >= 100
        ]
        if cards:
            return min(cards, key=lambda node: (node.bounds.top, node.bounds.left))
        image_nodes = [
            node
            for node in visible
            if node.bounds.top > 80 and node.class_name.lower().endswith("imageview")
        ]
        if image_nodes:
            return min(image_nodes, key=lambda node: (node.bounds.top, node.bounds.left))
        return None

    def _chat_camera_button(self, observation: Observation) -> Node | None:
        """定位输入框左侧真实的“拍照”按钮，避开上方快捷能力栏。"""
        if observation.stage != "chat":
            return None
        candidates = [
            node
            for node in observation.nodes
            if node.enabled
            and any(
                value.strip() == "拍照"
                for value in (node.text, node.content_desc)
            )
            and node_interaction_bounds(node).area > 0
        ]
        candidates = [
            node
            for node in candidates
            if node_interaction_bounds(node).center[0] <= self.width * 0.22
            and node_interaction_bounds(node).center[1] >= self.height * 0.78
        ]
        return min(
            candidates,
            key=lambda node: node_interaction_bounds(node).area,
            default=None,
        )

    def _ensure_picker_image(self) -> ToolResult:
        if not self.image_pushed:
            self.device.push_image(self.config.test_image_path)
            self.image_pushed = True
            time.sleep(1)
        observation = self.observe()
        # P 图和拍题的图片入口并不完全一致：拍题有时先进入相机预览，
        # 再由左下角相册入口切到 PictureSelector。所有重试都限制在已知业务页内。
        entry_markers = (
            "相册",
            "相册答题",
            "相片",
            "图片",
            "上传",
            "选择图片",
            "从相册",
            "相册入口",
            "gallery",
            "image",
        )
        for attempt in range(6):
            if observation.stage == "picker":
                break
            camera_button = self._chat_camera_button(observation)
            candidates = [
                node
                for node in observation.nodes
                if node.clickable
                and node.bounds.area > 0
                and any(word in node.searchable for word in entry_markers)
            ]
            if camera_button is not None:
                self._tap_node(camera_button)
            elif candidates:
                # 优先点击最靠近输入区左侧的图片入口，避免误触顶部说明文本。
                self._tap_node(
                    min(
                        candidates,
                        key=lambda node: (
                            abs(node.bounds.center[0] - self.width * 0.12)
                            + abs(node.bounds.center[1] - self.height * 0.86),
                            -node.bounds.area,
                        ),
                    )
                )
            elif observation.stage == "photo_preview":
                # 相机预览没有相册文字时，先返回拍题页，再由下一轮选择入口。
                self.device.input("keyevent", "KEYCODE_BACK")
            else:
                if attempt == 5:
                    break
                time.sleep(1)
                observation = self.observe()
                continue
            time.sleep(min(5, 2 + attempt))
            observation = self.observe()
        if observation.stage != "picker":
            return self._result(
                False,
                f"没有进入系统图片选择器（阶段={observation.stage or '未知'}，Activity={observation.activity[:100] or '未知'}）",
            )
        permission = find_node(observation.nodes, "允许", "Allow", clickable_only=True)
        if permission is not None:
            self._tap_node(permission)
            time.sleep(1)
            observation = self.observe()
        # PictureSelector 的文件名通常是不可点击的 content-desc，点击同边界的父 View
        # 可以稳定选中脚本刚推送的测试图片，而不是随机选到旧照片。
        filename = "auto-daily-test.png"
        label = find_node(observation.nodes, filename)
        if label is not None:
            matching = [
                node
                for node in observation.nodes
                if node.clickable and node.enabled and node.bounds == label.bounds
            ]
            if matching:
                self._tap_node(matching[0])
                time.sleep(2)
                return self._result(True, "已选择本地脱敏测试图片", stage=self.observe().stage)
        image_node = self._picker_image_node(observation.nodes)
        if image_node is None:
            self._tap_point(110, 220)
        else:
            self._tap_node(image_node)
        time.sleep(2)
        return self._result(True, "已选择本地脱敏测试图片", stage=self.observe().stage)

    def _open_exchange(self) -> ToolResult:
        observation = self._go_to_welfare()
        # 奖励弹窗的绿色“开心收下”在部分版本只有图片语义，没有可点击文本。
        # 兑换前先收下残留奖励，避免弹窗拦截兑换商城入口。
        self._dismiss_reward_popup(observation)
        for attempt in range(5):
            observation = self.observe()
            # 点进的可能是奖品记录/积分明细等相邻 WebView：只要积分余额可读，
            # 直接核对积分与价格，积分不足就安全跳过，不必强求 exchange 阶段。
            points, cost = self._read_points_and_cost(observation)
            if points is not None and cost is not None and points < cost:
                self.reward_card_name = self.config.card_name
                self.reward_unavailable = True
                self._write_state("unavailable")
                return self._result(
                    True,
                    f"目标商品“{self.config.card_name}”需要 {cost} 积分，当前仅 {points}，已安全跳过兑换",
                    stage=observation.stage,
                    reward_unavailable=True,
                    points=points,
                    cost=cost,
                )
            if observation.stage == "exchange" and (
                has_visible_exchange_product(observation, self.config.card_name)
                or not observation.nodes
            ):
                self._last_exchange_observation = observation
                return self._result(True, "已进入兑换商城", stage=observation.stage)
            # 福利页顶部“兑换商城”是 TextView 文案节点（不可点击），不要用
            # clickable_only 查找；节点离屏或零边界时先把页面滚回顶部，
            # 不再使用与布局绑定的固定坐标。
            mall = find_node(observation.nodes, "兑换商城", exact=True)
            if mall is None:
                mall = find_node(observation.nodes, "兑换商城")
            if mall is not None and mall.bounds.area > 0:
                self._tap_node(mall)
                time.sleep(3)
                after = self.observe()
                if after.stage == "exchange" and (
                    has_visible_exchange_product(after, self.config.card_name)
                    or not after.nodes
                ):
                    self._last_exchange_observation = after
                    return self._result(True, "已进入兑换商城", stage=after.stage)
                points, cost = self._read_points_and_cost(after)
                if points is not None and cost is not None and points < cost:
                    self.reward_card_name = self.config.card_name
                    self.reward_unavailable = True
                    self._write_state("unavailable")
                    return self._result(
                        True,
                        f"目标商品“{self.config.card_name}”需要 {cost} 积分，当前仅 {points}，已安全跳过兑换",
                        stage=after.stage,
                        reward_unavailable=True,
                        points=points,
                        cost=cost,
                    )
            elif observation.stage == "welfare":
                if not any(node.bounds.area > 0 for node in observation.nodes):
                    # WebView dump 超时时截图仍可能是福利页；空层级没有足够
                    # 证据证明入口离屏，先等待下一次观测，禁止用空数据滑动页面。
                    time.sleep(1)
                    continue
                # 手指向下滑动，让页面回到顶部；下一轮仍按最新无障碍树找入口。
                self.device.input(
                    "swipe",
                    str(self.width // 2),
                    str(round(self.height * 0.28)),
                    str(self.width // 2),
                    str(round(self.height * 0.86)),
                    "500",
                )
                time.sleep(1)
        return self._result(False, "兑换商城没有加载")

    def _read_points_and_cost(
        self, observation: Observation
    ) -> tuple[int | None, int | None]:
        """在积分明细/奖品记录等相邻页直接读取余额与 3 天卡价格。

        失败报告证明：积分不足时页面可能落在 prize_records 而不是 exchange，
        但余额（顶部纯数字）与卡价（“兑换QQ超级会员3天卡”附近的 -30000）
        依然可读。读不到任一值时返回 None，调用方继续走正常兑换流程。
        """
        points: int | None = None
        for node in observation.nodes:
            if node.bounds.area <= 0 or node.bounds.top > 160:
                continue
            value = node.text.strip().replace(",", "")
            match = re.fullmatch(r"(\d+)(?:\s*积分)?", value)
            if match is None:
                continue
            points = min(points, int(match.group(1))) if points is not None else int(match.group(1))
        if points is None:
            return None, None
        cost: int | None = None
        card_index: int | None = None
        ordered = list(observation.nodes)
        for index, node in enumerate(ordered):
            label = f"{node.text} {node.content_desc}".replace("\n", "")
            if "超级会员" in label and "天卡" in label:
                card_index = index
                break
        if card_index is None:
            return points, None
        for node in ordered[card_index : card_index + 12]:
            for token in re.findall(r"-?\d[\d,]*", f"{node.text} {node.content_desc}"):
                try:
                    number = int(token.replace(",", ""))
                except ValueError:
                    continue
                magnitude = abs(number)
                if 1000 <= magnitude <= 1000000:
                    cost = magnitude if cost is None else min(cost, magnitude)
        if cost is None:
            return points, None
        return points, cost

    def _dismiss_reward_popup(self, observation: Observation | None = None) -> bool:
        observation = observation or self.observe()
        reward_amount = any(
            re.fullmatch(r"\d+\s*积分", node.text.strip())
            and node.bounds.top > self.height // 2
            for node in observation.nodes
        )
        text_button = find_node(observation.nodes, "开心收下", "收下")
        if text_button is not None:
            self._tap_node(text_button)
            time.sleep(2)
            return True
        button = find_node(observation.nodes, "button", exact=True)
        if reward_amount and button is not None:
            self._tap_node(button)
            time.sleep(2)
            return True
        # WebView 奖励遮罩常完全不进入无障碍树。当前手机布局中，
        # 绿色“开心收下”按钮中心是可验证的亮绿色像素，普通任务行该位置为深色。
        if (
            observation.stage == "welfare"
            or WEBVIEW_ACTIVITY_MARKER in observation.activity.lower()
        ):
            x = self.width // 2
            y = round(self.height * WELFARE_REWARD_BUTTON_Y_RATIO)
            pixel = png_pixel(observation.image, x, y)
            if (
                pixel is not None
                and pixel[1] >= 160
                and pixel[1] >= pixel[0] * 2
                and pixel[1] >= pixel[2] * 1.25
            ):
                self._tap_point(x, y)
                time.sleep(2)
                return True
        return False

    def _find_card_and_button(
        self, observation: Observation, card_name: str | None = None
    ) -> tuple[Node, Node] | None:
        wanted_card = card_name or self.reward_card_name
        product = find_node(observation.nodes, wanted_card, exact=True)
        if product is None:
            product = find_node(observation.nodes, wanted_card)
        if product is None:
            return None
        buttons = [
            node
            for node in observation.nodes
            if node.enabled
            and node.bounds.area > 0
            and (node.text == "兑换" or node.content_desc == "兑换")
        ]
        if not buttons:
            return None
        px, py = product.bounds.center
        same_card = [
            node
            for node in buttons
            if abs(node.bounds.center[0] - px) < 220 and node.bounds.center[1] >= product.bounds.bottom
        ]
        if not same_card:
            # 商品卡片可能仍在视口边缘，不能把别的商品按钮误配给它。
            return None
        return product, min(same_card, key=lambda node: abs(node.bounds.center[1] - py))

    def _read_exchange_points(self, observation: Observation) -> int | None:
        """读取兑换页顶部的当前积分，无法确认时返回 None。"""
        labelled: list[tuple[int, int]] = []
        plain: list[tuple[int, int]] = []
        point_labels = [
            node
            for node in observation.nodes
            if node.bounds.area > 0 and "我的积分" in node.searchable
        ]
        numeric_nodes: list[tuple[Node, int]] = []
        for node in observation.nodes:
            # 新版兑换页把“我的积分”固定在 y=128 左右；保留标题栏下方
            # 的固定积分栏，不能用旧版 y=120 的边界误删真实积分。
            if node.bounds.area <= 0 or node.bounds.top > 220:
                continue
            value = node.text.strip().replace(",", "")
            match = re.fullmatch(r"(\d+)(?:\s*积分)?", value)
            if match is None:
                continue
            numeric_nodes.append((node, int(match.group(1))))
            item = (node.bounds.left, int(match.group(1)))
            if "积分" in value:
                labelled.append(item)
            else:
                plain.append(item)
        if point_labels:
            nearby = [
                (abs(node.bounds.left - label.bounds.right), value)
                for label in point_labels
                for node, value in numeric_nodes
                if label.bounds.top - 20 <= node.bounds.top <= label.bounds.bottom + 20
                and node.bounds.left >= label.bounds.left
            ]
            if nearby:
                return min(nearby, key=lambda item: item[0])[1]
        candidates = labelled or plain
        if not candidates:
            return None
        # 积分数字紧邻“我的积分：”，通常是顶部最靠左的纯数字节点。
        return min(candidates, key=lambda item: item[0])[1]

    def _read_product_cost(self, observation: Observation, product: Node) -> int | None:
        """读取目标商品卡片下方的积分价格。"""
        px, py = product.bounds.center
        candidates = []
        for node in observation.nodes:
            if node.bounds.area <= 0:
                continue
            value = node.text.strip().replace(",", "")
            match = re.fullmatch(r"(\d+)(?:\s*积分)?", value)
            if match is None:
                continue
            nx, ny = node.bounds.center
            if ny < product.bounds.bottom or ny > product.bounds.bottom + 130:
                continue
            if abs(nx - px) > 180:
                continue
            candidates.append((abs(ny - product.bounds.bottom), int(match.group(1))))
        return min(candidates, default=(0, None), key=lambda item: item[0])[1]

    def _recover_exchange_state(self, observation: Observation) -> str | None:
        """从同日状态文件恢复兑换流程，避免服务重启后重复扣积分。"""
        status = self._today_exchange_status(self.config.card_name)
        if status is None:
            return None
        self.reward_card_name = self.config.card_name
        self.exchange_confirmed = True
        if status == "used":
            self.prize_records_opened = True
            self.reward_use_clicked = True
            self.reward_use_confirmed = True
            return status
        if status == "unavailable":
            self.reward_unavailable = True
            return status
        return "pending"

    def _redeem_card(self) -> ToolResult:
        observation = self.observe()
        if observation.stage != "exchange":
            result = self._open_exchange()
            if not result.ok:
                return result
            if result.data.get("reward_unavailable"):
                return result
            # WebView 路由完成后紧接着的一次 dump 可能短暂复用旧福利层级；
            # 沿用刚由 _open_exchange 确认过的商城观测，避免在错误页面滚动。
            observation = getattr(self, "_last_exchange_observation", None)
            if observation is None or observation.stage != "exchange":
                observation = self.observe()
        self.reward_card_name = self.config.card_name
        self.reward_unavailable = False
        recovered = self._recover_exchange_state(observation)
        if recovered == "used":
            return self._result(
                True,
                "已从同日状态恢复：QQ超级会员卡已对绑定账号使用成功",
                stage=observation.stage,
                reward_use_confirmed=True,
            )
        if recovered == "pending":
            return self._result(
                True,
                "已从同日状态恢复：QQ超级会员卡已兑换，下一步打开奖品记录",
                stage=observation.stage,
                prize_records_opened=False,
            )
        if recovered == "unavailable":
            return self._result(
                True,
                "已从同日状态恢复：QQ超级会员3天卡积分不足，已安全跳过兑换",
                stage=observation.stage,
                reward_unavailable=True,
            )
        if self._has_exchange_success(observation):
            self.exchange_confirmed = True
            self._write_state("pending")
            return self._result(
                True,
                "当前页面已有明确兑换成功弹窗，跳过重复兑换",
                stage=observation.stage,
                prize_records_opened=False,
            )
        found = self._find_card_and_button(observation, self.reward_card_name)
        for _ in range(6):
            if found:
                break
            self.device.input("swipe", str(self.width // 2), "1150", str(self.width // 2), "450", "500")
            time.sleep(1)
            observation = self.observe()
            found = self._find_card_and_button(observation, self.reward_card_name)
        if not found:
            return self._result(False, f"没有找到目标商品：{self.config.card_name}")
        product, button = found
        points = self._read_exchange_points(observation)
        cost = self._read_product_cost(observation, product)
        if points is None:
            if getattr(self, "visual_exchange_points_verified", False):
                points = self.visual_exchange_points
        if cost is None:
            cost = self.visual_exchange_cost
        if points is None or cost is None:
            return self._result(
                False,
                f"已找到目标商品“{self.config.card_name}”，但无法确认积分或价格，拒绝盲目兑换",
                stage=observation.stage,
            )
        if points < cost:
            self.reward_unavailable = True
            self._write_state("unavailable")
            return self._result(
                True,
                f"目标商品“{self.config.card_name}”需要 {cost} 积分，当前仅 {points}，已安全跳过兑换",
                stage=observation.stage,
                reward_unavailable=True,
                points=points,
                cost=cost,
            )
        self._tap_node(button)
        time.sleep(2)
        self.exchange_target_clicked = True
        return self._result(True, f"已点击目标商品“{self.reward_card_name}”的兑换按钮", stage=self.observe().stage)

    def _find_reward_use_button(self, observation: Observation) -> Node | None:
        product = find_node(observation.nodes, self.reward_card_name)
        if product is None:
            return None
        buttons = [
            node
            for node in observation.nodes
            if node.enabled
            and node.bounds.area > 0
            and any(
                label in {"立即使用", "去使用", "使用"}
                for label in (node.text.strip(), node.content_desc.strip())
            )
        ]
        if not buttons:
            return None
        px, py = product.bounds.center
        same_card = [
            node
            for node in buttons
            if abs(node.bounds.center[0] - px) < 260
            and node.bounds.center[1] >= product.bounds.top - 30
        ]
        return min(same_card or buttons, key=lambda node: abs(node.bounds.center[1] - py))

    @staticmethod
    def _has_evidence(observation: Observation, *markers: str) -> bool:
        content = " ".join(
            [observation.ui]
            + [f"{node.text} {node.content_desc}" for node in observation.nodes]
        ).lower()
        return any(marker.lower() in content for marker in markers)

    @staticmethod
    def _has_exchange_success(observation: Observation) -> bool:
        """兑换详情页也会显示“奖品记录”说明，成功必须有更强的页面证据。"""
        content = " ".join(
            [observation.ui]
            + [f"{node.text} {node.content_desc}" for node in observation.nodes]
        ).lower()
        if "兑换成功" in content or "恭喜兑换成功" in content:
            return True
        return "已兑换" in content and ("奖品记录" in content or "前往" in content)

    def _has_reward_use_success(self, observation: Observation) -> bool:
        """只接受目标卡片附近的使用状态，避免把顶部“已使用”筛选标签当成结果。"""
        reward_card_name = getattr(self, "reward_card_name", self.config.card_name)
        card_nodes = [
            node
            for node in observation.nodes
            if reward_card_name.lower() in node.searchable and node.bounds.area > 0
        ]
        for card in card_nodes:
            nearby_status = [
                node
                for node in observation.nodes
                if node.text in {"已使用", "已生效"}
                and node.bounds.top >= card.bounds.bottom - 5
                and node.bounds.top <= card.bounds.bottom + 180
                and node.bounds.right >= card.bounds.left
                and node.bounds.left <= card.bounds.right
            ]
            if nearby_status:
                return True
        content = " ".join(f"{node.text} {node.content_desc}" for node in observation.nodes)
        return "奖励已绑定" in content and "QQ账号" in content

    def _tap_text_button(self, *labels: str) -> bool:
        observation = self.observe()
        node = find_node(observation.nodes, *labels, clickable_only=True)
        if node is None:
            node = find_node(observation.nodes, *labels)
        if node is None:
            return False
        self._tap_node(node)
        time.sleep(2)
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

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        guarded = self._guard_generation(name)
        if guarded is not None:
            if name == "tap":
                self._daily_ability_experience_navigation = False
            return guarded
        try:
            if name == "tap":
                ability_experience_action = bool(
                    getattr(self, "_daily_ability_experience_navigation", False)
                )
                self._daily_ability_experience_navigation = False
                x, y = int(args["x"]), int(args["y"])
                if x >= self.width or y >= self.height or x < 0 or y < 0:
                    raise AgentError(f"坐标越界：{x},{y}")
                before_tap = self.observe()
                experience_navigation_expected = ability_experience_action and self._daily_ability_navigation_button_visible(
                    before_tap
                )
                covered = self._foreground_label_covering_tap(
                    before_tap, x, y, self.width, self.height
                )
                if covered is not None:
                    return self._result(
                        False,
                        f"视觉目标被前景交互层（{covered}）遮挡，请先根据最新截图关闭浮层",
                        stage=before_tap.stage,
                        visual_replan=True,
                    )
                if ability_experience_action:
                    resolved_x, resolved_y = x, y
                else:
                    resolved_x, resolved_y = self._resolve_tap_point(before_tap, x, y)
                if (resolved_x, resolved_y) != (x, y):
                    args.update({"x": resolved_x, "y": resolved_y})
                    x, y = resolved_x, resolved_y
                    print("视觉点击落在文字节点，已提升到当前层级的可点击父控件")
                was_welfare_context = self._welfare_context and not ability_experience_action
                # 任务入口点击后，下一次观测必须重新判定真实页面。若继续保留
                # 福利上下文，WebView 的空/滞后无障碍树会把目标页伪装成福利页，
                # 后续状态机就会在错误页面上重规划。
                if was_welfare_context:
                    self._welfare_context = False
                self._tap_point(x, y)
                if experience_navigation_expected:
                    after_tap = self._observe_after_daily_ability_action(
                        before_tap, navigation_expected=True
                    )
                else:
                    time.sleep(0.8)
                    after_tap = self.observe()
                # 福利页任务行动按钮是 WebView 的异步路由入口。短暂仍显示福利页
                # 不代表点击失败，必须给路由和新 Activity 一个有界加载窗口。
                if was_welfare_context:
                    after_tap = self._observe_after_entry_tap()
                stage = after_tap.stage
                if stage != "welfare":
                    self._welfare_context = False
                if stage == "welfare" and was_welfare_context:
                    return self._result(
                        False,
                        "任务入口点击后仍停留在福利中心，已等待异步导航仍未切换页面",
                        stage=stage,
                        visual_replan=True,
                    )
                return self._result(
                    True,
                    "已点击指定位置",
                    stage=stage,
                    activity=after_tap.activity,
                    entry_evidence=self._entry_evidence(after_tap),
                )
            if name == "long_press":
                observation = self.observe()
                x, y = int(args["x"]), int(args["y"])
                duration = max(600, min(10000, int(args.get("duration_ms", 2000))))
                if not (0 <= x < self.width and 0 <= y < self.height):
                    raise AgentError(f"长按坐标越界：{x},{y}")
                covered = self._foreground_label_covering_tap(
                    observation, x, y, self.width, self.height
                )
                if covered is not None:
                    return self._result(
                        False,
                        f"长按目标被前景交互层（{covered}）遮挡，请先处理遮挡层",
                        stage=observation.stage,
                        visual_replan=True,
                    )
                audio_session = None
                if args.get("_daily_ability_voice") is True:
                    audio_session = self._start_test_audio()
                    if audio_session is not None:
                        time.sleep(0.15)
                try:
                    self.device.input(
                        "swipe", str(x), str(y), str(x), str(y), str(duration)
                    )
                finally:
                    self._finish_test_audio(audio_session)
                self.pending_generation = True
                time.sleep(0.5)
                after = self.observe()
                return self._result(
                    True,
                    "已长按并释放页面控件，下一步调用 wait_5s 等待结果",
                    stage=after.stage,
                    audio_injected=audio_session is not None,
                )
            if name == "swipe":
                values = [int(args[key]) for key in ("x1", "y1", "x2", "y2")]
                if any(value < 0 for value in values):
                    raise AgentError("滑动坐标不能为负数")
                start = (min(values[0], self.width - 1), min(values[1], self.height - 1))
                end = (min(values[2], self.width - 1), min(values[3], self.height - 1))
                duration = max(100, min(2000, int(args.get("duration_ms", 500))))
                self.device.input("swipe", str(start[0]), str(start[1]), str(end[0]), str(end[1]), str(duration))
                time.sleep(1)
                return self._result(True, "已完成滑动", stage=self.observe().stage)
            if name == "press_back":
                self.device.input("keyevent", "KEYCODE_BACK")
                time.sleep(1)
                after_back = self.observe()
                return self._result(
                    True,
                    "已返回上一页",
                    stage=after_back.stage,
                    activity=after_back.activity,
                    entry_evidence=self._entry_evidence(after_back),
                )
            if name == "open_welfare":
                after = self._go_to_welfare()
                return self._result(True, "已进入福利中心", stage=after.stage)
            if name == "open_daily_ability":
                return self._open_daily_ability(args, self.observe())
            if name == "input_test_prompt":
                return self._input_fixed_prompt(self.observe())
            if name == "send_message":
                observation = self.observe()
                if observation.stage in {"chat", "writing"}:
                    input_node = self._find_input(observation)
                    input_text = input_node.text.strip() if input_node is not None else ""
                    variant = getattr(self, "prompt_variant", None)
                    if observation.stage == "writing" or variant:
                        if input_text != self._fixed_prompt().strip():
                            return self._result(
                                False,
                                "当前输入框内容与本轮固定测试文本不一致，已拒绝提交",
                                stage=observation.stage,
                            )
                    elif not input_text:
                        return self._result(False, "输入框为空，必须先调用 input_test_prompt")
                button = self._find_send(observation)
                if button is None:
                    if observation.stage == "writing":
                        return self._result(
                            False,
                            "写作表单中未找到相对输入框定位的提交控件，未执行点击",
                            stage=observation.stage,
                        )
                    input_node = self._find_input(observation)
                    if input_node is not None:
                        # 输入框通常延伸到屏幕底部；发送箭头位于其右下角，而不是输入框中心。
                        fallback_y = min(
                            self.height - 45,
                            max(self.height // 2, input_node.bounds.bottom - 35),
                        )
                    else:
                        fallback_y = self.height - 60
                    self._tap_point(self.width - 60, fallback_y)
                else:
                    self._tap_node(button)
                if observation.stage == "writing":
                    after = self.observe()
                    variant = getattr(self, "prompt_variant", None)
                    for _ in range(WRITING_SUBMIT_VERIFY_ATTEMPTS):
                        marker_visible = not variant or any(
                            variant in node.text for node in after.nodes
                        )
                        if after.stage == "chat" and marker_visible:
                            break
                        time.sleep(WRITING_SUBMIT_VERIFY_INTERVAL)
                        after = self.observe()
                    marker_visible = not variant or any(
                        variant in node.text for node in after.nodes
                    )
                    if after.stage != "chat" or not marker_visible:
                        return self._result(
                            False,
                            "写作表单提交后未观察到本轮消息进入会话，未标记为已发送",
                            stage=after.stage,
                        )
                    self.pending_generation = True
                    return self._result(
                        True,
                        "已确认本轮写作消息进入会话，下一步必须调用 wait_5s",
                        stage=after.stage,
                    )
                self.pending_generation = True
                return self._result(True, "已发送，下一步必须调用 wait_5s", stage=self.observe().stage)
            if name == "wait_5s":
                return self._wait_generation()
            if name == "select_local_image":
                return self._ensure_picker_image()
            if name == "confirm_image":
                reselected_from_camera = False
                for attempt in range(3):
                    if self._tap_text_button("确认", "使用", "完成"):
                        return self._result(True, "已确认图片", stage=self.observe().stage)
                    observation = self.observe()
                    if (
                        not reselected_from_camera
                        and observation.stage == "photo_question"
                        and "cameracaptureactivity" in observation.activity.lower()
                    ):
                        # 部分新版拍题页第一次从相册选图只更新左下角缩略图，
                        # 仍停留在相机页；再次从当前页的相册入口选图后才会
                        # 进入 CameraResultActivity 并暴露“确认”按钮。
                        reselected_from_camera = True
                        reselection = self._ensure_picker_image()
                        if not reselection.ok:
                            return reselection
                    elif attempt < 2:
                        time.sleep(1)
                return self._result(False, "没有找到图片确认按钮")
            if name == "claim_reward":
                for attempt in range(3):
                    observation = self.observe()
                    if self._dismiss_reward_popup(observation):
                        return self._result(True, "已收下任务奖励", stage=self.observe().stage)
                    if attempt < 2:
                        time.sleep(2)
                return self._result(True, "未发现奖励弹窗，视为无需领取", stage=self.observe().stage)
            if name == "return_to_welfare":
                after = self._go_to_welfare()
                # 奖励遮罩可能在生成完成数秒后才注入 WebView；返回福利页时
                # 再做一次有界本地收尾，避免下一任务的视觉定位看到奖励弹窗。
                for attempt in range(3):
                    if self._dismiss_reward_popup(after):
                        after = self.observe()
                        break
                    if attempt < 2:
                        time.sleep(2)
                        after = self.observe()
                return self._result(True, "已返回福利中心并刷新任务页面", stage=after.stage)
            if name == "open_exchange":
                return self._open_exchange()
            if name == "redeem_qq_card":
                return self._redeem_card()
            if name == "confirm_exchange":
                if not self.exchange_target_clicked:
                    return self._result(False, "尚未定位并点击目标商品，拒绝确认兑换")
                # 商品列表的“兑换”会先进入详情页；先点击详情页的“立即兑换”。
                clicked = self._tap_text_button("立即兑换")
                after = self.observe()
                # 某些版本在详情页后还会出现二次确认弹窗。
                if self._has_evidence(after, "确认兑换", "确定兑换"):
                    clicked = self._tap_text_button("确认兑换", "确定兑换", "确认") or clicked
                    after = self.observe()
                if not clicked and not self._has_exchange_success(after):
                    return self._result(False, "没有兑换确认弹窗或成功提示", stage=after.stage)
                if not self._has_exchange_success(after):
                    return self._result(False, "兑换后没有发现明确成功证据", stage=after.stage)
                self.exchange_confirmed = True
                self._write_state("pending")
                return self._result(True, "已确认兑换且发现成功证据", stage=after.stage)
            if name == "open_prize_records":
                if not self.exchange_confirmed:
                    return self._result(False, "兑换尚未确认成功")
                clicked = self._tap_text_button("奖品记录", "前往奖品记录")
                after = self.observe()
                if not clicked and after.stage != "prize_records":
                    self._tap_point(self.width - 90, 148)
                    time.sleep(2)
                    after = self.observe()
                if after.stage != "prize_records" and not self._has_evidence(after, "奖品记录"):
                    return self._result(False, "没有进入奖品记录页面", stage=after.stage)
                self.prize_records_opened = True
                return self._result(True, "已打开奖品记录", stage=after.stage)
            if name == "use_bound_reward":
                if not self.prize_records_opened:
                    return self._result(False, "尚未打开奖品记录")
                button = self._find_reward_use_button(self.observe())
                if button is None:
                    return self._result(False, "奖品记录中没有找到目标卡片的使用按钮")
                self._tap_node(button)
                time.sleep(2)
                self.reward_use_clicked = True
                return self._result(True, "已点击绑定账号使用入口", stage=self.observe().stage)
            if name == "confirm_reward_use":
                if not self.reward_use_clicked:
                    return self._result(False, "尚未点击目标卡片的绑定账号使用入口")
                self._tap_text_button("立即使用", "确认使用", "确定", "确认")
                time.sleep(2)
                after = self.observe()
                evidence = self._has_reward_use_success(after)
                self.reward_use_confirmed = evidence
                if not evidence:
                    return self._result(False, "尚未发现绑定账号使用成功的页面证据", stage=after.stage)
                self._write_state("used")
                return self._result(True, "已确认卡片对绑定账号使用成功", stage=after.stage)
            if name == "return_to_home":
                after = self._go_to_ours()
                destination = "新版聊天首页" if after.stage == "chat" else "旧版“我们”页面"
                return self._result(True, f"已返回元宝{destination}", stage=after.stage)
            if name == "report_tasks":
                return self._result(True, "已记录模型观察到的任务进度")
            if name == "complete_task":
                return self._result(True, "完成工具调用已收到")
            return self._result(False, f"未知工具：{name}")
        except (KeyError, TypeError, ValueError) as exc:
            return self._result(False, f"工具参数错误：{type(exc).__name__}")
        except ManualActionRequired:
            # 登录和协议确认不能靠重试解决，也不能被降级成普通工具失败。
            raise
        except AgentError as exc:
            return self._result(False, str(exc)[:240])
