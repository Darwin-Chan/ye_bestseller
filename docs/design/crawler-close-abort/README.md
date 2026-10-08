# 采集关窗确认与中止：规格及工单归档

- 归档日期：2026-10-08（Asia/Shanghai）。
- 用途：按用户要求，将本会话已确认的规格及三张实施工单保存进版本库，避免仅保留在被 Git 忽略的本地目录。
- 本目录是本次提交时的逐字归档快照，不是新的工单状态源。后续执行和状态更新仍以主仓 `.scratch/crawler-close-abort/` 的本地工单为准；本次未改变 issue tracker 约定或 Git 忽略规则。
- 规格与工单中的本机绝对路径、执行边界、状态和历史记录均按原文保留。快照中的 ready-for-agent 只表示文档已就绪，不表示功能已实现或验收通过。
- 依赖顺序：01 → 02 → 03。本次归档未更新实施状态；01 的开发尚在隔离副本中，未完成审查及收尾验收，不包含在本次文档提交中。

## 归档与本地原件对应

| 本目录快照 | 主仓本地原件 |
| --- | --- |
| spec.md | .scratch/crawler-close-abort/spec.md |
| issues/01-confirm-close-and-wait-for-abort.md | .scratch/crawler-close-abort/issues/01-confirm-close-and-wait-for-abort.md |
| issues/02-confirm-paused-round-and-upgrade-to-abort.md | .scratch/crawler-close-abort/issues/02-confirm-paused-round-and-upgrade-to-abort.md |
| issues/03-cross-session-target-safety-and-final-acceptance.md | .scratch/crawler-close-abort/issues/03-cross-session-target-safety-and-final-acceptance.md |

同一提交另含本会话已确认的需求澄清记录与 ADR-0048。其余会话或需求的文档、主仓既有修改、开发副本、证据日志及真实数据均不包含在本次提交中。

## 分票交付记录

- `01-delivery-2026-10-08.md`：01票实施与验收记录。暂停最终规则由02承接，整体验收由03承接；不将归档票快照视为实时状态。
