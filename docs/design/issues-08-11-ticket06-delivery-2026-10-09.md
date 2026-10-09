# issues-08-11 票 06：异常及跨天遗留明确处置，不冒充暂停

实施与收尾：2026-10-09。范围仅票 06；本记录不关闭 issue 08 或 11，不替代票 07 联合验收。

**当前结论（2026-10-09）**：票 06 实施、双轴审查（含复审整改）与独立隔离分段全量已完成，自动验收通过。最终运行候选为 `f40c2ff31c99539b065829891d68903b04f5465c`（整改后），前置实现候选 `e5bf7d23bfb255215029542f5b5103c1c885527b`。主仓未集成；部署机人工与票 07 联合收尾未执行。

## 实施来源与边界

- 主仓：`F:\AI\projects\bestseller`。
- 实施副本（隔离 clone）：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller`。
- 分支：`codex/issues-08-11`；开工基线（实测核对）：`3b950c7e4096de8071f324028ff8b394442a79d9`（票 05 收尾后，工作区干净）。
- 提交（仅本票 8 个文件，未 amend、未合 main）：
  - `e5bf7d2` feat: dispose of leftover and cross-day rounds explicitly (ticket 06)
  - `f40c2ff` fix: close stub gaps found by the ticket 06 two-axis review
- 联合 spec 版本 1（SHA256 核对与交接一致：86F57192468589B0E6C00C4C254EF2FA6D62A77D252E7CA9D87504931836B2B8）、已确认 Q4/Q10、票 01/03/05 公共接口语义均未改写。

## 交付行为与公共接口

- `rounds.pending_rounds(db)`：全部未结束轮次（新的在前）——开始页逐个处置的数据源。
- `crawler_identity.settle_dead_registration(conn, who)`：残留身份行的显式结清——只在原进程确已不在（`process_alive` False 或创建证明分歧）时动手；OWNED 浏览器只按行内精确绑定关；核不实保留欠账；**从不终止任何进程**。「PID 已被接管」判据统一为 `_taken_over`；删除原语 `delete_registration` 与 `StopWatch._delete_target_in_transaction` 共用一处。
- `gui.Api`：`prepare_abort(round_id)` 新增遗留轮冻结（无活动采集；kind `pending`）；`prepare_cleanup_retry` + `confirm_abort` 的 kind `cleanup` 分支（已终态轮次后仍可重试收尾，只清欠账、不回写终态）；确认执行前重核冻结轮次与当时目标（新目标/新采集/写库失败拒绝或说明重试）；`finish_if_open` 实写目标轮 `ABANDONED`，不以无进程为空操作成功。`CloseSnapshot` docstring 记录 kind 产出方；关窗读取永不返回 `pending`/`cleanup`。
- `views`：开始页列出每个未结束轮的真实状态（「采集已停止，本轮未正常结束」/「已跨天，不能继续」/「已暂停可到过程页继续」）并提供逐个「中止此轮」；`residue` 暴露已终态轮的收尾欠账；过程页按 `state.round_id` 保留跨天明确暂停轮并说明不能继续，未正常结束/跨天不再跳结果页冒充「进行中」，无进程不显示暂停。
- 页面（`ui_live.html`）：`#pendingCard` 逐个「中止此轮（放弃）」；`#residueCard`「重试收尾核验」；两类确认弹窗都说明「不能继续 / 数据保留 / 不会误关日常 Edge」；取消纯客户端、零写入。

## 逐项自动验收映射

新用例全部在 `tests/test_leftover_rounds.py`（19 项初红后绿 + 复审整改 6 项先红后绿，共 25 项）。

