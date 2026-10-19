"""不可变数据结构：屏幕节点、观测快照与工具结果。"""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2


@dataclass(frozen=True)
class Node:
    text: str
    content_desc: str
    resource_id: str
    class_name: str
    clickable: bool
    enabled: bool
    bounds: Bounds
    # WebView/Compose 重绘期间，文字节点偶尔暂时变成 [0,0][0,0]。
    # 保留最近的可见父级区域，让语义校验仍能约束视觉点击范围。
    parent_bounds: Bounds | None = None
    parent_clickable: bool = False

    @property
    def searchable(self) -> str:
        return f"{self.text} {self.content_desc} {self.resource_id}".lower()

    @property
    def interaction_bounds(self) -> Bounds:
        """返回节点本身或最近可见父级的交互区域。"""
        return self.bounds if self.bounds.area > 0 else (self.parent_bounds or self.bounds)


    # Qwen 兼容端点偶尔把起点/终点作为 [x, y] 写进 x1/x2，
    # 有时同时填充 y1/y2，有时完全省略 y1/y2；按点坐标优先还原，
    # 避免把列表传给 int() 耗尽一次视觉重规划。
            # USB 序列号和本地模拟器序列号已经由 ADB 管理；只有网络设备需要先执行 connect。
                    # 冻结的 Waydroid 也可能返回 get-state=device；再做一次短 shell 探针。
                    # dumpsys 将活动网络编号和能力拆在 Current Networks 的
                    # NetworkAgentInfo 中，不能只检查“Active default network”一行。
                        # 精简实现或测试端点可能把 Capabilities 直接放在活动网络段。
                # 先删除旧文件，避免 uiautomator 返回空根节点时误读上一轮层级。
                    # uiautomator 偶尔先返回“已写入”再真正创建文件，短暂重读即可恢复。
        # 按钮通常位于弹窗右下角，使用比例可适配不同分辨率和窗口缩放。
        # wait_app_ready 已经拿到了窗口快照；先用快照处理，避免容器短暂冻结时
        # 再等待数轮 uiautomator 超时。
        # 容器冻结或系统弹窗切换瞬间，uiautomator 可能返回空根节点；此时
        # 只接受带有精确标题、SYSTEM_ERROR 类型和可见边界的系统窗口，避免误点业务页面。
                # 系统兼容性弹窗可能在 Activity 启动十几秒后才出现；在成功关闭前每轮检查，
                # 不能用“一次检查”标志把延迟出现的弹窗永久漏掉。
                    # 应用宝的桌面小窗可能压在元宝 Splash 上；每日任务不依赖它，关闭后再让元宝接管前台。
                # 人工登录和 ANR 都不能降级为普通超时；外层分别等待用户或重启会话。
        # 启动窗口超时通常意味着 ARM64 转译进程仍附着在旧实例；继续复用
        # 当前会话只会让下一次 Intent 再次被 ActivityManager 拒绝。交给外层
        # 的 AppUnresponsiveError 触发一次完整 Waydroid 会话重建。
        # PictureSelector 按媒体更新时间排序；触摸文件后再通知扫描器，确保本轮图片排在最前。
            # 应用宝的桌面小窗会抢占元宝启动层，并在 4GB 实例中诱发 ANR；每日任务不依赖它。
            # 元宝使用 ARM64 兼容层时，force-stop 可能留下旧进程记录，导致新 Activity
            # 被系统判定为“附着到旧进程”并永远停在 Splash。正常启动只发 Intent；
            # 故障恢复路径会在确认 ANR 或启动超时后显式清理进程。
        # Waydroid 没有用户会话时只输出 Session: STOPPED，不会输出 Container 行。
        # 只有明确报告容器仍在运行时才需要继续等待；缺少该行代表会话已退出。
            # 应用宝的小窗可能抢占启动层；每日任务不依赖它，可以安全关闭。
        # 不在正常路径 force-stop 元宝。ARM64 兼容层有时会留下无法回收的旧进程，
        # 随后的启动会卡在 Splash；ToolExecutor._restart_app 仅在恢复路径先清理。
            # 冷启动失败时清理本轮新建的会话，允许 systemd 的下一次重试从干净状态开始。


@dataclass(frozen=True)
class Observation:
    image: bytes
    ui_xml: str
    ui: str
    nodes: tuple[Node, ...]
    activity: str
    stage: str
    supplemental_task_progress: tuple[tuple[str, int], ...] = ()


        # WebView 常将任务标题和“已完成 x/3”分别暴露为兄弟节点；先查整行
        # 文本本身，避免零边界标题借用整页父容器后串到前一项的计数。
        # “做同款”入口只有在当前福利页明确显示累计进度时才是每日任务；
        # 单独出现的推荐模板按钮不加入任务目录。
    # WebView 重绘期间会保留上一页的零边界节点（例如不可见的“兑换商城”），
    # 这些节点不能覆盖当前截图中真实可见的福利任务语义。
    # 更新遮罩里的功能说明也会出现“拍题”等词，必须先标为应用阻塞层。
    # 福利页“做同款”等任务入口会打开站内活动 WebView；它们有明确的活动语义
    # （活动规则/参与方式/做同款），不能当成提问/写作输入页，否则本地输入会在
    # 错误页面上耗尽重试次数。节点为空但 Activity 明确是元宝 WebView 时也视为
    # 活动页候选，交给 _close_activity_page 按返回键关闭。
    # 新版活动专题页可能只暴露右侧“规则/记录”和专题内容，不再提供旧的
    # “活动规则/参与方式”文案。它仍然是福利 WebView 内的嵌套活动页，不能
    # 当作福利任务列表，否则兑换阶段会在专题卡片上盲点。
        # 新版页面的积分栏也带“兑换商城”，但能力站/任务目录才是福利页的
        # 正向证据。真实商城没有这些能力站或每日任务语义时再进入 exchange。
    # 福利任务目录会直接展示“使用拍题能力/使用P图能力”等能力名称，已完成
    # 的行还会隐藏“去拍题/去P图”按钮。只按能力关键词判断会把福利页误判成
    # 具体能力页，随后本地执行器会在错误页面上调用图片选择器。任务目录标题
    # 或多条任务行同时出现时，优先保留福利页语义；真正进入能力页后不会出现
    # 这些完整的任务行标题。
    # “我们”页的抽屉中有精确的“福利中心”入口和“任务”菜单；聊天首页的福利横幅
    # （“元宝福利中心”“做任务、得积分、兑福利”）必须排除，否则会被误判为“我们”页。
    # “智能P图”“拍题答疑”也会作为聊天页快捷入口常驻；只有没有聊天容器时，
    # 这些宽泛能力词才用于兜底识别具体能力页。


@dataclass
class ToolResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
