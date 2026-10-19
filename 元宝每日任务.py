#!/usr/bin/env python3
"""元宝每日任务入口（兼容门面）。

实际实现已拆分到 ``yuanbao_daily`` 包：

- ``constants`` / ``models`` / ``ui``：常量、屏幕数据与无障碍树解析；
- ``state`` / ``config`` / ``errors``：状态、配置与错误类型；
- ``device`` / ``welfare``：设备通道与福利页语义；
- ``tools`` / ``vision``：工具契约与视觉模型封装；
- ``executor`` / ``workflow`` / ``runner``：执行层、编排状态机与运行入口。

本文件只保留对外接口的再导出，供 systemd 单元、脚本调用与测试继续使用
``元宝每日任务`` 这个入口路径。
"""

from __future__ import annotations

import time

from yuanbao_daily import *  # noqa: F401,F403  保持历史入口的公开名称
from yuanbao_daily import __all__ as _PACKAGE_ALL


__all__ = [*_PACKAGE_ALL]


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    raise SystemExit(main())
