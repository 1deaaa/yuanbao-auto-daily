"""运行入口：装配设备、编排器与模型，并驱动一轮完整任务。"""

from __future__ import annotations

from pathlib import Path
import argparse
import contextlib
import json
import sys
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .device import is_adb_transport_failure
from .config import (
    Config,
    parse_run_time,
)
from .constants import (
    DAILY_ABILITY_EXPERIENCE_TOOLS,
    DAILY_ABILITY_RULE_NAME,
    DAILY_ABILITY_RULE_VERSION,
    DAILY_ABILITY_TOOLS,
    FAILURE_EXIT_STATUS,
    LOCAL_DETERMINISTIC_TOOLS,
    PACKAGE_NAME,
    ROOT,
    STABLE_MISSING_TASK_SCROLLS,
    VISUAL_EXCHANGE_TOOLS,
    VISUAL_NAVIGATION_MAX_STEPS,
    VISUAL_NAVIGATION_TOOLS,
    VISUAL_REPLAN_MAX_ATTEMPTS,
)
from .executor import ToolExecutor
from .device import (
    Device,
    WaydroidRuntime,
)
from .errors import (
    AgentError,
    AppUnresponsiveError,
    ManualActionRequired,
    ModelAccessError,
    RetryLimitExceeded,
)
from .models import (
    Observation,
    ToolResult,
)
from .reporting import (
    build_failure_report,
    show_failure_alert,
)
from .vision import VisionModel
from .welfare import (
    daily_ability_card_status,
    is_daily_ability_experience_visible,
)
from .ui import (
    action_signature,
    repeated_action_is_stuck,
    state_signature,
)

