# 0047. 2026-09-23 审查的其余候选挂起（待定暂不处理）

- 状态：已接受（其余候选挂起，不是否决；#elapsed 已于2026-10-08实施验收；#prep-owner 已于2026-10-10实施验收）
- 日期：2026-10-05

## 背景

2026-09-23 的架构审查（[architecture-review-2026-09-23.html](../reviews/architecture-review-2026-09-23.html)，
按用户要求排除销量分析程序）产出 8 Strong + 8 Worth exploring，按五组跟进：

- ③ 等待原语：六票收束，见 [ADR-0042](0042-waiting-primitive-in-one-module.md)；
- ④ 编码契约：2026-10-05 收口，见 [ADR-0046](0046-python-pipe-encoding-owned-by-launcher.md)
  与[交付记录](../ops/python-pipe-encoding-delivery-2026-10-05.md)；
- 其余候选 2026-10-05 经用户决定**待定暂不处理**。

## 决定

挂起（不是否决）下列候选：

- ① 交换台包线：包发布原语、找包读包唯一入口、七动作结局形状
  （报告 `#package-publish` / `#package-read` / `#action-outcome`）；
- ② 界面状态：开轮前准备运行态（`#prep-owner`）于2026-10-10实施验收（整项完成）；
  已抓时长段计时（`#elapsed`）于2026-10-08实施验收，见下文；
- ⑤ 工具边界：工具列表页装配收归 `listing`、诊断对账工具走生产读口
  （`#tool-page` / `#tool-reads`）；
- 8 条 Worth exploring 的其余各条（`#tx-seam`、`#image-evidence`、
  `#page-contract`、`#plan-owner`、`#judgment-paths`、`#launcher-spec`），以及报告
  「看过但不列入」附录各条。

理由：用户 2026-10-05 决定暂不处理（待定，非否决）。各条的摩擦、证据与强度都在报告卡片里，
逐条依据不在本条重复；报告归档即终稿，不加落地注解。

## 后果

- **将来的架构审查不应把上述候选当作新发现重提**；要提，得先说清前提变了——
  沿用 [ADR-0027](0027-2026-09-14-review-candidates-05-07-suspended.md) 的先例。
- 各条都仍是真实摩擦，不是被否决：真要捡起时按项目现行流程走
  （grill-with-docs → to-spec → to-tickets → implement），报告卡片自带 file:line 与证据、
  可直接当种子——③④ 两组已按这条流程走通，可作样板。

## 2026-10-08：仅恢复并实施 handled-predicate

用户本次授权恢复 `#handled-predicate`，按 grill-with-docs → to-spec → to-tickets → implement 推进。
已实施：计数、补采排除与跳过写入守卫直接复用单一内部已处理快照判据。
三处原先同值，无生产分叉证据；不改变成功/跳过/失败分类、成功库存判据、孤儿集合口径、
公开 interface 或候选 06 的店铺阶段及同轮续跑模型。

相关单文件 143 项通过；隔离分段全量 1388 项、7/7 段 OK，计数对账一致。Standards 在已核对范围内无硬违反或需整改气味，Spec 无发现；两轴审查完成，读取边界保留在交付记录。
决定、规格与验收证据见[交付记录](../design/handled-predicate-delivery-2026-10-08.md)。
其余上述挂起项及「看过但不列入」附录各条保持原状态。
## 2026-10-08：仅恢复 elapsed 的设计

用户授权恢复 `#elapsed`，按 grill-with-docs → to-spec → to-tickets → implement 推进。
grill、测试接缝、spec 与三票方案已获批准，工单 01–03 已发布为 ready-for-agent。用户明确暂不
implement；尚未认领或实施，未运行 elapsed 行为测试或全量。当前结论限源码控制流证据，不把强杀/崩溃等未验证场景写成已复现故障。
已接受设计见 [ADR-0051](0051-round-elapsed-owned-by-collection-execution.md)：轮次采集执行段累计，
一个权威 module 持有，单调差值、本地持久化、幂等结账与完整/下界/未记录区分。

`#prep-owner` 及其他挂起候选保持不变。本项仅允许准备完成后的计时起点、现有执行/停止结束
处的最小接线，不改变准备状态模型、超时或进程归属。完成实施和验收后再追加实际收口状态。

