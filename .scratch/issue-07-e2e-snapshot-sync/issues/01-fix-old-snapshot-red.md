# 01 固定旧快照红证据并建立保存后同步辅助

Status: resolved
Depends on: none

## 目标
稳定固定“页面先取得旧分析快照，测试随后直接修改并保存后台状态，页面继续使用旧 DOM”的红证据，并在测试侧建立最小、可复用的保存后同步辅助。此工单只处理测试同步接缝，不修改产品分析逻辑、数据库或 API。

## 范围
- 保留测试直接调用后台 fixture/API 完成编辑、确认和保存。
- 在保存返回处独立核对分析 ID、日期区间、成员、confirmed 状态、dirty=false 与 saved_at/等价完成信号。
- 保存前监听本次刷新产生的同分析快照 GET；主动 reload 同一分析；捕获并核对刷新返回的快照。
- 核对刷新后页面实际呈现的分组、确认状态、销量和结果页门禁。
- 为完整业务验收和专门旧快照回归提供有限范围测试辅助函数。

## 验收标准
1. 在稳定时序下，页面持有旧快照时后台保存确认状态，旧断言稳定失败，且失败信息能区分后台已保存、页面未刷新和结果页门禁未通过。
2. 正确同步路径能证明 S（保存返回）、R（刷新后的同分析 GET）、U（页面呈现）属于同一次分析且状态一致。
3. 辅助函数不执行保存、不循环刷新、不固定 sleep、不盲目重试，也不吞掉原有“畅销品”断言。
4. 只修改测试及必要的测试文档/证据文件；不得改变产品语义。

## 非目标
- 不把产品页面改成自动轮询、推送或 revision 同步。
- 不用自然运行通过作为红绿证据。
- 不删除或放宽结果页断言。

## 证据
证据写入主仓 `.scratch/logs/tickets/issue-07-e2e-snapshot-sync/01/`，记录红场景、同步场景及判定。

## Comments

- 2026-10-09：票 01 在隔离副本收口。红：AC01 受控重放 ×4 稳定红（原用例重放 ×2 ＋ 最小差异 ×2；三项区分——后台已保存 / 页面未刷新 / 结果页门禁未通过——全真）；绿：新回归用例 ×2 OK；邻接回归（原 e2e 用例单跑、双模块组载、`test_analysis_reuse` 全文件）OK。提交 `b1c6ae6`（副本分支 `claude/issue-07-e2e-snapshot-sync`，仅 `tests/snapshot_sync.py` 与 `tests/test_e2e_snapshot_sync.py` 两个新文件；产品代码零改动；未 push、未合 main）。证据与判定：`.scratch/logs/tickets/issue-07-e2e-snapshot-sync/01/20261009-t01-01/`（verdict.md 为判定全文，red_replay.py 为保留的红能力重放方法）。未执行项：分段全量 / 双维审查 / 原 m2 复验归票 04；完整场景接入归票 02；正常 UI 对照与对抗性反例归票 03。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`
- 隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\issue-07-e2e-snapshot-sync\01\bestseller`（git worktree，分支 `claude/issue-07-e2e-snapshot-sync`）
- 来源：来源提交 `405c6a7`；副本分支提交 `b1c6ae6`（本票两个新测试文件）；测试配置由副本已追踪 examples 创建（登记见 `sandbox.md`）
- 验收证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\issue-07-e2e-snapshot-sync\01\20261009-t01-01\`（红 ×4、绿 ×2、邻接 ×3、verdict.md）；分段全量未在本票执行（归票 04）
- 永久保留：上列证据目录全部日志、报告、重放脚本 `red_replay.py`（红能力以保留重放方法＋原始日志证明）
- 可删除候选：无（副本为票 01–04 共享树，随全线下线时统一处置）
- 暂留待确认：无
- 实际清理：未执行
