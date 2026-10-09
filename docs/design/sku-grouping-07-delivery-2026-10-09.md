# SKU 分组票07交付记录（2026-10-09）

## 范围与行为

仅票07；固定审查起点 `8e84f4c46198fbe5475c389141264f020342f9f6`，沿用共享分支 `codex/sku-grouping` 和 02 功能树。

- 新增按调用粒度持久化的 SKU 模型工作记录（`bestseller_monitor/sku_lifecycle.py`，`ModelWork`）：普通图文判断与候选推荐共享完成状态、业务调用计数、缓存与失败原因接缝；每条任务按「分支＋完整语境摘要」落键，状态区分缺少证据／需要配置／模型不可用／调用失败／已停止／图文证据不确定／已完成但无法推荐唯一组／已完成确定非同款／已完成有效同款，携带原因文案与 completed／retryable 标记。
- 模型未配置、不可用、调用失败与停止都不阻断有效编码规则和已有有效人工决定；未完成成员保留有效历史归属或暂时独立并标明原因；已完成但无法唯一推荐是成功结果，不重试、不误当调用失败；停止后的迟到结果不落盘、不应用。
- 选择性重试（`retry_matching`／`retry_matching_job` 的 SKU 分支）：重冻结当前证据、只回放当前人工约束，仅补未完成调用；有效已完成判断、推荐及成功不确定不重复调用；SKU 名称／图片／来源／候选成员／候选组／规则／分析区间／人工排除与绑定变化后旧结果不命中，只为当前合法语境调用。
- 历史推荐审计保留：新增候选失败／未配置／停止时，历史有效归属不被撤销；复用经完整旧候选语境与当前任务签名核验的推荐理由、候选与来源，不请求新模型调用。
- 缺图任务在补齐证据后可经「选择性重试」入口重新检查（recheckable），不重复调用已完成结果。
- 页面「SKU 模型任务」行展示已完成／未完成、逐状态原因与计数、业务调用与复用计数、可重试／缺证据提示与选择性重试入口；停止说明文案按 SKU／旧商品分流，保持旧商品行为。
- 未新增相似度百分比或伪置信度；不改同款门槛、召回预算与人工优先级；不进入旧共享格式，不实现票08。

## 未交付

票08跨机共享与新格式兼容仍未开放（`LIMITATIONS` 明示「SKU 跨机共享尚未开放」）。不修改父规格，不进行付费模型质量评测，不合并 main、不推送。

## 验证与证据

主接缝为公开 AnalysisService（真临时库存、草稿、人工决定与判断缓存），只在外部模型边界（`matching.request_json`）替换为可控制完成顺序、成功、失败、未配置与停止时机的替身；真实 headless Edge → HTTP → 服务覆盖关键页面路径。不 mock 整个分析服务，不以付费模型或网络为验收前提。

