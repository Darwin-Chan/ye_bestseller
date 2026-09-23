# SKU 图线：实施工单总览

Approval: 设计已确认（2026-09-23 grill-with-docs 两轮问答 Q1–Q7 收口，用户当轮"其余按建议""都按建议"；共享理解总结经核对后进入 to-spec）
日期：2026-09-23
来源：[规格](spec.md)、[需求清单与问答记录](需求清单-2026-09-23.md)、实测证据 `.scratch/probe_sku_images.py` / `probe-result.json` / `probe-page.html` / `.scratch/sku-image-proof/`
状态：**六票全部 resolved（2026-09-24，本线收口）**：01 `7365a2a`；02 `6943400`＋整改 `cf2461d`；03 `3ca2924`＋整改 `27583cb`；04 `5b6aa10`＋整改 `0da825d`；05 `2c5163f`＋整改 `c969040`；06 文档修订（随本笔落库）。

## 拆分原则

- 竖切：每票切穿一条可演示行为（解析 → 写库 → 重试 → 出门 → 收下 → 文档），不按"先全套解析再全套导出"横切。
- 沿用既有缝：真 HTML 片段（parse）、真库＋打桩图片通道（采集写入）、真 git 本地裸库＋ImageStore 替身（交换）；不新造脚手架。
- 与在飞线不碰同一文件：界面反馈批与匹配进度遮罩线在分析侧页面与判断流程，本线在采集与交换数据面；共享工作区提交纪律照旧（路径限定、新文件与自留 hunk 用私有索引、一律不 amend）。
- 工单正文引用父规格条目；不固定未来代码文件名与私有函数签名（表名 `sku_image_versions` 是规格级决定，沿用）。

## 编号、交付与直接依赖

| 编号 | 工单 | 直接依赖 | 完成后可演示的行为 |
| --- | --- | --- | --- |
| 01 | [图文同源：解析每个 SKU 的图地址并进观测载荷](issues/01-sku-image-parse-and-payload.md) | 无 | 一份真 HTML 解析出每个 SKU 的图地址；观测载荷行行带图地址；空值情形不报错 |
| 02 | [流水与代填：SKU 图随观测落库](issues/02-sku-image-ledger-and-fill.md) | 票 01 | 观测一次后库里出现 SKU 图流水（来源三态：专属图/主图代填/无图）；图失败不挡库存 |
| 03 | [补得回来：SKU 图失败重试走既有通道](issues/03-sku-image-retry.md) | 票 02 | 按最新失败行重试，图补上、库存不动、原失败行保留 |
| 04 | [带图出门：周包带 SKU 图流水与清单（v2）](issues/04-export-sku-images.md) | 票 02 | 周包含新表与资产并集，清单含 SKU 图 key，COS 只传新增；周报含行数与失败待办 |
| 05 | [收得下旧包：汇总软容忍与拉图](issues/05-merge-sku-images.md) | 票 04 | 新机收旧包照收；收新包先拉图再插行、缺图不挡；端到端两端一致 |
| 06 | [文档修订：README 与交换集口径](issues/06-docs.md) | 票 05 | README 两图一节与程序行为逐句一致；ADR-0045 状态更新 |

## 实施进展

