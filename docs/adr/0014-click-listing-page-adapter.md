# 0014. 点击式列表：卡片层 adapter + 遍历上下文

- 状态：已接受（描述被后续两条更新：[ADR-0018](0018-page-to-observation-in-detail.md) 把补采 adapter 改名 `open_detail` 且只交 html、`FailureKind` 去掉「访问异常」；[ADR-0020](0020-detail-visit-shared-across-paths.md) 把卡片句柄的 `denied` 换成 `page`（deny 判定归 `guard.ready_detail_page()`），`DenyTracker` 与两个 deny 异常也从 `click_listing` 搬进 `guard`）
- 日期：2026-09-13

## 背景

点击式列表的遍历与页面动作混在 `browser_pw.py` 里，测试只能站在私有函数上。

- 跑一遍「这家店」要打 4–7 个桩：`listing.wait_cards`、`listing.intervention_kind`、
  `listing.click_text_in_frames`、`browser_pw._scroll_cards_until_stable`、
  `browser_pw._read_card_title`、`browser_pw._capture_card`；翻页那一侧还要
  `listing.list_identity` / `listing.WAIT_NEXT_SEC` / `listing._POLL_SEC`。
- `_capture_card` 的 16 个位置参数形参表被抄了四遍（`test_p1.py` 两处、`test_deny_tracker.py`、
  `test_round_stop.py`）：改一次形参顺序，四个文件一起红，而它们本意只是「这几张卡会被读成什么」。
- `db` / `round_id` / `shop` / `cfg` / `human` / `offers` / `seen` / `emit` / `deny_tracker`
  这些「这家店这次遍历」的事实没有名字，靠参数在函数之间传来传去。

## 决策

新建 `bestseller_monitor/click_listing.py`，分三块：

- **卡片层 adapter**（端口，四个动作）：`prepare(describe, *, emit)`、
  `scroll_to_load(describe) -> int`、`card(index) -> 卡片句柄`、`advance(describe) -> bool`，
  外加 `load_failed(reason)` 把「榜单没拿到」表示成 `ListingLoadFailed`。
- **卡片句柄**：`title` / `open` / `opened` / `denied` / `url` / `offer_id` / `read` / `close`。
  `read` 返回 `detail.Observation`，就是候选 02 那道 `capture_observation(..., read)` 的 adapter；
  弹窗谁开谁关。
- **遍历上下文 `ShopWalk`**：持有那九件事实，`run()` 跑主遍历与同名补抓，
  `capture(card, list_title)` 处理一张卡（deny 重试、认领事件、按结果记事件、关卡片）。

生产实现是 `PlaywrightListing` / `PlaywrightCard`（把四个动作翻译成页面操作，页号由它记，
卡片句柄用它拼事件备注与机会标识）；`browser_pw.py` 只留浏览器会话、`capture_detail`
（补采那侧的页面 adapter）与 deny 跟踪。

三个细节值得写下来：

- **准备与推进不重做**：adapter 转发给候选 01 立的 `listing.prepare` / `listing.advance`，
  没有第二套翻页规则。
- **机会仍然在点开卡片之前申请**：`ShopWalk._enter_detail` 先
  `dedupe.claim_slot(..., card.ref)`，再 `human.before_detail()`，最后才 `capture`——
  预算限制的是详情访问，顺序不能反（候选 02 的评审专门盯过这一点）。
- **`detail.Observation` 多带一个失败种类**（`FailureKind`：读不到页 / 解析失败 / 访问异常）：
  「读不到页只发 `click_parse_error`、解析失败才多一条 `sku_count=0`」这条差别的依据，
  现在由遍历拿着种类决定；事件仍然只在一处发。
- **遍历要求 `db` 与 `round_id`**：规则要落库，签名上不再用默认值假装它们可空。

## 结果

测试面从「打一堆桩」变成「给一个脚本化 adapter」：`tests/test_click_listing.py` 新增 23 条
（遍历规则 8、同名与第二遍 3、deny 2、停止与预算 5、Playwright adapter 自己的翻译活儿 4、
外加重复商品只留一行榜单的 1 条），`tests/test_p1.py` 从 42 条瘦到 23 条（点击遍历那批搬走、
管线级用例改成打桩 `click_listing.crawl_store_by_click`），`test_round_stop.py` /
`test_deny_tracker.py` 里直接调 `_capture_card` 的用例改由遍历级用例守着。全套
`python -m unittest discover`：331 条通过、1 条跳过。

行为一行未改：事件的内容与顺序、机会的申请/绑定/退还、额度与重试、提交后的停止判定全部照旧
（那批用例就是改前那批的等价搬运）。`browser_pw.py` 从「什么都有」瘦到只剩会话、补采的详情
读取与 deny 跟踪。

代价有两处。其一，多了一个 module 与一层间接：读代码的人要先知道「遍历从 adapter 要页面」，
才能顺着 `PlaywrightListing` 找到真正的页面操作。其二，`crawl_store_by_click` 的签名变了
（第一个参数从 Playwright 页面变成 adapter），旧调用点必须改——仓库内只有 `pipeline` 与测试
用它，都已改完。

被否掉的方向：

- **批次层 adapter**（`next_batch() -> 一批卡片`）：把翻页与边界条件一起吞进去，
  与候选 01 刚立的 `advance` 重复。
- **只抽卡片句柄**：准备与推进那两条最常打的桩留着，收益不够。
- **协议放 `listing.py`、实现放 `browser_pw.py`**：榜单页行为与「页面从哪来」会再次混在一起。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「榜单批次」「商品」「详情观测」；来源是
`docs/reviews/architecture-review-2026-09-13.html` 候选 03，规格在
`.scratch/click-listing/spec.md`。
