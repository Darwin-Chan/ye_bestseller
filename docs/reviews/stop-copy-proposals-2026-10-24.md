# 停止、中止、暂停文案修改前后对照（2026-10-24）

共 28 项，按展示场景合并重复调用：A1–A4 是此前已完成的原因提示，B1 是用户本轮确认的 Q1，C1–G2 共 23 项建议均已按用户批准实施，已完成实施和验收处置。D7、D8 等一行列出多个状态变体，逐一对照。

“修改前”以本次工作开始前的源码为基线；“修改后”列为本次已实施文案；动态占位符按可靠记录填入。全量初跑及失败断言修正复测的准确结果见[交付记录](F:/AI/projects/bestseller/docs/design/stop-copy-consistency-2026-10-24/delivery.md)。所有建议保持现有暂停资格、终态、收尾与重试规则。按钮、确认操作和规则说明不必都加停止原因；本表只列建议调整项，全部原文与判定见[完整盘点](F:/AI/projects/bestseller/docs/reviews/stop-copy-audit-2026-10-24.md)。

占位符：`{原因}`取本次停止或失败的可靠记录；无记录统一显示“未记录”。不能根据停止结果推断用户操作。`{keys}`为未完成店铺，`{后续建议}`沿用当前重试条件生成的建议。原因里不混入入口操作建议；CLI 与 GUI 的续跑资格保持现状。

## 已完成：人工介入超时与未正常结束提示

| 编号 | 场景 | 修改前 | 修改后文案 | 状态与实现要求 | 源码 |
| --- | --- | --- | --- | --- | --- |
| A1 | 滑块超时 | 采集已停止，本轮未正常结束；请确认中止并完成收尾后，再开始新轮。 | 采集已停止，本轮未正常结束；停止原因：人工介入（滑块）等待超时；请确认中止并完成收尾后，再开始新轮。 | 已完成 | [bestseller_monitor/pages/ui_live.html:599](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:599) |
| A2 | 登录墙超时 | 采集已停止，本轮未正常结束；请确认中止并完成收尾后，再开始新轮。 | 采集已停止，本轮未正常结束；停止原因：人工介入（登录墙）等待超时；请确认中止并完成收尾后，再开始新轮。 | 已完成 | [bestseller_monitor/guard.py:495](F:/AI/projects/bestseller/bestseller_monitor/guard.py:495) |
| A3 | 未正常结束且没有原因记录 | 采集已停止，本轮未正常结束；请确认中止并完成收尾后，再开始新轮。 | 采集已停止，本轮未正常结束；停止原因：未记录；请确认中止并完成收尾后，再开始新轮。 | 已完成；不猜测原因 | [bestseller_monitor/views.py:276](F:/AI/projects/bestseller/bestseller_monitor/views.py:276) |
| A4 | 停机后跨天 | 采集已停止且已跨天，本轮未正常结束、不能继续；请确认中止并等待收尾完成后开始新轮。 | 采集已停止且已跨天，本轮未正常结束、不能继续；停止原因：{原因}；请确认中止并完成收尾后，再开始新轮。 | 已完成；跨天事实与最初停止原因分开 | [bestseller_monitor/views.py:276](F:/AI/projects/bestseller/bestseller_monitor/views.py:276) |

## 已确认 Q1：失败率超限

| 编号 | 场景 | 修改前 | 修改后文案 | 状态与实现要求 | 源码 |
| --- | --- | --- | --- | --- | --- |
| B1 | 失败率超限的标签和正文 | 标签：暂停待处理<br>正文：本轮因失败率超过阈值而暂停，需人工决策；未抓取店铺见下方。 | 标签：失败率超限中止<br>正文：本轮因失败率超过阈值而中止，已抓取数据已保留；本轮不可续跑，请完成收尾后再开始新轮；未抓取店铺见下方。 | 已按 Q1 修改；不改变终态或续跑能力 | [bestseller_monitor/views.py:151](F:/AI/projects/bestseller/bestseller_monitor/views.py:151) |

## 已实施：把停止原因与入口操作建议分开

