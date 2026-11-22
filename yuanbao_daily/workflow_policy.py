"""决策策略：本地快路径的适用条件与视觉模型可用工具。

这里只放纯策略，不持有运行状态：

- 页面阶段与当前工作流阶段不符时，立即交给视觉模型接管；
- 正常阶段给视觉模型一个“优先工具 + 导航工具”的宽集合，而不是把
  它锁死成唯一工具；
- 兑换确认和奖品使用等不可逆步骤仍保持最小工具集。
"""

from __future__ import annotations

from .constants import (
    DAILY_ABILITY_EXPERIENCE_TOOLS,
    DAILY_ABILITY_TOOLS,
    TASK_STEPS,
    VISUAL_NAVIGATION_TOOLS,
)
from .models import Observation

NAVIGATION_TOOLS = frozenset(
    {
        *VISUAL_NAVIGATION_TOOLS,
        "open_welfare",
        "return_to_welfare",
    }
)

_EXPERIENCE_TOOLS = frozenset(DAILY_ABILITY_EXPERIENCE_TOOLS) | NAVIGATION_TOOLS

# 新增任务巡查阶段：允许通用应用内操作，但不开放兑换/完成断言工具。
_EXTRA_TASK_TOOLS = frozenset(
    {
        "tap",
        "swipe",
        "press_back",
        "open_welfare",
        "return_to_welfare",
        "hold_to_speak",
        "long_press",
        "input_test_prompt",
        "send_message",
        "wait_5s",
        "select_local_image",
        "confirm_image",
        "claim_reward",
        "report_extra_tasks",
    }
)


PHASE_ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    "navigate_welfare": frozenset({"report_tasks"}) | NAVIGATION_TOOLS,
    "report": frozenset({"report_tasks"}) | NAVIGATION_TOOLS,
    "daily_ability": frozenset(DAILY_ABILITY_TOOLS) | NAVIGATION_TOOLS,
    "daily_ability_experience": _EXPERIENCE_TOOLS,
    "daily_ability_wait": frozenset({"wait_5s"}) | NAVIGATION_TOOLS,
    "verify_daily_ability": frozenset({"verify_daily_ability"}) | NAVIGATION_TOOLS,
    "verify_daily_ability_wait": frozenset({"wait_5s"}) | NAVIGATION_TOOLS,
    "open_target": frozenset({"report_tasks"}) | NAVIGATION_TOOLS,
    "perform": frozenset(
        {
            "input_test_prompt",
            "send_message",
            "wait_5s",
            "select_local_image",
            "confirm_image",
            "claim_reward",
        }
    )
    | NAVIGATION_TOOLS,
    "claim": frozenset({"claim_reward"}) | NAVIGATION_TOOLS,
    "return": frozenset({"return_to_welfare"}) | NAVIGATION_TOOLS,
    "open_exchange": frozenset({"open_exchange", "report_exchange"})
    | NAVIGATION_TOOLS,
    # 不可逆步骤不开放通用点击；确认页只允许“确认目标卡”或高层返回。
    "exchange_confirm": frozenset(
        {"confirm_exchange", "press_back", "return_to_welfare", "open_exchange"}
    ),
    "exchange_back": frozenset(
        {"press_back", "return_to_welfare", "open_exchange"}
    ),
    "redeem": frozenset({"redeem_qq_card", "report_exchange"}) | NAVIGATION_TOOLS,
    "prize_records": frozenset({"open_prize_records"}) | NAVIGATION_TOOLS,
    "use_reward": frozenset({"use_bound_reward"}) | NAVIGATION_TOOLS,
    "use_confirm": frozenset({"confirm_reward_use", "press_back"}),
    "return_home": frozenset({"return_to_home"}) | NAVIGATION_TOOLS,
    "extra_tasks": _EXTRA_TASK_TOOLS,
    "complete": frozenset({"complete_task"}),
}

# 接管模式下通常开放全部工具；不可逆阶段仍保持最小集合，避免“自由决策”
# 绕过商品/奖励核对。
RESTRICTED_TAKEOVER_PHASES = frozenset(
    {"exchange_confirm", "exchange_back", "use_confirm", "extra_tasks", "complete"}
)


_PERFORM_STAGES: dict[str, frozenset[str]] = {
    "question": frozenset({"chat", "writing", "app", "unknown"}),
    "writing": frozenset({"writing", "chat", "app", "unknown"}),
    "image": frozenset({"image", "chat", "app", "picker", "photo_preview", "unknown"}),
    "photo_question": frozenset(
        {"photo_question", "photo_preview", "picker", "chat", "app", "unknown"}
    ),
    "same_template": frozenset({"app", "chat", "welfare", "unknown"}),
}

