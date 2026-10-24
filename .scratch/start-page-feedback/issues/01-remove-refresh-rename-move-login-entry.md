# 01：开始页入口收拢：删「刷新浏览器状态」、两页改名、按钮上移右上角

Status: resolved（2026-10-10；票 01 实施并验收，02/03 未做）
Type: implementation
Feature: start-page-feedback
Date: 2026-10-10（Asia/Shanghai）
Blocked by: None（可立即开始）
Spec: [../spec.md](../spec.md)（版本 1；代码快照 33b89f8）

**What to build:** 开始页页头右上角出现「打开专用 Edge」按钮（在「进入本机试采」左边，仅开始页签显示）；全页删除「刷新浏览器状态」按钮（HTML 与绑定一并删）；开始卡只保留浏览器事实行与说明文字。全仓不再出现指向该按钮的「点刷新」指示：浏览器 facts 的 retry 文案与开始卡说明句改为自动核对口径（「确认退出后再开始采集（界面会自动核对）」一类同义句，措辞要让开始页与过程页两处同源显示都读得通）。过程页同文本按钮「打开专用 Edge / 登录目标网站」一并改名「打开专用 Edge」，行为不变。原有的自动核对时点（进页、切回开始页签、切换试采）全部保留——删按钮不等于减少核对。本票只动位置、名字与文案，**不动任何禁用投影与点击逻辑**（那是 02 的事）。

## 可验证验收

- [x] 开始页签：页头右侧依序为「打开专用 Edge」「进入本机试采」；开始卡内不再有登录入口按钮，也不再有刷新按钮。
- [x] 非开始页签（过程/结果）不显示页头「打开专用 Edge」按钮；过程页自有的登录入口卡片照旧显示（仅名字变化）。
- [x] 过程页登录入口文案为「打开专用 Edge」，其可用性/禁用/提示行为与改名前逐项一致（只有文案变化）。
- [x] 全仓无「刷新浏览器状态」残留；不再有「点刷新」类指示（含 `dedicated_browser.RETRY` 与开始卡说明句）。
- [x] 自动核对时点不变：进页取一次事实；切回开始页签、切换试采后事实行随之更新（页面用例断言）。
- [x] 受影响的既有测试接缝成对更新：`tests/test_manual_edge.py` 中以点击 `#refreshBrowserBtn` 充当刷新触发的用例改用等价触发（如直接走取数链），断言意图不变。
- [x] 目标文件回归绿（`-p` 单文件）。

## 接缝

- `bestseller_monitor/pages/ui_live.html`：页头（69 行区）、开始卡（83 行区）、`init()` 里的绑定（735 行区）、过程页按钮文案（166 行区）。
- `bestseller_monitor/dedicated_browser.py` 的 `RETRY`（15 行）：开始页与过程页事实行同源显示，改文案时两处语境都要通。
- 测试落点：`tests/test_manual_edge.py`。

## 非目标

- 不改任何按钮的禁用/点击流程（属 02）；不改过程页交互逻辑；不加轮询；不改服务端。

## 实施记录（2026-10-10）

