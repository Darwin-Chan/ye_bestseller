# 本轮已处理判据收束交付（2026-10-08）

## 来源与流程

- 来源：2026-09-23 架构审查 `#handled-predicate`（归档报告不修改）。三个判据当时同值，无生产分叉证据；本题是领域知识收束，不是新故障修复。
- 基线：`75b13294b20332c24cdb22634146301813d1a712`，候选 06 已提交。
- 顺序：grill-with-docs → to-spec → to-tickets → implement。用户分别确认最小方案、测试接缝和单票拆分后实施。
- 本机过程材料：`.scratch/handled-predicate/grill.md`、`spec.md`、`issues/01-unify-handled-predicate.md`（忽略目录，不随代码提交）。

## 已确认规格与决定

`bestseller_monitor/db.py` 的 `_HANDLED_SNAPSHOT_WHEN` 是唯一的已处理快照状态定义：成功或跳过。
计数列生成、pending 排除及跳过写入守卫直接引用它；三个快照表别名均是受控内部的 `s`。
既有 `_TALLY_HANDLED_WHEN` 重新命名，pending 与跳过守卫的两份内联条件删除。
不引入 SQL 构造函数、通用框架、新配置或公开 interface；数据继续参数绑定。

“本轮已处理”是同轮、同店、同商品有成功或跳过快照：

- 跳过是已处理，但不是成功库存观测或按商品/按名同日去重证据；成功库存判据仍单独定义。
- 失败未耗尽尝试仍可 pending；耗尽尝试不再 pending，仍不能计入 handled。MAX(attempt) 规则不变。
- 只共享状态条件，不合并整条查询：tally 按 ADR-0017 排除孤儿并单列，pending 只来自榜单，跳过守卫仍对同作用域孤儿生效。
- 商品去重、SKU 替换/保留计数、轮次/店铺/商品隔离、SQL 排序、公开返回形状和 schema 均保持。

## 候选 06 保护

不修改店铺阶段 module 或采集调用链。remaining_work 仍由 pending 派生。
完整榜单有可尝试工作时 next_action=detail；成功或跳过逐项减少剩余工作；无剩余工作时 next_action=none。
加强原有完整榜单 → 详情 deny 中断 → 同轮续跑连续测试：续跑不重扫榜单，两个商品分别成功和跳过，
remaining_work 为 2 → 1 → 0，动作 detail → detail → none；最终 handled=2，但成功商品/SKU 只有 1。
失败耗尽测试同时证明详情阶段完成不表示所有商品成功或跳过。

## 行为测试与验收

沿用真实临时 SQLite 夹具和已确认行为接缝；不靠私有常量/源码文本断言证明正确性。
保留既有快照读取方法作为是否重复追加的持久化证据；不 mock 数据库或阶段投影。
先增强成功、跳过测试，在旧实现上均通过；再做局部收束和逐场景回归。
连续用例初次扩展误用了不存在的配置字段，纠正为实际 max_attempts_per_page 后通过；这只是测试编写错误，不是生产故障。
未发现配置好的 mypy/pyright 等类型检查入口，不新增类型检查框架，不以语法检查冒充类型检查。

| 验收场景 | 行为证据 |
| --- | --- |
| 成功处理、退出 pending、阻止跳过 | test_success_clears_failure_and_counts |
| 跳过处理、非库存证据、幂等 | test_skipped_offer_counts_as_handled_and_is_not_a_failure；test_skip_is_not_inventory_evidence |
| 未到上限/达到上限失败，阶段无工作 ≠ 已处理 | test_pending_offers_and_attempts；test_failed_observation_is_not_dedupe_evidence |
| 多次快照、重复榜单、多 SKU | test_offer_counts_are_per_offer_not_per_sku |
| 跨轮次/店铺/商品 | test_handled_results_are_isolated_by_round_shop_and_offer（成功、跳过两组） |
| 成功/跳过孤儿计数排除与守卫 | test_round_tally_counts_every_question_in_one_place |
| 候选 06 连续续跑 | test_complete_listing_survives_detail_stop_and_resume_skips_listing；既有 views 阶段展示测试 |

单文件测试：test_db 75、test_p1 31、test_dedupe 10、test_views 27，合计 143，均 OK。
日志目录：`F:\AI\projects\bestseller\.scratch\logs\tickets\handled-predicate\01\focused`。

### 隔离分段全量

隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\handled-predicate\01\20261008-handled-predicate-final-2\bestseller`。
独立无硬链接 clone，来源为上述基线，只补入本任务六个文件；审查快照提交 `c8ffe02ad5552389ca649570782542bd2dd3e377`。
配置从已追踪的 config/config.example.toml、analysis.example.toml、shops.example.csv 创建，COS 桶留空，
不复制真配置、数据库或 profile。数据库、缓存、profile、交换库及其他写入路径核验在副本内。
首次 clone 遇到源 .git 的所有者校验拒绝，改用精确命令级 safe.directory 重试，不修改全局 Git 设置。

从副本根执行（全量仅此一趟，不加 -f，不改段集合）：

```text
python tools/run_full_suite.py --log-dir F:\AI\projects\bestseller\.scratch\logs\tickets\handled-predicate\01\20261008-handled-predicate-final-2
```

summary.txt 原文（同一行同时含合计、对账和结论）：

```text
合计 1388 项（预期 1388，计数对账一致），总耗时 634.8s，段结果 7/7 段 OK，结论 OK
```

逐段：exchange 65、judgment_set 50、plan_step 54、export 30、git_channel 17、shops_sync 17、其余段 1155，均 OK。
退出码 0。加载口径 1388 与段计数和 1388 一致，重段 233 + 其余段 1155，7 段集合覆盖全量。
summary 与七段日志绝对目录：`F:\AI\projects\bestseller\.scratch\logs\tickets\handled-predicate\01\20261008-handled-predicate-final-2`。
收尾文档更新前已核对代码/测试与隔离快照字节及 SHA-256 一致；收尾仅补文档和过程记录。

### 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`。
- 永久保留（不随副本清理）：主仓上述 summary/逐段日志、focused 单文件日志、审查报告，及 `.scratch/handled-predicate/` 的 grill/spec/ticket/隔离 manifest 和规范输入快照。
- 可删除候选：上述完整隔离副本（只读审查快照提交不向主仓合并）；需用户另行授权并按规范核验后才能删。
- 首次失败 clone 没有完整副本，尝试路径为 `F:\AI\projects\bestseller\.scratch\sandboxes\handled-predicate\01\20261008-handled-predicate-final\bestseller`；实际残留是该路径的运行层父目录 `F:\AI\projects\bestseller\.scratch\sandboxes\handled-predicate\01\20261008-handled-predicate-final`（空目录）；纳入本票清单，不自动清理。
- 实际清理：未删除任何隔离副本或失败现场；两份审查报告和两个已认领任务目录已点名移入主仓票证据 review 目录，归档哈希一致。
- 本机证据不进 Git；正式文档中的结论随提交保留，不宣称 `.scratch` 已有异地备份。

## 双轴审查

固定基线 `75b13294b20332c24cdb22634146301813d1a712`，只读审查快照 `c8ffe02ad5552389ca649570782542bd2dd3e377`。
两个最小上下文子代理在干净隔离副本中并行审查，分别交付报告；未修改仓库、暂存、提交或派发其他代理。

### Standards

在已核对的差异与成文规范范围内：硬违反 0，需整改的 baseline smell 0；最严重项：无。
核对单一内部定义、绑定参数、局部修改、真实行为测试、领域词义、ADR 和文档落点。
报告注明工具读取中断边界：最后补读 SQL 完整上下文未完成，不声称完整调用链审查或独立执行测试。

### Spec

指定快照中发现 0 项；最严重项：无。完整读取 spec、工单与六文件差异，未发现缺失、错误实现或范围扩大。
核对三处状态条件、作用域/榜单/孤儿集合、失败上限与库存区别、幂等、多 SKU 及同轮连续续跑。
追加读取夹具、阶段实现和日志的命令被中断；不声称这些追加输入已核验或独立复跑测试。

两轴分别报告，不合并或重排。主 agent 已读取实际 summary 和单文件日志，补读 pending/skip 的完整 SQL 上下文，
核验 CONTEXT、生产代码及两个测试文件与已测试快照的 SHA-256 一致；本节与 ADR 收尾仅补记录，无实现或测试修正。
原始两报告、自包含任务输入及逐文件归档 SHA-256 清单保留在
`F:\AI\projects\bestseller\.scratch\logs\tickets\handled-predicate\01\review`。
交接材料点名移出 `work/tasks` 与 `work/results`，未删除任何隔离副本或失败现场；其他交接材料不触碰。

轴内汇总：Standards 硬违反 0 / 需整改气味 0（最严重：无）；Spec 发现 0（最严重：无）。

## 范围与交付归属

生产改动仅在数据库 module；不涉及 schema 迁移、店铺阶段改写、销量分析、详情机会账本（候选 05）、
时间注入（候选 07）或 #elapsed / #prep-owner / #tx-seam。ADR-0047 只更新本候选，其他挂起项不变。
本题可逆的局部 SQL 常量收束不另建 ADR；领域词义记录在 CONTEXT。
本任务之外的 AGENTS、工单规范和目录盘点等文件不修改、不暂存、不提交。
