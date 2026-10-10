# start-page-feedback 整批（票 01/02/04 交付）· 两轴 code-review（2026-10-11）

- 评价对象：分支 `claude/start-page-feedback` 顶点 `4fd2200` 相对 `4d58cf3` 的整批 13 文件（`.scratch/start-page-feedback/` 5 个：spec＋4 票；`gui.py`、`bestseller_monitor/dedicated_browser.py`、`bestseller_monitor/pages/ui_live.html`、`tests/test_manual_edge.py`、`tests/test_ui_live.py`、`tests/test_paused_manual_login.py`、`tests/test_joint_acceptance.py`、`tests/test_explicit_pause_resume.py`；866+/130-）。
- 方式：两轴独立只读子代理（最小上下文、文件交接、不写不改不提交、两轴不合并排序）；任务文件与报告原文归档在票证据 `.scratch/logs/tickets/start-page-feedback/03/20261011-t03-01/review-r1/`。静态审查不替代测试；本候选的收尾分段全量 **2011/2011（预期 2011，计数对账一致）、7/7 段 OK、退出 0**（同目录 `summary.txt`）。

## Standards

0 硬违例。提示项 1 条＋判断项 2 条：

1. （提示项，非本批新开、待人工定）`.scratch/start-page-feedback/` 5 份文件强制跟踪与 `README.md:96/455`、`.gitignore:54`「.scratch/ 不进代码仓」相抵——沿袭 `4d58cf3` 前例；本批票据此明示。
2. （判断项，Duplicated Code）悬浮提示两形拼接逐字重复——`ui_live.html:373-375`（loadStart）与 `606-608`（tickRun）；「关闭专用 Edge 窗口不会自动继续本轮」在同一三元两分支重复（600/601）。`browserFactsText` 抽取建议已随事实面删除作废，此重复属顺延，改形须两处同步。
3. （判断项，轻，Speculative Generality）`openManualBrowser` 的 `return r`（785 行区）两调用方均未消费。

处置：**接受、不整改**——0 硬违例、均为两行级体例重复/未消费返回值；判据同 08 票审查先例（不为体例重整打断已测来源），位点如上供后续顺路整理。

## Spec

0 缺失/半做、0 范围蔓延。三票验收项均有新/改断言钉住（含：01 页头位置与页签显隐、无刷新钮/无「点刷新」指示、RETRY 改自动核对、三时点取数计数；02 占用/缺 Edge/unknown/正在打开点击报因（spawn 零、不假「已关闭」）、勾选还原、未勾选与关闭中走信息框、正式置灰与试采放行；04 两入口互斥显隐、runLoginCard/runBrowserFacts/changeScope 整链消失断言、替代动线、继续不置灰＋点击报因、title 两形与 retry 不进 title、开始钮无 title；明细见原文报告）。「服务端一行不改」逐字节核实成立：gui.py 唯一改动＝票 02 明文授权的 `get_start` 折入；`start_run`/`open_login_browser`/`_login_refusal`/`_browser_entry` 逐字节未动；`dedicated_browser.py` 仅 RETRY 文案。

与规格相悖 **1 条**（边缘态、无测试钉住；经主 agent 读码复核确认）：过程页入口在「未具备暂停资格」的无拒绝子态（遗留未正常结束轮/跨天轮）现为亮起可点——`ui_live.html` tickRun 的 `runLoginBrowserBtn.disabled = !!d.login_refusal`，而该态 `login_refusal` 为空且 `d.browser` 为 null（浏览器事实只在 `can_resume` 格查询，票 05 性能口径；`4d58cf3:571` 旧投影 `!(d.browser && can_open)` 在该态为灰）；Story 16/24、v2 grill 与票 04 验收②要求此类「置灰照旧＋悬浮提示给因」。服务端本就放行开窗（无数据风险），属投影回归。

处置：**挂账待用户定夺，未整改**（产品代码改动须经用户确认）。建议修法：`disabled = !!d.login_refusal || !d.can_resume`（一行；整改后须复审该轴并复跑相关测试与全量）；或经用户判定另行开票。

## 结论

Standards 轴 0 硬违例（2 判断项＋1 提示项记入并接受）；Spec 轴 0 缺失/半做、0 范围蔓延、1 条边缘态投影偏差挂账待用户定夺（未整改；不影响本候选自动验收与全量）。最终来源 `4fd2200`（其后仅票 03 收口记录提交 `a17edd6`）。
