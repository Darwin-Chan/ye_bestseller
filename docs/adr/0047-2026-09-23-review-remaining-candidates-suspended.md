# 0047. 2026-09-23 审查的其余候选挂起（待定暂不处理）

- 状态：已接受（挂起，不是否决）
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
- ② 界面状态：开轮前准备运行态、已抓时长段计时（`#prep-owner` / `#elapsed`）；
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