- 分支：`claude/start-page-feedback`（自本地 main `4d58cf3` 起；本线三票共用这一支；未 push、未合 main）。主仓工作树未切换分支（在副本中检出并提交，提交经 `git fetch` 回写主仓分支 ref）。
- 实施副本：`F:\AI\projects\bestseller\.scratch\sandboxes\start-page-feedback\01\20261010-t01-01\bestseller`（独立 clone，`-c safe.directory='*'` 在 clone 之前、`--no-hardlinks`；`config/config.toml` = example 仅改 `start_browser=false`（逐字节，CRLF 保留）、`config/analysis.toml` = example、`config/shops.csv` = example；全部写入路径随相对路径落在副本内，另扫过绝对路径仅 `chrome_path` 只读可执行文件；未复制主仓真实配置、数据库或登录态）。
- 改动 3 文件、+68/−6（提交 `97f526a`）：`ui_live.html`（页头新横向容器：`loginBrowserBtn` 改名「打开专用 Edge」置于「进入本机试采」左侧、`showTab` 里仅开始页签显示；开始卡删两按钮、只留事实行与说明文字；过程页 `runLoginBrowserBtn` 一并改名；删 `refreshBrowserBtn` 的 HTML 与绑定）、`dedicated_browser.py` `RETRY` 去「点刷新」改为「确认退出后即可继续（界面会自动核对）」、`tests/test_manual_edge.py`（新增 4 条页面用例 + 1 条自动核对 pin；既有点击 `#refreshBrowserBtn` 的刷新触发改用 `page.evaluate('refreshStart()')` 等价触发，断言意图不变）。禁用投影与点击逻辑一行未动（属 02）。
- 红→绿：改动前基线 21/21 绿；新断言红 4 项（`red-test_manual_edge.log`，改动前的 26 项运行：4 FAILED）→ 实现后 26/26 绿（`green-test_manual_edge.log`，rc=0）。自动核对 pin 用例改动前即绿（守卫性断言，用于锁「删按钮不减少核对」）。
- 邻域回归（单文件，11 文件 289 项全绿，日志 `neighbor-*.log`）：test_paused_manual_login 13、test_local_trial_gui 20、test_leftover_rounds 26、test_ui_live 19、test_identity_lifecycle 28、test_joint_acceptance 4、test_crawler_identity 11、test_explicit_pause_resume 16、test_abort_new_round 39、test_gui 87。
- 视觉佐证：`tools/_shot_live.py`（mock API）截图开始页/过程页，归主仓 `.scratch/tool-output/start-page-feedback/01/20261010-t01-01/`（含 `sha256.txt`）：页头依序两按钮、开始卡内无按钮、过程页隐藏页头入口且自理卡片按钮已改名，与断言一致。该脚本在第 4 步（暂停→继续）超时——mock 的 `get_run` 缺 `can_resume` 字段，属既有工具与页面演进的漂移，与本票无关，未修（超出票范围）。
- 口径说明：①「全仓无残留」在产品面落实（`bestseller_monitor/`、`gui.py`、入口脚本、tests 均无 `refreshBrowserBtn`/「刷新浏览器状态」；「点刷新」仅存于本文件历史文本与 `docs/design/issues-08-11-*` 归档交付记录，后者按 spec Further Notes 归「上线后同步」，不在本票改动）。②`ui_live.html` 内部 JS 注释保留「刷新/取数」行话（取数链语义，非用户可见指示）。③新用例字符串中出现「刷新浏览器状态」是为断言其不存在。
- 全量不在本票跑（留给 03 收尾），符合本线分工；本票无生产服务端改动。

## 目录与证据处置

- 主仓：F:\AI\projects\bestseller
- 隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\start-page-feedback\01\20261010-t01-01\bestseller（独立 clone；分支 `claude/start-page-feedback` 在此检出并提交至本记录提交；未复制主仓真实配置/库/登录态）
- 来源：本地 main `4d58cf3`（= 本线 spec+三票提交顶点）＋本票提交 `97f526a`（代码+测试）与记录提交；主仓分支 ref 经 fetch 回写，主仓工作树未动
- 验收证据：F:\AI\projects\bestseller\.scratch\logs\tickets\start-page-feedback\01\20261010-t01-01（baseline/red/green/neighbor-*.log；截图与 sha256 在主仓 `.scratch\tool-output\start-page-feedback\01\20261010-t01-01`）
- 永久保留：上述证据与截图（`.scratch` 不进 Git，跨机器需另行打包）
- 可删除候选：本副本 `.scratch\sandboxes\start-page-feedback\01\20261010-t01-01`（票 02/03 将按规范各自另建副本；删除需另行授权）
- 暂留待确认：同上副本，保留待本线收口或用户决定
- 实际清理：未执行（未获授权）

## Comments

2026-10-10：票 01 实施并验收（本轮会话，主 agent）。只动位置/名字/文案；02（点击报因）与 03（收口合 main）未做——本票提交只到 `claude/start-page-feedback` 分支，未并入 main、未 push。02 开工提示：本票未触碰任何禁用投影与点击流程（`loadStart` 的 `loginBrowserBtn.disabled`、`startBtn.disabled` 均原样）；页头入口的显隐现由 `showTab` 单一处驱动，02 若要改点击流注意沿用。
