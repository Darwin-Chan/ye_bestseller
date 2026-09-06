# 项目 Issue 列表

> 用途：汇总 1688 畅销榜 × SKU 库存快照 MVP 当前**尚未解决**的问题，作为项目 issue list。
> 维护约定：**每天结束前更新**；每条含 状态 / 描述 / 证据 / 下一步。已解决的移到「最近已解决」并在日期旁打勾。

最近更新：2026-09-06（本次基于代码审查 + 测试 + 数据库现场核查）

---

## A. 待处理（开放）（按优先级）

| ID | 优先级 | 状态 | 标题 | 说明 / 证据 | 下一步 |
|---|---|---|---|---|---|
| IS-04 | 中 | 待测 | **已采集完的店铺处理偏慢** | 已采集店（当天全采过）再次运行时仍偏慢。现有数据：round #4 A01 首跑 6分10秒/58 offer；round #6 A01 重跑 5分34秒（32 新 + 58 按名跳过）；IS-03 冒烟 A10 单页 110 秒（30 个全「暂缓/跳过」）。结论：已采集店的耗时主要在「遍历列表 + 每页滚动加载 + 读商品名」，不在点详情。 | 优化方向：已采集店只翻列表页读名、不点弹窗（预扫描/名称开关）；或整店已采集时只做「列表+按名判断」快速通过。可先拿一家全采店计时对比。 |
| IS-22 | 中 | 待处理 | **数据/轮次现状文档过期，未记录第 7/8/11/12 轮，且第 12 轮为「已放弃」** | 数据库（`data/bestseller.db`）现有轮次 #2,4,6,7,8,10,11 状态「完成」和 #12 状态「已放弃」（phase=abandoned，3/12 店、109 offer、691 快照、deny=203 次）；`inventory` 有 09-05（6012 行）与 09-06（691 行）。而 ISSUES「数据/环境现状」仍写「有效轮次 #2/#4/#6、round #10 今天完整跑」，日期停在 09-05。 | 更新轮次清单与「有效轮」口径（区分 完成/已放弃/进行中）；明确「已放弃轮次写入的 inventory 是否应参与同日去重判断」（当前会因当天已有库存而跳过，需确认是否回滚）。 |
| IS-23 | 低 | 待定 | **三条抓取驱动路径并存，直连路径大概率失效且无自动化覆盖** | `pipeline.py` 同时保留 `_run_pw_round`（Playwright 直连）、`_run_dp_round`（DrissionPage）、`_run_pwcdp_round`（CDP 点击，为主路径）。直连路径的 `crawl_shop_listing` 依赖 `<a href>` 抓商品，但 1688 卡片多为无 href 的图片卡；`_run_listing_phase`/`_run_detail_phase` 等函数无单测覆盖。 | 评估后移除未使用的直连路径，或在 PRD 注明其仅为降级备用；为保留逻辑补 fixture 单测，并明确唯一推荐驱动。 |
| IS-12 | 低 | 待定 | **低销商品降频采集** | 对 `diff` 较小（按 SKU 汇总库存变化）的商品，把采集频率从「每日」降到「每 2-3 天一次」以节省时间。待定：①「diff 小」阈值怎么定；② 按商品还是按店铺降频；③ 与现有「同日跳过/按名跳过」的关系（低销需按「距上次采集≥2-3 天」才采，而非「当日已采」）；④ 新品无历史 diff 时默认（建议先每日，有数据再降频）。 | 先明确阈值与「是否到采集日」判定，再定实现位置（pipeline 还是抓取循环）。低优先级，可后做。 |

---

## B. 最近已解决（供追溯）

