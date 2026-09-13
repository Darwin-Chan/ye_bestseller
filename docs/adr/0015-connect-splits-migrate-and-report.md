# 0015. 连接即迁移：拆出 migrate() 并交一份迁移报告

- 状态：已接受
- 日期：2026-09-13

## 背景

`db.connect()` 是 120 行、里面 12 段迁移，每段都在猜「这个库还缺它吗」。其中 11 段靠
**吞掉 `sqlite3.OperationalError`** 判断（重复加列会抛错，抛了就当已经迁过），只有快照去重
那段有显式条件（唯一索引在不在）；而「连接即迁移」意味着界面每约 2 秒刷新一次就要走一遍。

于是谁也说不清「这次开库到底做了什么」，测试只能按 SQL 文本认：

```python
# tests/helpers.py 当时的自注：生产语句是私有常量，用例只能按语句形状认它
SNAPSHOT_DEDUPE_MARK = "DELETE FROM snapshots"
```

配合「换掉 `sqlite3.connect` 再挂 trace callback」的观测装置，`test_db.py` 两条迁移用例与
`test_gui.py` 的刷新成本用例都靠它；改一句 SQL 的文字会同时打断两个 module 的用例。
`bench_refresh.py` 量那次整表去重时，还得前后各数一遍行数来倒推删了几行。

## 决策

- **迁移排成惰性清单**：`_MIGRATIONS` 里每段有名字与 `apply(conn, out) -> bool`
  （True = 这次真的动手了）。动作体与条件**逐字照旧**——包括那 11 段的隐式条件；
  这次不重写迁移逻辑，只让它可读、可数。
- **`migrate(conn) -> MigrationReport`**：按序跑完，交回
  `applied`（动过手的段名）、`deduped_snapshot_rows`（None = 这次没跑整表去重）、
  `created_indexes`。
- **`open(db_path)` 只管开库**：建目录、开连接、两条 PRAGMA、建表。
  **`connect = open + migrate`**，签名不变——界面、命令行、五个工具与十几个测试的
  20+ 处调用点一个字都不用改。
- **测试与基准读报告**：`test_db.py` 三条用例改成读报告（首次开库删 1 行、建索引；
  第二次开库两样都没有；全新的库删 0 行但建索引）；`test_gui.py` 的刷新用例改成
  「这次开库没有任何一段迁移动手」；`SNAPSHOT_DEDUPE_MARK` 与 `traced_connections` 删掉；
  `bench_refresh.py` 的整表去重成本直接读 `deduped_snapshot_rows`。

## 结果

「这次开库做了什么」从「按 SQL 文本猜」变成「读一份报告」：改 SQL 的文字不再打断别处用例，
界面刷新那条用例还顺带钉住了「刷新不付任何迁移成本」。`connect()` 只剩两行。

代价有两处。其一，报告只在 `migrate()` 的返回值里；`connect()` 的调用方（界面每次刷新的
那条路）拿不到它，想观察刷新成本得自己 `open()` + `migrate()`——这正是界面那条用例的做法，
但如果将来要长期监控刷新成本，得再给一个入口。其二，隐式条件照旧：报告能说「这一段动手了」，
说不出「为什么动手」；想读懂某一段仍要去看它的 PRAGMA/异常判断。

两轴审查在第一版里抓到两处**行为不等价**（都撞「动作体与条件逐字照旧」这条承诺），已改回：

1. `_skus_primary_key_on_sku_id` 原来把**整段**（PRAGMA、RENAME、重建、DROP）包在
   `except OperationalError` 里；重排时我只把 PRAGMA 包了进去，于是「上一次迁移半途留下的
   `skus_old` 表」会让 RENAME 抛错并冒到 `connect()`——**那个库直接打不开，界面刷新即崩**。
   已整段包回 try。
2. `migrate()` 把原来逐段的 `conn.commit()` 收成末尾一次：正常路径等价，但末段
   `_snapshot_success_index`（最贵的整表去重 + 建索引）一抛错就会把前面各段一起回滚。
   已按段恢复提交时机（两个 `_drop_column`、`drop_shops_active`、轮次列各提交一次）。

顺带补了 `tests/test_db.py` 的 `SkusPrimaryKeyMigrationTests`：旧主键迁移后主键变成
`(offer_id, sku_id)` 且同名 SKU 合并保留名字较大的那行；以及「残留 `skus_old` 时这一段
静默跳过、库照常打得开」——改前仓库里**没有**任何 skus 旧主键迁移的用例，所以第一版的
回归全套 337 条也照样全绿。另外把迁移日志统一成「迁移 <段名>：…」，`bench_refresh` 顺手
打印建了哪个索引。

被否掉的方向：

- **把「加列前先查缺哪列」显式化**（11 段手工改写）：条件写错就会让旧库迁移**静默失效**，
  最坏是这个库打不开，而收益只是读起来更明确——报告的观测性已经够用。
- **`connect()` 改成返回 `(conn, report)`**：20+ 处调用点全要改，收益只是省一次 open。
- **只拆函数、不列清单**：报告能给出整表动作，但「哪几段动过手」仍然数不出来。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「连接即迁移」「迁移报告」；来源是
`docs/reviews/architecture-review-2026-09-13.html` 候选 05，规格在
`.scratch/migration-report/spec.md`。
