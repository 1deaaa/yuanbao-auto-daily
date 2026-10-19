"""执行层入口：把受限工具动作落到设备上，并维护页面证据。

具体实现按职责拆在混入类中：

- ``executor_observation`` 观测、页面阶段与系统弹窗；
- ``executor_navigation`` 聊天首页、抽屉与福利入口导航；
- ``executor_content`` 固定文本、测试图片、生成等待与语音注入；
- ``executor_exchange`` 积分、兑换、奖品记录与绑定账号使用；
- ``executor_actions`` 每个受限工具一个独立处理方法。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Config
from .device import Device
from .errors import AgentError, ManualActionRequired
from .executor_actions import ExecutorActionsMixin
from .executor_content import ExecutorContentMixin
from .executor_exchange import ExecutorExchangeMixin
from .executor_navigation import ExecutorNavigationMixin
from .executor_observation import ExecutorObservationMixin
from .models import Bounds, Node, Observation, ToolResult


class ToolExecutor(
    ExecutorObservationMixin,
    ExecutorNavigationMixin,
    ExecutorContentMixin,
    ExecutorExchangeMixin,
    ExecutorActionsMixin,
):
    """组合各执行层能力；对外只暴露 ``execute`` 工具入口。"""

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

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        """按工具名分派到独立处理方法；参数与运行期错误在此统一收敛。"""
        guarded = self._guard_generation(name)
        if guarded is not None:
            if name == "tap":
                self._daily_ability_experience_navigation = False
            return guarded
        handler = self._TOOL_HANDLERS.get(name)
        if handler is None:
            return self._result(False, f"未知工具：{name}")
        try:
            return getattr(self, handler)(args)
        except (KeyError, TypeError, ValueError) as exc:
            return self._result(False, f"工具参数错误：{type(exc).__name__}")
        except ManualActionRequired:
            # 登录和协议确认不能靠重试解决，也不能被降级成普通工具失败。
            raise
        except AgentError as exc:
            return self._result(False, str(exc)[:240])
