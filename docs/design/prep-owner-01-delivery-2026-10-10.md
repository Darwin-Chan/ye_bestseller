# prep-owner 01：统一准备接纳、预算与开窗等待交付

日期：2026-10-10（Asia/Shanghai）。范围仅 01；02/03 未授权，#prep-owner 整项不关闭。
分支 `claude/prep-owner-01`（主仓 worktree 副本）；基点 `940f537`（main；交接基线 6b31059
之后的两提交属他任务、不涉准备线）。受测快照 `563dfaf`。

## 交付内容与 interface

新 module `bestseller_monitor/preparation.py`：单一持有者收好开轮前准备的接纳、30 秒单调
预算、等待、落定与「先落定后扫描」编排。

- `Preparation(cfg, open_conn, now, runtime)` 固定一个上下文（配置、连接能力、时刻来源、
  运行 adapter）；不自行按路径开库，不回读调用方可变状态。
- `request() → RequestDecision`（ACCEPTED / IN_FLIGHT / TRIAL_BYPASS / DISPATCH_FAILED）：
  原子接纳；未落定拒绝并携带已有凭据供显式等待；不排队、不重跑、不重置截止点；试采绕过；
  派发失败（含落点 BaseException）先落定+唤醒、再保留传播。
- `wait_for_open(run)` 只等指定运行落定或它的剩余预算（不含扫描）；`wait_for_finish(run)`
  等同次 worker 连同缺口扫描（同步 prepare 语义）；外来持有者凭据拒绝（ValueError）。
- `snapshot()` 不可变 `PreparationSnapshot`：NOT_STARTED / CONFIRMING / EXPIRED / SETTLED /
  TRIAL_BYPASS，当前 result、失败类别、一份历史 result，`running`/`confirming` 派生；
  预算每次按单调截止点推导，不靠定时器维护第二份状态，墙钟与它无关。
- 每次接纳独立运行凭据与落定/完成通知；A 落定扫描未完可接纳 B，A 的等待/扫描/完成只对 A。
- `ProductionPrepRuntime`：真单调钟、daemon 线程、Event 等待；测试经 Api 注入适配器控制
  单调时钟、派发与唤醒——规则不下放给 adapter。

接线：`Api.prepare()`（request + wait_for_finish；重复请求记录理由并返回 None，不隐式等待）、
`retry_prepare()`（ok/error）、新公开入口 `Api.prepare_for_window()`（main 用：request 或
**明确等已有那次**，只等落定/剩余预算）、`confirming_now()/prepare_running()` 纯委托快照。
owner 选择（正式一个实例 / 试采 bypass 实例；回正式恢复同一实例）在既有模式同步区内完成，
等待不持 Api 锁。`main()` 改为调 `prepare_for_window()` 后建窗：私有线程、`_prep_settled`
Event 与 30 秒预算编排全部删除。`views.UiState` 的 prep/plan_confirming/plan_waiting 三份
分散字段并成一份快照；页面 JSON 键（plan_confirming/plan_waiting/plan_gate/plan_locked/
plan_retry）与前端重画机制不变。`start_run` 未动（确认期收紧属票 02）。

行为保持：ADR-0035 的 30 秒兜底与本周计划分流、跨周提示、ADR-0049 试采隔离、ADR-0051
elapsed 归属（准备仍不计入已抓时长）、ADR-0011/0016/0048 的连接与停止决策，均未改写。

## 修改与提交

生产：`bestseller_monitor/preparation.py`（新）、`gui.py`、`bestseller_monitor/views.py`。
测试：`tests/test_preparation.py`（新 17 项）、`tests/test_gui.py`（67→76）、`tests/test_views.py`。

提交（分支内，未并入 main）：`b28680b`（holder module）→ `facf90f`（生产 adapter）→
`328277a`（Api/main/views 接线）→ `3e1ffa1`（扫描守卫）→ `f8c1eec`（双轴复审整改）→
`563dfaf`（import 清理，受测快照）。

## 验收记录

收尾分段全量（副本根、`--log-dir` 主仓本票证据、不加 `-f`、退出码 0）：

```text
python tools/run_full_suite.py --log-dir "F:\AI\projects\bestseller\.scratch\logs\tickets\prep-owner\01\20261010-implement"
```

summary.txt 原文（合计/对账/结论）：

```text
test_exchange：72 项，OK，134.8s；test_judgment_set：72 项，OK，72.6s；test_plan_step：54 项，OK，29.3s；test_export：30 项，OK，30.6s；test_git_channel：17 项，OK，10.3s；test_shops_sync：17 项，OK，11.7s；其余段（111 文件）：1710 项，OK，814.0s
合计 1972 项（预期 1972，计数对账一致），总耗时 1103.2s，段结果 7/7 段 OK，结论 OK
```