| 编号 | 场景 | 修改前 | 修改后文案 | 状态与实现要求 | 源码 |
| --- | --- | --- | --- | --- | --- |
| C1 | 收到界面暂停请求后的输出 | 本轮已暂停（可再次运行续跑）。 | 本轮已按界面请求暂停，已抓取数据已保留；请在原界面等待收尾完成后，点击「继续」。 | 已实施；仅用于原界面仍具备暂停资格的分支 | [bestseller_monitor/pipeline.py:74](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:74) |
| C2 | 异常停止日志及输出 | 日志：本轮暂停：{exc}<br>输出：本轮已暂停（可再次运行续跑）：{exc} | 日志：本轮采集已停止；停止原因：{原因}。<br>GUI：采集已停止，本轮未正常结束；停止原因：{原因}；请确认中止并完成收尾后，再开始新轮。<br>CLI：本轮采集已停止；停止原因：{原因}；同日、同范围重新运行可续跑尚未结束的本轮；跨天请开始新轮。 | 已实施；需要按入口生成后续操作，不能把 CLI 续跑建议写入共用原因 | [bestseller_monitor/pipeline.py:111](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:111) |
| C3 | 榜单阶段未完成的异常原因 | 榜单阶段未完成：{keys}；请检查存档页面后续跑 | 榜单阶段未完成：{keys} | 已实施；仅从共用原因中移出操作建议；CLI 仍可提示先检查存档页面，GUI 使用 C2 的中止提示 | [bestseller_monitor/pipeline.py:422](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:422) |
| C4 | 详情阶段未完成的异常原因 | 详情阶段未完成：{keys}；请继续补采后续跑 | 详情阶段未完成：{keys} | 已实施；后续操作按 C2 分入口显示 | [bestseller_monitor/pipeline.py:426](F:/AI/projects/bestseller/bestseller_monitor/pipeline.py:426) |

## 已实施：用户暂停与停止收尾

| 编号 | 场景 | 修改前 | 修改后文案 | 状态与实现要求 | 源码 |
| --- | --- | --- | --- | --- | --- |
| D1 | 过程页暂停提示（静态） | 已暂停（轮次保留为「进行中」，点「继续」接着抓取本轮剩余任务）。 | 本轮已由用户主动暂停，已抓取数据已保留；轮次保留为「进行中」，点击「继续」接着抓取本轮剩余任务。 | 已实施；这里有明确暂停资格 | [bestseller_monitor/pages/ui_live.html:147](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:147) |
| D2 | 过程页暂停提示（动态） | 已暂停（轮次保留为「进行中」，点「继续」接着抓取本轮剩余任务；关闭专用 Edge 窗口不会自动继续本轮）。 | 本轮已由用户主动暂停，已抓取数据已保留；轮次保留为「进行中」，点击「继续」接着抓取本轮剩余任务。关闭专用 Edge 窗口不会自动继续本轮。 | 建议 | [bestseller_monitor/pages/ui_live.html:606](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:606) |
| D3 | 已暂停但暂不能继续 | 已暂停（轮次保留为「进行中」），但还不能继续：{continue_gate}；关闭专用 Edge 窗口不会自动继续本轮。 | 本轮已由用户主动暂停，轮次保留为「进行中」；暂不能继续，原因：{continue_gate}。关闭专用 Edge 窗口不会自动继续本轮。 | 已实施；区分暂停来源与继续受阻原因 | [bestseller_monitor/pages/ui_live.html:604](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:604) |
| D4 | 继续启动失败 | 继续启动未成功：{resume_error}。原轮暂停资格保留，请明确点击继续重试。 | 本轮已由用户主动暂停；本次继续启动失败，原因：{resume_error}。原轮暂停资格保留，请明确点击「继续」重试。 | 已实施；失败原因原本已存在，补齐暂停背景 | [bestseller_monitor/pages/ui_live.html:602](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:602) |
| D5 | 开始页暂停轮次行 | 轮次 #{id}（{date}，{shops}）：已暂停，可到过程页继续；如需换范围或新建，请先确认中止。 | 轮次 #{id}（{date}，{shops}）：已由用户主动暂停，可到过程页继续；如需换范围或新建，请先确认中止。 | 建议 | [bestseller_monitor/views.py:288](F:/AI/projects/bestseller/bestseller_monitor/views.py:288) |
| D6 | 开始页暂停摘要 | 轮次 #{id}（{date}）已暂停：请到过程页继续；如需新建，请先确认中止本轮。 | 轮次 #{id}（{date}）已由用户主动暂停：请到过程页继续；如需新建，请先确认中止本轮。 | 已实施；多轮摘要中的「轮次 #{id} 已暂停」也同步增加「由用户主动」 | [bestseller_monitor/views.py:312](F:/AI/projects/bestseller/bestseller_monitor/views.py:312) |
| D7 | 关窗确认中的暂停阶段标题 | 本轮正在暂停，采集进程正在停止<br>本轮正在完成暂停收尾<br>本轮已暂停 | 已收到用户暂停请求，采集进程正在停止<br>用户暂停请求正在收尾<br>本轮已由用户主动暂停 | 已实施；三条按 stopping / cleanup / paused 分别替换 | [bestseller_monitor/crawler_close.py:165](F:/AI/projects/bestseller/bestseller_monitor/crawler_close.py:165) |
| D8 | 采集停止与收尾进度 | 正在停止采集进程…（最多 {N} 秒，超时会强制结束）<br>正在收尾：采集进程在关掉自己启动的浏览器…<br>正在核验停止目标：暂不执行破坏性操作…<br>正在重试收尾：仍只处理原停止目标… | 停止原因：{原因}；正在停止采集进程…（最多 {N} 秒，超时会强制结束）<br>停止原因：{原因}；正在收尾：关闭本次采集启动的浏览器…<br>停止原因：{原因}；正在核验采集进程身份，核验完成前不结束进程或关闭浏览器…<br>停止原因：{原因}；正在重试收尾，仅处理本次停止请求对应的采集进程和浏览器… | 已实施；需要保留原请求类型/原因；{原因}可为用户请求暂停、用户确认中止、用户确认关闭采集窗口，缺失则写未记录。收尾失败另显其失败原因 | [bestseller_monitor/pages/ui_live.html:354](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:354) |

