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
- 8 条 Worth exploring 的其余各条（`#tx-seam`、`#image-evidence`、`#handled-predicate`、
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
