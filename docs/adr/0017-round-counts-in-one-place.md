# 0017. 本轮计数只有一处：`Database.round_tally()`，口径统一到榜单行

- 状态：已接受
- 日期：2026-09-13

## 背景

「这一轮发现了多少商品、处理了多少、成功了多少」在四个地方各写一遍：

- `views._snapshot_counts`（`views.py:142`）：过程页与结果页的「商品数 / SKU 行数」，
  `COUNT(DISTINCT offer_id), COUNT(*) FROM snapshots WHERE page_status='成功' AND sku_id
  IS NOT NULL`；
- `tools/summary.summarize`：同一件事的第三种写法（先数 `COUNT(*) FROM shop_offers`，
  再数快照）；
- `db.offer_counts`（失败率用）：`SELECT ... FROM shop_offers WHERE EXISTS (SELECT 1 FROM
  snapshots ... page_status IN ('成功','跳过'))`；
- `Database.success_rows` / `failed_rows`：为同样两个谓词各配一个取行方法，其中
  `success_rows()` 全仓零调用，`failed_rows()` 只有两条测试在调。

口径已经分歧：`offer_counts` 数的是「榜单行里有过快照的」，`summary.summarize` 数的是
「快照里出现过的商品」。IS-49 那种**孤儿**（有快照、无榜单行）会让两个答案不一样——摘要
工具会把孤儿算成成功商品，失败率那一路不会。

## 决策

- **落点**：数据层 `Database.round_tally(round_id) -> RoundTally`。谓词属于数据层；页面、
  失败率、摘要工具都从这里读同一份口径。
- **口径统一到榜单行**：商品数按榜单行的 `(round_id, shop_key, offer_id)` 去重；成功
  商品数与成功 SKU 行数只数榜单行里有的商品。孤儿单列 `orphans`，不算成功商品。
- **一次算全 + 按店铺切片**：整轮三条 `GROUP BY shop_key` 的聚合成一份 `RoundTally`
  （整轮合计 + `per_shop` + 点击未得卡片），要某一家店时 `RoundTally.shop(shop_key)`
  切片，不为每家店各跑一遍（IS-38 的护栏）。
- **删掉两个没价值的取行方法**：`Database.success_rows()`（零调用）与
  `Database.failed_rows()`（只有测试在调），测试改断言 tally。
- **失败率算式留在 `pipeline._finalize_round`**：它是 CONTEXT.md 定义的领域词，计数只是
  它的输入。

## 结果

**这是一处有意的行为变化**：凡是数「成功商品 / SKU 行数」的地方，现在都排除了孤儿。结果页
与 `tools/summary.py` 的数字在库里有孤儿时会变小（过程页同理）；`tools/check_orphans.py`
不受影响（它是按行报孤儿，不是数它们）。用例
`test_round_tally_counts_every_question_in_one_place` 钉住了这件事：4 个榜单行商品
（1 成功两行 SKU、1 跳过、1 失败、1 只有榜单行）+ 1 个孤儿 → `discovered=4, handled=2,
failed_offers=2, success_offers=1, success_skus=2, orphans=1`。

**性能**：口径统一到榜单行要付一次「这条快照对应的商品在不在榜单里」的归属核对。第一版
把它写成相关 `EXISTS`（2.4 万行 46 秒），第二版写成 JOIN（SQLite 会挑「从榜单行驱动」的
顺序，3.6 万行 >3 分钟）。最后的形状是**三条覆盖索引聚合**：榜单行一条、快照侧一条、孤儿
侧一条，榜单口径 = 快照侧 − 孤儿那一份（差额正好是孤儿，不需要逐行核对）。30 万行合成库
（12 店 × 2.5 万快照）实测：

| 查询 | 实测 |
| --- | --- |
| 榜单行（`shop_offers`） | 0.07 秒 |
| 快照侧（`snapshots`） | 0.19 秒 |
| 孤儿侧（anti-join） | 0.20 秒 |
| `round_tally()` 合计 | 0.46 秒 |
| 改造前：逐店成功计数 | 0.08 秒 |
| 改造后：一次刷新（`get_run`） | 0.53 秒（护栏 1.0 秒） |

代价是刷新路径比改造前慢约 3 倍（0.15 → 0.53 秒），换来的是「页面、失败率、摘要同源」；
真实轮次规模约为合成库的 1/8，刷新在 0.1 秒量级。两条索引：
`idx_shop_offers_key`（SCHEMA）与 `idx_snapshots_round_counts`
（`snapshots(round_id, shop_key, offer_id, page_status, sku_id)`）。后者引用后加的两列，
老库的 `snapshots` 可能还没有它们，所以**只能由迁移建**（SCHEMA 先跑、迁移后跑）——
`migrate()` 的一次性报告因此多一条 `created_indexes`，两条钉住「新库/老库第一次开连接建
了哪些索引」的用例跟着更新。

被否掉的方向：

- **让页面继续读快照、只把失败率那一路收进来**：口径分歧还在，正是这一条要修的东西。
- **把逐店切片做成 `round_tally(round_id, shop_key=...)`**：调用形状会鼓励每家店各跑一遍
  （接进刷新路径后实测 1.3 秒，撞上 IS-38 的护栏）。
- **把近似索引的建法写进 SCHEMA**：老库（`snapshots` 没有 `page_status`）开库直接报
  `no such column`。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「本轮计数」「失败率」「跳过」「成功库存快照」；
来源是 `docs/reviews/architecture-review-2026-09-13-r2.html` 候选 01，规格在
`.scratch/round-tally/spec.md`。
