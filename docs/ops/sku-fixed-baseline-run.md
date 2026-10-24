# 固定配置 SKU 基线运行工具

2026-10-24 第二步交付。工具与离线验证已实现；真实输入接入待完成，真实模型尚未运行。设计依据见 [第二步决策](../design/sku-fixed-baseline-run-decisions-2026-10-24.md)，第一步事实见 [原样本运行条件核验](../research/SKU样本运行条件核验-2026-10-24.md) 和 [近期正式数据核验](../research/SKU近期正式数据运行条件核验-2026-10-24.md)。本工具输出程序结论，不能作为人工真值。

## 入口与生产复用范围

在仓库根目录使用 Python 3.11 或以上版本（需 Pillow）：

```powershell
python tools/run_sku_baseline.py --help
```

生产分析入口在 `bestseller_monitor/analysis.py` 的 SKU 路径：恢复人工约束、应用编码、执行 `sku_matching.apply`。本工具显式构造研究快照，复用 `sku_codes.code_evidence/apply_codes`、`sku_grouping` 的恢复和刷新、`matching.scored_pairs/ImageJudge`、`sku_lifecycle.ModelWork`、`sku_recommendations` 和生产组装逻辑。同码优先、异码默认分开、人工例外和无码不能桥接异码沿用生产规则。

生产 `ModelUsage` 能记录调用及部分 token，但没有研究所需的逐请求费用账，推荐还会计入 compare。本工具在 HTTP 边界记录 verify、judgment、recommendation、caption；原生产函数继续执行。离线模式仅替换 HTTP 传输，不替换判断、推荐、解析或组装。研究工具临时安装进程内观察钩子，**必须在专属进程运行**，不要从正在工作的 GUI 或分析服务内调用。

仅解析 analysis TOML 的 `[matching]`、`[matching.model]`、可选 `[matching.vision]`，忽略运行数据库及缓存路径；每集缓存固定在本次研究目录。默认 `precheck`；只有显式 `live` 模式能够访问真实服务。工具不自动读取本地草稿、共享人工账本或历史模型缓存。

配置按输入 TOML 冻结，未增加调参入口。第三步必须使用经确认的既有生产配置：先前调查为 direct、deepseek-flash、concurrency=2、candidates=6、min_score=4、timeout=30；这些调查值不能代替执行前核对。请求沿用生产 temperature=0、JSON 格式，未追加 max_tokens；视觉核验最多两次，研究层不添加自动重试。保留 concurrency 配置不改变生产逐对执行顺序。

## 可直接运行的合成演示

`tools/examples/sku_baseline/` 全部为合成夹具，包含一集两成员、一张生成的红色图片、固定模拟响应及 TEST 价格。示例日期不是真实研究日期，测试模型和价格不可用于真实运行。

```powershell
python tools/run_sku_baseline.py --manifest tools/examples/sku_baseline/manifest.json --config tools/examples/sku_baseline/analysis.toml --run-id demo-precheck
python tools/run_sku_baseline.py --manifest tools/examples/sku_baseline/manifest.json --config tools/examples/sku_baseline/analysis.toml --mode offline --fixtures tools/examples/sku_baseline/responses.json --pricing tools/examples/sku_baseline/pricing.json --max-calls 10 --budget 1 --run-id demo-offline
```

预检查只做校验、人工约束和编码归组，不运行模型阶段。预期退出码为 3；离线演示执行真实视觉核验及判断解析，模拟两次 HTTP 响应，将两成员归为一组，预期退出码为 0。重复使用 run-id 会被拒绝，改用新 ID 或在输入完全一致时显式 `--resume`。示例 verify 响应自动读取生产随机色块挑战，其他响应来自 fixture。

退出码：0 表示本次每集均按工具口径完成；2 表示输入、配置、输出覆盖、锁或续跑指纹校验失败；3 表示至少一集有未完成原因。CLI 输出 report JSON；每集原因在 `sets/<id>/result.json`。`--output-root` 可指定研究产物根，默认主仓 `.scratch/tool-output/sku-fixed-baseline-run/`。

## 真实输入契约与第一步交接

manifest 为 UTF-8 JSON，顶层结构如下（占位符不是可运行数据）：

```json
{
  "schema": 1,
  "synthetic": false,
  "analysis": {"start": "已确认起始日", "end": "已确认结束日"},
  "provenance": "输入清单版本和来源引用",
  "sets": [{
    "id": "SET-001",
    "stratum": "1-10",
    "candidate_source": "与原包一致的来源",
    "members": []
  }]
}
```

分析日期为 ISO 日期且 start <= end；set id 为最多 80 位字母、数字、下划线、短横线，不可重复（忽略大小写）或使用 Windows 保留名。分层为 `1-10`、`11-25`、`26-50`；成员身份在集内唯一。每个 member 必须提供：

