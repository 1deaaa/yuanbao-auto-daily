#!/bin/sh
# 只在 Waydroid 容器未运行时处理服务恢复，避免中途打断 Android 会话。
set -eu

UNIT="waydroid-container.service"
LXC_PATH="/var/lib/waydroid/lxc"
FD_THRESHOLD="${WAYDROID_FD_THRESHOLD:-800}"
MODE="${1:---watch}"

log_message() {
    /usr/bin/logger -t waydroid-container-health -- "$*"
}

container_state() {
    /usr/bin/lxc-info -P "$LXC_PATH" -n waydroid -sH 2>/dev/null || printf '%s\n' STOPPED
}

case "$MODE" in
    --watch|--daily) ;;
    *)
        log_message "未知检查模式：$MODE"
        exit 2
        ;;
esac

active="$(/usr/bin/systemctl is-active "$UNIT" 2>/dev/null || true)"
if [ "$active" != "active" ]; then
    # 非 active 状态由 systemd 的 Restart 策略或管理员处理，不强行启动被主动停用的服务。
    exit 0
fi

pid="$(/usr/bin/systemctl show --value -p MainPID "$UNIT" 2>/dev/null || printf '%s\n' 0)"
case "$pid" in
    ''|*[!0-9]*) pid=0 ;;
esac
if [ "$pid" -le 0 ] || [ ! -d "/proc/$pid/fd" ]; then
    exit 0
fi

fd_count="$(find "/proc/$pid/fd" -mindepth 1 -maxdepth 1 -type l 2>/dev/null | wc -l)"
state="$(container_state)"

if [ "$MODE" = "--daily" ]; then
    # 每日任务前只重启空闲容器管理器，清掉前一轮留下的句柄。
    if [ "$state" != "RUNNING" ]; then
        log_message "每日任务前重启空闲容器管理器：state=$state fd=$fd_count"
        /usr/bin/systemctl restart "$UNIT"
    fi
elif [ "$state" != "RUNNING" ] && [ "$fd_count" -ge "$FD_THRESHOLD" ]; then
    log_message "检测到空闲容器管理器句柄偏高，执行恢复：state=$state fd=$fd_count threshold=$FD_THRESHOLD"
    /usr/bin/systemctl restart "$UNIT"
fi