_EXPECTED_STAGES: dict[str, frozenset[str]] = {
    "redeem": frozenset({"exchange"}),
    "exchange_confirm": frozenset({"exchange"}),
    "prize_records": frozenset({"prize_records", "exchange"}),
    "use_reward": frozenset({"prize_records", "exchange"}),
    "use_confirm": frozenset({"prize_records", "exchange"}),
    "verify_daily_ability": frozenset({"welfare"}),
}

_STAGE_LABELS = {
    "welfare": "福利中心",
    "exchange": "兑换商城",
    "prize_records": "奖品记录",
    "activity": "福利活动页",
    "ours": "“我们”页",
    "chat": "聊天页",
    "app": "元宝应用页",
    "writing": "写作页",
    "image": "P图页",
    "photo_question": "拍题页",
    "photo_preview": "图片确认页",
    "picker": "系统图片选择器",
    "unknown": "未知页面",
}


def stage_mismatch(phase: str, target: str | None, observation: Observation) -> str | None:
    """返回当前页面已经偏离阶段预期的原因；None 表示仍在预期页面。

    视觉导航阶段（打开福利、任务入口、兑换入口）本来就会让模型看最新
    截图，不做漂移判断，避免重复触发接管。
    """
    if phase == "perform":
        expected = _PERFORM_STAGES.get(target or "")
    else:
        expected = _EXPECTED_STAGES.get(phase)
    if expected is None:
        return None
    stage = observation.stage or "unknown"
    if stage in expected:
        return None
    actual = _STAGE_LABELS.get(stage, stage or "未知页面")
    want = "、".join(_STAGE_LABELS.get(item, item) for item in sorted(expected))
    return f"页面已偏离阶段预期：期望 {want}，当前实际是 {actual}"


def model_tools(phase: str, target: str | None) -> frozenset[str] | None:
    """返回当前阶段视觉模型可用的工具集合。"""
    if phase == "perform" and target in TASK_STEPS:
        # 具体业务工具由 allowed 集合提供；这里不再按任务类型收窄，
        # 页面变化时模型可以先返回、滚动或重新进入。
        return PHASE_ALLOWED_TOOLS["perform"]
    return PHASE_ALLOWED_TOOLS.get(phase, NAVIGATION_TOOLS)


def takeover_tools(phase: str) -> frozenset[str] | None:
    """接管模式的工具集合；不可逆阶段仍保持最小集合。"""
    if phase in RESTRICTED_TAKEOVER_PHASES:
        return PHASE_ALLOWED_TOOLS.get(phase)
    return None


def local_step_applicable(
    name: str,
    observation: Observation,
    executor: object,
    substate: dict[str, bool],
) -> bool:
    """判断本地确定性动作是否仍适用于当前页面。

    这里只做“能不能安全地跑本地配方”的页面级校验；动作内部的输入/发送/
    选择器细节仍由执行器验证。页面不符合时调用方应立即交给视觉模型，
    不再盲目重试同一个本地动作。
    """
    stage = observation.stage
    if name in {"wait_5s", "claim_reward", "return_to_welfare", "return_to_home"}:
        return True
    if name == "input_test_prompt":
        if stage not in {"chat", "writing", "app"}:
            return False
        find_input = getattr(executor, "_find_input", None)
        return not callable(find_input) or find_input(observation) is not None
    if name == "send_message":
        if stage not in {"chat", "writing"}:
            return False
        find_input = getattr(executor, "_find_input", None)
        find_send = getattr(executor, "_find_send", None)
        return bool(
            (callable(find_input) and find_input(observation) is not None)
            or (callable(find_send) and find_send(observation) is not None)
        )
    if name == "select_local_image":
        return stage in {"chat", "image", "picker", "photo_preview", "app"}
    if name == "confirm_image":
        return stage in {"photo_question", "photo_preview", "picker", "chat", "app"}
    if name == "redeem_qq_card":
        return stage == "exchange"
    if name == "open_exchange":
        return stage in {"welfare", "exchange"}
    if name == "confirm_exchange":
        return False
    if name == "open_prize_records":
        return stage in {"exchange", "prize_records"} and bool(
            getattr(executor, "exchange_confirmed", False)
        )
    if name == "use_bound_reward":
        return stage == "prize_records" and bool(
            getattr(executor, "prize_records_opened", False)
        )
    if name == "confirm_reward_use":
        return stage == "prize_records" and bool(
            getattr(executor, "reward_use_clicked", False)
        )
    # 图片任务进入具体页面后，本地选图/确认仍需运行；其余情况保守放行。
    if name in {"tap", "swipe", "press_back"}:
        return True
    return True