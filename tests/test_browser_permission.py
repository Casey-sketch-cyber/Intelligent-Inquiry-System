"""Dedicated browser permissions must never change office browser defaults."""
import unittest
from unittest.mock import MagicMock, patch
from inquiry import browser, isolation


class PermissionTests(unittest.TestCase):
    def test_only_paired_worker_sets_exact_site_permission(self):
        for enabled, mode, expected in ((False, "local", False), (True, "vm", False), (True, "worker", True)):
            with self.subTest(mode=mode):
                pw = MagicMock()
                session = pw.chromium.connect_over_cdp.return_value.new_browser_cdp_session.return_value
                with patch.object(browser, "cdp_alive", return_value=True), patch.object(browser, "close_stale_tabs"), \
                     patch.object(isolation, "ENABLED", enabled), patch.object(isolation, "MODE", mode):
                    browser.connect(pw)
                context = pw.chromium.connect_over_cdp.return_value.contexts[0]
                if expected:
                    context.grant_permissions.assert_called_once_with(["local-network-access"], origin="https://www.ic.net.cn")
                else:
                    context.grant_permissions.assert_not_called()
                session.detach.assert_called_once()
