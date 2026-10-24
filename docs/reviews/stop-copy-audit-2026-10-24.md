# 停止、中止、暂停文案盘点（2026-10-24）

本表保留本次修改前的盘点基线（其中人工介入超时原因提示及 Q1 失败率超限修正已先行记录），下文原文与判定不代表后续实施后的现状。用户后来批准了全部建议，相关文案已实施；现行文案见[修改前后对照](F:/AI/projects/bestseller/docs/reviews/stop-copy-proposals-2026-10-24.md)，实施范围与验收见[交付记录](F:/AI/projects/bestseller/docs/design/stop-copy-consistency-2026-10-24/delivery.md)。

范围：受 Git 跟踪的运行时 Python、三个 HTML 页面、入口与 shells；检索“停止 / 中止 / 暂停 / 终止”，并补查未包含这些词的结束原因（启动失败、预算耗尽、跨天及人工介入超时）。注释、docstring、测试断言、历史设计文档不作为产品文案；tools 的诊断和演示字符串单列。程序节奏降温的“主动停顿”不属于停止轮次；与暂停相关的指标说明命中仍保留在字面值表。动态异常用占位符展示，无法穷举外部异常内容。相同字面文案可在多处复用。

判定：“是”表示完整提示直接说明触发原因；“部分”表示只说明类别、阶段或宽泛来源；“否”表示仅有停止状态、结果或后续操作；“不适用”用于按钮、确认操作和规则说明。收尾失败原因与最初停止原因分别判断。

## 主要场景

| 场景 | 完整文案或模板 | 原因是否明确 | 核对说明 | 源码 |
| --- | --- | --- | --- | --- |
| 滑块人工介入超时 | 采集已停止，本轮未正常结束；停止原因：人工介入（滑块）等待超时；请确认中止并完成收尾后，再开始新轮。 | 是（本次补齐） | 开始页、过程页；同轮重新执行后不复用旧原因。 | [bestseller_monitor/views.py:276](F:/AI/projects/bestseller/bestseller_monitor/views.py:276) |
| 登录墙人工介入超时 | 采集已停止，本轮未正常结束；停止原因：人工介入（登录墙）等待超时；请确认中止并完成收尾后，再开始新轮。 | 是（本次补齐） | 与滑块共用原因通道。 | [bestseller_monitor/guard.py:495](F:/AI/projects/bestseller/bestseller_monitor/guard.py:495) |
| 非正常停机但无原因记录 | 采集已停止，本轮未正常结束；停止原因：未记录；请确认中止并完成收尾后，再开始新轮。 | 否；明确说明未记录 | 旧记录、直接杀进程等不能臆测为滑块超时。 | [bestseller_monitor/views.py:263](F:/AI/projects/bestseller/bestseller_monitor/views.py:263) |
| 停机后跨天 | 采集已停止且已跨天，本轮未正常结束、不能继续；停止原因：{原因或未记录}；请确认中止并完成收尾后，再开始新轮。 | 取决于记录 | 跨天事实与最初停机原因分开；跨天本身不证明停机原因。 | [bestseller_monitor/views.py:276](F:/AI/projects/bestseller/bestseller_monitor/views.py:276) |
| 用户主动暂停 | 已暂停（轮次保留为「进行中」，点「继续」接着抓取本轮剩余任务；关闭专用 Edge 窗口不会自动继续本轮）。 | 否 | 说明状态和后续动作，未直接写“用户主动暂停”。 | [bestseller_monitor/pages/ui_live.html:601](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:601) |
| 身份验证超时终态 | 身份验证等待超时，本轮自动中止；已抓取数据已保留、不可续跑。请完成安全收尾后明确开始新轮。 | 是 | 标签：身份验证超时中止。 | [bestseller_monitor/views.py:136](F:/AI/projects/bestseller/bestseller_monitor/views.py:136) |
| 即将跨天终态 | 本轮因库存数据即将跨天而中止，已抓取数据已保留；请0点后启动新的抓取轮次。 | 是 | 标签：跨天中止。 | [bestseller_monitor/views.py:143](F:/AI/projects/bestseller/bestseller_monitor/views.py:143) |
| 整轮 deny 达阈值终态 | 本轮因整轮 deny 达到阈值而意外中止，已抓取数据已保留；本轮不可续跑，请启动新的抓取轮次。 | 是 | 标签“意外中止”单独看较泛，完整正文有原因。 | [bestseller_monitor/views.py:147](F:/AI/projects/bestseller/bestseller_monitor/views.py:147) |
| 失败率超限终态 | 本轮因失败率超过阈值而中止，已抓取数据已保留；本轮不可续跑，请完成收尾后再开始新轮；未抓取店铺见下方。 | 是（本轮按 Q1 修改） | 标签：失败率超限中止；代码记 FAIL_RATE_EXCEEDED 终态，不能在 GUI 续跑原轮。 | [bestseller_monitor/views.py:151](F:/AI/projects/bestseller/bestseller_monitor/views.py:151) |
| 详情预算耗尽终态 | 本轮详情预算已用尽，已抓取数据已保留；剩余商品留待下一轮重新发现。 | 是 | 标签：预算耗尽。虽未包含检索关键词，按停止语义纳入。 | [bestseller_monitor/views.py:155](F:/AI/projects/bestseller/bestseller_monitor/views.py:155) |
| 人工放弃终态 | 本轮由人工中止（放弃），已抓取数据已保留、不再续跑；未抓取店铺见下方。 | 是 | 说明触发者及动作，未提供用户个人放弃理由（当前没有输入此理由）。 | [bestseller_monitor/views.py:159](F:/AI/projects/bestseller/bestseller_monitor/views.py:159) |
| 启动或初始化失败 | 本次新建在启动或初始化时失败，未进入采集；请核实收尾与配置后明确开始新轮。 | 部分 | 有失败阶段，结果页通用模板未呈现具体异常 run.note。 | [bestseller_monitor/views.py:139](F:/AI/projects/bestseller/bestseller_monitor/views.py:139) |
| 未知或旧终态 | 本轮非正常结束，已抓取数据已保留；未抓取店铺见下方。 | 否 | “非正常结束”是结果，不是原因。 | [bestseller_monitor/views.py:165](F:/AI/projects/bestseller/bestseller_monitor/views.py:165) |
| 分析匹配停止汇总 | 本次匹配已停止：已完成 {judged} / {todo} 对{，未判完的对记为「失败」，建议重试} | 否 | 花括号后缀仅在 retryable > 0 时出现；未直接写“用户停止”。 | [bestseller_monitor/pages/bestseller-analysis.html:975](F:/AI/projects/bestseller/bestseller_monitor/pages/bestseller-analysis.html:975) |
| 商品对停止标记 | 已停止匹配，未判断 | 否 | 停止是未判断的直接背景，没有说明谁触发了停止。 | [bestseller_monitor/matching.py:113](F:/AI/projects/bestseller/bestseller_monitor/matching.py:113) |
| SKU 任务停止原因 | 用户/运行已停止 | 部分 | 并列两个来源，未区分具体来源。 | [bestseller_monitor/sku_lifecycle.py:19](F:/AI/projects/bestseller/bestseller_monitor/sku_lifecycle.py:19) |
| CLI 人工介入暂停 | 本轮已暂停（可再次运行续跑）：{exc} | 是，取决于异常内容 | 滑块现为“人工介入（滑块）等待超时”；CLI 可重跑开放轮，GUI 仅明确暂停才有继续资格。 | [bestseller_monitor/pipeline.py:112](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:112) |
| CLI 用户暂停 | 本轮已暂停（可再次运行续跑）。 | 否 | 日志有“收到界面暂停请求”；stdout 单句没有触发者。 | [bestseller_monitor/pipeline.py:74](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:74) |
| 停止/浏览器收尾失败 | {停止失败说明或异常} | 部分 | 许多分支说明的是收尾被卡住的原因，不是最初停止采集的原因。见后端逐项表。 | [bestseller_monitor/views.py:45](F:/AI/projects/bestseller/bestseller_monitor/views.py:45) |

