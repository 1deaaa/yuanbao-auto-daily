import os
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import 元宝每日任务 as task
import 视觉安卓代理 as prototype


class 假设备:
    def display_size(self):
        return 750, 1333


class 假执行器:
    def __init__(self):
        self.reward_use_confirmed = True
        self.calls = []

    def execute(self, name, args):
        self.calls.append((name, args))
        return task.ToolResult(True, "测试成功")

    def _find_send(self, observation):
        return None


class 元宝任务测试(unittest.TestCase):
    @staticmethod
    def 配置():
        """返回不读取开发者 .env 的测试配置，确保干净克隆也能运行测试。"""
        with patch.dict(os.environ, {"API_KEY": "test-only-key"}, clear=True):
            return task.Config.from_env(Path(__file__).with_name(".env.test"))

    def test_页面阶段优先级(self):
        def xml(text):
            return f'<hierarchy><node text="{text}" enabled="true" bounds="[0,0][100,100]" /></hierarchy>'

        self.assertEqual(task.detect_stage(task.parse_nodes(xml("每日问元宝得积分 兑换商城")), ""), "welfare")
        self.assertEqual(task.detect_stage(task.parse_nodes(xml("兑换商城 QQ超级会员1天卡")), ""), "exchange")
        self.assertEqual(task.detect_stage(task.parse_nodes(xml("奖品记录")), ""), "prize_records")
        self.assertEqual(task.detect_stage([], "com.tencent.hunyuan.app.chat/com.tencent.hunyuan.deps.camera.ui.activity.CameraResultActivity"), "photo_preview")
        self.assertEqual(task.detect_stage(task.parse_nodes(xml("相机胶卷")), "com.tencent.hunyuan.app.chat/com.tencent.hunyuan.app.PictureSelectorSupporterActivity"), "picker")
        self.assertEqual(task.detect_stage(task.parse_nodes(xml("本地相册")), "com.tencent.hunyuan.app.chat.RolePlayPickerActivity"), "picker")

    def test_配置和北京时间调度(self):
        config = self.配置()
        self.assertEqual(config.run_time, "05:00")
        self.assertEqual(config.timezone_name, "Asia/Shanghai")
        self.assertTrue(config.stop_waydroid_after_run)
        self.assertTrue(config.test_image_path.is_file())
        before = datetime(2026, 8, 27, 4, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        after = datetime(2026, 8, 27, 5, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(task.next_run_at(config, before).hour, 5)
        self.assertEqual(task.next_run_at(config, after).day, 28)

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
        ):
            device = runtime.discover_device("auto")
        start_session.assert_called_once_with()
        connect.assert_called_once_with()
        self.assertEqual(device.serial, "192.168.240.112:5555")
        self.assertTrue(runtime.managed)

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
        }
        self.assertTrue(workflow.dispatch("open_welfare", {}, observation).ok)
        result = workflow.dispatch("report_tasks", report, observation)
        self.assertTrue(result.ok)
        self.assertEqual(workflow.phase, "open_exchange")
        result = workflow.dispatch("open_exchange", {}, observation)
        self.assertTrue(result.ok)
        self.assertEqual(workflow.phase, "redeem")

    def test_未确认固定输入时禁止发送(self):
        executor = 假执行器()
        workflow = task.Workflow(self.配置(), executor)
        workflow.phase = "perform"
        workflow.target = "writing"
        observation = task.Observation(b"", "", "", (), "", "writing")
        result = workflow.dispatch("send_message", {}, observation)
        self.assertFalse(result.ok)
        self.assertEqual(executor.calls, [])

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
        card = task.Node("QQ超级会员1天卡", "", "", "android.widget.TextView", False, True, task.Bounds(139, 116, 750, 161))
        tab = task.Node("已使用", "", "", "android.widget.TextView", True, True, task.Bounds(390, 85, 489, 133))
        pending = task.Node("去使用", "", "", "android.widget.Button", True, True, task.Bounds(642, 205, 722, 250))
        observation = task.Observation(b"", "", "", (tab, card, pending), "", "prize_records")
        self.assertFalse(executor._has_reward_use_success(observation))
        used = task.Node("已使用", "", "", "android.widget.TextView", False, True, task.Bounds(139, 172, 210, 194))
        observation = task.Observation(b"", "", "", (tab, card, used), "", "prize_records")
        self.assertTrue(executor._has_reward_use_success(observation))

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