| 字段 | 要求 |
| --- | --- |
| shop_key / offer_id / sku_id | 非空字符串，保留真实身份 |
| product_name / raw_sku_name | 实际生产名称，不从展示值猜历史原值 |
| single_sku / name_source | true、false 或 null；null 必须有 evidence_note，不因候选集大小推断单规格 |
| matching_input | `{product_name, sku_name}`；single_sku=true 时 sku_name 为空，否则用原始 SKU 名 |
| observed_at / observed_date | 真实观测时间及中国时区日期；有图必须有精确 observed_at；日期相符且不得晚于 analysis.end |
| image_hash / image_source / image_error | 实际 SHA256；有图来源为 sku 或 product_main_same_observation；缺图须明确原因 |
| asset_path | 有图时提供，相对 manifest 或绝对路径；工具读取字节、核对 SHA256 和图片格式 |
| evidence_version | 按当前生产 code_evidence 后再调用 sku_matching.evidence_version 计算并核对 |
| provenance / evidence_note | 来源引用；局限和未知信息如实保留 |

编码字段由工具用生产规则重算。未提供 asset_path 时不补图，成员仍进入分母和编码流程。研究输入的摘要不等于历史原包自带版本：必须区分“从精确历史观测重算”和“尚缺观测”，不能把回退库存或展示字段包装成恢复成功。旧自动判断、推荐等输入结果被清除，不能借输入字段绕过模型阶段。

研究快照不提供真实销量曲线（内部 sales=0、points=[]），这些占位值不能用于销量分析。工具不从数据库提取输入，也不替第一步证明完整观测、库存有效段及最终区间成员资格；这些条件需第一步清单核验。

真实输入必须额外提供 `--pack 原样本包目录`，只读其中 sets.csv、members.csv，对照原 34 集/536 唯一成员归属，以及 set_id、candidate_size、分层和候选来源。工具不能把新版任意样本规模静默当作旧包；采用新完整样本方案时，必须先确认并另行修改这项边界校验和测试。

可选人工约束为每集 `manual={confirmed_applicable:true, provenance:"明确来源", snapshot:生产SKU快照}`。采用前需确认适用范围；成员身份、证据版本和约束引用必须在冻结边界内。只恢复生产支持的人工约束，丢弃快照中的自动判断与推荐。未提供时明确记录“未采用人工约束”。不要为构造该字段自动读取本机草稿。

## HTTP 模拟响应与定价

fixture 为 JSON，含默认 `usage`、`responses`，也可用 `sets.<set-id>` 覆盖每集响应。序列索引逐集独立：

```json
{
  "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 90},
  "responses": {
    "judgment": [{"value": {"same": true, "confidence": 0.95, "reason": "synthetic"}}],
    "recommendation": [{"value": {"group_id": null, "reason": "synthetic ambiguous"}}],
    "caption": [{"value": {"description": "synthetic description"}}]
  }
}
```

每项可覆盖 usage，或用 raw 返回原始字符串、transport_error/interrupt 模拟传输失败/中断；usage 缺失不补零。verify 可显式提供，也可省略后由离线传输读取色块挑战生成响应。生产 direct/caption 模式按配置实际执行，不为离线测试切换业务规则。

真实定价表必须包含 confirmed=true、source、effective_at、currency 以及 models 数组。每个模型指定与配置精确一致的 endpoint、model、input_per_million、cached_input_per_million、output_per_million、request_reserve；示例见合成 pricing.json，但它不是已确认的真实定价。价格和预留额用十进制，预留额必须大于零。

费用按未命中输入、命中输入、输出 token 乘各自百万 token 单价估算。缓存单价不同时，缺缓存命中/未命中拆分或两者和不等于输入量，费用记未知；同价时可只用总输入 token。字段缺失单独记 unknown_requests。estimated_cost 存在任一未知请求时为 null，known_estimated_cost 单列已知部分，supplier_bill 不伪填估算值。

`http_calls` 与调用上限保守统计所有已持久化的请求尝试，包括视觉核验、失败、中断及 intent/uncertain。意图写盘与实际发出之间也可能崩溃，因此未知状态不代表供应商已确认收到或计费；需结合逐请求状态核对。请求前检查累计已知费用加下次 request_reserve 是否超过预算；收到响应后再核算。缺费用、单次估算超过预留额或累计达到预算时暂停，最后一次响应也会核对。**预留额是本地门禁依据，不能保证供应商最终账单绝不超限**：生产没有限制输出长度，传输中断也可能已经计费。第三步需确认真实价格、预留额依据及可用的供应商侧费用限制。本轮未查询或确认真实价格。

## 产物、未完成与续跑

每个 run-id 独立生成：

