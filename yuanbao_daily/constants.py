"""集中存放全部可调参数与任务目录常量，避免散落在各执行层。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


DEFAULT_DEVICE = "auto"


DEFAULT_BASE_URL = "http://localhost:7860/v1"


DEFAULT_MODEL = "gemini-3.5-flash-lite"


DEFAULT_REASONING_EFFORT = "high"


REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})


DEFAULT_TIMEZONE = "Asia/Shanghai"


DEFAULT_RUN_TIME = "00:05"


DEFAULT_TEST_PROMPT = "###!!!### Connectivity test only. No writing needed. Reply only ok."


DEFAULT_TEST_IMAGE = str(ROOT / "测试题目.png")


DEFAULT_TEST_AUDIO = str(ROOT / "语音测试.wav")


PACKAGE_NAME = "com.tencent.hunyuan.app.chat"


DEFAULT_MAX_RETRIES = 3


DEFAULT_RETRY_COOLDOWN = 10


DEFAULT_MAX_STEPS = 260


DEFAULT_GENERATION_TIMEOUT = 180


DEFAULT_MODEL_REQUEST_TIMEOUT = 180


DEFAULT_CARD_NAME = "QQ超级会员3天卡"


DEFAULT_STATE_PATH = ROOT / ".yuanbao_daily_state.json"


DEFAULT_STOP_WAYDROID_AFTER_RUN = True


DEFAULT_AUTO_ACCEPT_PROTOCOL = True


TASK_STATE_SCHEMA = 2


DAILY_TASK_MODE_ABILITY_STATION = "ability_station"


# 这些工具的动作和成功条件完全由本地执行器掌握，不需要为每一步再请求视觉模型。


LOCAL_DETERMINISTIC_TOOLS = frozenset(
    {
        "input_test_prompt",
        "send_message",
        "wait_5s",
        "select_local_image",
        "confirm_image",
        "claim_reward",
        "return_to_welfare",
        "open_exchange",
        "redeem_qq_card",
        "confirm_exchange",
        "open_prize_records",
        "use_bound_reward",
        "confirm_reward_use",
        "return_to_home",
        "complete_task",
    }
)


# 页面重绘、输入法和奖励遮罩都可能让一次本地动作暂时失败；这些动作自身
# 有前置/后置校验，允许在同一画面上按 MAX_RETRIES 有界重试。视觉坐标点击
# 仍然严格拒绝同画面重复，避免模型坐标漂移造成连续误点。


RETRYABLE_SAME_STATE_TOOLS = LOCAL_DETERMINISTIC_TOOLS | {"wait_5s"}


# 视觉导航阶段由模型选择点击、滑动或返回；启动、报告和业务动作由编排器掌握。


VISUAL_NAVIGATION_TOOLS = frozenset({"tap", "swipe", "press_back"})


VISUAL_EXCHANGE_TOOLS = VISUAL_NAVIGATION_TOOLS | frozenset({"report_exchange"})


DAILY_ABILITY_TOOLS = frozenset(
    {"open_daily_ability", "continue_daily_ability"}
)


DAILY_ABILITY_EXPERIENCE_TOOLS = frozenset(
    {
        "tap",
        "hold_to_speak",
        "long_press",
        "swipe",
        "press_back",
        "return_to_welfare",
        "input_test_prompt",
        "send_message",
        "wait_5s",
        "select_local_image",
        "confirm_image",
    }
)


DAILY_ABILITY_EXPERIENCE_ACTION_MARKERS = (
    "去对话",
    "去精讲",
    "去批改",
    "去识图",
    "开始体验",
    "立即体验",
    "马上体验",
    "开始使用",
    "继续体验",
)


# 能力体验有时仍停留在福利 WebView 内，页面阶段会继续被识别为 welfare。
# 该浮层的关闭控件是比页面阶段更可靠的“仍在体验”证据。


DAILY_ABILITY_EXPERIENCE_OVERLAY_MARKERS = ("vip_learning_close_button",)


DAILY_ABILITY_EXPERIENCE_NAVIGATION_MARKERS = (
    "去对话",
    "去识图",
    "开始体验",
    "立即体验",
    "马上体验",
    "开始使用",
    "继续体验",
)


# 能力体验阶段允许的最大步数。体验页交互复杂（语音、问答、返回），但仍必须
# 有界：超过该步数就强制返回福利中心，避免整轮步数被一个页面耗尽。
DAILY_ABILITY_EXPERIENCE_STEP_LIMIT = 12


# 核验主轮播卡前等待福利页渲染的最长时间：返回福利页后 WebView 常有几秒
# 空层级或停在任务列表，直接核验会读不到主卡状态而空转。
DAILY_ABILITY_VERIFY_PREPARE_TIMEOUT = 15.0


DAILY_ABILITY_EXPERIENCE_NAVIGATION_TIMEOUT = 8.0


DAILY_ABILITY_EXPERIENCE_NAVIGATION_POLL_INTERVAL = 0.8


VISUAL_NAVIGATION_MAX_STEPS = 12


# 视觉动作失败通常只是目标离屏、浮层拦截或 WebView 尚未完成路由；这类失败
# 应回传给 VLMM 重新规划，而不是和 ADB/业务错误共用很小的重试额度。


VISUAL_REPLAN_MAX_ATTEMPTS = 8


# 只在福利任务目录连续多次没有任何语义/布局变化时收敛“目标不存在”；
# 单次看不到入口仍然必须交给 VLMM 滑动和重新观察。


STABLE_MISSING_TASK_SCROLLS = 6


WAYDROID_START_TIMEOUT = 90


WAYDROID_READY_TIMEOUT = 45


WAYDROID_STOP_TIMEOUT = 45


WAYDROID_POLL_INTERVAL = 1.0


APP_RESOLVE_TIMEOUT = 45


APP_RESOLVE_POLL_INTERVAL = 1.0


APP_READY_TIMEOUT = 90


APP_READY_POLL_INTERVAL = 1.0


STARTUP_OBSERVE_TIMEOUT = 45


STARTUP_OBSERVE_POLL_INTERVAL = 1.0


UI_DUMP_ATTEMPTS = 3


UI_DUMP_TIMEOUT = 8


UI_DUMP_RETRY_DELAY = 0.5


UI_DUMP_READ_ATTEMPTS = 4


UI_DUMP_READ_DELAY = 0.25


# 福利中心是 Chromium WebView，未压缩层级在重绘期间容易拖住 uiautomator。
# Android 的 uiautomator dump 从 API 18 起支持该选项，统一使用压缩树即可保留
# 文本、资源 ID 和边界等本项目所需字段，同时显著减少 ADB 传输量。


UI_DUMP_OPTIONS = ("--compressed",)


# 福利中心 WebView 的 Activity 标记。元宝 2.88 之后把福利页搬到了
# .biz.welfare.ui.WelfareWebActivity，旧的 com.tencent.yuanbao.mp.components
# .websdk.ext.ui.WebBrowserActivity 仍会出现在其它站内页，两者都要认。
WELFARE_ACTIVITY_MARKERS = ("webbrowseractivity", "welfarewebactivity")


WEBVIEW_UI_DUMP_ATTEMPTS = 1


WEBVIEW_UI_DUMP_TIMEOUT = 6


WEBVIEW_UI_DUMP_RETRY_DELAY = 1.5


ANDROID_WARNING_CHECK_INTERVAL = 3.0


ANDROID_WARNING_WINDOW_TITLE = "Android 系统"


ANDROID_WARNING_WINDOW_TYPE = "ty=SYSTEM_ERROR"


ANDROID_WARNING_WINDOW_FRAME = re.compile(
    r"^\s*Frames:.*?frame=\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
    re.MULTILINE,
)


APPSTORE_PACKAGE_NAME = "com.tencent.android.qqdownloader"


APP_ANR_WINDOW_MARKERS = ("Application Not Responding", "没有响应", "无响应")


ADB_TRANSPORT_FAILURE_MARKERS = (
    "adb 超时",
    "adb: device offline",
    "device offline",
    "cannot connect to",
    "transport endpoint is not connected",
    "broken pipe",
)


OURS_NAV_TIMEOUT = 30


OURS_NAV_POLL_INTERVAL = 1.0


OURS_FALLBACK_DELAY = 8.0


# 750x1333 实测首页底部“我们”入口的归一化位置；只用于已确认的首页空层级。


OURS_FALLBACK_X_RATIO = 0.852


OURS_FALLBACK_Y_RATIO = 0.963


# 900x1600 实测“我们”页福利入口的归一化位置；仅用于入口节点缺失时的后备点击。


WELFARE_ENTRY_FALLBACK_X_RATIO = 0.851


WELFARE_ENTRY_FALLBACK_Y_RATIO = 0.364


# 福利页主卡轮播内容和颜色会变化，只按固定主卡槽位的“去体验”按钮定位。


DAILY_ABILITY_RULE_VERSION = 4


DAILY_ABILITY_RULE_NAME = "main_carousel_experience_completed"


DAILY_ABILITY_BUTTON_X_RATIO = 0.62


DAILY_ABILITY_BUTTON_Y_RATIO = 0.516


DAILY_ABILITY_CARD_MIN_Y_RATIO = 0.40


DAILY_ABILITY_CARD_MAX_Y_RATIO = 0.62


DAILY_ABILITY_BUTTON_CHECK_LIMIT = 12


DAILY_ABILITY_VERIFY_CHECK_LIMIT = 8


WELFARE_TASK_LIST_SCROLL_START_RATIO = 0.83


WELFARE_TASK_LIST_SCROLL_END_RATIO = 0.31


WELFARE_TASK_CATALOG_SCROLL_MAX_ATTEMPTS = 3


WELFARE_REWARD_BUTTON_Y_RATIO = 0.61


WELFARE_LOAD_TIMEOUT = 30


WELFARE_LOAD_POLL_INTERVAL = 1.5


WELFARE_WEBVIEW_READY_DELAY = 3.0


# 新版聊天页应通过抽屉中的“福利中心”导航；旧版返回链接仍保留为无抽屉时的后备。


WELFARE_RETURN_LINK_FALLBACK_DELAY = 2.0


ENTRY_NAVIGATION_TIMEOUT = 12.0


ENTRY_NAVIGATION_POLL_INTERVAL = 1.0


TOOL_ARGUMENT_REPAIR_ATTEMPTS = 1


WRITING_SUBMIT_VERIFY_ATTEMPTS = 8


WRITING_SUBMIT_VERIFY_INTERVAL = 0.5


MAX_HISTORY_SUMMARY_CHARS = 1200


MAX_HISTORY_RESULT_CHARS = 1200


# 福利任务入口可能打开站内活动 WebView；这是已知误触，需要自动关回福利中心。
# 注意：“活动小窗”是常驻右上角的悬浮图标（3x3 像素级装饰节点），福利中心本身
# 也带它，不能作为活动页证据。只有真正的活动内容语义才能判定为活动页。


ACTIVITY_PAGE_MARKERS = (
    "活动规则",
    "参与方式",
    "评奖规则",
    "元宝ai视觉现场",
    "ai视觉现场",
)


FAILURE_EXIT_STATUS = 76


TASK_LABELS = {
    "question": "问元宝问题",
    "writing": "使用写作能力",
    "image": "使用P图能力",
    "photo_question": "使用拍题能力",
    "same_template": "使用推荐模板做同款",
}


TASK_ORDER = tuple(TASK_LABELS)


# 每项每日任务需要完成的次数。写作、拍题、P图和做同款都是 3 次；
# 趣问元宝卡片的进度显示为 (x/4)，问满 4 次才能领到积分奖励，
# 因此提问任务的目标次数是 4，不能沿用 3 次上限。
TASK_TARGET_COUNTS: dict[str, int] = {
    "question": 4,
    "writing": 3,
    "image": 3,
    "photo_question": 3,
    "same_template": 3,
}


TASK_MAX_PROGRESS = max(TASK_TARGET_COUNTS.values())


# 新版福利每日任务固定包含写作、拍题、P图；有明确累计进度的做同款任务也纳入。


NEW_WELFARE_DAILY_TASKS = ("writing", "photo_question", "image")


# 用户可通过 FORCE_ALL_DAILY_TASKS=true 要求本轮始终执行全部五类业务：
# 趣问元宝、写作、P图、拍题和做同款。页面是否存在入口仍交给 VLM 判断，
# 但不能因为当前目录缺少文案、一次点击失败或页面暂时空树而写入跳过集合。


FORCED_DAILY_TASKS = TASK_ORDER


        # 状态文件只是恢复优化，不能阻断当前已经完成的页面操作。
# 任务的固定子步骤集中维护。入口坐标和任务进度仍交给视觉模型，
# 进入具体能力页后的动作由本地执行器按这里的配方稳定完成。


TASK_STEPS: dict[str, tuple[str, ...]] = {
    "question": ("input", "sent", "waited"),
    "writing": ("input", "sent", "waited"),
    "image": ("image", "sent", "waited"),
    "photo_question": ("image", "confirmed", "waited"),
    "same_template": ("entry_clicked", "template_clicked", "sent", "waited"),
}


TEXT_TASKS = frozenset({"question", "writing"})


IMAGE_TASKS = frozenset({"image", "photo_question"})


# 入口缓存只在当前运行期间有效，不写入磁盘。布局变化或语义校验失败时，
# cached_action 会返回 None，下一步仍由视觉模型重新定位。
# “做同款”模板列表也在 WebView 活动里渲染，包含活动规则/参与方式等活动页
# 语义；用它定位任务行会误触活动横幅，因此语义入口不使用该标题。


TASK_ENTRY_MARKERS: dict[str, tuple[str, ...]] = {
    # 先列右侧行动按钮，再列任务标题。新版 WebView 只把这些文本节点
    # 暴露出来且 clickable=false；点击行动按钮的中心比点击标题稳定。
    "question": ("去提问", "问元宝任意问题", "提问"),
    "writing": ("去写作", "使用写作能力", "写作"),
    "image": ("去p图", "使用p图能力", "p图", "智能p图"),
    "photo_question": ("去拍题", "使用拍题能力", "拍题", "拍照答题"),
    "same_template": ("做同款模板", "试试大家都在玩的模版", "做同款"),
}


# 福利 WebView 有时只暴露任务标题，不暴露右侧行动按钮；有时又把整行
# 暴露成一个覆盖整行的 TextView。两种情况下都应以任务行的纵坐标为锚点，
# 点击右侧行动列，而不是点击标题节点中心。


TASK_ENTRY_ACTION_MARKERS: dict[str, tuple[str, ...]] = {
    "question": ("去提问",),
    "writing": ("去写作",),
    "image": ("去p图",),
    "photo_question": ("去拍题",),
    "same_template": ("做同款",),
}


# 趣问元宝卡片在 2.88 之后的标题文案；卡片标题是零高度节点，
# 用它可以把福利页滚回卡片位置，真正的入口是卡片里的推荐问题。
QUESTION_CARD_TITLE = "点击查看趣味内容或自主提问"


TASK_ENTRY_ROW_MARKERS: dict[str, tuple[str, ...]] = {
    "question": ("问元宝任意问题", "问元宝问题", QUESTION_CARD_TITLE),
    "writing": ("使用写作能力",),
    "image": ("使用p图能力",),
    "photo_question": ("使用拍题能力",),
    "same_template": (
        "使用推荐模板做同款",
        "做同款模板得积分",
        "试试大家都在玩的模版",
        "试试大家都在玩的模板",
    ),
}


LEGACY_DAILY_QUESTION_MARKERS = ("每日问元宝得积分", "每日问元宝")


# 每日任务行右侧行动按钮（“去写作/去拍题/去P图”）的中心横坐标比例。
# 2026-10-20 在 2.88.0 / 1080x1600 实测：按钮区间 [867,982]，中心 924，
# 占比 0.8556；旧值 0.965 会点到卡片右侧留白，点不到按钮。
TASK_ENTRY_ACTION_X_RATIO = 0.856


# 旧版“整行任务行”布局里，行动按钮贴在整行最右侧（实测 900 宽时中心 868）。
# 只有任务标题节点本身就铺满整行时才使用该比例。
LEGACY_TASK_ENTRY_ACTION_X_RATIO = 0.965


TASK_ENTRY_SCROLL_MAX_ATTEMPTS = 3


# 福利页改版后每日任务区顶部插入了签到卡与邀请/创作横幅，旧固定行坐标
# 已整体下移，继续盲点会误触“已完成”等非入口行。入口一律走无障碍语义
# 定位或视觉模型定位，不再保留任何固定行坐标。