- 2026-09-23：规格与六票登记（本文件）。01 状态 `ready-for-agent`；02 待 01 解除 `blocked`；03、04 待 02（03 与 04 可并行）；05 待 04；06 待 05。
- 2026-09-23：**票 01 落地**（`7365a2a`，审查后定稿）——载荷行键 `sku_image_url`；解析缝 8 项＋载荷缝 2 项，实页对账 14/14，隔离副本全量 1309 项全过（`.scratch/logs/tickets/sku-images/01/full-suite-isolated.log`）。票 02 解除 `blocked` → `ready-for-agent`，可直接开工。
- 2026-09-24：**票 02 落地**（`6943400`）——新表 `sku_image_versions`（SCHEMA 建表＋日期索引，去重索引走迁移条目 `sku_image_dedupe_index`，建不动跳过）；来源三态 `db.SKU_IMAGE_OWN/FILLED/NONE`；下载点 `detail._attach_sku_image_evidence`（逐行 `acquire(row["sku_image_url"])`）、写库点 `Database._write_sku_image_versions`（`submit_inventory_snapshot` 同事务，字节入 `product_image_assets` 池）。采集写入缝 9 项新用例＋1 项扩展（test_detail 4＋test_db 5），全量 1324 项本票相关全绿（3 项失败均在别线：2 项高负载抖动隔离复跑绿、1 项属在飞页面线未提交用例，日志 `.scratch/logs/tickets/sku-images/02/full-suite.log`）。票 03、04 解除 `blocked` → `ready-for-agent`（可并行）。
- 2026-09-24：**票 02 审查整改**（`cf2461d`，审查后定稿）——两轴子代理（只读）0 硬违规、0 缺陷；整改三则：图证据事务前校验（坏证据当场 `ValueError`，不落写库阶段打回整单）＋1 项用例、新用例复用 `helpers.product_picture`、补「无 sku_id 行流水与快照共用兜底编号」用例。两条判断题不改并留档（迁移函数与版本表同形的仓库先例、资产池 INSERT 旧习）。整改后复核全量 1331 项（`e0af300` 时点，日志 `.scratch/logs/tickets/sku-images/02/full-suite-after-review.log`）：test_db 67、test_detail 25 本票相关全绿；7 项红（e2e 2 / bestseller_ranking 3 / offline_report 2）全在分析页一线——另一会话 `e0af300`「分析结果独立成屏」在飞、页面用例未同步，已做隔离对照（同树回退本票两个提交，同一条用例照红），与本票无关。
- 2026-09-24：**票 03 落地**（`3ca2924`）——`db.Database.retry_sku_image` 照 `retry_product_image` 逐条对镜：只认失败行（`image_error` 非空）、BEGIN IMMEDIATE 内按 (店铺、商品、SKU) 找最新行（非最新拒「已有更新 SKU 图版本，请重试最新失败版本」）；成功按重试当下新开一行（地址照抄原失败行、来源=专属图）、字节 `INSERT OR IGNORE` 入 `product_image_assets` 池，同秒同结果按 `SKU_IMAGE_DEDUPE_KEY` 回查指回已有行。CLI `product_images` 加 `--retry-sku-image`（与 `--retry-version` 互斥、必给一个），旧用法不变。采集写入缝 5 条新用例，test_db 72 项全过；隔离副本全量（**计数对账一致 1336 == 1336、段结果 6/7 段 OK、结论 FAILED——唯一红因＝分析页线 3 项已知红**；`test_bestseller_ranking.RankingBrowserTests`，同副本去掉本补丁后三红照旧＝控制组。旧措辞运行器的合计行原文与「为何不照抄那半句」见票 03 验收记录，`6c7eb8e` 已修；日志 `.scratch/logs/tickets/sku-images/03/full-suite/`）。本票无下游要解除（05 的依赖是 04，06 待 05）。
- 2026-09-24：**票 04 落地**（`5b6aa10`）——导出侧四条：`EXCHANGE_TABLES` 加 `sku_image_versions`（列照本机表、`id` 不进包、按 `observed_date` 取周窗口，身份过滤照观测表模板，版本表先例无主键）；`product_image_assets` 取数改「本周版本行 ∪ 本周 SKU 图流水行」两个 arm、owned 各自内联（`{owned}` 占位在这张表上退役）；`EXPORT_FORMAT_VERSION` v1→v2（上一版格式的包读不成摘要→同数据也重发；同内容重跑仍不重发）；周报行数行加 `· SKU 图 N`、失败流水进待办（点到 `product_images --retry-sku-image`，空图与代填不算失败；计数 `export.failed_sku_images` 从包内流水行来）。交换缝 12 处（test_export 新增 5＋扩展 3、test_exchange 新增 3＋扩展 1）。隔离副本全量（对账行原文：`合计 1344 项（预期 1344），总耗时 1169.4s，对账不一致，结论 FAILED`，基线 `3ca2924`＋本票改动）——「对账不一致」是运行器措辞 bug（`6c7eb8e` 已修）：计数实际一致、**段结果 6/7 段 OK**，唯一红因＝分析页线已知红（`test_bestseller_ranking.RankingBrowserTests`，同副本撤掉本票改动照红）；日志 `.scratch/logs/tickets/sku-images/04/on-03/`（早一轮基线 `a10d749`、1339 项、计数同样一致、同样 3 项红，在上一级目录）。**过渡态记在票面 Comments**：merge 侧未跟（v1 旧包报「包里没有 `sku_image_versions`」、新包流水行读入但不合并），属票 05 票面。票 05 解除 `blocked` → `ready-for-agent`，06 待 05。
- 2026-09-24：**票 04 审查整改**（`0da825d`，审查后定稿）——两轴子代理（只读，固定点 `6c7eb8e...5b6aa10`）：Standards 已改两条（待办里打印的重试命令缺必给的 `--database`——照抄会 argparse 报错；`_PackageTable` docstring 的 `{owned}` 占位补上资产表这个例外）、一条判断题已改（两文件各一份直插夹具并进 `tests/helpers.py.insert_sku_image`，与 `insert_inventory_rows` 同规、列集一处，票 05 的汇总夹具也要用）；两条判断题按票面不改并留档（待办只报条数不给行编号、计数是「本周失败行数」不是「可重试条数」）；Spec 轴 0 缺失／0 越界／0 疑误。整改后复核全量（对账行原文：`合计 1344 项（预期 1344，计数对账一致），总耗时 1068.3s，段结果 6/7 段 OK，结论 FAILED`），3 项红仍是分析页线那三条（日志 `.scratch/logs/tickets/sku-images/04/after-review/`）。分析页线随后 `9974267` 把那三条红清零，在最新 trunk（含本票两笔）再复核一趟：`合计 1351 项（预期 1351，计数对账一致），总耗时 856.7s，段结果 7/7 段 OK，结论 OK`（856.7s 为并发在场口径的保守值——那趟与另一会话的重段全量、本机游戏有时间重叠；日志 `.scratch/logs/tickets/sku-images/04/final-head/`）。
- 2026-09-24：**票 03 审查整改**（`27583cb`，审查后定稿）——两轴子代理（只读，固定点 `a10d749`）：Standards 1 条硬违规＝验收记录没照抄运行器的合计行（已补记**正确口径**并注明「对账不一致」那半句是修复前措辞，票 03＋本文件）；判断题一律不改并留档——与主图先例同形（实现 35 行、9 列 INSERT、`(shop_key, offer_id, sku_id)` 成组）、守卫认「最新行」而非「最新失败行」（票面与主图同规，残余语义写进票 03）、先下载后守卫、返回键 `retried_sku_image` 照票面 `retried_*` 形状。整改只动测试侧：5 例改吃本类既有 `_submit_offer` 夹具（`+26/−56`）。整改后 test_db 72 项全绿、隔离副本复核全量（计数对账一致 1336 == 1336、6/7 段 OK、结论 FAILED——唯一红因＝同 3 项分析页线已知红；日志 `.scratch/logs/tickets/sku-images/03/full-suite-after-review/`）。
- 2026-09-24：**票 05 落地**（`2c5163f`）——汇总侧两条：①软容忍——`export.OPTIONAL_TABLES`
  （「v2 起新增、读 v1 包可缺席」的表名集，包格式的家）由 `read_package_meta`（`rows_` 键缺席
  按 0）与 `merge._read_package_rows`（缺表给空列表）共用，老六表缺键/缺表维持当场报错；
  `package_digest` 不动（上一版格式的包同数据也重发的口径不变）。②新表合并——
  `merge._merge_sku_image_versions` 照观测表一侧先查后写（判别式自 `db.SKU_IMAGE_DEDUPE_KEY`、
  已存在即跳过、新行计入 inserted），不进 claim 裁决与两张本机账；拉图链路零改动（包资产表本就
  是「版本行 ∪ SKU 图流水行」的并集），用例钉住先拉图再插行／已有不重拉／哈希对不上不落库／
  缺图不挡／补传到齐。`tools/acceptance_check.py` 两处（判断项）：收敛对照加
  `sku_image_versions`（除 `id`）、冷启动引用集改两表并集（`_CORE_TABLES` 按票面不加）。
  交换缝 12 处（test_merge 32→44）＋验收工具缝 3 处（test_acceptance_check 30→33）；隔离副本
  （基线 `e250f86`＋本票改动）分段全量 **`合计 1366 项（预期 1366，计数对账一致），总耗时
  781.4s，段结果 7/7 段 OK，结论 OK`**（日志 `.scratch/logs/tickets/sku-images/05/full-suite/`）；
  审查整改（`c969040`：汇总夹具并进 `helpers.insert_sku_image`＋旧包夹具补 `format_version`）后
  复跑 **`合计 1366 项（预期 1366，计数对账一致），总耗时 772.1s，段结果 7/7 段 OK，结论 OK`**
  （日志 `05/final/`）。票 06 解除 `blocked` → `ready-for-agent`。
