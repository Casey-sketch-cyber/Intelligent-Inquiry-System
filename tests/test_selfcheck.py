"""用替身验证 QQ 发送前只读检查、版本展示与故障记录。"""
import unittest
from unittest.mock import MagicMock, Mock, call, patch

from inquiry import qq, selfcheck
from test_search_pacing import FakeClock


class SelfcheckTests(unittest.TestCase):
    selectors = {
        "conversation_list": "recent-contact-list",
        "send_button": "send-msg",
        "message_list": "chat-msg-area__vlist",
        "editor": "ExEditor-qq-msg-editor",
    }

    def setUp(self):
        self.win = object()
        self.clock = FakeClock()
        self.controls = {name: Mock(IsEnabled=False) for name in self.selectors.values()}
        self.patch_for_test(selfcheck, "_cached", None)
        self.patch_for_test(selfcheck, "time", self.clock)
        self.initializer = self.patch_for_test(qq.auto, "UIAutomationInitializerInThread",
                                               return_value=MagicMock())
        self.passive = self.patch_for_test(qq, "passive_main_window", return_value=self.win)
        self.find = self.patch_for_test(qq, "find", side_effect=lambda _, name, *_args: self.controls.get(name))
        self.versions = self.patch_for_test(selfcheck.incidents, "_versions", return_value={
            "qq_version": "9.9.24.1", "edge_version": "150.1.2.3",
        })
        self.record = self.patch_for_test(selfcheck.incidents, "record")

    def patch_for_test(self, target, name, *args, **kwargs):
        method = patch.object(target, name, *args, **kwargs)
        self.addCleanup(method.stop)
        return method.start()

    def test_reports_versions_with_actual_button_type_even_when_disabled(self):
        state = selfcheck.snapshot()
        self.assertEqual(state, {"ok": True, "missing": [], "qq_version": "9.9.24.1", "edge_version": "150.1.2.3",
                                 "text": "QQ 自检通过", "checked_at": self.clock.now})
        self.find.assert_has_calls([
            call(self.win, "recent-contact-list"),
            call(self.win, "send-msg", qq.auto.ControlType.ButtonControl),
            call(self.win, "chat-msg-area__vlist"),
            call(self.win, "ExEditor-qq-msg-editor"),
        ])
        self.initializer.return_value.__enter__.assert_called_once_with()
        self.initializer.return_value.__exit__.assert_called_once_with(None, None, None)
        self.record.assert_not_called()

    def test_each_missing_control_blocks_fresh_check(self):
        for control, selector in self.selectors.items():
            with self.subTest(control=control):
                found = self.controls.pop(selector)
                with self.assertRaisesRegex(selfcheck.SelfcheckDeferred, selfcheck.LABELS[control]):
                    selfcheck.require_ready(win=self.win)
                self.assertEqual(selfcheck.latest()["missing"], [control])
                self.record.assert_called_with("uia_control_missing", "qq_controls", severity="warning",
                                               extra={"control": control})
                self.controls[selector] = found
        self.passive.assert_not_called()

    def test_unavailable_main_window_is_not_reported_ready(self):
        self.passive.return_value = None
        self.assertEqual(selfcheck.snapshot()["missing"], ["qq_window"])
        self.find.assert_not_called()
        with self.assertRaisesRegex(selfcheck.SelfcheckDeferred, "QQ 主窗口"):
            selfcheck.require_ready()
        self.record.assert_called_once_with("uia_control_missing", "qq_controls", severity="warning",
                                            extra={"control": "qq_window"})

    def test_missing_version_does_not_block_a_complete_probe(self):
        self.versions.side_effect = OSError("未找到文件版本")
        state = selfcheck.require_ready()
        self.assertTrue(state["ok"])
        self.assertIsNone(state["qq_version"])
        self.assertIsNone(state["edge_version"])

    def test_cache_results_are_detached_and_force_ignores_ttl(self):
        self.assertIsNone(selfcheck.latest())
        state = selfcheck.snapshot()
        check_time = state["checked_at"]
        state["missing"].append("editor")
        self.assertEqual(selfcheck.latest()["missing"], [])
        self.clock.now += selfcheck.CACHE_SECONDS - 1
        self.assertEqual(selfcheck.snapshot()["checked_at"], check_time)
        self.passive.assert_called_once_with()
        self.controls.pop(self.selectors["editor"])
        with self.assertRaises(selfcheck.SelfcheckDeferred):
            selfcheck.require_ready()
        self.clock.now += selfcheck.CACHE_SECONDS
        self.assertEqual(selfcheck.snapshot()["checked_at"], self.clock.now)
        self.assertEqual(self.passive.call_count, 3)

    def test_missing_incidents_are_logged_once_until_the_control_recovers(self):
        selector = self.selectors["message_list"]
        control = self.controls.pop(selector)
        selfcheck.snapshot(force=True)
        selfcheck.snapshot(force=True)
        self.record.assert_called_once()
        self.controls[selector] = control
        self.assertTrue(selfcheck.snapshot(force=True)["ok"])
        self.controls.pop(selector)
        selfcheck.snapshot(force=True)
        self.assertEqual(self.record.call_count, 2)

    def test_probe_exceptions_are_not_copied_into_status_or_events(self):
        self.find.side_effect = RuntimeError("不应外传的对话名称")
        state = selfcheck.snapshot()
        self.assertEqual(state["missing"], list(self.selectors))
        self.assertNotIn("不应外传", str(state))
        self.assertNotIn("不应外传", str(self.record.call_args_list))


class PassiveWindowTests(unittest.TestCase):
    def test_passive_probe_skips_minimized_without_restoring(self):
        with patch.object(qq, "_main_window_handles", return_value=[1]), \
             patch.object(qq.win32gui, "IsIconic", return_value=True), \
             patch.object(qq.win32gui, "ShowWindow") as restore, \
             patch.object(qq.auto, "ControlFromHandle") as attach:
            self.assertIsNone(qq.passive_main_window())
        restore.assert_not_called()
        attach.assert_not_called()

    def test_passive_probe_selects_main_window_or_keeps_missing_list_candidate(self):
        first, main = object(), object()
        with patch.object(qq, "_main_window_handles", return_value=[1, 2]), \
             patch.object(qq.win32gui, "IsIconic", return_value=False), \
             patch.object(qq.auto, "ControlFromHandle", side_effect=[first, main]), \
             patch.object(qq, "warm_up", return_value=False), \
             patch.object(qq, "find", side_effect=[None, object()]):
            self.assertIs(qq.passive_main_window(), main)

        with patch.object(qq, "_main_window_handles", return_value=[1]), \
             patch.object(qq.win32gui, "IsIconic", return_value=False), \
             patch.object(qq.auto, "ControlFromHandle", return_value=first), \
             patch.object(qq, "warm_up", return_value=True), \
             patch.object(qq, "find", return_value=None):
            self.assertIs(qq.passive_main_window(), first)
