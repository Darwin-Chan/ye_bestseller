"""Deny detection, interruptible cooldown and both UI count projections."""
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bestseller_monitor import browser_pw, click_listing, guard, pipeline, rounds, stop_request, views, waiting
from bestseller_monitor.config import Shop
from bestseller_monitor.db import Database, connect
from bestseller_monitor.delay import Humanizer
from frozen_clock import FROZEN_NOW, frozen_clock
from helpers import FakeCard, ScriptedListing, crawler_cfg, new_round


class DenyCountTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(frozen_clock())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.conn = connect(self.root / "deny.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.rid = new_round(self.db, "A05")
        self.cfg = crawler_cfg(raw_page_dir=self.root / "raw")
        self.shop = Shop("A05", "Test shop", "https://A05.example/")
        self.human = Humanizer(self.cfg)
        self.emit = self.db.event_logger(self.rid)
        self.started = self.db.record_crawler_process(pid=os.getpid(), round_id=self.rid)
        stop_request.install(lambda: stop_request.consume(self.db))
        self.addCleanup(stop_request.uninstall)

    def counts(self):
        state = views.UiState(round_id=self.rid, manually_paused=True)
        return (views.run_view(self.conn, state=state, now=FROZEN_NOW)["deny"],
                views.result_view(self.conn, state=state, now=FROZEN_NOW)["deny"])

    def ledger(self):
        return dict(self.conn.execute(
            "SELECT event, COUNT(*) FROM event_log WHERE round_id=? "
            "AND event IN ('deny_hit','deny_cooldown','click_deny') GROUP BY event",
            (self.rid,)).fetchall())

    def test_pause_during_first_deny_cooldown_is_visible_in_both_pages(self):
        during = []

        def cooldown(seconds):
            self.assertEqual(seconds, 600)
            during.append(self.counts())
            self.db.request_stop(round_id=self.rid, kind=stop_request.PAUSE,
                                 target_pid=os.getpid(), target_started_at=self.started)
            self.human.sleep(seconds)

        tracker = guard.DenyTracker(600, emit=self.emit, cooldown=cooldown)
        walk = click_listing.ShopWalk(
            ScriptedListing([]), self.shop, self.cfg, self.human,
            db=self.db, round_id=self.rid, emit=self.emit, deny_tracker=tracker)
        card = FakeCard("Denied product", denied=True)
        with self.assertRaises(stop_request.StopRequested):
            walk.capture(card, card.title())
        self.assertEqual(card.opens, 1)
        self.assertEqual(self.ledger().get("deny_hit"), 1)
        self.assertEqual(self.ledger().get("deny_cooldown"), 1)
        self.assertIsNotNone(self.db.stop_request()["ack_at"])
        self.assertEqual((during, self.counts()), ([(1, 1)], (1, 1)),
                         f"deny occurred; ledger={self.ledger()}; "
                         "UI counts=(process,result), during and after pause")

    def walk(self, tracker):
        return click_listing.ShopWalk(
            ScriptedListing([]), self.shop, self.cfg, self.human,
            db=self.db, round_id=self.rid, emit=self.emit, deny_tracker=tracker)

    def pause_once(self, kind=stop_request.PAUSE):
        def cooldown(seconds):
            self.db.request_stop(round_id=self.rid, kind=kind,
                                 target_pid=os.getpid(), target_started_at=self.started)
            self.human.sleep(seconds)
        tracker = guard.DenyTracker(600, emit=self.emit, cooldown=cooldown)
        with self.assertRaises(stop_request.StopRequested) as caught:
            self.walk(tracker).capture(FakeCard(denied=True), "Denied")
        self.assertEqual(caught.exception.kind, kind)
        self.assertEqual(self.ledger(), {"deny_hit": 1, "deny_cooldown": 1})

    def test_pause_without_deny_keeps_both_counts_zero(self):
        self.db.request_stop(round_id=self.rid, kind=stop_request.PAUSE,
                             target_pid=os.getpid(), target_started_at=self.started)
        with self.assertRaises(stop_request.StopRequested):
            self.human.sleep(1)
        self.assertEqual(self.ledger(), {})
        self.assertEqual(self.counts(), (0, 0))

    def test_full_cooldown_and_repeated_submission_count_once(self):
        elapsed = [0.0]
        clock = SimpleNamespace(monotonic=lambda: elapsed[0],
                                sleep=lambda s: elapsed.__setitem__(0, elapsed[0] + s))
        card = FakeCard(denied=True)
        during = []
        def cooldown(seconds):
            during.append(self.counts())
            self.human.sleep(seconds)
            card._denied = False
        tracker = guard.DenyTracker(600, emit=self.emit, cooldown=cooldown)
        with patch.object(waiting, "time", clock):
            self.assertEqual(self.walk(tracker).capture(card, "Denied then ready"), "11")
        self.assertEqual(elapsed[0], 600)
        self.assertEqual(self.ledger(), {"deny_hit": 1, "deny_cooldown": 1, "click_deny": 1})
        for _ in range(2):
            self.walk(tracker).capture(card, "Resubmit")
            self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(during, [(1, 1)])
        # The shop rows use the same projection after the listing is complete.
        self.db.save_shop_offers(self.rid, "A05", self.shop.url, self.shop.name,
                                 [(1, "11", card.url, "Test product", "")], 1)
        state = views.UiState(round_id=self.rid)
        for view in (views.run_view, views.result_view):
            data = view(self.conn, state=state, now=FROZEN_NOW)
            self.assertEqual(data["done"][0]["deny"], 1)

    def test_abort_during_cooldown_and_finished_result_keep_the_hit(self):
        self.pause_once(stop_request.ABORT)
        self.assertEqual(self.counts(), (1, 1))
        rounds.finish(self.db, rounds.load(self.db, self.rid), rounds.TerminalReason.ABANDONED,
                      now=FROZEN_NOW)
        self.assertEqual(views.result_view(self.conn, state=views.UiState(round_id=self.rid),
                                          now=FROZEN_NOW)["deny"], 1)

    def test_unexpected_cooldown_exception_does_not_erase_the_hit(self):
        def cooldown(seconds):
            raise RuntimeError("Synthetic cooldown interruption")
        tracker = guard.DenyTracker(600, emit=self.emit, cooldown=cooldown)
        with self.assertRaisesRegex(RuntimeError, "Synthetic"):
            self.walk(tracker).capture(FakeCard(denied=True), "Denied")
        self.assertEqual(self.ledger(), {"deny_hit": 1, "deny_cooldown": 1})
        self.assertEqual(self.counts(), (1, 1))

    def resume_tracker(self, *, expired=False):
        self.db.clear_stop_request(target_pid=os.getpid(), target_started_at=self.started)
        seed = self.db.deny_seed(self.rid, 600, now=time.time() + (601 if expired else 0))
        self.assertEqual(len(seed.hits), 0 if expired else 1)
        self.assertTrue(seed.cooldown_fired)
        def cooldown(seconds):
            self.fail("Resume must not repeat first-deny cooldown")
        return guard.DenyTracker(600, seed=seed.hits,
                                 cooldown_fired=seed.cooldown_fired,
                                 emit=self.emit, cooldown=cooldown)

    def test_pause_then_successful_resume_keeps_exactly_one_hit(self):
        self.pause_once()
        tracker = self.resume_tracker()
        self.walk(tracker).capture(FakeCard(), "Ready")
        for _ in range(3):
            self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(self.ledger(), {"deny_hit": 1, "deny_cooldown": 1})

    def test_expired_window_on_resume_does_not_erase_round_total(self):
        self.pause_once()
        tracker = self.resume_tracker(expired=True)
        self.walk(tracker).capture(FakeCard(), "Ready")
        self.assertEqual(self.counts(), (1, 1))

    def test_another_deny_on_resume_is_a_new_hit_without_repeating_cooldown(self):
        self.pause_once()
        tracker = self.resume_tracker()
        card = FakeCard(denied=True)
        original_sleep = self.human.sleep
        def backoff(seconds):
            card._denied = False
            original_sleep(seconds)
        with patch.object(self.human, "sleep", side_effect=backoff):
            self.walk(tracker).capture(card, "Denied again")
        self.assertEqual(self.ledger(), {"deny_hit": 2, "deny_cooldown": 1, "click_deny": 1})
        self.assertEqual(self.counts(), (2, 2))

    def test_backfill_deny_uses_the_same_ui_count_without_a_click_event(self):
        url = "https://detail.1688.com/offer/11.html"
        self.db.save_shop_offers(self.rid, "A05", self.shop.url, self.shop.name,
                                 [(1, "11", url, "Test product", "")], 1)
        offer = next(self.db.pending_offers(self.rid, max_attempts=2))
        tracker = guard.DenyTracker(600, emit=self.emit)
        with patch.object(browser_pw, "navigate_detail",
                          side_effect=lambda *a, **kw: FakeCard(denied=True).acquire()):
            pipeline._capture_one_pw(self.db, self.cfg, self.human, self.rid, offer, object(),
                                     emit=self.emit, deny_tracker=tracker)
        self.assertEqual(self.ledger(), {"deny_hit": 1})
        self.assertEqual(self.counts(), (1, 1))

    def test_legacy_rounds_keep_click_counts_and_repeat_reads_do_not_write(self):
        for _ in range(3):
            self.emit("click_deny", shop_key="A05")
        before = self.conn.total_changes
        for _ in range(3):
            self.assertEqual(self.counts(), (3, 3))
        self.assertEqual(self.conn.total_changes, before)

    def test_upgrade_mid_round_counts_legacy_hits_plus_new_ledger_without_pair_duplicates(self):
        self.emit("click_deny", shop_key="A05")
        tracker = guard.DenyTracker(600, emit=self.emit)
        card = FakeCard(denied=True)
        with patch.object(self.human, "sleep", side_effect=lambda s: setattr(card, "_denied", False)):
            self.walk(tracker).capture(card, "New deny")
        # One old click, then one new hit + its click: two incidents, three events.
        self.assertEqual(self.ledger(), {"deny_hit": 1, "click_deny": 2})
        self.assertEqual(self.counts(), (2, 2))
        tracker.record("A05")  # New hit interrupted before its outer click event.
        self.assertEqual(self.counts(), (3, 3))

    def test_new_round_boundary_does_not_hide_legacy_events_in_another_round(self):
        old = new_round(self.db, "A05", run_date="2026-01-01")
        self.db.append_event(old, "click_deny", shop_key="A05")
        guard.DenyTracker(600, emit=self.emit).record("A05")
        self.assertEqual(views.result_view(self.conn, state=views.UiState(round_id=old),
                                          now=FROZEN_NOW)["deny"], 1)
        self.assertEqual(views.result_view(self.conn, state=views.UiState(round_id=self.rid),
                                          now=FROZEN_NOW)["deny"], 1)

    def test_first_ledger_boundary_is_round_wide_and_shop_totals_add_up(self):
        self.db.add_shop(self.rid, "A06", "https://A06.example/", "Shop B")
        self.emit("click_deny", shop_key="A05")
        self.emit("click_deny", shop_key="A06")
        tracker = guard.DenyTracker(600, emit=self.emit)
        tracker.record("A05")
        self.emit("click_deny", shop_key="A05")
        tracker.record("A06")
        # New backfill deny in A06: no paired click.
        self.assertEqual(self.counts(), (4, 4))
        counts = [views._shop_span(self.conn, self.rid, key)[0] for key in ("A05", "A06")]
        self.assertEqual(counts, [2, 2])
