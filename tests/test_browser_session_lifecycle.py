import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bestseller_monitor import browser_proc, browser_pw


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.alive = True

    def poll(self):
        return None if self.alive else 0


class BrowserSessionEstablishmentTests(unittest.TestCase):
    def cfg(self, **overrides):
        values = {
            "attach_port": 9222,
            "timeout_ms": 45000,
            "user_data_path": Path("ignored-profile"),
            "chrome_path": os.sys.executable,
            "start_browser": True,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def stack(self, *, browser_pid=20303):
        page = MagicMock()
        ctx = MagicMock()
        ctx.new_page.return_value = page
        br = MagicMock()
        br.contexts = [ctx]
        br.new_browser_cdp_session.return_value.send.return_value = {
            "processInfo": [{"type": "browser", "id": browser_pid}],
        }
        pw = MagicMock()
        pw.chromium.connect_over_cdp.return_value = br
        factory = MagicMock()
        factory.start.return_value = pw
        return factory, pw, br, ctx, page

    def test_playwright_start_failure_cleans_our_process_and_preserves_the_error(self):
        proc = FakeProcess(20301)
        failure = RuntimeError("playwright start failed")
        factory = MagicMock()
        factory.start.side_effect = failure
        states = []

        def publish(*state):
            states.append(state)
            return True

        def terminate(pid):
            self.assertEqual(pid, proc.pid)
            proc.alive = False
            return True

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                patch.object(browser_proc, "process_creation_proof", return_value="proof"), \
                patch.object(browser_proc, "terminate_process_tree", side_effect=terminate) as kill:
            with self.assertRaises(RuntimeError) as raised:
                browser_pw.open_session(self.cfg(), publish_browser=publish)

        self.assertIs(raised.exception, failure)
        kill.assert_called_once_with(proc.pid)
        self.assertEqual(
            states,
            [("STARTING", 9222, None, None), ("UNKNOWN", 9222, None, None)],
        )

    def test_cdp_timeout_stops_playwright_and_our_process(self):
        proc = FakeProcess(20302)
        pw = MagicMock()
        pw.chromium.connect_over_cdp.side_effect = RuntimeError("connection refused")
        factory = MagicMock()
        factory.start.return_value = pw
        states = []

        def terminate(pid):
            proc.alive = False
            return True

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw, "_WAIT_LAUNCH_SEC", 0.01), \
                patch.object(browser_pw.time, "sleep"), \
                patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                patch.object(browser_proc, "process_creation_proof", return_value="proof"), \
                patch.object(browser_proc, "terminate_process_tree", side_effect=terminate):
            with self.assertRaisesRegex(RuntimeError, "无法连接浏览器调试端口 9222"):
                browser_pw.open_session(
                    self.cfg(),
                    publish_browser=lambda *state: states.append(state) or True,
                )

        pw.stop.assert_called_once_with()
        self.assertFalse(proc.alive)
        self.assertEqual(
            states,
            [("STARTING", 9222, None, None), ("UNKNOWN", 9222, None, None)],
        )

    def test_missing_default_context_cleans_only_acquired_resources(self):
        proc = FakeProcess(20303)
        factory, pw, br, _, _ = self.stack(browser_pid=proc.pid)
        br.contexts = []

        def terminate(pid):
            proc.alive = False
            return True

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                patch.object(browser_proc, "process_creation_proof", return_value="proof"), \
                patch.object(browser_proc, "terminate_process_tree", side_effect=terminate):
            with self.assertRaisesRegex(RuntimeError, "没有可复用的默认 context"):
                browser_pw.open_session(self.cfg())

        br.close.assert_called_once_with()
        pw.stop.assert_called_once_with()
        self.assertFalse(proc.alive)

    def test_configuration_failure_cleans_in_reverse_order_and_preserves_error(self):
        proc = FakeProcess(20304)
        factory, pw, br, _, page = self.stack(browser_pid=proc.pid)
        failure = RuntimeError("timeout configuration failed")
        page.set_default_timeout.side_effect = failure
        events = []
        page.close.side_effect = lambda: events.append("page")
        br.close.side_effect = lambda: events.append("cdp")
        pw.stop.side_effect = lambda: events.append("playwright")

        def terminate(pid):
            events.append("process")
            proc.alive = False
            return True

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                patch.object(browser_proc, "process_creation_proof", return_value="proof"), \
                patch.object(browser_proc, "terminate_process_tree", side_effect=terminate):
            with self.assertRaises(RuntimeError) as raised:
                browser_pw.open_session(self.cfg())

        self.assertIs(raised.exception, failure)
        self.assertEqual(events, ["page", "cdp", "playwright", "process"])

    def test_final_publisher_exception_is_original_and_is_not_reentered(self):
        proc = FakeProcess(20305)
        factory, _, _, _, _ = self.stack(browser_pid=proc.pid)
        failure = RuntimeError("database unavailable")
        states = []

        def publish(*state):
            states.append(state)
            if state[0] == "OWNED":
                raise failure
            return True

        def terminate(pid):
            proc.alive = False
            return True

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                patch.object(browser_proc, "process_creation_proof", return_value="proof"), \
                patch.object(browser_proc, "terminate_process_tree", side_effect=terminate):
            with self.assertRaises(RuntimeError) as raised:
                browser_pw.open_session(self.cfg(), publish_browser=publish)

        self.assertIs(raised.exception, failure)
        self.assertEqual(
            states,
            [("STARTING", 9222, None, None), ("OWNED", 9222, proc.pid, "proof")],
        )

    def test_rejected_starting_publication_aborts_before_launch(self):
        states = []

        with patch.object(browser_pw.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "状态发布被当前目标拒绝"):
                browser_pw.open_session(
                    self.cfg(),
                    publish_browser=lambda *state: states.append(state) or False,
                )

        popen.assert_not_called()
        self.assertEqual(states, [("STARTING", 9222, None, None)])

    def test_cas_rejection_during_final_publication_cleans_without_unknown_republish(self):
        proc = FakeProcess(20310)
        factory, _, _, _, _ = self.stack(browser_pid=proc.pid)
        states = []

        def publish(*state):
            states.append(state)
            return state[0] != "OWNED"

        def terminate(pid):
            proc.alive = False
            return True

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                patch.object(browser_proc, "process_creation_proof", return_value="proof"), \
                patch.object(browser_proc, "terminate_process_tree", side_effect=terminate):
            with self.assertRaisesRegex(RuntimeError, "状态发布被当前目标拒绝"):
                browser_pw.open_session(self.cfg(), publish_browser=publish)

        self.assertEqual(states, [
            ("STARTING", 9222, None, None),
            ("OWNED", 9222, proc.pid, "proof"),
        ])

    def test_start_mode_requires_an_explicit_existing_browser_path(self):
        states = []
        missing = Path("definitely-not-a-browser.exe")

        with patch.object(browser_pw.subprocess, "Popen") as popen:
            with self.assertRaises(FileNotFoundError):
                browser_pw.open_session(
                    self.cfg(chrome_path=missing),
                    publish_browser=lambda *state: states.append(state) or True,
                )

        popen.assert_not_called()
        self.assertEqual(
            states,
            [("STARTING", 9222, None, None), ("UNKNOWN", 9222, None, None)],
        )

    def test_attach_mode_ignores_browser_path_and_never_terminates_external_browser(self):
        factory, pw, br, ctx, page = self.stack(browser_pid=99001)
        states = []

        def publish(*state):
            states.append(state)
            return True

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw.subprocess, "Popen") as popen, \
                patch.object(browser_proc, "terminate_process_tree") as terminate:
            session = browser_pw.open_session(
                self.cfg(start_browser=False, chrome_path=Path("missing.exe")),
                publish_browser=publish,
            )
            self.assertEqual(session, (pw, br, page, ctx))
            browser_pw.close_session(pw, br, publish_browser=publish)

        popen.assert_not_called()
        terminate.assert_not_called()
        self.assertEqual(
            states,
            [("BORROWED", 9222, None, None), ("CLOSED", None, None, None)],
        )

    def test_matching_live_popen_and_cdp_identity_publishes_owned(self):
        proc = FakeProcess(20306)
        factory, pw, br, _, _ = self.stack(browser_pid=proc.pid)
        states = []

        def publish(*state):
            states.append(state)
            return True

        def terminate(pid):
            proc.alive = False
            return True

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                patch.object(browser_proc, "process_creation_proof", return_value="proof"), \
                patch.object(browser_proc, "terminate_process_tree", side_effect=terminate):
            browser_pw.open_session(self.cfg(), publish_browser=publish)
            browser_pw.close_session(pw, br, publish_browser=publish)

        self.assertEqual(
            states,
            [
                ("STARTING", 9222, None, None),
                ("OWNED", 9222, proc.pid, "proof"),
                ("CLOSED", None, None, None),
            ],
        )

    def test_missing_or_changed_creation_proof_never_publishes_owned(self):
        for proofs in ((None, None), ("launch-proof", "different-proof")):
            with self.subTest(proofs=proofs):
                proc = FakeProcess(20307)
                factory, pw, br, _, _ = self.stack(browser_pid=proc.pid)
                states = []

                def terminate(pid):
                    proc.alive = False
                    return True

                with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                        patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                        patch.object(browser_proc, "process_creation_proof",
                                     side_effect=proofs), \
                        patch.object(browser_proc, "terminate_process_tree",
                                     side_effect=terminate):
                    publish = lambda *state: states.append(state) or True
                    browser_pw.open_session(self.cfg(), publish_browser=publish)
                    browser_pw.close_session(pw, br, publish_browser=publish)

                self.assertEqual(states[1], ("UNKNOWN", 9222, None, None))

    def test_profile_handoff_is_borrowed_and_never_terminated(self):
        proc = FakeProcess(20308)
        proc.alive = False
        factory, pw, br, _, _ = self.stack(browser_pid=6104)
        states = []
        publish = lambda *state: states.append(state) or True

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                patch.object(browser_proc, "process_creation_proof", return_value="proof"), \
                patch.object(browser_proc, "terminate_process_tree") as terminate:
            browser_pw.open_session(self.cfg(), publish_browser=publish)
            browser_pw.close_session(pw, br, publish_browser=publish)

        terminate.assert_not_called()
        self.assertEqual(
            states,
            [
                ("STARTING", 9222, None, None),
                ("BORROWED", 9222, None, None),
                ("CLOSED", None, None, None),
            ],
        )

    def test_profile_handoff_keeps_transport_when_page_close_needs_retry(self):
        proc = FakeProcess(20311)
        proc.alive = False
        factory, pw, br, _, page = self.stack(browser_pid=6105)
        page.close.side_effect = [RuntimeError("page busy"), None]
        publish = lambda *state: True

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                patch.object(browser_proc, "process_creation_proof", return_value="proof"):
            browser_pw.open_session(self.cfg(), publish_browser=publish)
            browser_pw.close_session(pw, br, publish_browser=publish)

        br.close.assert_not_called()
        pw.stop.assert_not_called()
        browser_pw.close_session(pw, br, publish_browser=publish)

    def test_active_session_rejects_a_second_open_without_touching_it(self):
        factory, pw, br, _, page = self.stack()
        with patch("playwright.sync_api.sync_playwright", return_value=factory):
            browser_pw.open_session(self.cfg(start_browser=False))
            with self.assertRaisesRegex(RuntimeError, "已有活动"):
                browser_pw.open_session(self.cfg(start_browser=False))
            page.close.assert_not_called()
            browser_pw.close_session(pw, br)

    def test_wrong_handles_or_publisher_cannot_close_the_active_session(self):
        factory, pw, br, _, page = self.stack()
        publish = lambda *state: True
        other_publish = lambda *state: True
        with patch("playwright.sync_api.sync_playwright", return_value=factory):
            browser_pw.open_session(
                self.cfg(start_browser=False), publish_browser=publish)
            with self.assertRaisesRegex(RuntimeError, "不匹配"):
                browser_pw.close_session(MagicMock(), br, publish_browser=publish)
            with self.assertRaisesRegex(RuntimeError, "不匹配"):
                browser_pw.close_session(pw, br, publish_browser=other_publish)
            page.close.assert_not_called()
            browser_pw.close_session(pw, br, publish_browser=publish)

    def test_repeated_close_is_a_noop_but_stale_handles_cannot_close_a_successor(self):
        factory_a, pw_a, br_a, _, page_a = self.stack(browser_pid=301)
        factory_b, pw_b, br_b, _, page_b = self.stack(browser_pid=302)
        with patch("playwright.sync_api.sync_playwright",
                   side_effect=[factory_a, factory_b]):
            browser_pw.open_session(self.cfg(start_browser=False))
            browser_pw.close_session(pw_a, br_a)
            browser_pw.close_session(pw_a, br_a)
            self.assertEqual(page_a.close.call_count, 1)

            browser_pw.open_session(self.cfg(start_browser=False))
            with self.assertRaisesRegex(RuntimeError, "不匹配"):
                browser_pw.close_session(pw_a, br_a)
            page_b.close.assert_not_called()
            browser_pw.close_session(pw_b, br_b)

    def test_failed_borrowed_page_close_retains_transport_for_explicit_retry(self):
        factory, pw, br, _, page = self.stack()
        page.close.side_effect = [RuntimeError("page busy"), None]
        states = []
        publish = lambda *state: states.append(state) or True
        with patch("playwright.sync_api.sync_playwright", return_value=factory):
            browser_pw.open_session(
                self.cfg(start_browser=False), publish_browser=publish)
            browser_pw.close_session(pw, br, publish_browser=publish)

            br.close.assert_not_called()
            pw.stop.assert_not_called()
            self.assertEqual(states, [("BORROWED", 9222, None, None)])

            browser_pw.close_session(pw, br, publish_browser=publish)

        br.close.assert_called_once_with()
        pw.stop.assert_called_once_with()
        self.assertEqual(states[-1], ("CLOSED", None, None, None))

    def test_next_open_retries_pending_close_before_establishing_successor(self):
        factory_a, pw_a, br_a, _, page_a = self.stack(browser_pid=401)
        factory_b, pw_b, br_b, _, _ = self.stack(browser_pid=402)
        page_a.close.side_effect = [RuntimeError("page busy"), None]
        states_a = []
        publish_a = lambda *state: states_a.append(state) or True

        with patch("playwright.sync_api.sync_playwright",
                   side_effect=[factory_a, factory_b]):
            browser_pw.open_session(
                self.cfg(start_browser=False), publish_browser=publish_a)
            browser_pw.close_session(pw_a, br_a, publish_browser=publish_a)

            browser_pw.open_session(self.cfg(start_browser=False))
            self.assertEqual(states_a[-1], ("CLOSED", None, None, None))
            browser_pw.close_session(pw_b, br_b)

    def test_failed_owned_process_termination_is_retried_with_the_same_popen(self):
        proc = FakeProcess(20309)
        factory, pw, br, _, _ = self.stack(browser_pid=proc.pid)
        outcomes = iter((False, True))

        def terminate(pid):
            succeeded = next(outcomes)
            if succeeded:
                proc.alive = False
            return succeeded

        with patch("playwright.sync_api.sync_playwright", return_value=factory), \
                patch.object(browser_pw.subprocess, "Popen", return_value=proc), \
                patch.object(browser_proc, "process_creation_proof", return_value="proof"), \
                patch.object(browser_proc, "terminate_process_tree",
                             side_effect=terminate) as kill:
            browser_pw.open_session(self.cfg())
            browser_pw.close_session(pw, br)
            self.assertTrue(proc.alive)
            browser_pw.close_session(pw, br)

        self.assertEqual(kill.call_args_list, [unittest.mock.call(proc.pid)] * 2)

    def test_closed_publication_failure_does_not_retain_a_clean_session(self):
        factory, pw, br, _, _ = self.stack()
        states = []

        def publish(*state):
            states.append(state)
            if state[0] == "CLOSED":
                raise RuntimeError("database unavailable")
            return True

        with patch("playwright.sync_api.sync_playwright", return_value=factory):
            browser_pw.open_session(
                self.cfg(start_browser=False), publish_browser=publish)
            browser_pw.close_session(pw, br, publish_browser=publish)
            pw2, br2, _, _ = browser_pw.open_session(self.cfg(start_browser=False))
            browser_pw.close_session(pw2, br2)

        self.assertEqual(states, [
            ("BORROWED", 9222, None, None),
            ("CLOSED", None, None, None),
        ])


if __name__ == "__main__":
    unittest.main()