- 2026-09-24：**票 06 落地（本线收口，无下游要解除）**——文档对齐实现（只动文档、程序行为零改动）：
  README「商品主图资产」扩成「商品主图与 SKU 图」（主图口径原文保留；SKU 图同规、代填与来源三态、
  重试入口含可照抄示例；「数据落在哪」补字节池与两张观测流水的落点与量级）；ops 文档 54 行改七表
  ＋`v2`＋兼容方向、182 行改七表并加 `sku_image_versions`，另按判断项加 §6 新表口径与 §8 验收半句
  （同段「五张表」→六张、54 行指错节的引用改指 §6）；「六表」在现行文档清零；CONTEXT.md 两条词条
  与交换集七表复核无漂移、随本票落库；ADR-0045 转「已接受并实现」并补各票提交号，ADR-0039 状态行
  加后注指路。隔离副本（基线 `c969040`＋本票改动）分段全量 **`合计 1366 项（预期 1366，计数对账
  一致），总耗时 759.8s，段结果 7/7 段 OK，结论 OK`**（日志
  `.scratch/logs/tickets/sku-images/06/full-suite/`）；trunk 随后落到 `a3f0e32`（世界模板试点，
  只动 `test_exchange` 夹具、+4 项用例），在 `a3f0e32`＋本票改动上再跑一趟 **`合计 1370 项
  （预期 1370，计数对账一致），总耗时 744.0s，段结果 7/7 段 OK，结论 OK`**（日志
  `.scratch/logs/tickets/sku-images/06/on-a3f0e32/`）。判断项与留笔见票 06 Comments。
