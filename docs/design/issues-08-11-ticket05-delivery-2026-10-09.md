# 票05交付：暂停后手动处理登录，再明确继续原轮

日期：2026-10-09（Asia/Shanghai）。仅票05隔离分支交付；不是主仓集成、不是部署机人工验收、不代表 issue 08/11 关闭。

## 来源与范围

- 主仓/本地工单/spec/永久证据根：`F:\AI\projects\bestseller`。
- 实施仓：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller`。
- 分支 `codex/issues-08-11`，开工 HEAD `a18f18e946faaa180c23cb66ab1e67dfae010624`（干净，含票01–04交付）；未从 main 另建分支。
- 提交：实现 `ca8dd6d`；审查整改 `1363aa9`、`17649b1`；全量暴露的基准兼容修复 `bb781b0`；本交付文档为后续提交。
- 不实施票06/07，不重设计票02身份验证，不自动合 main，不改历史诊断/manifest/原始HTML/active-run，不把父 issue 标为处理完成。

## 逐项自动验收

| 验收 | 结果及公开接缝 |
|---|---|
| 08-A09/A12 入口时机 | 真实 `ui_live.html` 过程页：采集中与「暂停停止/收尾中」入口禁用且 `open_login_browser` 拒绝（未暂停时还给出原因「采集或身份验证等待中…」）；本界面明确暂停且停止收尾完成后入口可用，点击只经同一专用浏览器服务打开，不采集、不建轮。 |
| 08-A10 同一目录 | 手动入口与续跑子进程同读一份配置解析出的可执行文件/专用目录/端口；关键配置在界面打开后变化（`attach_port` 改动）→ `continue_gate` 明说「浏览器配置已变化，请重新打开界面」，`resume_run` 拒绝、`spawn=0`，不静默换目录。 |
| 08-A11 占用 | 手动窗口开着（occupied）时 `resume_run` 拒绝、`spawn=0`；页面「继续」按钮禁用并给出原因；重复点登录入口返回 `reused`，只启动一次，不另开标签。 |
| 08-A12 未知/门禁 | 目录释放无法核实（unknown / port_busy）时继续拒绝；采集、验证等待、停止、收尾、关闭中入口禁用且 API 拒绝；暂停资格由服务端真实状态（停止窗口 idle + 无采集进程）判定，前端禁用只是引导。 |
| 08-A13 归属（继承） | **本票不新增归属测试**：复用票04的同一服务与既有关闭/归属保护，未改其判据。 |
| 08-A14 模式/配置 | 直接以 `start_browser=false` 打开界面：登录与起采准确拒绝（「要求 browser.start_browser=true」）；界面打开后改成 false：按「配置已变化」拒绝继续与登录，不静默改配置、不显示账号状态；坏配置同样拒绝。 |
| 11-A01/A03 继续 | 关窗不自动继续（只读轮询三次 `spawn=0`，`can_resume` 保持）；停止/收尾期间继续被拒；显式点继续恢复同一原轮 ID 与完整成员（A/B/C/D），真实 `pipeline.run_round` 执行时 A/B/C 与已成功的 501 被跳过、只访问 D 的剩余任务，终态 COMPLETED。 |
| 关窗语义 | 手动窗口开着也不改关窗语义：取消（不调 `begin_close`）保留暂停与原窗口；确认关窗仍写 ABANDONED 并等收尾，之后无普通续跑入口。 |
| 登录失败 | 手动打开失败不影响原轮：无终态、成员与进度不变、仍 `can_resume`；打开失败后同为「失败」态，继续保守拒绝（与开始页同源）。 |
| 轮询成本 | 真实浏览器事实要跑一次秒级进程枚举，过程页每 2 秒轮询：采集中**零查询**，暂停态每次刷新恰一次（回归用例钉住）。 |

## TDD、定向测试和新回放

永久证据 `E = F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\05\20261009-ticket05-01`。

- 红：`E/01-ticket05-baseline-red.log`。在只含开工基线 `a18f18e` 的隔离副本里，仅补入本票的测试文件后运行：13 项中 12 项失败（7 failures + 7 errors，退出 1）；唯一在基线上就通过的是 `test_continue_is_refused_while_the_pause_is_still_stopping`（它考的是既有的停止窗口门禁）。红日志保留，不冒称通过。
- 绿：`E/10-ticket05-final-verbose.log`（本票 13 项 OK）、`E/11-ticket04-manual-edge-regression.log`（19 项 OK）、`E/12-ticket03-pause-resume-regression.log`（16 项 OK）、`E/13-bench-refresh-regression.log`（2 项 OK）。均在最终副本里跑，来源见 `E/00-source-manifest.txt`。
- 其他定向回归：`test_gui` 67、`test_ui_live` 19、`test_crawler_close` 51、`test_identity_lifecycle` 28、`test_local_trial_gui` 20、`test_abort_new_round` 39、`test_stop_target` 20、`test_round_stop` 11、`test_round_wiring` 17、`test_run_cli` 17、`test_browser_session_lifecycle` 24、`test_helpers` 7，均 OK。
- 真实调度复现：`test_manual_login_then_explicit_continue_reaches_real_scheduling` 走真实 `pipeline.run_round`、真实临时 SQLite、离线路由的详情页与 `browser_pw` 会话替身；暂停由真实过程页按钮触发的主动停顿打断（断言「真的点了暂停」），随后手动开关专用 Edge，再由显式继续完成剩余任务（只访问 502，轮次 COMPLETED、成员仍为 A/B/C/D）。不是把历史超时改称用户暂停，冻结旧诊断未改。
- 全部离线静音、测试专用配置与临时目录、无真实账号/真实采集/日常 profile；`compileall` 通过、`git diff --check a18f18e..HEAD` 无输出（项目无单独类型检查配置，本次不伪称静态类型检查）。

## 分段全量

- 初候选 `ca8dd6d`：`full-suite-01` 退出非零，合计 1610/1610、**6/7 段 OK**、结论 FAILED——`其余段` 有 1 项真实回归：`test_bench_refresh.test_measure_reports_refresh_and_legacy_costs`（`get_run` 现在也走 `_login_refusal`，读 `cfg.role`，基准的合成 config 替身缺该项）。失败日志保留，不冒称通过。
- 最终候选 `bb781b0`：`full-suite-02` 退出 0，不加 `-f`，六重段与 94 文件其余段串行覆盖全量。原始 `summary.txt`：

```text
分段全量测试 2026-10-09 20:43:52
仓库：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\05\20261009-ticket05-final-03\bestseller
传参：python tools/run_full_suite.py --log-dir F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\05\20261009-ticket05-01\full-suite-02

