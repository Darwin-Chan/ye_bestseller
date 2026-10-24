# deny 已发生但两页显示 0：诊断与修复

日期：2026-10-25。现场包：2026-10-24-1046-m1-界面deny次数显示0：首次deny降温期暂停导致click_deny未记账。
本次仅修复 deny 次数投影；A09 详情就绪、人工校验、响铃及交换台均不在范围内。

## Phase 1：真实反馈回路

命令：`python -m unittest discover -s tests -p test_deny_counts.py -v`。
最初单测 0.114 秒失败：deny_hit=1、deny_cooldown=1、click_deny=0；
降温中及暂停后的 (过程页, 结果页) 均为 (0,0)，期望均为 (1,1)。
只替代浏览器边界；ShopWalk.capture → begin_detail_visit → ready_detail_page →
DenyTracker.record → Humanizer.sleep → waiting → stop_request.consume → StopRequested
以及 Database 和 views.run_view/result_view 都是真实实现。
使用冻结日期、临时数据库、虚拟等待时钟；无需账号、真实页面或实际等待 600 秒。

## Phase 2：复现与最小化

一张 deny 卡、一个店铺、一次首次降温即可复现，无需前 179 个成功商品或滑块事件。

| 场景 | 原代码事件/返回顺序 | 原代码两页次数 |
| --- | --- | --- |
| 完整降温 | deny_hit → deny_cooldown → 等待 → record 返回 → click_deny → capture 返回 | 等待中 0，返回后 1 |
| 降温期暂停 | deny_hit → deny_cooldown → 等待 → StopRequested；record 未返回 | 等待中和暂停后均 0 |
| 暂停后成功续跑 | 从账本恢复降温记忆 → 成功 capture 返回，无新 deny | 仍为 0 |

_round_counts 只数本轮 click_deny，_shop_span 同样按店铺数 click_deny；
过程页和结果页共用这些投影。证据包给出现场数据库摘录，并非完整数据库文件；
没有修改现场数据，现场事件数量与可执行回路相符。

## Phase 3：事先公布的可证伪假设

按下列顺序先向用户公布预测，再执行探针：

1. 暂停中断延迟点击写账：完整等待会有 click_deny，等待中注入停止稳定缺失。
2. UI 事实来源不全：补采即使正常返回，也可能只有 deny_hit 而无 click_deny。
3. 续跑只恢复限流记忆：不会补回点击结果；新一次命中应作为第二次，而不重罚首次降温。
4. 延迟留证有相同阻塞点：等待内暂停会使原始 deny 页未落盘，计数修复不能恢复缺失页面。

## Phase 4：定向验证

同一条真实调用链分别改变完整等待/暂停/中止/等待异常，记录事件回调、等待进入/返回、
capture 返回及两页取数；补采只替代 browser_pw.navigate_detail 并走 _capture_one_pw。
预测均获支持：中止和异常均只有 hit/cooldown；补采正常返回也能只有 hit；
完整等待后才有 click_deny 及原始页，打断则无存档。未增加生产临时日志。

## Phase 5：失败回归与最小修复

扩展后的修复前 12 测试：11 failures、0 errors；剩余通过的是纯旧版兼容场景。
新增正常暂停无 deny 的负对照后共 13 测试。
生产代码仅变更 Database.deny_count 和两页共用的计数入口。
采用 [ADR-0063](../adr/0063-deny-count-from-detection-ledger.md) 的轮次统一边界：
首次 deny_hit 前的历史 click_deny + 全部 deny_hit；不把配对的两种事件相加。
现有首次降温、商品级退避、停止、阈值、失败率和原始页留证机制不变。
不迁移事件、不伪造 click_deny，也不通过商品身份猜测事件配对。
首趟全量揭示了新 SQL 的 GUI 大库刷新退化：首 hit 查找及可选店铺条件导致逐店扫描整轮。
逐变量的 3 万条事件探针中，明确 shop/event 条件使约 31 万 SQL VM 步降至 15 万，
再加入 (round_id, event) 索引降至约 100，结果始终为 1。
补充该索引到既有开库流程和 EVENT_REFRESH_INDEXES，沿用既有旧库补建索引测试，
并保持 GUI 性能验收阈值不变；专项 GuiRefreshCost 测试通过。

## Phase 6：验收

修复后，原始单测和三场景矩阵均变绿，等待中/暂停后/续跑后两页均为 1；
完整等待仍仅 1，第二次真实命中为 2，重复读取不写数据库。
回归文件 13 测试通过；既有 test_views、test_db、test_deny_tracker、test_click_listing、
test_detail_visit、test_p1、test_stop_request、test_round_stop 均通过。
索引修正后 13 项计数测试、85 项数据库测试及 GUI 大库刷新专项再次通过。
分段全量结果：2026-10-25 01:16:42，在最终优化副本中运行，7/7 段通过；合计 2212 项，预期 2212，计数对账一致，总耗时 1522.7 秒，结论 OK。首趟未加事件索引的候选副本保留为失败证据：同样 2212 项中 1923 项段仅有 GUI 大库刷新性能阈值失败；索引修正后的专项测试与最终全量均通过。

## 证据落点

本机工单：`.scratch/deny-count/issues/01-interrupted-cooldown.md`。
主仓日志：`.scratch/logs/tickets/deny-count/01/`，含 Phase 1 初红、Phase 2 矩阵、
Phase 4 探针、Phase 5 红绿及 Phase 6 原始场景重放。
临时脚本仅保留在明确诊断目录 `.scratch/tool-output/deny-count/01/`，不进入运行时目录。
所有测试使用合成身份和数据，无凭据；敏感字段如需展示统一写为 `<REDACTED>`。