## 2026-10-08：elapsed 01 单段基础实施（整项尚未关闭）

用户随后授权仅实施 01。单段计时权威、公共采集起止/基础检查点、本机持久化、旧轮未知与两页同口径已贯通；独立双轴复审无剩余可行动/阻断发现，隔离分段全量 1446/1446 项、7/7 段 OK、计数对账一致；01 已验收。GUI 旧累计字段和公式已移除。实际证据及精度限制见 [01 交付记录](../design/elapsed-01-delivery-2026-10-08.md)。02 的约每秒调度、多段/中断精度及 03 的跨入口完整验收仍待实施，不能将 #elapsed 标为整项完成。
`#prep-owner` 及其他挂起候选保持不变；准备后进入计时与已有停止检查的最小衔接不改变它们的状态模型、超时或进程归属。


## 2026-10-08：elapsed 02 多段/下界已验收（整项尚未关闭）

用户仅授权02。约秒协作检查点、暂停续跑累计、中止尾段与可finally异常结账、可证明中断下界、
登记缺口和兼容迁移已交付；新增14项公开interface生命周期测试。初审Spec发现新持锁者/旧身份窗口P2，
经失败行为复现和最小只读OS核验修复。最终Standards/Spec复审无剩余可行动发现，独立隔离最终全量
1532/1532项、7/7段OK、计数对账一致（711.6s），02已验收。
见 [02交付记录](../design/elapsed-02-delivery-2026-10-08.md)。
03重开/接管/正常关窗完整跨入口矩阵未授权实施，父spec及#elapsed整项仍不关闭。
#prep-owner及其他挂起候选保持原状态；本票只把原执行检查改为计时事件，并只读解释身份，
不改变准备状态、停止超时、轮次终态或进程归属模型。


## 2026-10-08：elapsed 03 已验收，整项完成

用户随后授权03，重开/CLI接管/正常关窗与历史结果矩阵新增12项全部通过；03没有新增生产故障，保留01/02已正确实现。Standards/Spec双轴各0项可行动发现，03独立隔离全量1544/1544项、7/7段OK、计数对账一致（701.5s），退出0、不带-f。
#elapsed 已实施并完成约定自动化验收；轮次持有采集执行累计，GUI与views只消费保存事实，旧未知不反推。决策见 [ADR-0051](0051-round-elapsed-owned-by-collection-execution.md)，定义、证据、处置与现场限制见 [03整项交付记录](../design/elapsed-03-delivery-2026-10-08.md)。
原生Windows关窗、真实强杀/崩溃/掉电未做现场验收，不把adapter覆盖说成现场已验证。#prep-owner、#page-contract、其余挂起候选及附录保持原状态；仅保留现有准备后计时/执行结束最小衔接，未改变状态模型、超时和进程归属。归档HTML未修改。

## 2026-10-09：prep-owner 设计确认与工单发布（未实施）

用户授权恢复 `#prep-owner`，按 grill-with-docs → to-spec → to-tickets → implement 推进。
Q1–Q8 及最终运行态/interface、新测试 seam 均已确认；grill、to-spec、to-tickets 已完成，01→02→03粒度与依赖获批。
已发布本机 tracker spec及三张独立工单，正式[规格](../design/prep-owner-spec-2026-10-09.md)与[工单存档](../design/prep-owner-tickets-2026-10-09.md)随本次授权提交；未修改生产实现，实施范围尚未授权。
基线公开 interface 诊断 12 项：6 个通过对照、6 个行为断言失败，0 error；这是旧实现的复现证据，
不是交付验收或生产事故记录。记录见 [prep-owner 访谈与取证](../design/prep-owner-interview-2026-10-09.md)。
`#page-contract` 及其余候选状态不变；不重开已完成的 `#elapsed`、handled-predicate 或店铺阶段。
归档 HTML 未修改。

## 2026-10-10：prep-owner 01 实施并验收（02/03 未授权）

