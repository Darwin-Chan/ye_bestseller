# prep-owner 02：统一当前结局、历史结果与开轮闸门交付

日期：2026-10-10（Asia/Shanghai）。范围仅 02；03 未授权，#prep-owner 整项不关闭。
分支 `claude/prep-owner-01`（沿用票01 的 worktree 副本）；基点 `9c49d27`（票01 顶点，
未并入 main）；受测快照 `eacf17f`（其后仅本文件与 ADR-0047 的文档提交）。

## 交付内容与 interface

「确认期不许开轮」与「本次结局 / 历史 / 闸门」的解释收进 **一处**（`plan_step.start_plan`
及其 `StartPlan`），开始页与后端开轮共用；历史只保留原有跨周提示。

- `plan_step.start_plan(db, cfg, week, prep, *, history=None, confirming=False)`：
  `prep` 仍是本界面**这一次**准备的结局；`history` 是最近一份历史结局，**只**用于
  `week_changed`（本次没落定时由它兜住原有「本周已变」提示），不证明当前成功、不清
  `stale`、不假造结果；`confirming` 是「还在 30 秒预算内」这一预算事实。
- `StartPlan` 增 `confirming` 字段与 `locked` / `lock_reason` 属性：确认期与闸门同折进
  「现在不许开轮」，确认期优先说「正在确认」（此时「重试准备」也会被拒，指去重试是死路），
  闸门原样转交理由。文案常量 `CONFIRMING_GATE_REASON` 与页面旧内联句逐字节相同。
- `gui.Api`：`_plan_block_reason` → `_plan_lock_reason`——确认期内与开始页一致拒绝；本地
  有本周计划按原规则降级放行；没有则守闸门；纯汇总机先判角色；试采不看正式闸门。
  `start_run` 消费 `plan.lock_reason`，不自行解释结局/预算。`resume_run`、停止、关窗未动。
- `views.start_view`：把 `(result, history, confirming)` 三件事实转交 start_plan，
  `plan_locked = plan.locked`、`plan_confirming = plan.confirming`；五个 JSON 键
  （plan_confirming / plan_waiting / plan_gate / plan_locked / plan_retry）与 plan_note
  形状不变，无自动重画、无逃生口。`plan_waiting` 仍是快照 `running` 的转交。

行为保持：同步 `prepare()` 返回与 `retry_prepare()` ok/error 契约不变；ADR-0035 的 30 秒
兜底、本周计划分流、跨周提示、ADR-0049 试采隔离、ADR-0051 elapsed 归属、ADR-0011/0016/0048
的连接与停止决策，均未改写。

## 修改与提交

生产：`bestseller_monitor/plan_step.py`、`bestseller_monitor/views.py`、`gui.py`。
测试：`tests/test_plan_step.py`（+6）、`tests/test_views.py`（+1）、`tests/test_gui.py`（+6）、
`tests/test_preparation.py`（+1）。

提交（分支内，未并入 main）：`28dd901`（生产：一处判定）→ `a0e7491`（测试：确认期闸门、
history、迟到落定）→ `eacf17f`（Spec 复审整改：异常后重试消费新结局 + history 跨周 Api 级）。

## 验收记录

日常单文件（副本内，全绿）：test_plan_step 60、test_views 28、test_preparation 18、
test_gui 82、test_local_trial_gui 20、test_ui_live 19、test_run_cli 17、test_bench_refresh 2、
test_abort_new_round 39、test_leftover_rounds 26、test_manual_edge 21、test_paused_manual_login 13、
test_explicit_pause_resume 16、test_elapsed 36、test_elapsed_cross_entry 12、
test_elapsed_lifecycle 14、test_local_trial_exchange 2、test_pacing 19、test_joint_acceptance 4。

TDD 红→绿：StartPlanTests 新 6 项先红（缺 history/confirming 入参）；views 跨周提示先红
（history 未转交）；D06（页面锁定而后端开轮）先红——均为本票修复的缺口的复现。红/绿逐条
命令与结果见证据 `dev-log.md`。

