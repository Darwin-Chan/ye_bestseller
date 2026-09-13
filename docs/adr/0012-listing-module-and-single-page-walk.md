# 0012. 榜单页收成一个 module：准备与推进各一道 interface

- 状态：已接受
- 日期：2026-09-13

## 背景

`browser_pw.crawl_store_by_click` 里同一条路径写了两遍：主页遍历与同名商品补抓第二遍。

**推进**（点下一页 → 回退加载更多 → 等 load → 确认列表身份变了 → 等卡片）两份几乎逐字相同
（`browser_pw.py:411-427` 与 `:465-479`），只差文案。**准备**（打开页面 → 等首屏卡片 →
人工介入 → 点「销量」排序）两份已经各自长歪：顺序不同（主页先处理介入再排序，补抓先排序再处理
介入）、事件不同（`list_load` / `list_sort` 只有主页那遍发）、兜底不同（主页首屏没卡片直接
抛 `ListingLoadFailed`，补抓那遍只等一次就往下走）。最后一条是隐性 bug：补抓等不到卡片也继续，
`_scroll_cards_until_stable` 返回 0、循环空转，而主页那遍已经抓到了商品，于是「补抓一张卡都没
读到」会被记成正常完成。

榜单页还有两个家：`pagination.py`（列表身份与翻页等待，IS-36）与 `listing.py`（失败异常与
原始页存档），加新代码时先看哪个得猜。

测试的覆盖也是歪的：IS-36 的四个用例只在 `crawl_store_by_click` 级别、只走主路径，为了跑一次要
打六个桩；而补抓那遍的「没换页就报失败」从未被断言——那条用例没给列表身份打桩，
`list_identity` 读不到列表结构返回空元组，`wait_for_change` 就放行了。

## 决策

- **榜单页只有一个 module**：删掉 `pagination.py`，它的列表身份与等待并进 `listing.py`；
  再把 `browser_pw.py` 里那几个纯榜单页原语搬过来（商品卡片选择器、条件等待、点文字按钮、
  页面存档、失败异常、`_WAIT_UI/SORT/NEXT`）——`advance` 与 `prepare` 都要用它们，
  留在原处会让依赖成环。
- **两道 interface**：
  `prepare(page, url, cfg, human, *, describe, punished=False, emit=None)` 与
  `advance(page, human, cfg, describe) -> bool`。`describe` 由调用方给（如「店铺 A01 第 3 页」
  /「店铺 A01 补抓第 3 页」），用于日志与失败文案；翻页前的拟人化延迟归 `advance`。
  `advance` 返回 False = 这一页后面没有下一批；点过而列表身份始终没变 → 抛
  `ListingLoadFailed`（消息保留「翻页后未确认新一页加载」）。
- **`browser_pw.py` 保留驱动本体**：浏览器会话、deny 跟踪、逐卡点击与详情落库、
  `_scroll_cards_until_stable`、`_read_card_title`，以及弹窗期的等待。
- **两处漂移统一成主页那份行为**，`prepare` 因此没有开关：补抓那遍也按「等卡片 → 人工介入 →
  排序」的顺序走，首屏没卡片同样报 `ListingLoadFailed`，`list_load` / `list_sort` 与人工介入
  事件也经同一个 `emit` 发。**这是一处有意的行为修正，不是纯重构**。
- **搬到 `listing.py` 的私有名在 `browser_pw.py` 只留工具需要的兼容别名**（`_PRODUCT_IMG_SEL`、
  `_click_text_in_frames`）；本文件内部一律写 `listing.*`。
- **IS-36 的用例下沉到 `advance` 的接缝**（只打 `click_text_in_frames` / `list_identity` /
  `wait_cards` 三个桩），`crawl_store_by_click` 级别保留两条「遍历把 interface 用对了」的断言；
  补抓那遍的确认规则第一次被真正断言。

## 结果

榜单页只有一处可看，推进与准备各一份实现；补抓路径不再可能静默跑空；`advance` 的接缝让 IS-36
的四个用例少打一半的桩。全套 `python -m unittest discover`：318 条通过、1 条跳过。

代价有三处。其一，补抓那遍的行为变了（多两行事件、会因首屏没卡片而失败）——罕见路径
（只有同店出现同名商品才走），但读 `event_log` 的人要知道多出来的两行是什么。其二，
`browser_pw.py` 里留了两个兼容别名给诊断工具，别名是**值/函数引用**：测试若去打桩
`listing` 而代码走别名，打桩会静默失效。其三，榜单页的 `_WAIT_*` 常量现在跨两个文件
（`UI/SORT/NEXT` 在 `listing.py`，`SCROLL/BACK/LAUNCH/POPUP` 在 `browser_pw.py`），
因为它们各自服务不同的等待对象。

别名那一条当天就咬了一次：测试改成打桩 `listing.intervention_kind`，而采集路径还在用别名，
于是真的走进了「人工介入等待」——`guard.wait_for_resolution` 的循环在开发机上把警报音响了
五分钟。除了把内部调用一律改成 `listing.*`，`bestseller_monitor/sound.py` 的默认也从「响」
改成「不响」：只有采集入口 `run.py` 按 `config.toml` 的 `alarm_on_intervention` 显式打开，
测试与一次性工具什么都不配。代价是一次性工具若想要警报需要自己 `configure(True)`
（`diag_verify_state.py` / `diag_card_urls.py` 本来就显式关掉），换来的是「测试跑出声音」在
结构上不可能再发生。

被否掉的方向：

- **只收 `advance`、不动 `prepare`**：改动更小，但放过了那三处已经漂移的差异，其中一条是
  「补抓跑空还记成完成」。
- **把「遍历某店的 N 页」整个收成一个 module**（每页内容走回调）：两份每页内部做的事本就不同
  （主页逐卡读并按名暂缓，补抓只挑同名卡），为不存在的需求加抽象。
- **新建 `listing_walk.py`**：不解决「榜单页有两个家」，只是把第三个家搬过来。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「榜单批次」「补采」；来源是
`docs/reviews/architecture-review-2026-09-13.html` 候选 01，规格与工单在
`.scratch/listing-module/spec.md`。
