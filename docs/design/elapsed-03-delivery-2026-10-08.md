# elapsed 03：跨入口验收与整项交付（2026-10-08）

状态：03 已验收；双轴审查及03独立隔离全量通过，01/02/03与#elapsed整项收口。真实Windows原生关窗等现场限制不因自动化通过而解除。

## 定义与统计范围

已抓时长是本机数据库中某轮有效采集执行段累计，不是轮次墙钟跨度。开轮前准备、用户暂停等待、执行结束后的 GUI 核验及浏览器归属清理不计；准备完成且轮次确定之后，浏览器启动/连接、页面加载/读取、人工验证、重试退避、节流计入。暂停/中止请求不是段终点，仍执行的尾段计入。同轮续跑累加、新轮隔离。正常确认关窗复用 ADR-0048 中止监督，不以窗口消失或计时保存代替停止完成。

## 权威与接口

`bestseller_monitor/elapsed.py` 是唯一计时知识持有者：`begin` 预约并开始段，返回段的 `tick` / `checkpoint` / `settle`；`read` 返回解释好的 `Fact(seconds, quality)`。轮次拥有数字，执行贡献有唯一身份的段，SQLite 持久化累计与完整性；单调基点只存在执行进程内存。公共 pipeline 表达准备后开始、合作检查、有效执行结束这些事件，不自行计算秒数；GUI 不再累加或外推，views 只格式化事实。

完整记录为 complete；活跃成功保存快照为 recorded；未知历史/未记录尾段为 lower_bound（至少）；无计时记录为 unrecorded（未记录）。旧状态文件的计时字段不再作为权威；旧轮无数据库计时记录不反推，旧轮续跑只能增加可证明的新段，不能消除旧缺失。接口和决策详见 ADR-0051。

## 证据边界与已修复行为

01/02 已完成的行为修复：真实 SQLite/可推进时钟复现后，过程页和结果页改为读取采集累计，而非 GUI 会话累计或轮次跨度；公共采集入口接入段开始/检查点/执行结束结账；暂停请求后的执行尾段正确记录，同轮多段累计；重复结账幂等，身份替换/异常无 finally/保存失败时仅展示可证明的事实。01/02 的独立红绿与复审证据保留在各票目录，不以旧报告行号当当前事实。

03 逐切片验证已有实现，新增 12 项全部通过，未发现需要新增生产修复的故障。切片中的红色日志是测试前置错误（终态枚举大小写、GUI 尚未认领活动轮次、监督 adapter 已 gone、同日仍活动轮复用、假 PID 与执行身份不匹配），修正测试设置后通过；不得将其称为生产故障，也未为流程强行重构。

确定性 adapter 验证不等于真实 Windows 原生关窗、进程强杀、崩溃、掉电的现场验收。GUI 消失而采集继续的测试只丢弃 GUI adapter，不实际杀窗口进程。无法恢复程序未保存的时间：无 finally 或最终保存失败仅最后成功快照下界；数据库连预约都拒写时甚至无法证明执行发生。合作检查约每秒保存是目标，不是最多损失一秒的硬承诺。GUI 迟到观察不补时间，不跨进程复用单调基点。

## 03 行为覆盖

新增 `tests/test_elapsed_cross_entry.py`，复用现有时钟、进程、窗口与停止 runtime adapter，真实 SQLite、pipeline、CLI main、GUI API 与安装关窗事件，无真实 sleep：

1. GUI 消失/重开、外部执行继续、两页反复读取不启动新段。
2. 确认关窗计入执行尾段，排除清理等待，保存不放行未完成的窗口关闭。
3. 取消/关闭确认框及重复刷新不提前结账。
4. 真实本地试采 CLI 暂停及 GUI 接管只读同轮事实。
5. 明确暂停后等待再确认关闭不增加时长。
6. 暂停停止中/清理中确认沿用原监督，不新开段。
7. 已中止时重复关闭不再确认或重复累计。
8. 目标替换、旧段迟到结账、新轮保持隔离。
9. GUI 旧轮记忆替换与显式历史结果分别读取所属轮。
10. 正式 CLI main 本地临时配置及 GUI 重开同样使用唯一持有者。
11. 确认过程中自然完成保留真实终态和累计。
12. 旧未知、旧续跑下界、无 finally 检查点与完整事实保存/重载后两页一致。

父 spec 的单段、暂停恢复、多轮、保存/加载、墙钟调整、幂等、异常和兼容由已有 elapsed / elapsed_lifecycle / GUI / views / round_wiring / crawler_close / local_trial / run_cli 测试覆盖，03 全量保留全部既有回归。30项User Stories逐项对照保留于主仓证据的parent-spec-coverage.md。

## deletion test 与范围

