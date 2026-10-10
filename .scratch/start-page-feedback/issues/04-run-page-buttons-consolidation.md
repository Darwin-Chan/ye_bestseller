# 04：浏览器事实面收整：过程页入口上移＋点击报因、删两张事实卡、删「改范围」

Status: resolved（2026-10-11；票 04 实施并验收，03 未做）
Type: implementation
Feature: start-page-feedback
Date: 2026-10-10（Asia/Shanghai；2026-10-11 修订：并入「删开始页浏览器事实卡」为改动三，伞题名改宽）
Blocked by: 02（同一文件顺排；复用其单按钮信息框壳。02 已交付，本票可开工）
Spec: [../spec.md](../spec.md)（版本 3）
决策记录: F:/AI/projects/bestseller/.scratch/start-page-feedback/grill-decisions-run-page.md（过程页按钮收整）与 grill-decisions-start-card.md（删开始页事实卡）（均为工作材料，未跟踪；本机主仓）

**What to build:** 过程页的「打开专用 Edge」上移页头（与开始版同槽、按页签互斥显示；过程页签有轮次才显，结果页签不显），「暂停后处理登录」整卡删除（含浏览器事实行与说明文字）。入口与「继续」跟随票 02 的点击报因口径：非浏览器检测类（采集中/验证等待/停止收尾中/未具备暂停资格/收尾欠账等服务端拒绝类）保持现状置灰、原因挂按钮悬浮提示；浏览器检测类与配置错误类（占用/无法确认/打开失败/端口被占/正在打开/缺 Edge/坏配置）亮起，点击弹单按钮「知道了」信息框报因（复用票 02 的壳）。「继续」can_resume 时显示、不再因 continue_gate 置灰；点击直调既有 resume_run（服务端为权威检查），失败弹信息框原样原因；暂停横幅照旧讲状态，「关闭窗口不会自动继续本轮」句迁入横幅。删「改范围（中止后重选）」前端整链（按钮、禁用行、changeScope 与自动回开始页机制、doAbort 的 changeRange 形参、绑定），替代动线＝中止本轮 →（等收尾）→ 结果页「返回开始」→ 重选 → 明确开始；不加替代按钮与教学旁注。**（改动三）删开始页第一张卡整卡**（浏览器事实行与说明文字，HTML 与 JS 写入一并清）：原因类信息仍全部走点击信息框（票 02 交付，不回归）；配置来源、专用目录、状态句折叠进「打开专用 Edge」按钮的悬浮提示——常态「配置：…；专用目录：…；状态句」，被服务端拒绝而置灰时「拒绝句；配置：…；专用目录：…」；retry 尾句不进悬浮提示（只活在信息框）；开始按钮不加悬浮提示（置灰原因均已有横幅/处置卡面）；两页由此在 UI 层不再有任何浏览器事实面。服务端一行不改（改范围＝确认中止＋收尾＋重选＋明确开始的 Q8 语义原样）。

## 可验证验收

- [x] 过程页签有轮次时，页头右侧依序「打开专用 Edge」「进入本机试采」；无轮次时过程版隐藏；结果页签两版都不显；「暂停后处理登录」整卡与事实行（`runBrowserFacts`）不再存在；开始页入口保持票 01 交付原样（其事实行与说明文字随改动三删除）。
- [x] 忙/资格类（采集中/验证等待/停止收尾中/未具备暂停资格/收尾欠账）置灰照旧且悬浮提示给出原因；检测/配置类在暂停资格具备后亮起、点击弹信息框（同源原因）；被拒绝时不误开（启动计数为零）、不显示假的「已关闭」、不接管无关实例。
- [x] 「继续」：can_resume 时显示；带 continue_gate（占用/配置等）时不再置灰，点击弹信息框原样原因且 spawn 计数为零；无门禁时照旧恢复原轮、不重复已完成任务；「关闭窗口不会自动继续本轮」在暂停横幅可见。
- [x] 删「改范围」：按钮与前端机械（含自动跳回开始页）消失；取消确认无副作用；「中止本轮」→ ABANDONED → 结果页 →「返回开始」→ 改勾选 → 开始抓取，新轮意图携带新范围、不再触发范围冲突；Q8 路径语义与服务端拒绝逐项一致。
- [x] （改动三）开始页签无浏览器事实面残留：事实行元素与其写入代码、说明文字均不存在（HTML/JS 零引用）。
- [x] （改动三）「打开专用 Edge」悬浮提示两形：常态＝「配置：…；专用目录：…；状态句」；被服务端拒绝而置灰（纯汇总机·正式等）时＝「拒绝句；配置：…；专用目录：…」；开始按钮不加悬浮提示（其置灰原因仍有横幅/处置卡的面，原样）。
- [x] （改动三）删卡不减少核对：取数时点一个不少（进页、切回开始页签、试采切换、两个按钮点击）；正常成功路径零打扰；开始页其余行为＝票 01/02 交付原样——投影与报因矩阵（占用/无法确认/配置错误/正在打开/未勾选/启动失败全走信息框）不回归。
- [x] 红→绿：新增/改写页面用例先红后绿（沿用本线第一组高层接缝：实际页面事件 → 真实 Api/配置/视图）；成对更新按下方清单；邻域单文件回归绿。