## 本次改动

超时异常区分“滑块 / 登录墙”；非终态的分类停止原因以 collection_interrupted 事件保存在所属轮次，GUI 开始页、逐轮待处置提示和过程页共用该原因。collection_started 标记新一次执行，防止旧原因在同轮重跑后继续显示。没有记录时显示“未记录”。不修改轮次终态、暂停资格、续跑入口或浏览器收尾规则。

停止进度框仍显示当前收尾阶段；用户明确暂停时仍显示原暂停提示。这里补充的是采集进程退出后、未正常结束轮次的原因。

领域文档已定义：[续跑](F:/AI/projects/bestseller/CONTEXT.md:47) 要求 GUI 明确暂停资格；[轮次终态](F:/AI/projects/bestseller/CONTEXT.md:55) 已包括失败率超限。下表的“暂停/中止”问题按这些现有定义判断；Q1 已确认沿用既有终态定义，不引入新行为。

## 措辞问题与决策（Q1 已修改，其余为建议）

| 问题 | 代码事实 | 建议 | 是否涉及行为变更 |
| --- | --- | --- | --- |
| 失败率超限写“暂停待处理” | FAIL_RATE_EXCEEDED 已是轮次终态；GUI 不可继续该轮；交换报告写“因失败率超限中止” | 用户已确认并落实：改为“失败率超限中止”，正文明确不可续跑、收尾后开始新轮 | 只改文案即可；若希望可以继续，则是另一项需求 |
| CLI 的“暂停可续跑”与 GUI 的“需确认中止”不相同 | CLI 重跑可打开同一开放轮；GUI 只有本界面明确暂停且完成收尾才允许继续。榜单/详情未完成的异常原文还带“后续跑”建议，本次原因栏直接引用时可能与 GUI 的中止提示并列 | 分入口描述；避免让 GUI 用户以为所有未结束轮都可点继续 | 统一文案不等于开放续跑能力 |
| 用户暂停、匹配停止、收尾进度未写触发者 | 操作上下文可知，但脱离上下文后看不出是谁触发 | 可补“用户主动暂停 / 用户停止匹配”；收尾同时保留原请求类型 | 需要另行决定统一覆盖哪些页面 |
| 启动失败和未知终态较泛 | 通用模板没有展示具体异常；旧数据可能无因可查 | 有可靠原因则呈现，没有则明确未记录 | 不应补猜测的原因 |
| 跨天 CLI 文案“请0点后继续抓取” | 本轮已终态，下一次是新轮 | 改成“0点后开始新轮”更清晰 | 文案调整 |
| 确认框用“终止”，页面按钮用“中止” | 两者指同一人工放弃动作 | 统一用“中止” | 文案调整 |

## 前端与确认框逐项表

