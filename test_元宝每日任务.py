import os
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import 元宝每日任务 as task
import 视觉安卓代理 as prototype


class 假设备:
    def display_size(self):
        return 750, 1333


class 假执行器:
    def __init__(self):
        self.reward_use_confirmed = True
        self.reward_unavailable = False
        self.calls = []
        self.prompt_variants = []

    def set_prompt_variant(self, variant):
        self.prompt_variants.append(variant)

    def execute(self, name, args):
        self.calls.append((name, args))
        return task.ToolResult(True, "测试成功")

    def _find_send(self, observation):
        return None

    def _find_input(self, observation):
        return task.Node(
            "已自动填充模板内容",
            "",
            "edConversationInput",
            "android.widget.EditText",
            True,
            True,
            task.Bounds(20, 1000, 720, 1300),
        )


class 元宝任务测试(unittest.TestCase):
    @staticmethod
    def 配置():
        """返回不读取开发者 .env 的测试配置，确保干净克隆也能运行测试。"""
        test_state_path = Path("/tmp/auto-daily-test-state.json")
        test_state_path.unlink(missing_ok=True)
        with patch.dict(
            os.environ,
            {"API_KEY": "test-only-key", "STATE_PATH": str(test_state_path)},
            clear=True,
        ):
            return task.Config.from_env(Path(__file__).with_name(".env.test"))

    def test_页面阶段优先级(self):
        def xml(text):
            return f'<hierarchy><node text="{text}" enabled="true" bounds="[0,0][100,100]" /></hierarchy>'

        self.assertEqual(task.detect_stage(task.parse_nodes(xml("每日问元宝得积分 兑换商城")), ""), "welfare")
        self.assertEqual(task.detect_stage(task.parse_nodes(xml("兑换商城 QQ超级会员3天卡")), ""), "exchange")
        self.assertEqual(task.detect_stage(task.parse_nodes(xml("奖品记录")), ""), "prize_records")
        self.assertEqual(task.detect_stage([], "com.tencent.hunyuan.app.chat/com.tencent.hunyuan.deps.camera.ui.activity.CameraResultActivity"), "photo_preview")
        self.assertEqual(task.detect_stage(task.parse_nodes(xml("相机胶卷")), "com.tencent.hunyuan.app.chat/com.tencent.hunyuan.app.PictureSelectorSupporterActivity"), "picker")
        self.assertEqual(task.detect_stage(task.parse_nodes(xml("本地相册")), "com.tencent.hunyuan.app.chat.RolePlayPickerActivity"), "picker")

    def test_聊天页福利横幅不会误判为我们页(self):
        """新版聊天首页横幅同时包含“福利中心”和“任务”，必须归为聊天页。"""
        chat_nodes = task.parse_nodes(
            '<hierarchy>'
            '<node text="元宝福利中心" enabled="true" bounds="[128,983][290,1022]" />'
            '<node text="做任务、得积分、兑福利" enabled="true" bounds="[128,1025][348,1054]" />'
            '<node text="[icon] 福利中心" enabled="true" bounds="[406,1004][512,1033]" />'
            '<node text="我们" enabled="true" bounds="[752,1566][784,1590]" />'
            '<node resource-id="com.tencent.hunyuan.app.chat:id/edConversationInput" '
            'enabled="true" bounds="[64,1360][704,1420]" />'
            '</hierarchy>'
        )
        self.assertEqual(
            task.detect_stage(
                chat_nodes, "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2"
            ),
            "chat",
        )
        ours_nodes = task.parse_nodes(
            '<hierarchy>'
            '<node text="福利中心" enabled="true" bounds="[724,569][808,598]" />'
            '<node text="任务" enabled="true" bounds="[136,695][190,734]" />'
            '</hierarchy>'
        )
        self.assertEqual(
            task.detect_stage(
                ours_nodes, "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2"
            ),
            "ours",
        )

    def test_福利入口兼容新旧文案(self):
        home = "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2"

        def observation(nodes, activity=home, stage="chat"):
            return task.Observation(b"", "", "", nodes, activity, stage)

        exact = task.parse_nodes(
            '<hierarchy>'
            '<node text="福利中心" enabled="true" bounds="[724,569][808,598]" />'
            '</hierarchy>'
        )
        self.assertEqual(
            task.ToolExecutor._welfare_entry_node(observation(exact, stage="ours")).text,
            "福利中心",
        )
        banner_nodes = task.parse_nodes(
            '<hierarchy>'
            '<node text="元宝福利中心" enabled="true" bounds="[128,983][290,1022]" />'
            '<node text="[icon] 福利中心" enabled="true" bounds="[406,1004][512,1033]" />'
            '</hierarchy>'
        )
        # 两个节点都含“福利中心”，取面积更小的行动按钮节点。
        self.assertEqual(
            task.ToolExecutor._welfare_entry_node(observation(banner_nodes)).text,
            "[icon] 福利中心",
        )
        # 模糊匹配只在首页生效，嵌套页或聊天记录里的同名文本不得误点。
        self.assertIsNone(
            task.ToolExecutor._welfare_entry_node(
                observation(
                    banner_nodes,
                    "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
                    "app",
                )
            )
        )
        self.assertIsNone(task.ToolExecutor._welfare_entry_node(observation(())))

    def test_当前页已有福利入口时不再绕行我们页(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 900
        executor.height = 1600
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        executor._welfare_context = False
        executor._welfare_scroll_normalized = False
        entry = task.Node(
            "[icon] 福利中心",
            "",
            "",
            "android.widget.TextView",
            False,
            True,
            task.Bounds(406, 1004, 512, 1033),
        )
        chat = task.Observation(
            b"",
            "",
            "",
            (entry,),
            "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2",
            "chat",
        )
        welfare = task.Observation(
            b"",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "welfare",
        )
        executor.observe = Mock(side_effect=[chat, welfare])
        executor._go_to_ours = Mock(side_effect=AssertionError("不应绕行“我们”页"))
        with patch("元宝每日任务.time.sleep"):
            result = executor._go_to_welfare()
        self.assertEqual(result.stage, "welfare")
        self.assertEqual(calls, [("tap", "459", "1018")])
        executor._go_to_ours.assert_not_called()

    def test_非首页缺少入口节点时回退我们页(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 900
        executor.height = 1600
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        executor._welfare_context = False
        executor._welfare_scroll_normalized = False
        nested = task.Observation(
            b"",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/com.tencent.hunyuan.app.AITemplateDetailActivity",
            "app",
        )
        ours = task.Observation(
            b"",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2",
            "ours",
        )
        welfare = task.Observation(
            b"",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "welfare",
        )
        executor.observe = Mock(side_effect=[nested, welfare])
        executor._go_to_ours = Mock(return_value=ours)
        with patch("元宝每日任务.time.sleep"):
            result = executor._go_to_welfare()
        self.assertEqual(result.stage, "welfare")
        executor._go_to_ours.assert_called_once()


    def test_配置和北京时间调度(self):
        config = self.配置()
        self.assertEqual(config.model_id, "gemini-3.5-flash-lite")
        self.assertEqual(config.reasoning_effort, "high")
        self.assertEqual(config.run_time, "00:05")
        self.assertEqual(config.timezone_name, "Asia/Shanghai")
        self.assertEqual(config.card_name, "QQ超级会员3天卡")
        self.assertTrue(config.auto_accept_protocol)
        self.assertEqual(config.model_request_timeout_seconds, 180)
        self.assertTrue(config.stop_waydroid_after_run)
        self.assertTrue(config.test_image_path.is_file())
        before = datetime(2026, 8, 27, 0, 4, tzinfo=ZoneInfo("Asia/Shanghai"))
        after = datetime(2026, 8, 27, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(task.next_run_at(config, before).hour, 0)
        self.assertEqual(task.next_run_at(config, before).minute, 5)
        self.assertEqual(task.next_run_at(config, after).day, 28)

    def test_推理强度从环境变量读取并校验(self):
        with patch.dict(
            os.environ,
            {"API_KEY": "test-only-key", "REASONING_EFFORT": "LOW"},
            clear=True,
        ):
            config = task.Config.from_env(Path(__file__).with_name(".env.test"))
        self.assertEqual(config.reasoning_effort, "low")

        with patch.dict(
            os.environ,
            {"API_KEY": "test-only-key", "REASONING_EFFORT": "unsupported"},
            clear=True,
        ):
            with self.assertRaisesRegex(task.AgentError, "REASONING_EFFORT"):
                task.Config.from_env(Path(__file__).with_name(".env.test"))

    def test_已知Android系统提示自动消除(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        calls = []
        executor.width = 750
        executor.height = 1333
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        nodes = task.parse_nodes(
            '<hierarchy>'
            '<node text="您的设备内部出现了问题。请联系您的设备制造商了解详情。" '
            'class="android.widget.TextView" enabled="true" bounds="[88,627][662,695]" />'
            '<node text="确定" resource-id="android:id/button1" '
            'class="android.widget.Button" clickable="true" enabled="true" bounds="[555,701][645,777]" />'
            '</hierarchy>'
        )
        self.assertTrue(executor._dismiss_android_warning(nodes))
        self.assertEqual(calls, [("tap", "600", "739")])

    def test_元宝更新提示自动消除(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 750
        executor.height = 1333
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        nodes = task.parse_nodes(
            '<hierarchy>'
            '<node text="元宝新版本" resource-id="com.tencent.hunyuan.app.chat:id/title" '
            'class="android.widget.TextView" enabled="true" bounds="[305,295][445,337]" />'
            '<node resource-id="com.tencent.hunyuan.app.chat:id/upgrade_dialog" '
            'class="android.view.ViewGroup" enabled="true" bounds="[56,229][694,687]" />'
            '<node resource-id="com.tencent.hunyuan.app.chat:id/skip" '
            'class="android.widget.ImageView" clickable="true" enabled="true" '
            'bounds="[350,636][401,687]" />'
            '</hierarchy>'
        )
        self.assertTrue(executor._dismiss_upgrade_prompt(nodes))
        self.assertEqual(calls, [("tap", "375", "661")])

    def test_更新提示不会误判为拍题页面(self):
        nodes = task.parse_nodes(
            '<hierarchy>'
            '<node text="新版本 v2.83.10" resource-id="com.tencent.hunyuan.app.chat:id/version" '
            'enabled="true" bounds="[300,300][450,340]" />'
            '<node resource-id="com.tencent.hunyuan.app.chat:id/upgrade_dialog" '
            'enabled="true" bounds="[56,229][694,687]" />'
            '<node text="拍题输入文字都能讲" enabled="true" bounds="[100,400][600,520]" />'
            '</hierarchy>'
        )
        self.assertEqual(task.detect_stage(nodes, ""), "app")

    def test_无障碍空根节点不会复用旧层级(self):
        device = task.Device("192.168.240.112:5555")
        valid_xml = '<hierarchy><node text="当前页面" enabled="true" bounds="[0,0][10,10]" /></hierarchy>'.encode()
        with (
            patch.object(
                device,
                "adb",
                side_effect=[
                    b"",
                    b"ERROR: null root node returned by UiTestAutomationBridge.",
                    b"",
                    b"UI hierchary dumped to: /sdcard/auto_daily_ui.xml",
                    valid_xml,
                ],
            ) as adb,
            patch("元宝每日任务.time.sleep"),
        ):
            self.assertEqual(device.ui_dump(), valid_xml.decode())
        self.assertEqual(adb.call_args_list[0].args[:4], ("shell", "rm", "-f", "/sdcard/auto_daily_ui.xml"))
        self.assertEqual(
            adb.call_args_list[1].args[:5],
            ("shell", "uiautomator", "dump", "--compressed", "/sdcard/auto_daily_ui.xml"),
        )
        self.assertEqual(adb.call_count, 5)

    def test_无障碍文件短暂未落盘时重读(self):
        device = task.Device("192.168.240.112:5555")
        valid_xml = '<hierarchy><node text="当前页面" enabled="true" bounds="[0,0][10,10]" /></hierarchy>'.encode()
        with (
            patch.object(
                device,
                "adb",
                side_effect=[
                    b"",
                    b"UI hierchary dumped to: /sdcard/auto_daily_ui.xml",
                    task.AgentError("文件尚未落盘"),
                    valid_xml,
                ],
            ) as adb,
            patch("元宝每日任务.time.sleep"),
        ):
            self.assertEqual(device.ui_dump(), valid_xml.decode())
        self.assertEqual(adb.call_count, 4)

    def test_WebView无障碍树未空闲时退回截图观测(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.device = SimpleNamespace(
            screenshot=lambda: b"png",
            activity=lambda: "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            ui_dump=Mock(side_effect=task.AgentError("Android 无障碍树不可用")),
        )
        with patch("元宝每日任务.print") as output:
            observation = executor.observe()
        self.assertEqual(observation.stage, "welfare")
        self.assertEqual(observation.nodes, ())
        self.assertTrue(observation.activity.endswith("WebBrowserActivity"))
        executor.device.ui_dump.assert_called_once_with(attempts=1, timeout=3)
        output.assert_called_once()

    def test_无障碍失败时按窗口边界自动关闭系统提示(self):
        device = task.Device("192.168.240.112:5555")
        windows = (
            "Window #2 Window{49a310a u0 Android 系统}:\n"
            "  mAttrs={(0,0)(wrapxwrap) ty=SYSTEM_ERROR}\n"
            "  isVisible=true\n"
            "  Frames: parent=[0,0][1050,1867] display=[0,0][1050,1867] "
            "frame=[147,802][903,1065] last=[147,802][903,1065]\n"
        ).encode()
        with (
            patch.object(device, "ui_dump", side_effect=task.AgentError("空根节点")),
            patch.object(device, "adb", side_effect=[windows, b""]) as adb,
            patch("元宝每日任务.time.sleep"),
        ):
            self.assertTrue(device.dismiss_android_warning())
        self.assertEqual(adb.call_args_list[0].args[:4], ("shell", "dumpsys", "window", "windows"))
        self.assertEqual(
            adb.call_args_list[1].args[:5], ("shell", "input", "tap", "824", "1003")
        )

    def test_应用启动窗口未消失时不会交给模型(self):
        device = task.Device("192.168.240.112:5555")
        with (
            patch.object(
                device,
                "adb",
                side_effect=[
                    b"topResumedActivity=ActivityRecord{u0 com.tencent.hunyuan.app.chat/.Main}",
                    b"Window{1 u0 com.tencent.hunyuan.app.chat/SplashScreen}",
                    b"",
                    b"topResumedActivity=ActivityRecord{u0 com.tencent.hunyuan.app.chat/.Main}",
                    b"Window{2 u0 com.tencent.hunyuan.app.chat/.Main}",
                    b"",
                ],
            ) as adb,
            patch.object(device, "dismiss_android_warning", return_value=False),
            patch("元宝每日任务.time.sleep"),
        ):
            device.wait_app_ready("com.tencent.hunyuan.app.chat", timeout=1)
        self.assertEqual(adb.call_count, 6)

    def test_等待Android默认网络验证(self):
        device = task.Device("192.168.240.112:5555")
        with (
            patch.object(
                device,
                "adb",
                side_effect=[
                    b"Active default network: 100\nCapabilities: INTERNET\n",
                    b"Active default network: 100\nCapabilities: INTERNET&VALIDATED\n",
                ],
            ) as adb,
            patch("元宝每日任务.time.sleep"),
        ):
            device.wait_network_ready(timeout=2, poll_interval=0)
        self.assertEqual(adb.call_count, 2)
        adb.assert_called_with("shell", "dumpsys", "connectivity", timeout=10)

    def test_等待Android默认网络按活动编号匹配能力(self):
        device = task.Device("192.168.240.112:5555")
        connectivity = (
            "Active default network: 100\n"
            "Current network preferences:\n"
            "Current Networks:\n"
            "  NetworkAgentInfo{network{100} handle{1} ni{Ethernet CONNECTED} "
            "nc{[ Transports: ETHERNET Capabilities: INTERNET&VALIDATED ]}}\n"
            "  NetworkAgentInfo{network{101} handle{2} ni{Ethernet CONNECTED} "
            "nc{[ Transports: ETHERNET Capabilities: INTERNET ]}}\n"
            "Inactivity Timers:\n"
        ).encode()
        with patch.object(device, "adb", return_value=connectivity) as adb:
            device.wait_network_ready(timeout=1, poll_interval=0)
        adb.assert_called_once_with("shell", "dumpsys", "connectivity", timeout=10)

    def test_ADB传输故障触发会话清理分类(self):
        self.assertTrue(task.is_adb_transport_failure(task.AgentError("ADB 超时：shell")))
        self.assertTrue(task.is_adb_transport_failure(task.AgentError("adb: device offline")))
        self.assertFalse(task.is_adb_transport_failure(task.AgentError("模型动作解析失败")))

    def test_默认网络未验证时有界失败(self):
        device = task.Device("192.168.240.112:5555")
        with (
            patch.object(
                device,
                "adb",
                return_value=b"Active default network: 100\nCapabilities: INTERNET\n",
            ),
            patch("元宝每日任务.time.sleep"),
            self.assertRaisesRegex(task.AgentError, "默认网络未就绪"),
        ):
            device.wait_network_ready(timeout=0, poll_interval=0)

    def test_启动阶段ANR立即报告为可恢复错误(self):
        device = task.Device("192.168.240.112:5555")
        with patch.object(
            device,
            "adb",
            side_effect=[
                b"topResumedActivity=ActivityRecord{u0 com.tencent.hunyuan.app.chat/.Main}",
                "Window{1 u0 com.tencent.hunyuan.app.chat/.Main} title=元宝没有响应".encode(),
            ],
        ) as adb:
            with self.assertRaises(task.AppUnresponsiveError):
                device.wait_app_ready("com.tencent.hunyuan.app.chat", timeout=30)
        self.assertEqual(adb.call_count, 2)

    def test_ActivityManager明确标记元宝ANR(self):
        device = task.Device("192.168.240.112:5555")
        processes = (
            "  *APP* UID 10127 ProcessRecord{abc 1234:com.tencent.hunyuan.app.chat/u0a127}\n"
            "    packageList={com.tencent.hunyuan.app.chat}\n"
            "    mCrashing=false null mNotResponding=true [dialog]\n"
            "  *APP* UID 1000 ProcessRecord{def 5678:android.system/u0}\n"
            "    mCrashing=false null mNotResponding=false\n"
        ).encode()
        with patch.object(device, "adb", return_value=processes) as adb:
            self.assertTrue(device.app_not_responding())
        adb.assert_called_once_with("shell", "dumpsys", "activity", "processes", timeout=15)

    def test_人工阻塞时保留Waydroid现场(self):
        config = self.配置()
        runtime = Mock()
        runtime.managed = True
        device = Mock()
        device.keep_awake.return_value = "0"
        device.wait_app_ready.side_effect = task.ManualActionRequired("需要人工处理")
        runtime.discover_device.return_value = device
        with patch("元宝每日任务.WaydroidRuntime", return_value=runtime):
            with self.assertRaises(task.ManualActionRequired):
                task.run_once(config)
        device.restore_keep_awake.assert_not_called()
        runtime.stop.assert_not_called()

    def test_首页空层级只在已确认Activity按一次比例后备(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 750
        executor.height = 1333
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        empty_home = task.Observation(
            b"",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2",
            "app",
        )
        ours = task.Observation(
            b"",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2",
            "ours",
        )
        executor.observe = Mock(side_effect=[empty_home, ours])
        clock = iter([0, 9, 10])
        with patch("元宝每日任务.time.monotonic", side_effect=lambda: next(clock)), patch(
            "元宝每日任务.time.sleep"
        ):
            result = executor._navigate_to_ours_until(30)
        self.assertIs(result, ours)
        self.assertEqual(calls, [("tap", "639", "1284")])

    def test_协议自动处理且登录页标记为人工阻塞(self):
        protocol = task.Observation(
            b"", "", "", tuple(task.parse_nodes(
                '<hierarchy><node text="欢迎使用 元宝" enabled="true" bounds="[0,0][10,10]" />'
                '<node text="同意并继续" enabled="true" bounds="[0,0][10,10]" /></hierarchy>'
            )), "com.tencent.hunyuan.app.chat/.Login", "app"
        )
        login = task.Observation(
            b"", "", "", tuple(task.parse_nodes(
                '<hierarchy><node text="手机号登录" enabled="true" bounds="[0,0][10,10]" /></hierarchy>'
            )), "com.tencent.hunyuan.app.chat/.Login", "app"
        )
        # 协议页由观测阶段的精确按钮处理；这里不再降级为人工阻塞。
        self.assertIsNone(task.ToolExecutor._manual_blocker(protocol))
        self.assertIn("未登录", task.ToolExecutor._manual_blocker(login))

    def test_协议页精确自动同意(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.config = SimpleNamespace(auto_accept_protocol=True)
        executor.width = 750
        executor.height = 1333
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        nodes = task.parse_nodes(
            '<hierarchy>'
            '<node text="欢迎使用 元宝" enabled="true" bounds="[145,470][605,560]" />'
            '<node text="同意并继续" class="android.widget.Button" clickable="true" '
            'enabled="true" bounds="[381,744][570,820]" />'
            '</hierarchy>'
        )
        activity = "com.tencent.hunyuan.app.chat/.biz.login.v2.HYLoginMainActivity"
        self.assertTrue(executor._dismiss_protocol_prompt(nodes, activity))
        self.assertEqual(calls, [("tap", "475", "782")])

    def test_Compose协议文字节点不可点击时仍自动同意(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.config = SimpleNamespace(auto_accept_protocol=True)
        executor.width = 900
        executor.height = 1600
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        nodes = task.parse_nodes(
            '<hierarchy>'
            '<node text="欢迎使用 元宝" enabled="true" bounds="[378,658][522,692]" />'
            '<node text="同意并继续" class="android.widget.TextView" clickable="false" '
            'enabled="true" bounds="[503,914][593,940]" />'
            '</hierarchy>'
        )
        activity = "com.tencent.hunyuan.app.chat/.biz.login.v2.HYLoginMainActivity"
        self.assertTrue(executor._dismiss_protocol_prompt(nodes, activity))
        self.assertEqual(calls, [("tap", "548", "927")])

    def test_协议页按钮不可点击时仍保留人工阻塞(self):
        observation = task.Observation(
            b"", "", "", tuple(task.parse_nodes(
                '<hierarchy><node text="欢迎使用 元宝" enabled="true" bounds="[0,0][10,10]" />'
                '<node text="同意并继续" enabled="true" bounds="[0,0][10,10]" /></hierarchy>'
            )),
            "com.tencent.hunyuan.app.chat/.biz.login.v2.HYLoginMainActivity",
            "app",
        )
        self.assertIn("协议", task.ToolExecutor._manual_blocker(observation))

    def test_导航超时后第二轮仍执行完整导航(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        ours = task.Observation(b"", "", "", (), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "ours")
        executor._navigate_to_ours_until = Mock(side_effect=[task.AgentError("首次导航超时"), ours])
        executor._restart_app = Mock()
        result = executor._go_to_ours()
        self.assertIs(result, ours)
        self.assertEqual(executor._navigate_to_ours_until.call_count, 2)
        executor._restart_app.assert_called_once_with()

    def test_人工阻塞不会被工具层吞掉(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.pending_generation = False
        executor._go_to_welfare = Mock(side_effect=task.ManualActionRequired("需要登录"))
        with self.assertRaises(task.ManualActionRequired):
            executor.execute("open_welfare", {})

    def test_已有Waydroid会话启动失败不被关闭(self):
        config = self.配置()
        runtime = Mock()
        runtime.managed = True
        runtime.session_started = False
        device = Mock()
        device.keep_awake.return_value = "0"
        runtime.launch_app.side_effect = task.AgentError("首屏加载失败")
        runtime.discover_device.return_value = device
        with patch("元宝每日任务.WaydroidRuntime", return_value=runtime):
            with self.assertRaises(task.AgentError):
                task.run_once(config)
        self.assertEqual(runtime.launch_app.call_count, 2)
        runtime.launch_app.assert_any_call(device, task.PACKAGE_NAME)
        device.launch_app.assert_not_called()
        runtime.stop.assert_not_called()

    def test_Waydroid停止时自动启动并等待ADB(self):
        runtime = task.WaydroidRuntime(start_timeout=1, poll_interval=0)
        statuses = iter(
            [
                "Session:\tSTOPPED\nContainer:\tSTOPPED\n",
                "Session:\tRUNNING\nIP address:\t192.168.240.112\n",
            ]
        )
        with (
            patch.object(runtime, "_status", side_effect=lambda: next(statuses)),
            patch.object(runtime, "_start_session") as start_session,
            patch.object(task.Device, "connect") as connect,
            patch.object(task.Device, "wait_ready") as wait_ready,
        ):
            device = runtime.discover_device("auto")
        start_session.assert_called_once_with()
        connect.assert_called_once_with()
        wait_ready.assert_called_once()
        self.assertEqual(device.serial, "192.168.240.112:5555")
        self.assertTrue(runtime.managed)

    def test_Waydroid冷启动失败清理半启动会话(self):
        runtime = task.WaydroidRuntime(start_timeout=0, poll_interval=0)
        with (
            patch.object(
                runtime,
                "_status",
                return_value="Session:\tSTOPPED\nContainer:\tSTOPPED\n",
            ),
            patch.object(runtime, "_start_session") as start_session,
            patch.object(runtime, "stop") as stop,
        ):
            with self.assertRaises(task.AgentError):
                runtime.discover_device("auto")
        start_session.assert_called_once_with()
        stop.assert_called_once_with()

    def test_Waydroid停止等待会话退出(self):
        runtime = task.WaydroidRuntime(stop_timeout=1, poll_interval=0)
        statuses = iter(
            [
                "Session:\tRUNNING\nContainer:\tRUNNING\n",
                "Session:\tSTOPPED\nContainer:\tSTOPPED\n",
            ]
        )
        completed = SimpleNamespace(returncode=0)
        with (
            patch("元宝每日任务.subprocess.run", return_value=completed) as run,
            patch.object(runtime, "_status", side_effect=lambda: next(statuses)),
        ):
            self.assertTrue(runtime.stop())
        run.assert_called_once_with(
            ["waydroid", "session", "stop"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_Waydroid标准停止状态可省略容器行(self):
        self.assertTrue(
            task.WaydroidRuntime._fully_stopped(
                "Session:\tSTOPPED\nVendor type:\tMAINLINE\n"
            )
        )
        self.assertFalse(
            task.WaydroidRuntime._fully_stopped(
                "Session:\tSTOPPED\nContainer:\tRUNNING\n"
            )
        )

    def test_Waydroid冻结容器先解冻再发现设备(self):
        runtime = task.WaydroidRuntime(start_timeout=1, poll_interval=0)
        statuses = iter(
            [
                "Session:\tRUNNING\nContainer:\tFROZEN\nIP address:\t192.168.240.112\n",
                "Session:\tRUNNING\nContainer:\tRUNNING\nIP address:\t192.168.240.112\n",
                "Session:\tRUNNING\nContainer:\tRUNNING\nIP address:\t192.168.240.112\n",
            ]
        )
        with (
            patch.object(runtime, "_status", side_effect=lambda: next(statuses)),
            patch.object(runtime, "_unfreeze_container") as unfreeze,
            patch.object(task.Device, "connect"),
            patch.object(task.Device, "wait_ready"),
        ):
            device = runtime.discover_device("auto")
        unfreeze.assert_called_once_with()
        self.assertEqual(device.serial, "192.168.240.112:5555")

    def test_普通用户无权解冻时重启Waydroid会话(self):
        runtime = task.WaydroidRuntime(start_timeout=1, poll_interval=0)
        statuses = iter(
            [
                "Session:\tRUNNING\nContainer:\tFROZEN\nIP address:\t192.168.240.112\n",
                "Session:\tSTOPPED\nContainer:\tSTOPPED\n",
                "Session:\tRUNNING\nIP address:\t192.168.240.112\n",
                "Session:\tRUNNING\nIP address:\t192.168.240.112\n",
            ]
        )
        with (
            patch.object(runtime, "_status", side_effect=lambda: next(statuses)),
            patch.object(runtime, "_unfreeze_container", side_effect=task.AgentError("需要 root")) as unfreeze,
            patch.object(runtime, "stop", return_value=True) as stop,
            patch.object(runtime, "_start_session") as start_session,
            patch.object(task.Device, "connect"),
            patch.object(task.Device, "wait_ready"),
        ):
            device = runtime.discover_device("auto")
        unfreeze.assert_called_once_with()
        stop.assert_called_once_with()
        start_session.assert_called_once_with()
        self.assertEqual(device.serial, "192.168.240.112:5555")

    def test_显式ADB设备启动应用不调用Waydroid(self):
        device = task.Device("emulator-5554")
        with (
            patch.object(
                device,
                "adb",
                side_effect=[
                    b"priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n"
                    b"com.example.test/.MainActivity\n",
                    b"Starting: Intent { cmp=com.example.test/.MainActivity }",
                ],
            ) as adb,
            patch("元宝每日任务.time.sleep"),
        ):
            device.launch_app("com.example.test")
        self.assertEqual(adb.call_count, 2)
        adb.assert_any_call(
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "com.example.test",
            timeout=20,
        )
        adb.assert_any_call(
            "shell",
            "am",
            "start",
            "-W",
            "-n",
            "com.example.test/.MainActivity",
            timeout=30,
        )

    def test_Waydroid刚启动时等待应用入口就绪(self):
        device = task.Device("192.168.240.112:5555")
        with (
            patch.object(
                device,
                "adb",
                side_effect=[
                    b"priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n",
                    b"priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n"
                    b"com.example.test/.MainActivity\n",
                    b"Starting: Intent { cmp=com.example.test/.MainActivity }",
                ],
            ) as adb,
            patch("元宝每日任务.time.sleep"),
        ):
            device.launch_app("com.example.test")
        self.assertEqual(adb.call_count, 3)
        self.assertEqual(adb.call_args_list[0], adb.call_args_list[1])
        self.assertEqual(adb.call_args_list[2].args[-1], "com.example.test/.MainActivity")

    def test_后台Waydroid任务临时保持唤醒并恢复设置(self):
        device = task.Device("192.168.240.112:5555")
        with patch.object(device, "adb", side_effect=[b"0\n", b"", b"", b"7\n"]) as adb:
            previous = device.keep_awake()
            device.restore_keep_awake(previous)
        self.assertEqual(previous, "0")
        self.assertEqual(adb.call_args_list[0].args[-2:], ("global", "stay_on_while_plugged_in"))
        self.assertEqual(adb.call_args_list[1].args[-3:], ("power", "stayon", "true"))
        self.assertEqual(adb.call_args_list[2].args[-3:], ("power", "stayon", "false"))
        self.assertEqual(adb.call_args_list[3].args[-3:], ("global", "stay_on_while_plugged_in", "0"))

    def test_冷启动首页导航等待层级就绪并点击实际节点(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 750
        executor.height = 1333
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        ours = task.Node("我们", "", "", "android.widget.TextView", False, True, task.Bounds(620, 1265, 659, 1304))
        observations = iter(
            [
                task.Observation(b"", "", "", (), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "unknown"),
                task.Observation(b"", "", "", (ours,), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "chat"),
                task.Observation(b"", "", "", (ours,), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "ours"),
            ]
        )
        executor.observe = Mock(side_effect=lambda: next(observations))
        with patch("元宝每日任务.time.sleep"):
            result = executor._go_to_ours()
        self.assertEqual(result.stage, "ours")
        self.assertEqual(calls, [("tap", "639", "1284")])

    def test_展开输入浮层先收起再导航(self):
        """新版聊天页发送后的全屏输入浮层会吞掉底部导航，必须先收起。"""
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 900
        executor.height = 1600
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        collapse = task.Node(
            "",
            "收起输入框",
            "com.tencent.hunyuan.app.chat:id/session_input_arrow_down",
            "android.widget.ImageView",
            True,
            True,
            task.Bounds(818, 173, 859, 220),
        )
        ours = task.Node(
            "",
            "我们",
            "",
            "android.widget.ImageView",
            False,
            True,
            task.Bounds(744, 1517, 791, 1564),
        )
        observations = iter(
            [
                task.Observation(
                    b"", "", "", (collapse,), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "chat"
                ),
                task.Observation(
                    b"", "", "", (ours,), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "chat"
                ),
                task.Observation(
                    b"", "", "", (ours,), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "ours"
                ),
            ]
        )
        executor.observe = Mock(side_effect=lambda: next(observations))
        with patch("元宝每日任务.time.sleep"):
            result = executor._go_to_ours()
        self.assertEqual(result.stage, "ours")
        self.assertEqual(calls, [("tap", "838", "196"), ("tap", "767", "1540")])

    def test_没有展开输入浮层时不额外点击(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 900
        executor.height = 1600
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        ours = task.Node(
            "",
            "我们",
            "",
            "android.widget.ImageView",
            False,
            True,
            task.Bounds(744, 1517, 791, 1564),
        )
        observations = iter(
            [
                task.Observation(
                    b"", "", "", (ours,), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "chat"
                ),
                task.Observation(
                    b"", "", "", (ours,), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "ours"
                ),
            ]
        )
        executor.observe = Mock(side_effect=lambda: next(observations))
        with patch("元宝每日任务.time.sleep"):
            result = executor._go_to_ours()
        self.assertEqual(result.stage, "ours")
        self.assertEqual(calls, [("tap", "767", "1540")])

    def test_模板详情Activity被归类为app时仍会返回(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 750
        executor.height = 1333
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        nested = task.Observation(
            b"",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/com.tencent.hunyuan.app.AITemplateDetailActivity",
            "app",
        )
        ours = task.Observation(b"", "", "", (), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "ours")
        # 导航等待每轮读取两次时钟；连续观察到详情页超过 12 秒后应返回首页。
        observations = iter([nested] * 7 + [ours])
        executor.observe = Mock(side_effect=lambda: next(observations))
        clock = iter(range(0, 40))
        with patch("元宝每日任务.time.monotonic", side_effect=lambda: next(clock)), patch(
            "元宝每日任务.time.sleep"
        ):
            result = executor._go_to_ours()
        self.assertEqual(result.stage, "ours")
        self.assertIn(("keyevent", "KEYCODE_BACK"), calls)

    def test_嵌套页面返回超时会周期性重试返回键(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 750
        executor.height = 1333
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        nested = task.Observation(
            b"",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/com.tencent.hunyuan.app.AITemplateDetailActivity",
            "app",
        )
        ours = task.Observation(
            b"", "", "", (), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "ours"
        )
        executor.observe = Mock(side_effect=[nested] * 16 + [ours])
        clock = iter(range(0, 80))
        with patch("元宝每日任务.time.monotonic", side_effect=lambda: next(clock)), patch(
            "元宝每日任务.time.sleep"
        ):
            result = executor._navigate_to_ours_until(40)
        self.assertEqual(result.stage, "ours")
        self.assertGreaterEqual(calls.count(("keyevent", "KEYCODE_BACK")), 2)

    def test_福利WebView无障碍树为空仍交给模型确认(self):
        observation = task.Observation(
            b"png",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "app",
        )
        with patch("元宝每日任务.time.monotonic", return_value=4):
            result = task.ToolExecutor._welfare_webview_observation(observation, 0)
        self.assertIsNotNone(result)
        self.assertEqual(result.stage, "welfare")

    def test_福利WebView只剩窗口装饰节点按空层处理(self):
        """Waydroid 多窗口装饰层会让 WebView 层级只剩标题栏，不能误判为 app。"""
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.device = SimpleNamespace(
            activity=lambda: "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            ui_dump=lambda **kwargs: (
                '<hierarchy><node class="android.view.ViewGroup" content-desc="元宝的标题栏。" '
                'enabled="true" bounds="[0,0][900,1600]" />'
                '<node resource-id="android:id/back_window" enabled="true" bounds="[8,8][62,62]" />'
                "</hierarchy>"
            ),
            screenshot=lambda: b"png",
        )
        executor.width = 900
        executor.height = 1600
        executor.config = SimpleNamespace(auto_accept_protocol=False)
        executor._welfare_context = True
        executor._welfare_scroll_normalized = True
        observation = executor.observe()
        self.assertEqual(observation.stage, "welfare")
        self.assertEqual(observation.nodes, ())

    def test_软键盘显示时先收起再点击我们(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 900
        executor.height = 1600
        calls = []
        executor.device = SimpleNamespace(
            input=lambda *args: calls.append(args),
            keyboard_shown=Mock(side_effect=[True, False]),
        )
        ours = task.Node(
            "",
            "我们",
            "",
            "android.widget.ImageView",
            False,
            True,
            task.Bounds(744, 1517, 791, 1564),
        )
        observations = iter(
            [
                task.Observation(
                    b"", "", "", (ours,), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "chat"
                ),
                task.Observation(
                    b"", "", "", (ours,), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "chat"
                ),
                task.Observation(
                    b"", "", "", (ours,), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "ours"
                ),
            ]
        )
        executor.observe = Mock(side_effect=lambda: next(observations))
        with patch("元宝每日任务.time.sleep"):
            result = executor._go_to_ours()
        self.assertEqual(result.stage, "ours")
        self.assertEqual(
            calls,
            [
                ("keyevent", "KEYCODE_BACK"),
                ("tap", "767", "1540"),
            ],
        )

    def test_显式设备发现不读取Waydroid状态(self):
        runtime = task.WaydroidRuntime()
        with (
            patch.object(task.Device, "connect") as connect,
            patch.object(runtime, "_status") as status,
        ):
            device = runtime.discover_device("emulator-5554")
        self.assertEqual(device.serial, "emulator-5554")
        connect.assert_called_once_with()
        status.assert_not_called()

    def test_报告进度和兑换前置断言(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        observation = task.Observation(b"", "", "", (), "", "welfare")
        report = {
            "daily_done": True,
            "question": 3,
            "writing": 3,
            "image": 3,
            "photo_question": 3,
            "same_template": 3,
        }
        self.assertTrue(workflow.dispatch("open_welfare", {}, observation).ok)
        result = workflow.dispatch("report_tasks", report, observation)
        self.assertTrue(result.ok)
        self.assertEqual(workflow.phase, "open_exchange")
        result = workflow.dispatch("open_exchange", {}, observation)
        self.assertTrue(result.ok)
        self.assertEqual(workflow.phase, "redeem")

    def test_报告进度单调合并且问题三次自动确认每日任务(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.配置(), state_path=Path(directory) / "state.json")
            workflow = task.Workflow(config, 假执行器())
            result = workflow._parse_report(
                {
                    "daily_done": False,
                    "question": 3,
                    "writing": 0,
                    "image": 0,
                    "photo_question": 0,
                    "same_template": 0,
                }
            )
            self.assertTrue(result.ok)
            self.assertTrue(workflow.daily_done)
            self.assertEqual(workflow.progress["question"], 3)
            result = workflow._parse_report(
                {
                    "daily_done": False,
                    "question": 1,
                    "writing": 0,
                    "image": 0,
                    "photo_question": 0,
                    "same_template": 0,
                }
            )
            self.assertFalse(result.ok)
            self.assertTrue(workflow.daily_done)
            self.assertEqual(workflow.progress["question"], 3)
            saved = task.read_daily_state(config.state_path)
            self.assertTrue(saved["daily_done"])
            self.assertEqual(saved["task_progress"]["question"], 3)

    def test_同日状态恢复到下一个任务(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.配置(), state_path=Path(directory) / "state.json")
            task.write_daily_state(
                config.state_path,
                {
                    "date": datetime.now(ZoneInfo(config.timezone_name)).date().isoformat(),
                    "daily_done": True,
                    "task_progress": {
                        "question": 3,
                        "writing": 1,
                        "image": 0,
                        "photo_question": 0,
                        "same_template": 0,
                    },
                    "exchange_status": None,
                },
            )
            workflow = task.Workflow(config, 假执行器())
            workflow.restore_state()
            self.assertTrue(workflow.state_restored)
            workflow.dispatch(
                "open_welfare",
                {},
                task.Observation(b"", "", "", (), "", "welfare"),
            )
            self.assertEqual(workflow.target, "writing")
            self.assertEqual(workflow.expected_count, 2)
            self.assertEqual(workflow.phase, "open_target")

    def test_同日已完成状态直接跳过兑换(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.配置(), state_path=Path(directory) / "state.json")
            task.write_daily_state(
                config.state_path,
                {
                    "date": datetime.now(ZoneInfo(config.timezone_name)).date().isoformat(),
                    "card": config.card_name,
                    "daily_done": True,
                    "task_progress": {key: 3 for key in task.TASK_ORDER},
                    "exchange_status": "used",
                },
            )
            executor = 假执行器()
            workflow = task.Workflow(config, executor)
            workflow.restore_state()
            self.assertTrue(workflow.terminal_restore)
            self.assertTrue(executor.reward_use_confirmed)
            workflow.dispatch(
                "open_welfare",
                {},
                task.Observation(b"", "", "", (), "", "welfare"),
            )
            self.assertEqual(workflow.phase, "return_ours")
            self.assertEqual(
                workflow.expected_tool(task.Observation(b"", "", "", (), "", "")),
                "return_to_ours",
            )

    def test_完整无障碍层级直接定位任务入口(self):
        executor = 假执行器()
        executor.width, executor.height = 750, 1333
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "writing"
        observation = task.Observation(
            b"", "", "",
            (
                task.Node(
                    "去写作",
                    "",
                    "",
                    "android.widget.Button",
                    True,
                    True,
                    task.Bounds(560, 900, 720, 980),
                ),
            ),
            "",
            "welfare",
        )
        self.assertEqual(
            workflow.cached_action(observation),
            ("tap", {"x": 640, "y": 940}),
        )

    def test_任务标题整行节点定位到右侧行动列(self):
        executor = 假执行器()
        executor.width, executor.height = 900, 1600
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "writing"
        observation = task.Observation(
            b"",
            "",
            "",
            (
                task.Node(
                    "使用写作能力",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(38, 1174, 860, 1228),
                ),
            ),
            "WebBrowserActivity",
            "welfare",
        )
        self.assertEqual(
            workflow.cached_action(observation),
            ("tap", {"x": 806, "y": 1201}),
        )

    def test_目标行在视口外时有界滚动并在显现后语义定位(self):
        executor = 假执行器()
        executor.width, executor.height = 900, 1600
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "image"
        offscreen = task.Observation(
            b"",
            "",
            "",
            (
                task.Node(
                    "使用p图能力",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(0, 0, 0, 0),
                ),
            ),
            "WebBrowserActivity",
            "welfare",
        )
        expected_scroll = (
            "swipe",
            {
                "x1": 450,
                "y1": 1344,
                "x2": 450,
                "y2": 608,
                "duration_ms": 500,
            },
        )
        for _ in range(task.TASK_ENTRY_SCROLL_MAX_ATTEMPTS):
            self.assertEqual(workflow.cached_action(offscreen), expected_scroll)
        self.assertIsNone(workflow.cached_action(offscreen))

        visible = task.Observation(
            b"",
            "",
            "",
            (
                task.Node(
                    "使用p图能力",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(119, 810, 300, 850),
                ),
                task.Node(
                    "去p图",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(747, 810, 860, 864),
                ),
            ),
            "WebBrowserActivity",
            "welfare",
        )
        self.assertEqual(
            workflow.cached_action(visible),
            ("tap", {"x": 803, "y": 837}),
        )
        self.assertNotIn("image", workflow.entry_scroll_attempts)

    def test_做同款任务行在视口外时滚动并语义定位入口(self):
        executor = 假执行器()
        executor.width, executor.height = 900, 1600
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "same_template"
        offscreen = task.Observation(
            b"",
            "",
            "",
            (
                task.Node(
                    "使用推荐模板做同款",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(0, 0, 0, 0),
                ),
            ),
            "WebBrowserActivity",
            "welfare",
        )
        self.assertEqual(
            workflow.cached_action(offscreen),
            (
                "swipe",
                {
                    "x1": 450,
                    "y1": 1344,
                    "x2": 450,
                    "y2": 608,
                    "duration_ms": 500,
                },
            ),
        )
        visible = task.Observation(
            b"",
            "",
            "",
            (
                task.Node(
                    "使用推荐模板做同款",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(119, 810, 400, 850),
                ),
                task.Node(
                    "做同款",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(747, 810, 860, 864),
                ),
            ),
            "WebBrowserActivity",
            "welfare",
        )
        action = workflow.cached_action(visible)
        self.assertEqual(action, ("tap", {"x": 803, "y": 837}))
        self.assertTrue(workflow.dispatch(*action, visible).ok)
        self.assertEqual(workflow.phase, "perform")

    def test_做同款进入推荐页后本地选择首张可点击模板卡(self):
        workflow = task.Workflow(self.配置(), 假执行器())
        workflow.phase = "perform"
        workflow.target = "same_template"
        workflow.substate = {"entry_clicked": True}
        first_card = task.Node(
            "", "", "", "android.view.View", True, True, task.Bounds(24, 950, 443, 1548)
        )
        second_card = task.Node(
            "", "", "", "android.view.View", True, True, task.Bounds(457, 950, 876, 1548)
        )
        observation = task.Observation(
            b"",
            "",
            "",
            (second_card, first_card),
            "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2",
            "app",
        )

        action = workflow.cached_action(observation)
        self.assertEqual(action, ("tap", {"x": 233, "y": 1249}))
        self.assertTrue(workflow.dispatch(*action, observation).ok)
        self.assertTrue(workflow.substate["template_clicked"])

    def test_有目标任务行时不使用顶部签到的同名入口(self):
        executor = 假执行器()
        executor.width, executor.height = 900, 1600
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "question"
        observation = task.Observation(
            b"",
            "",
            "",
            (
                task.Node(
                    "去提问",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(747, 406, 860, 459),
                ),
                task.Node(
                    "问元宝问题",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(38, 1174, 860, 1228),
                ),
            ),
            "WebBrowserActivity",
            "welfare",
        )
        self.assertEqual(
            workflow.cached_action(observation),
            ("tap", {"x": 806, "y": 1201}),
        )

    def test_模型点到任务行中部时校正到右侧行动列(self):
        executor = 假执行器()
        executor.width, executor.height = 900, 1600
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "writing"
        observation = task.Observation(
            b"",
            "",
            "",
            (
                task.Node(
                    "使用写作能力",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(38, 1174, 860, 1228),
                ),
            ),
            "WebBrowserActivity",
            "welfare",
        )
        result = workflow.dispatch("tap", {"x": 485, "y": 1200}, observation)
        self.assertTrue(result.ok)
        self.assertEqual(executor.calls, [("tap", {"x": 806, "y": 1201})])

    def test_福利层级完整时本地读取进度不请求模型(self):
        nodes = []
        rows = (
            ("问元宝问题", "问元宝任意问题累计3次，已完成3/3"),
            ("使用写作能力", "使用写作能力生成3次结果，已完成1/3"),
            ("使用P图能力", "使用P图能力生成3次结果，已完成0/3"),
            ("使用拍题能力", "使用拍题能力生成3次结果，已完成2/3"),
            ("使用推荐模板做同款", "使用推荐模板做同款3次，已完成1/3"),
        )
        for index, (title, detail) in enumerate(rows):
            bounds = task.Bounds(40, 400 + index * 150, 800, 480 + index * 150)
            nodes.extend(
                [
                    task.Node(title, "", "", "android.widget.TextView", False, True, bounds),
                    task.Node(detail, "", "", "android.widget.TextView", False, True, bounds),
                ]
            )
        observation = task.Observation(
            b"", "", "", tuple(nodes), "WebBrowserActivity", "welfare"
        )
        self.assertEqual(
            task.parse_local_welfare_progress(observation),
            {
                "daily_done": True,
                "question": 3,
                "writing": 1,
                "image": 0,
                "photo_question": 2,
                "same_template": 1,
            },
        )

    def test_福利层级仅暴露部分进度时只返回可见计数(self):
        observation = task.Observation(
            b"",
            "",
            "",
            (
                task.Node(
                    "问元宝问题已完成3/3",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(40, 500, 400, 550),
                ),
                task.Node(
                    "使用写作能力",
                    "",
                    "",
                    "android.widget.TextView",
                    False,
                    True,
                    task.Bounds(40, 700, 800, 750),
                ),
            ),
            "WebBrowserActivity",
            "welfare",
        )
        self.assertEqual(
            task.parse_local_welfare_progress(observation),
            {"daily_done": True, "question": 3},
        )

    def test_离屏零边界计数不会串到其它任务(self):
        nodes = [
            task.Node(
                "问元宝问题",
                "",
                "",
                "android.widget.TextView",
                False,
                True,
                task.Bounds(40, 500, 400, 545),
            ),
            task.Node(
                "问元宝任意问题累计3次，已完成3/3",
                "",
                "",
                "android.widget.TextView",
                False,
                True,
                task.Bounds(40, 550, 800, 580),
            ),
            task.Node(
                "使用写作能力",
                "",
                "",
                "android.widget.TextView",
                False,
                True,
                task.Bounds(40, 700, 400, 745),
            ),
            task.Node(
                "使用写作能力生成3次结果，已完成3/3",
                "",
                "",
                "android.widget.TextView",
                False,
                True,
                task.Bounds(40, 750, 800, 780),
            ),
        ]
        for title, detail in (
            ("使用P图能力", "使用P图能力生成3次结果，已完成3/3"),
            ("使用拍题能力", "使用拍题能力生成3次结果，已完成0/3"),
            ("使用推荐模板做同款", "使用推荐模板做同款累计3次，已完成0/3"),
        ):
            nodes.extend(
                [
                    task.Node(
                        title,
                        "",
                        "",
                        "android.widget.TextView",
                        False,
                        True,
                        task.Bounds(0, 0, 0, 0),
                    ),
                    task.Node(
                        detail,
                        "",
                        "",
                        "android.widget.TextView",
                        False,
                        True,
                        task.Bounds(0, 0, 0, 0),
                    ),
                ]
            )
        observation = task.Observation(
            b"", "", "", tuple(nodes), "WebBrowserActivity", "welfare"
        )
        self.assertEqual(
            task.parse_local_welfare_progress(observation),
            {"daily_done": True, "question": 3, "writing": 3},
        )

    def test_福利页可见进度超过已保存进度时同步并跳过已完成任务(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.配置(), state_path=Path(directory) / "state.json")
            executor = 假执行器()
            executor.width, executor.height = 900, 1600
            workflow = task.Workflow(config, executor)
            workflow.daily_done = True
            workflow.progress.update(
                {
                    "question": 2,
                    "writing": 0,
                    "image": 0,
                    "photo_question": 0,
                    "same_template": 0,
                }
            )
            workflow.phase = "open_target"
            workflow.target = "question"
            workflow.expected_count = 3
            observation = task.Observation(
                b"",
                "",
                "",
                (
                    task.Node(
                        "问元宝问题已完成3/3",
                        "",
                        "",
                        "android.widget.TextView",
                        False,
                        True,
                        task.Bounds(40, 500, 400, 550),
                    ),
                    task.Node(
                        "使用写作能力",
                        "",
                        "",
                        "android.widget.TextView",
                        False,
                        True,
                        task.Bounds(119, 1505, 735, 1544),
                    ),
                    task.Node(
                        "去写作",
                        "",
                        "",
                        "android.widget.TextView",
                        False,
                        True,
                        task.Bounds(747, 1513, 860, 1567),
                    ),
                ),
                "WebBrowserActivity",
                "welfare",
            )

            self.assertTrue(workflow.reconcile_visible_progress(observation))
            self.assertEqual(workflow.progress["question"], 3)
            self.assertEqual(workflow.target, "writing")
            self.assertEqual(workflow.expected_count, 1)
            self.assertEqual(
                workflow.cached_action(
                    task.Observation(b"", "", "", (), "WebBrowserActivity", "welfare")
                ),
                ("tap", {"x": 803, "y": 1540}),
            )
            saved = task.read_daily_state(config.state_path)
            self.assertEqual(saved["task_progress"]["question"], 3)

    def test_报告后福利空树沿用刚确认的任务入口坐标(self):
        executor = 假执行器()
        executor.width, executor.height = 900, 1600
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "report"
        nodes = [
            # 页面顶部签到卡也有一个“去提问”，任务入口必须选择列表中的后一个。
            task.Node(
                "去提问", "", "", "android.widget.TextView", False, True,
                task.Bounds(747, 406, 860, 459),
            ),
            task.Node(
                "问元宝问题", "", "", "android.widget.TextView", False, True,
                task.Bounds(119, 1029, 241, 1068),
            ),
            task.Node(
                "去提问", "", "", "android.widget.TextView", False, True,
                task.Bounds(747, 1037, 860, 1091),
            ),
        ]
        stable = task.Observation(b"stable", "", "", tuple(nodes), "WebBrowserActivity", "welfare")
        result = workflow.dispatch(
            "report_tasks",
            {
                "daily_done": False,
                "question": 0,
                "writing": 0,
                "image": 0,
                "photo_question": 0,
                "same_template": 0,
            },
            stable,
        )
        self.assertTrue(result.ok)
        self.assertEqual(workflow.target, "daily_question")
        self.assertEqual(
            workflow.cached_action(task.Observation(b"same", "", "", (), "WebBrowserActivity", "welfare")),
            ("tap", {"x": 803, "y": 1064}),
        )
        self.assertNotIn("daily_question", workflow.primed_entry_points)

    def test_每日问元宝完成后恢复到同一任务行(self):
        executor = 假执行器()
        executor.width, executor.height = 900, 1600
        workflow = task.Workflow(self.配置(), executor)
        workflow.daily_done = False
        workflow.target = "daily_question"
        workflow.expected_count = None
        workflow.phase = "return"
        workflow.entry_points["daily_question"] = (803 / 900, 1064 / 1600)
        result = workflow.dispatch(
            "return_to_welfare", {}, task.Observation(b"", "", "", (), "", "app")
        )
        self.assertTrue(result.ok)
        self.assertEqual(workflow.target, "question")
        self.assertEqual(
            workflow.cached_action(task.Observation(b"", "", "", (), "", "welfare")),
            ("tap", {"x": 803, "y": 1064}),
        )

    def test_任务返回福利中心后本地递增而不再报告(self):
        workflow = task.Workflow(self.配置(), 假执行器())
        workflow.daily_done = True
        workflow.progress["question"] = 3
        workflow.progress["writing"] = 0
        workflow.progress["image"] = 0
        workflow.progress["photo_question"] = 0
        workflow.progress["same_template"] = 0
        workflow.target = "writing"
        workflow.expected_count = 1
        workflow.phase = "return"
        result = workflow.dispatch(
            "return_to_welfare",
            {},
            task.Observation(b"", "", "", (), "", "app"),
        )
        self.assertTrue(result.ok)
        self.assertEqual(workflow.progress["writing"], 1)
        self.assertEqual(workflow.phase, "open_target")
        self.assertEqual(workflow.target, "writing")
        self.assertEqual(workflow.expected_count, 2)

    def test_做同款必须先点福利入口再点推荐模板(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "same_template"
        welfare_button = task.Node(
            "做同款", "", "", "android.widget.Button", True, True, task.Bounds(620, 700, 718, 750)
        )
        recommendation_button = task.Node(
            "+ 做同款", "", "", "android.widget.Button", True, True, task.Bounds(268, 1110, 350, 1160)
        )
        welfare = task.Observation(b"", "", "", (welfare_button,), "", "welfare")
        recommendation = task.Observation(
            b"", "", "", (recommendation_button,), "", "app"
        )
        result = workflow.dispatch("tap", {"x": 670, "y": 725}, welfare)
        self.assertTrue(result.ok)
        self.assertEqual(workflow.substate, {"entry_clicked": True})
        result = workflow.dispatch("send_message", {}, recommendation)
        self.assertFalse(result.ok)
        result = workflow.dispatch("tap", {"x": 310, "y": 1135}, recommendation)
        self.assertTrue(result.ok)
        self.assertEqual(workflow.substate["template_clicked"], True)
        result = workflow.dispatch("send_message", {}, recommendation)
        self.assertTrue(result.ok)
        result = workflow.dispatch("wait_5s", {}, recommendation)
        self.assertTrue(result.ok)
        self.assertEqual(workflow.phase, "claim")

    def test_重复任务入口在同一运行中复用且保留语义校验(self):
        executor = 假执行器()
        executor.width, executor.height = 750, 1333
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "question"
        workflow.expected_count = 1
        entry = task.Node(
            "去提问", "", "", "android.widget.Button", True, True, task.Bounds(560, 700, 720, 780)
        )
        observation = task.Observation(b"", "", "", (entry,), "", "welfare")

        self.assertTrue(workflow.dispatch("tap", {"x": 640, "y": 740}, observation).ok)
        workflow.phase = "open_target"
        cached = workflow.cached_action(observation)
        self.assertEqual(cached, ("tap", {"x": 640, "y": 740}))

        unrelated = task.Node(
            "去写作", "", "", "android.widget.Button", True, True, task.Bounds(560, 700, 720, 780)
        )
        self.assertIsNone(
            workflow.cached_action(task.Observation(b"", "", "", (unrelated,), "", "welfare"))
        )

    def test_福利入口优先选择右侧行动按钮而不是任务标题(self):
        executor = 假执行器()
        executor.width, executor.height = 900, 1600
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "question"
        title = task.Node(
            "问元宝问题", "", "", "android.widget.TextView", False, True,
            task.Bounds(119, 1029, 241, 1068),
        )
        action = task.Node(
            "去提问", "", "", "android.widget.TextView", False, True,
            task.Bounds(747, 1037, 860, 1091),
        )
        observation = task.Observation(b"", "", "", (title, action), "WebBrowserActivity", "welfare")
        self.assertEqual(workflow.cached_action(observation), ("tap", {"x": 803, "y": 1064}))

    def test_做同款模板缓存页面变化时失效(self):
        executor = 假执行器()
        executor.width, executor.height = 750, 1333
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "perform"
        workflow.target = "same_template"
        workflow.substate = {"entry_clicked": True}
        card = task.Node(
            "做同款", "", "", "android.widget.Button", True, True, task.Bounds(24, 950, 443, 1250)
        )
        observation = task.Observation(b"", "", "", (card,), "", "app")
        self.assertTrue(workflow.dispatch("tap", {"x": 200, "y": 1100}, observation).ok)
        workflow.phase = "perform"
        workflow.substate = {"entry_clicked": True}
        self.assertEqual(
            workflow.cached_action(observation), ("tap", {"x": 200, "y": 1100})
        )
        workflow.substate = {"entry_clicked": True}
        welfare = task.Observation(b"", "", "", (), "", "welfare")
        self.assertIsNone(workflow.cached_action(welfare))
        detail = task.Observation(
            b"", "", "", (),
            "com.tencent.hunyuan.app.chat/com.tencent.hunyuan.app.AITemplateDetailActivity",
            "app",
        )
        self.assertIsNone(workflow.cached_action(detail))

    def test_做同款模板误点图片时回退到可见按钮文字(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "perform"
        workflow.target = "same_template"
        workflow.substate = {"entry_clicked": True}
        recommendation_button = task.Node(
            "做同款", "", "", "android.widget.TextView", False, True, task.Bounds(509, 1282, 554, 1303)
        )
        recommendation = task.Observation(b"", "", "", (recommendation_button,), "", "app")
        result = workflow.dispatch("tap", {"x": 100, "y": 500}, recommendation)
        self.assertTrue(result.ok)
        self.assertEqual(executor.calls, [("tap", {"x": 531, "y": 1292})])
        self.assertTrue(workflow.substate["template_clicked"])

    def test_做同款模板首次无文字时允许推荐卡片点击(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "perform"
        workflow.target = "same_template"
        workflow.substate = {"entry_clicked": True}
        card = task.Node(
            "", "", "", "android.view.View", True, True, task.Bounds(24, 950, 443, 1548)
        )
        recommendation = task.Observation(b"", "", "", (card,), "", "app")
        result = workflow.dispatch("tap", {"x": 200, "y": 1200}, recommendation)
        self.assertTrue(result.ok)
        self.assertEqual(executor.calls, [("tap", {"x": 200, "y": 1200})])
        self.assertTrue(workflow.substate["template_clicked"])

    def test_做同款模板坐标无效时回退首张大卡片(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "perform"
        workflow.target = "same_template"
        workflow.substate = {"entry_clicked": True}
        first_card = task.Node(
            "", "", "", "android.view.View", True, True, task.Bounds(24, 950, 443, 1548)
        )
        second_card = task.Node(
            "", "", "", "android.view.View", True, True, task.Bounds(457, 950, 876, 1548)
        )
        recommendation = task.Observation(
            b"", "", "", (second_card, first_card), "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2", "app"
        )
        result = workflow.dispatch("tap", {"x": 10, "y": 200}, recommendation)
        self.assertTrue(result.ok)
        self.assertEqual(executor.calls, [("tap", {"x": 233, "y": 1249})])
        self.assertTrue(workflow.substate["template_clicked"])

    def test_做同款创建首页层级为空时按屏幕比例回退(self):
        workflow = task.Workflow(self.配置(), 假执行器())
        workflow.width = 900
        workflow.height = 1600
        observation = task.Observation(
            b"",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/.home.v2.YBHomeActivityV2",
            "welfare",
        )
        self.assertEqual(workflow._template_card_fallback(observation), (234, 1248))

    def test_福利WebView空层级允许视觉点击做同款入口(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "same_template"
        observation = task.Observation(
            b"png",
            "",
            "",
            (),
            "com.tencent.yuanbao.WebBrowserActivity",
            "welfare",
        )
        result = workflow.dispatch("tap", {"x": 620, "y": 980}, observation)
        self.assertTrue(result.ok)
        self.assertEqual(workflow.phase, "perform")
        self.assertTrue(workflow.substate["entry_clicked"])

    def test_任务入口点击后仍在福利中心不会推进本地子步骤(self):
        class 停留执行器(假执行器):
            def execute(self, name, args):
                self.calls.append((name, args))
                return task.ToolResult(True, "点击已发送", {"stage": "welfare"})

        executor = 停留执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "question"
        observation = task.Observation(b"png", "", "", (), "WebBrowserActivity", "welfare")
        result = workflow.dispatch("tap", {"x": 805, "y": 1065}, observation)
        self.assertFalse(result.ok)
        self.assertEqual(workflow.phase, "open_target")
        self.assertEqual(workflow.substate, {})

    def test_空树福利页不再使用固定行坐标盲点(self):
        # 福利页改版后任务行整体下移：旧 (806,1064) 已变成“已完成”非入口行。
        # 空树福利页必须回退到视觉模型按截图定位，不得盲点。
        executor = 假执行器()
        executor.width, executor.height = 900, 1600
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "daily_question"
        observation = task.Observation(
            b"png", "", "", (),
            "com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "welfare",
        )
        self.assertIsNone(workflow.cached_action(observation))

    def test_失败报告包含时间状态和堆栈且隐藏密钥(self):
        config = self.配置()
        config = replace(config, api_key="secret-test-key")
        error = task.RetryLimitExceeded(
            "连续 3 次工具/状态失败",
            phase="open_target",
            target="question",
            step=9,
            failures=3,
            recent_action={"tool": "tap", "arguments": {"x": 803, "y": 1064}},
            observation=task.Observation(b"", "", "当前页面", (), "WebBrowserActivity", "welfare"),
        )
        report = task.build_failure_report(config, error)
        self.assertIn("报错时间", report)
        self.assertIn("错误类型：RetryLimitExceeded", report)
        self.assertIn("失败阶段：open_target", report)
        self.assertIn("当前任务：question", report)
        self.assertIn("完整 Python 堆栈", report)
        self.assertNotIn("secret-test-key", report)

    def test_同款入口先滚动再使用本机固定坐标(self):
        executor = 假执行器()
        executor.width, executor.height = 900, 1600
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "same_template"
        observation = task.Observation(
            b"png",
            "",
            "",
            (),
            "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "welfare",
        )
        self.assertEqual(
            workflow.cached_action(observation),
            (
                "swipe",
                {
                    "x1": 450,
                    "y1": 1344,
                    "x2": 450,
                    "y2": 608,
                    "duration_ms": 500,
                },
            ),
        )
        self.assertEqual(
            workflow.cached_action(observation),
            ("tap", {"x": 806, "y": 1491}),
        )

    def test_奖励遮罩无障碍为空时用绿色像素探针收下(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width, executor.height = 900, 1600
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        observation = task.Observation(b"png", "", "", (), "WebBrowserActivity", "welfare")
        with patch("元宝每日任务.png_pixel", return_value=(30, 220, 126, 255)):
            with patch("元宝每日任务.time.sleep"):
                self.assertTrue(executor._dismiss_reward_popup(observation))
        self.assertEqual(calls, [("tap", "450", "1035")])

    def test_生成页奖励遮罩无障碍为空时也用绿色像素探针收下(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width, executor.height = 900, 1600
        calls = []
        executor.device = SimpleNamespace(input=lambda *args: calls.append(args))
        observation = task.Observation(
            b"png", "", "", (),
            "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "app",
        )
        with patch("元宝每日任务.png_pixel", return_value=(30, 220, 126, 255)):
            with patch("元宝每日任务.time.sleep"):
                self.assertTrue(executor._dismiss_reward_popup(observation))
        self.assertEqual(calls, [("tap", "450", "1035")])

    def test_积分不足时返回我们页仍可完成(self):
        executor = 假执行器()
        executor.reward_use_confirmed = False
        executor.reward_unavailable = True
        workflow = task.Workflow(self.配置(), executor)
        workflow.daily_done = True
        workflow.progress = {key: 3 for key in task.TASK_ORDER}
        workflow.phase = "complete"
        result = workflow.dispatch("complete_task", {}, task.Observation(b"", "", "", (), "", "ours"))
        self.assertTrue(result.ok)
        self.assertTrue(workflow.finished)

    def test_兑换积分和三天卡价格读取(self):
        config = self.配置()
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.config = config
        product = task.Node(
            "QQ超级会员3天卡", "", "", "android.widget.TextView", False, True, task.Bounds(380, 300, 720, 700)
        )
        points = task.Node("15000", "", "", "android.widget.TextView", False, True, task.Bounds(610, 70, 700, 110))
        cost = task.Node("30000积分", "", "", "android.widget.TextView", False, True, task.Bounds(500, 720, 620, 760))
        observation = task.Observation(b"", "", "", (points, product, cost), "", "exchange")
        self.assertEqual(executor._read_exchange_points(observation), 15000)
        self.assertEqual(executor._read_product_cost(observation, product), 30000)

    def test_卡住恢复跳过阶段白名单(self):
        # open_exchange 阶段本不允许 tap；恢复期间 VLM 自由决策必须能执行，
        # 否则恢复在强阶段下注定失败（2026-09-16 补跑步骤 83 的熔断）。
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_exchange"
        workflow.target = None
        observation = task.Observation(b"", "", "", (), "", "welfare")
        result = workflow.dispatch("tap", {"x": 50, "y": 30}, observation)
        self.assertFalse(result.ok)
        self.assertIn("不应调用", result.message)
        workflow.note_recovery_attempt()
        result = workflow.dispatch("tap", {"x": 50, "y": 30}, observation)
        self.assertTrue(result.ok)
        self.assertFalse(workflow.recovering)
        self.assertEqual(executor.calls, [("tap", {"x": 50, "y": 30})])

    def test_兑换商城入口按文本节点点击(self):
        # 福利页顶部“兑换商城”是不可点击 TextView；必须能定位，不能只靠固定坐标。
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.config = self.配置()
        executor.width = 900
        executor.height = 1600
        taps = []
        executor.device = SimpleNamespace(input=lambda *args: taps.append(args))
        mall = task.Node(
            "兑换商城", "", "", "android.widget.TextView", False, True, task.Bounds(65, 249, 204, 310)
        )
        welfare = task.Observation(
            b"", "", "",
            (mall,),
            "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "welfare",
        )
        exchange = task.Observation(
            b"", "", "",
            (task.Node("QQ超级会员3天卡", "", "", "android.widget.TextView", False, True, task.Bounds(32, 1215, 737, 1250)),),
            "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "exchange",
        )
        executor._go_to_welfare = Mock(return_value=welfare)
        executor._dismiss_reward_popup = Mock(return_value=False)
        executor.observe = Mock(side_effect=[welfare, exchange])
        with patch("元宝每日任务.time.sleep"):
            result = executor._open_exchange()
        self.assertTrue(result.ok)
        self.assertEqual(result.data.get("stage"), "exchange")
        # 点击的是“兑换商城”文本中心 (134, 279)，而不是旧固定坐标 (110, 233)。
        self.assertIn(("tap", "134", "279"), taps)

    def test_奖品记录页可直接判定积分不足跳过兑换(self):
        # 2026-09-16 补跑现场：落在 prize_records 但余额 9000、卡价 30000，
        # 应直接判积分不足成功返回，而不是“兑换商城没有加载”连败熔断。
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.config = self.配置()
        executor.width = 900
        executor.height = 1600
        executor.device = SimpleNamespace(input=lambda *args: None)
        balance = task.Node("9000", "", "", "android.widget.TextView", False, True, task.Bounds(65, 141, 232, 226))
        card = task.Node(
            "兑换QQ超级会员3天卡", "", "", "android.widget.TextView", False, True, task.Bounds(32, 1215, 737, 1250)
        )
        cost = task.Node("-30000", "", "", "android.widget.TextView", False, True, task.Bounds(749, 1245, 816, 1272))
        records = task.Observation(
            b"", "", "",
            (balance, card, cost),
            "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "prize_records",
        )
        points, price = executor._read_points_and_cost(records)
        self.assertEqual(points, 9000)
        self.assertEqual(price, 30000)
        # 余额顶部有多余数字时取最小值（余额通常是最靠左的顶部数字）。
        many = task.Observation(
            b"", "", "",
            (
                task.Node("1500", "", "", "android.widget.TextView", False, True, task.Bounds(762, 243, 816, 270)),
                balance,
                card,
                cost,
            ),
            "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "prize_records",
        )
        self.assertEqual(executor._read_points_and_cost(many), (9000, 30000))

    def test_无文字发送箭头优先于上传图标(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 750
        executor.height = 1333
        upload = task.Node(
            "",
            "",
            "ic_upload",
            "android.widget.ImageView",
            True,
            True,
            task.Bounds(550, 1190, 610, 1250),
        )
        arrow = task.Node(
            "",
            "",
            "",
            "android.widget.ImageView",
            True,
            True,
            task.Bounds(647, 1239, 715, 1307),
        )
        observation = task.Observation(b"", "", "", (upload, arrow), "", "app")
        self.assertIs(task.ToolExecutor._find_send(executor, observation), arrow)

    def test_未确认固定输入时禁止发送(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "perform"
        workflow.target = "writing"
        observation = task.Observation(b"", "", "", (), "", "writing")
        result = workflow.dispatch("send_message", {}, observation)
        self.assertFalse(result.ok)
        self.assertEqual(executor.calls, [])

    def test_本地状态明确时强制下一步工具(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        observation = task.Observation(b"", "", "", (), "", "writing")
        workflow.phase = "open_target"
        workflow.target = "same_template"
        self.assertEqual(workflow.expected_tool(observation), "tap")
        workflow.phase = "perform"
        workflow.target = "writing"
        self.assertEqual(workflow.expected_tool(observation), "input_test_prompt")
        workflow.substate["input"] = True
        self.assertEqual(workflow.expected_tool(observation), "send_message")
        workflow.substate["sent"] = True
        self.assertEqual(workflow.expected_tool(observation), "wait_5s")
        workflow.phase = "report"
        self.assertEqual(workflow.expected_tool(observation), "report_tasks")

    def test_福利活动页识别与恢复(self):
        web = "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity"

        def activity_nodes():
            return task.parse_nodes(
                '<hierarchy>'
                '<node text="元宝Ai视觉现场" enabled="true" bounds="[0,0][900,1600]" />'
                '<node content-desc="活动规则" class="android.widget.Button" clickable="true" '
                'enabled="true" bounds="[796,108][874,183]" />'
                '<node content-desc="参与方式" class="android.widget.Image" enabled="true" '
                'bounds="[156,656][744,1166]" />'
                '<node content-desc="活动小窗" enabled="true" bounds="[849,148][851,150]" />'
                '</hierarchy>'
            )

        # 活动页必须单独识别，不能当成福利页。Activity 缺失时退回 unknown，
        # 避免非元宝页面被误标为应用内嵌套页。
        self.assertEqual(task.detect_stage(activity_nodes(), web), "activity")
        welfare_nodes = task.parse_nodes(
            '<hierarchy><node text="每日问元宝得积分" enabled="true" bounds="[0,0][100,100]" /></hierarchy>'
        )
        self.assertEqual(task.detect_stage(welfare_nodes, web), "welfare")
        welfare_action_nodes = task.parse_nodes(
            '<hierarchy><node text="去提问" enabled="true" bounds="[0,0][100,100]" /></hierarchy>'
        )
        self.assertEqual(task.detect_stage(welfare_action_nodes, web), "welfare")
        self.assertEqual(task.detect_stage(activity_nodes(), ""), "unknown")
        # “活动小窗”是常驻悬浮图标：福利中心本身也带它，不能单独作为活动页证据。
        floating_icon = task.parse_nodes(
            '<hierarchy>'
            '<node text="每日问元宝得积分" enabled="true" bounds="[0,0][100,100]" />'
            '<node content-desc="活动小窗" enabled="true" bounds="[849,148][851,150]" />'
            '</hierarchy>'
        )
        self.assertEqual(task.detect_stage(floating_icon, web), "welfare")

        # 含活动语义的节点不能作为文字任务入口。
        workflow = task.Workflow(self.配置(), 假执行器())
        workflow.phase = "open_target"
        workflow.target = "daily_question"
        observation = task.Observation(b"", "", "", activity_nodes(), web, "activity")
        self.assertIsNone(workflow._semantic_entry_action(observation))

        # 宽泛旧文案已移除：纯“问元宝”标题不再被当作提问入口。
        self.assertNotIn("问元宝", task.TASK_ENTRY_MARKERS["daily_question"])
        self.assertNotIn("问元宝问题", task.TASK_ENTRY_MARKERS["daily_question"])

    def test_活动页输入先关回福利中心(self):
        web = "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity"
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 900
        executor.height = 1600
        taps = []
        executor.device = SimpleNamespace(input=lambda *args: taps.append(args))
        nodes = task.parse_nodes(
            '<hierarchy>'
            '<node text="元宝Ai视觉现场" enabled="true" bounds="[0,0][900,1600]" />'
            '<node content-desc="返回" class="android.widget.Button" clickable="true" '
            'enabled="true" bounds="[27,5][102,81]" />'
            '<node content-desc="活动规则" class="android.widget.Button" clickable="true" '
            'enabled="true" bounds="[796,108][874,183]" />'
            '</hierarchy>'
        )
        welfare = task.Observation(b"", "", "", (), web, "welfare")
        executor.observe = Mock(return_value=welfare)
        executor._welfare_context = False
        with patch("元宝每日任务.time.sleep"):
            result = executor._input_fixed_prompt(
                task.Observation(b"", "", "", nodes, web, "activity")
            )
        self.assertFalse(result.ok)
        self.assertIn("已返回任务列表", result.message)
        self.assertIn(("tap", "64", "43"), taps)

    def test_活动页返回键关闭时保留福利上下文(self):
        web = "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity"
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.width = 900
        executor.height = 1600
        inputs = []
        executor.device = SimpleNamespace(input=lambda *args: inputs.append(args))
        nodes = task.parse_nodes(
            '<hierarchy><node text="元宝Ai视觉现场" enabled="true" bounds="[0,0][900,1600]" /></hierarchy>'
        )
        welfare = task.Observation(b"", "", "", (), web, "welfare")
        executor.observe = Mock(return_value=welfare)
        executor._welfare_context = False
        with patch("元宝每日任务.time.sleep"):
            after = executor._close_activity_page(
                task.Observation(b"", "", "", nodes, web, "activity")
            )
        self.assertEqual(after.stage, "welfare")
        self.assertTrue(executor._welfare_context)
        self.assertIn(("keyevent", "KEYCODE_BACK"), inputs)

    def test_误触活动页的入口自动作废并回福利中心(self):
        web = "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity"
        executor = 假执行器()
        executor._close_activity_page = Mock()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "daily_question"
        workflow.entry_points["daily_question"] = (0.5, 0.5)
        nodes = task.parse_nodes(
            '<hierarchy><node text="元宝Ai视觉现场" enabled="true" bounds="[0,0][900,1600]" /></hierarchy>'
        )
        tapped = task.Observation(b"", "", "", nodes, web, "activity")
        welfare = task.Observation(b"", "", "", (), web, "welfare")
        executor._close_activity_page.return_value = welfare
        result = workflow.dispatch("tap", {"x": 450, "y": 800}, tapped)
        self.assertFalse(result.ok)
        self.assertIn("已返回任务列表", result.message)
        self.assertNotIn("daily_question", workflow.entry_points)
        self.assertIn("daily_question", workflow.fixed_entry_disabled)
        executor._close_activity_page.assert_called_once()
        self.assertEqual(workflow.phase, "open_target")
        self.assertEqual(executor.calls, [])

    def test_卡住恢复只在同一卡点触发一次(self):
        workflow = task.Workflow(self.配置(), 假执行器())
        workflow.phase = "open_target"
        workflow.target = "question"
        # 初始可进入恢复。
        self.assertTrue(workflow.should_enter_recovery())
        workflow.note_recovery_attempt()
        self.assertTrue(workflow.recovering)
        self.assertEqual(workflow.recover_attempts, 1)
        # 同一卡点恢复中不再重复进入。
        self.assertFalse(workflow.should_enter_recovery())
        # 恢复动作执行后回到正常流程，但同一卡点不再恢复。
        workflow.finish_recovery("tap 执行成功")
        self.assertFalse(workflow.recovering)
        self.assertFalse(workflow.should_enter_recovery())
        # 切到新阶段/目标后允许再次恢复。
        workflow.phase = "perform"
        self.assertTrue(workflow.should_enter_recovery())
        self.assertEqual(workflow.recover_attempts, 0)

    def test_卡住恢复期间不强制工具并附加恢复提示(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "question"
        observation = task.Observation(b"", "", "", (), "", "welfare")
        self.assertEqual(workflow.expected_tool(observation), "tap")
        workflow.note_recovery_attempt()
        # 恢复期间 VLM 自由决策：不强制 tap，context 带恢复提示。
        self.assertIsNone(workflow.expected_tool(observation))
        self.assertIn("智能恢复", workflow.recovery_hint())
        self.assertIn("open_target", workflow.recovery_hint())

    def test_卡住恢复动作只执行不推进业务状态(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "question"
        observation = task.Observation(b"", "", "", (), "", "welfare")
        workflow.note_recovery_attempt()
        result = workflow.dispatch("swipe", {"x1": 450, "x2": 450, "y1": 1200, "y2": 600, "duration_ms": 500}, observation)
        self.assertTrue(result.ok)
        # 恢复动作成功后退出恢复态，但阶段/目标/子状态保持不动，等下一步观察效果。
        self.assertFalse(workflow.recovering)
        self.assertEqual(workflow.phase, "open_target")
        self.assertEqual(workflow.target, "question")
        self.assertEqual(workflow.substate, {})
        self.assertEqual(
            executor.calls,
            [("swipe", {"x1": 450, "x2": 450, "y1": 1200, "y2": 600, "duration_ms": 500})],
        )

    def test_任务推进后清理恢复状态(self):
        workflow = task.Workflow(self.配置(), 假执行器())
        workflow.phase = "open_target"
        workflow.target = "question"
        workflow.note_recovery_attempt()
        workflow._select_next()
        self.assertFalse(workflow.recovering)
        self.assertEqual(workflow.recover_attempts, 0)
        self.assertIsNone(workflow.recover_phase)

    def test_卡住恢复时作废坏入口缓存(self):
        workflow = task.Workflow(self.配置(), 假执行器())
        workflow.phase = "open_target"
        workflow.target = "writing"
        workflow.entry_points["writing"] = (803 / 900, 1201 / 1600)
        # 恢复前语义入口因已有缓存直接返回 None（复用坏坐标）。
        nodes = task.parse_nodes(
            '<hierarchy><node text="去写作" enabled="true" bounds="[747,1174][860,1228]" /></hierarchy>'
        )
        welfare = task.Observation(
            b"", "", "",
            nodes,
            "com.tencent.hunyuan.app.chat/com.tencent.yuanbao.mp.components.websdk.ext.ui.WebBrowserActivity",
            "welfare",
        )
        self.assertIsNone(workflow._semantic_entry_action(welfare))
        # 进入恢复即作废坏缓存，语义入口重新按最新层级定位。
        workflow.note_recovery_attempt()
        self.assertNotIn("writing", workflow.entry_points)
        action = workflow._semantic_entry_action(welfare)
        self.assertIsNotNone(action)
        self.assertEqual(action[0], "tap")

    def test_本地幂等动作允许同画面有界重试(self):
        signature = task.action_signature("input_test_prompt", {})
        self.assertFalse(
            task.repeated_action_is_stuck(
                "input_test_prompt", signature, "same", signature, "same"
            )
        )
        tap_signature = task.action_signature("tap", {"x": 1, "y": 2})
        self.assertTrue(
            task.repeated_action_is_stuck(
                "tap", tap_signature, "same", tap_signature, "same"
            )
        )

    def test_文字任务轮次自动使用唯一测试短语标记(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "perform"
        workflow.target = "question"
        workflow.expected_count = 2
        observation = task.Observation(b"", "", "", (), "", "chat")
        result = workflow.dispatch("input_test_prompt", {}, observation)
        self.assertTrue(result.ok)
        self.assertEqual(executor.prompt_variants, ["question-2"])

    def test_图片选择器中禁止模型随意点击(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "perform"
        workflow.target = "photo_question"
        observation = task.Observation(b"", "", "", (), "", "picker")
        result = workflow.dispatch("tap", {"x": 100, "y": 200}, observation)
        self.assertFalse(result.ok)
        self.assertEqual(executor.calls, [])

    def test_未选图时禁止提交图片任务(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "perform"
        workflow.target = "image"
        observation = task.Observation(b"", "", "", (), "", "image")
        result = workflow.dispatch("send_message", {}, observation)
        self.assertFalse(result.ok)
        self.assertEqual(executor.calls, [])

    def test_完整节点中的兑换成功证据不受压缩截断影响(self):
        nodes = (
            task.Node("", "恭喜兑换成功", "", "android.view.View", False, True, task.Bounds(0, 0, 1, 1)),
        )
        observation = task.Observation(b"", "", "", nodes, "", "exchange")
        self.assertTrue(task.ToolExecutor._has_exchange_success(observation))

    def test_拍题确认图片后允许自动提交版本等待(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "perform"
        workflow.target = "photo_question"
        workflow.substate = {"image": True, "confirmed": True}
        observation = task.Observation(b"", "", "", (), "", "photo_question")
        result = workflow.dispatch("wait_5s", {}, observation)
        self.assertTrue(result.ok)
        self.assertEqual(executor.calls, [("wait_5s", {})])

    def test_奖品记录顶部筛选标签不是使用成功证据(self):
        config = self.配置()
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.config = config
        card = task.Node("QQ超级会员3天卡", "", "", "android.widget.TextView", False, True, task.Bounds(139, 116, 750, 161))
        tab = task.Node("已使用", "", "", "android.widget.TextView", True, True, task.Bounds(390, 85, 489, 133))
        pending = task.Node("去使用", "", "", "android.widget.Button", True, True, task.Bounds(642, 205, 722, 250))
        observation = task.Observation(b"", "", "", (tab, card, pending), "", "prize_records")
        self.assertFalse(executor._has_reward_use_success(observation))
        used = task.Node("已使用", "", "", "android.widget.TextView", False, True, task.Bounds(139, 172, 210, 194))
        observation = task.Observation(b"", "", "", (tab, card, used), "", "prize_records")
        self.assertTrue(executor._has_reward_use_success(observation))

    def test_兑换成功弹窗按无障碍描述识别去使用(self):
        executor = task.ToolExecutor.__new__(task.ToolExecutor)
        executor.reward_card_name = "QQ超级会员3天卡"
        card = task.Node(
            "QQ超级会员3天卡", "", "", "android.widget.TextView", False, True, task.Bounds(330, 650, 650, 880)
        )
        use_button = task.Node(
            "", "去使用", "", "android.view.View", False, True, task.Bounds(251, 941, 615, 1035)
        )
        observation = task.Observation(b"", "", "", (card, use_button), "", "exchange")
        self.assertIs(executor._find_reward_use_button(observation), use_button)

    def test_福利中心硬性拦截永久忽略任务(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "open_target"
        workflow.target = "writing"
        ignored = task.Node("去邀请", "", "", "android.widget.TextView", False, True, task.Bounds(622, 698, 717, 742))
        observation = task.Observation(b"", "", "", (ignored,), "", "welfare")
        result = workflow.dispatch("tap", {"x": 670, "y": 720}, observation)
        self.assertFalse(result.ok)
        self.assertEqual(executor.calls, [])

    def test_模型降级_json解析(self):
        self.assertEqual(
            task.VisionModel._parse_json_tool('{"action":"wait"}'),
            ("wait_5s", {}),
        )
        self.assertEqual(
            task.VisionModel._parse_json_tool('```json\n{"tool":"open_welfare","arguments":{}}\n```'),
            ("open_welfare", {}),
        )

    def test_视觉模型设置有限请求超时(self):
        fake_client = SimpleNamespace()
        with patch("元宝每日任务.OpenAI", return_value=fake_client) as openai:
            task.VisionModel(self.配置())
        self.assertEqual(openai.call_args.kwargs["timeout"], 180)

    def test_视觉模型收到403时不重复重试(self):
        class 拒绝访问(Exception):
            status_code = 403

        config = replace(self.配置(), max_retries=3, retry_cooldown_seconds=0)
        with patch("元宝每日任务.OpenAI", return_value=SimpleNamespace()):
            model = task.VisionModel(config)
        calls = []

        def rejected_request():
            calls.append(1)
            raise 拒绝访问("Region not supported")

        with self.assertRaises(task.ModelAccessError):
            model._with_retries(rejected_request, "视觉模型调用")
        self.assertEqual(len(calls), 1)

    def test_视觉请求保留同轮历史但只发送最新截图(self):
        class 请求记录器:
            def __init__(self):
                self.calls = []

            def __call__(self, **kwargs):
                self.calls.append(kwargs)
                call_id = f"call-{len(self.calls)}"
                call = SimpleNamespace(
                    id=call_id,
                    function=SimpleNamespace(name="wait_5s", arguments="{}"),
                )
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(tool_calls=[call], content=None)
                        )
                    ]
                )

        recorder = 请求记录器()
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=recorder))
        )
        with patch("元宝每日任务.OpenAI", return_value=fake_client):
            model = task.VisionModel(self.配置())
        observations = [
            task.Observation(
                f"png-{index}".encode(),
                f"xml-{index}",
                f"ui-{index}",
                (),
                f"Activity/{index}",
                "welfare",
            )
            for index in range(3)
        ]
        for index, observation in enumerate(observations):
            name, args, call_id = model.next_tool(observation, f"phase={index}")
            self.assertEqual((name, args), ("wait_5s", {}))
            model.record_tool_result(call_id, task.ToolResult(index != 1, f"result-{index}"))

        first, second, third = [item["messages"] for item in recorder.calls]
        self.assertEqual([item["role"] for item in first], ["system", "user"])
        self.assertEqual(
            [item["role"] for item in second],
            ["system", "user", "assistant", "user", "user"],
        )
        self.assertEqual(
            [item["role"] for item in third],
            ["system", "user", "assistant", "user", "user", "assistant", "user", "user"],
        )
        self.assertEqual(first[0]["content"], second[0]["content"])
        self.assertEqual(recorder.calls[0]["tools"], recorder.calls[1]["tools"])
        self.assertEqual(recorder.calls[1]["tools"], recorder.calls[2]["tools"])
        self.assertEqual(recorder.calls[0]["tool_choice"], "auto")
        self.assertEqual(recorder.calls[1]["tool_choice"], "auto")
        self.assertEqual(
            recorder.calls[0]["extra_body"], {"reasoning_effort": "high"}
        )
        self.assertEqual(recorder.calls[1]["extra_body"], {"reasoning_effort": "high"})
        self.assertIn("协议兼容说明", first[0]["content"])
        self.assertNotIn("phase=0", first[0]["content"])
        self.assertNotIn("ui-0", json.dumps(second[1:-1], ensure_ascii=False))
        self.assertFalse(
            any(
                isinstance(message.get("content"), list)
                and any(block.get("type") == "image_url" for block in message["content"])
                for message in second[1:-1]
            )
        )
        self.assertTrue(
            any(block.get("type") == "image_url" for block in second[-1]["content"])
        )
        self.assertNotIn("上一动作执行结果", second[-1]["content"][0]["text"])
        self.assertEqual(
            json.dumps(second[:4], ensure_ascii=False, sort_keys=True),
            json.dumps(third[:4], ensure_ascii=False, sort_keys=True),
        )

    def test_阶段错误后可强制唯一工具(self):
        class 请求记录器:
            def __init__(self):
                self.calls = []

            def __call__(self, **kwargs):
                self.calls.append(kwargs)
                call = SimpleNamespace(
                    id="forced-call",
                    function=SimpleNamespace(
                        name="report_tasks",
                        arguments=json.dumps(
                            {
                                "daily_done": True,
                                "question": 3,
                                "writing": 3,
                                "image": 3,
                                "photo_question": 3,
                                "same_template": 3,
                            }
                        ),
                    ),
                )
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(tool_calls=[call], content=None)
                        )
                    ]
                )

        recorder = 请求记录器()
        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=recorder))
        )
        with patch("元宝每日任务.OpenAI", return_value=fake_client):
            model = task.VisionModel(self.配置())
        observation = task.Observation(b"png", "", "", (), "Activity", "welfare")
        expected = {
            "daily_done": True,
            "question": 3,
            "writing": 3,
            "image": 3,
            "photo_question": 3,
            "same_template": 3,
        }
        self.assertEqual(
            model.next_tool(observation, "阶段=report", forced_tool="report_tasks")[:2],
            ("report_tasks", expected),
        )
        self.assertEqual(
            recorder.calls[0]["tool_choice"],
            {"type": "function", "function": {"name": "report_tasks"}},
        )

    def test_通用原型请求保留压缩历史(self):
        class 请求记录器:
            def __init__(self):
                self.calls = []

            def __call__(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content='{"action":"wait","seconds":1}'
                            )
                        )
                    ]
                )

        recorder = 请求记录器()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=recorder))
        )
        history = []
        first = prototype.ask_model(
            client, "test-model", b"png-1", "ui-1", "测试目标", (750, 1333), history
        )
        prototype.append_history(history, 1, "ui-1", first)
        second = prototype.ask_model(
            client, "test-model", b"png-2", "ui-2", "测试目标", (750, 1333), history
        )
        self.assertEqual(first, second)
        self.assertEqual(
            [item["role"] for item in recorder.calls[1]["messages"]],
            ["system", "user", "assistant", "user", "user"],
        )
        self.assertEqual(
            recorder.calls[0]["messages"][0]["content"],
            recorder.calls[1]["messages"][0]["content"],
        )
        self.assertNotIn("ui-1", json.dumps(recorder.calls[1]["messages"][1:-1]))
        self.assertFalse(
            any(
                isinstance(message.get("content"), list)
                and any(block.get("type") == "image_url" for block in message["content"])
                for message in recorder.calls[1]["messages"][1:-1]
            )
        )


if __name__ == "__main__":
    unittest.main()
