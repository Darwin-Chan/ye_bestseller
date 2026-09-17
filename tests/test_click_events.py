"""点击事件的协议：卡片身份的两种投影、结果种类的分类、旧事件名的解释。

这个模块是「一张卡的一次详情尝试」那条协议的唯一定义处。用例从公开面进入：
生产者拿 `(事件名, kwargs)` 去发，消费者拿 `classify()` 把行读回来。
"""
import unittest

from bestseller_monitor.click_events import (CardRef, ClickOutcome, SkipReason,
                                             classify, denied, no_offer, not_opened,
                                             ok, skipped, unreadable)


class CardRefTests(unittest.TestCase):
    def test_a_card_identity_has_two_projections(self):
        card = CardRef(page=2, index=20)

        self.assertEqual(card.note, "page=2&idx=20", "事件备注里的位置")
        self.assertEqual(card.ref, "card:p2:i20", "详情机会账本上的标识")

    def test_a_card_identity_is_read_back_from_a_note(self):
        self.assertEqual(CardRef.from_note("page=2&idx=20"), CardRef(page=2, index=20))
        self.assertEqual(CardRef.from_note("page=1&idx=0&offer_id=9&sku=3"),
                         CardRef(page=1, index=0))
        self.assertEqual(CardRef.from_note("page=3&idx=7&n=3&skip"),
                         CardRef(page=3, index=7))

    def test_a_note_without_a_position_has_no_card_identity(self):
        for note in (None, "", "name=商品1 @page=1 @idx=3"):
            self.assertIsNone(CardRef.from_note(note), f"note={note!r}")


class OutcomeTests(unittest.TestCase):
    def test_the_outcome_value_is_the_event_name_written_to_the_log(self):
        self.assertEqual(ClickOutcome.SUBMITTED.value, "click_ok")
        self.assertEqual(ClickOutcome.SKIPPED.value, "click_skipped")
        self.assertEqual(ClickOutcome.UNREADABLE.value, "click_parse_error")
        self.assertEqual(ClickOutcome.NOT_OPENED.value, "click_no_popup")
        self.assertEqual(ClickOutcome.NO_OFFER.value, "click_url_notoffer")
        self.assertEqual(ClickOutcome.DENIED.value, "click_deny")

    def test_only_the_outcomes_that_reached_a_product_carry_an_offer_id(self):
        """deny 即使知道编号也不写 offer_id 列，所以有没有编号由种类唯一决定。"""
        self.assertTrue(ClickOutcome.SUBMITTED.has_offer_id)
        self.assertTrue(ClickOutcome.SKIPPED.has_offer_id)
        self.assertTrue(ClickOutcome.UNREADABLE.has_offer_id)
        self.assertFalse(ClickOutcome.NOT_OPENED.has_offer_id)
        self.assertFalse(ClickOutcome.NO_OFFER.has_offer_id)
        self.assertFalse(ClickOutcome.DENIED.has_offer_id)


class ClassifyTests(unittest.TestCase):
    def test_every_event_name_maps_to_its_outcome(self):
        cases = {
            "click_ok": ClickOutcome.SUBMITTED,
            "click_skipped": ClickOutcome.SKIPPED,
            "click_parse_error": ClickOutcome.UNREADABLE,
            "click_no_popup": ClickOutcome.NOT_OPENED,
            "click_url_notoffer": ClickOutcome.NO_OFFER,
            "click_deny": ClickOutcome.DENIED,
        }
        for name, expected in cases.items():
            with self.subTest(event=name):
                self.assertIs(classify(name, "page=1&idx=0").outcome, expected)

    def test_a_legacy_event_name_is_read_as_its_current_outcome(self):
        """库里真有 9 条 click_parse_empty（轮次 10–12），读侧要认得它。"""
        got = classify("click_parse_empty", "page=2&idx=20&offer_id=986412627824")

        self.assertIs(got.outcome, ClickOutcome.UNREADABLE)
        self.assertEqual(got.card, CardRef(page=2, index=20))
        self.assertEqual(got.offer_id, "986412627824")

    def test_an_unrecognised_event_name_has_no_classification(self):
        self.assertIsNone(classify("detail_parse", "sku_count=3"))
        self.assertIsNone(classify("popup_close", None))

    def test_a_row_without_a_position_still_classifies(self):
        got = classify("click_deny", None)

        self.assertIs(got.outcome, ClickOutcome.DENIED)
        self.assertIsNone(got.card)

    def test_the_offer_id_is_read_back_from_the_note(self):
        self.assertEqual(classify("click_ok", "page=1&idx=0&offer_id=9&sku=3").offer_id, "9")
        self.assertIsNone(classify("click_no_popup", "page=1&idx=0").offer_id)

    def test_the_skip_reason_distinguishes_today_from_a_duplicate(self):
        today = classify("click_skipped", "page=1&idx=0&offer_id=9")
        dup = classify("click_skipped", "page=1&idx=0&offer_id=9&dup=1")

        self.assertIs(today.skip_reason, SkipReason.TODAY)
        self.assertIs(dup.skip_reason, SkipReason.DUPLICATE)
        self.assertIsNone(classify("click_ok", "page=1&idx=0&offer_id=9&sku=3").skip_reason)

    def test_the_deny_ladder_is_read_back_from_the_note(self):
        first = classify("click_deny", "page=1&idx=0&n=1")
        self.assertEqual(first.deny_step, 1)
        self.assertIsNone(first.deny_terminal)

        last = classify("click_deny", "page=1&idx=0&n=3&skip")
        self.assertEqual((last.deny_step, last.deny_terminal), (3, "skip"))

        abort = classify("click_deny", "page=1&idx=0&n=1&round_abort")
        self.assertEqual((abort.deny_step, abort.deny_terminal), (1, "round_abort"))

        shop = classify("click_deny", "page=1&idx=0&n=1&shop_skip")
        self.assertEqual((shop.deny_step, shop.deny_terminal), (1, "shop_skip"))

        self.assertIsNone(classify("click_deny", "page=1&idx=0").deny_step)