| 位置/场景 | 文案 | 原因判定 | 说明 | 源码 |
| --- | --- | --- | --- | --- |
| 采集按钮 | 暂停本轮；中止本轮（放弃）；中止此轮（放弃）；中止正在跑的这一轮（放弃）；中止正在跑的这一轮 #{round_id}（放弃） | 不适用 | 操作入口 | [bestseller_monitor/pages/ui_live.html:101](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:101) |
| 遗留轮说明 | 每个轮次单独确认「中止此轮」；确认前不写终态，一次授权只处置它指向的那一轮。 | 不适用 | 确认规则 | [bestseller_monitor/pages/ui_live.html:106](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:106) |
| 暂停静态占位 | 已暂停（轮次保留为「进行中」，点「继续」接着抓取本轮剩余任务）。 | 否 | 动态版本见主要场景表 | [bestseller_monitor/pages/ui_live.html:147](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:147) |
| 停止静态占位（两处） | 正在停止采集进程… | 否 | 过程页与结果页均有 | [bestseller_monitor/pages/ui_live.html:149](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:149) |
| 暂停/中止释义 | 暂停＝保留本轮可续跑；中止＝放弃本轮（数据保留，不再续跑，下次开新轮）。 | 不适用 | 规则说明 | [bestseller_monitor/pages/ui_live.html:159](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:159) |
| 停止阶段：关闭浏览器 | 正在收尾：采集进程在关掉自己启动的浏览器… | 否 | 有当前阶段，没有原停止原因 | [bestseller_monitor/pages/ui_live.html:355](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:355) |
| 停止阶段：核验 | 正在核验停止目标：暂不执行破坏性操作… | 否 | 有当前阶段，没有原停止原因 | [bestseller_monitor/pages/ui_live.html:356](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:356) |
| 停止阶段：重试 | 正在重试收尾：仍只处理原停止目标… | 否 | 有当前阶段，没有原停止原因 | [bestseller_monitor/pages/ui_live.html:357](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:357) |
| 停止阶段：等待 | 正在停止采集进程…（{最多 N 秒，或空}超时会强制结束） | 否 | 说明等待与超时策略 | [bestseller_monitor/pages/ui_live.html:359](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:359) |
| 跨天前端兜底 | 本轮已跨天，不能继续；请中止本轮后开始新轮。 | 部分 | 说明跨天禁续跑；未提供最初停机原因，正常使用后端完整 note | [bestseller_monitor/pages/ui_live.html:598](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:598) |
| 未结束前端兜底 | 采集已停止，本轮未正常结束；停止原因：未记录；请确认中止并完成收尾后，再开始新轮。 | 否；明确未记录 | 本次补齐；正常使用后端完整 note | [bestseller_monitor/pages/ui_live.html:599](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:599) |
| 续跑启动失败 | 继续启动未成功：{resume_error}。原轮暂停资格保留，请明确点击继续重试。 | 有本次失败原因；无原暂停原因 | 动态异常可能仍较泛 | [bestseller_monitor/pages/ui_live.html:602](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:602) |
| 暂停后的继续门禁 | 已暂停（轮次保留为「进行中」），但还不能继续：{continue_gate}；关闭专用 Edge 窗口不会自动继续本轮。 | 有禁续跑原因；无原暂停原因 | 动态门禁原因 | [bestseller_monitor/pages/ui_live.html:604](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:604) |
| 打开登录浏览器门禁 | 先暂停本轮并等停止收尾完成，才能打开专用 Edge 处理登录。 | 不适用 | 操作前置条件 | [bestseller_monitor/pages/ui_live.html:616](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:616) |
| 采集中止确认标题 | 确认终止本轮抓取？ | 不适用 | “终止”与按钮“中止”不统一；静态与动态两处 | [bestseller_monitor/pages/ui_live.html:674](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:674) |
| 采集中止确认正文 | 本轮将结束且不能继续；已抓取数据保留。收尾完成后，需明确点击开始才能新建。 | 不适用 | 事前确认后果，非事后原因提示 | [bestseller_monitor/pages/ui_live.html:675](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:675) |
| 遗留轮中止确认 | 确认中止此轮？ / 轮次 #{id}（{run_date}，{shop_keys}）{，已跨天或空}将结束且不能继续；已抓取数据保留。等待收尾完成后，需明确点击开始才能新建。 | 不适用 | 确认对象与后果 | [bestseller_monitor/pages/ui_live.html:715](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:715) |
| 采集关窗确认 | 采集正在进行中。<br><br>关闭将中止本轮采集，效果与‘中止本轮（放弃）’相同。已提交的采集数据保留，本轮不可续跑。<br><br>按‘确定’＝中止并关闭；按‘取消’＝保留窗口。 | 不适用 | 确认来源：用户关闭窗口 | [bestseller_monitor/crawler_close.py:31](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:31) |
| 暂停中关窗标题 | 本轮正在暂停，采集进程正在停止 / 本轮正在完成暂停收尾 / 本轮已暂停 | 否 | 当前状态，不重复触发者 | [bestseller_monitor/crawler_close.py:165](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:165) |
| 采集关窗处理中标题 | {title}（正在中止采集，请稍候…） | 否 | 状态提示 | [bestseller_monitor/crawler_close.py:179](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:179) |
| 主窗口中止确认标题 | {WINDOW_TITLE}（确认中止并关闭） | 不适用 | 确认动作 | [gui.py:1350](F:/AI/projects/bestseller/gui.py:1350) |
| 匹配按钮与提示 | 停止匹配 / 停下后已判断的会留下，稍后可从本页继续 | 不适用 | 操作与后果 | [bestseller_monitor/pages/bestseller-analysis.html:85](F:/AI/projects/bestseller/bestseller_monitor/pages/bestseller-analysis.html:85) |
| 匹配停止等待 | 正在停止匹配… / 等一个在途判断结束（最多 {stop_timeout_sec 或 30} 秒）；已判的会留下 | 否 | 有等待对象，没有原停止原因 | [bestseller_monitor/pages/bestseller-analysis.html:965](F:/AI/projects/bestseller/bestseller_monitor/pages/bestseller-analysis.html:965) |
| 匹配停止后说明（SKU） | 停下后自动进入本页；未完成项保留原因，稍后可以继续判断 | 不适用 | 停止后果与重试说明 | [bestseller_monitor/pages/bestseller-analysis.html:929](F:/AI/projects/bestseller/bestseller_monitor/pages/bestseller-analysis.html:929) |
| 匹配停止后说明（商品） | 停下后自动进入本页；没判断的对记为「失败」，点「模型匹配同款」可以接着判 | 不适用 | 停止后果；失败标签可能被误解为模型失败 | [bestseller_monitor/pages/bestseller-analysis.html:930](F:/AI/projects/bestseller/bestseller_monitor/pages/bestseller-analysis.html:930) |
| 匹配停止后共用说明 | 点「停止匹配」与点关窗确认里的「关闭并停止」进的都是这一态；收尾完成前窗口不消失 | 不适用 | 说明两种用户操作共用停止流程 | [bestseller_monitor/pages/bestseller-analysis.html:932](F:/AI/projects/bestseller/bestseller_monitor/pages/bestseller-analysis.html:932) |
| 匹配停止请求失败 | 停止请求没能送达：{reason.message} | 有本次请求失败原因 | 异常详情动态生成，非原停止原因 | [bestseller_monitor/pages/bestseller-analysis.html:1054](F:/AI/projects/bestseller/bestseller_monitor/pages/bestseller-analysis.html:1054) |
| 匹配关窗确认正文 | 关闭会停在这里：已判断的会保存，重新开始会接着算，不会重复花钱；但这一趟的分析结果会没。<br><br>按「确定」＝关闭并停止匹配；按「取消」＝继续匹配。 | 不适用 | 确认后果 | [analyze.py:132](F:/AI/projects/bestseller/analyze.py:132) |

## Python 命中字面值逐项表

本节保留最初扫描原文，用于溯源；其中失败率超限的旧标签、正文已按 Q1 替换，当前文案见“主要场景”和修改前后对照。

下表列出 120 处运行时 Python 字符串命中（含日志、拼接片段、短标签和重复引用），不是 120 条独立 GUI 提示。f-string 保留源码占位形式；完整句子优先参考上面的场景表。此表用于检查检索覆盖，不能把日志存在原因视为页面已经展示原因。

