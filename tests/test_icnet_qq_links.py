"""离线验证供应商 QQ 直开链接的范围和失败时的页面清理。"""
import unittest
from unittest.mock import Mock, call, patch

from inquiry import icnet


class SavedQQLinkTests(unittest.TestCase):
    QQ = "12345"
    LINK = "https://wpa.qq.com/msgrd?v=3&uin=12345&site=qq&menu=yes"

    def test_normalizes_only_known_wpa_links(self):
        for link in (self.LINK, self.LINK.replace("https://", "http://"), self.LINK.removeprefix("https:")):
            with self.subTest(link=link):
                self.assertEqual(icnet.direct_qq_url({"uin": self.QQ, "inthref": link}), self.LINK)
        secondary = "https://wpa1.qq.com/msgrd?uin=12345"
        self.assertEqual(icnet.direct_qq_url({"uin": self.QQ, "href": secondary}), secondary)
        self.assertIsNone(icnet.direct_qq_url({"uin": self.QQ}))

    def test_refuses_links_that_cannot_be_safely_rebuilt(self):
        invalid = (
            "https://wpa.qq.com.example.org/msgrd?uin=12345",
            "https://other@wpa.qq.com/msgrd?uin=12345",
            "https://wpa.qq.com:443/msgrd?uin=12345",
            "https://wpa.qq.com./msgrd?uin=12345",
            "https://wpa.qq.com/msgrd/?uin=12345",
            "https://wpa.qq.com/Msgrd?uin=12345",
            "https://wpa.qq.com/msgrd?uin=12345#fragment",
            "https://wpa.qq.com/msgrd?uin=12345&uin=12345",
            "https://wpa.qq.com/msgrd?uin=12345&u%69n=99999",
            "https://wpa.qq.com/msgrd?uin=12345&signature=test",
            "https://wpa.qq.com/msgrd?uin=12345&site=https://other.example.org",
            "https://wpa.qq.com/msgrd?uin=12345&menu=no",
            "https://wpa.qq.com/msgrd?uin=12345&v=2",
            "https://wpa.qq.com/msgrd?uin=123456",
            "https://wpa.qq.com/msgrd?uin=１２３４５",
            "https://wpa.qq.com/msgrd?uin=12345&",
            "https://wpa.qq.com/msgrd?uin=12345\n",
            "https://wpa.qq.com\\/msgrd?uin=12345",
            "tencent://message/?uin=12345",
            "javascript:void(0)",
        )
        for link in invalid:
            with self.subTest(link=link):
                self.assertIsNone(icnet.direct_qq_url({"uin": self.QQ, "inthref": link, "href": self.LINK}))

    def browser(self):
        context = Mock(pages=[])
        page = context.new_page.return_value
        page.is_closed.return_value = False
        page.goto.return_value.status = 200
        return context, page, context.new_cdp_session.return_value

    def test_saved_link_opens_one_page_and_limits_document_navigation(self):
        context, page, session = self.browser()
        before = Mock()

        def goto(url, **kwargs):
            before.assert_called_once()
            on_request = session.on.call_args.args[1]
            for request_id, target in (("first", url), ("redirect", "https://admin.qidian.qq.com/static_proxy/start")):
                on_request({"requestId": request_id, "request": {"url": target}})
            return Mock(status=200)

        page.goto.side_effect = goto
        self.assertIs(icnet.launch_saved_qq(icnet.SearchTabs(context), {"uin": self.QQ, "inthref": self.LINK},
                                            before_launch=before), page)
        context.new_page.assert_called_once()
        context.new_cdp_session.assert_called_once_with(page)
        self.assertEqual(session.send.call_args_list, [
            call("Fetch.enable", {"patterns": [{"resourceType": "Document"}]}),
            call("Fetch.continueRequest", {"requestId": "first"}),
            call("Fetch.continueRequest", {"requestId": "redirect"}),
        ])
        page.close.assert_not_called()

    def test_invalid_or_failed_launch_never_leaves_a_tab(self):
        context, page, session = self.browser()
        with self.assertRaises(icnet.QQLaunchFailed):
            icnet.launch_saved_qq(context, {"uin": self.QQ})
        context.new_page.assert_not_called()

        def untrusted_redirect(*args, **kwargs):
            handler = session.on.call_args.args[1]
            handler({"requestId": "bad", "request": {"url": "https://admin.qidian.qq.com.invalid/unsafe"}})
            return Mock(status=200)

        page.goto.side_effect = untrusted_redirect
        with self.assertRaises(icnet.QQLaunchFailed):
            icnet.launch_saved_qq(context, {"uin": self.QQ, "inthref": self.LINK})
        session.send.assert_called_with("Fetch.failRequest", {"requestId": "bad", "errorReason": "BlockedByClient"})
        page.close.assert_called_once()

    def test_visible_row_only_checks_an_existing_matching_model(self):
        row = {"company": "示例供货商", "qqs": [{"uin": self.QQ}]}
        page = Mock(url="https://www.ic.net.cn/search/MODEL.html")
        page.evaluate.return_value = [{**row, "index": 4}]
        context = Mock(pages=[page])
        with patch.object(icnet, "search") as search:
            self.assertIsNone(icnet.find_visible_row(context, "MO", row))
            self.assertEqual(icnet.find_visible_row(context, "MODEL", row), (page, {**row, "index": 4}))
            other_first = {**row, "qqs": [{"uin": "98765"}, *row["qqs"]]}
            self.assertIsNone(icnet.find_visible_row(context, "MODEL", other_first, selected_only=True))
            with self.assertRaises(icnet.SearchCancelled):
                icnet.find_visible_row(context, "MODEL", row, cancelled=lambda: True)
        self.assertEqual(page.evaluate.call_args_list, [call(icnet.EXTRACT_JS)] * 2)
        context.new_page.assert_not_called()
        search.assert_not_called()

    def test_login_and_captcha_hold_saved_launch(self):
        for url in ("https://www.ic.net.cn/searchPnCode.php", "https://member.ic.net.cn/login"):
            with self.subTest(url=url), patch.object(icnet.incidents, "record"):
                context = Mock(pages=[Mock(url=url)])
                with self.assertRaises(icnet.NotLoggedIn):
                    icnet.launch_saved_qq(context, {"uin": self.QQ, "inthref": self.LINK})
                context.new_page.assert_not_called()
