"""详情观测 module：一次详情采集的规则（候选 02）。

两条路径（点击式主遍历、逐店补采）都从 `capture_observation` 穿过；测试走它的 interface，
用脚本化的 `observe` adapter 与真实内存库，断言结果与库里的行，不碰私有函数。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bestseller_monitor import dedupe, detail, rounds
from bestseller_monitor.db import Database, DayBoundaryReached, connect, cst_date, utcnow
from helpers import crawler_cfg, new_round


class DetailTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.conn = connect(self.tmp / "detail.db")
        self.addCleanup(self.conn.close)
        self.db = Database(self.conn)
        # 「今天已经采过」落在昨天那一轮（库存行仍是今天的，同日去重看的是日期不是轮次），
        # 本次观测发生在今天这一轮。
        self.past_round = new_round(self.db, "A01", run_date="2026-01-01")
        self.round_id = new_round(self.db, "A01")
        # 详情预算沿用这份用例原来的默认（10）：它靠「机会用尽」那一支说话，
        # 吃采集配置替身的 1000 会让那条路够不着。
        self.cfg = crawler_cfg(raw_page_dir=self.tmp / "raw",
                               max_detail_opportunities_per_round=10)
        self.human = MagicMock()
        self.visits = 0

    # ---------- 夹具 ----------
    def target(self, **overrides):
        values = dict(
            shop_key="A01", shop_url="https://A01.example/", shop_name="店铺A",
            product_url="https://detail.1688.com/offer/11.html",
            slot_key="card:p1&i0", offer_id="11", list_title="商品11", duplicate=False,
        )
        values.update(overrides)
        return detail.DetailTarget(**values)

    def observe(self, *observations):
        """脚本化的 adapter：按顺序交回观测，用完后一直重复最后一个；记录被叫了几次。"""
        seq = list(observations)

        def adapter():
            self.visits += 1
            return seq.pop(0) if len(seq) > 1 else seq[0]

        return adapter

    def payload(self, offer_id="11", rows=(("默认(单规格)", 3),)):
        return detail.Observation(
            payload={"product_name": "商品11",
                     "rows": [{"sku_name": n, "sku_stock": s} for n, s in rows]},
        )

    def collected_today(self, offer_id):
        """把某商品记成「今天已采过」：更早那一轮的成功观测 + 当天库存。"""
        self.db.submit_inventory_snapshot(
            round_id=self.past_round, shop_key="A01", shop_url="https://A01.example/",
            shop_name="店铺A", offer_id=offer_id,
            product_url=f"https://detail.1688.com/offer/{offer_id}.html",
            list_title="商品", detail_title="商品", main_image_url="",
            sku_rows=[{"sku_name": "默认(单规格)", "sku_stock": 1}],
            collected_at=utcnow(), attempt=1,
        )

    def capture(self, target, observe, **kwargs):
        return detail.capture_observation(
            self.db, self.cfg, self.human, self.round_id, target, observe, **kwargs)

    def rows(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def opportunity_ledger(self):
        return [(row["identity"], row["offer_id"]) for row in self.rows(
            "SELECT identity, offer_id FROM detail_opportunities WHERE round_id=?",
            (self.round_id,))]

    def snapshots(self, page_status):
        return self.rows(
            "SELECT * FROM snapshots WHERE round_id=? AND page_status=?",
            (self.round_id, page_status))


class ImageEvidenceTests(DetailTestCase):
    def test_shared_capture_retries_image_and_persists_asset(self):
        import io
        from PIL import Image
        stream = io.BytesIO()
        Image.new('RGB', (1, 1), 'red').save(stream, format='PNG')
        observation = self.payload()
        observation.payload['main_image_url'] = 'https://image.example/item.png'
        with patch('bestseller_monitor.product_images.urlopen',
                   side_effect=[OSError('temporary'), io.BytesIO(stream.getvalue())]) as fetch:
            result = self.capture(self.target(), self.observe(observation))
        self.assertEqual(result.outcome, detail.Outcome.SUBMITTED)
        self.assertEqual(fetch.call_count, 2)
        version = self.rows('SELECT * FROM product_information_versions')[0]
        self.assertIsNotNone(version['content_hash'])
        self.assertIsNone(version['image_error'])
        self.assertEqual(self.rows('SELECT content FROM product_image_assets')[0]['content'], stream.getvalue())


class SameDaySkipTests(DetailTestCase):
    """同日去重：今天采过的商品不重复访问详情，也不消耗详情预算（ADR-0001）。"""

    def test_known_offer_collected_today_is_skipped_without_a_visit_or_a_slot(self):
        self.collected_today("11")
        target = self.target(offer_id="11", slot_key="11")

        result = self.capture(target, self.observe(self.payload()))

        self.assertEqual(result.outcome, detail.Outcome.SKIPPED_TODAY)
        self.assertEqual(self.visits, 0, "今天采过就不该再打开详情")
        self.assertEqual(self.opportunity_ledger(), [], "跳过不占详情机会")
        self.assertEqual(len(self.snapshots("跳过")), 1)

    def test_the_slot_the_call_site_reserved_is_released_on_a_same_day_skip(self):
        """点击路径在打开卡片前就占了机会（预算限制的是详情访问），发现今天采过就要退还。"""
        self.collected_today("11")
        dedupe.claim_slot(self.db, self.round_id, "A01", "card:p1&i0", 10)

        result = self.capture(self.target(), self.observe(self.payload("11")))

        self.assertEqual(result.outcome, detail.Outcome.SKIPPED_TODAY)
        self.assertEqual(result.offer_id, "11")
        self.assertEqual(self.visits, 0, "判出今天采过就不再读页面")
        self.assertEqual(self.opportunity_ledger(), [], "同日跳过要把占用的机会退回去")
        self.assertEqual(len(self.snapshots("跳过")), 1)

    def test_a_repeated_card_does_not_submit_the_same_offer_twice(self):
        """同一次遍历里第二次看到同一个商品：不再提交（改前点击路径也是这个行为）。"""
        self.capture(self.target(), self.observe(
            detail.Observation(failure="解析失败：页面结构变了")), attempts=1)

        result = self.capture(self.target(duplicate=True), self.observe(self.payload("11")))

        self.assertEqual(result.outcome, detail.Outcome.DUPLICATE)
        self.assertEqual(self.snapshots("成功"), [], "重复卡不再提交，也不写成功快照")

    def test_known_offer_with_no_attempts_left_is_not_visited(self):
        self.db.mark_failure(self.round_id, "A01", "11", 2, "上一轮失败",
                             shop_url="https://A01.example/", shop_name="店铺A",
                             product_url="https://detail.1688.com/offer/11.html",
                             product_name="商品11")
        target = self.target(offer_id="11", slot_key="11")

        result = self.capture(target, self.observe(self.payload()))

        self.assertEqual(result.outcome, detail.Outcome.ATTEMPTS_EXHAUSTED)
        self.assertEqual(self.visits, 0)
        self.assertEqual(self.opportunity_ledger(), [])

    def test_an_empty_observation_is_recorded_as_a_read_failure(self):
        """adapter 什么都没交回来（既没 payload 也没原因）：按读取失败记一行。"""
        result = self.capture(self.target(), self.observe(detail.Observation()), attempts=1)

        self.assertEqual(result.outcome, detail.Outcome.FAILED)
        self.assertEqual(self.snapshots("失败")[0]["detail_note"], "详情页读取失败")


class FailureTests(DetailTestCase):
    """失败：记一行失败快照，原因由 module 统一拼（含原始页存档）。"""

    def test_a_failed_observation_is_recorded_with_the_raw_page(self):
        notes: list[str] = []

        result = self.capture(
            self.target(), self.observe(detail.Observation(
                failure="解析失败：页面结构变了", raw_html="<html>raw</html>")),
            attempts=1, on_attempt_failed=notes.append)

        self.assertEqual(result.outcome, detail.Outcome.FAILED)
        rows = self.snapshots("失败")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["shop_url"], row["shop_name"], row["product_url"]), (
            "https://A01.example/", "店铺A", "https://detail.1688.com/offer/11.html"))
        self.assertIn("解析失败：页面结构变了", row["detail_note"])
        self.assertIn("原始页面：", row["detail_note"])
        raw_path = Path(row["detail_note"].split("原始页面：")[1])
        self.assertTrue(raw_path.exists(), "原始页要存下来供校准")
        self.assertEqual(notes, [row["detail_note"]], "调用方拿到的就是这一条原因")

    def test_a_failure_without_a_page_keeps_just_the_reason(self):
        result = self.capture(
            self.target(offer_id="11", slot_key="11"),
            self.observe(detail.Observation(failure="详情页读取失败：连接被重置")))

        self.assertEqual(result.outcome, detail.Outcome.FAILED)
        self.assertEqual(self.snapshots("失败")[0]["detail_note"], "详情页读取失败：连接被重置")

    def test_retries_until_the_attempt_budget_runs_out(self):
        """补采路径：一次失败接着试下一次，直到成功或额度用尽。"""
        result = self.capture(
            self.target(offer_id="11", slot_key="11"),
            self.observe(detail.Observation(failure="详情页读取失败：超时"),
                         self.payload("11")))

        self.assertEqual(result.outcome, detail.Outcome.SUBMITTED)
        self.assertEqual(self.visits, 2)
        self.assertEqual(self.snapshots("失败"), [],
                         "成功提交会清掉该商品本轮的失败记录（库存快照提交口径）")
        self.assertEqual([row["attempt"] for row in self.snapshots("成功")], [2])
        self.human.sleep.assert_called_once_with(self.human.retry_delay.return_value)

    def test_a_single_attempt_leaves_the_retry_to_the_next_pass(self):
        """点击路径：失败了就交给随后的补采，不在卡片上原地重试。"""
        result = self.capture(
            self.target(),
            self.observe(detail.Observation(failure="解析失败：页面结构变了")),
            attempts=1)

        self.assertEqual(result.outcome, detail.Outcome.FAILED)
        self.assertEqual(self.visits, 1)
        self.human.sleep.assert_not_called()


class SubmitTests(DetailTestCase):
    """成功提交：整条商品结果一次写进去，提交之后才判该不该停。"""

    def test_a_successful_observation_submits_the_whole_product_at_once(self):
        result = self.capture(
            self.target(), self.observe(self.payload("11", rows=(("红", 5), ("蓝", 7)))))

        self.assertEqual((result.outcome, result.offer_id),
                         (detail.Outcome.SUBMITTED, "11"))
        rows = self.snapshots("成功")
        self.assertEqual([row["sku_stock"] for row in rows], [5, 7])
        self.assertEqual({row["shop_name"] for row in rows}, {"店铺A"})
        self.assertTrue(self.db.inventory_exists("A01", "11", cst_date()))

    def test_the_stop_check_after_submit_keeps_what_was_already_submitted(self):
        run_date = rounds.load(self.db, self.round_id).run_date
        # 进详情前还是白天；提交之后再问一次时，北京时间已经跨过这一轮的那一天。
        with patch.object(detail, "utcnow", side_effect=[
                f"{run_date}T04:00:00+00:00",   # 进详情前：还能开工
                f"{run_date}T04:00:01+00:00",   # 提交用的采集时刻
                f"{run_date}T16:00:00+00:00",   # 提交后：北京时间已是次日 0 点
        ]):
            with self.assertRaises(DayBoundaryReached):
                self.capture(self.target(), self.observe(self.payload("11")))

        self.assertEqual(len(self.snapshots("成功")), 1, "提交过的数据保留，判定不回滚它")


class ObserveHtmlTests(unittest.TestCase):
    """`observe_html`：把当前 HTML 翻译成一次观测（候选 01 / ADR-0023）。

    两条路径（点击式列表的弹窗、逐店补采的详情页）只交「怎么拿到 html」；读到什么算失败、
    留不留原始页只在这里判一次。
    """

    URL = "https://detail.1688.com/offer/11.html"
    HTML = ('<script>{"skuInfoMap":{"红色":{"skuId":"red","name":"红色",'
            '"price":10,"canBookCount":3}}}</script>')

    def test_a_read_page_becomes_a_payload_with_the_main_image(self):
        with patch.object(detail, "extract_main_image", return_value="https://img/1.png"):
            observation = detail.observe_html(self.HTML, self.URL)

        self.assertIsNone(observation.failure)
        self.assertEqual(observation.payload["main_image_url"], "https://img/1.png")
        self.assertEqual(observation.payload["rows"],
                         [{"sku_id": "red", "sku_name": "红色", "sku_price": 10.0,
                           "sku_stock": 3}])

    def test_the_payload_carries_only_the_keys_that_are_read(self):
        """成功的 payload 只有三个键：`product_name` / `rows` / `main_image_url`。

        它曾经还带一个 `html`，而全仓没有一处读它、也不落库——失败路径要用的原始页走的是
        `DetailParseFailed(html=…)` / `Observation.raw_html`，那是另一条活路。
        """
        with patch.object(detail, "extract_main_image", return_value="https://img/1.png"):
            observation = detail.observe_html(self.HTML, self.URL)

        self.assertEqual(sorted(observation.payload),
                         ["main_image_url", "product_name", "rows"])

    def test_a_parse_failure_keeps_the_raw_page(self):
        observation = detail.observe_html("<html>没有 SKU</html>", self.URL)

        self.assertEqual(observation.kind, detail.FailureKind.PARSE)
        self.assertIn("未解析到 SKU", observation.failure)
        self.assertEqual(observation.raw_html, "<html>没有 SKU</html>")

    def test_a_main_image_crash_is_a_parse_failure_with_the_raw_page(self):
        """主图字段异常算解析崩溃（旧补采路径记成「访问异常」且丢了原始页）。"""
        with patch.object(detail, "extract_main_image",
                          side_effect=ValueError("图片字段异常")):
            observation = detail.observe_html(self.HTML, self.URL)

        self.assertEqual(observation.kind, detail.FailureKind.PARSE)
        self.assertIn("图片字段异常", observation.failure)
        self.assertEqual(observation.raw_html, self.HTML, "原始页要留着供校准")

    def test_the_observation_tells_the_caller_whether_it_got_a_page(self):
        """`ok` / `sku_count`：两条路径问「这次读成了吗、几行 SKU」只用这一份判据。"""
        with patch.object(detail, "extract_main_image", return_value=None):
            read = detail.observe_html(self.HTML, self.URL)
        failed = detail.observe_html("<html>没有 SKU</html>", self.URL)

        self.assertTrue(read.ok)
        self.assertEqual(read.sku_count, 1)
        self.assertFalse(failed.ok)
        self.assertEqual(failed.sku_count, 0, "没读到页面时事件备注也写 sku_count=0")