用户授权仅实施 01（「接手 prep-owner 票01 实施，范围仅票01」）。统一准备接纳、30 秒单调预算、
按运行等待与「先落定后扫描」编排已收进新 module `bestseller_monitor/preparation.py`；
Api/main/views 改为消费同一份不可变快照，main 的私有线程、Event 与预算编排删除。
start_run 的确认期收紧、当前结果/历史/闸门页语义及跨入口矩阵属 02/03，未实施。

验收：独立只读双轴审查（Standards 0 硬违规、Spec 0 缺主项/0 scope creep；整改后增量复审
双轴通过）；deletion test 通过（调用方零残留、深模块成立）；隔离副本分段全量 1972/1972 项、
7/7 段 OK、计数对账一致（1103.2s、退出码 0、不带 -f）。受测快照 `563dfaf`（分支
`claude/prep-owner-01`，尚未并入 main）。交付记录见
[01 交付记录](../design/prep-owner-01-delivery-2026-10-10.md)，逐项证据在
`.scratch/logs/tickets/prep-owner/01/20261010-implement/`。

`#prep-owner` 整项不关闭（02/03 未授权、未实施）；`#page-contract` 及其余挂起候选状态不变；
归档 HTML 未修改。

## 2026-10-10：prep-owner 02 实施并验收（03 未授权）

用户授权仅实施 02（「接手 prep-owner 票02 实施，范围仅票02」）。「确认期不许开轮」与
「当前结局、历史结果、开轮闸门」的解释统一收进 `plan_step.start_plan` / `StartPlan`
（`history` 只喂原有跨周提示；`locked` / `lock_reason` 供开始页与 `start_run` 共用同一判定）；
`start_run` 确认期内与开始页一致拒绝、预算后按本地计划降级、无则守闸门；`views.start_view`
只转交事实，JSON 键形状不变。旧成功回填与调用方重复解释结局/预算的知识删除（deletion test
通过）。模式切换/迟到扫描跨入口矩阵与 elapsed/关窗整项回归（03）未实施。

验收：独立只读双轴审查（Standards 0 硬违规、2 条判断题保留；Spec 0 scope creep，1 实缺 +
1 次要全部整改，增量复审双轴通过）；deletion test 通过；隔离副本分段全量 **1986/1986** 项、
7/7 段 OK、计数对账一致（1014.4s、退出码 0、不带 -f）。受测快照 `eacf17f`（分支
`claude/prep-owner-01`，尚未并入 main）。交付记录见
[02 交付记录](../design/prep-owner-02-delivery-2026-10-10.md)，逐项证据在
`.scratch/logs/tickets/prep-owner/02/20261010-implement/`。

`#prep-owner` 整项不关闭（03 未授权、未实施）；`#page-contract` 及其余挂起候选状态不变；
归档 HTML 未修改。

## 2026-10-10：prep-owner 03 实施并验收，整项完成

用户授权仅实施 03（「接手 prep-owner 票03 实施，范围仅票03」，本线最后一票含整项验收）。
03 以验证为主：跨入口矩阵测试钉住迟到工作归属（A 落定扫描未完可接纳 B，A 的等待/扫描/
完成只对 A）、缺口扫描阻塞/异常、落定后切试采（旧扫描不改写当前取数）、试采三个准备入口
零正式副作用与回正式的跨周解释、main 在预算耗尽与异常路径上的建窗与页面/start_run 一致；
elapsed（准备后起算、同轮/新轮边界）与确认关窗、停止监督回归通过。矩阵即绿，03 无新增
生产故障，保留 01/02 已正确实现；生产代码零改动。

验收：独立只读双轴审查（Standards 首轮 0 硬违规、判断题整改后增量复审全部闭合；
Spec 首轮 0 缺失整项/0 scope creep、整改后增量复审闭合）；deletion test 通过；
隔离副本分段全量 1991/1991 项、7/7 段 OK、计数对账一致（1000.9s、退出码 0、不带 -f）。
受测快照 `13ce197`（分支 `claude/prep-owner-01`，尚未并入 main）。交付记录见
[03 交付记录](../design/prep-owner-03-delivery-2026-10-10.md)，逐项证据在
`.scratch/logs/tickets/prep-owner/03/20261010-implement/`。

`#prep-owner` 整项完成（三票验收均成立）；`#page-contract` 及其余挂起候选状态不变；
归档 HTML 未修改。
