# 票03交付：明确暂停后按原范围和进度继续

日期：2026-10-09（Asia/Shanghai）。仅记录本票隔离分支自动交付；不是主仓集成、原生人工验收或父issue关闭。

## 来源与范围

- 主仓/本地规格/证据根：F:\AI\projects\bestseller
- 实施仓：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller，分支 codex/issues-08-11。
- 开工基线：67df12b98de00fe7a4910c8260eab58c27fccc4e；开工工作区干净，包含完整票01/02。未从 main 新建替代分支。
- 实现提交：4bf7dfe2b6f05142830ed4f62a28fee534a09585。
- Standards时钟边界修复：74b014e50a237fdf9a5381f6d887651445eba79c。
- 最终代码/兼容修复：25bfee5cd008166e453063ef8f4cc89cf03b6969。交付文档随后独立提交，准确文档提交记录于本地工单Answer及Git日志。
- 仅票03；不引入手动登录入口、完整异常/跨天处置、不合入main、不关闭issue08/11、不替代票07联合验收。

## 逐项验收接缝

| 验收 | 自动验证及证据 |
|---|---|
| 11-A01 | shipped HTML实际暂停/继续；真实StopCoordinator完成前禁止继续；同日原轮ID、全成员和SQLite进度保留。单票用例page_pause及four_shop。 |
| 11-A02/03 | A/B/C当天库存各30且完整保存榜单，D501已完成、502身份等待中实际点暂停；开始页默认仅D。继续传A/B/C/D；恢复实际导航只有502，既有501和前三店不重复执行；完成handled92。不是spawn推断。 |
| 11-A04 | 暂停后删除A/B/C，D换名称/URL/home/offer_list；实际继续调度仍原轮快照。另round_shops公共测试断言全部original-name/URL与新导航不沿用；CLI公共main进入真实run_round并保留计划偏离记账。 |
| 11-A06/跨天拒绝 | 无本界面暂停、已终态、跨日、收尾未完成拒绝；保留01未知暂停关窗拒绝。public_resume_changed_scope及GUI原回归。 |
| 11-A09 | 真实HTML改范围取消无中止/启动；确认冻结目标，提交ABANDONED并等待原目标收尾；收尾后仅导航开始页，由明确开始携带new意图。 |
| 11-A10/check_scope | GUI明确resume意图经真实CLI传到公共run_round；持锁SQLite事务复核原轮ID/日期/全快照/唯一活动轮/最新轮/残留目标；终态、身份替换、范围篡改、预检后另一轮均拒绝且不接管/新建，保留check_scope和全部成员。 |
| 失败与竞争 | Popen失败、浏览器open_session失败、child拒绝均保留真实暂停可显式重试；执行确认commit前不消费。界面轮询不自动重试；旧ack不吞第二次暂停，已消费attempt不能重放。 |
| 02+03 | 身份验证等待中真实点击暂停，后续业务页面恢复不自动继续；stop cleanup后显式继续同轮。IDENTITY_VERIFICATION_TIMEOUT先提交后拒绝继续不覆盖；保留28项身份生命周期与01先提交真终态/关窗/新建接缝。 |

## TDD与测试证据

- 旧历史验证超时与冻结诊断未改写；新四店证据明确记录实际暂停点击，不能把历史超时改称用户暂停。
- 初期红绿：04→06明确resume命令、08→10公共持锁拒绝、12→14spawn重试、16→18子进程浏览器失败资格、24→25配置快照、27→32改范围实际事件。
- 加强红绿：34→36/50第二次暂停、38→40真实页面启动错误、42→44旧attempt拒绝重放、48→50无轮询先消费执行确认、59→61领域时钟注入。
- 02/03、20、29/30与36的错误含测试夹具/桥接/时钟类型修正；如实保留，不能当作产品行为红灯。
- 定向回归最终第一阶段284项通过，详细计数55-focused-results.json；审查修复后本票16项通过，61-clock-and-replacement-green.log。
- 源码/输入/测试配置：51-source-config-manifest.json、62-final-source-config-manifest.json；两份manifest保留原字节hash和CRLF规范化对比（仅历史文档换行不同，测试代码逐文件对应）。
- 新四店复现：52-four-shop-new-reproduction.json、63-final-four-shop-new-reproduction.json；输出来自公共调度测试实际导航断言。
- 编译检查与git diff --check已运行；未配置/未声称额外静态类型检查。

## 最终回归及分段全量

