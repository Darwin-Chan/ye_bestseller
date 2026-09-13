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

**这是有意的行为变化，共三处**（前两处在库里没有孤儿时也成立，别把它只当成孤儿的事）：

1. **成功口径**：数「成功商品 / SKU 行数」的地方（过程页、结果页、摘要）都排除了孤儿。
   `tools/check_orphans.py` 不受影响（它按行报孤儿，不是数它们）。
2. **摘要的 `fail_offers`**：从「有 `失败` 快照的商品数」改成失败率那条口径
   （`discovered - handled`，即「榜单行里没成功的商品数」）。于是「本轮一次都没碰到的榜单行
   商品」现在算失败（旧口径不算），而孤儿的失败快照不算（它不在榜单里）。
3. **摘要的 `shop_offers`**：从「榜单行行数」改成「按商品去重的榜单行数」。同一商品在
   `shop_offers` 落两行（不同档位）时，旧口径会多算。

失败率那一路（`pipeline._finalize_round` 的算式与两条日志）行为不变：旧 `offer_counts` 本来
就是榜单口径。用例 `test_round_tally_counts_every_question_in_one_place` 钉住了 1：4 个榜单
行商品（1 成功两行 SKU、1 跳过、1 失败、1 只有榜单行）+ 1 个孤儿 → `discovered=4, handled=2,
failed_offers=2, success_offers=1, success_skus=2, orphans=1`；摘要那两处见
`tests/test_summary.py`。

**性能**：口径统一到榜单行要付一次「这条快照对应的商品在不在榜单里」的归属核对。第一版
把它写成相关 `EXISTS`（2.4 万行 46 秒），第二版写成 JOIN（SQLite 会挑「从榜单行驱动」的
顺序，3.6 万行 >3 分钟）。最后的形状是**三条覆盖索引聚合**：榜单行一条、快照侧一条、孤儿
侧一条，榜单口径 = 快照侧 − 孤儿那一份（差额正好是孤儿，不需要逐行核对）。30 万行合成库
（12 店 × 2.5 万 = 30 万快照 **+ 30 万榜单行**；`tools/bench_refresh.py` 的合成库现在两类
都写，否则 `discovered` 恒为 0）实测：

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

两轴审查（成文标准硬违规 0；另一轴的独立核对见下）另抓到几处，都已收口：

1. 「成功或跳过」与「成功库存快照」两个判据在 `_SNAPSHOT_TALLY_SQL` / `_ORPHAN_TALLY_SQL`
   里各写两遍，`round_tally()` 里第三条查询还内联着——「判据只定义一次」当时只兑现到 module
   边界。现在两个判据是模块常量（`_TALLY_HANDLED_WHEN` / `_TALLY_SUCCESS_WHEN`），三条查询
   拼同一份 `_tally_columns()`，榜单侧那条也收成 `_DISCOVERED_TALLY_SQL`。
2. `click_card_failures(round_id, shop_key=None)` 的按店铺参数全仓零调用，而本 ADR 正是把
   「逐店切片」列为被否方向——形状却从旁边的参数漏了进来。参数删掉，它只管整轮。
3. `_tally_by_shop(round_id, width, sql)` 的 `width` 是要与 SELECT 列数对齐的魔术数，返回
   无列名的 `tuple`、由 `_shop_tally` 按位置解包（改一条 SQL 的列数就静默错位）。改成
   `_counts_by_shop(round_id, sql)`：列名取查询自己的 `AS` 别名，交回
   `{店铺编号: {列名: 计数}}`。
4. 整轮合计原先写作 `ShopTally(shop_key="")`——空串当「整轮」哨兵，`shop("")` 会把它当成
   一家店返回。改成 `shop_key=None`（字段类型跟着变成 `str | None`）；`RoundTally.shop()`
   对本轮没出现过的店铺返回零计数而不是 `None`，调用方不用自己补默认
   （`views._shop_breakdown` 因此少一处 `or ShopTally(...)`）。
5. **规格轴**：摘要那两处口径变化原先只在正文里含糊带过，与「除孤儿那处外行为零变化」的
   说法打架。现在「结果」段把三处变化逐条写明，并点出失败率那一路是不变的那条。
6. **规格轴**：`tools/bench_refresh.py` 补写 30 万榜单行是必要的（不补 `discovered` 恒为
   0），但它换了护栏的测量对象；「性能」段已写明合成库是「30 万快照 + 30 万榜单行」。
7. **规格轴**：规格里「结果页 / 摘要 / 失败率三处都会变小」的说法不成立——失败率那一路
   本来就只数榜单行（新旧都是 `(2, 1)`），本 ADR 没这么写过，记一笔免得再被引用。

规格轴另做了一次独立核对：`work/oracle_tally.py` 造 60 轮随机库（每店 0–6 条榜单行、
0–2 个孤儿、成功/跳过/失败/不完整四种状态），把 `round_tally()` 与「按定义手算的集合」
逐店、逐整轮对照，**0 处不一致**——「榜单口径 = 快照侧 − 孤儿那一份」这个式子成立。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「本轮计数」「失败率」「跳过」「成功库存快照」；
来源是 `docs/reviews/architecture-review-2026-09-13-r2.html` 候选 01，规格在
`.scratch/round-tally/spec.md`。
