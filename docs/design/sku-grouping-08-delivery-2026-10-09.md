# SKU 分组票08交付记录（2026-10-09）

## 范围与行为

仅票08；固定审查起点 `26349a338da3435a3670569773218778639fe931`（票07 收口），沿用共享分支 `codex/sku-grouping` 和 02 功能树。

- 新模块 `bestseller_monitor/sku_sharing.py`：SKU 判断集走 `judged-<机器>` 库的新文件 `data/sku-judgments.db.gz`（口径版本 `sku-v1`），含五表——`sku_judgments`（图文判断）、`sku_recommendations`（候选推荐）与三张人工决定表（`sku_manual_groups` 携成员身份＋证据版本＋组 ID、`sku_excluded_skus`、`sku_excluded_pairs`）＋ `sku_meta`；包身份＝逐表内容摘要。旧商品级包 `data/judgments.db.gz` 一字不动。
- 发布：与远端 HEAD 那份同内容不写不提交；远端是更新的格式版本时明确拒绝覆盖；五表全空不发布（可读说明）。
- 收取：按内容摘要幂等（`sku_imports` 导入账）——先并人工决定进本机 SKU 账本、后导判断与推荐（逐行 `INSERT OR IGNORE`，本机已有行不动）；未知版本明确拒绝、一行不导；坏包/未发布/未克隆只记那一家。
- 本机 SKU 账本（草稿库新表，产品账本同层分表）：保存时写入（在场行按当前证据版本改写、缺席行保留、来源随行）；收取时并入（同一条跳过、同源旧话让位、异源矛盾记 `sku_decision_conflicts` 两边都不动）。
- 回放（`apply_shared`）：本地草稿回放之后、编码规则之前——本地人工主张永不覆盖；同族行（含先在场的子集先应用、成员后到齐，来源机的同源扩员）补员进同一组；成员证据版本全对才保留 `confirmed`，对不上整组退回未确认并标注「与共享决定时的证据不一致」；组 ID 跨机随行（推荐语境靠它命中）。
- 跨机复用仍走既有缓存键与签名：收进来的判断/推荐只在本地重算出相同对键/语境摘要且签名一致时命中（实测双机业务调用 0）；应用前照旧重查人工合法性。
- 交换台 `run_once` 在既有判断集发布/收取处增跑 SKU 通道并在周报第三节记账；退出码沿用两档语义（拉不动=需人看、明确拒绝/坏包=没做成事）。
- 版本与部署：路径隔离＝已部署旧程序对新文件不读不写（真实旧代码实测＋逐字节锚定）；升级顺序任意、旧通道新旧互通不受影响。`LIMITATIONS` 清空，CONTEXT 更新为 08 已开放（03 与 08 均完成，整套方案具备交付条件）。

## 未交付

- SKU 冲突账只记不展示：`sku_decision_conflicts` 与周报计数为可见面，SKU 确认页的冲突展示面留后续（兼容说明里如实登记）。
- 无（八票至此全部实现；不合并 main、不推送）。
- 挂账（审查记录、不在本票范围）：商品账本 `_LedgerMerge.standalone` 的让位也在冲突判定之前（与 SKU 的 B1 同形顺序；可达性未证，留与商品账本下次触及一并核）；`export.py` 的 `pack_gzip` docstring 称商品判断集共用它、实际仍各写一份（该文件被逐字节冻结，解冻旧通道时修）。

## 验证与证据

- TDD 与定向复跑（功能树根）：`tests/test_sku_sharing.py` **49 项**（包/发布/收取/并入语义/保存写账本/双机端到端/旧代码共存/真实浏览器），SKU 家族 test_sku_codes 22、test_sku_drafts 23、test_sku_grouping 4、test_sku_matching 15、test_sku_ranking 9、test_sku_recommendations 24、test_sku_recovery 25、test_sku_switch_note 7，`test_exchange.py -k SkuJudgmentSetTests` 3 项，test_judgment_set 50 项——全绿。
- 浏览器证据：`F:\AI\projects\bestseller\.scratch\tool-output\sku-grouping\08\20261009-sku08-01\collected-recommendation-on-page.png`（收取后页面：「本次业务调用 0，复用 3」，推荐理由、其他候选、待确认状态齐全）。
- 定向日志：`.scratch\logs\tickets\sku-grouping\08\20261009-sku08-01\`（red-*/green-*/run-once-* 逐切片留档）。
- 隔离副本分段全量：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\08\full-20261009-sku08-01\bestseller`（来源 `0db8958`，344 个跟踪文件逐字节核对、写路径全在副本内）；summary 原文：**合计 1683 项（预期 1683，计数对账一致），总耗时 871.8s，段结果 7/7 段 OK，结论 OK**；进程 exit=0，不加 -f、无提前停止（较票07 的 1631 项增 52 = 本票 49 项新用例 + 交换台 3 项）。准备首跑连中两坑（票号目录残留 07、PS 5.1 解码 git UTF-8 路径）后改用 Python 完整字节核对就地补齐，记录在同目录 `preparation-errors.txt`。
- T01–T30 覆盖：各票工单的父规格映射并集覆盖 T01–T30 全部（T21 由本票覆盖；T20 由 01/08 覆盖），详细映射见本工单收口段。

