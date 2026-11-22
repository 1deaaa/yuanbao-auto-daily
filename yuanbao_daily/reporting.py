"""失败报告生成与置顶告警窗口；报告内容需脱敏。"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .constants import DEFAULT_STATE_PATH
from .config import Config
from .constants import DEFAULT_TIMEZONE
from .state import read_daily_state

def _redact_failure_text(value: Any, config: Config | None) -> str:
    """错误报告只保留诊断信息，避免把密钥或环境变量中的敏感值带入窗口。"""
    text = str(value)
    secrets = {
        getattr(config, "api_key", ""),
        os.environ.get("API_KEY", ""),
        os.environ.get("LOCAL_LLM_API_KEY", ""),
    }
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<已隐藏>")
    return text


def build_failure_report(config: Config | None, error: BaseException) -> str:
    """生成可直接放入 Tkinter 和 journal 的完整失败报告。"""
    timezone_name = getattr(config, "timezone_name", DEFAULT_TIMEZONE)
    try:
        now = datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, ValueError):
        now = datetime.now()
    observation = getattr(error, "observation", None)
    recent_action = getattr(error, "recent_action", None)
    state_path = getattr(config, "state_path", DEFAULT_STATE_PATH)
    state = read_daily_state(state_path)
    lines = [
        "元宝每日任务失败报告",
        "=" * 26,
        f"报错时间（{timezone_name}）：{now.isoformat(timespec='seconds')}",
        f"任务日期：{now.date().isoformat()}",
        f"错误类型：{type(error).__name__}",
        f"错误原因：{_redact_failure_text(error, config)}",
        f"模型：{getattr(config, 'model_id', '(未知)')}",
        f"ADB 设备：{getattr(config, 'device', '(未知)')}",
        f"最大重试次数：{getattr(config, 'max_retries', '(未知)')}",
        f"已用失败次数：{getattr(error, 'failures', '(未知)')}",
        f"失败步骤：{getattr(error, 'step', '(未知)')}",
        f"失败阶段：{getattr(error, 'phase', '(未知)')}",
        f"当前任务：{getattr(error, 'target', '(未知)')}",
    ]
    if recent_action is not None:
        lines.append(
            "最近动作："
            + _redact_failure_text(
                json.dumps(recent_action, ensure_ascii=False, sort_keys=True), config
            )
        )
    if observation is not None:
        lines.extend(
            [
                f"当前 Activity：{_redact_failure_text(getattr(observation, 'activity', ''), config) or '(未知)' }",
                f"当前页面阶段：{_redact_failure_text(getattr(observation, 'stage', ''), config) or '(未知)' }",
            ]
        )
        ui = _redact_failure_text(getattr(observation, "ui", ""), config)
        if ui:
            lines.extend(["当前无障碍摘要：", ui[:8000]])
    lines.extend(
        [
            f"状态文件：{state_path}",
            "状态内容："
            + json.dumps(state, ensure_ascii=False, sort_keys=True),
            "",
            "完整 Python 堆栈：",
            _redact_failure_text(
                "".join(traceback.format_exception(type(error), error, error.__traceback__)),
                config,
            ),
        ]
    )
    return "\n".join(lines)


def copy_failure_report_to_clipboard(root: Any, report: str) -> None:
    """将脱敏后的失败报告写入桌面剪贴板。"""
    root.clipboard_clear()
    root.clipboard_append(report)
    # 让 Tk 立即处理剪贴板请求，窗口关闭后 KDE 剪贴板管理器仍可接管内容。
    root.update()


def _set_copy_button_text_if_alive(root: Any, button: Any, value: str) -> None:
    """失败窗口关闭后，忽略已销毁控件上的延迟回调。"""
    try:
        if root.winfo_exists() and button.winfo_exists():
            button.config(text=value)
    except Exception:
        return


def show_failure_alert(config: Config | None, error: BaseException) -> None:
    """弹出置顶且阻塞的失败窗口，必须由用户手动关闭。"""
    report = build_failure_report(config, error)
    print(report, file=sys.stderr)
    try:
        import tkinter as tk
    except Exception as exc:
        print(f"无法加载 Tkinter，详细报告已写入日志：{type(exc).__name__}: {exc}", file=sys.stderr)
        return
    try:
        root = tk.Tk()
        root.title("元宝每日任务失败")
        root.geometry("980x720")
        root.minsize(700, 480)
        # KDE Wayland/XWayland 对置顶和强制聚焦的支持取决于窗口管理器；
        # 这些增强失败时仍必须进入 mainloop，让报告窗口保持常驻。
        with contextlib.suppress(Exception):
            root.attributes("-topmost", True)
        with contextlib.suppress(Exception):
            root.lift()
            root.focus_force()
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame = tk.Frame(root, padx=12, pady=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        scrollbar = tk.Scrollbar(frame)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text = tk.Text(
            frame,
            wrap="none",
            font=("等线", 11),
            padx=8,
            pady=8,
            yscrollcommand=scrollbar.set,
        )
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar.config(command=text.yview)
        text.insert("1.0", report)
        text.config(state="disabled")
        def copy_report() -> None:
            try:
                copy_failure_report_to_clipboard(root, report)
                _set_copy_button_text_if_alive(root, copy_button, "已复制报错")
                with contextlib.suppress(Exception):
                    root.after(
                        1500,
                        lambda: _set_copy_button_text_if_alive(
                            root, copy_button, "复制报错"
                        ),
                    )
            except Exception as exc:
                _set_copy_button_text_if_alive(root, copy_button, "复制失败")
                print(
                    f"复制失败报告到剪贴板失败：{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

        copy_button = tk.Button(
            frame,
            text="复制报错",
            command=copy_report,
            height=2,
        )
        copy_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.mainloop()
    except Exception as exc:
        # 没有图形会话或 Tk 后端异常时，不能覆盖原始任务错误。
        print(f"失败窗口初始化失败，详细报告已写入日志：{type(exc).__name__}: {exc}", file=sys.stderr)
