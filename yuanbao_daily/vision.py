"""视觉模型封装：单工具调用、协议降级与同轮历史维护。"""

from __future__ import annotations

from pathlib import Path
import base64
import json
import time
from typing import Any, Callable
from openai import (
    APIError,
    BadRequestError,
    OpenAI,
)

from .config import Config
from .constants import (
    MAX_HISTORY_RESULT_CHARS,
    MAX_HISTORY_SUMMARY_CHARS,
    TOOL_ARGUMENT_REPAIR_ATTEMPTS,
)
from .errors import (
    AgentError,
    ModelAccessError,
)
from .models import (
    Observation,
    ToolResult,
)
from .tools import TOOL_DEFINITIONS
from .ui import (
    encode_adb_input_text,
    normalize_tool_arguments,
)

def load_prompt(path: Path) -> str:
    if not path.is_file():
        raise AgentError(f"提示词文件不存在：{path}")
    return path.read_text(encoding="utf-8")


class VisionModel:
    """调用 OpenAI Chat Completions，兼容支持或不支持 tools 的 v1 端点。"""

    def __init__(self, config: Config):
        self.config = config
        # 视觉网关无响应时必须回到统一重试边界，不能无限占用 Waydroid 会话。
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.model_request_timeout_seconds,
        )
        self.tools_supported = True
        # 系统提示必须在同一轮请求中完全一致；动态状态放到最新 user 消息末尾。
        # 端点不支持 tools 时也沿用同一份静态协议，避免降级请求改变前缀。
        self.system_prompt = (
            load_prompt(config.prompt_path).rstrip()
            + '\n\n协议兼容说明：若当前端点不支持 tools，请只输出'
            + ' {"tool":"工具名","arguments":{}}；支持 tools 时仍只调用一个工具。'
        )
        self.history: list[dict[str, Any]] = []
        self._pending_turn: dict[str, Any] | None = None
        self._turn_number = 0

    def _request(
        self,
        messages: list[dict[str, Any]],
        forced_tool: str | None = None,
        allowed_tools: frozenset[str] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": messages,
            # Chat Completions 规范中的 reasoning_effort 是请求体顶层字段。
            # 使用 extra_body 让 openai>=1.40 的旧客户端也能把它原样透传给兼容网关。
            "extra_body": {"reasoning_effort": self.config.reasoning_effort},
        }
        if self.tools_supported:
            kwargs["tools"] = [
                definition
                for definition in TOOL_DEFINITIONS
                if allowed_tools is None
                or definition["function"]["name"] in allowed_tools
            ]
            # 仅在编排器发现阶段错误后强制该阶段唯一工具；正常请求保持 auto，避免改变缓存前缀。
            kwargs["tool_choice"] = (
                {
                    "type": "function",
                    "function": {"name": forced_tool},
                }
                if forced_tool
                else "auto"
            )
        try:
            return self.client.chat.completions.create(**kwargs)
        except (BadRequestError, APIError) as exc:
            detail = str(exc).lower()
            tools_unsupported = any(
                marker in detail
                for marker in (
                    "does not support tools",
                    "unsupported tools",
                    "unknown field: tools",
                    "unrecognized field: tools",
                    "tools is not supported",
                    "function calling is not supported",
                )
            )
            if self.tools_supported and isinstance(exc, BadRequestError) and tools_unsupported:
                # 一些 OpenAI 兼容端点实现了视觉输入却没有 function tools，降级为严格 JSON。
                self.tools_supported = False
                return self.client.chat.completions.create(
                    model=self.config.model_id,
                    messages=messages,
                    extra_body={"reasoning_effort": self.config.reasoning_effort},
                )
            raise exc

    def _with_retries(self, operation: Callable[[], Any], label: str) -> Any:
        last: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                return operation()
            except Exception as exc:  # 上游兼容端点的异常类型不统一，统一进入重试边界。
                status_code = getattr(exc, "status_code", None)
                if status_code in {401, 403}:
                    raise ModelAccessError(
                        f"{label}被拒绝（HTTP {status_code}）：{str(exc)[:240]}"
                    ) from exc
                last = exc
                if attempt >= self.config.max_retries:
                    break
                print(f"{label}失败（第 {attempt}/{self.config.max_retries} 次），{self.config.retry_cooldown_seconds} 秒后重试")
                time.sleep(self.config.retry_cooldown_seconds)
        raise AgentError(f"{label}连续失败：{type(last).__name__ if last else '未知错误'}") from last

    @staticmethod
    def _result_json(result: ToolResult) -> str:
        payload = {"ok": result.ok, "message": result.message, **result.data}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[
            :MAX_HISTORY_RESULT_CHARS
        ]

    @staticmethod
    def _action_json(name: str, args: dict[str, Any]) -> str:
        return json.dumps(
            {"tool": name, "arguments": args},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _history_summary(context: str, observation: Observation, turn_number: int) -> str:
        # 历史只保留编排状态和页面定位信息；截图、完整 UI 和可能的用户输入不回放。
        summary = (
            f"历史回合 {turn_number} 的观测摘要（仅作上下文，当前页面以最新截图为准）：\n"
            f"编排状态：{context}\n"
            f"Activity：{observation.activity or '(未知)'}\n"
            f"页面阶段：{observation.stage or '(未知)'}"
        )
        return summary[:MAX_HISTORY_SUMMARY_CHARS]

    @staticmethod
    def _current_observation(observation: Observation, context: str) -> dict[str, Any]:
        user_text = (
            "当前屏幕观测（这是本轮唯一的最新页面依据）：\n"
            f"编排状态：{context}\n"
            f"当前 Activity：{observation.activity or '(未知)'}\n"
            f"当前页面阶段：{observation.stage or '(未知)'}\n"
            "以下无障碍层级只作辅助，截图是最终依据：\n"
            f"{observation.ui or '(无可用层级信息)'}"
        )
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(observation.image).decode("ascii"),
                        "detail": "high",
                    },
                },
            ],
        }

    def next_tool(
        self,
        observation: Observation,
        context: str,
        forced_tool: str | None = None,
        allowed_tools: frozenset[str] | None = None,
    ) -> tuple[str, dict[str, Any], str]:
        user_message = self._current_observation(observation, context)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            *self.history,
            user_message,
        ]
        for repair_attempt in range(TOOL_ARGUMENT_REPAIR_ATTEMPTS + 1):
            response = self._with_retries(
                lambda: self._request(
                    messages,
                    forced_tool=forced_tool,
                    allowed_tools=allowed_tools,
                ),
                "视觉模型调用",
            )
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            if len(tool_calls) > 1:
                raise AgentError("模型一次返回了多个工具调用，已拒绝以保持单步状态可验证")
            try:
                if tool_calls:
                    call = tool_calls[0]
                    name = call.function.name
                    args = json.loads(call.function.arguments or "{}")
                    if not isinstance(args, dict):
                        raise AgentError("模型工具参数必须是 JSON 对象")
                    call_id = call.id
                else:
                    name, args = self._parse_json_tool(message.content or "")
                    call_id = "fallback-current"
                args = normalize_tool_arguments(name, args)
            except (AgentError, json.JSONDecodeError, TypeError, ValueError) as exc:
                if repair_attempt >= TOOL_ARGUMENT_REPAIR_ATTEMPTS:
                    raise AgentError("模型连续返回无效工具参数，未执行本轮动作") from exc
                print("模型工具参数无效，已要求按当前工具 JSON Schema 重试一次")
                messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "上一次返回的工具参数无效，尚未执行任何操作。请根据当前可用工具的 JSON Schema，"
                            "重新观察当前页面，只返回一个工具调用及合法的 JSON 对象参数。"
                        ),
                    },
                ]
                continue
            self._pending_turn = {
                "context": context,
                "observation": observation,
                "name": name,
                "args": args,
            }
            return name, args, call_id
        raise AgentError("模型没有返回可执行的工具调用")

    @staticmethod
    def _parse_json_tool(content: str) -> tuple[str, dict[str, Any]]:
        candidate = content.strip()
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise AgentError(f"模型没有返回工具调用 JSON：{content[:160]!r}") from exc
        if "tool" in value:
            name = value["tool"]
            args = value.get("arguments", {})
        elif value.get("action") == "tap":
            name, args = "tap", {"x": value.get("x"), "y": value.get("y")}
        elif value.get("action") == "swipe":
            name, args = "swipe", value
        elif value.get("action") == "wait":
            name, args = "wait_5s", {}
        elif value.get("action") == "done":
            name, args = "complete_task", {}
        elif value.get("action") == "type":
            name, args = "input_test_prompt", {}
        else:
            raise AgentError("模型 JSON 没有可识别的工具")
        if not isinstance(name, str) or not isinstance(args, dict):
            raise AgentError("模型工具名或参数类型错误")
        return name, args

    def record_tool_result(self, call_id: str, result: ToolResult) -> None:
        pending = self._pending_turn
        if pending is None:
            return
        self._append_history(
            pending["context"],
            pending["observation"],
            pending["name"],
            pending["args"],
            result,
        )
        self._pending_turn = None

    def record_local_tool_result(
        self,
        name: str,
        args: dict[str, Any],
        observation: Observation,
        context: str,
        result: ToolResult,
    ) -> None:
        """本地工具只更新 Workflow，不把可推导的流水账塞回模型上下文。"""
        # 参数保留在接口中，便于调试调用方；页面和子状态会在下一次视觉请求中
        # 通过最新截图及 Workflow.context 提供，不重复消耗输入 token。
        del name, args, observation, context, result

    def _append_history(
        self,
        context: str,
        observation: Observation,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
    ) -> None:
        self._turn_number += 1
        self.history.extend(
            [
                {
                    "role": "user",
                    "content": self._history_summary(
                        context, observation, self._turn_number
                    ),
                },
                {
                    "role": "assistant",
                    "content": self._action_json(name, args),
                },
                {
                    "role": "user",
                    "content": (
                        "编排器已执行上一动作，结果如下；下一步必须以最新屏幕观测为准：\n"
                        + self._result_json(result)
                    ),
                },
            ]
        )