| 用途 | 字面值/模板 | 原因判定 | 说明 | 源码 |
| --- | --- | --- | --- | --- |
| 分析关窗确认 | <br><br>关闭会停在这里：已判断的会保存，重新开始会接着算，不会重复花钱；但这一趟的分析结果会没。<br><br>按「确定」＝关闭并停止匹配；按「取消」＝继续匹配。 | 不适用 | 用户确认关闭并停止；完整见前端表 | [analyze.py:132](F:/AI/projects/bestseller/analyze.py:132) |
| 浏览器日志 | 浏览器 PID %s 终止后仍未确认退出。 | 否 | 只说明终止后尚未退出，未给出未退出的底层原因 | [bestseller_monitor/browser_proc.py:247](F:/AI/projects/bestseller/bestseller_monitor/browser_proc.py:247) |
| 关窗确认 | 采集正在进行中。<br><br>关闭将中止本轮采集，效果与‘中止本轮（放弃）’相同。已提交的采集数据保留，本轮不可续跑。<br><br>按‘确定’＝中止并关闭；按‘取消’＝保留窗口。 | 不适用 | 用户关闭窗口的事前确认 | [bestseller_monitor/crawler_close.py:31](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:31) |
| 关窗诊断日志 | 停止监督状态码：%s | 部分 | 记录失败阶段或状态；底层详情可能另有异常栈 | [bestseller_monitor/crawler_close.py:82](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:82) |
| 关窗错误提示 | f"中止或收尾暂未完成：{exc}" | 有收尾失败原因 | 动态异常详情；不是最初停止原因 | [bestseller_monitor/crawler_close.py:98](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:98) |
| 关窗错误提示 | f"中止或收尾失败，请重试：{exc}" | 有收尾失败原因 | 动态异常详情；不是最初停止原因 | [bestseller_monitor/crawler_close.py:101](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:101) |
| 关窗诊断日志 | 暂停收尾暂不可核验；继续原暂停 | 部分 | 记录失败阶段或状态；底层详情可能另有异常栈 | [bestseller_monitor/crawler_close.py:128](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:128) |
| 关窗诊断日志 | 暂停独立收尾驱动失败；保留窗口与暂停状态 | 部分 | 记录失败阶段或状态；底层详情可能另有异常栈 | [bestseller_monitor/crawler_close.py:131](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:131) |
| 关窗诊断日志 | 无法启动暂停独立收尾；保留窗口与暂停状态 | 部分 | 记录失败阶段或状态；底层详情可能另有异常栈 | [bestseller_monitor/crawler_close.py:140](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:140) |
| 关窗状态标题 | 本轮正在暂停，采集进程正在停止 | 否 | 只说明中止/暂停状态 | [bestseller_monitor/crawler_close.py:165](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:165) |
| 关窗状态标题 | 本轮正在完成暂停收尾 | 否 | 只说明中止/暂停状态 | [bestseller_monitor/crawler_close.py:166](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:166) |
| 关窗状态标题 | 本轮已暂停 | 否 | 只说明中止/暂停状态 | [bestseller_monitor/crawler_close.py:167](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:167) |
| 关窗状态标题 | f"{title}（正在中止采集，请稍候…）" | 否 | 只说明中止/暂停状态 | [bestseller_monitor/crawler_close.py:179](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:179) |
| 关窗错误提示 | f"无法完成关窗确认或中止，请重试：{exc}" | 有收尾失败原因 | 动态异常详情；不是最初停止原因 | [bestseller_monitor/crawler_close.py:192](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:192) |
| 旧库状态兼容常量 | 意外中止 | 不适用 | 只用于旧库迁移，不是当前完整展示文案 | [bestseller_monitor/db.py:441](F:/AI/projects/bestseller/bestseller_monitor/db.py:441) |
| 旧库状态兼容常量 | 意外中止 | 不适用 | 只用于旧库迁移，不是当前完整展示文案 | [bestseller_monitor/db.py:450](F:/AI/projects/bestseller/bestseller_monitor/db.py:450) |
| 指标说明 | 有效采集执行段累计；不含开轮前准备和暂停间隔。 | 不适用 | 解释耗时统计不包含暂停 | [bestseller_monitor/elapsed.py:49](F:/AI/projects/bestseller/bestseller_monitor/elapsed.py:49) |
| 交换完整性报告 | 当天采集因身份验证等待超时自动中止，已保存数据保留 | 是 | 有对应终态原因 | [bestseller_monitor/exchange.py:110](F:/AI/projects/bestseller/bestseller_monitor/exchange.py:110) |
| 交换完整性报告 | 当天采集在详情预算耗尽后中止 | 是 | 有对应终态原因 | [bestseller_monitor/exchange.py:112](F:/AI/projects/bestseller/bestseller_monitor/exchange.py:112) |
| 交换完整性报告 | 当天采集因失败率超限中止 | 是 | 有对应终态原因 | [bestseller_monitor/exchange.py:113](F:/AI/projects/bestseller/bestseller_monitor/exchange.py:113) |
| 交换完整性报告 | 当天采集撞上当日截止线中止 | 是 | 有对应终态原因 | [bestseller_monitor/exchange.py:114](F:/AI/projects/bestseller/bestseller_monitor/exchange.py:114) |
| 交换完整性报告 | 当天采集因被拒次数超限中止 | 是 | 有对应终态原因 | [bestseller_monitor/exchange.py:115](F:/AI/projects/bestseller/bestseller_monitor/exchange.py:115) |
| 交换完整性报告 | 当天采集被中止（放弃） | 部分 | 只写放弃，未明确谁触发 | [bestseller_monitor/exchange.py:116](F:/AI/projects/bestseller/bestseller_monitor/exchange.py:116) |
| 交换同步操作名 | 终止变基 | 不适用 | 指 Git 变基 abort，非采集停止 | [bestseller_monitor/git_channel.py:357](F:/AI/projects/bestseller/bestseller_monitor/git_channel.py:357) |
| 交换同步操作名 | 终止变基 | 不适用 | 指 Git 变基 abort，非采集停止 | [bestseller_monitor/git_channel.py:359](F:/AI/projects/bestseller/bestseller_monitor/git_channel.py:359) |
| 人工介入日志/异常 | 身份验证等待超时，本轮自动中止，已保存数据保留 | 是 | 说明身份验证等待超时 | [bestseller_monitor/guard.py:494](F:/AI/projects/bestseller/bestseller_monitor/guard.py:494) |
| 人工介入日志/异常 | 人工介入已解决，停止响铃，继续。 | 是 | 说明停止响铃是因为人工介入已解决 | [bestseller_monitor/guard.py:502](F:/AI/projects/bestseller/bestseller_monitor/guard.py:502) |
| 商品对匹配标记 | 已停止匹配，未判断 | 否 | 未判断背景是停止，未说明原停止来源 | [bestseller_monitor/matching.py:113](F:/AI/projects/bestseller/bestseller_monitor/matching.py:113) |
| 暂停日志/店铺原因 | 用户在界面暂停，本店未完成 | 是 | 明确用户在界面暂停 | [bestseller_monitor/pipeline.py:38](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:38) |
| 暂停日志/店铺原因 | 收到界面暂停请求：本轮停在检查点，已抓数据保留、可续跑。 | 是 | 明确用户在界面暂停 | [bestseller_monitor/pipeline.py:73](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:73) |
| CLI stdout | 本轮已暂停（可再次运行续跑）。 | 否 | 未直接写用户触发；对应日志有原因 | [bestseller_monitor/pipeline.py:74](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:74) |
| 日志/stdout/持久化原因 | 本轮因整轮 deny 超过阈值而意外中止：{exc}；已抓取数据已保留，不可续跑 | 是 | 具体阈值、跨天或验证超时 | [bestseller_monitor/pipeline.py:78](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:78) |
| 日志/stdout/持久化原因 | 整轮 deny 超过阈值，中止本轮：{exc} | 是 | 具体阈值、跨天或验证超时 | [bestseller_monitor/pipeline.py:80](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:80) |
| CLI 日志/stdout | 本轮意外中止：{note} | 由占位原因决定 | 分类异常/轮次 note 被插入文本；GUI 续跑策略另见主要场景 | [bestseller_monitor/pipeline.py:81](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:81) |
| 日志/stdout/持久化原因 | 身份验证等待超时，本轮已自动中止，已保存数据保留，不可续跑 | 是 | 具体阈值、跨天或验证超时 | [bestseller_monitor/pipeline.py:103](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:103) |
| CLI 日志/stdout | 本轮暂停：{exc} | 由占位原因决定 | 分类异常/轮次 note 被插入文本；GUI 续跑策略另见主要场景 | [bestseller_monitor/pipeline.py:111](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:111) |
| CLI 日志/stdout | 本轮已暂停（可再次运行续跑）：{exc} | 由占位原因决定 | 分类异常/轮次 note 被插入文本；GUI 续跑策略另见主要场景 | [bestseller_monitor/pipeline.py:112](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:112) |
| 内部保护异常 | f"没有登记这个停止异常：{type(exc).__name__}" | 不适用 | 缺少停止分类的开发错误 | [bestseller_monitor/pipeline.py:136](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:136) |
| 并发拒绝提示 | 已有采集进程在运行：同一时刻只能跑一轮，等它结束或在界面里中止它。 | 有拒绝原因 | 已有任务占用 | [bestseller_monitor/pipeline.py:261](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:261) |
| 日志/stdout/持久化原因 | f"上一轮（{stale}）已跨天，按跨天中止收尾；本轮新建 #{round_id}" | 是 | 具体阈值、跨天或验证超时 | [bestseller_monitor/pipeline.py:311](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:311) |
| 终态保留日志/stdout | 轮次 #%s 已经是 %s，本次的停止原因（%s）不改写它的终态。 | 部分 | 给出已有终态或终态码，未解释原始触发细节 | [bestseller_monitor/pipeline.py:380](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:380) |
| 终态保留日志/stdout | f"\n>>> 轮次 #{settled.id} 已经是 {settled.reason.value}，"<br>          "本次停止不改写它的终态。\n" | 部分 | 给出已有终态或终态码，未解释原始触发细节 | [bestseller_monitor/pipeline.py:382](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:382) |
| 收尾日志 | 身份验证已超时，时长收尾失败；仍按原超时停止 | 有停止原因；收尾失败较泛 | 明确身份超时；失败底层异常在栈中 | [bestseller_monitor/pipeline.py:461](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:461) |
| 开轮拒绝提示 | f"轮次 #{current.id}（{current.run_date}）尚未结束；开始页只新建轮次。"<br>            "请先明确中止旧轮并等待收尾完成，再开始新轮。" | 有拒绝原因 | 未结束轮/店铺范围变化；非原停止原因 | [bestseller_monitor/rounds.py:330](F:/AI/projects/bestseller/bestseller_monitor/rounds.py:330) |
| 开轮拒绝提示 | f"轮次 #{current.id}（{current.run_date}）已在进行中，店铺范围是"<br>        f"{_keys_text(current.shop_keys)}；本次是{_keys_text(request.shop_keys)}。"<br>        "要换店铺范围，请先中止本轮。" | 有拒绝原因 | 未结束轮/店铺范围变化；非原停止原因 | [bestseller_monitor/rounds.py:388](F:/AI/projects/bestseller/bestseller_monitor/rounds.py:388) |
| SKU 任务原因 | 用户/运行已停止 | 部分 | “用户/运行”未区分来源 | [bestseller_monitor/sku_lifecycle.py:19](F:/AI/projects/bestseller/bestseller_monitor/sku_lifecycle.py:19) |
| 暂停日志/异常 | 已认领界面暂停请求：回执已写，界面据此放宽收尾窗口。 | 是 | 明确来自界面暂停请求 | [bestseller_monitor/stop_request.py:75](F:/AI/projects/bestseller/bestseller_monitor/stop_request.py:75) |
| 暂停日志/异常 | 收到界面暂停请求 | 是 | 明确来自界面暂停请求 | [bestseller_monitor/stop_request.py:76](F:/AI/projects/bestseller/bestseller_monitor/stop_request.py:76) |
| 停止监督日志 | 采集进程已回执停止请求，再等 %.0f 秒收尾。 | 否 | 仅说明回执与额外等待 | [bestseller_monitor/stop_request.py:528](F:/AI/projects/bestseller/bestseller_monitor/stop_request.py:528) |
| 停止监督日志 | 停止目标暂不可核验（%s）：%s | 部分 | 核验或清理失败阶段；%s 由运行状态/异常补充 | [bestseller_monitor/stop_request.py:536](F:/AI/projects/bestseller/bestseller_monitor/stop_request.py:536) |
| 停止监督日志 | 停止目标清理暂不可用：%s | 部分 | 核验或清理失败阶段；%s 由运行状态/异常补充 | [bestseller_monitor/stop_request.py:598](F:/AI/projects/bestseller/bestseller_monitor/stop_request.py:598) |
| 停止失败提示 | 无法强制停止采集，请检查进程权限 | 部分 | 说明权限建议/当前状态，未给出底层失败详情，也不含最初停机原因 | [bestseller_monitor/views.py:48](F:/AI/projects/bestseller/bestseller_monitor/views.py:48) |
| 停止失败提示 | 采集已停止，浏览器收尾暂未完成 | 否 | 说明权限建议/当前状态，未给出底层失败详情，也不含最初停机原因 | [bestseller_monitor/views.py:54](F:/AI/projects/bestseller/bestseller_monitor/views.py:54) |
| 停止失败提示 | 原采集进程尚未停止 | 否 | 说明权限建议/当前状态，未给出底层失败详情，也不含最初停机原因 | [bestseller_monitor/views.py:55](F:/AI/projects/bestseller/bestseller_monitor/views.py:55) |
| 停止失败提示 | 停止状态暂不可确认 | 否 | 说明权限建议/当前状态，未给出底层失败详情，也不含最初停机原因 | [bestseller_monitor/views.py:56](F:/AI/projects/bestseller/bestseller_monitor/views.py:56) |
| GUI 终态标签/正文 | 身份验证超时中止 | 是 | 完整场景见主要场景表 | [bestseller_monitor/views.py:136](F:/AI/projects/bestseller/bestseller_monitor/views.py:136) |
| GUI 终态标签/正文 | 身份验证等待超时，本轮自动中止；已抓取数据已保留、不可续跑。请完成安全收尾后明确开始新轮。 | 是 | 完整场景见主要场景表 | [bestseller_monitor/views.py:136](F:/AI/projects/bestseller/bestseller_monitor/views.py:136) |
| GUI 终态标签/正文 | 跨天中止 | 是 | 完整场景见主要场景表 | [bestseller_monitor/views.py:143](F:/AI/projects/bestseller/bestseller_monitor/views.py:143) |
| GUI 终态标签/正文 | 本轮因库存数据即将跨天而中止，已抓取数据已保留；请0点后启动新的抓取轮次。 | 是 | 完整场景见主要场景表 | [bestseller_monitor/views.py:144](F:/AI/projects/bestseller/bestseller_monitor/views.py:144) |
| GUI 终态短标签 | 意外中止 | 否 | 短标签较泛，完整正文另见主要场景 | [bestseller_monitor/views.py:147](F:/AI/projects/bestseller/bestseller_monitor/views.py:147) |
| GUI 终态标签/正文 | 本轮因整轮 deny 达到阈值而意外中止，已抓取数据已保留；本轮不可续跑，请启动新的抓取轮次。 | 是 | 完整场景见主要场景表 | [bestseller_monitor/views.py:148](F:/AI/projects/bestseller/bestseller_monitor/views.py:148) |
| GUI 终态短标签 | 暂停待处理 | 否 | 完整正文有失败率原因，但“暂停”与终态冲突 | [bestseller_monitor/views.py:151](F:/AI/projects/bestseller/bestseller_monitor/views.py:151) |
| GUI 终态标签/正文 | 本轮因失败率超过阈值而暂停，需人工决策；未抓取店铺见下方。 | 是 | 有失败率原因；“暂停”与终态语义冲突 | [bestseller_monitor/views.py:152](F:/AI/projects/bestseller/bestseller_monitor/views.py:152) |
| GUI 终态标签/正文 | 人工中止 | 是 | 完整场景见主要场景表 | [bestseller_monitor/views.py:159](F:/AI/projects/bestseller/bestseller_monitor/views.py:159) |
| GUI 终态标签/正文 | 本轮由人工中止（放弃），已抓取数据已保留、不再续跑；未抓取店铺见下方。 | 是 | 完整场景见主要场景表 | [bestseller_monitor/views.py:160](F:/AI/projects/bestseller/bestseller_monitor/views.py:160) |
| GUI 终态短标签 | 意外中止 | 否 | 短标签较泛，完整正文另见主要场景 | [bestseller_monitor/views.py:170](F:/AI/projects/bestseller/bestseller_monitor/views.py:170) |
| GUI 完整文案片段 | 采集已停止且已跨天，本轮未正常结束、不能继续 | 单独无原因；拼接后有原因栏 | 完整展示见主要场景，避免将片段误当完整提示 | [bestseller_monitor/views.py:277](F:/AI/projects/bestseller/bestseller_monitor/views.py:277) |
| GUI 完整文案片段 | 采集已停止，本轮未正常结束 | 单独无原因；拼接后有原因栏 | 完整展示见主要场景，避免将片段误当完整提示 | [bestseller_monitor/views.py:278](F:/AI/projects/bestseller/bestseller_monitor/views.py:278) |
| GUI 完整文案片段 | f"{head}；停止原因：{reason}；"<br>            "请确认中止并完成收尾后，再开始新轮。" | 由原因记录决定 | 与标题、逐轮原因拼接；无记录明确显示“未记录” | [bestseller_monitor/views.py:279](F:/AI/projects/bestseller/bestseller_monitor/views.py:279) |
| GUI 明确暂停提示 | ：已暂停，可到过程页继续；如需换范围或新建，请先确认中止。 | 否 | 未直接写用户主动暂停 | [bestseller_monitor/views.py:288](F:/AI/projects/bestseller/bestseller_monitor/views.py:288) |
| GUI 明确暂停提示 | f"轮次 #{row.id}（{row.run_date}）已暂停：请到过程页继续；"<br>                "如需新建，请先确认中止本轮。" | 否 | 未直接写用户主动暂停 | [bestseller_monitor/views.py:313](F:/AI/projects/bestseller/bestseller_monitor/views.py:313) |
| GUI 完整文案片段 | 采集已停止，本轮未正常结束 | 单独无原因；拼接后有原因栏 | 完整展示见主要场景，避免将片段误当完整提示 | [bestseller_monitor/views.py:318](F:/AI/projects/bestseller/bestseller_monitor/views.py:318) |
| GUI 明确暂停提示 | f"轮次 #{row.id} 已暂停" | 否 | 未直接写用户主动暂停 | [bestseller_monitor/views.py:323](F:/AI/projects/bestseller/bestseller_monitor/views.py:323) |
| GUI 完整文案片段 | 停止原因： | 由原因记录决定 | 与标题、逐轮原因拼接；无记录明确显示“未记录” | [bestseller_monitor/views.py:328](F:/AI/projects/bestseller/bestseller_monitor/views.py:328) |
| GUI 待处置提示片段 | 。请在下方逐个确认中止并等待收尾完成，再开始新轮。 | 不适用 | 要求逐轮确认的后续动作，前文有逐轮原因 | [bestseller_monitor/views.py:330](F:/AI/projects/bestseller/bestseller_monitor/views.py:330) |
| GUI 入口限制提示 | f"采集进程正在跑（{who}）：同一时刻只能有一个，现在点开始会被拒绝；"<br>            "要停它就用下面的「中止」按钮。" | 有拒绝原因 | 说明采集在跑/仍在核验收尾；不是原停止原因 | [bestseller_monitor/views.py:374](F:/AI/projects/bestseller/bestseller_monitor/views.py:374) |
| GUI 入口限制提示 | 上一次停止仍在核验或收尾，请等待收尾完成再明确开始新轮。 | 有拒绝原因 | 说明采集在跑/仍在核验收尾；不是原停止原因 | [bestseller_monitor/views.py:429](F:/AI/projects/bestseller/bestseller_monitor/views.py:429) |
| 身份验证等待说明 | f"需要人工完成身份验证；本次最长等待 {minutes:g} 分钟，超时自动中止。" | 不适用 | 预告超时自动中止，不是已停止结果 | [bestseller_monitor/views.py:575](F:/AI/projects/bestseller/bestseller_monitor/views.py:575) |
| GUI API 提示 | 已有抓取任务在运行，请先暂停或中止。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:90](F:/AI/projects/bestseller/gui.py:90) |
| GUI API 提示 | 正在关闭程序，不能开始、续跑或暂停。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:159](F:/AI/projects/bestseller/gui.py:159) |
| GUI API 提示 | 采集或停止仍在进行，不能切换模式。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:235](F:/AI/projects/bestseller/gui.py:235) |
| GUI API 提示 | 停止或收尾仍在进行，请稍后刷新重试。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:475](F:/AI/projects/bestseller/gui.py:475) |
| GUI API 提示 | 上一轮的停止/浏览器收尾仍未完成；请先在开始页完成收尾核验，再打开手动浏览器。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:481](F:/AI/projects/bestseller/gui.py:481) |
| GUI API 提示 | 上一次停止仍在核验或收尾，请稍后重试。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:627](F:/AI/projects/bestseller/gui.py:627) |
| GUI API 提示 | 上一次停止仍在核验或收尾，请稍后重试。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:668](F:/AI/projects/bestseller/gui.py:668) |
| GUI API 提示 | f"轮次 #{stale.id}（{stale.run_date}）已经跨天，不能再续跑；"<br>                            "请先明确中止旧轮并等待收尾，再开始新轮。" | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:677](F:/AI/projects/bestseller/gui.py:677) |
| GUI API 提示 | 没有本界面明确暂停并完成收尾的轮次可继续；数据库尚未结束不等于已暂停，请先明确中止旧轮。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:682](F:/AI/projects/bestseller/gui.py:682) |
| GUI 动作日志 | 暂停：身份尚未登记，结束入口时冻结的子进程。 | 是 | 记录界面暂停或人工放弃；不额外收集用户个人理由 | [gui.py:749](F:/AI/projects/bestseller/gui.py:749) |
| GUI API 提示 | 暂时无法确认停止目标，请稍后重试。 | 部分 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:754](F:/AI/projects/bestseller/gui.py:754) |
| GUI API 提示 | 暂时无法确认停止目标，请稍后重试。 | 部分 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:766](F:/AI/projects/bestseller/gui.py:766) |
| GUI 动作日志 | 暂停：已写下停止请求（目标 PID %s），等它自己停下。 | 是 | 记录界面暂停或人工放弃；不额外收集用户个人理由 | [gui.py:770](F:/AI/projects/bestseller/gui.py:770) |
| GUI API 提示 | 停止或收尾仍在进行，请稍后重试。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:806](F:/AI/projects/bestseller/gui.py:806) |
| GUI API 提示 | f"轮次 #{round_id} 已经结束，无需中止。" | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:816](F:/AI/projects/bestseller/gui.py:816) |
| GUI API 提示 | 停止或收尾仍在进行，请稍后重试。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:838](F:/AI/projects/bestseller/gui.py:838) |
| GUI API 提示 | 采集目标已变化，请重新确认中止。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:847](F:/AI/projects/bestseller/gui.py:847) |
| GUI API 提示 | 轮次已不存在，请重新确认中止。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:851](F:/AI/projects/bestseller/gui.py:851) |
| 持久化轮次原因 | GUI 人工中止（放弃） | 是 | 明确 GUI 人工放弃/关窗 | [gui.py:854](F:/AI/projects/bestseller/gui.py:854) |
| GUI 收尾错误提示 | f"中止或收尾尚未完成：{exc}" | 有收尾失败原因 | 动态异常详情；非最初停止原因 | [gui.py:856](F:/AI/projects/bestseller/gui.py:856) |
| GUI API 提示 | f"轮次 #{settled.id} 已按其它方式结束，保留原终态，"<br>                             "未改为人工中止。" | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:864](F:/AI/projects/bestseller/gui.py:864) |
| GUI 诊断日志 | 遗留轮中止后身份收尾暂不可核验：%s | 部分 | 失败阶段与动态详情；非最初停止原因 | [gui.py:872](F:/AI/projects/bestseller/gui.py:872) |
| GUI API 提示 | 停止或收尾仍在进行，请稍后重试。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:903](F:/AI/projects/bestseller/gui.py:903) |
| GUI API 提示 | 停止或收尾仍在进行，请稍后重试。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:924](F:/AI/projects/bestseller/gui.py:924) |
| GUI API 提示 | 中止确认已失效，请重新确认。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:955](F:/AI/projects/bestseller/gui.py:955) |
| GUI API 提示 | 轮次或采集目标已变化，请重新确认中止。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:963](F:/AI/projects/bestseller/gui.py:963) |
| GUI API 提示 | 采集目标已变化，请重新确认中止。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:976](F:/AI/projects/bestseller/gui.py:976) |
| GUI API 提示 | 采集目标已变化，请重新确认中止。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:1007](F:/AI/projects/bestseller/gui.py:1007) |
| GUI API 提示 | 停止目标已变化，请重新确认。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:1015](F:/AI/projects/bestseller/gui.py:1015) |
| 持久化轮次原因 | GUI 人工中止（放弃） | 是 | 明确 GUI 人工放弃/关窗 | [gui.py:1018](F:/AI/projects/bestseller/gui.py:1018) |
| GUI API 提示 | 暂时无法确认停止目标，请稍后重试。 | 部分 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:1036](F:/AI/projects/bestseller/gui.py:1036) |
| GUI API 提示 | 轮次已变化，请重新确认中止。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:1040](F:/AI/projects/bestseller/gui.py:1040) |
| 持久化轮次原因 | GUI 人工中止（放弃） | 是 | 明确 GUI 人工放弃/关窗 | [gui.py:1043](F:/AI/projects/bestseller/gui.py:1043) |
| GUI API 提示 | 暂时无法确认停止目标，请稍后重试。 | 部分 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:1050](F:/AI/projects/bestseller/gui.py:1050) |
| GUI API 提示 | 暂时无法确认停止目标，请稍后重试。 | 部分 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:1057](F:/AI/projects/bestseller/gui.py:1057) |
| GUI 动作日志 | 中止：轮次 #%s 已收尾为人工放弃，等采集进程自己停下。 | 部分 | 记录界面暂停或人工放弃；不额外收集用户个人理由 | [gui.py:1059](F:/AI/projects/bestseller/gui.py:1059) |
| GUI 收尾错误提示 | f"中止或收尾尚未完成：{exc}" | 有收尾失败原因 | 动态异常详情；非最初停止原因 | [gui.py:1062](F:/AI/projects/bestseller/gui.py:1062) |
| GUI 诊断日志 | 人工放弃已提交，原暂停升级待重试：%s | 部分 | 失败阶段与动态详情；非最初停止原因 | [gui.py:1085](F:/AI/projects/bestseller/gui.py:1085) |
| GUI API 提示 | 暂停轮次或采集目标已变化，请重新确认关闭。 | 有拒绝/状态说明 | 说明当前动作为何被拒绝或无需执行；不等于说明原停机原因 | [gui.py:1133](F:/AI/projects/bestseller/gui.py:1133) |
| 持久化轮次原因 | GUI 关窗人工中止（放弃） | 是 | 明确 GUI 人工放弃/关窗 | [gui.py:1152](F:/AI/projects/bestseller/gui.py:1152) |
| GUI 诊断日志 | 人工放弃已核实，停止请求清理待重试：%s | 部分 | 失败阶段与动态详情；非最初停止原因 | [gui.py:1165](F:/AI/projects/bestseller/gui.py:1165) |
| 窗口标题 | f"{WINDOW_TITLE}（确认中止并关闭）" | 不适用 | 确认动作 | [gui.py:1350](F:/AI/projects/bestseller/gui.py:1350) |