| ID | 解决日期 | 说明 |
|---|---|---|
| IS-14 | 2026-09-06 | **已改代码**：收尾不再全局 `taskkill /IM msedge.exe /F`，改为记录本次 `subprocess.Popen(...)` 的 PID，仅 `taskkill /PID <pid> /T /F` 结束本次启动的浏览器进程树（`browser_pw.close_session`、`browser_dp.stop_browser`）。**待下一轮实测确认**不误关其它 Edge 窗口。 |
| IS-15 | 2026-09-06 | **已改代码**：① `_ingest_detail` 遇到当日已采商品，先把它计入 `shop_offers` 榜单，再 `mark_skipped` 补写一条“成功/跳过”快照；② 按名暂缓也通过 `find_offer_id_by_name`（仅当同名唯一）补记录。**待下一轮实测确认**轮次商品数不再被低估。 |
| IS-16 | 2026-09-06 | **已改代码**：新增 `db.click_card_failures()`（按 shop+page+idx 去重，`click_ok/click_skipped` 视为成功，`click_no_popup/click_url_notoffer/click_deny` 且再无成功即为失败），`_finalize_round` 据此把「点击后未得到商品」的卡片计入失败率，使失败率>10% 的兜底对点击失败也生效。**待下一轮实测确认**。 |
| IS-17 | 2026-09-06 | **已改代码**：把「滑块/登录墙/deny/punish」判定收敛到 `guard.py`（`is_punish_url/is_deny_url/is_login_url/vtype/intervention_kind/wait_for_resolution` 等）；`browser_pw` 改为从 `guard` 导入（保留旧名别名供诊断工具），`browser_dp` 复用 `guard` 常量与 URL 判定，删除各自重复实现。**待下一轮实测确认**。 |
| IS-18 | 2026-09-06 | **已改代码**：点击式路径的 `se()` 支持按调用覆盖 `phase`；`_ingest_detail` 内详情事件（`detail_parse/click_ok/click_parse_empty/click_skipped/popup_open/popup_close/click_deny`）统一标为 `phase="detail"`，列表事件仍为 `listing`，避免分析脚本按阶段错置。 |
| IS-19 | 2026-09-06 | **已改代码**：主路径 `crawl_store_by_click` 接入 `human.after_load()`（`read_delay_sec`）与 `human.before_action()`（`action_delay_sec`），使这两项拟人化延迟在 `pw_cdp` 下真正生效；另在 `config.toml` 增 `[builtin_waits]` 留档内置固定等待，并在 PRD 增「延迟与等待口径」说明（区分可配置与内置固定）。 |
| IS-20 | 2026-09-06 | **已改代码**：`report._stamp()` 改为把 `started_at`(UTC) 转北京时间再命名，与库存「当日去重」口径一致；新增跨日边界单测（UTC 09-05 23:30 → 北京 09-06 07:30）。 |
| IS-21 | 2026-09-06 | **已改代码**：`config.toml` 的 `profile_dir` 统一为 `profiles/account1_edge`（与 `user_data_path` 一致，避免 Playwright 直连用错 Chrome 配置）；`requirements.txt` 补充 `DrissionPage>=4.0`。 |
| IS-05 | 2026-09-05 | 商品名 `list_title` 提取改为「仅含一张商品图的最小容器取首行」，不再依赖 `已售/¥` 文案；round #4 335/335 非空。 |
| IS-01 | 2026-09-05 | 5 家 0 商品店：采用方案 3（`shops.csv` 显式 `offer_list_url`），已填 12 家；验证 A05/A06/A10/A11/A12 各 1 页全部采到（30/30/29/30/30 offer）。 |
| IS-03 | 2026-09-05 | 同名商品「按名跳过」误判：改为**计数+暂缓+计数>1补抓**。每店每个商品名计数；计数=1且已有库存→暂缓；计数≥2→当场抓；最终计数>1→第二遍按名补抓（offer_id 去重）。 |
| IS-02 | 2026-09-05 | 点击→弹窗可靠性：埋点 + 分析脚本 + 定位根因=连续高频采集触发的淘宝反爬限流。已加 `_is_deny_url`、`deny_backoff_sec` + 埋点 `click_deny`。 |
| IS-13 | 2026-09-06 | 抗 deny 限流：`_capture_card` 遇 deny 按「该商品」计数——第 1 次退避 30s、第 2 次退避 60s、第 3 次响铃扫码、解除后限时 30s 重抓；滚动 10 分钟窗口：该店 deny≥7 跳过该店、整轮 deny≥10 中止本轮。扫码/解除判定（URL 离开 deny）为初版，待真实命中后优化。 |
| IS-06 | 2026-09-05 | 同日去重：按 `(shop_key, offer_id, 当日)` 与按 `(shop_key, product_name, 当日)` 双轨，按名可点前跳过、offer_id 兜底。 |
| IS-07 | 2026-09-05 | 新增「同店同名商品异常检测」：重复商品名记 `warning` + `event_log.duplicate_name`。 |
| IS-08 | 2026-09-05 | 修复 `cst_date()` 缺失默认参数的回归（补了无参调用测试）。 |
| IS-09 | 2026-09-05 | 误报「人工介入」大修：① `_is_punish_url` 排除 `_____tmd_____/punish?x5secdata` 装饰 URL；② `intervention_kind` 仅当有验证文案或非「点我反馈」拦截页才算；③ `wait_for_resolution` 加「确认窗口 + 刷新兜底」。 |
| IS-10 | 2026-09-05 | A02 `inventory.product_name` 回填为卡片标题（90 offer / 514 行）。 |
| IS-11 | 2026-09-05 | 清理废弃轮次（round #1、#3、#5）及测试日志（run_test*.log、run_full.log）。 |

