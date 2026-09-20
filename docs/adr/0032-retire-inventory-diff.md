# 0032. `inventory.diff` 退役

- 状态：已接受（2026-09-20 实现落地，票据 01）
- 日期：2026-09-20

## 背景

`inventory.diff` 是**写入时算的派生列**（`_upsert_inventory`），口径是
「当前日期库存 − 最近更早日期同 SKU 库存」（`CONTEXT.md` 的「库存差分」）。

多机分片之后它成了最难处理的一块：**基准是"本机库里"的上一日行**。店铺每周换机器接手时，
新机器本地没有该店上一日的库存，差分就缺基准；而导入别人的数据又会让已经写进库的 diff
失真——它算的是导入之前的本地状态。

查下来，这一列**没有任何生产读方**：

- 写入方只有 `db._upsert_inventory` 一处；全仓 `*.py` 里读它的只有一条测试断言
  （`tests/test_db.py:734`）。
- `analysis.py` 从 `stock` 序列自己算销量，从不读这一列；
  `views.py` 只读计数；`tools/` 里没有读它的。
- 分析侧的规格自己就是这么定的：该列"可供诊断和一致性比较，不作为任意区间销量的唯一计算来源"
  （`.scratch/bestseller-analysis/spec.md:125-126`）。

也就是说：多机化给一个**没人读的字段**制造了一整套难题。

## 决策

**删掉 `inventory.diff`，口径随列一并退役。** 跨机合并不再需要"差分重算"——
这一问不是被回答了，是**被取消了**。

顺带删掉 `CONTEXT.md` 的「库存差分」词条，以及 `PRD-1688库存快照MVP.md:92` 那条口径描述
所对应的实现。

## 被否掉的方向

- **每次导入后全库重算**（一条 `LAG(stock) OVER (PARTITION BY shop_key, offer_id, sku_id ORDER BY date)`）：
  口径唯一、成本可忽略（实测规模约 2.5 千行/天），但它维护的是一个**没人读**的字段，
  还把一个派生值重新变成权威存储。
- **保留但降级为"采集机本机诊断值"**：留下一个"有时对有时不对、没人读但看着权威"的列，
  比删掉更坏——下一个人会按它下结论。

## 结果与代价

**收益**：写路径少一次**逐行基线查询**。`_upsert_inventory` 现在是"逐行循环 + 每行一次 SELECT"，
只为算 diff 而存在；删掉后是纯 upsert，可以改 `executemany`。按实测 2479 行/天算，
**每天少 2500 次查询**。

**代价，说在明处**：

- **旧包多一列**：包格式是票据 03 的结论，交换集成员不变但列集变了。导入一律**显式投影列**，
  不做 `SELECT *`。
- **程序与库必须一起升**：`dist/` 里打包好的 exe 用的旧 `db.py`，它的 INSERT 里写着 `diff`，
  写新库会当场报 `no column named diff`，采集直接挂。删列把版本偏斜从静默降级变成**硬失败**。
- **改动跨 effort**：`bestseller-analysis` 已交付。它现在不读这一列，将来若想要"净变化"
  得从 `stock` 序列自算（它本来就在自算销量，成本近零）；但两边 spec 会打架，本条就是留痕。
- **已存的值不完全可复现**：写入时的基准行可能被后来的 `_clear_other_granularity`
  （同店同商品同日反粒度清理）删掉。既然没人读，无实际影响；但"随时能重算"不等于
  "能算回同样的历史值"。
- **文件不会立刻变小**：实测删列后 40.1 → 40.1 MiB，空闲页留着复用，回收要 `VACUUM`——不必做。

**实测过的部分**（不是推断）：在生产库副本上 `ALTER TABLE inventory DROP COLUMN diff`——
外键开启、22315 行，**0.052 秒成功**，22249 行非空 `stock` 完好，列顺序正常。
SQLite 3.50.4（需要 ≥3.35）。仓库里没有视图、触发器、索引或外键引用该列；
`_drop_column` 助手已有先例（`skus.main_image_url`），且自带"列不存在就跳过"的护栏，
对「连接即迁移」是安全的。三台采集机与每台汇总机各在第一次开库时付一次（实测 0.05 秒）。

**落地实证（2026-09-20，票据 01）**：在生产库副本上按连接即迁移跑通——22315 行 `inventory`，
开库 61–76 毫秒（两次实测）完成删列与建索引；迁移报告如实列出两段动作（`drop_inventory_diff`、
`version_dedupe_index`，建起 `idx_product_information_dedupe`），重开不再付成本；
行数与 22249 行非空 `stock` 完好、`integrity_check` 通过。旧程序写新库的硬失败在副本上复现：
`table inventory has no column named diff`（回归守卫：`tests/test_db.py` 的
`InventoryDiffRetirementTests`）。迁移后分析链路照常出数（2026-09-07..09-13 窗口、20387 行）。

**它不是单向门**：后悔了就是 `_add_column` 加回 + 从 `stock` 序列回填。

**要写进 spec 的连带结论**：diff 退役后，跨机合并的正确性**全部压在 `stock` 序列上**。
分析侧的 `prior` / `fallback` 规则意味着区间起点前最近的那条观测决定首日销量，
所以"合并时漏了一行"会改变分析结果——这正是"缺口如实报告"要盯的东西。

完整设计与验收见 [`.scratch/multi-machine-collection/spec.md`](../../.scratch/multi-machine-collection/spec.md)（票据 09 收口）；
合并判据见 [ADR-0031](0031-cross-machine-merge-decided-by-package-claim.md)。
