"""向视觉模型公开的工具声明；工具名与参数是模型与执行层的契约。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .constants import TASK_ORDER, TASK_TARGET_COUNTS

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "open_welfare",
            "description": "优先通过新版聊天页左上角抽屉打开“福利中心”；旧版设备兼容“我们”页入口。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_daily_ability",
            "description": (
                "能力站主轮播卡的内容和颜色每天会变化。若最新截图确认主卡固定位置显示"
                "‘去体验’，将 button_visible 设为 true；执行器只点击主卡固定按钮位置并把体验页交给模型。"
                "顶部横幅里的同名按钮不算；未确认可见时设为 false。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "button_visible": {"type": "boolean"},
                },
                "required": ["button_visible"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "continue_daily_ability",
            "description": (
                "仅当最新截图已经显示能力卡打开的体验浮层或体验页面时调用；将当前页面交给模型继续处理，"
                "不会点击卡片。主福利页仍显示普通卡片时不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"experience_visible": {"type": "boolean"}},
                "required": ["experience_visible"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_daily_ability",
            "description": (
                "完成体验并返回福利中心后，读取主轮播卡固定位置的按钮状态，不需要识别标题或颜色。"
                "只有截图明确显示‘已完成’才报告完成；仍显示‘去体验’不得当作完成。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_status": {
                        "type": "string",
                        "enum": ["已完成", "去体验", "未显示"],
                    },
                },
                "required": ["card_status"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_tasks",
            "description": (
                "只读报告福利中心最新截图中的每日任务目录和实际计数。报告阶段的恢复进度可能过期，"
                "必须逐项读取当前页面，不得复制上下文或历史中的数字。2.88.0 新版目录固定为写作、"
                "拍题、P图三项；若福利页出现‘做同款’任务并明确显示已完成 x/3，须一并纳入；"
                "没有进度的推荐模板和邀请新用户不属于每日任务。"
                "只有在编排器已滚动到每日任务区域后，才能按当前截图报告进度。"
                "趣问元宝卡片的进度写作 (x/4)，x 是今天已完成的次数，"
                "按实际数字报告，不要套用 3 次上限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "available_tasks": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(TASK_ORDER)},
                        "uniqueItems": True,
                    },
                    "question": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": TASK_TARGET_COUNTS["question"],
                    },
                    "writing": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": TASK_TARGET_COUNTS["writing"],
                    },
                    "image": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": TASK_TARGET_COUNTS["image"],
                    },
                    "photo_question": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": TASK_TARGET_COUNTS["photo_question"],
                    },
                    "same_template": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": TASK_TARGET_COUNTS["same_template"],
                    },
                },
                "required": [
                    "available_tasks",
                    "question",
                    "writing",
                    "image",
                    "photo_question",
                    "same_template",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tap",
            "description": "点击当前截图中的一个可见位置。只能点击屏幕内坐标。",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hold_to_speak",
            "description": (
                "按住页面上的语音输入控件并说话，用于明确要求语音输入的页面。"
                "只需确认存在语音输入控件，无需自行判断坐标；"
                "程序会按住该控件并注入短测试语音。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_ms": {"type": "integer", "minimum": 600, "maximum": 10000},
                },
                "required": ["duration_ms"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "long_press",
            "description": "按住当前截图中可见的控件一段时间后释放。用于页面明确要求‘按住’的操作，如语音录制。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "duration_ms": {"type": "integer", "minimum": 600, "maximum": 10000},
                },
                "required": ["x", "y", "duration_ms"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "swipe",
            "description": "在当前屏幕内滑动以查看被遮挡的控件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "x2": {"type": "integer"},
                    "y2": {"type": "integer"},
                    "duration_ms": {"type": "integer"},
                },
                "required": ["x1", "y1", "x2", "y2"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_back",
            "description": "返回上一页；只在当前页面没有更明确的返回控件时使用。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "input_test_prompt",
            "description": "输入预置的最简测试文本，不要自行传入文本；提问和写作必须使用它。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "点击当前页面发送按钮。发送后必须立刻调用 wait_5s。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_5s",
            "description": "等待至少 5 秒，并继续检查右下角终止按钮，直到 AI 输出完成或超时。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_local_image",
            "description": (
                "选择程序准备好的本地脱敏测试图片。P图聊天页点击输入框左侧‘拍照’，"
                "相机页再点击语义按钮‘相册入口’；只有进入‘本地相册’并点中"
                "auto-daily-test.png 才算成功，不得使用固定屏幕坐标猜入口。"
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_image",
            "description": "确认刚才选择的图片，主要用于拍题流程。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claim_reward",
            "description": "如果出现“开心收下”奖励弹窗就点击收下；没有弹窗也可以调用。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "return_to_welfare",
            "description": "完成一次子任务后返回福利中心并刷新任务计数。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_exchange",
            "description": "兼容旧调用方的兑换商城入口工具；正常流程应由视觉模型点击当前截图中的兑换商城。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_exchange",
            "description": (
                "当最新截图明确已经显示兑换商城时，报告当前可见积分、QQ超级会员3天卡价格和目标商品是否在当前视口。"
                "无障碍层级为空时也以截图为准；不要猜测数字，未进入商城或看不到目标卡时不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page": {"type": "string", "enum": ["exchange"]},
                    "points": {"type": "integer", "minimum": 0},
                    "cost": {"type": "integer", "minimum": 1},
                    "target_visible": {"type": "boolean"},
                },
                "required": ["page", "points", "cost", "target_visible"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "redeem_qq_card",
            "description": "只定位 QQ 超级会员 3 天卡；读取当前积分和商品价格，积分不足时安全跳过兑换。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_exchange",
            "description": "确认兑换弹窗；只有目标商品已被定位后才能调用。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_prize_records",
            "description": "按兑换成功提示打开绿色的“前往奖品记录”。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_bound_reward",
            "description": "在奖品记录中定位目标卡并点击立即使用绑定账号。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_reward_use",
            "description": "确认绑定账号使用卡片，并要求页面出现成功证据。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "return_to_home",
            "description": "完成兑换处理后回到元宝首页；新版聊天首页和旧版“我们”页均有效。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "只有所有任务和奖品使用断言都通过后才能调用，调用后本轮结束并等待下一次计划运行。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]