| 验收项 | 行为与新用例 | 结果 |
| --- | --- | --- |
| 11-A05 | `test_start_view_lists_each_unfinished_round_with_its_real_state`、`test_run_page_never_treats_a_leftover_as_an_explicit_pause`、`test_start_page_cancel_of_leftover_abort_changes_nothing`：开始页显示「采集已停止，本轮未正常结束」，无普通继续入口，服务端拒绝 | 通过 |
| 11-A06 | 同上 + 03 既有拒绝用例：无明确暂停依据旧轮继续被拒、终态与数据不变 | 通过 |
| 11-A11 | `test_confirmed_leftover_abort_writes_the_target_round`：确认后实际写目标轮 ABANDONED（非空操作成功）、幂等、收尾干净后放行新建 | 通过 |
| 11-A12 | `test_leftover_abort_confirmation_survives_only_unchanged_facts`、`..._refuses_while_a_collection_runs`、`..._keeps_a_terminal_that_landed_after_confirmation`、`..._write_failure_is_retryable_and_blocks_new`、`..._settles_dead_owned_browser_with_exact_proof`、`..._never_clears_unverifiable_browser_debt`、`..._retries_a_failed_browser_close_until_os_proof_settles`、`..._keeps_real_terminal_and_cleanup_notes_together`、`..._exposes_retry_entry_and_settles`、`..._unverifiable_process_with_complete_row_is_explained`、`test_start_page_retry_settles_residue_debt`：冻结指定轮与存在目标、执行前重核、新目标拒绝、写库失败可重试、核不实保留欠账且解释、重试入口闭环、不误关日常 Edge（terminate 零调用、close 只带精确绑定） | 通过 |
| 11-A14 | `test_auto_retained_exits_keep_their_real_terminal_not_user_pause`：自动保留轮按真实终态、历史无终态按「未正常结束」呈现，不标用户暂停/中止、无迁移 | 通过 |
| 11-A15 | `test_cross_day_paused_round_cannot_resume_and_is_shown`、`test_cross_day_paused_shown_even_when_another_leftover_is_newer`、`test_multiple_leftovers_are_disposed_of_one_at_a_time`：跨天不继续、不默默结束、显式中止收尾后才放行新建；自然跨天规则未动 | 通过 |
| 多个遗留逐个 | `test_multiple_leftovers_are_disposed_of_one_at_a_time`：一次授权只处置冻结轮，仍有旧轮时继续拒绝新建 | 通过 |
| 只读无副作用 | `test_read_only_views_never_end_or_start_anything_for_leftovers`：查询不写终态、不起采、不碰浏览器 | 通过 |
| 两类重开回归 | `test_normal_close_reopen_vs_abnormal_exit_reopen`：正常关窗确认中止后重开无旧轮继续、可合法新建；异常退出重开列遗留、显式中止后新建。`test_cancel_close_keeps_a_real_pause_intact`：取消关窗维持原状态 | 通过 |
| 过程页孤儿 | `test_run_page_keeps_an_unfinished_round_explicit_instead_of_a_result`：无进程不显示暂停/继续、显式中止后回结果页 | 通过 |

## TDD、回归与证据

- 主仓证据根：`F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\06\20261009-ticket06-01`。
- 红→绿：`00-red-leftover-rounds.log`（19 项首轮红，11 errors + 5 failures）→ `04-green-leftover-rounds.log`（19 项 OK）；复审整改 `09-red-review-fixes.log`（6 项新用例红）→ `10-progress-review-fixes.log`（25 项 OK）。中间过程日志 01/02 保留。
- 相关单文件回归（`03/06/07/11` 前缀逐文件日志，实现提交与整改提交各一轮）：test_leftover_rounds、test_abort_new_round(39)、test_explicit_pause_resume(16)、test_manual_edge(19)、test_identity_lifecycle(28)、test_crawler_close(51)、test_stop_target(20)、test_crawler_mutex(6)、test_local_trial_gui(20)、test_gui(72)、test_bench_refresh(2)、test_round_wiring(17)、test_round_stop(11)、test_crawler_identity(11)、test_p1(31)、test_run_cli(17)、test_ui_live(19)、test_views(27)、test_paused_manual_login(13)、test_stop_request(22)、test_elapsed(10)、test_click_listing(33)、test_detail(44)、test_pacing(19)、test_local_trial_cli(24)——全部 OK、退出 0。
- compileall 与 `git diff --check` 退出 0（05-compileall.txt；无配置的类型检查器，不宣称类型检查通过）。
- 测试全部离线静音：测试专用配置（machine_id=m-test、alarm=false）、临时 SQLite、真实页面桥网络全拦截；未使用真实账号、未启动真实采集、未关日常 Edge。

## 隔离副本分段全量（收尾口径，不加 -f）

- **最终全量（最终候选 f40c2ff）**：副本 `.scratch/sandboxes/issues-08-11/06/20261009-ticket06-final-02/bestseller`（clone --no-hardlinks，venv 于副本路径重建，config 三件同源复制）：