## 已实施：匹配停止

| 编号 | 场景 | 修改前 | 修改后文案 | 状态与实现要求 | 源码 |
| --- | --- | --- | --- | --- | --- |
| E1 | 匹配停止后的摘要 | 本次匹配已停止：已完成 {judged} / {todo} 对<br>存在可重试项时追加：，未判完的对记为「失败」，{后续建议} | 本次匹配已停止；停止原因：{原因}；已完成 {judged} / {todo} 对。<br>存在可重试项时追加：未完成项保留为可重试的「失败」状态，{后续建议}。 | 已实施；需区分用户停止、关窗停止、运行异常和未记录；保留原有重试条件 | [bestseller_monitor/pages/bestseller-analysis.html:975](F:/AI/projects/bestseller/bestseller_monitor/pages/bestseller-analysis.html:975) |
| E2 | 停止匹配等待框 | 正在停止匹配…<br>等一个在途判断结束（最多 {N} 秒）；已判的会留下 | 正在停止匹配…<br>停止原因：{原因}。等待当前判断结束（最多 {N} 秒）；已完成的判断会保留。 | 已实施；需要从本次停止请求带入原因 | [bestseller_monitor/pages/bestseller-analysis.html:964](F:/AI/projects/bestseller/bestseller_monitor/pages/bestseller-analysis.html:964) |
| E3 | 商品匹配停止后说明 | 停下后自动进入本页；没判断的对记为「失败」，点「模型匹配同款」可以接着判 | 停下后自动进入本页；因停止而未判断的对保留为可重试的「失败」状态，点「模型匹配同款」可以接着判。 | 已实施；不改变失败状态、按钮条件或重试规则 | [bestseller_monitor/pages/bestseller-analysis.html:930](F:/AI/projects/bestseller/bestseller_monitor/pages/bestseller-analysis.html:930) |
| E4 | 商品对未判断原因 | 已停止匹配，未判断 | 匹配已停止，本对未完成判断；停止原因：{原因}。 | 已实施；需补原因传递，并保持现有状态识别兼容，不能直接更改用于判码的字符串 | [bestseller_monitor/matching.py:113](F:/AI/projects/bestseller/bestseller_monitor/matching.py:113) |
| E5 | SKU 匹配停止原因 | 用户/运行已停止 | 本次匹配已停止；停止原因：{原因}。 | 已实施；有记录才写用户主动停止匹配、用户关闭窗口或具体运行原因；缺失写未记录 | [bestseller_monitor/sku_lifecycle.py:19](F:/AI/projects/bestseller/bestseller_monitor/sku_lifecycle.py:19) |

