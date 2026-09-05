# 项目 Issue 列表

> 用途：汇总 1688 畅销榜 × SKU 库存快照 MVP 当前**尚未解决**的问题，作为项目 issue list。
> 维护约定：**每天结束前更新**；每条含 状态 / 描述 / 证据 / 下一步。已解决的移到“最近已解决”并在日期旁打勾。

最近更新：2026-09-05

---

## A. 待处理（开放）（按优先级）

| ID | 优先级 | 状态 | 标题 | 说明 / 证据 | 下一步 |
|---|---|---|---|---|---|
| IS-02 | 高 | 进行中 | **点击→弹窗可靠性不稳定（根因：反爬限流 deny）** | round #10 实测整体成功率约 65%；主失败是 `url_notoffer`（142），集中在 A07(62)/A12(41)/A09(27)。**诊断确认：点击落到 `market.m.taobao.com/.../bsop-punish-test-webapp/deny_pc.html`（淘宝 deny/验证页，需 App 扫码），是连续高频采集触发的反爬限流**，非卡片模板问题。A12/A09 冷却后已恢复，A07 当时仍被 deny；`no_popup` 每店约 3（多为 logo/非商品卡），`parse_empty` 4。 | **已把 `deny_pc`/`bsop-punish` 纳入验证识别**（`_is_punish_url`+`intervention_kind`），不再静默当“非offer”。**下一步：给爬虫加“遇 deny 自动降速/退避”**，降低触发限流概率（比响铃扫码更治本）。 |
| IS-04 | 中 | 待测 | **已采集完的店铺处理偏慢** | 已采集店（当天全采过）再次运行时仍偏慢。现有数据：round #4 A01 首跑 6分10秒/58 offer；round #6 A01 重跑 5分34秒（32 新 + 58 按名跳过）；IS-03 冒烟 A10 单页 110 秒（30 个全“暂缓/跳过”，几乎没点弹窗）。**结论：已采集店的耗时主要在“遍历列表 + 每页滚动加载 + 读商品名”，不在点详情。** | 优化方向：已采集店只翻列表页读名、不点弹窗（预扫描/名称开关）；或整店已采集时只做“列表+按名判断”快速通过。可先拿一家全采店计时对比。 |
| IS-12 | 低 | 待定 | **低销商品降频采集** | 新增机制：对 `diff` 较小（按 SKU 汇总库存变化）的商品，把采集频率从“每日”降到“**每 2-3 天一次**”，以节省整体抓取时间。待定项：① “diff 小”阈值怎么定（如近 N 日 `Σ|ΣSKU diff|` 低于多少）；② 按商品还是按店铺降频；③ 与现有“同日跳过/按名跳过”的关系（低销需按“距上次采集 ≥2-3 天”才采，而非“当日已采”）；④ 新品无历史 diff 时默认（建议先按每日，有数据后再降频）。 | 先明确阈值与“是否到采集日”判定；再定实现位置（pipeline 还是抓取循环）。低优先级，可后做。 |

## B. 最近已解决（供追溯）

| ID | 解决日期 | 说明 |
|---|---|---|
| IS-05 | 2026-09-05 | 商品名 `list_title` 提取改为“仅含一张商品图的最小容器取首行”，不再依赖 `已售/¥` 文案；round #4 335/335 非空。 |
| IS-01 | 2026-09-05 | 5 家 0 商品店：采用方案 3（`shops.csv` 显式 `offer_list_url`），已填 12 家；验证 A05/A06/A10/A11/A12 各 1 页全部采到（30/30/29/30/30 offer）。 |
| IS-03 | 2026-09-05 | 同名商品“按名跳过”误判：改为**计数+暂缓+计数>1补抓**。每店每个商品名计数；计数=1且已有库存→暂缓；计数≥2→当场抓；最终计数>1→第二遍按名补抓（offer_id 去重，避免重复/遗漏）。选项A：计数==1（唯一名已有库存）默认跳过，接受极小概率“榜外同名”漏采。逻辑经模拟验证（同名/全新增/混合顺序/三同名均不漏），A10 冒烟确认计数+暂缓正常。 |
| IS-06 | 2026-09-05 | 同日去重：按 `(shop_key, offer_id, 当日)` 与按 `(shop_key, product_name, 当日)` 双轨，按名可点前跳过、offer_id 兜底。 |
| IS-07 | 2026-09-05 | 新增“同店同名商品异常检测”：重复商品名记 `warning` + `event_log.duplicate_name`（A02×2、A07、A08、A10 共 5 条）。 |
| IS-08 | 2026-09-05 | 修复 `cst_date()` 缺失默认参数的回归（补了无参调用测试）。 |
| IS-09 | 2026-09-05 | 误报“人工介入”大修：① `_is_punish_url` 排除 `_____tmd_____/punish?x5secdata` 装饰 URL；② `intervention_kind` 仅当有验证文案或非“点我反馈”拦截页才算；③ `wait_for_resolution` 加“确认窗口 + 刷新兜底”。A02 卡片验证 `intervention=None`。 |
| IS-10 | 2026-09-05 | A02 `inventory.product_name` 回填为卡片标题（90 offer / 514 行）。 |
| IS-11 | 2026-09-05 | 清理废弃轮次（round #1、#3、#5）及测试日志（run_test*.log、run_full.log）。 |

## C. 数据/环境现状（便于每天参考）

- 有效轮次：round #2（A02，90 offer/514 快照）、round #4（12 店，335 offer/1947 快照）、round #6（A01 补采，32 offer/162 快照）。
- round #10（今天完整跑）：A04/A05/A06/A09/A10/A11/A12 等补采；整体点击成功率约 65%，主失败为 `url_notoffer`（点击落到淘宝 `deny_pc.html`，即连续高频采集触发的反爬限流）。A05/A10/A11≈95%，A07/A12/A09 因 deny 较低；`deny_pc` 已被识别为“需人工”。
- 主数据：shops / products / skus / inventory 只增改不删；本次运行会用 `shops.csv` upsert 回 `shops`。
- 抓取驱动：`driver=pw_cdp`（Playwright 接管已登录 Edge，`profiles/account1_edge`）。
- 运行：`python run.py`（**仅店铺模式**，商品URL清单抓取已移除）；`--mode` 仅保留 `shops`（兼容旧命令）。`--limit-shops Axx` / `--pages-per-shop N` 可单店限页。
- `shops.csv` 列：`shop_key, shop_name, shop_url, pages, active, offer_list_url`。`offer_list_url` 为“全部商品页”URL，填了用 `page.goto` 进入它，空则自动拼 `/page/offerlist.htm`；`shop_url` 是店铺首页（存在 `Shop.home_url`）。
- 工具：`tools/analyze_delay.py`（延迟×验证关联分析）、`tools/sync_list_titles.py`（把某店 `inventory.product_name` 刷成卡片标题）、`tools/diag_verify_state.py`（诊断弹窗验证状态）、`tools/diag_offer_id_presolve.py`（列表页 offer_id 预解析探测）。

---

**每天结束前**：把新增/解决的 issue 更新进本文件，保持状态、证据、下一步清晰；不清晰的地方标“待确认”。
