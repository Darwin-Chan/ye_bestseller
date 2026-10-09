"""Ticket 02 SKU grouping integration coverage at the public AnalysisService seam."""
import tempfile
import unittest
from pathlib import Path

from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.db import Database, connect
from helpers import new_round


class SkuGroupingServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inventory.db"
        self.conn = connect(self.path)
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        self.service = AnalysisService(
            AnalysisConfig(self.path, member_kind="sku"), running=lambda: False
        )

    def observe(self, day, rows, *, offer="11", shop="A01", title="杯子"):
        self.db.submit_inventory_snapshot(
            round_id=new_round(self.db, shop, run_date=day),
            shop_key=shop,
            shop_url="https://shop.test/" + shop,
            shop_name="店铺" + shop,
            offer_id=offer,
            product_url="https://offer.test/" + offer,
            list_title=title,
            detail_title=title,
            main_image_url="",
            sku_rows=rows,
            collected_at=day + "T01:00:00+00:00",
            attempt=1,
        )

    @staticmethod
    def member(shop, offer, sku):
        return {"shop_key": shop, "offer_id": offer, "sku_id": sku}

    @classmethod
    def group_for(cls, snapshot, member):
        key = (member["shop_key"], member["offer_id"], member["sku_id"])
        return next(
            group
            for group in snapshot["groups"]
            if any(
                (item["shop_key"], item["offer_id"], item["sku_id"]) == key
                for item in group["members"]
            )
        )

    def test_public_mutations_are_sku_scoped_and_refresh_group_tallies(self):
        rows = [
            {"sku_id": "red", "sku_name": "红", "sku_stock": 100},
            {"sku_id": "blue", "sku_name": "蓝", "sku_stock": 80},
        ]
        self.observe("2026-09-07", rows)
        self.observe("2026-09-14", [{"sku_id": "red", "sku_name": "红", "sku_stock": 90}, {"sku_id": "blue", "sku_name": "蓝", "sku_stock": 60}])
        self.observe(
            "2026-09-07",
            [{"sku_id": "red", "sku_name": "红", "sku_stock": 70}],
            offer="11",
            shop="B01",
        )
        self.observe(
            "2026-09-14",
            [{"sku_id": "red", "sku_name": "红", "sku_stock": 60}],
            offer="11",
            shop="B01",
        )

        snapshot = self.service.start("2026-09-07", "2026-09-14")
        red = self.member("A01", "11", "red")
        blue = self.member("A01", "11", "blue")
        other_shop_red = self.member("B01", "11", "red")
        self.assertEqual(3, len(snapshot["members"]))
        self.assertNotEqual(self.group_for(snapshot, red)["id"], self.group_for(snapshot, other_shop_red)["id"])

        red_group = self.group_for(snapshot, red)
        blue_group = self.group_for(snapshot, blue)
        moved = self.service.edit_group(snapshot["id"], "move", blue_group["id"], blue, red_group["id"])
        merged = self.group_for(moved, red)
        self.assertEqual(2, merged["sku_count"])
        self.assertEqual(1, merged["shop_count"])
        self.assertEqual(1, merged["product_count"])
        self.assertEqual(30, merged["sales"])
        self.assertEqual(1, sum(item["sku_id"] == "blue" for item in merged["members"]))
        self.assertEqual(1, sum(item["sku_id"] == "blue" for group in moved["groups"] for item in group["members"]))
        self.assertEqual(1, self.group_for(moved, other_shop_red)["sku_count"])

        confirmed = self.service.confirm_groups(snapshot["id"], [merged["id"]])
        self.assertTrue(self.group_for(confirmed, red)["confirmed"])
        self.assertTrue(self.service.save_draft(snapshot["id"])["saved_at"])
        restored = AnalysisService(self.service.config, running=lambda: False).get(snapshot["id"])
        self.assertTrue(self.group_for(restored, red)["confirmed"])
        self.assertEqual(2, self.group_for(restored, red)["sku_count"])

    def test_exclusion_replays_across_evidence_change_and_rejoin_is_explicit(self):
        initial = [
            {"sku_id": "red", "sku_name": "红", "sku_stock": 100},
            {"sku_id": "blue", "sku_name": "蓝", "sku_stock": 80},
        ]
        self.observe("2026-09-07", initial)
        self.observe("2026-09-14", initial)
        snapshot = self.service.start("2026-09-07", "2026-09-14")
        red = self.member("A01", "11", "red")
        blue = self.member("A01", "11", "blue")
        red_group = self.group_for(snapshot, red)
        blue_group = self.group_for(snapshot, blue)
        merged = self.service.edit_group(snapshot["id"], "move", blue_group["id"], blue, red_group["id"])
        merged_group = self.group_for(merged, red)
        excluded = self.service.edit_group(snapshot["id"], "exclude", merged_group["id"], blue)
        excluded_group = self.group_for(excluded, blue)
        self.assertTrue(excluded_group["excluded_group"])
        self.assertEqual(1, self.group_for(excluded, red)["sku_count"])
        self.service.save_draft(snapshot["id"])

        # The stable SKU identity is unchanged while names, images, and sales evidence change.
        self.observe(
            "2026-09-21",
            [
                {"sku_id": "red", "sku_name": "红色新名", "sku_stock": 90},
                {"sku_id": "blue", "sku_name": "蓝色新名", "sku_stock": 70},
            ],
        )
        replayed = self.service.start("2026-09-07", "2026-09-21")
        replayed_blue = self.group_for(replayed, blue)
        self.assertTrue(replayed_blue["excluded_group"])
        self.assertEqual("蓝色新名", next(item["raw_sku_name"] for item in replayed["members"] if item["sku_id"] == "blue"))

        rejoined = self.service.edit_group(replayed["id"], "rejoin", replayed_blue["id"], blue, self.group_for(replayed, red)["id"])
        self.assertFalse(self.group_for(rejoined, blue)["excluded_group"])
        self.assertEqual(2, self.group_for(rejoined, red)["sku_count"])
        self.assertFalse(rejoined.get("excluded"))

    def test_remove_clears_stale_exclusion_edges_for_the_removed_sku(self):
        rows = [
            {"sku_id": "red", "sku_name": "红", "sku_stock": 100},
            {"sku_id": "blue", "sku_name": "蓝", "sku_stock": 80},
        ]
        self.observe("2026-09-07", rows)
        self.observe("2026-09-14", rows)
        snapshot = self.service.start("2026-09-07", "2026-09-14")
        red = self.member("A01", "11", "red")
        blue = self.member("A01", "11", "blue")
        red_group = self.group_for(snapshot, red)
        blue_group = self.group_for(snapshot, blue)
        merged = self.service.edit_group(snapshot["id"], "move", blue_group["id"], blue, red_group["id"])
        excluded = self.service.edit_group(snapshot["id"], "exclude", self.group_for(merged, red)["id"], blue)
        self.assertTrue(excluded["excluded"])

        removed = self.service.edit_group(snapshot["id"], "remove", self.group_for(excluded, blue)["id"], blue)

        self.assertFalse(removed["excluded"])
        self.assertFalse(self.group_for(removed, blue)["excluded_group"])

    def test_new_member_returns_saved_group_to_pending_confirmation(self):
        initial = [
            {"sku_id": "red", "sku_name": "红", "sku_stock": 100},
            {"sku_id": "blue", "sku_name": "蓝", "sku_stock": 80},
        ]
        self.observe("2026-09-07", initial)
        self.observe("2026-09-14", initial)
        snapshot = self.service.start("2026-09-07", "2026-09-14")
        red = self.member("A01", "11", "red")
        blue = self.member("A01", "11", "blue")
        red_group = self.group_for(snapshot, red)
        blue_group = self.group_for(snapshot, blue)
        merged = self.service.edit_group(snapshot["id"], "move", blue_group["id"], blue, red_group["id"])
        self.service.confirm_groups(snapshot["id"], [self.group_for(merged, red)["id"]])
        self.service.save_draft(snapshot["id"])

        self.observe(
            "2026-09-21",
            [
                {"sku_id": "red", "sku_name": "红", "sku_stock": 90},
                {"sku_id": "blue", "sku_name": "蓝", "sku_stock": 70},
                {"sku_id": "green", "sku_name": "绿", "sku_stock": 60},
            ],
        )
        replayed = self.service.start("2026-09-07", "2026-09-21")
        replayed_red = self.group_for(replayed, red)
        self.assertEqual(2, replayed_red["sku_count"])
        self.assertTrue(replayed_red["confirmed"])
        green_group = self.group_for(replayed, self.member("A01", "11", "green"))
        self.assertEqual(1, green_group["sku_count"])
        self.assertFalse(green_group["confirmed"])

if __name__ == "__main__":
    unittest.main()