摘要绝对路径：`F:\AI\projects\bestseller\.scratch\logs\tickets\prep-owner\01\20261010-implement\summary.txt`；
逐段日志同目录 `seg-*.log`。不使用 elapsed 旧全量或 00 诊断红测冒充本票验收。

全量期间发生的唯一改动＝`tests/test_gui.py` 一行 import 清理（在其余段加载测试文件之前落盘，
该段最终加载的就是终版）；其余文件全程未动。收尾时工作树干净，被测试的字节集合与受测快照
`563dfaf` 一致；该文件随后单独复跑 76 项通过。

日常单文件全绿（均在本票副本）：test_preparation 17、test_gui 76、test_views 27、
test_plan_step 54、test_local_trial_gui 20、test_manual_edge、test_elapsed 9、
test_elapsed_cross_entry 12、test_elapsed_lifecycle 14、test_paused_manual_login 13、
test_explicit_pause_resume 16、test_crawler_close 51、test_abort_new_round 39、
test_leftover_rounds 26、test_local_trial_exchange 2、test_pacing 17、test_ui_live 19、
test_bench_refresh 2、test_joint_acceptance 4、test_run_cli 17。

双轴审查（独立只读子代理，Standards/Spec 并行；报告与任务描述在证据目录）：

- Standards 首轮：**0 硬违规**；6 条判断题（4 条整改：共用等待 helper、清未用 import、
  补扫描来由注释、试采判别统一按 outcome；2 条保留：三处 `lambda: connect(...)` 各自绑定
  不同取值时点、`confirming_now/RequestDecision.accepted` 为规格明许的委托面）。
- Spec 首轮：**0 缺主项、0 scope creep**；2 个轻微覆盖缺口（NOT_STARTED 无直测、超时后重复
  请求无直测）与 1 个派发点 BaseException 自锁窗口——全部整改（新增直测；
  `request()` 派发点改捕 BaseException，先落定+完成再传播）。
- 增量复审（修复快照 `3e1ffa1..f8c1eec`）双轴**均通过**，无新问题；Standards 复审另抓到
  `tests/test_gui.py` 未用 `import threading`，已随手清理（`563dfaf`）。

deletion test（证据 `deletion-test.txt`，grep 证据 + 论证）：改动后的 `gui.py` 里预算公式、
私有等待、线程编排零残留；`views.py` 不碰准备知识；删掉 module，接纳/防重、单调截止点、
运行身份与通知、落定先于扫描、异常落定、固定上下文会在 Api 三入口＋main＋views 两处判定
共 5+ 个调用点以已复现故障的形状重新散开——深模块成立、非传导层。

## 边界与未做

- 02/03 未授权：start_run 在确认期内与开始页一致拒绝、当前结果/历史/闸门页语义与跨周历史
  输入、模式切换与迟到扫描的跨入口矩阵及 elapsed/关窗回归整项验收，均未做。
- 不真实等 30 秒；不接真实 Git/数据/浏览器/登录态（业务走真实 `plan_step.prepare_week`，
  计划库走不可达快路径）；adapter 覆盖不等于原生窗口/真实故障现场验收。
- CONTEXT.md 未改：本票不引入新词汇，既有词条（开轮前准备、闸门、界面会话事实）表述不变。

## 目录与证据处置

- 主仓：F:\AI\projects\bestseller
- 隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\prep-owner\01\20261010-implement\bestseller
  （主仓 worktree，分支 `claude/prep-owner-01`；形态沿用 issue-06「主仓 worktree 作副本」前例，
  开工前经用户确认。测试 `config/*.toml`、`shops.csv` 自 example 复制、`start_browser=false`，
  所有写入路径均在副本内——逐项核验见 `config-path-audit.json`；未复制真实库/登录态/主仓真实配置）
- 来源：基点 `940f537`（main）+ 本票六提交至 `563dfaf`；无未提交改动
- 验收证据：F:\AI\projects\bestseller\.scratch\logs\tickets\prep-owner\01\20261010-implement\
  （summary.txt、seg-01..07 日志、config-path-audit.py/json、deletion-test.txt、
  review-standards.md、review-spec.md、review-tasks/、draft-delivery.md）
- 永久保留：上述证据目录全部材料（不随副本清理）
- 可删除候选：无（00 材料与其他工单材料不属本票，不列）
- 暂留待确认：副本保留待 02/03 复用或授权清理；分支 `claude/prep-owner-01` 在并入 main 前保留；
  00 副本与证据保持原状
- 实际清理：未执行（未获授权）
