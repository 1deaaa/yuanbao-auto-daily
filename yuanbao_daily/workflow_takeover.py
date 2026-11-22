"""VLM 接管层：页面变化后把连续多步决策交还给视觉模型。

与旧的“卡住一次恢复”不同，接管是有预算的持续状态：模型可以根据最新
截图连续执行点击、滑动、返回和业务工具，直到工作流阶段重新推进。本地
确定性步骤只在页面仍符合预期时执行。
"""

from __future__ import annotations

from .constants import (
    LOCAL_FAILURE_TAKEOVER_THRESHOLD,
    TASK_LABELS,
    VLM_TAKEOVER_STEP_LIMIT,
)
from .models import Observation
from .workflow_policy import (
    local_step_applicable,
    model_tools,
    takeover_tools,
)


class WorkflowTakeoverMixin:
    """接管状态与本地快路径适用性判断。"""

    def sync_takeover(self) -> None:
        """阶段/目标已经推进时结束接管，回到正常快路径。"""
        if not self.takeover:
            return
        if (
            self.takeover_phase != self.phase
            or self.takeover_target != self.target
        ):
            self.exit_takeover("阶段或目标已推进")

    def enter_takeover(self, reason: str) -> None:
        if self.takeover:
            return
        self.takeover = True
        self.takeover_phase = self.phase
        self.takeover_target = self.target
        self.takeover_reason = reason[:400]
        self.takeover_steps = 0
        self.local_failure_streak = 0
        print(
            f"进入 VLM 接管（阶段={self.phase}，目标={self.target or '无'}）：{reason[:160]}"
        )

    def exit_takeover(self, reason: str = "") -> None:
        if not self.takeover:
            return
        detail = f"（{reason}）" if reason else ""
        print(f"VLM 接管结束{detail}，恢复本地快路径")
        self.takeover = False
        self.takeover_phase = None
        self.takeover_target = None
        self.takeover_reason = ""
        self.takeover_steps = 0

    def note_local_failure(self, message: str) -> bool:
        """记录本地步骤失败；达到阈值时立即进入接管。返回是否已触发。"""
        self.local_failure_streak += 1
        self.last_local_failure = message[:400]
        if self.phase == "extra_tasks":
            # 新增任务巡查本身已经由视觉模型连续决策，不需要再套一层接管。
            return False
        if (
            not self.takeover
            and self.local_failure_streak >= LOCAL_FAILURE_TAKEOVER_THRESHOLD
        ):
            self.enter_takeover(
                f"本地步骤连续失败 {self.local_failure_streak} 次：{message}"
            )
            return True
        return False

    def reset_local_failure(self) -> None:
        self.local_failure_streak = 0
        self.last_local_failure = ""

    def reserve_takeover_step(self) -> bool:
        """占用一步接管预算；耗尽时返回 False。"""
        self.takeover_steps += 1
        return self.takeover_steps <= VLM_TAKEOVER_STEP_LIMIT

    def takeover_allowed_tools(self) -> frozenset[str] | None:
        return takeover_tools(self.phase)

    def takeover_hint(self) -> str:
        label = TASK_LABELS.get(self.target or "", self.target or "无")
        history = ""
        if self.last_local_failure:
            history = f"最近本地失败：{self.last_local_failure}\n"
        return (
            "你现在接管页面操作。之前的本地流程假设可能已经过期，"
            "不要再依赖历史坐标或旧页面结构。\n"
            f"当前阶段={self.phase}，当前目标={label}。\n"
            "先依据最新截图判断页面，再只调用一个工具。可以使用导航工具"
            "（tap/swipe/press_back/open_welfare/return_to_welfare）或当前阶段允许的业务工具；"
            "如果页面已经回到福利中心，可以调用 report_tasks 重新对账；"
            "如果任务页已经变化，按截图完成当前任务所需步骤，不要停留在旧子步骤上。\n"
            "兑换安全：目标只有 QQ 超级会员 3 天卡。任何 QQ 音乐/绿钻/黄钻或其它会员卡都必须"
            "立即放弃并返回商城；确认页必须明确显示目标卡，否则调用 press_back，绝不能点确认。\n"
            f"{history}"
            f"接管原因：{self.takeover_reason}"
        )

    def deterministic_step_applicable(
        self, name: str, observation: Observation
    ) -> bool:
        return local_step_applicable(
            name, observation, self.executor, self.substate
        )

    def preferred_model_tools(self) -> frozenset[str] | None:
        return model_tools(self.phase, self.target)