## 接缝

- `bestseller_monitor/pages/ui_live.html`（行区以落地时现状为准）：页头容器与 `showTab`（两枚元素同槽互斥显隐）；过程页按钮行（删 `changeScopeBtn`）；`runLoginCard` 整卡删除及其显隐行；过程页禁用行与绑定（`runLoginBrowserBtn`、`resumeBtn`、删除 `changeScopeBtn` 绑定）；信息框壳复用票 02 交付的 `#confirmModal` 单按钮变体。**（改动三）**开始页第一张卡（浏览器事实行＋说明文字）整卡删除、开始页取数回填处的事实行写入删除、「打开专用 Edge」悬浮提示两形的拼接点（常态/置灰）。
- API 消费：`gui.Api.get_run` 的 `browser` 事实与 `continue_gate`、`open_login_browser` 的 `ok/error/reused`、`resume_run` 的拒绝原因均原样消费；`prepare_abort`/`confirm_abort` 保留（中止本轮/遗留轮共用）；服务端零改动（改动三亦然：`browser_error`/`login_refusal` 字段与语义不动，只换页面消费面）。
- 成对更新测试：`tests/test_paused_manual_login.py`（`runBrowserFacts` 断言改信息框断言）、`tests/test_joint_acceptance.py`（J-A01 同）、`tests/test_explicit_pause_resume.py`（change-scope 用例改走 `#abortBtn`＋`#backBtn`，断言意图不变）、`tests/test_manual_edge.py`（页头槽位与显隐接缝扩展）；`tests/test_ui_live.py` 视信息框接线补页面事件断言。（改动三扩：`tests/test_manual_edge.py` 页夹具就绪判据与 6 个读 `#browserFacts` 的用例（含 `test_start_page_shows_why_the_login_entry_is_disabled` 改断言悬浮提示）；`tests/test_joint_acceptance.py` J-A01 开始页段；「删按钮不减少核对」钉子换观测面（拟用取数调用计数）；`_shot_t02.py` 截图脚本与票 02 佐证同步重拍。）

## 非目标

- 服务端授权/拒绝判据与文案；开始页除改动三外的行为（票 01/02 的交付：投影、信息框、报因矩阵不回归）；关窗/暂停/继续语义本身；机器角色档位；其它按钮（暂停/中止/试采切换/重试准备/中止此轮）；母 spec/台账补句与验收语言同步（归票 03）。

## 实施记录（2026-10-11）