def run_once(config: Config) -> None:
    runtime = WaydroidRuntime()
    completed = False
    startup_ready = False
    manual_action_required = False
    cleanup_after_transport_failure = False
    device: Device | None = None
    executor: ToolExecutor | None = None
    workflow: Workflow | None = None
    model: VisionModel | None = None
    previous_stay_awake: str | None = None
    startup_attempt = 0
    try:
        # 启动阶段最多恢复一次。所有后续任务步骤仍由同一套工作流和模型历史处理。
        while True:
            try:
                device = runtime.discover_device(config.device)
                print(f"设备={device.serial} 模型={config.model_id} 端点={config.base_url}")
                if runtime.managed:
                    # 后台会话没有可见窗口，Android 屏幕超时会通过 Waydroid hardware 服务冻结容器。
                    previous_stay_awake = device.keep_awake()
                if runtime.managed:
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
                        workflow.phase = "return_home"
                    elif workflow.daily_ability_done:
                        # 同日恢复仍重新读取任务目录，页面改版或任务移除不沿用旧 target。
                        workflow.phase = "report"
                    else:
                        workflow.phase = "daily_ability"
                    print("步骤 0：已在福利中心，跳过入口点击")
                else:
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
                            break
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
                    else:
                        raise AgentError(
                            f"启动视觉导航达到 {VISUAL_NAVIGATION_MAX_STEPS} 步，仍未进入福利中心"
                        )
                startup_ready = True
                break
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
                device = None
                executor = None
                workflow = None
                model = None
        failures = 0
        previous_state: str | None = None
        previous_action: str | None = None
        recent_action: dict[str, Any] | None = None
        visual_replan_attempts = 0
        visual_replan_phase: tuple[str, str | None] | None = None
        visual_replan_reason = ""
        stable_missing_scrolls = 0
        last_scroll_catalog_signature: tuple[tuple[Any, ...], ...] | None = None
        last_visual_action: str | None = None
        for step in range(1, config.max_steps + 1):
            observation = executor.observe()
            if workflow.phase == "daily_ability":
                local_card_status = daily_ability_card_status(observation)
                if local_card_status == "已完成":
                    workflow.daily_ability_done = True
                    workflow.daily_ability_evidence = DAILY_ABILITY_RULE_NAME
                    workflow._persist_task_state()
                    workflow.phase = "report"
                    print("本地已确认主轮播卡固定区域显示‘已完成’，跳过重复体验")
                    continue
            if workflow.phase == "daily_ability_experience":
                if getattr(executor, "pending_generation", False):
                    workflow.daily_ability_experience_opened = True
                elif is_daily_ability_experience_visible(observation):
                    workflow.daily_ability_experience_opened = True
                elif workflow.daily_ability_experience_opened:
                    workflow.phase = "verify_daily_ability"
                    print("体验操作已返回福利中心，进入主轮播卡完成状态核验")
            if workflow.phase == "report":
                prepare_report = getattr(executor, "prepare_welfare_task_report", None)
                if callable(prepare_report):
                    observation = prepare_report(observation)
            current_state = state_signature(observation.ui, observation.image, observation.activity)
            catalog_signature = workflow.task_catalog_signature(observation)
            phase_key = (workflow.phase, workflow.target)
            if phase_key != visual_replan_phase:
                visual_replan_attempts = 0
                visual_replan_reason = ""
                stable_missing_scrolls = 0
                last_scroll_catalog_signature = None
                last_visual_action = None
                visual_replan_phase = phase_key
            elif last_visual_action == "swipe":
                if workflow.can_conclude_target_unavailable(observation):
                    # WebView 滚动后常有一两次空无障碍树；只要最新完整目录仍
                    # 明确没有目标，就累计稳定缺失次数，不让瞬态空树重新开始。
                    stable_missing_scrolls += 1
                    if catalog_signature:
                        last_scroll_catalog_signature = catalog_signature
                elif observation.nodes:
                    # 目录已经完整暴露但目标重新出现或页面语义改变，重新观察。
                    stable_missing_scrolls = 0
            if (
                stable_missing_scrolls >= STABLE_MISSING_TASK_SCROLLS
                and workflow.can_conclude_target_unavailable(observation)
            ):
                workflow.skip_unavailable_target()
                failures = 0
                previous_state, previous_action = current_state, None
                stable_missing_scrolls = 0
                last_scroll_catalog_signature = None
                last_visual_action = None
                continue
            if workflow.reconcile_return_to_welfare(observation):
                failures = 0
                visual_replan_attempts = 0
                visual_replan_reason = ""
                stable_missing_scrolls = 0
                last_scroll_catalog_signature = None
                last_visual_action = None
                previous_state, previous_action = current_state, None
                continue
            if workflow.reconcile_visible_progress(observation):
                failures = 0
                visual_replan_attempts = 0
                visual_replan_reason = ""
                stable_missing_scrolls = 0
                last_scroll_catalog_signature = None
                last_visual_action = None
                previous_state, previous_action = current_state, None
                print(f"步骤 {step}：已根据福利页面的可见进度恢复当前任务")
                continue
            try:
                # 本地状态已明确的阶段/子步骤强制唯一工具；入口点击等仍由模型定位。
                # 卡住恢复期间不强制工具，让 VLM 看最新截图自由决策。
                forced_tool = workflow.expected_tool(observation)
                context = workflow.context(observation)
                if workflow.recovering:
                    context = f"{context}\n{workflow.recovery_hint()}"
                    print(
                        f"步骤 {step}：进入卡住恢复，VLM 自由决策 "
                        f"（阶段={workflow.phase}，目标={workflow.target or '无'}）"
                    )
                visual_navigation = workflow.requires_visual_navigation()
                cached_action = (
                    None
                    if workflow.recovering or visual_navigation
                    else workflow.cached_action(observation)
                )
                if visual_navigation:
                    # 页面入口和模板按钮的位置由 VLMM 根据本轮最新截图决定；
                    # 不使用历史缓存，也不强制 tap，让模型在目标离屏时选择 swipe。
                    visual_context = (
                        f"{context}\n本轮是视觉导航；只能从 tap、swipe、press_back 中选择，"
                        "禁止再次调用 open_welfare 或使用历史坐标。"
                    )
                    if visual_replan_attempts:
                        visual_context += (
                            "上一视觉动作没有达到目标，失败原因是："
                            f"{visual_replan_reason or '页面状态未按预期变化'}。"
                            "请只依据这次最新截图重新规划，禁止重复相同动作或相同位置；"
                            "目标不可见时先滑动，浮层遮挡时先关闭浮层，点击后必须等待并确认页面阶段变化。"
                        )
                    if workflow.phase == "return":
                        visual_context += (
                            "当前目标是返回福利中心；优先按截图点击左上角抽屉入口，再点侧栏“福利中心”；"
                            "只有没有抽屉入口时才尝试顶部返回链接或旧版“我们”入口。"
                            "只有真正进入福利页后才算成功。"
                        )
                    allowed_visual_tools = VISUAL_NAVIGATION_TOOLS
                    if workflow.phase == "open_exchange" and workflow.substate.get("entry_clicked"):
                        allowed_visual_tools = VISUAL_EXCHANGE_TOOLS
                    semantic_recovery_needed = bool(visual_replan_attempts)
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
                        call_id = None
                    else:
                        name, args, call_id = model.next_tool(
                            observation,
                            visual_context,
                            forced_tool=None,
                            allowed_tools=allowed_visual_tools,
                        )
                    used_cached_action = False
                elif cached_action is not None:
                    # 同一任务的第 2、3 次通常复用同一入口；页面语义变化时
                    # cached_action 会失效，下面仍会回到视觉模型定位。
                    name, args = cached_action
                    call_id = None
                    used_cached_action = True
                elif workflow.phase == "daily_ability" and not workflow.recovering:
                    ability_context = (
                        f"{context}\n如果当前是普通福利主页面，只允许调用 open_daily_ability；"
                        "主轮播卡固定位置明确显示‘去体验’时传 button_visible=true，否则传 false。"
                        "如果最新截图已显示点击后打开的体验浮层或体验页面，则调用 continue_daily_ability 并传"
                        " experience_visible=true，把当前页面交给模型处理。不要识别标题/颜色，也不要滑动换卡。"
                    )
                    name, args, call_id = model.next_tool(
                        observation,
                        ability_context,
                        allowed_tools=DAILY_ABILITY_TOOLS,
                    )
                    used_cached_action = False
                elif (
                    workflow.phase == "daily_ability_experience"
                    and not workflow.recovering
                    and forced_tool is None
                ):
                    experience_context = (
                        f"{context}\n你接手了主轮播卡当前打开的体验页面。按页面实际要求完成互动，"
                        "只使用当前截图中可见的控件；如果页面明确要求按住控件（例如语音输入），"
                        "调用 long_press 并按要求设置时长，然后等待结果。"
                        "体验可能只是同一福利 WebView 中的浮层（例如出现‘去对话’按钮），此时仍要按截图继续操作，"
                        "不能因为阶段名称是 welfare 就直接核验或返回。完成后再返回福利中心，不要打开其它福利卡片。"
                    )
                    voice_action = workflow.daily_ability_voice_action(observation)
                    if voice_action is not None:
                        name, args = voice_action
                        call_id = None
                        print("能力体验检测到‘按住说话’，执行本地必做长按")
                    else:
                        name, args, call_id = model.next_tool(
                            observation,
                            experience_context,
                            allowed_tools=DAILY_ABILITY_EXPERIENCE_TOOLS,
                        )
                    used_cached_action = False
                elif forced_tool in LOCAL_DETERMINISTIC_TOOLS:
                    # 输入、发送、等待、返回和兑换都有可验证的本地前置/后置条件；
                    # 直接执行可以显著减少重复视觉请求，同时把结果写入同轮历史。
                    name, args, call_id = forced_tool, {}, None
                    used_cached_action = False
                elif forced_tool == "report_tasks":
                    # 进度文字和页面语义也属于易变 UI，报告由 VLMM 读取最新截图；
                    # 本地解析器只用于已有任务状态的安全校验，不替代视觉判断。
                    name, args, call_id = model.next_tool(
                        observation,
                        f"{context}\n本阶段唯一允许调用 {forced_tool}；不得返回滑动、点击或其它工具。",
                        forced_tool=forced_tool,
                        allowed_tools=frozenset({forced_tool}),
                    )
                    used_cached_action = False
                else:
                    forced_context = context
                    if forced_tool:
                        forced_context += (
                            f"\n本阶段唯一允许调用 {forced_tool}；若目标卡或完成状态未显示，"
                            "仍须使用该工具按 schema 报告未显示，不得改用其它工具。"
                        )
                    name, args, call_id = model.next_tool(
                        observation,
                        forced_context,
                        forced_tool=forced_tool,
                        allowed_tools=(
                            frozenset({forced_tool}) if forced_tool else None
                        ),
                    )
                    used_cached_action = False
            except ModelAccessError as exc:
                failures += 1
                raise RetryLimitExceeded(
                    f"视觉模型服务不可用：{str(exc)[:300]}",
                    phase=workflow.phase,
                    target=workflow.target or "",
                    step=step,
                    failures=failures,
                    recent_action=recent_action,
                    observation=observation,
                ) from exc
            except AgentError as exc:
                failures += 1
                print(f"步骤 {step}：模型动作解析失败（{str(exc)[:160]}）")
                # 模型调用失败同样先给一次 VLM 恢复机会：很可能是页面未到位或
                # 瞬态问题，恢复提示会让模型重新观察最新截图再决策。
                if failures >= config.max_retries and workflow.should_enter_recovery():
                    workflow.note_recovery_attempt()
                    print(
                        f"步骤 {step}：模型调用连续 {failures} 次失败，熔断前转 VLM 自由决策恢复一次 "
                        f"（阶段={workflow.phase}，目标={workflow.target or '无'}）"
                    )
                    if config.retry_cooldown_seconds:
                        time.sleep(config.retry_cooldown_seconds)
                    continue
                if failures >= config.max_retries:
                    raise RetryLimitExceeded(
                        f"视觉模型动作连续失败 {failures} 次，达到最大重试次数",
                        phase=workflow.phase,
                        target=workflow.target or "",
                        step=step,
                        failures=failures,
                        recent_action=recent_action,
                        observation=observation,
                    ) from exc
                time.sleep(config.retry_cooldown_seconds)
                continue
            recent_action = {"tool": name, "arguments": args}
            signature = action_signature(name, args)
            # 卡住恢复动作不受“同画面重复”拦截：恢复本就是在同一卡住画面上
            # 换手段再试一次（换坐标重 tap / swipe / press_back），拦截它就等于
            # 堵死恢复。恢复只允许一次，后续仍受正常计数与熔断保护。
            if workflow.recovering:
                result = workflow.dispatch(name, args, observation)
                failures = failures + 1 if not result.ok else 0
                previous_state, previous_action = current_state, signature
            elif repeated_action_is_stuck(
                name,
                signature,
                current_state,
                previous_action,
                previous_state,
            ):
                failures += 1
                result = ToolResult(False, "页面状态未变化且重复同一动作，已拒绝循环点击")
            else:
                result = workflow.dispatch(name, args, observation)
                failures = failures + 1 if not result.ok else 0
            if used_cached_action and not result.ok:
                workflow.invalidate_cached_action()
                # 缓存失败后允许下一轮重新执行同名的视觉点击；否则循环保护会把
                # 这次恢复误判成模型重复点击。
                previous_state, previous_action = None, None
                print("缓存入口校验失败，已清除坐标并交给视觉模型重新定位")
            else:
                previous_state, previous_action = current_state, signature
            if call_id is None:
                model.record_local_tool_result(name, args, observation, context, result)
            else:
                model.record_tool_result(call_id, result)
            safe_args = (
                json.dumps(args, ensure_ascii=False, sort_keys=True)
                if name in {"report_tasks", "tap", "swipe"}
                else ""
            )
            source = "缓存" if used_cached_action else ("本地" if call_id is None else "视觉模型")
            print(
                f"步骤 {step}：工具={name} 来源={source} {safe_args} "
                f"结果={'成功' if result.ok else '失败'}；{result.message[:180]}"
            )
            if visual_navigation and result.ok:
                visual_replan_attempts = 0
                visual_replan_reason = ""
                if name == "swipe" and workflow.phase == "open_target":
                    last_scroll_catalog_signature = catalog_signature
                    last_visual_action = "swipe"
                else:
                    stable_missing_scrolls = 0
                    last_scroll_catalog_signature = None
                    last_visual_action = None
            if workflow.finished:
                completed = True
                return
            # 视觉导航失败是感知/规划问题，不应和 ADB、输入或兑换错误共用
            # MAX_RETRIES。把失败原因带回下一轮最新截图，让 VLMM 自己换策略。
            if (
                visual_navigation
                and name in VISUAL_NAVIGATION_TOOLS
                and not result.ok
            ):
                visual_replan_attempts += 1
                visual_replan_reason = result.message[:240]
                failures = 0
                previous_state, previous_action = None, None
                last_visual_action = None
                last_scroll_catalog_signature = None
                if visual_replan_attempts >= VISUAL_REPLAN_MAX_ATTEMPTS:
                    raise RetryLimitExceeded(
                        f"视觉导航连续重规划 {visual_replan_attempts} 次仍未达到目标",
                        phase=workflow.phase,
                        target=workflow.target or "",
                        step=step,
                        failures=visual_replan_attempts,
                        recent_action={
                            **(recent_action or {}),
                            "result": result.message[:500],
                        },
                        observation=observation,
                    )
                print(
                    f"步骤 {step}：视觉导航未达目标，交回 VLMM 第 {visual_replan_attempts}/"
                    f"{VISUAL_REPLAN_MAX_ATTEMPTS} 次重规划"
                )
                if config.retry_cooldown_seconds:
                    time.sleep(config.retry_cooldown_seconds)
                continue
            if not result.ok:
                # 熔断前先给 VLM 一次自由决策恢复机会：意外弹窗、页面未到位等
                # 未被程序覆盖的情况，让模型看最新截图自己决定（关弹窗/滑动/
                # 重 tap/返回等），而不是连续失败几次就直接死。同一卡点只恢复
                # 一次；恢复动作本身的成败仍计入 failures，失败则正常熔断。
                if failures >= config.max_retries and workflow.should_enter_recovery():
                    workflow.note_recovery_attempt()
                    print(
                        f"步骤 {step}：连续 {failures} 次失败，熔断前转 VLM 自由决策恢复一次 "
                        f"（阶段={workflow.phase}，目标={workflow.target or '无'}）"
                    )
                    if config.retry_cooldown_seconds:
                        time.sleep(config.retry_cooldown_seconds)
                    continue
                if failures >= config.max_retries:
                    raise RetryLimitExceeded(
                        f"连续 {failures} 次工具/状态失败，达到最大重试次数",
                        phase=workflow.phase,
                        target=workflow.target or "",
                        step=step,
                        failures=failures,
                        recent_action={
                            **(recent_action or {}),
                            "result": result.message[:500],
                        },
                        observation=observation,
                    )
                if config.retry_cooldown_seconds:
                    time.sleep(config.retry_cooldown_seconds)
        raise RetryLimitExceeded(
            f"达到单轮最大步骤数 {config.max_steps}，未完成每日任务",
            phase=workflow.phase,
            target=workflow.target or "",
            step=config.max_steps,
            failures=failures,
            recent_action=recent_action,
            observation=observation,
        )
    except ManualActionRequired:
        # 保留协议、登录或 ANR 现场供用户处理；退出码 75 会阻止 systemd 自动重启。
        manual_action_required = True
        raise
    except AgentError as exc:
        if is_adb_transport_failure(exc):
            cleanup_after_transport_failure = True
            print("检测到 ADB/Waydroid 传输故障，本轮结束前清理会话并交给 systemd 重试")
        raise
    finally:
        if (
            device is not None
            and previous_stay_awake is not None
            and not manual_action_required
        ):
            with contextlib.suppress(AgentError):
                device.restore_keep_awake(previous_stay_awake)
        elif manual_action_required:
            # 保持现场可见且避免 Waydroid 立即冻结，待用户处理协议/登录/ANR 后下次成功运行再恢复设置。
            print("已保留 Waydroid 人工处理现场")
        if config.stop_waydroid_after_run and runtime.managed and (
            completed
            or (
                not startup_ready
                and not manual_action_required
                and runtime.session_started
            )
            or (cleanup_after_transport_failure and runtime.session_started)
        ):
            if completed:
                print("每日任务已完成，正在关闭 Waydroid 以释放内存")
            elif cleanup_after_transport_failure:
                print("ADB/Waydroid 传输故障，正在关闭本轮会话以便干净重试")
            else:
                # 首步导航失败时清理本轮会话，避免 systemd 重试继续复用卡死页面。
                print("每日任务尚未进入福利中心，正在关闭 Waydroid 以清理启动现场")
            if not runtime.stop():
                print("警告：Waydroid 未确认完全停止，下次运行前仍会再次尝试启动")