| 路径 | 内容 |
| --- | --- |
| frozen.json | 冻结输入、内嵌研究图片、资产/原包摘要、有效配置、请求参数、规则、提示词方法摘要、代码 HEAD/工作区状态及实际源码摘要 |
| requests.json | 逐请求意图、请求摘要、类别/模型/集/生产任务、响应状态、usage/token、费用估算及耗时；不保存 Authorization 或原始错误响应体 |
| report.json | 运行、规模层和总体分母、完成集数、调用/token/费用/耗时 |
| members.csv | 每成员组、依据、判断、推荐、任务状态、版本及说明 |
| sets/<id>/result.json | 完整程序快照、组装结果、未完成原因、覆盖统计和逐集成本 |
| sets/<id>/checkpoint.json、cache.sqlite | 生产任务检查点及本集独立研究缓存 |
| run.lock | 进程文件锁；进程退出自动释放，文件可保留 |

缺图、未判断、生产任务失败、暂停、仅预检查都会保留分母及原因。推荐 group_id=null 可以是成功完成，不能因未唯一推荐就重复请求。complete 仅指该集按当前生产流程和证据检查完成，不证明所有成员两两比较、所有输入完整性已被审计或分组正确；未召回不等于判否。coverage 分别列出所有图片对、召回对、达分对及实际生产判断/推荐任务数，召回对仍可能因编码、人工规则或已在一组而无需执行。

续跑需相同命令加 `--resume`。输入字节、资产、原包、有效配置、生产/工具源码、模式、fixture、定价、调用上限及预算均参与指纹；修改任一项拒绝续跑。**当前不能提高预算或调用上限后继续旧运行**。已完成任务复用生产结果与缓存，不新增请求；有可靠响应但失败的任务，显式续跑可按生产规则再执行，仍累计请求账。没有研究层自动重试。

发现未闭合 intent/uncertain，或已响应却没有持久化生产任务检查点时，暂停待人工核对；工具没有自动消除未知账的选项，不要手改账本后强行续跑。可读性与故障核对资料应保留，续跑不是清空费用账或重新运行的别名。单运行目录有操作系统锁，拒绝并发续跑。

## 第三步具体执行方案（尚未授权运行）

1. **先确认完整输入方案与可取得来源**。第一步报告表明：8 集94 SKU 有恢复的同次图文输入，4 集35 SKU 缺同次图，22 集407 SKU 缺可精确关联的原观测，原分析窗口未冻结。94 个也尚不能作为已通过完整观测与最终区间资格的正式整批基线。研究目标已明确为“评价当前流程在完整输入下的分组效果”。继续旧包需补资格与缺口；若改用新的完整观测样本，先批准新方案/来源和版本，再冻结真实日期，保留旧包审计，另行修改并验证工具的原包边界门禁。无需猜测从未记录的旧窗口。第一窗口随后核验的近期正式材料中，2026-10-17 有5,033个SKU及可关联的名称、库存和图片版本，但原始SKU快照/观测批次尚未进入本机正式库，生产取证函数当前仍返回缺图输入。该日仅是新样本建议来源，尚未冻结或获准替换原包。可继续查源机器原始观测，或先确认是否接受保留来源限制的独立研究重建口径；不能直接把同日流水时间写成未经证明的原观测时间。
2. 第一窗口或后续输入准备工作交付最终 manifest、实际图片、源引用和哈希、观测完整性/单规格依据/身份来源、已确认分析日期、最终成员资格清单；逐项说明缺口。另交原包（或获准的新版本包）及显式确认适用的人工约束；未采用约束亦需明确。
3. 固定实际执行代码版本并保留所有参与源码摘要，冻结既有匹配配置、提示词与参数。先运行 precheck，核对分母、日期/证据、编码结果、人工约束和所有未完成原因。若目标要求完整输入，仍有证据缺口就继续准备输入，不把部分执行称作完整输入评价。
4. 确认真正使用的 endpoint/model、供应商模型能力、计费字段、价格来源与生效日期、币种、调用上限、预算、每模型请求预留额、供应商侧费用控制，以及环境变量密钥就绪。视觉核验也计费；需要额外的真实能力探测时另行确认，不能在预检查中偷偷调用。
5. 用户明确授权该冻结运行后，才使用 `--mode live --allow-live --pricing 已确认定价 --max-calls 已确认次数 --budget 已确认金额`，同时传 manifest、config、pack、新 run-id。本轮不提供真实密钥，不执行此命令。运行前保留原样本包只读副本和研究输出位置，保证专属进程。
6. 逐集运行后核对请求账、未完成、分母/分层覆盖及费用；未知请求或预算停止按上述核对流程处理，不自动重发。导出成员/分组结论用于后续评价。是否回填原 program_context.csv、是否开始人工标注和评价指标计算另行确认；人工参照沿用已确认的标注方案，不把程序结论当真值。

## 验证与处置

定向 25 项入口测试全部通过，覆盖生产 direct/caption、推荐与组装、编码、人工约束、缺证据、跨集隔离、预算/未知费用、续跑/漂移/未知请求、锁与运行路径隔离。测试只模拟 HTTP，未使用真实服务或运行数据库。

分段全量结果及验证边界见 [交付验证记录](../reviews/sku-fixed-baseline-run-validation-2026-10-24.md)。隔离副本、主仓证据和合成演示均保留，不自动清理。源码、输入与工单状态以该记录为准。
