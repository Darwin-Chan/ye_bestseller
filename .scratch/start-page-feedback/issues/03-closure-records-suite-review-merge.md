# 03：收口：验收语言与记录、收尾全量、双轴审查、按授权并入 main

Status: resolved（2026-10-11；①–⑤ 全部完成并记录——审查整改 96be8c7＋复审通过，并入 main c7f5a9c＋适配 06bd22a，合并后全量 2016/2016；未 push）
Type: implementation
Feature: start-page-feedback
Date: 2026-10-10（Asia/Shanghai）
Blocked by: 01、02、04（04 与 02 同文件顺排）
Spec: [../spec.md](../spec.md)（版本 3）

**What to build:** 本批收口。① issues-08-11 验收语言按新口径更新：08-A12/A14 的「入口禁用」表述改为「点击报因＋服务端拒绝」；母 spec 可用性表相关脚注同步（正式模式格不变；试采格以票 08 口径为准），两份文档不得互相矛盾。v2 补充：该措辞更新的范围扩至过程页入口（检测/配置类点击报因、忙/资格类维持禁用）；另在母 spec 描述改范围路径处与《issues 处理状态台账》issues-08-11 小节**各补一句**「改范围按钮已删（快捷方式；路径语义不变，来自 start-page-feedback 票 04）」。v3 补充：08 系材料凡引「开始页事实行／入口禁用可见原因／本机配置与专用目录可查看」处（母 spec、08 票、`docs/design/` 走查/交付记录、台账）改写为「点击报因＋悬停（悬浮提示）可见」口径；`docs/reviews/` 归档评审报告不改，其中 `browserFactsText(d)` 抽取建议随之作废（在台账或本批记录里点一句即可）。② 交付/台账记录按本仓惯例更新（issues 处理状态台账补记等）。③ 隔离副本分段全量：主仓绝对路径 `--log-dir`、不加 `-f`，抄「合计/对账/结论」三行与退出码。④ 双轴独立审查（Standards/Spec，最小上下文只读子代理，两轴不合并排序），报告归档。⑤ 经用户明确授权后用 resolving-merge-conflicts 流程并入 main——未授权不合并、不 push。

## 可验证验收

- [x] 验收语言更新落盘：08-A12/A14 及母 spec 相关脚注与新行为一致、无互相矛盾；母 spec 与台账各含「改范围按钮已删（快捷方式；路径语义不变）」一句；「开始页可见原因／配置可查看」处的改写落盘（含 `docs/design/` 走查记录；`docs/reviews/` 归档报告未改、其建议作废一句已点）。（2026-10-11 完成，7 份文档，见实施记录①；边界注明：issue 08/11 的 01–07 历史票与 grill 其余段落未动——历史工单档不改写，旧按钮名仅在因三类引用而改写的句内随之更新。）
- [x] 交付/台账记录更新（日期、提交、验证结果、证据路径）。（台账 2026-10-11 状态行＋08/11 两处补句；本文件实施记录；证据路径见下。）
- [x] 隔离副本分段全量：合计/对账/结论三行齐全、7/7 段 OK、退出码 0；证据落 `.scratch/logs/tickets/start-page-feedback/03/<run>/`。（合计 **2011 项（预期 2011，计数对账一致）、总耗时 1201.3s、7/7 段 OK、结论 OK**、退出码 0；`03/20261011-t03-01/`。）
- [x] 两轴审查报告归档，0 未解决硬性项/阻断项（或整改后复审通过、记录整改提交）。（报告归档（`review-r1/` 任务与原文＋`docs/reviews/start-page-feedback-batch-review-2026-10-11.md`）；Standards 0 硬违例；Spec 1 条边缘态投影偏差已整改——`96be8c7`（灰源补 `!d.can_resume`＋新用例先红后绿）并经复审通过（复审报告 `.scratch/logs/tickets/merge-start-page-feedback-into-main/01/rework-review-report.md`：「偏差已关闭、无新引入问题」），无未解决项。）
- [x] 并入 main 仅在用户明确授权后执行；合并后按惯例留 main 侧验证证据。（用户 2026-10-11 指令「整改偏差，然后授权合并进 main」（经 /resolving-merge-conflicts 流程）；合并 `c7f5a9c`＋适配 `06bd22a`；合并树分段全量 **2016/2016、对账一致、7/7 段 OK、退出 0**；证据 `.scratch/logs/tickets/merge-start-page-feedback-into-main/01/`。未 push。）

