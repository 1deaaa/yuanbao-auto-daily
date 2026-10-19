"""运行期错误类型；区分可重试、需人工处理与模型访问失败。"""
from typing import Any


class AgentError(RuntimeError):
    """代理可以报告给模型、并由外层重试的错误。"""


class ManualActionRequired(AgentError):
    """需要用户先完成登录、验证码或扫码，不能靠自动重试解决。"""


class AppUnresponsiveError(AgentError):
    """应用启动阶段无响应，可以通过重启应用或 Waydroid 会话恢复。"""


class RetryLimitExceeded(AgentError):
    """本轮已耗尽有界重试次数，交给用户界面报告并等待人工关闭。"""

    def __init__(
        self,
        message: str,
        *,
        phase: str = "",
        target: str = "",
        step: int | None = None,
        failures: int | None = None,
        recent_action: dict[str, Any] | None = None,
        observation: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.target = target
        self.step = step
        self.failures = failures
        self.recent_action = recent_action
        self.observation = observation


class ModelAccessError(AgentError):
    """视觉模型凭据或地区权限被拒绝，重试不能改变结果。"""
