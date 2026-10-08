# elapsed 02：多段、检查点与中断下界

日期：2026-10-08（Asia/Shanghai）。仅用户授权的02；01已交付，03未实施，整项不关闭。
依据：ADR-0051、主仓 `.scratch/elapsed/spec.md` 与02票。审查初始基线69066da421b255168884224a20a28fb20b6d4713；集成验收基线15345f313e4db4ee2374d27bc7cac7d9d753546d（并行SKU独立提交）。

## 定义与范围

已抓时长属于本机数据库轮次，是有效采集执行段的累计，不是轮次墙钟跨度。
准备不计；浏览器启动/连接、采集工作、人工验证等待、退避和自动节流计入。
暂停间隔不计；按钮发出暂停/中止请求不是执行终点，请求后的有效尾段仍计入。
执行结束后浏览器归属清理和GUI核验不计。沿用既有轮次、锁、进程归属与停止超时，不改变终态。

## 权威 interface 与 deletion test

`bestseller_monitor/elapsed.py` 继续独占单调基点、差值、冻结、累计与事实解释。
`begin(conn, round_id, process_started=..., new_round=..., clock=...)` 返回执行进程内的 Segment；
`tick()` 表达协作检查事件、约每秒尝试保存，`checkpoint()` 显式保存，`settle()` 冻结执行终点且幂等。
`read(conn, round_id)` 返回已保存 Fact（seconds/quality/text/note）。GUI与两页不自行外推或拼累计公式。
生产默认 time.monotonic；测试可推进时钟；单调基点仅在当前进程内，不持久化也不跨进程比较。
采集调用方只将原检查点调用改成tick，不知道节拍公式。删除elapsed后，调度、精度、缺口与结账知识
将不复存在，不是把GUI私有字段迁到别处。

## 行为证据

新增 `tests/test_elapsed_lifecycle.py` 14项通过公开计时、公共采集生命周期、GUI暂停/中止与页面读取验收。
既有单段基础9项保留。真实SQLite、现有锁/进程adapter、可推进Clock；不使用真实sleep。

新增约秒调度及问题修复的红绿证据：
- 协作检查原来缺少一秒节拍；tick测试先缺少interface而失败，再由窄入口承载目标节拍。
- 不执行settle且锁释放后，5秒保存快照原仍为recorded；现在是lower_bound，两页均“至少5秒”，
  观察前等待80秒不补算。红绿日志 red-02-orphan.log / green-02-orphan.log。
- 同轮已有完整5秒，新段INSERT失败会让旧5秒冒充完整；现在先持久化pending_execution登记预约，
  已记录缺口可见下界；接班新段3秒后是“至少8秒”，未知旧缺口永久保留。
  红绿日志 red-04-registration-gap.log / green-04-registration-gap.log。

确认原有正确行为（不宣称新增修复）：120秒→暂停→等待600秒→180秒→暂停累计300秒；
中止5秒检查点后7秒尾段最终12秒，重复中止/刷新不增长；finally异常12秒完整结账而异常仍上抛，
原可续轮不被改终态；失败的最终保存只见5秒下界，恢复写入重试12秒，不把30秒清理加进去；
同一执行登记恢复清除临时缺口。等待原语0.5秒切片的三段3秒等待保存快照分别2/5/8秒，结账9秒。
新轮不继承旧轮，旧段迟到事件不结账新轮；新进程重置单调基点，已保存秒数仍累加。
墙钟前拨/回拨不影响当前单调差值；锁仍在但启动身份更替/缺失不能证明旧段活跃。
测试适配器用来验证协作等待的时长范围，不声称对真实浏览器每种验证/退避路径逐个现场操作。

Spec初审另发现“新执行已持锁但旧身份未替换”窗口：旧段曾从下界变回recorded。
公开read + 原OS adapter反例已失败（red-14-new-holder.log）；最小增加只读存活和OS创建证明核验，
旧目标死亡、无法核验、创建证明缺失或PID复用都保留下界（green-14-new-holder.log）。
明确活跃身份夹具仍为recorded，不把单纯快照滞后误当缺失。不是在本票重新设计停止/归属模型。

## 持久化与兼容

