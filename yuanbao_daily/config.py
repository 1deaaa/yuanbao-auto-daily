"""运行配置：环境变量解析与默认值。"""

from __future__ import annotations

import re
from pathlib import Path
import os
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dotenv import load_dotenv

from .constants import (
    DEFAULT_AUTO_ACCEPT_PROTOCOL,
    DEFAULT_BASE_URL,
    DEFAULT_CARD_NAME,
    DEFAULT_DEVICE,
    DEFAULT_GENERATION_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_STEPS,
    DEFAULT_MODEL,
    DEFAULT_MODEL_REQUEST_TIMEOUT,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_RETRY_COOLDOWN,
    DEFAULT_RUN_TIME,
    DEFAULT_STATE_PATH,
    DEFAULT_STOP_WAYDROID_AFTER_RUN,
    DEFAULT_TEST_AUDIO,
    DEFAULT_TEST_IMAGE,
    DEFAULT_TEST_PROMPT,
    DEFAULT_TIMEZONE,
    REASONING_EFFORTS,
    ROOT,
)
from .errors import AgentError

def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise AgentError(f"配置 {name} 必须是整数") from exc
    if value < minimum:
        raise AgentError(f"配置 {name} 不能小于 {minimum}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise AgentError(f"配置 {name} 必须是布尔值")


def _env_choice(name: str, default: str, choices: set[str] | frozenset[str]) -> str:
    value = _first_env(name, default=default).lower()
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise AgentError(f"配置 {name} 必须是以下值之一：{allowed}")
    return value


def _project_path(value: str) -> Path:
    """解析配置路径；相对路径统一相对于项目目录，便于跨主机运行。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def parse_run_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        raise AgentError(f"每日运行时间格式错误，应为 HH:MM：{value!r}")
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class Config:
    model_id: str
    reasoning_effort: str
    base_url: str
    api_key: str
    device: str
    stop_waydroid_after_run: bool
    auto_accept_protocol: bool
    run_time: str
    timezone_name: str
    max_retries: int
    retry_cooldown_seconds: int
    max_steps: int
    generation_timeout_seconds: int
    model_request_timeout_seconds: int
    test_prompt: str
    test_image_path: Path
    test_audio_path: Path
    card_name: str
    prompt_path: Path
    state_path: Path
    force_all_daily_tasks: bool

    @classmethod
    def from_env(cls, env_path: Path = ROOT / ".env") -> "Config":
        # 环境变量优先于 .env，便于 systemd、容器和临时测试覆盖配置。
        if env_path.exists():
            load_dotenv(env_path, override=False)
        model_id = _first_env("MODEL_ID", "LOCAL_LLM_MODEL", default=DEFAULT_MODEL)
        reasoning_effort = _env_choice(
            "REASONING_EFFORT", DEFAULT_REASONING_EFFORT, REASONING_EFFORTS
        )
        base_url = _first_env("BASE_URL", "LOCAL_LLM_BASE_URL", default=DEFAULT_BASE_URL).rstrip("/")
        api_key = _first_env("API_KEY", "LOCAL_LLM_API_KEY")
        if not api_key:
            raise AgentError("缺少 API_KEY，请在 .env 或环境变量中配置")
        run_time = _first_env("DAILY_RUN_TIME", default=DEFAULT_RUN_TIME)
        parse_run_time(run_time)
        timezone_name = _first_env("TIMEZONE", "DAILY_TIMEZONE", default=DEFAULT_TIMEZONE)
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise AgentError(f"找不到时区：{timezone_name}") from exc
        test_image = _project_path(_first_env("TEST_IMAGE_PATH", default=DEFAULT_TEST_IMAGE))
        test_audio = _project_path(_first_env("TEST_AUDIO_PATH", default=DEFAULT_TEST_AUDIO))
        prompt_path = _project_path(_first_env("PROMPT_PATH", default="每日任务提示词.md"))
        state_path = _project_path(_first_env("STATE_PATH", default=".yuanbao_daily_state.json"))
        config = cls(
            model_id=model_id,
            reasoning_effort=reasoning_effort,
            base_url=base_url,
            api_key=api_key,
            device=_first_env("ANDROID_DEVICE", default=DEFAULT_DEVICE),
            stop_waydroid_after_run=_env_bool(
                "STOP_WAYDROID_AFTER_RUN", DEFAULT_STOP_WAYDROID_AFTER_RUN
            ),
            # 只自动确认元宝首次协议页；登录、验证码和扫码仍需人工完成。
            auto_accept_protocol=_env_bool(
                "AUTO_ACCEPT_PROTOCOL", DEFAULT_AUTO_ACCEPT_PROTOCOL
            ),
            run_time=run_time,
            timezone_name=timezone_name,
            max_retries=_env_int("MAX_RETRIES", DEFAULT_MAX_RETRIES, minimum=1),
            retry_cooldown_seconds=_env_int(
                "RETRY_COOLDOWN_SECONDS", DEFAULT_RETRY_COOLDOWN, minimum=0
            ),
            max_steps=_env_int("MAX_STEPS", DEFAULT_MAX_STEPS, minimum=1),
            generation_timeout_seconds=_env_int(
                "GENERATION_TIMEOUT_SECONDS", DEFAULT_GENERATION_TIMEOUT, minimum=5
            ),
            model_request_timeout_seconds=_env_int(
                "MODEL_REQUEST_TIMEOUT_SECONDS", DEFAULT_MODEL_REQUEST_TIMEOUT, minimum=5
            ),
            test_prompt=_first_env("TEST_PROMPT", default=DEFAULT_TEST_PROMPT),
            test_image_path=test_image,
            test_audio_path=test_audio,
            # 兑换目标固定为三天卡，避免配置或模型误选其它商品。
            card_name=DEFAULT_CARD_NAME,
            prompt_path=prompt_path,
            state_path=state_path,
            force_all_daily_tasks=_env_bool("FORCE_ALL_DAILY_TASKS", False),
        )
        if not config.test_prompt or len(config.test_prompt) > 200:
            raise AgentError("TEST_PROMPT 为空或过长")
        if not config.test_image_path.is_file():
            raise AgentError(f"测试图片不存在：{config.test_image_path}")
        return config
