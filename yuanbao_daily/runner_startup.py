"""启动阶段：设备发现、应用拉起、进入福利中心与一次性恢复。

启动只允许一次整体恢复；恢复后仍由同一套工作流和模型历史继续处理，
避免把“设备没起来”和“页面任务失败”混成同一种错误。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from .config import Config
from .constants import (
    PACKAGE_NAME,
    VISUAL_NAVIGATION_MAX_STEPS,
    VISUAL_NAVIGATION_TOOLS,
    VISUAL_REPLAN_MAX_ATTEMPTS,
)
from .device import Device
from .waydroid_runtime import WaydroidRuntime
from .errors import AgentError, AppUnresponsiveError, ManualActionRequired, RetryLimitExceeded
from .executor import ToolExecutor
from .models import Observation
from .vision import VisionModel
from .workflow import Workflow


class Session:
    """一次运行所需的设备、执行器、编排器与模型。"""

    def __init__(
        self,
        device: Device,
        executor: ToolExecutor,
        workflow: Workflow,
        model: VisionModel,
        previous_stay_awake: str | None,
    ):
        self.device = device
        self.executor = executor
        self.workflow = workflow
        self.model = model
        self.previous_stay_awake = previous_stay_awake


def start_session(config: Config, runtime: WaydroidRuntime) -> Session:
    """拉起设备与应用并进入福利中心；失败时按有界策略恢复一次。"""
    startup_attempt = 0
    while True:
        device: Device | None = None
        workflow: Workflow | None = None
        previous_stay_awake: str | None = None
        try:
            device = runtime.discover_device(config.device)
            print(f"设备={device.serial} 模型={config.model_id} 端点={config.base_url}")
            if runtime.managed:
                # 后台会话没有可见窗口，Android 屏幕超时会通过 Waydroid hardware 服务冻结容器。
                previous_stay_awake = device.keep_awake()
                # Waydroid 的 app launch 会登记活动应用；直接 am start 会让 suspend_action=freeze
                # 把后台会话冻结，随后所有 ADB 操作都会超时。
                runtime.launch_app(device, PACKAGE_NAME)
                app_launcher = lambda package_name: runtime.launch_app(device, package_name)
            else:
                app_launcher = device.launch_app
                device.launch_app(PACKAGE_NAME)
            # am start 返回只代表 Intent 已提交；必须等启动窗口消失，防止读取旧无障碍树。
            device.wait_app_ready(PACKAGE_NAME)
            # 登录态页面会同时依赖 HTTPS 和 TIM 长连接；先确认 Android 默认网络已验证，
            # 避免把刚启动时的瞬态断网误报成登录态失效或交给视觉模型反复点击。
            device.wait_network_ready()
            executor = ToolExecutor(config, device, app_launcher=app_launcher)
            workflow = Workflow(config, executor)
            workflow.restore_state()
            model = VisionModel(config)
            # 启动后的首个页面入口也交给 VLMM；页面文案、入口位置和返回路径
            # 都可能随 WebView 改版变化，本地不再复用福利入口坐标。
            startup_observation = executor.observe_startup()
            if startup_observation.stage == "welfare":
                if workflow.terminal_restore:
                    # 本地状态显示全部完成时，也要先读一次福利页任务目录，
                    # 确认没有把没做的任务误记成已完成。
                    workflow.phase = "report"
                elif workflow.daily_ability_done:
                    # 同日恢复仍重新读取任务目录，页面改版或任务移除不沿用旧 target。
                    workflow.phase = "report"
                else:
                    workflow.phase = "daily_ability"
                print("步骤 0：已在福利中心，跳过入口点击")
            else:
                _navigate_to_welfare(
                    config, runtime, executor, workflow, model, startup_observation
                )
            return Session(device, executor, workflow, model, previous_stay_awake)
        except ManualActionRequired:
            raise
        except AgentError as exc:
            if startup_attempt >= 1 or not runtime.managed:
                raise RetryLimitExceeded(
                    f"启动阶段恢复失败，已达到本轮最大恢复次数：{exc}",
                    phase=getattr(workflow, "phase", "navigate_welfare"),
                    target=getattr(workflow, "target", None) or "",
                    step=0,
                    failures=startup_attempt + 1,
                ) from exc
            startup_attempt += 1
            print(f"启动阶段失败，正在执行第 {startup_attempt} 次恢复：{str(exc)[:180]}")
            if device is not None and previous_stay_awake is not None:
                with contextlib.suppress(AgentError):
                    device.restore_keep_awake(previous_stay_awake)
            previous_stay_awake = None
            if runtime.session_started or isinstance(exc, AppUnresponsiveError):
                if not runtime.stop():
                    raise AgentError("启动失败后无法停止 Waydroid 会话") from exc
            elif device is not None:
                # 会话由用户或其它服务管理时不停止会话，只清理元宝坏进程再重试一次。
                with contextlib.suppress(AgentError):
                    device.adb("shell", "am", "force-stop", PACKAGE_NAME, timeout=20)


def _navigate_to_welfare(
    config: Config,
    runtime: WaydroidRuntime,
    executor: ToolExecutor,
    workflow: Workflow,
    model: VisionModel,
    startup_observation: Observation,
) -> None:
    """从启动页进入福利中心；语义明确时走本地导航，否则由模型决策。"""
    navigation_observation = startup_observation
    navigation_replans = 0
    navigation_replan_reason = ""
    local_open_attempted = False
    for navigation_step in range(1, VISUAL_NAVIGATION_MAX_STEPS + 1):
        navigation_context = (
            f"{workflow.context(navigation_observation)}\n"
            "这是启动后的视觉导航阶段，请根据最新截图进入福利中心。"
            "只允许从 tap、swipe、press_back 中选择；不要调用 open_welfare。"
            "如果页面有“返回福利中心”或等价入口，优先点击；目标不在当前视口时先滑动。"
        )
        if navigation_replans:
            navigation_context += (
                "上一视觉动作没有达到目标，失败原因是："
                f"{navigation_replan_reason or '页面状态未按预期变化'}。"
                "请只依据这次最新截图重新规划，禁止重复相同动作或相同位置；"
                "若有前景浮层先关闭，目标不可见时先滑动，只有确认进入福利中心后才算成功。"
            )
        semantic_action = workflow.semantic_visual_recovery_action(
            navigation_observation
        )
        if semantic_action is not None:
            # 当前无障碍树已经给出明确入口时，启动阶段也走本地
            # 语义导航；只有没有可靠语义证据时才请求视觉模型。
            name, args = semantic_action
            call_id = None
            action_source = "本地语义"
        elif not local_open_attempted:
            # 冷启动页面未暴露入口节点时，先复用执行器的语义导航，
            # 避免把常规启动流程交给随机性更高的视觉工具参数。
            name, args, call_id = "open_welfare", {}, None
            action_source = "本地语义"
            local_open_attempted = True
        else:
            name, args, call_id = model.next_tool(
                navigation_observation,
                navigation_context,
                allowed_tools=VISUAL_NAVIGATION_TOOLS,
            )
            action_source = "视觉模型"
        result = workflow.dispatch(name, args, navigation_observation)
        if call_id is None:
            model.record_local_tool_result(
                name, args, navigation_observation, navigation_context, result
            )
        else:
            model.record_tool_result(call_id, result)
        print(
            f"步骤 0.{navigation_step}：工具={name} 来源={action_source} "
            f"结果={'成功' if result.ok else '失败'}；{result.message[:180]}"
        )
        if workflow.phase != "navigate_welfare":
            return
        if not result.ok:
            navigation_replans += 1
            navigation_replan_reason = result.message[:240]
            if navigation_replans >= VISUAL_REPLAN_MAX_ATTEMPTS:
                raise AgentError(
                    f"启动视觉导航连续重规划 {navigation_replans} 次仍未进入福利中心：{result.message}"
                )
        else:
            navigation_replans = 0
            navigation_replan_reason = ""
        navigation_observation = executor.observe()
    raise AgentError(
        f"启动视觉导航达到 {VISUAL_NAVIGATION_MAX_STEPS} 步，仍未进入福利中心"
    )
