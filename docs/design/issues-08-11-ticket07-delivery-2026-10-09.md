# issues-08-11 票 07：联合验收、原场景新回放、全量与审查

实施与收尾：2026-10-09。范围仅票 07；本记录不关闭 issue 08 或 11，不替代部署机人工验收。

**当前结论（2026-10-09）**：联合交付（票 01–06）的自动联合验收、原场景新回放、隔离副本分段全量与两轴审查已完成，自动部分通过。最终候选 `fe36da726192321887fdd66542593e6aa7bfb258`（本票提交 `b5af358` 联合验收链、`0e2fee3` 链摘要、`be8fda6` 审查整改、`fe36da7` 复审补强用例；其余 28 个提交为票 01–06）。**主仓未集成**（`main` 现已含 SKU 分组线且超出本分支基点，集成须另行授权并保留 SKU/schema 与 elapsed 接口）；**08-H01、11-H01 部署机人工验收待用户现场反馈**，反馈未收前本票不能声称全部验收通过，issue 08/11 均保持未关闭。

## 实施来源与边界

- 主仓：`F:\AI\projects\bestseller`。实施仓（隔离 clone）：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller`。
- 分支 `codex/issues-08-11`；联合交付固定点 `69066da421b255168884224a20a28fb20b6d4713`（票 01 固定来源）；开工 HEAD `072592a`、工作区干净（实测核对）。
- 联合 spec 版本 1（SHA256 `86F57192468589B0E6C00C4C254EF2FA6D62A77D252E7CA9D87504931836B2B8`，核对一致）、grill 决策、七张工单与 01–06 各票公共接口语义均未改写；本票**无产品代码改动**。
- 本票提交（路径限定、未 amend、未合 main）：
  - `b5af358` test(joint): add J-A01–J-A04 joint acceptance chains (ticket 07)
  - `0e2fee3` test(joint): record TICKET07 chain summaries for the acceptance evidence
  - `be8fda6` fix: gate the manual login entry on unsettled cleanup debt (ticket 07 review)（两轴审查 r1 整改）
  - `fe36da7` test(stop): pin proof-divergence settlement for an unreadable pid (ticket 07 review r2)（复审补强）
  - `bc064fa` docs: ticket07 joint acceptance, replay, full-suite and two-axis review record（本交付与审查记录及其后续补注；仅归档文档，不改已测运行代码——最终全量与回放对象为 `fe36da7`；归档提交自身不再逐一登记）
- 整改后产品代码改动仅两处：`gui.Api._login_refusal` 补「收尾欠账未结清」门禁（spec 行 59）；`crawler_identity.taken_over` 单点化并供 `StopWatch` 浏览器结账复用（判据行为差异：存活读数不可用但创建证明分歧时按已退出结账）。

## 联合验收链（J-A01–J-A04）

新文件 `tests/test_joint_acceptance.py`（4 项）。复用两组已确认高层接缝：真实页面守卫→等待→公共 `pipeline.run_round`；shipped `ui_live.html` 实际按钮事件→真实 `gui.Api`/配置解析/临时 SQLite/停止收尾。只适配浏览器/OS 副作用（专用浏览器启动与进程事实）、时钟与发声；建轮、身份登记与榜单保存按票 03/05 真实调度用例的同一代班手法（`seed_round`）。默认用脱敏 fixture；发布验收联跑时给 `IDENTITY_SOURCE` 指现场原件。

| 链 | 实际执行（断言） | 结果 |
| --- | --- | --- |
| J-A01 | 开始页手动入口（同一解析目录、不建轮不采集）→关闭→真实开始按钮（`--new-round-intent` A/B/C/D + 同一 `--gui-browser-settings` 目录）→现场身份验证页→过程页真实点击暂停（等待中，rProgressLine 显示身份验证与 0.1 分钟）→收尾→过程页手动入口（开着时继续被拒、零导航零 spawn）→关闭→真实继续按钮（`--resume-round-intent` 同轮 ID、A/B/C/D）→真实调度只访问剩余 502/503（A/B/C 与已完成的 501 不重复）→COMPLETED、成员不变 | 通过 |
| J-A02 | 开始→身份验证持续存在（现场 HTML）→0.1 分钟时限→真实等待超时→`IDENTITY_VERIFICATION_TIMEOUT`（数据保留、不访问下一任务：仅 501）→界面「身份验证超时中止」→继续被拒→收尾未结清前新建被拒、`rounds.latest` 不变（无自动下一轮）→收尾结清后**明确**新建（新轮意图 A/B） | 通过 |
| J-A03 | 明确暂停→确认正常关窗→收尾→重开：无旧轮续跑入口、可合法新建（正常关窗不被当暂停保留）；异常退出→重开：「采集已停止，本轮未正常结束」、不给普通恢复、读取不写终态→显式中止实写 ABANDONED → 明确新建（异常退出不被当用户中止） | 通过 |
| J-A04 | 原始条件（A/B/C/D 原轮、前三店当天已采满 30 件、开始页默认只剩 D）：旧开始页路径被拒并给处置说明→过程页明确暂停→暂停后开始页仍不提供换范围直接开始→继续携带原轮完整范围→真实调度只访问剩余 502、handled 92、COMPLETED；无暂停依据的跨天遗留：显示「已跨天，不能继续」、不伪装暂停→显式中止收尾后新建放行 | 通过 |

- 发布验收联跑（同一文件，`IDENTITY_SOURCE` 指现场原件）：4/4 通过（副本内，见下）。
- 链摘要（`08-joint-chains-summaries.txt`，抽取自全量其余段日志）：`TICKET07_JA01_CHAIN`/`JA02_TIMEOUT`/`JA03_REOPEN`/`JA04_FOUR_SHOP` 四行 JSON，记录配置目录、开始/暂停/继续范围、实际导航与终态。
- 四店回放（J-A04 行）：原始条件与新产品意图差异——原现场是「开始页默认 D 勾选当续跑、范围门禁拒绝」（固定历史 `11-original-1.log` 等只读引用）；新流程改为「开始页表达新轮/显式处置，明确暂停后从过程页继续原轮完整范围」。

## 原场景新回放（现场身份验证 HTML）

- 新脚本与清单（主仓设计时输出，不覆盖旧运行）：`.scratch/tool-output/issues-08-11/07/20261009-ticket07-01/identity_replay_ticket07.py`、`manifest.json`；历史固定脚本/清单/active-run/副本/日志均只读保留（哈希凭证 `09-fixed-history-unchanged.txt`）。
- 结果：`.scratch/logs/tickets/issues-08-11/07/20261009-ticket07-01/07-identity-replay.json`（原始日志 `07-identity-replay.log`，退出 0；整改前候选 0e2fee3 的同口径记录另存 `07-identity-replay-01.json/.log`）。要点：
  - 待测来源：副本提交 `fe36da7`、未提交覆盖为空；guard SHA-256 `0266bc18…f19b1`（与票 02 复审后读数一致）、waiting/pipeline 哈希同录。
  - 输入：现场 HTML SHA-256 `249fbbad…2b7`（与历史记录逐位一致）、普通商品 URL `https://detail.1688.com/offer/521844920352.html`、禁脚本（`java_script_enabled=False`）、全网络拦截（文档导航供现场 HTML，其余 6 个请求 abort）。
  - 真实读数：正文 251 字、强特征提示在场、`captcha_visible=false`、`intervention_kind=身份验证`（`vtype=identity`）、`resolved=false`（未解除）。
  - 等待链：真实 `guard.wait_for_resolution`——`verification_appear`(identity) → 固定截止（2 秒确认 + 6 秒正式等待）→ `IdentityVerificationTimeout`，无 `verification_solved`，期间 1 次导航请求（不自动刷新）。
  - 终态：同一页面进入公共 `pipeline.run_round` → 轮次写 `IDENTITY_VERIFICATION_TIMEOUT`、不可续跑、discovered=1、访问止于当前任务（无下一任务/店铺）。