```text
分段全量测试 2026-10-09 21:56:28
仓库：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\06\20261009-ticket06-final-02\bestseller
传参：python tools/run_full_suite.py --log-dir F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\06\20261009-ticket06-01\full-suite-02

test_exchange：65 项，OK，110.2s；日志 seg-01-test_exchange.log
test_judgment_set：50 项，OK，49.1s；日志 seg-02-test_judgment_set.log
test_plan_step：54 项，OK，31.2s；日志 seg-03-test_plan_step.log
test_export：30 项，OK，30.7s；日志 seg-04-test_export.log
test_git_channel：17 项，OK，12.0s；日志 seg-05-test_git_channel.log
test_shops_sync：17 项，OK，11.6s；日志 seg-06-test_shops_sync.log
其余段（95 文件）：1405 项，OK，602.1s；日志 seg-07-其余段(95_文件).log

合计 1638 项（预期 1638，计数对账一致），总耗时 847.0s，段结果 7/7 段 OK，结论 OK
```

  退出码 0（`full-suite-02-exit-code.txt`）。来源与文件哈希：`12-source-manifest-final.txt`。
- 先行全量（前置候选 e5bf7d2，整改前）：副本 `final-01`，`full-suite-01` 合计 1632 项（预期 1632，对账一致）、7/7 段 OK、退出 0、830.7s。仅作中间记录；最终结论以 final-02 为准。

## 双轴审查（含复审整改）

- 固定基线 `3b950c7...`；两轴独立只读子代理并行，不合并排序。报告：`docs/reviews/issues-08-11-ticket06-code-review-2026-10-09.md`（本分支受跟踪），主仓票证据原文 13/14/15。
- Standards 初审：0 硬性违例；判断项 4 条。Spec 初审：6 项验收其余通过，发现 3 处半做/取轮/覆盖与 2 处方式问题；无范围蔓延。
- 整改（f40c2ff）：删除原语与 `_taken_over` 单点化、docstring 归属更正、收尾重试入口闭环（`prepare_cleanup_retry` + `#residueCard`）、`run_view` 按认领轮取跨天、note 合并、BEGIN 移入 try、核不实给出解释。
- 复审：两轴确认整改落实、无新增硬违例/无残留缺口；未动判断项（文案分载体、拒绝文案沿先例、kind 弦化、守卫前导块 4 份可延后）经复审接受。复审后候选未再改动，全量 final-02 与复审对象同为 f40c2ff。

## 未完成与待人工事项

- 主仓未集成；票 07 联合链路、原场景新回放、联合全量与联合双轴审查未执行。
- 08-H01、11-H01 部署机原生人工验收待用户反馈；未做真实登录、采集、有声测试，未关日常 Edge，未迁移或改写固定历史诊断材料。
- 其余票 02/05 的既有边界不变：真实身份验证页贯穿整链（02×05×06）归票 07。

## 目录与证据处置（关闭前最终记录）

- 主仓：`F:\AI\projects\bestseller`。
- 隔离副本（保留）：
  - 实施副本 `.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller`（分支 codex/issues-08-11，含票 01–06 提交与本路径重建 venv，供后续串行票与人工集成）；
  - 先行全量副本 `.scratch\sandboxes\issues-08-11\06\20261009-ticket06-final-01\bestseller`（HEAD e5bf7d2）；
  - 最终全量副本 `.scratch\sandboxes\issues-08-11\06\20261009-ticket06-final-02\bestseller`（HEAD f40c2ff）。
- 来源：开工基线 3b950c7 + 本票两笔提交；测试配置三件同源复制、相对路径全部落在副本内；无真实账号、数据库、profile、venv 搬运（venv 各副本重建）。
- 验收证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\06\20261009-ticket06-01`（红/绿、逐文件回归、两轮分段全量与 summary、两次审查原文与复验、来源清单）；最终结论行见 `full-suite-02/summary.txt` 与 `full-suite-02-exit-code.txt`（0）。
- 永久保留：上述全部票证据与固定历史诊断材料；报告另在本分支 `docs/reviews/` 受跟踪保存。`.scratch` 不进 Git，跨机器需显式打包。
- 可删除副本候选：无。实际副本清理：未执行。
- 暂留待确认：票 03/05 遗留的三个测试副本与票 01 的两个失败现场副本（见对应票清单）不归本票处置；本票三个副本保留待人工/后续集成核验且用户明确授权后再评估。