---

## C. 数据 / 环境现状（2026-09-06 核查）

**轮次（`data/bestseller.db`，时间为 UTC）**

- 完成：#2（1 店/90 offer/514 快照）、#4（12 店/335 offer/1947 快照）、#6（A01 补采/32 offer/162 快照）、#7（1 店/30 offer/153 快照）、#8（4 店/119 offer/717 快照）、#10（12 店/343 offer/1756 成功 + 8 失败）、#11（3 店/127 offer/764 快照，09-05 晚 23:53 结束）。
- 已放弃：#12（09-05 16:21 UTC 起跑，约北京 09-06 00:21；3/12 店、109 offer、691 快照、deny 计数 203，用户手动放弃）。**当前无「进行中」轮次。**
- `inventory`：09-05 共 6012 行、09-06 共 691 行（来自已放弃的 #12）。

**点评**：#12 已放弃但仍写下了 09-06 的库存，按「同日去重」逻辑会让这些商品在 09-06 当天被当作「已采过」而跳过，需确认是否需要回滚或排除（见 IS-22）。

**运行与配置**

- 抓取驱动：`driver=pw_cdp`（Playwright 连接接管已登录 Edge，`profiles/account1_edge`）。
- 运行：`python run.py`（仅店铺模式；`--mode` 仅保留 `shops`）。`--limit-shops Axx` / `--pages-per-shop N` 可单店限页。
- `shops.csv`：当前 12 家（A01–A12），各家 `pages=3`、`active=1`，均显式配置 `offer_list_url`。列：`shop_key, shop_name, shop_url, pages, active, offer_list_url`。`offer_list_url` 为「全部商品页」URL，填了就 `page.goto` 进入，空则自动拼 `/page/offerlist.htm`。
- 主数据：shops / products / skus / inventory 只增改不删；每次运行会以 `shops.csv` upsert 回 `shops`。
- 工具：`tools/analyze_click.py`（点击成功率）、`tools/analyze_delay.py`（延迟×验证关联）、`tools/sync_list_titles.py`、`tools/diag_offer_id_presolve.py`、`tools/diag_verify_state.py`、`tools/summary.py`。
- 测试：`python -m unittest discover -s tests -p "test_*.py"` 共 27 项全部通过；`py_compile` 全部源码通过（33 个文件）。

---

**每天结束前**：把新增/解决的 issue 更新进本文件，保持状态、证据、下一步清晰；不清晰的地方标「待确认」。
