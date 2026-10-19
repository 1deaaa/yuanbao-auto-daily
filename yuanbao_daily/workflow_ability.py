"""能力站阶段推进：主卡点击、体验完成与完成核验的状态迁移。

从 ``Workflow._after_successful_action`` 拆出，使能力站状态迁移可以单独
阅读和测试。
"""

from __future__ import annotations

from typing import Any

from .constants import (
    DAILY_ABILITY_BUTTON_CHECK_LIMIT,
    DAILY_ABILITY_RULE_NAME,
    DAILY_ABILITY_VERIFY_CHECK_LIMIT,
)
from .models import ToolResult


class WorkflowAbilityMixin:
    """能力站相关的状态迁移。"""

    def _advance_daily_ability_opening(
        self, name: str, result: ToolResult
    ) -> ToolResult:
        """主卡点击后的状态迁移：确认点击证据并进入体验阶段。"""
        if (
            name == "open_daily_ability"
            and result.data.get("ability_card_visible") is False
        ):
            self.daily_ability_button_checks += 1
            if self.daily_ability_button_checks > DAILY_ABILITY_BUTTON_CHECK_LIMIT:
                return ToolResult(
                    False,
                    "多次检查仍未确认主轮播卡‘去体验’按钮，未点击其它卡片",
                    {"stage": result.data.get("stage", ""), "visual_replan": True},
                )
            self.phase = "daily_ability_wait"
            return result
        if result.data.get("ability_station_clicked") is not True:
            return ToolResult(
                False,
                "主轮播卡‘去体验’没有得到点击证据",
                {"stage": result.data.get("stage", ""), "visual_replan": True},
            )
        self.daily_ability_click_attempts += 1
        self.daily_ability_button_checks = 0
        self.daily_ability_verification_checks = 0
        # 主卡点击证据成立后，体验可能仍在同一福利 WebView 的浮层中；先
        # 记录“体验已启动”，但完成状态仍必须等返回后的主卡“已完成”证据。
        self.daily_ability_experience_opened = name == "open_daily_ability" or (
            result.data.get("stage") != "welfare"
        )
        self.daily_ability_voice_submitted = False
        self.phase = "daily_ability_experience"
        return result

    def _advance_daily_ability_verification(self, result: ToolResult) -> ToolResult:
        """完成核验：只有主卡明确显示“已完成”才写入完成状态。"""
        card_status = result.data.get("card_status")
        if card_status == "已完成":
            self.daily_ability_done = True
            self.daily_ability_evidence = DAILY_ABILITY_RULE_NAME
            self._persist_task_state()
            self.phase = "report"
        elif card_status == "去体验":
            return ToolResult(
                False,
                "模型处理体验页面后主轮播卡仍显示‘去体验’，未将大任务记为完成",
                {"stage": "welfare", "visual_replan": True},
            )
        else:
            self.daily_ability_verification_checks += 1
            if self.daily_ability_verification_checks > DAILY_ABILITY_VERIFY_CHECK_LIMIT:
                return ToolResult(
                    False,
                    "未能读取主轮播卡状态并核验‘已完成’，未写入完成状态",
                    {"stage": "welfare", "visual_replan": True},
                )
            self.phase = "verify_daily_ability_wait"
        return result

    def _advance_daily_ability_experience(
        self, name: str, args: dict[str, Any], result: ToolResult
    ) -> ToolResult:
        """体验阶段：记录语音提交，并在浮层消失后转入完成核验。"""
        if name == "long_press" and args.get("_daily_ability_voice") is True:
            self.daily_ability_voice_submitted = True
        evidence = result.data.get("entry_evidence")
        experience_visible = isinstance(evidence, dict) and evidence.get(
            "daily_ability_experience"
        ) is True
        experience_visible = experience_visible or result.data.get(
            "daily_ability_experience"
        ) is True
        if getattr(self.executor, "pending_generation", False):
            self.daily_ability_experience_opened = True
        elif result.data.get("stage") != "welfare" or experience_visible:
            self.daily_ability_experience_opened = True
        elif self.daily_ability_experience_opened or name == "press_back":
            self.phase = "verify_daily_ability"
        return result

    def _advance_daily_ability_phase(
        self, name: str, args: dict[str, Any], result: ToolResult
    ) -> ToolResult:
        """能力站阶段推进：按当前阶段分派到对应的状态迁移。"""
        phase = self.phase
        if phase == "daily_ability" and name in {
            "open_daily_ability",
            "continue_daily_ability",
        }:
            return self._advance_daily_ability_opening(name, result)
        if phase == "verify_daily_ability" and name == "verify_daily_ability":
            return self._advance_daily_ability_verification(result)
        if phase == "daily_ability_experience":
            return self._advance_daily_ability_experience(name, args, result)
        if phase == "daily_ability_wait" and name == "wait_5s":
            self.phase = "daily_ability"
        elif phase == "verify_daily_ability_wait" and name == "wait_5s":
            self.phase = "verify_daily_ability"
        return result
