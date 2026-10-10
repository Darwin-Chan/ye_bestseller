# prep-owner 03：验证模式切换、迟到扫描与跨入口兼容交付

日期：2026-10-10（Asia/Shanghai）。范围仅 03；本线最后一票，含整项验收。
分支 `claude/prep-owner-01`（沿用票01 的 worktree 副本）；基点 `79fbc04`（票02 顶点，
未并入 main）；受测快照 `13ce197`（其后仅本文件与 ADR-0047 的文档提交）。

## 交付内容与验收矩阵

03 以验证为主：把 01/02 已落地的机制在**跨入口矩阵**上钉住，生产代码零改动
（全部即绿，同 #elapsed 03 形状：没有新增生产故障，保留 01/02 已正确实现）。
新增 5 条用例（`tests/test_gui.py`）；01/02 落下的公开面用例共同覆盖：

1. A 落定后 B 启动；A 的两种等待/扫描/完成只归 A；不误等 B 预算、不返回 B 结果、不清 B 状态
   - 新：`PrepContextIsolationTests.test_late_work_never_answers_for_the_run_that_follows`
     （A 扫描卡住时接纳 B；开窗等待者加入 B、在 B 的预算内返回、拿 B 的快照；A 迟到
     扫描/完成不改写 B——`prepare_running`/`plan_waiting` 仍指 B）
   - 既有：`test_preparation.RunIdentityTests.test_a_late_scan_and_finish_stay_with_their_own_run`；
     `GuiPlanWiringTests.test_prepare_for_window_joins_an_existing_run`
2. 扫描阻塞仍可建窗；扫描异常不撤销结局；落定后切试采，旧扫描用原正式连接、不写当前事实
   - 新：`PrepContextIsolationTests.test_a_late_scan_after_a_mode_switch_never_writes_the_current_facts`；
     `GuiPlanWiringTests.test_a_failed_gap_scan_does_not_revoke_the_page_confirmation`
   - 既有：`PrepContextIsolationTests.test_the_window_waits_for_the_preparation_to_settle`、
     `test_a_late_scan_keeps_the_original_connection_capability`（D05）；
     `test_preparation.test_a_scan_failure_does_not_revoke_the_settled_result`
3. 试采零正式连接/Git/计划副作用；回正式恢复原事实并按跨周规则解释、不自动准备；新上下文不复用
   - 新：`PrepContextIsolationTests.test_the_trial_entries_leave_no_formal_side_effects_and_the_return_reads_cross_week`
   - 既有：`PrepContextIsolationTests.test_returning_to_the_formal_context_restores_the_same_preparation_facts`；
     `test_local_trial_gui.test_explicit_trial_has_one_isolated_context_on_all_three_pages`
4. 结合 02 的异常/超时/历史路径：main、UiState、开始页、start_run 一致（Api/main 集成）
   - 新：`PrepContextIsolationTests.test_the_window_opens_when_the_budget_runs_out_and_the_page_follows_the_settlement`
   - 既有（票02）：`test_an_old_confirmation_does_not_survive_a_failed_retry`（D04）、
     `test_a_timed_out_preparation_is_consumed_when_it_late_settles`、
     `test_a_failed_attempt_can_be_retried_and_the_new_outcome_is_consumed`、
     `test_the_cross_week_hint_survives_a_failed_retry`、
     `test_start_run_is_refused_while_the_preparation_is_confirming`（D06）、
     `test_start_run_keeps_the_gate_after_the_budget_without_a_local_plan`（C03）；
     main 异常：`PrepContextIsolationTests.test_the_window_still_opens_when_the_preparation_fails`
5. elapsed 准备后起算、同轮/新轮边界、确认关窗/停止监督回归 —— 既有钉桩
   （`test_elapsed.test_single_segment_saves_checkpoints_and_freezes_final_value_on_reload`、
   `test_common_collection_entry_excludes_preparation_and_gui_reload_uses_final_fact`、
   `test_elapsed_cross_entry` 12、`test_elapsed_lifecycle` 14、`test_crawler_close` 51、
   `test_abort_new_round` 39、`test_leftover_rounds` 26、`test_explicit_pause_resume` 16、
   `test_paused_manual_login` 13、`test_manual_edge` 21）本票日常全绿，收尾全量在副本内全跑
6. deletion test：证据 `deletion-test.txt`（gui/views 零预算/线程/结局解释残留、tests 零私有
   准备访问；按 13 个调用点论证删除 preparation/StartPlan 会重新散开）
7. 双轴审查：证据 `review-standards.md` / `review-spec.md`（首轮 + 整改 `13ce197` + 增量复审）
8. 整项验收：ADR-0047 追加 #prep-owner 终态（三票验收均成立），关闭记录含全部相关副本/证据
   处置清单

## 修改与提交

生产：无（验证票）。
测试：`tests/test_gui.py`（+5 用例；复审整改含共享 helper：`running_main` contextmanager、
`store_plan_week`）。

提交（分支内，未并入 main）：`d18c17f`（矩阵测试）→ `13ce197`（双轴复审整改，受测快照）
→ 本文档与 ADR-0047 的文档提交（紧随其后，提交号见工单关闭记录）。

## 验收记录

