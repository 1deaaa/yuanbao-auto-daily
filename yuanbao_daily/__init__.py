"""元宝每日任务自动化包。

分层职责：

- ``constants`` 全部可调参数与任务目录常量；
- ``models`` / ``ui`` 屏幕数据结构与无障碍树解析；
- ``state`` / ``config`` / ``errors`` 状态、配置与错误类型；
- ``device`` ADB 通道与 Waydroid 会话；
- ``welfare`` 福利页语义与页面阶段判定；
- ``tools`` / ``vision`` 工具契约与视觉模型封装；
- ``executor`` 工具动作执行层；
- ``workflow`` 任务编排状态机；
- ``runner`` 运行入口与失败报告。
"""

from .config import Config, parse_run_time
from .constants import (
    DAILY_ABILITY_EXPERIENCE_TOOLS,
    DAILY_ABILITY_RULE_NAME,
    DAILY_ABILITY_RULE_VERSION,
    DAILY_ABILITY_TOOLS,
    DAILY_TASK_MODE_ABILITY_STATION,
    DAILY_TASK_MODE_LEGACY,
    DEFAULT_CARD_NAME,
    DEFAULT_STATE_PATH,
    DEFAULT_TEST_AUDIO,
    DEFAULT_TEST_IMAGE,
    DEFAULT_TEST_PROMPT,
    FAILURE_EXIT_STATUS,
    FORCED_DAILY_TASKS,
    IMAGE_TASKS,
    LOCAL_DETERMINISTIC_TOOLS,
    NEW_WELFARE_DAILY_TASKS,
    PACKAGE_NAME,
    RETRYABLE_SAME_STATE_TOOLS,
    ROOT,
    TASK_ENTRY_ACTION_MARKERS,
    TASK_ENTRY_MARKERS,
    TASK_ENTRY_ROW_MARKERS,
    TASK_ENTRY_SCROLL_MAX_ATTEMPTS,
    TASK_LABELS,
    TASK_ORDER,
    TASK_STATE_SCHEMA,
    TASK_STEPS,
    TEXT_TASKS,
    VISUAL_EXCHANGE_TOOLS,
    VISUAL_NAVIGATION_TOOLS,
)
from .device import Device, WaydroidRuntime, is_adb_transport_failure
from .errors import (
    AgentError,
    AppUnresponsiveError,
    ManualActionRequired,
    ModelAccessError,
    RetryLimitExceeded,
)
from .executor import ToolExecutor
from .models import Bounds, Node, Observation, ToolResult
from .reporting import build_failure_report, show_failure_alert
from .runner import main, next_run_at, run_daemon, run_once
from .state import read_daily_state, write_daily_state
from .tools import TOOL_DEFINITIONS
from .ui import (
    action_signature,
    compact_ui,
    encode_adb_input_text,
    find_node,
    find_semantic_node,
    has_visible_text,
    node_interaction_bounds,
    normalize_tool_arguments,
    parse_bounds,
    parse_nodes,
    png_pixel,
    repeated_action_is_stuck,
    state_signature,
)
from .vision import VisionModel, load_prompt
from .welfare import (
    daily_ability_card_status,
    detect_stage,
    has_authoritative_local_task_catalog,
    has_complete_new_welfare_task_catalog,
    has_visible_exchange_product,
    is_daily_ability_experience_visible,
    is_new_welfare_page,
    is_new_welfare_webview,
    merge_supplemental_task_progress,
    parse_local_welfare_progress,
)
from .workflow import Workflow

__all__ = [
    "AgentError",
    "AppUnresponsiveError",
    "Bounds",
    "Config",
    "DAILY_ABILITY_EXPERIENCE_TOOLS",
    "DAILY_ABILITY_RULE_NAME",
    "DAILY_ABILITY_RULE_VERSION",
    "DAILY_ABILITY_TOOLS",
    "DAILY_TASK_MODE_ABILITY_STATION",
    "DAILY_TASK_MODE_LEGACY",
    "DEFAULT_CARD_NAME",
    "DEFAULT_STATE_PATH",
    "DEFAULT_TEST_AUDIO",
    "DEFAULT_TEST_IMAGE",
    "DEFAULT_TEST_PROMPT",
    "Device",
    "FAILURE_EXIT_STATUS",
    "FORCED_DAILY_TASKS",
    "IMAGE_TASKS",
    "LOCAL_DETERMINISTIC_TOOLS",
    "ManualActionRequired",
    "ModelAccessError",
    "NEW_WELFARE_DAILY_TASKS",
    "Node",
    "Observation",
    "PACKAGE_NAME",
    "RETRYABLE_SAME_STATE_TOOLS",
    "ROOT",
    "RetryLimitExceeded",
    "TASK_ENTRY_ACTION_MARKERS",
    "TASK_ENTRY_MARKERS",
    "TASK_ENTRY_ROW_MARKERS",
    "TASK_ENTRY_SCROLL_MAX_ATTEMPTS",
    "TASK_LABELS",
    "TASK_ORDER",
    "TASK_STATE_SCHEMA",
    "TASK_STEPS",
    "TEXT_TASKS",
    "TOOL_DEFINITIONS",
    "ToolExecutor",
    "ToolResult",
    "VISUAL_EXCHANGE_TOOLS",
    "VISUAL_NAVIGATION_TOOLS",
    "VisionModel",
    "WaydroidRuntime",
    "Workflow",
    "action_signature",
    "build_failure_report",
    "compact_ui",
    "daily_ability_card_status",
    "detect_stage",
    "encode_adb_input_text",
    "find_node",
    "find_semantic_node",
    "has_authoritative_local_task_catalog",
    "has_complete_new_welfare_task_catalog",
    "has_visible_exchange_product",
    "has_visible_text",
    "is_adb_transport_failure",
    "is_daily_ability_experience_visible",
    "is_new_welfare_page",
    "is_new_welfare_webview",
    "load_prompt",
    "main",
    "merge_supplemental_task_progress",
    "next_run_at",
    "node_interaction_bounds",
    "normalize_tool_arguments",
    "parse_bounds",
    "parse_local_welfare_progress",
    "parse_nodes",
    "parse_run_time",
    "png_pixel",
    "read_daily_state",
    "repeated_action_is_stuck",
    "run_daemon",
    "run_once",
    "show_failure_alert",
    "state_signature",
    "write_daily_state",
]
