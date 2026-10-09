# 票 02 双轴审查与修复闭环（2026-10-09）

- 固定前置：`dbdb3180d8ba35f4b8cc4adb5ef9a7c426800c8a`（完整票 01）。
- 首轮候选：`7e65abfcb5645195b672e9b281756ed26a02d375`；复审候选：`6c3d90791431ebff77302ae722d73be3249f1f55`。
- 范围仅票 02；不含主仓集成、03–07、父 issue 关闭。两轮并行只读 Standards / Spec，派发前实施仓干净，最小上下文、文件交接，不修改源码或执行 Git 写操作。
- 原始报告/任务永久证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\02\20261009-ticket02\review-r{1,2}-{standards,spec}.md`，任务另存 `*-task.md`。

## Standards

首轮无发现。复审原文如下（标题轻整）：

### Standards 复审报告

范围：固定 diff `dbdb3180d8ba35f4b8cc4adb5ef9a7c426800c8a...6c3d90791431ebff77302ae722d73be3249f1f55`。只读仓 HEAD 与候选一致，读取时工作区干净。核对 AGENTS、CONTEXT、领域文档、隔离副本规范、关联 ADR、交付记录及身份生命周期测试；不执行 Spec 轴重排。

#### 结论

**无可行动的新增 Standards 发现。**

- ADR-0020 的 adapter / 共享详情访问 / guard 职责边界保留：导航方交访问意图，guard 仍自行读取页面证据；未恢复 ADR-0029 已删除的响应流布尔通道。判定与解除继续共用 `_PageEvidence`，符合 ADR-0030 的同源原则。新增业务可用性、读取失败及身份等待语义已在 CONTEXT 与交付说明明确记录。
- 身份超时接入既有终态枚举、停止分类表、公开视图与导出说明，没有另造停止或关窗机制；超时分类与浏览器欠账分别保留，未以终态冒充安全收尾。文档与证据落点遵循运行时/设计时及隔离副本规范；最终全量尚待记录，交付说明没有提前宣布完成。

#### 首轮 P1 闭环（静态核对）

`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\browser_pw.py` 将重定向前商品目标交给 `OpenedDetail`；`listing.py` 传原榜单目标；`detail_visit.py` 将意图交至 guard。`click_listing.py` 点击前冻结链接目标、识别就地地址变化，商品 URL / offer_id 改为 guard 恢复后动态读取。`tests\test_identity_lifecycle.py` 包含详情、榜单、弹窗、就地导航四条恢复回归及另一商品不解除的负例。首轮“以验证入口冻结原目标”的机制已在这些可取得原目标的生产路径上修正，未发现新增职责冲突。

Fowler smell 仅作判断：目标 URL 的少量跨层传递是必要访问意图，不足以认定新增 Data Clumps / Shotgun Surgery；不据此提出无依据重构。

#### 未验证

本次未运行测试、全量、真实账号或原生人工操作；四条回归使用离线 DOM / History API 地址变化，不等于验证过真实 HTTP 重定向或部署机站点 DOM。无原目标链接时的保守超时回退仍不由此报告保证恢复。最终独立全量与票 07 联合验收由主 agent 另行确认；未等待兄弟，也未修改源码或提交。

## Spec

首轮 1 项 P1：以已重定向的验证 URL 冻结业务目标，恢复原业务页面仍被拒绝至自动超时中止。已逐行为红→绿修复至复审候选；主 agent 实际执行四项新回归，详见 `44`/`45`、`47`/`48`、`50`/`51`、`53`/`54` 日志。初始重定向/编码/弹窗超时夹具错误另记，不冒充产品红灯。复审原文如下（标题轻整）：

### Spec 复审报告

范围：固定基线 `dbdb3180d8ba35f4b8cc4adb5ef9a7c426800c8a` → 候选 `6c3d90791431ebff77302ae722d73be3249f1f55`，核对完整增量、工单、联合 spec、确认决策、交付记录及生命周期测试。检查时 HEAD 为候选且工作区干净。仅静态只读审查；未运行测试、全量或真实账号验收。

#### 结论

**无新增可行动发现。** 未确认新增缺失/部分需求、超范围实现或错误契约。首轮 P1 在本候选中已形成静态闭环；这不是声称新增回归已由本审查实际执行。

#### 首轮 P1 闭环

需求 08-A05 / spec §4：“恢复到当前访问目标可用业务页面”，并继续原轮。

- `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\browser_pw.py:345` 将导航前 product_url 交给 OpenedDetail；详情 seam 将访问意图传入 guard，不再只从重定向后的验证地址取目标。
- 同仓 `bestseller_monitor\listing.py:249` 传入请求榜单 URL；`bestseller_monitor\click_listing.py:413–433` 在点击前取得合法商品链接，恢复后动态读取 card.url / offer_id；就地验证导航也能交给守卫。
- 同仓 `tests\test_identity_lifecycle.py:283–383` 增加逐店详情、榜单、真实弹窗及就地导航回归；另一商品不能解除的负例保留。新回归采用 History API 模拟地址变化，不把它描述为真实 HTTP 302 验收。

#### 其余契约

对照 08-A01–08、08-A15、11-A06/14/16：页面级身份与正向业务证据、未知不解、正式固定截止（含提示/报警耗时）、身份分支不刷新、用户停止优先、唯一专属超时终态、先提交终态不被覆盖、终态写入失败传播、收尾 UNKNOWN 保留确切绑定与新建门禁，以及公开页面/结果/导出说明均有对应实现与测试文本。其他类型仍走原刷新分支及普通 InterventionTimeout 暂停分类；未见将全部 RoundPauseRequired 改成中止。

#### 未验证边界

最终独立分段全量、部署机动态验证/真实账号与原生人工链路未由本审查验证；不等待其他任务，不据交付记录宣称这些通过。票03–07、主仓集成及父事项结案不属于本次结论。

## 双轴汇总

Standards：首轮 0、复审 0，无未关闭发现。Spec：首轮 1（最高 P1）、修复并复审关闭，复审新增 0、未关闭 0。审查为静态只读；独立全量和真实离线回放由主 agent 另行执行与记录，不能用审查代替测试或票 07 联合验收。

主 agent 自动验收最终结果：本票独立分段全量 1565/1565、7/7 段 OK、退出 0，证据 full-suite-02/summary.txt。身份生命周期 28 项及真实原件新回放通过。仅后续收尾文档变更，不再改变本候选的运行代码；部署机/账号人工验收仍未进行。
