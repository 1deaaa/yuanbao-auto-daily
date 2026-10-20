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
from .workflow import Workflow
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
from .runner_loop import run_task_loop
from .runner_startup import Session, start_session
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
    """跑一轮每日任务：启动会话 → 执行任务 → 收尾并释放设备。"""
    runtime = WaydroidRuntime()
    completed = False
    startup_ready = False
    manual_action_required = False
    cleanup_after_transport_failure = False
    session: Session | None = None
    device: Device | None = None
    previous_stay_awake: str | None = None
    try:
        # 启动阶段最多恢复一次。所有后续任务步骤仍由同一套工作流和模型历史处理。
        session = start_session(config, runtime)
        device = session.device
        previous_stay_awake = session.previous_stay_awake
        startup_ready = True
        # 主循环在独立模块中执行，页面语义、工具契约与设备操作各归其位。
        completed = run_task_loop(
            config, session.executor, session.workflow, session.model
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