test_exchange：65 项，OK，95.1s；日志 seg-01-test_exchange.log
test_judgment_set：50 项，OK，45.6s；日志 seg-02-test_judgment_set.log
test_plan_step：54 项，OK，28.3s；日志 seg-03-test_plan_step.log
test_export：30 项，OK，29.0s；日志 seg-04-test_export.log
test_git_channel：17 项，OK，11.4s；日志 seg-05-test_git_channel.log
test_shops_sync：17 项，OK，12.3s；日志 seg-06-test_shops_sync.log
其余段（94 文件）：1380 项，OK，629.8s；日志 seg-07-其余段(94_文件).log

合计 1613 项（预期 1613，计数对账一致），总耗时 851.4s，段结果 7/7 段 OK，结论 OK
```

- 本票结果不替代票07的联合全量。

## 双轴审查

- Standards：无硬性文档标准违反；3 项基线异味（入口判据在两处重复、裸状态元组、跨页命名不一致）→ `1363aa9` 收成 `Api._browser_entry` 一处 + 具名常量 `_BROWSER_ENTRY_BLOCKED` + 过程页不再另存 `can_open_login`。
- Spec：无范围蔓延；1 项 P 级（暂停横幅硬编码「先关闭专用 Edge」补救，配置变动时说明错误）→ `1363aa9` 改为原因原样呈示；补 11-A01「停止/收尾期间继续被拒」与「手动打开失败不动原轮」，关窗用例补「取消保留暂停」。08-A13 判为票04服务的继承覆盖，本票不新增测试、不冒充新证据。
- 报告：`docs/reviews/issues-08-11-ticket05-code-review-2026-10-09.md`（两轴分列、不合并排序），原件随交付提交进实施仓文档；审查子代理只读、最小上下文，未写文件、未提交。

## 目录与证据处置

- 实施副本：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller`，保留用于后续串行票。
- 红证据副本：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\05\20261009-ticket05-red-01\bestseller`（checkout `a18f18e` + 仅补入本票测试文件；只为红日志，不跑全量）。
- 初候选测试副本：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\05\20261009-ticket05-final-02\bestseller`（clone 自实施仓、HEAD `ca8dd6d`；对应 `full-suite-01`）。
- 最终测试副本：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\05\20261009-ticket05-final-03\bestseller`（clone 自实施仓、HEAD `bb781b0`；对应 `full-suite-02`，并跑定向绿日志）。
- 三副本 config.toml/analysis.toml/shops.csv 从 examples/实施仓派生、报警 false、无真实 exe/DB/profile/凭据/venv 复制；shops.csv 只有离线示例店。两者被 `.gitignore` 忽略，不进 Git。
- 永久保留：`E` 全部红绿日志、来源清单、两轮分段日志与 `summary.txt`、审查报告。
- 可删除候选：无。
- 暂留待确认：上述三个票05副本均保留，待人工/后续集成核验且用户明确授权后再评估。
- 实际副本清理：未执行。未触碰他人任务目录。

## 部署机人工步骤与局限

1. 用准备好的部署目录、本机配置（独立 profile、正确 Edge 路径、`start_browser=true`）双击采集 exe。
2. 开始采集后在过程页点「暂停本轮」，等它完成停止/收尾并显示「已暂停，可继续」。
3. 在过程页点「打开专用 Edge / 登录 1688」手动登录或处理验证；GUI 只显示进程事实，不宣称账号已登录。
4. **自行关闭**专用 Edge（含仍占目录的后台窗口）；关窗不会自动继续。
5. 点「继续」，核对仍是同一轮次、原范围与保存进度，已完成任务不重复。
6. 若继续按钮不可点，按页面给出的原因处理（占用/无法核实/配置已变）；配置改动后重开界面。

已知局限：本票**不**做真实身份验证贯穿整链的验收（依赖 02，归票07）；不提供账号登录检测、自动验证、跨天续跑或异常旧轮处置（06）；Windows OS 事实查询失败保守拒绝。未合 main、未部署原生真实登录，08/11 未关闭。
