import unittest

from bestseller_monitor.guard import (
    is_deny_url,
    is_login_url,
    is_punish_url,
    vtype,
)


class GuardTests(unittest.TestCase):
    def test_punish_url_excludes_tmd_decoration(self):
        # 站点会在正常详情 URL 后追加 _____tmd_____/punish?x5secdata=... 上报装饰，不算真验证
        self.assertFalse(
            is_punish_url("https://detail.1688.com/offer/1.html/_____tmd_____/punish?x5secdata=xxx")
        )
        self.assertTrue(is_punish_url("https://x/punish?x5secdata=1"))
        self.assertTrue(is_punish_url("https://x/punishTextFetch"))
        self.assertTrue(is_punish_url("https://x/punish/1"))
        self.assertFalse(is_punish_url("https://detail.1688.com/offer/1.html"))

    def test_deny_and_login(self):
        self.assertTrue(is_deny_url("https://x/bsop-punish-test-webapp/deny_pc.html"))
        self.assertTrue(is_deny_url("https://x/deny_pc"))
        self.assertFalse(is_deny_url("https://x/punish"))   # punish 不算 deny
        self.assertTrue(is_login_url("https://login.1688.com/"))
        self.assertTrue(is_login_url("https://login.taobao.com/"))
        self.assertFalse(is_login_url("https://shop.1688.com/"))

    def test_vtype(self):
        self.assertEqual(vtype("登录墙"), "login")
        self.assertEqual(vtype("滑块"), "slider")
        self.assertEqual(vtype(None), "none")


if __name__ == "__main__":
    unittest.main()