源码核对：GUI 无 `_elapsed_base` / `_run_start_ts`，无 elapsed 累计公式；views 只消费解释后的数字和质量，两处文案均“已抓时长”。若拿掉 elapsed，差值、持久化、身份、幂等、下界解释将丢失，调用方没有第二份可接管的计时算法。这是源码/行为上的删除检验，不执行破坏性删除生产文件。

本票生产代码未改变；整项仅在公共入口、合作检查与现有执行结束边界做最小衔接。#prep-owner 仍未处理：GUI 准备等待使用的墙钟属于其范围，不为 elapsed 顺手删除；不改准备状态模型、停止超时、进程归属、轮次终态。没有通用时钟框架、入口全面时间注入、跨机器时间汇总、交换包扩展或归档 HTML 修改。

## 验收与处置

- 固定03基线：275545fe9cba7998d49d1dc57934f26238f33ced。
- 主仓：F:\AI\projects\bestseller。
- 隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\elapsed\03\20261008-implement\bestseller；独立 no-hardlinks clone，仅补本票点名文件。
- 主仓证据：F:\AI\projects\bestseller\.scratch\logs\tickets\elapsed\03\20261008-implement。
- 配置：复用02安全测试配置字节，重新解析确认数据库、profile、交换根、缓存、输出均03副本内，禁启浏览器、matching disabled，补 shops.csv；未复制真实库、登录态或虚拟环境。
- 主仓跨入口：12 tests / OK；副本同12项通过。相关单文件共258项通过：elapsed9、lifecycle14、cross_entry12、GUI67、views27、crawler_close51、round_wiring17、local_trial_gui20、local_trial_cli24、run_cli17。compileall通过；环境无mypy/pyright及项目类型检查配置，编译检查不冒称静态类型检查。
- 永久保留：主仓票证据（日志、manifest、配置核验、审查文本）、本机工单/spec 和正式文档；不随副本清理，scratch 不随 Git、也不等于异地备份。
- 可删除候选：上述03副本（验收完成后）；只是候选，未授权实际删除。
- 暂留待确认：上述副本供复核，待用户决定保留或授权清理。
- 实际清理：副本未删除，其他任务未 add/commit/stash。仅本任务差异提交，提交 SHA 收口后记入本机03票与证据，避免文档自引用循环。

### 双轴与最终全量

03固定范围：`275545fe9cba7998d49d1dc57934f26238f33ced...d333d15d84534dbc82d5a218af3022d2e560db7c`（隔离审查快照）。Standards：0项可行动发现；Spec：0项可行动发现。两轴只读源码/日志核对，审查代理未自行运行测试；报告与完整任务文本保留主仓03证据。

从上述副本根实际执行（不加-f，退出码0）：

```text
python tools/run_full_suite.py --log-dir "F:\AI\projects\bestseller\.scratch\logs\tickets\elapsed\03\20261008-implement\full-suite"
```

summary原文：

```text
合计 1544 项（预期 1544，计数对账一致），总耗时 701.5s，段结果 7/7 段 OK，结论 OK
```

摘要：F:\AI\projects\bestseller\.scratch\logs\tickets\elapsed\03\20261008-implement\full-suite\summary.txt。七段实际65/50/54/30/17/17/1311项；日志同目录。不以01/02或关窗任务旧全量作为03证据。manifest/config-audit/config-nested-audit/tested-source-main-match记录来源、哈希及安全路径。最终只补状态/证据文档，不改变已审查且全量受测的实现或测试。

### 修改与提交范围

本票共5个版本库文件：tests/test_elapsed_cross_entry.py、docs/design/elapsed-03-delivery-2026-10-08.md、docs/adr/0051-round-elapsed-owned-by-collection-execution.md、docs/adr/0047-2026-09-23-review-remaining-candidates-suspended.md、CONTEXT.md的elapsed词条。没有新增生产代码改动；01/02具体修复与文件记录保留各自交付文档。

CONTEXT只投影elapsed差异到暂存区，SKU等其他内容逐字保留；其他任务设计文档及交接不提交。03提交信息：`test: verify elapsed cross-entry lifecycle and close delivery (elapsed ticket 03)`；实际SHA记录本机票/spec及主仓证据commit.txt。隔离副本保留未删；仅四个本票已结束审查交接目录按AGENTS协议归档校验后清理（handoff-cleanup.txt），工作区结果副本保留待确认。

补充最终五文件审查范围：`275545fe9cba7998d49d1dc57934f26238f33ced...5bf55b237ef27e5c2dfa6f3dd1a2fa5244771728`。最终Standards：0项可行动发现；最终Spec：0项可行动发现。两份报告与任务文本归档主仓03证据；初审/最终均只读静态及既有日志核对。本段仅记复核结果，未修改受测测试或生产实现。