日常单文件（副本内，全绿）：test_gui 87、test_preparation 18、test_plan_step 60、
test_views 28、test_local_trial_gui 20、test_local_trial_exchange 2、test_elapsed 9、
test_elapsed_cross_entry 12、test_elapsed_lifecycle 14、test_crawler_close 51、
test_abort_new_round 39、test_leftover_rounds 26、test_manual_edge 21、
test_paused_manual_login 13、test_explicit_pause_resume 16、test_ui_live 19、
test_run_cli 17、test_joint_acceptance 4、test_bench_refresh 2。

TDD 记录：5 条新用例先写先跑，全部即绿（行为钉桩；无先红项——本票未发现属本规格的问题，
与 #elapsed 03 同形）。逐条命令与结果见证据 `dev-log.md`。

双轴审查（独立只读子代理，Standards/Spec 并行；任务与报告在证据目录）：

- Standards 首轮：**0 硬违规**；3 条判断题（main 补丁栈/`store_plan` 重复、用例名、措辞）
  ——全部整改（`13ce197`：`running_main`、`store_plan_week`、改名、措辞收窄）；1 条保留
  （`_spawn_crawler` 断言属既有 idiom）。增量复审**全部闭合、无新问题、无过度整改**。
- Spec 首轮：**0 缺失整项、0 scope creep、0 错误断言**；1 条 seam 折中（扫描的「配置/周」
  无公开面观测——见「边界与未做」）＋弱断言/措辞整改（文案收窄、补 1 条直断言）。
  增量复审 2/3/4 闭合、1 条转本记录（即「边界与未做」该行），**无代码新问题**。

deletion test（证据 `deletion-test.txt`，grep 证据 + 删除论证）：gui/views 零预算/线程/等待/
结局解释残留（只剩两处转交与一处类型注解）；tests 中零私有准备状态访问；删掉持有者接口会在
13 个调用点以 D01–D06 的形状重新散开。

收尾分段全量（副本根、`--log-dir` 主仓本票证据、不加 `-f`、退出码 0）：

```text
python tools/run_full_suite.py --log-dir "F:\AI\projects\bestseller\.scratch\logs\tickets\prep-owner\03\20261010-implement"
```

summary.txt 原文（合计/对账/结论）：

```text
test_exchange：72 项，OK，94.1s；test_judgment_set：72 项，OK，54.9s；test_plan_step：60 项，OK，22.5s；test_export：30 项，OK，23.6s；test_git_channel：17 项，OK，10.1s；test_shops_sync：17 项，OK，8.9s；其余段（111 文件）：1723 项，OK，786.8s
合计 1991 项（预期 1991，计数对账一致），总耗时 1000.9s，段结果 7/7 段 OK，结论 OK
```

全量期间工作树未被任何改动：收尾时 `git status` 干净、顶点 `13ce197`，被测试的字节集合与
受测快照一致。摘要绝对路径：
`F:\AI\projects\bestseller\.scratch\logs\tickets\prep-owner\03\20261010-implement\summary.txt`；
逐段日志同目录 `seg-*.log`。

## 边界与未做

- 清单第 2 条「旧扫描仍访问原正式**连接/配置/周**」：只有**连接**被独立观测（D05：`gate.calls`
  与「不打开试采连接」）；配置/周为构造冻结（`Preparation` 固定 `_cfg`，扫描用 `result.week`）
  的结构性保证，公开面无可观测副作用，未单独造观测量——这是 seam 折中，如实记录。
- 不真实等 30 秒；不接真实 Git/数据/浏览器/登录态（业务走真实 `plan_step.prepare_week`，
  计划库走不可达快路径与本地裸远端两种）；adapter 覆盖不等于原生窗口/真实故障现场验收。
- 回归证据以收尾分段全量为准；日常记录只作开发反馈。
- CONTEXT.md 未改：本票不引入新词汇。

## 目录与证据处置

- 主仓：F:\AI\projects\bestseller
- 隔离副本（01/02/03 共用）：F:\AI\projects\bestseller\.scratch\sandboxes\prep-owner\01\20261010-implement\bestseller
  （主仓 worktree，分支 `claude/prep-owner-01`；测试 `config/*.toml`、`shops.csv` 自 example
  复制、`start_browser=false`，开工前重核路径逐项在副本内——`config-path-audit.json`；
  未复制真实库/登录态/主仓真实配置）
- 来源：基点 `79fbc04`（票02 顶点）+ 本票提交至 `13ce197`（受测快照；其后仅文档）；
  收尾时工作树干净
- 验收证据：F:\AI\projects\bestseller\.scratch\logs\tickets\prep-owner\03\20261010-implement
  （summary.txt「合计 1991 项（预期 1991，计数对账一致），总耗时 1000.9s，段结果 7/7 段 OK，
  结论 OK」、seg-01..07 日志、config-path-audit.py/json、dev-log.md、deletion-test.txt、
  review-standards.md、review-spec.md、review-tasks/、draft-delivery.md、delivery-record-copy.md）
- 永久保留：上述证据目录全部材料（不随副本清理）
- 可删除候选：无（00/01/02 材料与其他工单材料不属本票，不列）
- 暂留待确认：副本与分支 `claude/prep-owner-01` 保留待并入 main 或授权清理；
  00 诊断副本与证据（`.scratch\sandboxes\prep-owner\00\`、`.scratch\logs\tickets\prep-owner\00\`）
  按 grill 记录保持原状；01/02 证据保持原状
- 实际清理：未执行（未获授权）