## 已实施：启动失败与未知结束

| 编号 | 场景 | 修改前 | 修改后文案 | 状态与实现要求 | 源码 |
| --- | --- | --- | --- | --- | --- |
| F1 | 采集启动失败 | 本次新建在启动或初始化时失败，未进入采集；请核实收尾与配置后明确开始新轮。 | 本次新建在启动或初始化时失败，未进入采集；失败原因：{原因}；请核实收尾与配置后明确开始新轮。 | 已实施；从本轮可靠的启动失败记录取原因，无记录写未记录 | [bestseller_monitor/views.py:139](F:/AI/projects/bestseller/bestseller_monitor/views.py:139) |
| F2 | 未知终态兜底 | 标签：意外中止<br>正文：本轮非正常结束，已抓取数据已保留；未抓取店铺见下方。 | 标签：非正常结束<br>正文：本轮非正常结束；停止原因：{原因}；已抓取数据已保留；未抓取店铺见下方。 | 已实施；现有旧数据通常只能显示未记录 | [bestseller_monitor/views.py:170](F:/AI/projects/bestseller/bestseller_monitor/views.py:170) |
| F3 | 交换报告中的采集启动失败 | 当天采集启动或初始化时失败，未进入采集 | 当天采集启动或初始化时失败，未进入采集；失败原因：{原因} | 已实施；仅使用交换数据已有的可靠记录，缺失写未记录 | [bestseller_monitor/exchange.py:117](F:/AI/projects/bestseller/bestseller_monitor/exchange.py:117) |
| F4 | 交换报告中的未知终态 | 当天采集的终态不详（旧数据）<br>无终态时：当天采集的终态不详 | 当天采集的终态不详（旧数据）；结束原因：未记录<br>无终态时：当天采集的终态不详；结束原因：未记录 | 已实施；不把无终态记录推断为已中止或用户停止 | [bestseller_monitor/exchange.py:118](F:/AI/projects/bestseller/bestseller_monitor/exchange.py:118) |

## 已实施：跨天提示与中止用词

| 编号 | 场景 | 修改前 | 修改后文案 | 状态与实现要求 | 源码 |
| --- | --- | --- | --- | --- | --- |
| G1 | CLI 即将跨天提示 | 库存数据即将跨天，请0点后继续抓取 | 库存数据即将跨天，本轮已中止；请0点后开始新轮 | 已实施；共用常量由各调用处补句末标点，避免重复句号 | [bestseller_monitor/db.py:431](F:/AI/projects/bestseller/bestseller_monitor/db.py:431) |
| G2 | 人工中止确认框标题（两处） | 确认终止本轮抓取？ | 确认中止本轮抓取？ | 已实施；与页面按钮统一 | [bestseller_monitor/pages/ui_live.html:211](F:/AI/projects/bestseller/bestseller_monitor/pages/ui_live.html:211) |

## 实现边界与验证

C2–C4 需要把共用原因与入口操作建议分开。D8、E1、E2、E4、E5 需要传递或保存停止来源；F1、F3 需要接入本轮可靠的失败记录。E4 当前文案参与状态识别，实施时必须保持原有状态识别和历史记录兼容。它们不是直接替换字符串就能完整解决的项。

此前原因展示改动的分段全量：2163 项（预期 2163，对账一致），7/7 段 OK，结论 OK。证据：[summary.txt](F:/AI/projects/bestseller/.scratch/logs/tickets/stop-reasons/01/20261024-copy/summary.txt)。该证据对应先前记录的输入，不包含随后确认的 B1 文案。

B1 本轮仅修改结果标签与正文，执行 `python -m unittest discover -s tests -p test_views.py`：33 项通过（3.928s）。这份 B1 定向记录属于历史阶段；其余建议的当前验收另记在交付记录中。


## 全部实施授权（2026-10-24）

用户已批准 C1–G2 全部 23 项建议，要求使用 grill-with-docs、to-spec、to-tickets、implement。上表“建议/尚未修改”保留为批准前基线。用户已以“按建议”确认测试入口与工单拆分，23 项均已实施、统一全量与双轴审查待完成；见[规格](F:/AI/projects/bestseller/docs/design/stop-copy-consistency-2026-10-24/spec.md)和[工单拆分](F:/AI/projects/bestseller/docs/design/stop-copy-consistency-2026-10-24/tickets.md)。
