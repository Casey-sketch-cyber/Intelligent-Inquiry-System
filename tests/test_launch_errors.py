import unittest
from unittest.mock import Mock,patch
from inquiry import icnet,qq


class LaunchErrorTests(unittest.TestCase):
    def test_tencent_refusal_is_terminal_and_other_web_pages_are_ignored(self):
        page=Mock(url="https://admin.qidian.qq.com/static_proxy/test")
        page.locator.return_value.inner_text.return_value="抱歉，无法发起临时会话。您可以先添加对方为好友，再发送消息"
        self.assertIn("不支持",icnet.qq_launch_error(page))
        page.url="https://example.com/"
        self.assertIsNone(icnet.qq_launch_error(page))

    def test_refusal_does_not_wait_ninety_seconds_or_read_unrelated_chat(self):
        with patch.object(qq,"current_chat_name") as read:
            with self.assertRaisesRegex(RuntimeError,"不支持"):
                qq.wait_chat_change(object(),"previous",timeout=90,launch_error=lambda:"不支持临时会话")
        read.assert_not_called()

    def test_site_verification_is_detected_before_repeated_result_waits(self):
        context,page=Mock(),Mock(url="https://www.ic.net.cn/searchPnCode.php?l=ins")
        context.pages=[]
        with patch.object(icnet,"search_page",return_value=page):
            with self.assertRaisesRegex(icnet.NotLoggedIn,"校验"):
                icnet.search(context,"LPC2387FBD100K")
        page.wait_for_selector.assert_not_called()

    def test_existing_verification_page_blocks_all_new_search_requests(self):
        context=Mock(pages=[Mock(url="https://www.ic.net.cn/searchPnCode.php?l=ins")])
        with patch.object(icnet,"search_page") as navigate:
            with self.assertRaisesRegex(icnet.NotLoggedIn,"没有重新"):
                icnet.search(context,"LPC1788FBD208K")
        navigate.assert_not_called()
