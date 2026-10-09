# 04 清理竞态并完成回归、审查与原环境复验记录

Status: resolved
Depends on: 02, 03

## 目标
收口 issue 07 的测试竞态修复，清理隐式时序依赖，完成目标回归、分段全量和 Standards/Spec 审查；只据实际验收结果更新台账，不因自然测试通过而关闭 issue。

## 验收标准
- 清理自然竞态、隐式等待和无理由固定 sleep；保留明确的 S/R/U 同步契约及“畅销品”断言。
- 日常目标测试通过；隔离副本中执行 `python tools/run_full_suite.py`，显式将日志写入主仓 `.scratch/logs/tickets/issue-07-e2e-snapshot-sync/04/`。
- `summary.txt` 记录全量合计、分段对账和结论；段并集与加载口径一致。
- 完成 code-review 的 Standards 与 Spec 两个维度，并记录发现及处理结论。
- 单独记录原始 m2/真实环境是否复验；未实际验收的部分不得标记完成或关闭 issue 07。
- 记录隔离副本路径、证据路径及保留/可删除/暂留待确认清单；不清理无关工作区改动。

## 阻塞
依赖 02、03 完成。

## 非目标
不修改无关分析服务、数据库或 UI 逻辑；不把测试竞态证据表述为线上产品缺陷。

## Comments

- 2026-10-10：票 04 在隔离副本收口（全线收尾）。AC17 静态检查通过：过 `405c6a7..f0146b1` 全 diff，无 `time.sleep`、无无条件超时等待、无盲目刷新/重跑、无吞断言、无删结果页断言、无产品/无关改动；两处墙钟等待（`wait_for_timeout(25)` 轮询间隔、`held_event.wait(5)` 事件等待）均有界且条件式（定性见 04 证据 verdict.md 第一节）。目标回归四趟全绿（票 02 目标用例、`test_analysis_reuse` 10/10、票 01 回归、票 03 反例 6/6）。分段全量按收尾口径（不加 `-f`，`--log-dir` 显式写主仓 04 证据目录）：**合计 1702 项（预期 1702，计数对账一致）、总耗时 1063.7s、7/7 段 OK、退出码 0**（`summary.txt`；基线 1695＋本线净增 7）。Standards/Spec 双维 code-review 完成（两轴并行只读子代理，报告落 04 证据目录）：Standards 无文档标准硬违规（3 项判断性 smell＋1 备注，记录备将来）；Spec 忠实实现规格与票 01–03 AC 映射（1 项轻微部分实现，记录不整改）；**两轴均无阻塞 → 未触发整改，`f0146b1` 即最终修订，分段全量证据有效**。证据：`.scratch/logs/tickets/issue-07-e2e-snapshot-sync/04/20261010-t04-01/`（verdict.md、summary.txt、seg-01..07 日志、green-*、code-review-{standards,spec}.md、ac17-*）。**未执行项**：原 m2/真实环境复验——单独记录为**未执行**（本机不可替代；判法＝在 m2 按原包条件复跑目标用例/分段全量并记录环境版本）；主仓集成——未执行（另行授权；本线未 push、未合 main）。**issue 07 保持「未处理」**（台账已按 08/11 先例补进展并保持未处理）。附带遗留（原包「交换台锁致原生弹窗、整段阻塞」）仍在，不因本机通过自动关闭。

- 2026-10-10（集成补记）：本线分支并入 main——无冲突合并 `5632cc9`（父 `b66661c`；当日 issue 06 线的合并在先落定），仅本线 4 个测试文件入树（+741/−2）、与 `f0146b1` 逐字节一致（`git diff f0146b1 5632cc9 -- 4 文件` 为空），未推送。合并后 main 的分段全量验证在隔离副本执行，证据 `.scratch/logs/tickets/merge-issue-07-into-main/01/`（与 issue 06 线全量错峰；结果随后补记）。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`
- 隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\issue-07-e2e-snapshot-sync\01\bestseller`（git worktree，分支 `claude/issue-07-e2e-snapshot-sync`，顶点 `f0146b1`；票 01–04 共用这一棵树）
- 来源：来源提交 `405c6a7`；副本分支三次提交 `b1c6ae6` → `449608b` → `f0146b1`；测试配置由副本已追踪 examples 创建（登记见 `sandbox.md`）
- 验收证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\issue-07-e2e-snapshot-sync\04\20261010-t04-01\`（`summary.txt`「合计 1702 项（预期 1702，计数对账一致）… 7/7 段 OK，结论 OK」、逐段日志、目标回归与 AC17 日志、双维审查报告、`verdict.md`）；前半段证据在 `01\20261009-t01-01\`、`02\20261009-t02-01\`、`03\20261010-t03-01\`
- 永久保留：上列四个证据目录全部内容（含 `red_replay.py` 等保留重放脚本——红能力以「保留重放方法＋原始日志」证明）；审查任务文件 `.scratch\work\tasks\issue07-t04-review-standards.claimed\`、`…-spec.claimed\`
- 可删除候选：`…\issue-07-e2e-snapshot-sync\01\bestseller` 整树（登记为候选；分支合并/交付决定后再议，实际删除需授权并按规范 §5 核验边界与保留材料）
- 暂留待确认：无
- 实际清理：未执行
