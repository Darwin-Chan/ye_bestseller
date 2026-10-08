# elapsed 01：单段计时、持久化及两页贯通交付

日期：2026-10-08（Asia/Shanghai）。范围仅 01；02/03 未认领，#elapsed 整项不关闭。

## 精确定义与生命周期

已抓时长属于本机数据库轮次，是有效采集执行段累计，不是轮次起止的墙钟跨度。
开轮前及子进程内准备、用户暂停间隔不计；轮次确定且准备完成后，从有效驱动开始到采集工作结束计入；随后浏览器归属清理不计，包含浏览器启动/连接、页面读取及有效执行中的人工验证、退避和节流。
停止请求不是立即段终点；采集仍有效执行的尾段计入，之后 GUI 仅核验/归属清理不计。
01 贯通单段正常路径；暂停恢复、多段及不可观察退出的完整精度矩阵待 02，跨入口和关窗完整验收待 03。

## interface 与状态归属

- `bestseller_monitor.elapsed.begin(conn, round_id, process_started=..., new_round=..., clock=...)`：公共采集路径进入有效执行时建立唯一执行段，返回 Segment。new_round 来自轮次模块 opened.created，不能把旧轮伪装成新轮。
- `Segment.checkpoint()` / `Segment.settle()`：保存本段累计/冻结最终值，返回是否成功。重复保存覆盖本段而不是叠加；已结账段冻结，保存失败不冒充完整、不覆盖原采集/停止异常。
- `elapsed.read(conn, round_id)`：只读已成功持久化的 Fact（seconds / quality / text / note），不写计时、不外推；views 不再选择轮次跨度。
- 基点仅在执行进程内存，时差用可注入的单调时钟；SQLite 的 elapsed_rounds / elapsed_segments 保存轮次历史覆盖及已计算的秒数、唯一执行身份、身份登记 started_at 和结账标记。不交换这些本机表，不跨进程比较单调基点。
- GUI 的 start_ts / _elapsed_base / _run_start_ts / _current_elapsed 及 UiState.elapsed_sec 已移除。GUI 只提供会话事实，两页消费同一轮次计时权威。

删除测试：删掉 elapsed module 会令累计、保存、幂等及未知/下界解释失去所有者；GUI 不保留一套备用公式，views 也不再根据开始/结束时间猜时长。

## 兼容与精度限制

旧库通过既有 connect/open 建表入口无损增加本机表，不回填历史。完全没有记录为“未记录”；旧轮后续新段只贡献可证明时间，整轮仍显示“至少”。新轮初始可证明 0 与旧轮未知不同。

完整正常结账为确定值；活跃读数仅最近保存快照。01 接现有采集检查入口，尚无约每秒独立调度保证；浏览器或存储阻塞期间数值可能暂不变化。结束轮次缺少最终保存时解释为下界。开放轮次上不可走收尾的退出辨识、完整多段质量和检查点精度待 02；不声称已实现或能精确恢复未知尾段。

## 行为证据与边界

逐条先失败后最小实现，红/绿日志留在主仓票证据目录。两页 tracer 在未接入时观测到过程页 0 而持久化事实为 12；公共采集入口 tracer 最初缺少 clock seam，基础检查点 tracer 观测到保存前仍为 0。存储授权拒绝 adapter 验证计时错误不会替换采集控制流，最终未保存不误称完整。

复用真实 SQLite、StopClock、ScriptedListing/FakeCard 和现有浏览器 adapter：准备推进 60 秒不计，浏览器连接推进 12 秒，浏览器归属清理再推进 30 秒不计，有效采集后两页/GUI 重新加载为 12；结束后再推进 80 秒不增长。检查点后再推进但未保存，GUI 不补算。首轮 Spec 审查发现清理时间误计（42 秒，应为 12 秒），修复为驱动执行结束先结账、再清理；Standards 审查发现初次 INSERT 失败会阻断采集，修复为登记失败隔离并可重试。两项均先留下失败测试，再修复通过。最终保存失败后的重试仍冻结第一次结束时刻，不把后续清理补入。

旧报告其余退出方式仍按源码推论/待验证区分；没有真实 OS 崩溃/强杀现场复现，没有以关窗任务旧全量冒充本票验收，也不替关窗任务声称原生 Windows 人工验收完成。

## 修改与相关检查

