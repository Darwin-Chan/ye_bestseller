# 项目 Issue 列表

> 用途：汇总 1688 畅销榜 × SKU 库存快照 MVP 当前**尚未解决**的问题，作为项目 issue list。
> 维护约定：**每天结束前更新**；每条含 状态 / 描述 / 证据 / 下一步。已解决的移到“最近已解决”并在日期旁打勾。

最近更新：2026-09-05

---

## A. 待处理（开放）（按优先级）

| ID | 优先级 | 状态 | 标题 | 说明 / 证据 | 下一步 |
|---|---|---|---|---|---|
| IS-01 | 高 | 进行中 | **5 家店采到 0 个商品** | round #4 里 A05 / A06 / A10 / A11 / A12 点了 93 张卡却没开出详情弹窗（各店 0 offer）。A03=90/93、A08=89/93 正常，说明是这些店的页面模板/点击路径不同，不是数据问题。 | **已采用方案 3**：`shops.csv` 新增 `offer_list_url` 列，每家店显式配置“全部商品页”URL（填了用它抓，空则自动 `/page/offerlist.htm`）。**等你把 A05/A06/A10/A11/A12 的 offer_list_url 填好，我重跑验证。** |
| IS-02 | 高 | 待处理 | **点击→弹窗可靠性不稳定** | round #4 各店成功率：A03 97%、A08 96%、A01 62%、A09 53%、A07 32%、A04 20%。部分卡片点击后弹窗超时 / 同标签跳转 / 非商品卡，导致漏采（A01 漏的 32 个靠 round #6 补回）。注意 round #4 因“0 商品店不计失败”而被误标为完成，掩盖了缺口。 | 给 `_click_one_product` 加诊断：每次点击结果（弹窗超时 / 同标签 / 拿到URL / 无URL / 非商品卡）写入 `event_log`，重跑一轮量化每类失败。 |
| IS-03 | 中 | 待定 | **同名商品“按名跳过”误判风险** | 同店出现 2 个以上**完全相同**商品名时，按名跳过可能把“另一个同名 offer”漏掉（比例约 1%）。已讨论过你的 defer+重定位找回、我的“预扫描+重名一律开详情”、以及“只用 offer_id 全开”三种方案，**尚未定案**。 | 等你确认选哪种方案后我实现；默认倾向“预扫描+重名一律开详情”，可先跑 1 家验证。 |
| IS-04 | 中 | 待测 | **已采集完的店铺处理偏慢** | 已采集店（当天全采过）再次运行时仍偏慢。参考：round #4 A01 首跑 6分10秒/58 offer；round #6 A01 重跑 5分34秒（32 新 + 58 按名跳过，仍有 32 个要抓所以不轻快）。**“全部已采、纯跳过”的店还没测过。** | 明天测：选一家已全采店（或先把 A01 补全后重跑），测“纯按名跳过”的耗时；并研究优化（预扫描后全跳过 / 已采店只翻列表不点弹窗 / 用已采 offer_id 或名称集合做开关判断）。 |
| IS-12 | 低 | 待定 | **低销商品降频采集** | 新增机制：对 `diff` 较小（按 SKU 汇总库存变化）的商品，把采集频率从“每日”降到“**每 2-3 天一次**”，以节省整体抓取时间。待定项：① “diff 小”阈值怎么定（如近 N 日 `Σ|ΣSKU diff|` 低于多少）；② 按商品还是按店铺降频；③ 与现有“同日跳过/按名跳过”的关系（低销需按“距上次采集 ≥2-3 天”才采，而非“当日已采”）；④ 新品无历史 diff 时默认（建议先按每日，有数据后再降频）。 | 先明确阈值与“是否到采集日”判定；再定实现位置（pipeline 还是抓取循环）。低优先级，可后做。 |

## B. 最近已解决（供追溯）

| ID | 解决日期 | 说明 |
|---|---|---|
| IS-05 | 2026-09-05 | 商品名 `list_title` 提取改为“仅含一张商品图的最小容器取首行”，不再依赖 `已售/¥` 文案；round #4 335/335 非空。 |
| IS-06 | 2026-09-05 | 同日去重：按 `(shop_key, offer_id, 当日)` 与按 `(shop_key, product_name, 当日)` 双轨，按名可点前跳过、offer_id 兜底。 |
| IS-07 | 2026-09-05 | 新增“同店同名商品异常检测”：重复商品名记 `warning` + `event_log.duplicate_name`（A02×2、A07、A08、A10 共 5 条）。 |
| IS-08 | 2026-09-05 | 修复 `cst_date()` 缺失默认参数的回归（补了无参调用测试）。 |
| IS-09 | 2026-09-05 | 误报“人工介入”大修：① `_is_punish_url` 排除 `_____tmd_____/punish?x5secdata` 装饰 URL；② `intervention_kind` 仅当有验证文案或非“点我反馈”拦截页才算；③ `wait_for_resolution` 加“确认窗口 + 刷新兜底”。A02 卡片验证 `intervention=None`。 |
| IS-10 | 2026-09-05 | A02 `inventory.product_name` 回填为卡片标题（90 offer / 514 行）。 |
| IS-11 | 2026-09-05 | 清理废弃轮次（round #1、#3、#5）及测试日志（run_test*.log、run_full.log）。 |

## C. 数据/环境现状（便于每天参考）

- 有效轮次：round #2（A02，90 offer/514 快照）、round #4（12 店，335 offer/1947 快照）、round #6（A01 补采，32 offer/162 快照）。
- 主数据：shops / products / skus / inventory 只增改不删；本次运行会用 `shops.csv` upsert 回 `shops`。
- 抓取驱动：`driver=pw_cdp`（Playwright 接管已登录 Edge，`profiles/account1_edge`）。
- 运行：`python run.py --mode shops`（店铺模式；默认 auto 会因 `product_urls.csv` 存在而走商品URL模式，需 `--mode shops`）。`--limit-shops Axx` / `--pages-per-shop N` 可单店限页。
- `shops.csv` 列：`shop_key, shop_name, shop_url, pages, active, offer_list_url`。`offer_list_url` 为“全部商品页”URL，填了用 `page.goto` 进入它，空则自动拼 `/page/offerlist.htm`；`shop_url` 是店铺首页（存在 `Shop.home_url`）。
- 工具：`tools/analyze_delay.py`（延迟×验证关联分析）、`tools/sync_list_titles.py`（把某店 `inventory.product_name` 刷成卡片标题）、`tools/diag_verify_state.py`（诊断弹窗验证状态）、`tools/diag_offer_id_presolve.py`（列表页 offer_id 预解析探测）。

---

**每天结束前**：把新增/解决的 issue 更新进本文件，保持状态、证据、下一步清晰；不清晰的地方标“待确认”。