class RecordTests(unittest.TestCase):
    """生产者侧：调用方拿 `(事件名, kwargs)` 去发，不再自己拼事件名与备注。"""

    def test_a_submitted_card_records_its_sku_count(self):
        event, payload = ok(CardRef(page=1, index=0), "9", 3)

        self.assertEqual(event, "click_ok")
        self.assertEqual(payload, {"offer_id": "9",
                                   "note": "page=1&idx=0&offer_id=9&sku=3"})

    def test_a_skipped_card_records_which_kind_of_skip(self):
        event, today = skipped(CardRef(page=2, index=20), "9", SkipReason.TODAY)
        _, duplicate = skipped(CardRef(page=2, index=20), "9", SkipReason.DUPLICATE)

        self.assertEqual(event, "click_skipped")
        self.assertEqual(today, {"offer_id": "9", "note": "page=2&idx=20&offer_id=9"})
        self.assertEqual(duplicate["note"], "page=2&idx=20&offer_id=9&dup=1")

    def test_an_unreadable_card_records_the_product_it_reached(self):
        event, payload = unreadable(CardRef(page=1, index=0), "9")

        self.assertEqual(event, "click_parse_error")
        self.assertEqual(payload, {"offer_id": "9", "note": "page=1&idx=0&offer_id=9"})

    def test_a_card_that_never_opened_records_only_its_position(self):
        event, payload = not_opened(CardRef(page=1, index=3))

        self.assertEqual(event, "click_no_popup")
        self.assertEqual(payload, {"note": "page=1&idx=3"})

    def test_a_page_that_is_not_a_product_records_only_its_position(self):
        event, payload = no_offer(CardRef(page=1, index=3))

        self.assertEqual(event, "click_url_notoffer")
        self.assertEqual(payload, {"note": "page=1&idx=3"})

    def test_a_denied_card_records_its_step_on_the_ladder(self):
        event, first = denied(CardRef(page=1, index=0), 1)
        _, last = denied(CardRef(page=1, index=0), 3, terminal="skip")
        _, abort = denied(CardRef(page=1, index=0), 1, terminal="round_abort")

        self.assertEqual(event, "click_deny")
        self.assertEqual(first, {"phase": "detail", "note": "page=1&idx=0&n=1"})
        self.assertEqual(last["note"], "page=1&idx=0&n=3&skip")
        self.assertEqual(abort["note"], "page=1&idx=0&n=1&round_abort")

    def test_the_recorded_payload_is_read_back_by_the_classifier(self):
        """写出去的东西要能被读回来——同一条协议的两端在这里对上。"""
        pairs = [
            ok(CardRef(page=2, index=5), "9", 4),
            skipped(CardRef(page=2, index=5), "9", SkipReason.DUPLICATE),
            unreadable(CardRef(page=2, index=5), "9"),
            not_opened(CardRef(page=2, index=5)),
            no_offer(CardRef(page=2, index=5)),
            denied(CardRef(page=2, index=5), 3, terminal="skip"),
        ]
        for event, payload in pairs:
            with self.subTest(event=event):
                got = classify(event, payload["note"])
                self.assertIsNotNone(got, event)
                self.assertEqual(got.card, CardRef(page=2, index=5))


if __name__ == "__main__":
    unittest.main()