- 分支：`claude/start-page-feedback`（续用；顶点 `59995af` 起，未重建）。提交：`ad57547`（页面+测试 6 文件，+379/−102）与本记录提交；未 push、未合 main（集成归票 03）。主仓工作树仍在 main、未动。
- 实施副本：`F:\AI\projects\bestseller\.scratch\sandboxes\start-page-feedback\04\20261011-t04-01\bestseller`（独立 clone，`-c safe.directory='*'` 在 clone 之前、`--no-hardlinks`；`config/config.toml` = example 仅改 `start_browser=false`（逐字节、CRLF 保留）、`analysis.toml`、`shops.csv` 各一；写路径全在副本内，绝对路径仅只读 `chrome_path`）。
- 页面（`ui_live.html`，服务端零改动）：
  - 改动一：`runLoginBrowserBtn` 移入页头横向容器（`loginBrowserBtn` 与试采开关之间；初始隐藏）；新增 `currentTab`/`runHasRound` 与 `syncLoginEntries()`——开始页签显开始版、过程页签有轮次显过程版、结果页签两版都不显（`showTab` 与 `refreshOnce` 双驱动）；`runLoginCard` 整卡删除（含 `runBrowserFacts`）；过程版投影改为「灰源＝`login_refusal` 唯一、原因挂 title」；`resumeBtn` 去掉 `disabled=!!continue_gate` 置灰支、点击失败改 `showInfo({title:"暂时无法继续"})`（原样原因；`window.alert` 全页已清零）；暂停横幅迁入「关闭专用 Edge 窗口不会自动继续本轮」。
  - 改动二：删 `changeScopeBtn` 按钮/两行禁用/`changeScope()`/`rangeChangeRound` 变量与其在 `renderResult` 的自动跳回/`doAbort` 的 `changeRange` 形参/绑定；`prepare_abort`/`confirm_abort` 原样保留；`doAbort(confirmation, stayOnStart)` 两个调用点（遗留轮、收尾重试）成对改参。
  - 改动三：开始页第一张卡整卡删除（`browserFacts` 元素、说明文字与 `loadStart` 写入一并清）；`loginBrowserBtn.title` 两形（常态「配置：…；专用目录：…；状态句」/置灰「拒绝句；配置：…；专用目录：…」）；retry 尾句不进 title；开始按钮不加 title。
  - 两页共用新的 `openManualBrowser(button, after)`（成功/失败/reused 三分支＋回程句），开始版与过程版点击报因单源（过程版尾句指向「继续」）。
- 红→绿（从副本根：`python -X utf8 -m unittest discover -s tests -p "<file>.py"`）：
  - baseline：test_manual_edge 33/33、test_ui_live 20/20 绿（`baseline-*.log`）。
  - red：manual_edge 35 项 22 错（页面夹具就绪判据改挂按钮悬浮提示后，全体页面用例按预期红在「title 未写入」）；ui_live 22 项恰 2 错（两条新用例）；paused 2 FAIL+3 ERROR（改写的 5 条）；explicit 1 FAIL（改写的中止动线用例）；joint 1 ERROR（J-A01）（`red-*.log`，rc 全非 0）。
  - 补红（新用例单例）：未知态过程版入口点击报因那条为批次后补，另做单例红——副本内暂存新页面、`git checkout --` 回旧页面跑单例（`red-test_paused_manual_login_run_entry_unknown.log`：旧投影该入口在 unknown 态置灰，用例如期红），恢复新页面后全文件 16/16 绿。
  - green：manual_edge 35/35、ui_live 22/22、paused 16/16、explicit 16/16、joint 4/4（`green-*.log`，rc 全 0）。