## 未含检索关键词但属于停止原因或相关失败的文案

| 场景 | 文案/模板 | 原因判定 | 说明 | 源码 |
| --- | --- | --- | --- | --- |
| 人工介入超时异常 | 人工介入（{滑块/登录墙/验证类型}）等待超时 | 是 | 异常内容已供 GUI 引用 | [bestseller_monitor/guard.py:496](F:/AI/projects/bestseller/bestseller_monitor/guard.py:496) |
| 榜单阶段未完成 | 榜单阶段未完成：{shop_keys}；请检查存档页面后续跑 | 是 | 通过 RoundPauseRequired 落入 CLI 暂停说明及本次 GUI 原因 | [bestseller_monitor/pipeline.py:422](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:422) |
| 详情阶段未完成 | 详情阶段未完成：{shop_keys}；请继续补采后续跑 | 是 | 同上；GUI 当前仍须中止旧轮，不开放普通继续 | [bestseller_monitor/pipeline.py:426](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:426) |
| 整轮 deny 阈值异常 | 整轮 {deny_window_minutes} 分钟内 deny≥{deny_round_limit} | 是 | 窗口与阈值均有记录；GUI 终态正文不显示具体数值 | [bestseller_monitor/guard.py:371](F:/AI/projects/bestseller/bestseller_monitor/guard.py:371) |
| 单店 deny 阈值异常 | 店铺 {shop_key} {deny_window_minutes} 分钟内 deny≥{deny_shop_limit} | 是 | 只结束该店并降温，不是整轮停止 | [bestseller_monitor/guard.py:375](F:/AI/projects/bestseller/bestseller_monitor/guard.py:375) |
| 单店人工介入失败 | 人工介入未完成：{exc} | 由异常决定 | 滑块/登录墙异常现含类型与超时 | [bestseller_monitor/pipeline.py:110](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:110) |
| 身份验证单店失败 | 身份验证等待超时，本店未完成 | 是 | 店铺原因 | [bestseller_monitor/pipeline.py:104](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:104) |
| 单店 deny 放弃 | deny 超过阈值，这家店本轮到此为止：{exc} | 是 | 单店停止 | [bestseller_monitor/pipeline.py:116](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:116) |
| CLI/存储跨天原因 | 库存数据即将跨天，请0点后继续抓取 | 是 | “继续抓取”容易被理解为继续原轮，实际原轮已终态 | [bestseller_monitor/db.py:431](F:/AI/projects/bestseller/bestseller_monitor/db.py:431) |
| CLI/存储预算耗尽原因 | 本轮详情预算已用尽，剩余商品留待下一轮 | 是 | 预算耗尽 | [bestseller_monitor/db.py:432](F:/AI/projects/bestseller/bestseller_monitor/db.py:432) |
| 失败率具体原因 | 失败率 {fail_rate:.1%} 超过阈值 {fail_rate_limit:.0%}（快照失败 {failed_offers}，点击未得商品 {click_fail}），需人工决策 | 是 | CLI 和终态 note 含数值；GUI 当前展示通用失败率模板 | [bestseller_monitor/pipeline.py:433](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:433) |
| 无法核验身份 | 暂时无法核验原采集身份 | 部分 | 收尾受阻阶段；没有底层原因 | [bestseller_monitor/views.py:49](F:/AI/projects/bestseller/bestseller_monitor/views.py:49) |
| 数据库不可用 | 数据库暂不可用 | 部分 | 收尾受阻原因类别，无异常详情 | [bestseller_monitor/views.py:50](F:/AI/projects/bestseller/bestseller_monitor/views.py:50) |
| 无法观察进程 | 暂时无法读取采集进程状态 | 部分 | 收尾受阻阶段；没有底层原因 | [bestseller_monitor/views.py:51](F:/AI/projects/bestseller/bestseller_monitor/views.py:51) |
| 无法核验浏览器归属 | 暂时无法核验采集浏览器归属 | 部分 | 收尾受阻阶段；没有底层原因 | [bestseller_monitor/views.py:52](F:/AI/projects/bestseller/bestseller_monitor/views.py:52) |
| 浏览器绑定变化 | 采集浏览器绑定已变化，暂不处置 | 有处置被拒原因 | 不是最初停止原因 | [bestseller_monitor/views.py:53](F:/AI/projects/bestseller/bestseller_monitor/views.py:53) |
| 交换报告启动失败 | 当天采集启动或初始化时失败，未进入采集 | 部分 | 有阶段，无具体异常 | [bestseller_monitor/exchange.py:117](F:/AI/projects/bestseller/bestseller_monitor/exchange.py:117) |
| 交换报告未知终态 | 当天采集的终态不详（旧数据） | 否；明确旧数据不详 | 没有编造原因 | [bestseller_monitor/exchange.py:118](F:/AI/projects/bestseller/bestseller_monitor/exchange.py:118) |