- TDD 红绿与切片日志：`F:\AI\projects\bestseller\.scratch\logs\tickets\sku-grouping\07\20261009-sku07-01`（slice-03…18、red-*／green-*、fixed-*、b-*-red/green、historical-safety-red、acceptance-map.md）。
- 收尾定向复跑（功能树根，日志同目录 `closing-cleanup-*.log`）：test_sku_recovery **25 项**（20 服务＋5 浏览器）、test_sku_drafts 23、test_sku_matching 15、test_sku_recommendations 24、test_sku_codes 22、test_stop_matching_ui 6，全绿。C 轮审查代理独立复跑 recovery 25（含浏览器）、matching 15、recommendations 24、drafts 23，同样全绿。
- 浏览器截图 8 张：`F:\AI\projects\bestseller\.scratch\tool-output\sku-grouping\07\20261009-sku07-01`（均在 B 轮整改后重跑生成）；人工抽查 pair-partial-failure、recommendation-stopped-partial、needs-config-and-evidence、historical-recommendation-new-failure、pair-retry-legal-pending 五张，与公开服务快照逐项一致。
- `python -m compileall -q bestseller_monitor` 与 `git diff --check` 通过（清理提交后）；未配置静态类型检查器，不把 compileall 称为类型检查。
- 安全隔离副本分段全量：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\07\full-20261009-sku07-03\bestseller`（来源提交 `26c43053dff21df72b35156f992886058e6f3aa2`）；summary 原文：**合计 1631 项（预期 1631，计数对账一致），总耗时 763.4s，段结果 7/7 段 OK，结论 OK**；进程 exit=0，不加 -f、无提前停止、仅一次收尾全量。副本由独立 `git clone --no-hardlinks` 自功能树建立，341 个跟踪文件逐字节核对（Git 检出换行差异点名恢复来源字节）；安全配置仅取票06隔离副本三份文件，config-safety 核验全部写路径在副本内；未复制真实数据库、登录态与虚拟环境。首跑准备脚本因 PowerShell 5.1 以 GB2312 解码 git 的 UTF-8 路径输出而中断（clone 已成功），改用 Python 完整字节核对脚本补齐，诊断与脚本留档在同目录 `preparation-errors.txt`／`prepare-encoding-probe.ps1`。

### Standards

固定基线 `8e84f4c`。初审（67333fa）：文档硬违例 0，Fowler 判断性异味 2（analysis.py 人工重载流程重复；sku_matching.py 扫描 `work.tasks` 的 Feature Envy）→ a387d27 整改（`_apply_sku_model_work` 收口、`failure_reason` 窄接口）。B 轮复审（a387d27）：硬违例 0、异味 0。C 轮最终验证（a12fcfa）：增量硬违例 0、判断性异味 3（均轻微：推荐记录字段形状重复、signature 表达式双份、页面停止文案静态／JS 双份）＋1 处 import 位置注记 → 收尾清理提交 `26c4305`（纯重构，行为不变）＋去除文件尾空行；清理后定向复跑全绿，全量在清理后代码上实测。

### Spec

同一固定基线。初审（67333fa）：P1 新候选失败撤销仍有效的历史图文归属；P2 仅缺图草稿缺少补证据后的继续入口 → a387d27 整改（`valid_history` 保留合法历史归属；`recheckable`＋页面重新检查入口）。B 轮复审（a387d27）：1 项 P2（保住历史推荐成员但丢失推荐理由、候选与来源）→ a12fcfa 整改（`preserve_history` 以完整旧候选语境与当前任务签名核验复用审计，0 新调用；补服务与真实浏览器回归，修复前红灯留档）。C 轮最终验证（a12fcfa）：B 轮 P2 确认真正修复（服务与页面用例真实走分支、`call` 不可达、修复前 `b-audit-red.log` 红）；本轴最终剩余 **0 项 P、0 项范围膨胀、0 项实现错误**；仅 1 条非 P 覆盖建议（保留路径的「模型未配置」变体缺直接用例）→ 收尾补充该变体回归（含不重复外部调用断言），通过。

### 提交与证据位置

- `679bd166d82939eae36410cb9e42a79a4d6e3d8d`：票07实现、领域词条、公开服务／浏览器测试。
- `67333fa9e851b2b847702062aabf1fbe844b0db7`：草稿门控期望调整（重试入口开放、未确认结果保门控）。
- `a387d2799c074a4aa5f4793c6b056eaed2e97cdd`：初审双轴整改。
- `a12fcfa9ae88731e0bb1b0161324c20c5b0eca03`：B 轮 Spec 整改（历史推荐审计保留）；C 轮审查与全量的实测代码含此提交。
- `26c43053dff21df72b35156f992886058e6f3aa2`：C 轮判断性异味清理、行尾空行、（增补）未配置保留变体回归；为全量实测代码。
- 本交付文档收口为文档提交，实际 SHA 记录在主仓票07工单；不改变已审已测代码。
- 定向、红绿、验收映射及双轴原文（含 C 轮任务与报告、哈希清单）：`F:\AI\projects\bestseller\.scratch\logs\tickets\sku-grouping\07\20261009-sku07-01`（审查在 `reviews` 子目录）。
- 全量 summary、7 段日志、来源 SHA256 清单、配置核验与准备记录：`F:\AI\projects\bestseller\.scratch\logs\tickets\sku-grouping\07\full-20261009-sku07-03`。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`；仅点名更新票07工单与本票证据，不提交或还原其他会话改动。
- 共享功能树：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller`；保留供后票（08）使用，不删除、移动、重命名。
- 收尾全量副本：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\07\full-20261009-sku07-03\bestseller`；本票验收现场。
- 历史副本：`...\07\full-20261009-sku07-01\bestseller`（首跑 prepare 失败于非 ASCII 路径引用，未跑测试）、`...\07\full-20261009-sku07-02\bestseller`（a12fcfa 准备完成、未跑全量，被 -03 取代）。
- 永久保留：上述主仓证据（红绿日志、审查任务／报告／哈希、截图 8 张、本交付记录）；不随副本清理；`.scratch` 不进 Git，不表示异地备份。
- 可删除候选：-01／-02 两个未用于验收的副本（精确路径如上）；C 轮任务池 claimed 目录（`F:\AI\projects\bestseller\work\tasks\sku07_*_20261009_c.claimed`）。
- 暂留待确认：-03 全量副本作为本票验收现场保留；后续由用户确认留存／清理授权，实际清理前须重核独有材料与边界。
- 实际清理：未删除任何功能树、副本或任务池目录；仅本收尾会话在证据目录内生成自己的日志与脚本。

仅票07完成后停止；08未实施，父规格及其他票状态不修改，不合并 main、不推送。
