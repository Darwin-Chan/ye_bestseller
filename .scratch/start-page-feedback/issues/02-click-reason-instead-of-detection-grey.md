# 02：浏览器检测/配置类不再预置灰：点击报因（页面内信息框）

Status: resolved（2026-10-10；票 02 实施并验收，03/04 未做）
Type: implementation
Feature: start-page-feedback
Date: 2026-10-10（Asia/Shanghai）
Blocked by: 01（同一文件顺排；01 新文案「界面会自动核对」指的就是本票的点击核对）
Spec: [../spec.md](../spec.md)（版本 2；票 04 复用本票交付的信息框壳）

**What to build:** 开始页两个按钮在**浏览器检测类**（专用 Edge 已占用/无法确认/打开失败/调试端口被占/正在打开）与**配置错误类**（缺 Edge/坏配置/外部连接模式）下不再预置灰。点「开始抓取」：先走既有取数链取一次最新开始页事实（**不丢失当前勾选**）——存在浏览器/配置门禁原因或开始提示就弹页面内信息框（原因优先级：浏览器/配置门禁文案 → 开始提示文案），否则照旧调用开始 API（其服务端拒绝路径不变，失败同样走信息框）。点「打开专用 Edge」：直接调用既有公开 API——失败弹原样原因；`reused`（已在运行/正在打开）弹「已有窗口」信息框（指向原窗口，绝不显示假的「已关闭」）；成功照旧刷新事实行。信息框 = 既有确认弹窗壳的单按钮变体（按钮文案「知道了」，隐藏取消）；「未勾选店铺」提示也改用它，不再用系统 alert。**保持置灰的格一切照旧**：计划锁（含勾选联动）、纯汇总机·正式（两按钮）、采集中/身份验证等待、停止收尾中、遗留轮、收尾欠账；对应的提示文案与处置卡（「中止此轮」「重试收尾核验」、过程页「继续」等）一律照旧。试采下登录入口不因角色置灰（票 08 `e2cee2c` 口径，不回归）。服务端授权与拒绝语义一行不改。

## 可验证验收

- [x] 占用态：开始页两按钮均亮；点「打开专用 Edge」弹「已有窗口」信息框（已在运行、请用原窗口、关闭后再开始采集）；点「开始抓取」弹信息框给出「请手动关闭专用 Edge（包括后台窗口）后，再开始抓取」类原因，且未 spawn 子进程、未建轮次。
- [x] 无法确认/打开失败/端口被占/配置错误（缺 Edge、坏配置、外部连接模式）：两按钮亮，点击弹同源原因的信息框；不显示假的「已关闭」、不接管无关实例（spawn 计数为零）。
- [x] 「正在打开」瞬时态点按不双开：弹对应信息框（reused），启动计数保持 1。
- [x] 正常路径零打扰：计划就绪、专用 Edge 未开、勾好店铺 → 点开始直接进入采集（无弹框）；点「打开专用 Edge」正常开窗并刷新事实行。
- [x] 点开始时的自动核对不丢失当前勾选；被拒绝的提示是页面内信息框（单按钮「知道了」），不是系统 alert；「未勾选店铺」也走信息框。
- [x] 保持置灰的格原样：计划锁、纯汇总机·正式、采集中/身份验证等待、停止收尾、遗留轮、收尾欠账在页面上仍为灰；相关既有断言保留，确因投影来源变化需调整的成对更新（如 `test_closed_launcher_with_background_profile_blocks_start` 的 `start_allowed` 断言），意图不变。
- [x] 试采：纯汇总机进「本机试采」后「打开专用 Edge」可点可用；切回正式恢复拒绝与置灰。
- [x] 过程页交互在本票范围内除改名外零变化（暂停后可用性、占用提示、继续门禁照旧；过程页自身的收整在票 04，届时按票 04 口径生效）；服务端拒绝路径未被绕过。
- [x] 红→绿：新增用例先红后绿覆盖上述点击流（沿用本线第一组高层接缝：实际页面事件 → 真实 Api/配置/视图）；邻域定向回归绿。

## 接缝