- 历史对照：原始 08-dom.log 为 verdict=null 的历史红（固定基线 75b1329）；本次在联合候选上同一输入判为身份验证并走完整超时中止链。

## 逐项自动验收索引（08-A01–08-A15、11-A01–11-A16）

「当前来源」＝本轮在最终候选 `fe36da7` 上实际运行（最终全量 `full-suite-02`；审查整改前另有先行全量 `full-suite-01`，对象 `0e2fee3`）：联合链与回放（上文）、或全量运行器其余段日志 `full-suite-02/seg-07-其余段(96_文件).log` 中实际执行的用例（合计 1644/1644、7/7 段 OK、退出 0）。「票证据」＝各票红→绿/审查/单票全量的原始证据根（`…\issues-08-11\<票号>\<run>\`），仅作来源链与历史对照——**本次通过以最终候选上的本轮运行与全量重跑为准**。

| 验收项 | 行为（摘要） | 本轮运行证据（当前来源） | 票来源证据 |
| --- | --- | --- | --- |
| 08-A01 | 现场 HTML/普通 URL/captcha=false → identity 且未解除、非滑块 | 回放读数（251 字/captcha false/identity/resolved false）+ J-A01/J-A02 联跑 + `test_identity_lifecycle.test_archived_identity_at_product_url_without_captcha_is_not_resolved`（seg-07） | 02/20261009-ticket02（38/57/58 清单与回放） |
| 08-A02 | 确认后真正等待、发 identity 事件、不自动刷新 | `test_confirmed_identity_waits_without_reload_then_emits_solution`、`test_shipped_page_shows_actual_identity_wait_and_user_pause_works`（seg-07）+ J-A01（过程页显示类型与时限）+ 回放 wait 段（appear→timeout、1 次导航） | 02/20261009-ticket02 |
| 08-A03 | 加载/读失败/关闭不解、未知安全停止 | `test_unknown_or_closed_page_never_proves_resolution`、`test_business_empty_result_transient_signal_and_read_failures`、`test_nonempty_unknown_page_is_not_initially_usable`、`test_identity_form_cannot_release_even_if_prompt_disappears`（seg-07） | 02/20261009-ticket02 |
| 08-A04 | 换类型不重置截止、不发 solved | `test_type_changes_and_unknown_keep_one_deadline_including_notice_cost`、`test_alarm_overhead_cannot_solve_after_frozen_deadline`（seg-07） | 02/20261009-ticket02 |
| 08-A05 | 可靠恢复发解决、继续剩余任务 | `test_reliable_restore_finishes_remaining_tasks_in_original_round`、`test_redirected_identity_restores_original_target_and_finishes_round`（seg-07）+ J-A01 继续段（502/503 实际执行） | 02/20261009-ticket02 |
| 08-A06 / 11-A06 | 超时专属终态、不访问下一任务、不可续跑、无自动新建 | J-A02（含收尾前拒绝新建、无自动下一轮）+ 回放 terminal 段 + `test_real_wait_timeout_ends_round_preserves_data_and_never_visits_next_task`（seg-07） | 02/20261009-ticket02 |
| 08-A07 | 等待中暂停/中止/并发终态；取消点有效 | `test_user_pause_during_wait_wins_over_later_business_restoration`、`test_confirmed_close_during_identity_wait_abandons_not_solves`、`test_cancelled_close_during_identity_wait_keeps_original_wait`、`test_first_committed_terminal_wins_timeout_race`（seg-07）+ J-A01 | 02/20261009-ticket02 |
| 08-A08 | 正常页/空结果/描述引用/装饰 URL/反馈 deny 负例 | `test_normal_business_quote_and_decorated_url_are_not_identity`、`test_feedback_and_deny_do_not_become_identity_or_resolved`、`test_business_empty_result_transient_signal_and_read_failures`（seg-07） | 02/20261009-ticket02 |
| 08-A09 | 手动入口不采集不建轮、不宣称登录 | `test_page_login_does_not_collect_or_create_round`（seg-07）+ J-A01 第一段（pending_rounds 空、无身份行、无「账号已登录」） | 04/20261009-ticket04-01 |
| 08-A10 | 同一解析目录（迁移根/空格非 ASCII）；配置不一致拒绝 | `test_moved_root_and_changed_cwd_use_one_resolved_contract`、`test_exe_location_resolves_migrated_deployment_before_page_event`、`test_cli_rechecks_frozen_config_without_creating_round`、`test_profile_changed_after_gui_precheck_is_rejected_under_crawler_lock`（seg-07）+ J-A01（settings 目录 == 手动入口目录）+ 票 05 配置变化拒绝用例（seg-07） | 04/20261009-ticket04-01、05/20261009-ticket05-01 |
| 08-A11 | 重复/并发单次启动、占用提示、开始/继续拒绝 | `test_repeated_concurrent_open_and_read_only_refresh`、`test_opening_is_observable_and_concurrent_clicks_do_not_launch_twice`、`test_unconfirmed_creation_does_not_allow_second_launch_or_collection`、`test_unknown_browser_with_same_profile_blocks_public_open_and_start`、`test_continue_is_refused_while_the_manual_window_holds_the_profile`（seg-07）+ J-A01（占用时 continue_gate） | 04/20261009-ticket04-01、05/20261009-ticket05-01 |
| 08-A12 | 各门禁（采集/等待/停止/收尾/关闭/角色/未知）拒绝 | `test_collecting_and_stop_cleanup_reject_api_and_page`、`test_unknown_permission_and_missing_edge_refuse_api_and_page`、`test_role_disallows_entry_api_and_page`、`test_manual_login_entry_opens_only_after_an_explicit_pause_cleanup`（seg-07）+ J-A02（收尾/超时轮门禁） | 04/20261009-ticket04-01、05/20261009-ticket05-01 |
| 08-A13 | 关 GUI 不关手动窗口、不碰日常 Edge/无关端口、重开重观察 | `test_gui_close_does_not_close_manual_window`、`test_os_profile_and_unrelated_port_are_not_taken_over`、`test_daily_edge_profile_is_rejected_without_query_or_launch`、`test_gui_cdp_creation_mismatch_never_uses_or_closes_foreign_browser`、`test_borrowed_user_browser_is_never_closed_by_abort_cleanup`（seg-07） | 04/20261009-ticket04-01（票 05 判定为继承覆盖，不冒充新证据） |
| 08-A14 | 缺 Edge/坏配置/外部模式准确拒绝 | `test_unknown_permission_and_missing_edge_refuse_api_and_page`、`test_false_mode_and_invalid_config_have_accurate_errors`、`test_external_mode_and_invalid_config_refuse_login_and_continue`（seg-07） | 04/20261009-ticket04-01、05/20261009-ticket05-01 |
| 08-A15 | 清理/写终态失败不冒充成功、安全重试 | `test_failed_timeout_terminal_write_never_claims_success_or_starts_new`、`test_browser_cleanup_failure_cannot_hide_real_timeout_or_release_gate`、`test_owned_cleanup_failure_retains_exact_process_binding`、`test_elapsed_cleanup_error_cannot_replace_identity_timeout`、`test_leftover_abort_confirmation_write_failure_is_retryable_and_blocks_new`、`test_start_page_retry_settles_residue_debt`（seg-07） | 02/20261009-ticket02、06/20261009-ticket06-01 |
| 08-H01 | 部署机双击/手动登录/有效登录态复用 | **待用户现场反馈**（步骤见下） | — |
| 11-A01 | 暂停保留原轮 ID/范围/进度、停止中拒绝继续、实际继续 | `test_page_pause_cleanup_then_continue_carries_frozen_complete_round`、`test_continue_is_refused_while_the_pause_is_still_stopping`、`test_manual_login_then_explicit_continue_reaches_real_scheduling`（seg-07）+ J-A01 | 03/20261009-ticket03-serial-01、05/20261009-ticket05-01 |
| 11-A02 | 四店默认 D 不作续跑、无范围冲突、成员保持 | `test_four_shop_real_identity_pause_then_default_D_resumes_only_remaining_task`（seg-07）+ J-A04（默认勾选 ['D']、续跑 A/B/C/D） | 03/20261009-ticket03-serial-01 |
| 11-A03 | 已完成/无需执行任务跳过、D 剩余执行（非 spawn 计数） | 同上两条（实际导航断言）+ J-A01（A/B/C 与 501 不重复）+ handled 92（J-A04） | 03/20261009-ticket03-serial-01 |
| 11-A04 | 配置删店/改名不改原轮快照、门禁留痕 | `test_deleted_and_renamed_configuration_cannot_replace_original_round_addresses`、`test_public_cli_resume_carries_snapshot_and_refuses_terminal_without_fallback`（seg-07） | 03/20261009-ticket03-serial-01 |
| 11-A05 | 开始页旧轮（默认/全选）不作普通续跑、显式处置提示 | `test_explicit_new_never_adopts_or_ends_an_unfinished_round`、`test_real_start_page_rejects_unfinished_round_before_click`、`test_start_view_lists_each_unfinished_round_with_its_real_state`（seg-07）+ J-A04 旧路径被拒 | 01/20261008-postmerge、06/20261009-ticket06-01 |
| 11-A06 | 已终态/超时/无暂停依据轮继续被拒、数据不变 | `test_completed_before_abort_keeps_completion_and_refuses_continue`、`test_unfinished_database_row_is_not_proof_of_this_windows_pause`、`test_resume_without_explicit_pause_and_after_terminal_is_refused`（seg-07）+ J-A02（超时轮拒绝恢复） | 01/20261008-postmerge、02、03、06 |
| 11-A07 | 中止写 ABANDONED、不可继续、沿原停止目标不叠停止 | `test_fully_paused_abort_and_repeat_keep_data_and_first_terminal`、`test_abort_during_pause_cleanup_reuses_original_stop_and_target`（seg-07） | 01/20261008-postmerge |
| 11-A08 | 收尾完成后无续跑、明确新建、旧轮不阻挡 | `test_real_page_cleanup_never_creates_next_round_until_explicit_start`、`test_public_locked_new_round_opens_only_authorized_scope_and_cleans_identity`、`test_real_missing_browser_startup_releases_cleanup_gate_for_explicit_retry`（seg-07）+ J-A02/J-A03/J-A04 明确新建 | 01/20261008-postmerge |
| 11-A09 | 改范围取消不动作/确认中止后重选 | `test_real_page_change_scope_cancel_is_inert_confirm_waits_then_explicit_new`（seg-07） | 03/20261009-ticket03-serial-01 |
| 11-A10 | 重复启动拒绝、持锁复核不误接管 | `test_new_intent_is_rechecked_after_acquiring_the_real_collection_lock`、`test_locked_new_intent_rejects_changed_date_metadata_and_finished_competitor`、`test_locked_resume_refuses_replacement_round_after_gui_preflight`、`test_public_resume_rejects_changed_scope_cross_day_and_live_cleanup`（seg-07） | 01/20261008-postmerge、03/20261009-ticket03-serial-01 |
| 11-A11 | 无进程旧轮实写终态的中止（非空操作） | `test_confirmed_leftover_abort_writes_the_target_round`、`test_run_page_never_treats_a_leftover_as_an_explicit_pause`（seg-07）+ J-A03 异常退出段 | 06/20261009-ticket06-01 |
| 11-A12 | 确认后目标变化/写库失败/未知 → 可解释拒绝、不误关 | `test_leftover_abort_confirmation_survives_only_unchanged_facts`、`..._refuses_while_a_collection_runs`、`..._keeps_a_terminal_that_landed_after_confirmation`、`..._write_failure_is_retryable_and_blocks_new`、`..._settles_dead_owned_browser_with_exact_proof`、`..._never_clears_unverifiable_browser_debt`（seg-07） | 06/20261009-ticket06-01 |
| 11-A13 | 关窗确认/取消各阶段、重开无续跑 | `test_native_close_cancel_keeps_each_running_or_paused_phase`、`test_native_confirm_in_each_pause_phase_ends_once_and_reopen_cannot_resume`、`test_native_confirmed_close_abandons_and_waits_through_failed_cleanup`、`test_confirm_close_still_ends_the_paused_round`、`test_cancel_close_keeps_a_real_pause_intact`（seg-07）+ J-A03 | 01/20261008-postmerge、05/20261009-ticket05-01、06/20261009-ticket06-01 |
| 11-A14 | 自动退出/历史超时按真实终态、不标用户暂停 | `test_auto_retained_exits_keep_their_real_terminal_not_user_pause`、`test_public_export_gap_explains_automatic_identity_timeout`（seg-07）+ J-A03 异常退出呈现 | 02、06 |
| 11-A15 | 跨天暂停/遗留不继续、显式中止后新建、自然跨天不回归 | `test_cross_day_paused_round_cannot_resume_and_is_shown`、`test_cross_day_paused_shown_even_when_another_leftover_is_newer`、`test_multiple_leftovers_are_disposed_of_one_at_a_time`、`test_day_boundary_finishes_round_and_forces_new_round`（seg-07）+ J-A04 跨天段 | 06/20261009-ticket06-01、test_p1 既有回归 |
| 11-A16 | 并发/重复终态先提交者胜、幂等 | `test_first_committed_terminal_wins_a_real_sqlite_write_race`、`test_repeating_terminal_commit_returns_real_fact_even_with_stale_round`、`test_first_committed_terminal_wins_timeout_race`（seg-07） | 01/20261008-postmerge、02/20261009-ticket02 |
| 11-H01 | 原生过程页暂停/继续/中止/新建/关窗确认与取消 | **待用户现场反馈**（步骤见下） | — |
| J-A01–J-A04 | 联合链（上文） | `tests/test_joint_acceptance.py`（4 项，seg-07 实际执行）+ `08-joint-chains-summaries.txt` + 现场原件联跑 4/4 | 本票 |

「仅有旧测试或历史诊断记录不算本次通过」：上表每项的本轮证据都在 `0e2fee3` 上实际重跑（联合链/回放直接运行；其余用例含在 1642/1642 的分段全量中，见其余段日志逐用例行）。

## 隔离副本与分段全量

- 最终副本：`.scratch/sandboxes/issues-08-11/07/20261009-ticket07-final-02/bestseller`（自实施 clone `--no-hardlinks --branch codex/issues-08-11` 于 `be8fda6`，审查补强后 ff 至 `fe36da7`；未带任何未提交改动；venv 于副本路径重建；config 三件自实施 clone 同源复制、哈希与票 06 副本一致）。来源清单：`00-source-manifest.txt`。
- 先行副本（整改前）：`.scratch/sandboxes/issues-08-11/07/20261009-ticket07-full-01/bestseller`（`0e2fee3`；`full-suite-01` 与 0e2fee3 回放的运行处）。
- 命令（副本根；未加 `-f`；显式主仓证据绝对路径；先行与最终各跑一次）：

```text
python tools/run_full_suite.py --log-dir F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\07\20261009-ticket07-01\full-suite-0N
```

```text
先行 full-suite-01（候选 0e2fee3，2026-10-09）：65+50+54+30+17+17 重段 + 其余段（96 文件）1409 项
合计 1642 项（预期 1642，计数对账一致），总耗时 788.3s，段结果 7/7 段 OK，结论 OK；退出 0