- 第一阶段10文件284项通过；时钟修复后本票16项通过；最终兼容修复后crawler_mutex 6、本票16、abort_new_round 39、identity_lifecycle 28，共89项通过（68红→69/70绿）。
- 69中误拼test_identity_verification_lifecycle.py发现0项、退出5，**不是通过证据**；70用实际文件名补跑28项通过。所有失败与夹具修正日志均保留。
- 最终四店复现：74-final-four-shop-reproduction.json。实际点击暂停；默认勾选仅D；原id=1且A/B/C/D保留，恢复实际导航仅剩余502，handled=92，COMPLETED。配置删除ABC且D换name/URL/home/list仍原快照权威。
- 最终源码/配置哈希：73-final-compat-manifest.json。314份跟踪文件规范化一致，代码逐字节一致，少数既有01文档CRLF不同保留双hash。T3 clone74b014e仅补25bfee5的pipeline；配置从T2测试配置派生，写入路径全部在T3内、报警关闭，无真实DB/profile/venv复制。
- 前两轮全量失败如实保留：full-suite-01为1579/1579、FAILED(errors=5)、退出1；full-suite-02为1581/1581、FAILED(errors=5)、退出1，均6/7段OK且对账一致。5错误均为本票无条件传resume_intent=None破坏原停止/真实终态竞争适配器，**不是已知假红**。25bfee5仅在明确继续时扩展参数，既有6项回归红→绿；详见76-full-failure-classification.md。
- 最终T3在第二轮退出后串行跑冻结代码，未修改在跑副本。命令 `python tools/run_full_suite.py --log-dir "F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\03\20261009-ticket03-serial-01\full-suite-03"`，未加-f，退出0。

```text
分段全量测试 2026-10-09 13:25:28
仓库：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\03\20261009-ticket03-compat-03\bestseller
传参：python tools/run_full_suite.py --log-dir F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\03\20261009-ticket03-serial-01\full-suite-03

test_exchange：65 项，OK，87.5s；日志 seg-01-test_exchange.log
test_judgment_set：50 项，OK，41.4s；日志 seg-02-test_judgment_set.log
test_plan_step：54 项，OK，25.2s；日志 seg-03-test_plan_step.log
test_export：30 项，OK，25.5s；日志 seg-04-test_export.log
test_git_channel：17 项，OK，11.8s；日志 seg-05-test_git_channel.log
test_shops_sync：17 项，OK，10.4s；日志 seg-06-test_shops_sync.log
其余段（92 文件）：1348 项，OK，502.7s；日志 seg-07-其余段(92_文件).log

合计 1581 项（预期 1581，计数对账一致），总耗时 704.4s，段结果 7/7 段 OK，结论 OK
```

## 双轴code-review（独立报告，不混合重排）

### Standards
初审1条P3：open_resume隐式读挂钟违反ADR-0005；74b014e改为持锁pipeline显式传now，禁止领域读取挂钟的测试及替换轮回归通过。74b014e和25bfee5独立复审均无新增，此P3已关闭，未解决0项。

### Spec
初审、时钟修复复审及最终兼容修复复审均无有依据新增发现；未解决0项。静态审查不替代测试/人工证明。

固定基线/候选与完整原报告见 `docs/reviews/issues-08-11-ticket03-code-review-2026-10-09.md`；主仓证据56/57、71、77保留同源原件。

## 目录与证据处置

- 主仓：F:\AI\projects\bestseller。
- 实施隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller；含票01/02/03分支交付，保留用于后续串行票，不清理。
- 测试副本一：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\03\20261009-ticket03-serial-01\bestseller；clone开工基线后补本票文件，首轮全量针对4bf7dfe，不能冒充时钟修复版本。
- 测试副本二：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\03\20261009-ticket03-reviewfix-02\bestseller；从4bf7dfe独立clone，补74b014e三份文件与同源测试配置，用于时钟修复复测和第二轮全量（仍5个兼容错误，非最终通过）。未复制真实DB/profile/虚拟环境。
- 测试副本三：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\03\20261009-ticket03-compat-03\bestseller；clone74b014e补pipeline兼容修复，最终代码25bfee5；全量full-suite-03通过。
- 验收证据：F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\03\20261009-ticket03-serial-01；永久保留全部红绿日志、manifest、复现JSON、分段日志和审查报告；.scratch不随Git交付，跨机器需显式携带或归档。
- 可删除候选：无。
- 暂留待确认：三个票03测试副本均保留，待人工/后续集成证据核验且用户明确授权后再评估；实施副本保留供后续串行票。
- 实际副本清理：未执行。仅按AGENTS归档并点名清理已结束的本票审查任务交接目录，未触碰他人任务。

## 未完成与人工事项

未集成主仓main、未部署原生Windows界面人工验收（08-H01/11-H01）、未实施04–07、未做票07联合验收、未处理完整异常/跨天旧轮界面。Headless Edge为独立临时context，HTTP(S)离线拦截；不使用日常Edge/profile、真实账号或网络采集。测试使用现有Python及已安装依赖，未创建/搬运虚拟环境。