## 工具与演示代码（不计入产品提示）

| 用途 | 文案或说明 | 原因判定 | 源码 |
| --- | --- | --- | --- |
| 界面截图演示 | 内嵌演示数据含“人工中止”结果页，使用模拟暂停状态；不是运行时结果来源。 | 不适用 | [tools/_shot_live.py:11](F:/AI/projects/bestseller/tools/_shot_live.py:11) |
| 诊断/问题记录工具 | f"    暂停前端口占用者={port_owner()}" | 不适用 | [tools/diag_edge_cleanup.py:125](F:/AI/projects/bestseller/tools/diag_edge_cleanup.py:125) |
| 诊断/问题记录工具 | <br>[2/2] 上一轮被暂停强杀留下浏览器，本轮 handoff 不误杀（期望绿） | 不适用 | [tools/diag_edge_cleanup.py:146](F:/AI/projects/bestseller/tools/diag_edge_cleanup.py:146) |
| 诊断/问题记录工具 | 已认领界面暂停请求 | 不适用 | [tools/diag_stop_request.py:174](F:/AI/projects/bestseller/tools/diag_stop_request.py:174) |
| 诊断/问题记录工具 | f"轮次终态={round_row['terminal_reason'] if round_row else '（没有轮次）'}"<br>                   f" 停止请求={'已清掉' if _stop_request(db_path) is None else '还在'}" | 不适用 | [tools/diag_stop_request.py:182](F:/AI/projects/bestseller/tools/diag_stop_request.py:182) |
| 诊断/问题记录工具 | <br>[1/2] 界面请求暂停：采集进程在检查点自己停下（期望绿） | 不适用 | [tools/diag_stop_request.py:224](F:/AI/projects/bestseller/tools/diag_stop_request.py:224) |
| 诊断/问题记录工具 | 停止请求 | 不适用 | [tools/issue_bundle.py:71](F:/AI/projects/bestseller/tools/issue_bundle.py:71) |
| 诊断/问题记录工具 | stop_requests 那一行：有没有还没被认领的暂停请求 | 不适用 | [tools/issue_bundle.py:72](F:/AI/projects/bestseller/tools/issue_bundle.py:72) |