elapsed_rounds新增可空pending_execution：段登记前先记录缺口；本执行成功登记清除预约，接班执行
替换遗留预约将history_known置为未知。elapsed_segments新增可空process_pid，与process_started和轮次
配合现有会话锁及只读OS存活/创建证明解释未结账快照。read用同一SQL快照取得段事实；只读，不清身份或补猜退出终点。
迁移仅补列，无历史回填。01旧已结账段仍有效；旧未结账缺pid无法证明活跃，按下界；
旧轮无时长记录显示“未记录”，续跑仅证明新段，未知历史不会因新段完整而消失。
原始秒数只保存成功记录；质量为complete、recorded（活跃已存快照）、lower_bound或unrecorded。

## 限制与证据边界

约1秒是合作检查的保存目标，不是硬丢失上限；阻塞、调度和存储故障会令快照更旧。
无法执行finally的退出仅已保存下界。本票测试是确定性不结账/锁释放adapter，不是OS强杀、
掉电、真实崩溃现场。未做Windows原生关窗或CLI跨入口全矩阵，留给03。
若数据库连登记预约也完全拒绝写入，程序无法留下本次执行曾发生的持久化证据；日志明确失败，
没有记录不制造秒数，但旧库中已有完整值也不能凭空获知这个未记录事件。持久化缺口保护以预约
写入成功为前提；不承诺在全存储失效后恢复未知事实。
本轮错误注入针对INSERT/UPDATE拒绝；磁盘损坏、系统掉电和锁抢占竞态现场未验收。

## 验收与处置（02已通过，整项未关闭）

隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\elapsed\02\20261008-final\bestseller（最终集成验收）；F:\AI\projects\bestseller\.scratch\sandboxes\elapsed\02\20261008-implement\bestseller为初审副本，保留。
证据：F:\AI\projects\bestseller\.scratch\logs\tickets\elapsed\02\20261008-implement。
原审查副本来源基线69066da，最终副本来源已提交15345f3，加点名02生产/测试/文档；不复制SKU未提交改动、真实库/profile或虚拟环境。
安全测试配置来自example；配置清单、哈希、审查和最终全量对账记录在主仓证据区。
最终修复快照722439af78a57101cd4ab6f3b25fb44057d6a6bd；初审快照42094915b469e8b094ac0c31127ec70287ef66ba。
Standards初审0发现，Spec初审1项P2；红绿修复后最终Standards0、Spec无剩余可行动发现。
四份只读报告、任务文本、快照及manifest均归档在主仓票证据区。修复后单文件：elapsed9、生命周期14、
GUI67、views27、DB75、crawler_close51、本地试抓GUI20/CLI24；前置round_wiring17、p1 31、waiting17、
stop_request20、round_stop11、stop_target18、stop_matching_ui6、crawler_mutex6也通过。
compileall通过；环境无mypy/pyright及相应项目配置，编译检查不冒称静态类型检查。

实际从最终副本根执行：

```text
python tools/run_full_suite.py --log-dir "F:\AI\projects\bestseller\.scratch\logs\tickets\elapsed\02\20261008-implement\full-suite-final"
```

不加-f，退出码0；summary原文：

```text
合计 1532 项（预期 1532，计数对账一致），总耗时 711.6s，段结果 7/7 段 OK，结论 OK
```

摘要绝对路径：F:\AI\projects\bestseller\.scratch\logs\tickets\elapsed\02\20261008-implement\full-suite-final\summary.txt。
7段实际65/50/54/30/17/17/1299项；日志同目录。全量后主仓/副本受测生产与测试文件哈希一致，
final-source-manifest.json及final-config-and-checks.json记录来源、安全配置及哈希。
验收后仅补状态/证据文档，不改变已审查与受测实现；不沿用01或其他任务全量。
初审Spec发现P2后，未修复快照的全量已中止（full-suite/ABORTED.txt），不作通过证据；最终修复全量另用full-suite-final。

保留：主仓票证据（包括红绿、相关单文件、manifest、安全配置与双轴报告）、elapsed过程材料和正式文档。
可删除候选：上述两个完整隔离副本，仅候选，不授权自动删除。
暂留待确认：两个副本等待用户决定03是否复用或授权清理；没有新建虚拟环境。
实际清理：未删除副本或其他任务材料；审查交接目录按协议单独归档/清理。
#prep-owner仍未处理；本票只复用现有执行检查的最小接线，不新增准备模型或改变超时/进程归属。