双轴审查（独立只读子代理，Standards/Spec 并行；任务与报告在证据目录）：

- Standards 首轮：**0 硬违规**；2 条判断题均保留（三件事实两处解包——反向引用约束下合理；
  `CONFIRMING_GATE_REASON` 命名与同族常量并列可接受）。增量复审（修复快照）**通过、无问题**。
- Spec 首轮：**0 scope creep、0 硬伤**；1 个实缺（验收 5 缺「异常后明确重试并消费新结局」的
  Api 级用例）＋1 个次要（history 跨周无 Api 级用例）——全部整改（`eacf17f`），
  增量复审**两个缺项均已闭合、通过**。

deletion test（证据 `deletion-test.txt`，grep 证据 + 删除论证）：确认期规则只在
`StartPlan.locked/lock_reason`；结局/历史选择只在 `start_plan` 的 `latest`；gui/views 只转交
与消费，零 `PrepStatus` 解释、零预算公式、零旧成功回填；删掉这处接口会在 ≥3 处重新散开
（D06 形状、跨周提示丢失、文案与优先级漂移）。

收尾分段全量（副本根、`--log-dir` 主仓本票证据、不加 `-f`、退出码 0）：

```text
python tools/run_full_suite.py --log-dir "F:\AI\projects\bestseller\.scratch\logs\tickets\prep-owner\02\20261010-implement"
```

summary.txt 原文（合计/对账/结论）：

```text
test_exchange：72 项，OK，107.5s；test_judgment_set：72 项，OK，57.2s；test_plan_step：60 项，OK，23.9s；test_export：30 项，OK，25.0s；test_git_channel：17 项，OK，10.8s；test_shops_sync：17 项，OK，10.7s；其余段（111 文件）：1718 项，OK，779.2s
合计 1986 项（预期 1986，计数对账一致），总耗时 1014.4s，段结果 7/7 段 OK，结论 OK
```

全量期间工作树未被任何改动：收尾时 `git status` 干净、顶点 `eacf17f`，被测试的字节集合与
受测快照一致。摘要绝对路径：
`F:\AI\projects\bestseller\.scratch\logs\tickets\prep-owner\02\20261010-implement\summary.txt`；
逐段日志同目录 `seg-*.log`。

## 边界与未做

- 03 未授权：模式切换与迟到扫描的跨入口矩阵、elapsed/关窗整项回归验收，未做。
- 不真实等 30 秒；不接真实 Git/数据/浏览器/登录态（READY 用本地裸远端走真实发布路径）；
  adapter 覆盖不等于原生窗口/真实故障现场验收。
- CONTEXT.md 未改：本票不引入新词汇（闸门、确认期锁、未能确认最新均已有词条或表述）。

## 目录与证据处置

- 主仓：F:\AI\projects\bestseller
- 隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\prep-owner\01\20261010-implement\bestseller
  （主仓 worktree，分支 `claude/prep-owner-01`；沿用票01 副本不另建；测试 `config/*.toml`、
  `shops.csv` 自 example 复制、`start_browser=false`，开工前重核路径逐项在副本内——
  `config-path-audit.json`；未复制真实库/登录态/主仓真实配置）
- 来源：基点 `9c49d27`（票01 顶点）+ 本票三提交至 `eacf17f`；无未提交改动
- 验收证据：F:\AI\projects\bestseller\.scratch\logs\tickets\prep-owner\02\20261010-implement\
  （summary.txt、seg-01..07 日志、config-path-audit.py/json、dev-log.md、deletion-test.txt、
  review-standards.md、review-spec.md、review-tasks/、draft-delivery.md、delivery-record-copy.md）
- 永久保留：上述证据目录全部材料（不随副本清理）
- 可删除候选：无（00/01 材料与其他工单材料不属本票，不列）
- 暂留待确认：副本保留待 03（若获授权）复用或授权清理；分支 `claude/prep-owner-01`
  在并入 main 前保留；00/01 副本与证据保持原状
- 实际清理：未执行（未获授权）