- `gui.Api.get_start`：`browser_error` 字段已备好；`start_allowed`/`start_hint` 目前还掺入浏览器/配置门禁（gui.py 458–462 的折入），这是「检测类预置灰」在服务器侧的来源，需还原为只表达计划/忙/遗留/欠账那半边。开始页两个按钮的前端禁用投影随之复位：登录按钮以 `login_refusal`（服务端拒绝）为唯一灰源，检测/配置类一律亮起（`can_open` 的 API 语义与过程页不动）。
- `bestseller_monitor/pages/ui_live.html`：开始页两按钮的禁用行（362/424 行区）与 `startBtn`/`loginBrowserBtn` 点击绑定（729–737 行区）、既有确认弹窗壳（`#confirmModal`，加单按钮信息变体）。
- `gui.Api.open_login_browser` 的返回形状（`ok/error/reused/browser`）与 `dedicated_browser` 的事实/文案保持原样，只改消费方式。
- 信息框壳交付后由票 04 复用（过程页入口与「继续」的点击报因）；本票不为 04 预留任何过程页改动。
- 测试落点：`tests/test_manual_edge.py`、`tests/test_ui_live.py`。

## 非目标

- 不改服务端授权/拒绝判据与文案；不改过程页交互（其收整见票 04——本票交付的信息框壳供其复用，但本票不动过程页任何行）；不加轮询；不改 `_BROWSER_ENTRY_BLOCKED` 的 API 语义（只改页面投影）。

## 实施记录（2026-10-10）

- 分支：`claude/start-page-feedback`（续用；起点 `a28cfcf`）。提交：`884a891`（代码+测试，4 文件 +253/−17）与本记录提交；未 push、未合 main（集成归票 03）。主仓工作树仍在 main、未动。
- 实施副本：`F:\AI\projects\bestseller\.scratch\sandboxes\start-page-feedback\02\20261010-t02-01\bestseller`（独立 clone，`-c safe.directory='*'` 在 clone 之前、`--no-hardlinks`；`config/config.toml` = example 仅 `start_browser=false`（逐字节、CRLF 保留）、`analysis.toml`、`shops.csv` 各一；写路径全在副本内，绝对路径仅只读 `chrome_path`）。
- 服务端（`gui.py` get_start，行 429–439 区）：浏览器/配置门禁原因（`_browser_entry` 的 `gate`）不再折入 `start_allowed`/`start_hint`，只保留在 `browser_error` 供点击核对；`login_refusal` 与 `can_open`/`_BROWSER_ENTRY_BLOCKED` 的 API 语义、`start_run`/`open_login_browser` 的拒绝面均未动。
- 页面（`ui_live.html`）：①登录按钮灰源改为 `login_refusal` 唯一（`can_open` 与「正在打开」不再置灰；防双开靠服务端 `reserve_open`/`reused`）；②`startRun` 先走取数链 `refreshStart()` 核对（`loadStart` 现返回取数结果），清单重绘后按店 key 还原勾选（`snapshotSelection`/`restoreSelection`）；门禁文案优先于 `start_hint`，两类与开始 API 失败（`!r.ok`）、「未勾选店铺」全部改走信息框；③信息框壳 `showInfo(opts)`（单按钮「知道了」、隐藏取消、不进确认动作；`openConfirm` 已补取消按钮复位，确认弹窗不受影响）；④`loginBrowserBtn` 点击：失败原样原因、`reused` 弹「已在运行/正在打开 + 原窗口 + 关闭后再开始采集」，成功照旧刷新事实行。过程页/暂停/继续/中止一行未动；`window.alert` 全页仅剩 `resumeRun` 一处（过程页，票 04 范围）。
- 红→绿：改动前基线 26/26 绿（`baseline-test_manual_edge.log`）；加断言后 33 项中 10 红（8 FAIL+2 ERROR，`red-test_manual_edge.log`）、`test_ui_live.py` 20 项中 2 红（1 FAIL+1 ERROR，`red-test_ui_live.log`）→ 实现后 33/33、20/20 绿（`green-*.log`，rc=0）。
- 成对更新（意图不变）：`test_closed_launcher_with_background_profile_blocks_start` 与 `test_unconfirmed_creation_does_not_allow_second_launch_or_collection` 的 `start_allowed` 断言改为「亮起 + `browser_error` 给因」（服务端 start_run 拒绝断言原样保留）；`test_unknown_permission_and_missing_edge_refuse_api_and_page` 的页面半边改为「两按钮亮 + 点击弹原样原因」；`test_ui_live.py` 的 STOP 桩 `get_start` 改为开始前形状（`crawler: null`/`start_hint: ""`；旧形状把「采集中」的 `start_hint` 给了可点击的按钮，与真实服务端不符），切页签用例计数 1→2／2→3（点开始新增一次核对取数）。
- 邻域回归（单文件、11 文件 297 项全绿，日志 `neighbor-*.log`）：test_paused_manual_login 13、test_local_trial_gui 20、test_leftover_rounds 26、test_identity_lifecycle 28、test_joint_acceptance 4、test_crawler_identity 11、test_explicit_pause_resume 16、test_abort_new_round 39、test_gui 87（＋本票两文件 33、20）。两个既有钉子（收尾欠账入口仍灰、验证等待仍灰）保持绿。
- 测试接缝坑两条（备查）：①`test_manual_edge` 的 pywebview 桩代理过去对任何属性都返回调度函数，页面自存属性 `api.__shops` 读不到（旧代码路径没触发过它）；已改为 `(n in t) ? t[n] : dispatcher`，与真实 pywebview 普通对象对齐。②`page.evaluate` 的表达式若以函数值收尾，Playwright 求值工具会把它当谓词调用一次（alert 桩自己数到自己，`;0` 收尾规避）。
- 视觉佐证：`t02_1_occupied_buttons_lit.png`（占用态两按钮亮）、`t02_2_open_click_existing_window_box.png`（「已有窗口」单按钮信息框）、`t02_3_start_click_reason_box.png`（「暂不能开始抓取」原样原因，背后勾选保持 2/3），脚本副本 `_shot_t02.py` 与 `sha256.txt` 同目录，归主仓 `.scratch/tool-output/start-page-feedback/02/20261010-t02-01/`。
- 口径说明：①「试采下入口可点可用」由既有 `test_merge_only_trial_allows_the_login_entry_and_returns_to_refusal` 覆盖（可点断言 + API 放行），点击走同一处理器，未另造用例。②「正常路径零打扰」由既有 `test_page_login_does_not_collect_or_create_round` 与 `test_ui_live` 的失败/重试用例覆盖（成功路径无弹框）。③`can_open` 字段仍照常返回（API 语义未动），只是页面不再拿它做禁用投影。④全量不在本票跑（留给 03 收尾）。
- 行尾：四个提交 blob 均 CR=0（无行尾翻动；工作树 `test_manual_edge.py` 因 python 补丁为 LF，autocrlf 提交归一）。

