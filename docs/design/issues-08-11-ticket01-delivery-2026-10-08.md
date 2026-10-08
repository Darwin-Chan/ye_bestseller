# issues-08-11 票 01：明确中止结束原轮，收尾后才能新建

实施开始：2026-10-08；初审修复及复审候选：2026-10-09。范围仅票 01；本记录不关闭 issue 08 或 11，不替代票 07 联合收尾。

## 实施来源与边界

- 主仓：`F:\AI\projects\bestseller`。
- 实施及测试副本：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller`。
- 分支：`codex/issues-08-11-ticket01`。
- 来源／用户确认的审查固定基线：`69066da421b255168884224a20a28fb20b6d4713`。
- 初审候选：`bc6cc8a296a165dc43b393ab0085f3a020403f5e`（仅本票 13 个源代码／测试／领域说明文件）。
- 主仓后续 SKU、计时提交与未提交内容没有复制、暂存、回滚或合入。本分支没有自动集成到主仓；尤其计时 `Segment.tick()`、`checkpoint()` 及新 schema 要在实际集成时保留最新版接口。
- 产品 grill/spec/tickets 已确认，不重启澄清。用户额外确认：本票单独做隔离分段全量及双轴审查，票 07 联合链路／原场景回放／人工验收仍独立必做。

## 公共接口与真实测试接缝

1. shipped `ui_live.html` 的开始／中止／继续事件，通过真实公开 Api 和 views 表达启动及停止资格；测试以无日常 profile 的独立 headless Edge 打开真实页面，网络被拦截，只关闭本测试启动的浏览器。
2. 临时 SQLite 的 rounds／身份／停止事实；`NewRoundIntent` 只读预检，不提前制造空轮。`pipeline.run_round` 取得真实采集锁后再经 `rounds.open_new` 的写事务复核日期、完整范围元数据及轮次高水位。
3. 中止先提交 `ABANDONED`；暂停停止／收尾中升级沿原冻结目标和原绑定，不追加第二次停止；真实先提交的终态保留，重复操作幂等。
4. 只有进程核验、浏览器归属／创建证明、必要身份清理及互斥释放均安全，才开放用户明确新建。超时、未知状态、清理失败与已知原目标欠账不是收尾成功。`BORROWED` 用户浏览器不关闭。
5. native close guard 通过公开 Api 实测四阶段默认取消／确认中止及等待收尾；不把既有关闭测试冒充新增验收。只替换 OS 进程／浏览器副作用与时钟，不 mock 轮次／停止业务实现。
6. 普通 CLI 未带新建意图时保留既有 scope/日期及外部浏览器连接契约；GUI 的开始和继续不能借 CLI 旧默认行为隐式收养未终态旧轮。完整暂停续跑、身份验证、手动 Edge 与遗留处置界面仍属票 02–06。

## 逐项自动验收映射（最终复审／全量待填写）

下列用例均在 `tests/test_abort_new_round.py`，相关普通 CLI／试采／停止／关窗回归另见测试汇总。

| 验收项 | 具体行为与新用例 | 当前结果 |
| --- | --- | --- |
| 11-A07 | `test_fully_paused_abort_and_repeat_keep_data_and_first_terminal`；`test_abort_during_pause_cleanup_reuses_original_stop_and_target`：运行／本界面暂停轮明确中止，保留库存、拒绝继续，停止中升级沿原目标 | 新用例通过 |
| 11-A08 | `test_real_page_cleanup_never_creates_next_round_until_explicit_start`；`test_public_locked_new_round_opens_only_authorized_scope_and_cleans_identity`：收尾完成不自动新建，用户点击后持锁创建指定范围并清理本次身份 | 新用例通过 |
| 11-A05 | `test_explicit_new_never_adopts_or_ends_an_unfinished_round`；`test_real_start_page_rejects_unfinished_round_before_click`：默认 D／全范围均拒绝未结束轮，不隐式结束或续跑 | 新用例通过 |
| 11-A06 | `test_unfinished_database_row_is_not_proof_of_this_windows_pause`；`test_completed_before_abort_keeps_completion_and_refuses_continue`：完成／中止轮不可继续，无终态本身不是本界面暂停证明 | 新用例通过 |
| 11-A10 | `test_new_intent_is_rechecked_after_acquiring_the_real_collection_lock`；`test_locked_new_intent_rejects_changed_date_metadata_and_finished_competitor`：页面／Api 重复启动门禁和公共持锁后的最终意图复核 | 新用例通过 |
| 11-A13 | `test_native_close_cancel_keeps_each_running_or_paused_phase`；`test_native_confirm_in_each_pause_phase_ends_once_and_reopen_cannot_resume`；`test_native_confirmed_close_abandons_and_waits_through_failed_cleanup`：运行／停止／收尾／完全暂停时取消无中止副作用，确认则结束并等待 | 新用例通过；部署机原生人工验收未执行 |
| 11-A16 | `test_first_committed_terminal_wins_a_real_sqlite_write_race`；`test_repeating_terminal_commit_returns_real_fact_even_with_stale_round`：真实 SQLite 并发终态及重复中止保留先提交事实 | 新用例通过 |
| 写库／清理失败、未知目标 | `test_terminal_write_failure_does_not_stop_or_release_new_round_gate`；`test_failed_browser_cleanup_never_becomes_success_at_a_deadline`；`test_unknown_browser_proof_never_closes_or_releases_new_round_gate`；`test_public_pipeline_preserves_owned_browser_debt_when_close_fails` | 新用例通过 |
| 归属／PID 复用／后来的进程 | `test_reused_browser_pid_is_not_closed_and_os_proof_can_settle_old_cleanup`；`test_replacement_cannot_discard_known_old_browser_cleanup_debt`；`test_borrowed_user_browser_is_never_closed_by_abort_cleanup`：不作用于后来 B，不关闭用户 Edge，旧欠账不抹除 | 新用例通过 |
| 启动失败与基础门禁 | `test_failed_os_launch_does_not_create_an_empty_round`；`test_new_intent_wire_format_rejects_ambiguous_scope_or_date`；普通 CLI／角色／计划／试采回归 | 新用例及相关回归通过 |

## TDD、回归与证据

- 主仓证据根：`F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\01\20261008-postmerge`。
- 01–17 前缀日志保留逐项红→绿，以及夹具／测试桥误红的纠正日志；错误标签不覆盖原证据。冻结历史诊断 HTML、manifest、active-run 和旧日志保持不变。
- 初审前 11 个相关单文件合计 **278 条通过**；当时新增文件 28 条。其中 GUI 67 条以 `19-gui-plan-fixture-green.log` 为准，新增公开接缝 28 条以 `19-public-seam-validation-green.log` 为准；其余以 `targeted-final-*` 为准。完整列表：`20-targeted-summary-and-pending.json`。
- 原 `targeted-final-test_gui.py.log` 为失败日志（夹具缺本周计划，先被计划门禁拒绝），补真实计划后另存新绿日志，未计为通过。一次不存在文件的 discover 加载 0 条，不算通过。
- 测试配置来自副本中的示例配置，`machine_id=m-test`、`alarm_on_intervention=false`；db/profile/cache/交换库都在副本，analysis 的相对路径也在副本。没有复制真实账号、Cookie、数据库或旧 venv。`21-candidate-and-test-config.json` 记录候选及配置哈希／解析路径。
- 仓库没有已配置类型检查器，副本也无 mypy／pyright；`18-static-validation.txt` 中 compileall 仅为语法验证，不声称类型检查通过。diff --check 已通过。

## 初审修复与最新回归（2026-10-09）

- 最新 11 个相关单文件 **287 条通过**，新增公开接缝文件 **35 条**，停止目标文件 **20 条**；另补驱动接线和 P1 文件，13 个单文件合计 **335 条**。逐文件精确计数与有效日志见主仓 `37-repaired-candidate-regression-summary.json`；保留原 `34-*` 的两文件失败，不混作绿灯。
- Spec P1 页面旧确认：新增 `prepare_abort/confirm_abort`，服务端 nonce 冻结原轮和原目标，弹框确定不能授权后来执行。真实 HTML 新回归先红后绿，另以真实 SQLite trace 在最终写库边界登记同轮竞争执行，页面及正常关窗都在写锁内拒绝（23、29、30）。无目标的完全暂停也是冻结事实，不是泛化授权。
- Spec P2 浏览器建立失败：公开持锁 `run_round` 将明确新建的失败尝试记录为真实 `STARTUP_FAILED` 终态；不制造空进行中轮、不伪造人工放弃。OWNED/UNKNOWN 仍独立阻止新建，先提交完成终态不改写（24、31）。
- Spec P1 首次 tick 前身份替换：停止开始即冻结原浏览器，所有观察到替换的分支保留原欠账，不作用于 B。补强杀后再观察替换，以及原采集仍活着时不能先关闭浏览器（25、27、35）。
- Standards P3：收尾资格由 `crawler_identity.browser_cleanup_complete` 统一判定；保留各调用方 None 与条件清理职责。
- 旧测试迁移说明：`test_stop_target` 两条原 IDLE/释放预期与本票安全门禁冲突，改为 VERIFYING/target_replaced，保留 B 身份和不终止／不关闭断言；`test_crawler_close` 原目标欠账未知时的新鲜关窗授权改为保持窗口、保留 B/旧债务而非假称完成；`test_crawler_mutex` 五处驱动替身补接受新增可选 `explicit_new`，业务断言不放宽。原回归失败记录保留（28、34），迁移后另存绿灯（33、36）。冻结历史诊断材料未改。
- 23 的首次页面选择器误红与修正后的真正产品红都保留。31 是既有修复的保护绿灯，不称为新的红→绿。

## 双轴审查

固定命令：`git diff 69066da421b255168884224a20a28fb20b6d4713...HEAD`。独立、最小上下文只读两代理审查；源代码不在共享主仓并发修改。

### Standards

初审无硬性违例；一个 P3 资格判定重复已处理，独立复审待进行。原报告：`docs/reviews/issues-08-11-ticket01-initial-standards-2026-10-08.md`。

### Spec

初审三个发现（P1 旧页面授权、P2 启动空轮、P1 首次清理前丢欠账）已按 TDD 修复，独立复审待进行。原报告：`docs/reviews/issues-08-11-ticket01-initial-spec-2026-10-08.md`。

## 本票分段全量

未运行；须待审查问题处理完毕，在副本根执行不带 `-f` 的 `python tools/run_full_suite.py --log-dir <主仓绝对证据路径>`。最终填精确命令、退出码及 summary 合计／对账／结论，不以相关回归冒充全量。

## 未完成与待人工事项

- 复审／本票全量／最终提交待完成；主仓集成未做。
- 票 02–07 未实施；票 07 联合链路、原场景新回放及其全量／双轴审查不因本票免除。
- 08-H01、11-H01 待部署机用户反馈，自动测试桥不替代真实原生入口及真实有效登录态复用人工验证；未做真实登录、采集、有声测试。
- `.scratch` 与隔离 clone 均不是远程发布／异地备份；代码提交不携带本地工单、示例以外配置或证据，跨机器续作须显式携带。

## 副本、证据与处置

- **保留**：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller`，独立 clone／本票分支／本路径重建 venv；供审查、全量、后续集成使用。
- **永久保留证据**：`F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\01\20261008-postmerge`，以及工单引用的固定历史诊断材料，不随副本清理丢失。
- **暂留待确认**：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-ticket01-implementation`（ownership 失败现场）；`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-ticket01-implementation-2\bestseller`（先前合并冲突现场）。尚未删除，后续先核对归属、独有材料与授权。
- **可删除候选**：无。**实际清理**：未执行副本或历史证据清理。初审两份交接任务已点名收取并清理；复审亦只清本次点名交接目录，结果保存到主仓证据及本分支审查报告。