## 接缝

- 验收语言：`.scratch/issues-08-11/spec.md`（含描述改范围路径的行区）与 `.scratch/issues-08-11/issues/08-trial-login-entry-and-start-page-reason.md` 的验收表/措辞；台账 `docs/ops/issues处理状态台账-2026-10-08.md`（2026-10-11 该台账移至 `.scratch/tool-output/issues/issues处理状态台账-2026-10-08.md`，本机、不进版本库）；v3 扩：`docs/design/issues-08-11-grill-decisions-2026-10-08.md`（「本机配置路径及专用数据目录可查看」句）与 `docs/design/issues-08-11-ticket07-delivery-2026-10-09.md`（人工走查「开始页核对配置来源与专用目录」步）。
- 审查报告落 `docs/reviews/`（受跟踪）；全量证据落主仓 `.scratch/logs/tickets/`。
- 合流按本仓惯例：分支在隔离副本验证后并入，主仓侧验证与记录提交。

## 非目标

- 无产品代码改动（若审查/全量暴露缺陷，另开票或经用户确认后在本批整改并留痕）；不关闭 issue 08/11（人工项 08-H01/11-H01 仍待用户现场反馈）。

## 实施记录（2026-10-11）

- 分支：`claude/start-page-feedback`（续用；顶点 `4fd2200` 起，未重建）。本票记录提交位于分支末尾；未 push、未合 main（⑤ 待用户明确授权）。
- 隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\start-page-feedback\03\20261011-t03-01\bestseller`（独立 clone，`-c safe.directory='*'` 在 clone 之前、`--no-hardlinks`；clone 后 `git checkout -B main 4fd2200` 使副本本地 `main` 指向被测树——满足 `test_sku_sharing` 的 legacy-vs-main 布局要求；`config/config.toml` = example 仅改 `start_browser=false`（逐字节、CRLF 保留）、`analysis.toml`、`shops.csv` 各一；三件配置归档于证据目录，config.toml 哈希与 01/02/04 副本逐字节一致）。
- ① 验收语言同步（7 份文档；改前/改后快照与逐份 diff 在证据 `lang-sync/`，`sha256.txt` 同目录）：
  - 未跟踪 2 份（主仓磁盘直改）：`.scratch/issues-08-11/spec.md`——08-A12/A14 改「点击报因／悬停＋服务端拒绝」两分口径；可用性表后新增「2026-10-11 按 start-page-feedback 批（票 03）同步界面呈现」段（禁用两形、两页入口同口径、事实面退场、服务端语义不变、纯汇总机·正式与试采格以票 08 为准）；Solution 行 37 旧按钮名与「及说明」随注同步；决策 7 改范围处补「（补：……一键快捷方式已删……路径语义不变。）」。`.scratch/issues-08-11/issues/08-trial-login-entry-and-start-page-reason.md`——标题、What to build②、Q4、验收①、接缝 5 处加同步注（保留原词、注明新形态与出处）。
  - 受跟踪 5 份（已落主仓磁盘；提交落点照集成先例＝并入 main 时的 main 侧收尾提交，本记录提交不携其内容）：`docs/ops/issues处理状态台账-2026-10-08.md`（2026-10-11 该台账移至 `.scratch/tool-output/issues/issues处理状态台账-2026-10-08.md`，本机、不进版本库）——§4 08 节补记加同步注、§4 11 节补「改范围按钮已删（快捷方式；路径语义不变）」句、§6 追加 2026-10-11 语言同步行、08 历史行加指向新行的短注；`docs/design/issues-08-11-grill-decisions-2026-10-08.md`——行 185「可查看」加「为悬浮提示里的悬停查看」注；`docs/design/issues-08-11-ticket04/ticket05/ticket07-delivery-2026-10-09.md`——三份「部署机人工步骤」按新界面同步（悬停核对配置/状态句、点击报因、无事实行、无刷新钮、入口上移页头、按钮名随句更新），ticket07 集成记录追加 2026-10-11 补条。
  - 边界与口径：`docs/reviews/` 归档报告一律未改；`browserFactsText(d)` 抽取建议作废一句已点进台账（§6 2026-10-11 行）；issue 08/11 的 01–07 历史票与 grill 其余段落未动（改范围句按 grill Q4 口径只补母 spec 与台账两处）。
  - 行尾说明：Edit 工具将三份混合行尾文件归一（spec.md 1 处 CRLF→LF、grill-decisions 26 处 LF→CRLF、ticket07 1 处 LF→CRLF）；autocrlf=true 下 git diff 无行尾噪音（`tracked-docs.diff` 仅内容变更）。
- ② 交付/台账记录：台账更新见上（§6 行含证据路径）；本文件即交付记录。
- ③ 隔离副本分段全量（副本根、`--log-dir` 主仓绝对路径、不加 `-f`）：**合计 2011 项（预期 2011，计数对账一致）、总耗时 1201.3s、段结果 7/7 段 OK、结论 OK；退出码 0**（全段 0 FAIL/ERROR）。重段 268 项＋其余段 1743 项（段 1–6：exchange 72、judgment_set 72、plan_step 60、export 30、git_channel 17、shops_sync 17）。逐段日志 `seg-01..07-*.log`、`summary.txt`、`full-suite-01-console.log`、`full-suite-01-exit-code.txt` 同目录。
- ④ 两轴独立审查（只读子代理、最小上下文、两轴不合并排序；任务文件与报告原文在 `review-r1/`；对象＝`4d58cf3..4fd2200` 整批 13 文件）：
  - Standards：0 硬违例；判断项 2 条（悬浮提示两形拼接逐字重复、`openManualBrowser` 未消费返回值）＋提示项 1 条（`.scratch` 强跟踪与 README/.gitignore「.scratch 不进代码仓」相抵——沿袭前例、待人工定，非本批新开）；处置：接受、不整改（判据同 08 票审查先例「不为体例重整打断已测来源」，位点在报告）。
  - Spec：0 缺失/半做、0 范围蔓延（「服务端一行不改」逐字节核实成立：gui.py 唯一改动＝票 02 明文授权的 get_start 折入）；与规格相悖 **1 条**（边缘态、无测试钉住，主 agent 已读码复核成立）：过程页入口在「未具备暂停资格」的无拒绝子态（遗留未正常结束轮/跨天轮）现亮起可点——`ui_live.html` tickRun 的 `runLoginBrowserBtn.disabled = !!d.login_refusal`，而该态 `login_refusal` 为空且 `d.browser` 为 null（浏览器事实只在 `can_resume` 格查，票 05 性能口径），旧投影 `!(d.browser && can_open)` 在该态为灰；Story 16/24、v2 grill 与票 04 验收②要求此类「置灰照旧＋悬浮提示给因」。服务端本就放行开窗（无数据风险），属投影回归。**处置：挂账待用户定夺，未整改**（产品代码改动须经用户确认）；建议修法 `disabled = !!d.login_refusal || !d.can_resume`（一行；整改后须复审该轴并复跑相关测试与全量）。
- ⑤ 并入 main：**待用户明确授权**（本轮会话未含授权句）。未合并、未 push。合并动作将另建 run/记录。

## 目录与证据处置

- 主仓：F:\AI\projects\bestseller
- 隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\start-page-feedback\03\20261011-t03-01\bestseller（独立 clone；被测树＝分支顶点 `4fd2200`；未复制主仓真实配置/库/登录态）
- 来源：分支 `claude/start-page-feedback` 顶点 `4fd2200`（`4d58cf3` + 01/02/04 全部提交）＋本记录提交；主仓分支 ref 经 fetch 回写，主仓工作树未动
- 验收证据：F:\AI\projects\bestseller\.scratch\logs\tickets\start-page-feedback\03\20261011-t03-01（来源清单、三件配置、分段全量与 summary/退出码、`lang-sync/`（before/after/diffs/sha256.txt）、`review-r1/`（任务与报告原文））；合并验证证据 `.scratch\logs\tickets\merge-start-page-feedback-into-main\01`（合并净改动 diff、定向与全量日志、复审任务与报告、来源记录）
- 永久保留：上述全部；主仓 7 份同步文档的改后版本（未跟踪 2 份在主仓磁盘 `.scratch/issues-08-11/`；受跟踪 5 份与 `docs/reviews/` 整理版报告已随 2026-10-11 收尾提交落定）
- 可删除候选：本副本 `.scratch\sandboxes\start-page-feedback\03\20261011-t03-01`（删除需另行授权）
- 暂留待确认：同上副本；本线四副本（01/02/04/03）保留——本线已收口，删除待用户授权
- 实际清理：未执行（未获授权）

## 集成记录（2026-10-11）

经用户明确授权（原句：「整改偏差，然后授权合并进 main」——经 /resolving-merge-conflicts 流程指令），本线并入 main；本节为落地后所记。审查整改先于合并落定：`96be8c7`（过程页入口灰源补 `!d.can_resume`＋新用例先红后绿；复审通过——报告见 `.scratch/logs/tickets/merge-start-page-feedback-into-main/01/rework-review-report.md`）。上方正文保留为合流前的事实。

- 合并链：`c7f5a9c`（本线顶点 `96be8c7` × main@`340f036`——local-trial-merge-only 线并入后的顶点；**唯一内容冲突** `bestseller_monitor/pages/ui_live.html` 页头区：对线「试采入口收窄为纯汇总机功能（`trialModeBtn` 默认 hidden、由 `d.trial_available` 投影）」× 本线「页头横向容器＋两枚『打开专用 Edge』入口」——解法：容器结构用本线、hidden 与注释用对线，双方意图都保留；`gui.py` 与 `tests/test_ui_live.py` 自动合并，逐块核过——对线 `trial_available` 投影 × 本线 `get_start` 折入、两侧用例并存）→ `06bd22a`（合并适配：`test_fact_rechecks_stay_automatic_on_tab_return_and_trial_switch` 按收窄后语义改 merge_only 夹具、试采态起步、末尾切回正式；三个核对时点断言不变）。
- 合并树验证：定向九文件全绿（ui_live 23、manual_edge 35、local_trial_gui 21、paused 17、gui 87、joint 4、explicit 16、local_trial_cli 25、leftover 26）；分段全量 **2016 项（预期 2016，计数对账一致）、总耗时 1209.7s、7/7 段 OK、结论 OK、退出码 0**。主仓 fast-forward 落地（`340f036 → 06bd22a`，工作区未提交改动保留不涉）。证据：`.scratch/logs/tickets/merge-start-page-feedback-into-main/01/`。
- 语言同步的受跟踪 5 份（台账、`docs/design/` ×4）与本批两轴报告随本线收尾提交一并落定（`docs: record the start-page-feedback main integration and its verification`；见台账 2026-10-11 集成行）。
- 本线「未合并」旧边界自此解除；08/11 剩余未完成仅部署机人工验收（08-H01/11-H01）待用户现场反馈，两 issue 保持未处理。未 push。

## Comments

2026-10-11：票 03 收口执行（本轮会话，主 agent）。①②③④ 完成：语言同步 7 份落盘（快照与逐份 diff 留证）、台账更新、分段全量 2011/2011 对账一致 7/7 段 OK 退出 0、两轴审查完成（Standards 0 硬违例；Spec 1 条边缘态投影偏差经复核确认、**挂账待用户定夺**——见实施记录④，未整改）。⑤ 并入 main 待用户明确授权；本票未 push。待人项：用户对偏差项定夺（整改／另开票／接受挂账）＋ ⑤ 授权。

2026-10-11（后记）：用户经 /resolving-merge-conflicts 指令「整改偏差，然后授权合并进 main」。整改 `96be8c7`（红→绿＋复审通过）；合并 `c7f5a9c`＋适配 `06bd22a` 已落地 main（fast-forward `340f036 → 06bd22a`）；合并树定向九文件全绿、分段全量 2016/2016、7/7 段 OK、退出 0。本票五步全部完成，置 resolved。未 push。