def next_run_at(config: Config, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(config.timezone_name)
    if now is None:
        current = datetime.now(tz)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=tz)
    else:
        current = now.astimezone(tz)
    hour, minute = parse_run_time(config.run_time)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return target


def run_daemon(config: Config) -> None:
    tz = ZoneInfo(config.timezone_name)
    while True:
        target = next_run_at(config)
        seconds = max(0.5, (target - datetime.now(tz)).total_seconds())
        print(f"下一次运行：{target.isoformat()}")
        time.sleep(seconds)
        started = datetime.now(tz)
        print(f"开始每日任务：{started.isoformat()}")
        try:
            run_once(config)
            print("本轮完成，进入下一轮等待")
        except RetryLimitExceeded as exc:
            print("本轮达到最大重试次数，打开失败报告窗口；关闭后继续等待下一次计划时间", file=sys.stderr)
            show_failure_alert(config, exc)
        except Exception as exc:
            print(f"本轮失败：{type(exc).__name__}；下一次计划时间仍按配置计算")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="元宝每日任务视觉代理")
    parser.add_argument("--once", action="store_true", help="立即运行一轮")
    parser.add_argument("--daemon", action="store_true", help="按 .env 中的北京时间运行时间持续调度")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)
    config: Config | None = None
    try:
        config = Config.from_env(args.env_file)
        if args.daemon:
            run_daemon(config)
        else:
            run_once(config)
        return 0
    except KeyboardInterrupt:
        print("已中止。", file=sys.stderr)
        return 130
    except ManualActionRequired as exc:
        print(f"需要人工处理：{exc}", file=sys.stderr)
        # 75 由 systemd 的 RestartPreventExitStatus 识别，避免重复拉起同一阻塞页面。
        return 75
    except RetryLimitExceeded as exc:
        print(f"失败：{type(exc).__name__}；{str(exc)[:300]}", file=sys.stderr)
        show_failure_alert(config, exc)
        # 76 配合 systemd 的 RestartPreventExitStatus，避免窗口关闭后再次自动重试。
        return FAILURE_EXIT_STATUS
    except Exception as exc:
        print(f"失败：{type(exc).__name__}；{str(exc)[:300]}", file=sys.stderr)
        return 1
if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