## Standards

A 轮（a798900）：文档硬违例 0；Fowler 判断性异味 7（最大项 sku_sharing 与 judgment_set 机器件各写一份——已以模块文档说明「有意分家」，纯工具件共用 export 的 `pack_gzip`；其余：冲突写入/账本改写/来源助手/夹具重复已收口）；行为缺陷 1（`_sku_source` 二元排除行把第二端当来源——已修＋补回归）；文档失真 2（CONTEXT 票06 词条两处——已同步）；小疵 2（未用导入、docstring——已修）。
B 轮（adb1d3b）：0 硬违例；残留小件 2＋微瑕 1（冲突写入复用未彻底、类前空行、docstring 与实现口径）——随 762c4af 一并收口；「机器件有意各写一份」判**可接受**（judgment_set 受与 main 逐字节一致的部署冻结，解重须动冻结模块）。C 轮（762c4af）：0 硬违例，增量三块与 B 轮残留逐项成立；1 项判断——第二条 B1 回归无判别力（让位点在常规流程不可达，旧码也绿）——已按预置行直写重造共存态使其在旧码上必红（0db8958）。终态：硬违例 0、可报告异味 0（有意保留项在案）。

## Spec

A 轮（a798900）：十条验收代码层全部成立（缺失 0、部分 0、范围膨胀 0）；实现错误 1——`apply_shared` 同族补员被误判矛盾（F1：先在场的子集先应用/成员后到齐时不补员且记假冲突）——已整改（同族判定放宽到同源宽/窄行，补员进同一组；同源旧窄行结算不冲突）并补两组回归（同族补员、证据变化保留归属退确认）。
B 轮（adb1d3b）：F1 确认真正修复（运行验证）；新发现并入期实现错误 B1——同源同集让位抢在冲突判定之前，矛盾时共享行会被先删、永久丢失（复现：部分在场应用并保存后重收新包）——762c4af 整改：三处让位一律移到确认无矛盾之后、矛盾只记不动两边，补两条回归（组与排除身份的「矛盾自更新不许先删」）。C 轮（762c4af）：以临时脚本重跑 B1 同型复现证实修复成立（账本不丢 m1 行、矛盾照记、两边不动、其后 F1 扩员仍发生）；`26349a3...HEAD` 完整差异无新增缺失/部分/范围膨胀/实现错误，A/B 全部发现收口；唯一观察（排除侧回归不钉整改行）已随 0db8958 强化。终态：缺失 0、部分 0、范围膨胀 0、实现错误 0。

## 提交与证据位置

- `a798900`：票08实现、交换台集成、测试与领域词条。
- `adb1d3b`：A 轮双轴整改（F1、来源助手、共用件、拆分、回归与文本）。
- `762c4af`：B 轮整改（B1 让位重排、冲突写入字段回收、docstring/空行、两条回归）。
- `0db8958`：排除侧 B1 回归强化（预置行造共存态）；**为全量实测代码**。
- 本交付文档与兼容说明为收口文档提交，实际 SHA 记录在主仓票08工单；不改变已审已测代码。
- 双轴原文与任务（A/B/C）：`.scratch\logs\tickets\sku-grouping\08\20261009-sku08-01\reviews\`。
- 兼容说明：功能树 `docs/design/sku-cross-machine-compat-2026-10-09.md`。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`；仅点名更新本工单与本票证据，不提交或还原其他会话改动。
- 实施功能树：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller`；`codex/sku-grouping` 保留（共享行历史）。
- 收尾全量副本：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\08\full-20261009-sku08-01\bestseller`；本票验收现场。
- 历史残留：`...\sandboxes\sku-grouping\07\full-20261009-sku08-01\bestseller`（首跑路径写错落进 07 目录的克隆，未核验未跑测试；对应 `...\logs\tickets\sku-grouping\07\full-20261009-sku08-01\` 空证据目录）——属本票误落产物，保留未删。
- 永久保留：主仓证据目录（设计记录、逐切片红绿日志、A/B/C 三任务与六报告、截图 1 张、准备记录与失败诊断）、兼容说明与交付文档；`.scratch` 不进 Git，不表示异地备份。
- 可删除候选：上述 07 目录下的误落克隆与空证据目录；两次失败准备留在 08 目录内的临时中间物（如有）。仅为候选，不构成删除授权。
- 暂留待确认：`...\08\full-20261009-sku08-01\bestseller` 作为本票验收现场保留；后续由用户确认留存／清理授权，实际清理前须重核独有材料与边界。
- 实际清理：本轮未删除任何功能树、副本或目录。