生产：gui.py、bestseller_monitor/elapsed.py、db.py、pipeline.py、views.py、pages/ui_live.html。
测试：tests/test_elapsed.py（9 项）、test_gui.py、test_views.py、test_crawler_mutex.py（兼容执行结束 callback）；保留停止、准备、归属、关窗与采集回归。
正式文档：CONTEXT 的 elapsed 定义、ADR-0011 的有限替代说明、ADR-0047 单票状态、ADR-0051 和本记录；其他 SKU/候选文档排除。

已通过单文件：elapsed 9、GUI 67、views 27、round_wiring 17、crawler_mutex 6、crawler_close 51、db 75；p1 31 项通过。compileall 通过。仓库没有配置 mypy/pyright，环境亦未安装，不能将编译检查称为静态类型检查。

## 独立验收（01 已通过）

固定基点 c8d0fb9926d7572e85966efbaaeb658e3637bdfb；首轮快照 78158a5a498f5572a87eb17e3a77c04aa59a4f41；修复复审快照 4bc0d429da7253db9ace10f219e9abcb078a82df。
隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\elapsed\01\20261008-implement\bestseller。
主仓证据：F:\AI\projects\bestseller\.scratch\logs\tickets\elapsed\01\20261008-implement。
来源及逐文件 SHA-256：source-manifest.json；安全配置核验：config-and-checks.json。CONTEXT 只复制 HEAD + elapsed 定义，未复制其他任务 SKU 内容。未复制真实库/profile/虚拟环境。

首轮双轴各发现一项 P2，均以新增失败测试修复。修复快照复审：Standards 无剩余可行动发现，Spec 在 01 范围内无剩余阻断发现。报告保存为 review-initial-standards.md / review-initial-spec.md / review-final-standards.md / review-final-spec.md。审查为独立只读；最终保存失败后重试冻结仅做源码核对，没有单独故障注入行为验收，不以审查冒充测试。

从隔离副本根实际执行一次：

```text
python tools/run_full_suite.py --log-dir "F:\AI\projects\bestseller\.scratch\logs\tickets\elapsed\01\20261008-implement\full-suite"
```

不带 -f，退出码 0。summary.txt 合计/对账/结论原文：

```text
合计 1446 项（预期 1446，计数对账一致），总耗时 651.6s，段结果 7/7 段 OK，结论 OK
```

摘要绝对路径：F:\AI\projects\bestseller\.scratch\logs\tickets\elapsed\01\20261008-implement\full-suite\summary.txt。
7 段各为 65、50、54、30、17、17、1213 项；此为本票实际证据，不使用关窗任务旧全量替代。全量后核验生产/测试文件 SHA-256 与审查快照一致；验收后补充的状态/证据文档不改变受测实现。收尾时主仓另有并行文档提交 316f492（只改其他任务设计文档），不属于本票、不在本票审查差异内。

## 后续与处置

#prep-owner 仍挂起；本项只在现有准备完成之后进入段、在现有采集检查及结束点衔接，不重构准备模型、停止窗口或进程归属。
- 主仓：F:\AI\projects\bestseller。
- 隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\elapsed\01\20261008-implement\bestseller；来源 c8d0fb9 加 source-manifest.json 点名改动；测试 config/*.toml、shops.csv 来自 example，路径检查见 config-and-checks.json。
- 保留：F:\AI\projects\bestseller\.scratch\logs\tickets\elapsed\01\20261008-implement 全部票日志、红绿证据、manifest、四份审查报告及任务描述；F:\AI\projects\bestseller\.scratch\elapsed 的工单/spec/grill；正式 ADR 与本交付记录。主仓过程证据不随 Git 传播，尚不等于异地备份。
- 可删除候选：F:\AI\projects\bestseller\.scratch\sandboxes\elapsed\01\20261008-implement\bestseller（独立 clone；代码快照、测试安全配置及本地缓存；必要验收证据已在主仓保留）。仅候选，未获删除授权，不自动删除。
- 暂留待确认：该副本仍保留，等待用户决定是否用于 02 或授权清理；复用/删除前重新核验独有提交、数据、路径与重解析点。没有新建独立虚拟环境。
- 实际清理：未清理隔离副本。按 AGENTS 交接协议仅归档到主仓证据区后，清理本票点名的四个已结束审查交接目录，未触及其他任务。