## 目录与证据处置

- 主仓：F:\AI\projects\bestseller
- 隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\start-page-feedback\02\20261010-t02-01\bestseller（独立 clone；分支 `claude/start-page-feedback` 在此检出并提交至本记录提交；未复制主仓真实配置/库/登录态）
- 来源：分支 `claude/start-page-feedback` 顶点 `a28cfcf`（票 01 已交付）＋本票提交 `884a891` 与记录提交；主仓分支 ref 经 fetch 回写，主仓工作树未动
- 验收证据：F:\AI\projects\bestseller\.scratch\logs\tickets\start-page-feedback\02\20261010-t02-01（baseline/red/green/neighbor-*.log；截图与 sha256、`_shot_t02.py` 在主仓 `.scratch\tool-output\start-page-feedback\02\20261010-t02-01`）
- 永久保留：上述证据、截图与截图脚本（`.scratch` 不进 Git）
- 可删除候选：本副本 `.scratch\sandboxes\start-page-feedback\02\20261010-t02-01`（票 03/04 各自另建副本；删除需另行授权）
- 暂留待确认：同上副本，保留待本线收口或用户决定
- 实际清理：未执行（未获授权）

## Comments

2026-10-10：票 02 实施并验收（本轮会话，主 agent）。只改开始页点击流/投影与 `get_start` 的折入；服务端授权与拒绝语义一行未改；过程页一行未动。信息框壳 `showInfo({title, sub})` 在 `ui_live.html`（`closeConfirm` 旁）交付，票 04 直接复用；`openConfirm` 已复位取消按钮显示，两壳可交错使用。03 收尾提示：集成（合 main）需用户授权后按票 03 走；全量留给 03；本票未 push。04 提示：过程页「继续」（`resumeBtn`）与入口 `runLoginBrowserBtn` 的点击报因直接改用 `showInfo`，其禁用投影与悬浮提示按票 04 口径另改。
