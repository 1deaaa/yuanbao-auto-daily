"""当天任务状态的读写；只保存可安全恢复的非敏感字段。"""

from __future__ import annotations

import os
from pathlib import Path
import json
from typing import Any

def read_daily_state(path: Path) -> dict[str, Any]:
    """读取可恢复的当天状态；文件损坏时按空状态处理。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_daily_state(path: Path, state: dict[str, Any]) -> None:
    """原子写入非敏感状态，避免进程中断留下半个 JSON。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        # 状态文件只是恢复优化，不能阻断当前已经完成的页面操作。
        print(f"状态文件写入失败：{type(exc).__name__}")