## 覆盖与验证

- Python 字面值扫描以 `git ls-files` 为输入，排除注释和 docstring；结果逐项分类如上。HTML 经关键词搜索逐行核对，拼接句按实际显示组合。入口、shells、三个页面均纳入；交换页面没有这组停止关键词的独立文案。
- 扫描还纳入“终止”；按语义补查 RoundPauseRequired、人工介入超时、跨天、预算耗尽、失败率超限和停止失败映射。按钮、日志和状态片段均与完整 GUI 提示区分。
- 定向验证：guard 44、views 33、round_wiring 20、leftover_rounds 26，均通过；其中包含实际 HTML 横幅的浏览器验证、原因不跨轮串用、新执行清除旧原因、未知原因降级以及暂停/续跑资格保持。
- 分段全量：合计 2163 项（预期 2163，计数对账一致），总耗时 1257.9s，段结果 7/7 段 OK，结论 OK；退出码 0。
- 验收证据：[summary.txt](F:/AI/projects/bestseller/.scratch/logs/tickets/stop-reasons/01/20261024-copy/summary.txt)；副本输入及 SHA-256 核对见 [inputs.json](F:\AI\projects\bestseller\.scratch\logs\tickets\stop-reasons\01\20261024-copy\inputs.json)。
- 修改文件的 Git diff 空白检查通过；隔离副本保留，未执行清理。


### Q1 后续验证（2026-10-24）

用户确认失败率超限使用“中止”后，修改 views.py 标签和正文；定向 `test_views.py` 33 项通过（3.928s）。上面的分段全量记录对应此前保存的输入，不包含这次纯文案修改。
