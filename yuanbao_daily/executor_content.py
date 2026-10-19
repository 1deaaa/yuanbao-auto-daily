"""内容层：固定测试文本、测试图片、生成等待与能力体验语音注入。"""

from __future__ import annotations

from typing import Any
from typing import Any, Iterable
from pathlib import Path
import contextlib
import os
import shutil
import subprocess
import time

from .constants import (
    DAILY_ABILITY_BUTTON_X_RATIO,
    DAILY_ABILITY_BUTTON_Y_RATIO,
    DAILY_ABILITY_CARD_MAX_Y_RATIO,
    DAILY_ABILITY_CARD_MIN_Y_RATIO,
    DAILY_ABILITY_EXPERIENCE_NAVIGATION_MARKERS,
    DAILY_ABILITY_EXPERIENCE_NAVIGATION_POLL_INTERVAL,
    DAILY_ABILITY_EXPERIENCE_NAVIGATION_TIMEOUT,
)
from .errors import (
    AgentError,
)
from .models import (
    Node,
    Observation,
    ToolResult,
)
from .ui import (
    encode_adb_input_text,
    find_node,
    node_interaction_bounds,
)
from .welfare import (
    is_daily_ability_experience_visible,
)


class ExecutorContentMixin:
    """按职责拆分的混入类；方法实现与原主类完全一致。"""

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


    def _fixed_prompt(self) -> str:
        value = self.config.test_prompt
        variant = getattr(self, "prompt_variant", None)
        return f"{value} {variant}" if variant else value


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


    def _guard_generation(self, tool_name: str) -> ToolResult | None:
        if self.pending_generation and tool_name != "wait_5s":
            return self._result(False, "消息仍可能在生成，必须先调用 wait_5s 等待右下角终止按钮消失")
        return None


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
