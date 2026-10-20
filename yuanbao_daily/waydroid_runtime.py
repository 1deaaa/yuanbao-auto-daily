"""Waydroid 会话生命周期：启动、停止、冻结恢复与应用拉起。"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any

from .constants import (
    APP_ANR_WINDOW_MARKERS,
    APP_READY_POLL_INTERVAL,
    APP_READY_TIMEOUT,
    APP_RESOLVE_POLL_INTERVAL,
    APP_RESOLVE_TIMEOUT,
    APPSTORE_PACKAGE_NAME,
    PACKAGE_NAME,
    WAYDROID_POLL_INTERVAL,
    WAYDROID_READY_TIMEOUT,
    WAYDROID_START_TIMEOUT,
    WAYDROID_STOP_TIMEOUT,
)
from .device import Device
from .errors import AgentError, AppUnresponsiveError, ManualActionRequired


class WaydroidRuntime:
    """管理一次任务所需的 Waydroid 会话生命周期。"""

    def __init__(
        self,
        start_timeout: float = WAYDROID_START_TIMEOUT,
        stop_timeout: float = WAYDROID_STOP_TIMEOUT,
        poll_interval: float = WAYDROID_POLL_INTERVAL,
    ):
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self.poll_interval = poll_interval
        self._session_process: Any | None = None
        self._session_started = False
        self.managed = False

    @property
    def session_started(self) -> bool:
        """返回本轮是否由运行时启动过用户会话。"""
        return self._session_started

    @staticmethod
    def _status() -> str:
        try:
            result = subprocess.run(
                ["waydroid", "status"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"无法读取 Waydroid 状态：{type(exc).__name__}") from exc
        return result.stdout

    @staticmethod
    def _extract_ip(status: str) -> str | None:
        match = re.search(r"^IP address:\s*(\d+\.\d+\.\d+\.\d+)\s*$", status, re.MULTILINE)
        return match.group(1) if match else None

    @staticmethod
    def _session_running(status: str) -> bool:
        return bool(re.search(r"^Session:\s*RUNNING\s*$", status, re.MULTILINE))

    @staticmethod
    def _container_frozen(status: str) -> bool:
        return bool(re.search(r"^Container:\s*FROZEN\s*$", status, re.MULTILINE))

    @staticmethod
    def _fully_stopped(status: str) -> bool:
        # Waydroid 没有用户会话时只输出 Session: STOPPED，不会输出 Container 行。
        # 只有明确报告容器仍在运行时才需要继续等待；缺少该行代表会话已退出。
        if not re.search(r"^Session:\s*STOPPED\s*$", status, re.MULTILINE):
            return False
        container = re.search(r"^Container:\s*(\S+)\s*$", status, re.MULTILINE)
        return container is None or container.group(1) == "STOPPED"

    def _start_session(self) -> None:
        try:
            self._session_process = subprocess.Popen(
                ["waydroid", "session", "start"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise AgentError(f"无法启动 Waydroid 会话：{type(exc).__name__}") from exc

    @staticmethod
    def launch_app(device: Device, package_name: str = PACKAGE_NAME) -> None:
        """通过 Waydroid 应用入口启动应用，登记活动应用并避免后台会话自动冻结。"""
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_name):
            raise AgentError(f"应用包名格式错误：{package_name!r}")
        if package_name == PACKAGE_NAME:
            # 应用宝的小窗可能抢占启动层；每日任务不依赖它，可以安全关闭。
            device.adb("shell", "am", "force-stop", APPSTORE_PACKAGE_NAME, timeout=20)
        # 不在正常路径 force-stop 元宝。ARM64 兼容层有时会留下无法回收的旧进程，
        # 随后的启动会卡在 Splash；ToolExecutor._restart_app 仅在恢复路径先清理。
        try:
            result = subprocess.run(
                ["waydroid", "app", "launch", package_name],
                capture_output=True,
                text=True,
                timeout=APP_RESOLVE_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"Waydroid 应用启动超时：{package_name}") from exc
        except OSError as exc:
            raise AgentError(f"无法调用 Waydroid 应用入口：{type(exc).__name__}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:180]
            raise AgentError(f"Waydroid 应用启动失败：{detail or '无输出'}")
        time.sleep(2)

    @staticmethod
    def _unfreeze_container() -> None:
        try:
            result = subprocess.run(
                ["waydroid", "container", "unfreeze"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AgentError(f"无法解冻 Waydroid 容器：{type(exc).__name__}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:180]
            raise AgentError(f"Waydroid 容器解冻失败：{detail or '无输出'}")

    def _recover_frozen_container(self) -> None:
        """优先解冻；普通用户无权操作容器时，重启一次用户会话作为后备。"""
        try:
            self._unfreeze_container()
            return
        except AgentError as exc:
            print(f"Waydroid 容器解冻不可用（{str(exc)[:140]}），正在重启用户会话")
        if not self.stop():
            raise AgentError("Waydroid 容器冻结且无法停止用户会话")
        self._start_session()
        self._session_started = True

    def discover_device(self, configured: str) -> Device:
        """必要时启动会话，并等待容器 IP 与 ADB 都可用。"""
        value = configured.strip()
        if value and value.lower() != "auto":
            return Device.discover(value)

        self.managed = True
        status = self._status()
        frozen_recovery_attempted = False
        if self._container_frozen(status):
            print("Waydroid 容器当前已冻结，正在解冻")
            self._recover_frozen_container()
            frozen_recovery_attempted = True
            status = self._status()
        if (
            not self._extract_ip(status)
            and not self._session_running(status)
            and not self._session_started
        ):
            self._start_session()
            self._session_started = True

        try:
            deadline = time.monotonic() + self.start_timeout
            last_error: AgentError | None = None
            unfreeze_attempted = frozen_recovery_attempted
            while time.monotonic() < deadline:
                status = self._status()
                if self._container_frozen(status) and not unfreeze_attempted:
                    unfreeze_attempted = True
                    print("Waydroid 容器在启动等待期间被冻结，正在解冻")
                    self._recover_frozen_container()
                    continue
                ip = self._extract_ip(status)
                if ip:
                    device = Device(f"{ip}:5555")
                    try:
                        device.connect()
                        remaining = max(1.0, deadline - time.monotonic())
                        device.wait_ready(timeout=min(WAYDROID_READY_TIMEOUT, remaining))
                        return device
                    except AgentError as exc:
                        last_error = exc

                if self._session_process is not None and self._session_process.poll() is not None:
                    if not self._session_running(status):
                        break
                time.sleep(self.poll_interval)

            if last_error is not None:
                raise AgentError(f"Waydroid 启动后 ADB 未就绪：{last_error}") from last_error
            raise AgentError("Waydroid 启动超时，未获得容器 IP")
        except AgentError:
            # 冷启动失败时清理本轮新建的会话，允许 systemd 的下一次重试从干净状态开始。
            if self._session_started:
                print("Waydroid 冷启动失败，正在清理半启动会话")
                self.stop()
            raise

    def stop(self) -> bool:
        """停止用户会话及其容器，并等待状态变为 STOPPED。"""
        try:
            result = subprocess.run(
                ["waydroid", "session", "stop"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"关闭 Waydroid 失败：{type(exc).__name__}")
            return False

        deadline = time.monotonic() + self.stop_timeout
        stopped = False
        while time.monotonic() < deadline:
            try:
                status = self._status()
            except AgentError:
                break
            if self._fully_stopped(status):
                stopped = True
                break
            time.sleep(self.poll_interval)

        if self._session_process is not None:
            try:
                self._session_process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                with contextlib.suppress(OSError):
                    self._session_process.terminate()
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    self._session_process.wait(timeout=2)
            self._session_process = None
        self._session_started = False

        if result.returncode != 0:
            return False
        return stopped