- 成对更新（意图不变）：`test_manual_edge` 页夹具就绪判据换 `#loginBrowserBtn` title；`test_page_login_does_not_collect_or_create_round`、`test_facts_and_copy_switch_to_automatic_recheck_wording`、`test_exe_location_resolves_migrated_deployment_before_page_event` 与 J-A01 开始页段改挂 title/点击信息框；`test_fact_rechecks_stay_automatic_on_tab_return_and_trial_switch` 观测面换「取数计数＋title」（进页/切回/试采各恰一次）；`test_start_page_shows_why_the_login_entry_is_disabled` 改断言 title（拒绝句＋配置＋专用目录、无状态句）；页头槽位用例重写为 `test_header_login_entries_share_one_slot_across_tabs`（无轮次隐藏）；`test_continue_is_refused_while_the_manual_window_holds_the_profile` 改「不置灰＋点击弹原样原因＋spawn 零」；J-A01 过程页段同口径；`test_real_page_change_scope_cancel...` 重写为 `test_real_page_abort_cancel_is_inert_confirm_waits_then_explicit_new`（`#abortBtn`＋取消无副作用＋确认→结果页停留（不再自动跳回）→`#backBtn`→重选 D→明确开始）。新增：`test_start_page_has_no_live_browser_facts_surface`、`test_change_scope_chain_and_run_login_card_are_gone`、暂停横幅迁移句、过程版入口已占用「已有窗口」、未知态点击报因（真实 API）；`test_ui_live` 新类 `UiLiveRunPageEntryTests` 两条（继续失败信息框、过程版入口失败/已有窗口信息框）。
- 邻域回归（单文件、11 文件全绿、共 303 项）：test_manual_edge 35、test_paused_manual_login 15（邻域跑批时点；其后补的 1 条新用例另做单例红，全文件 16/16 绿见 green）、test_local_trial_gui 20、test_leftover_rounds 26、test_ui_live 22、test_identity_lifecycle 28、test_joint_acceptance 4、test_crawler_identity 11、test_explicit_pause_resume 16、test_abort_new_round 39、test_gui 87（`neighbor-*.log`）。
- 测试接缝坑（备查）：沿用票 02 两条（pywebview 桩代理 `(n in t) ? t[n] : dispatcher`；`evaluate` 表达式以函数值收尾会被当谓词，`;0` 规避）；单例红用「暂存新页面→checkout 旧页面→单例→恢复」的副本内舞蹈，前提是没有并发用例在跑页面；`playwright expect(...).to_have_class` 无自定义消息参数。
- 视觉佐证（主仓 `.scratch/tool-output/start-page-feedback/04/20261011-t04-01/`）：`_shot_t04.py`（mock API；HTML 指副本）产出 6 图——t04_1 开始页无事实卡两按钮亮（重拍票 02 对位图 1）、t04_2 开始版「已有窗口」信息框（重拍 2）、t04_3 点开始报因信息框（重拍 3）、t04_4 过程页页头过程版入口＋暂停横幅（新）、t04_5「继续」点击报因信息框（新）、t04_6 结果页停留＋「返回开始」（新）；`_t04_probe.json` 记录两形 title、开始按钮无 title、四元素缺席与页头互斥显隐；`sha256.txt` 同目录。票 02 旧图保留在其原目录作历史。
- 口径说明：①`window.alert` 全页清零（原 `resumeRun` 一处，改信息框）；②两页 UI 层事实面清零后，浏览器事实只出现在两处：点击信息框（原因类，含 retry 句）与入口按钮悬浮提示（配置/目录/状态句两形）；③开始按钮不加 title（置灰原因均有横幅/处置卡面）；④取数时点一个不少（进页/切回开始页签/试采切换/两按钮点击；夹具无轮次时过程版入口不显、不另取数——与旧卡显隐节奏一致）；⑤服务端（`gui.py`/`views.py`/`dedicated_browser.py`）一行未改；⑥全量不在本票跑（留给票 03 收尾）；⑦提交 blob 6 文件 CR=0（无行尾翻动）。

## 目录与证据处置

- 主仓：F:\AI\projects\bestseller
- 隔离副本：F:\AI\projects\bestseller\.scratch\sandboxes\start-page-feedback\04\20261011-t04-01\bestseller（独立 clone；分支 `claude/start-page-feedback` 在此检出并提交至本记录提交；未复制主仓真实配置/库/登录态）
- 来源：分支 `claude/start-page-feedback` 顶点 `59995af`（v3 工件）＋本票提交 `ad57547`（页面+测试）与记录提交；主仓分支 ref 经 fetch 回写，主仓工作树未动
- 验收证据：F:\AI\projects\bestseller\.scratch\logs\tickets\start-page-feedback\04\20261011-t04-01（baseline/red/green/neighbor-*.log＋补单单例红日志）；截图、截图脚本、_t04_probe.json 与 sha256.txt 在主仓 `.scratch\tool-output\start-page-feedback\04\20261011-t04-01`
- 永久保留：上述证据、截图、截图脚本与 probe（`.scratch` 不进 Git，跨机器需另行打包）
- 可删除候选：本副本 `.scratch\sandboxes\start-page-feedback\04\20261011-t04-01`（票 03 将按规范另建副本；删除需另行授权）
- 暂留待确认：同上副本，保留待本线收口或用户决定
- 实际清理：未执行（未获授权）

## Comments

2026-10-11：票 04 实施并验收（本轮会话，主 agent）。三块改动全部落地：过程页入口上移页头＋点击报因＋删「暂停后处理登录」整卡；删「改范围」前端整链（替代动线＝中止 →（等收尾）→ 结果页「返回开始」→ 重选 → 明确开始，Q8 语义原样）；删开始页浏览器事实卡（配置来源/专用目录/状态句进悬浮提示两形、原因类仍在点击信息框、retry 不进 title）。服务端一行未改；`window.alert` 全页清零；两页 UI 层浏览器事实面清零。03 收尾提示：集成（合 main）需用户授权后按票 03 走；全量留给 03；本票未 push。票 03 的语言同步面照 v3 spec：08 系材料「可见原因/配置可查看」改写为「点击报因＋悬停可见」；母 spec 与台账「改范围按钮已删（快捷方式；路径语义不变）」各补一句。