最终 full-suite-02（候选 fe36da7，2026-10-09）：65 项 105.2s / 50 项 45.3s / 54 项 29.5s / 30 项 28.8s / 17 项 12.9s / 17 项 12.1s + 其余段（96 文件）1411 项 681.3s
合计 1644 项（预期 1644，计数对账一致），总耗时 915.1s，段结果 7/7 段 OK，结论 OK；退出 0
```

- 退出码文件 `full-suite-0N-exit-code.txt`；逐段日志、`summary.txt`、控制台日志同目录永久保留。本票联合链 4 项在其余段日志内有逐用例行与 `TICKET07_*` 摘要；两处审查新增用例（`test_unsettled_cleanup_debt_keeps_the_manual_login_entry_disabled`、`test_an_unreadable_pid_with_a_diverged_proof_still_settles`）均在最终其余段逐用例行内。

## 两轴 code-review（含复审整改）

- 固定基线 `69066da4`（覆盖整个联合交付 30 个提交）；r1 评价对象 `0e2fee3`；整改 `be8fda6`；复审补强 `fe36da7`。两轴独立只读子代理（最小上下文、文件交接、不合并排序），报告原文归档在票证据 `review-r1/`、`review-r2/`，完整记录见本分支 `docs/reviews/issues-08-11-ticket07-code-review-2026-10-09.md`。
- r1 Standards：0 硬性违例；8 组判断项（结账判据两处重复、gui 四份守卫前导、note 键值反解析、vtype 五连判、夹具三份重复、小重复、注释语言、test_manual_edge 体例）。
- r1 Spec：0 阻断；(a) spec 行 59「已有终态但收尾未完成 → 手动入口禁用」在重开只剩欠账态未落实；(b) 新终态 `STARTUP_FAILED` 未在 spec 枚举。
- 整改 `be8fda6`：`_login_refusal` 补欠账门禁（红→绿 `test_unsettled_cleanup_debt_keeps_the_manual_login_entry_disabled`）；`taken_over` 单点化供 StopWatch 复用（alive 不可读但证明分歧 → 按已退出结账）；联合测试改跨模块复用 `PausedBrowserOS`。影响面 18 个相关单文件回归全部通过。
- r2 复审：Standards——① 消解（判「修正非回归」）、⑤ 部分消解（3→2，余下维持接受）、门禁无新违例，补标覆盖余量一处；Spec——(a) 通过（顺序无回退、票 05 放行侧不破、页面与服务端同端）、(b) `STARTUP_FAILED` 可接受为已确认口径（ADR-0025 2026-10-09 补充节；枚举接缝齐；建议的 spec §7 补行文实质记入本收口记录，不回溯改写已发布 spec v1）。
- 复审补强 `fe36da7`：新增 `test_an_unreadable_pid_with_a_diverged_proof_still_settles`（红证据 `11-red-taken-over-settle-on-0e2fee3.log`；绿见最终全量）。
- 复审后产品代码不再变化；最终全量与回放均针对 `fe36da7`（见上节与回放节）。

## 08/11 分别报告（本阶段）

- **issue 08（未关闭）**：功能（专用 Edge 手动登录入口、身份验证识别/等待/解除/超时自动中止）在隔离分支全部落地并有本轮自动验收（08-A01–08-A15 见索引）；**08-H01 部署机人工验收待用户反馈**；主仓集成未做。限制：未用真实账号/日常 Edge，不自动绕过验证；真实现场行为以用户反馈为准。
- **issue 11（未关闭）**：功能（明确暂停后按原范围/进度继续、明确新建门禁、正常关窗/异常退出/跨天遗留显式处置、终态先提交者胜）在隔离分支全部落地并有本轮自动验收（11-A01–11-A16 见索引）；**11-H01 原生界面人工验收待用户反馈**；主仓集成未做。限制：同上；原机 m1 现场即时状态以其后续反馈为准。
- 台账（`docs/ops/issues处理状态台账-2026-10-08.md`）：08/11 均保持**未处理**（未满足全部所需标准），追加本轮进展与剩余项记录，不合并结案。（2026-10-11 该台账移至 `.scratch/tool-output/issues/issues处理状态台账-2026-10-08.md`，本机、不进版本库。）

## 部署机人工验收步骤（待用户现场反馈）

### 08-H01（只双击采集 exe + 手动登录）

1. 用准备好的完整部署目录（源码/config/依赖/WebView2/dist/inventory_fetch.exe），本机 `config/config.toml` 为独立专用 profile、正确 Edge 路径、`start_browser=true`；不动日常 Edge。
2. 双击 `dist/inventory_fetch.exe`；悬停开始页页头「打开专用 Edge」，核对悬浮提示里的配置来源与「专用浏览器数据目录」为本机实际值（非开发机路径）。
3. 点页头「打开专用 Edge」：应只打开专用 Edge（1688 主页）、不开始采集、不建轮次；浏览器事实经点击报因与悬浮提示呈现（界面无常驻事实行），无「账号已登录」结论。**重复点击**不应另开窗口（提示已有窗口/占用）。确认**日常 Edge 不受影响**。
4. 在专用窗口自行登录 1688（含必要验证）；未关窗口时尝试开始应被准确提示占用（点击报因）。**关闭专用 Edge**（含仍占目录的后台窗口），悬停「打开专用 Edge」核对状态句为已关闭/未占用。
5. 满足周计划等门禁后明确选店开始；在允许的正常场景核实**复用仍有效的登录态**（若登录已失效，如实记录，不把「能打开浏览器」当通过）。
6. 反馈方式：脱敏的本机配置路径、轮次 ID、状态与事件时间；不发账号凭据/Cookie。

### 11-H01（原生过程页暂停/继续与中止/新建）

1. 采集中在过程页点「暂停本轮」：先显示停止/收尾，完成后显示「已暂停，可继续」；核对同一轮次 ID、原范围与已保存进度。
2. 暂停完成前「继续」应不可用；完成后从过程页页头「打开专用 Edge」手动处理后**先关窗口再点继续**：应恢复原轮并跳过已完成任务（不重复抓取）。
3. 点「中止本轮」确认后：核对不可继续、数据保留；收尾完成后回开始页「明确新建」不被旧轮阻挡。
4. 关窗：采集中/暂停中各触发一次关窗——**取消**应维持原状态（不中止、不自动继续）；**确认**应结束原轮、等待收尾后退出；重开不提供旧轮继续。
5. 现场若自然遇到身份验证：观察准确类型、等待提示与实际时限、暂停/中止按钮；真正解除后自动继续、或实际到时限自动中止（不主动触发/不绕过验证）。不便现场等待的项如实标未验证。
6. 反馈同上（脱敏、不带凭据）。

## 未完成与边界（不得提前声称通过）

- **主仓未集成**：`main` 现含 SKU 分组线（`c74d743`）且超出本分支基点 `69066da4`；集成属另行授权的事，须保留 SKU/schema 与 elapsed 接口并另做集成验证，不靠切分支/覆盖文件完成。
- **08-H01、11-H01 待用户反馈**；部署机原生 Windows/exe/pywebview 与真实登录不由自动测试或测试桥代替。
- 全部自动验收离线、静音：测试专用配置/临时目录/真实临时 SQLite；未使用真实账号、未启动真实采集、未关日常 Edge、未改写固定历史诊断材料。
- 本票联合全量不替代未来的主仓集成验证；六票单票全量（1537/1565/1581/1600/1613/1638）为各自历史结果，不叠作本票结论。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`。
- 隔离副本（保留）：`.scratch\sandboxes\issues-08-11\07\20261009-ticket07-final-02\bestseller`（HEAD `fe36da7`，最终全量与最终回放处，本路径重建 venv）；`.scratch\sandboxes\issues-08-11\07\20261009-ticket07-full-01\bestseller`（HEAD `0e2fee3`，先行全量与 0e2fee3 回放处）；实施副本 `.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller`（分支全量提交）继续保留。
- 来源：固定点 `69066da4` + 票 01–06 共 28 个提交 + 本票 4 个提交（`b5af358`/`0e2fee3`/`be8fda6`/`fe36da7`）；无未提交覆盖（`00-source-manifest.txt`、回放 JSON 均录）。
- 验收证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\07\20261009-ticket07-01`（来源清单、回放 JSON/日志、分段全量与 summary/退出码、链摘要、固定材料哈希、审查报告与复验）。
- 新工具输出：`.scratch\tool-output\issues-08-11\07\20261009-ticket07-01`（新回放脚本与清单）。
- 永久保留：上述全部；`docs/design`、`docs/reviews` 的票 07 记录另在本分支受跟踪保存。`.scratch` 不进 Git，跨机器需显式打包。
- 可删除候选：无。实际副本清理：未执行。
- 暂留待确认：票 01–06 的既有副本/暂留项不归本票处置；本票副本保留至人工反馈与集成核验后由用户决定。

## 集成记录（2026-10-10）

经用户授权（「合并进main」），本线整体并入 main；本节为落地后所记，上方正文保留为票 07 收尾时（fe36da7）的事实。

- 合并链：`9e88bea`（本线 × main@`a0b1ffb`；**唯一内容冲突** `CONTEXT.md` 尾区双追加——本线「中止/收尾/明确新建」不变量与主侧 SKU 词条整节两边保留；另三处同文件（`exchange.py`/`pipeline.py`/`tests/test_local_trial_exchange.py`）异区自动合并，逐块对两侧 diff 核过）→ `221ff20`（跨线用例适配：elapsed 的「暂停尾段同轮续跑」用例按 ADR-0024 2026-10-09 替代节的口径补一步界面观察——该用例按旧政策（gone/replaced 即结束旧 operation）的时间线编写，跳过观察会让同轮续跑顶掉身份行后被新政策保留的可见欠账挡住第二次暂停；先在合并前主侧单独复核确认其非既有红，再成对更新；elapsed 记账断言未动）→ `28c86d1`（叠加 issue 06 与 e2e 两笔先行落地；真交集 5 文件全部为并集，逐文件前后 diff 核过）→ `0545f6b`（折入其后 main 修复）→ 主仓 fast-forward 落地（`4f2c764 → 0545f6b`，71 文件）。
- 集成验证（隔离副本 `.scratch/sandboxes/issues-08-11/integration/20261010-merge-01/bestseller`）：分段全量 **1944/1944（预期 1944，计数对账一致）、7/7 段 OK、退出 0**。中间三趟红均已定性并记录：① elapsed 用例旧政策期望（已按上款适配）；② 进度遮罩用例重负载偶红（隔离复跑 ×3 全绿、另趟同树全量亦绿，已挂独立任务建议）；③ 副本分支布局假红（合并暂落在 study 分支、本地 `main` ref 落后；修正 ref 布局后单测与整文件复绿，树内容未变）。来源清单、逐轮全量与 summary、回放与审查证据：`.scratch/logs/tickets/issues-08-11/integration/20261010-merge-01/`。
- 本线「主仓未集成」旧边界自此解除；08/11 剩余未完成仅**部署机人工验收（08-H01、11-H01）待用户现场反馈**，两 issue 保持未处理（见台账 2026-10-10 集成行）。
- 2026-10-10 补：**试采入口口径**（票 08，`e2cee2c` 快进 main）——纯汇总机的本机试采与正式采集共用同一份专用 profile，登录准备本就必需；手动登录入口原先对纯汇总机无条件拒绝（全仓唯一未按试采豁免的角色判据）。现已按用户确认放行（正式模式拒绝不变），且开始页禁用按钮显示原因。**08-H01 在 m4 上的走查路径：双击 exe 后先点「进入本机试采」，登录入口随后可用**；试采下占用核验/重复点击/归属/收尾门禁一律沿用。
- 2026-10-11 补（start-page-feedback 批，票 03/04）：上方 08-H01/11-H01 走查步骤与相关交付记录已按该批界面同步——入口改名「打开专用 Edge」并上移页头（开始页与过程页同槽互斥显隐）、「刷新浏览器状态」按钮已删（占用核对改悬停状态句，界面自动核对）、浏览器事实行已删（原因类经点击报因，无「账号已登录」结论不变）、配置来源/专用目录/状态句改经入口悬浮提示查看；上款「开始页禁用按钮显示原因」同步为「点击报因＋悬浮提示」两形。界面与证据见该批票 03（`.scratch/logs/tickets/start-page-feedback/03/20261011-t03-01/`）。
- 2026-10-11 补（人工走查反馈）：用户在 m4 按上方「部署机人工验收步骤」完成 08-H01（走「本机试采」路径）与 11-H01（原生过程页）现场走查，均报告通过（未附脱敏明细）。按 grill Q3(c) 口径：m4 试采为「界面与登录态复用」补充结论；「正式采集复用」仍以采集机反馈为准，两 issue 关闭候两者反馈齐——issue 08/11 保持未处理（见台账 2026-10-11 行、反馈记录 `.scratch/logs/tickets/issues-08-11/07/20261011-human-walkthrough-feedback/`）。同日，本机试采两处回归（`1b44245` 卡片就绪窗口、`7755654` 业务页标记）已诊断修复并入 main 并完成真机复验；本次走查在当日两处修复并入后的树上完成。
