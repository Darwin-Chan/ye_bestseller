# 0010. 采集驱动只留一条：点击式连接接管

- 状态：已接受
- 日期：2026-09-12

## 背景

`pipeline.py` 同时保留三条驱动路径，分发只看 `cfg.driver`：`pw_cdp` 走点击式连接接管，`drission` 走 DrissionPage，其余一切值落到 Playwright 直连（`bestseller_monitor/pipeline.py` 的分发处）。IS-23 记的就是这个组合。

**只有点击式那条在真机上活着。** `config/config.toml` 写着 `driver = "pw_cdp"`；`run.log` 里 `开始抓取店铺`（两条旧路径的榜单入口日志）的末次出现是 2026-09-04 18:50，`等待店铺商品加载`（DrissionPage 独有）只出现过 1 次，而 `点击式抓取店铺` 有 110 次；2026-09-12 的第 28–32 轮全部走点击路径。

**另外两条不是「慢一点的备用」，是结构上抓不到榜单。** `listing.crawl_shop_listing` 与 `browser_dp.crawl_shop_listing` 都用 `a[href*='/offer/']` 枚举商品，而 1688 店铺列表是图片卡片、没有 href——这是 2026-09-04 的实测结论，也是 `browser_pw.crawl_store_by_click` 被写出来的原因（见 `docs/实现进展.md`）。它们跑到榜单阶段只会得到 `ListingLoadFailed`。

**它们和今天的运行契约也不兼容。** 过程页的「当前处理中的店 / 每店耗时 / deny 数」全部读 `event_log`（`gui.py` 的过程页取数），失败率里那块「点击未得商品」同样只来自事件表（`Database.click_card_failures()`），而 `event_log` 只有 `_run_pwcdp_round` 通过 `record_params` + `event_logger` 写。走旧驱动，界面会显示 deny=0、无耗时、无当前店，失败率被低估。

**误配只差一步。** `Config.driver` 的缺省值是 `drission`（`config.py`），`config/config.toml` 的注释写着「drission（推荐，可过 1688 登录）/ playwright」，而任何拼错的值都被 `else` 静默送进 Playwright 直连。

**自动化覆盖也不在它们身上。** 两条旧路径的列表函数与阶段函数有 fixture 测试（`tests/test_pagination.py`、`tests/test_listing_resume.py`、`tests/test_p1.py`），真正零执行的是 `_run_pw_round` / `_run_dp_round` 的装配体，以及「驱动分发」本身——所有测试都写死 `driver="pw_cdp"`。此外 `browser_pw.crawl_store_listing`（href + XHR 拦截兜底版）没有任何调用方，是第四份死代码。

## 决策

- **只留一条驱动路径**：删除 `_run_pw_round`、`_run_dp_round` 与 `browser_dp.py` 整套、`listing.crawl_shop_listing`、`detail.capture_detail_payload`、`pipeline` 顶部的 `sync_playwright` import、`pagination` 的 drission 版列表身份与等待、`guard` 的「兼容旧接口」段、`browser_pw.crawl_store_listing`。榜单阶段的失败异常与原始页存档留在 `listing.py`，模块文档改写为它现在真正做的事。
- **`driver` 键保留作过渡闸**：只接受 `pw_cdp`；写了别的值（`drission`、`playwright`、拼错的值）一律**启动即报错**并说明该驱动已下线。缺键按 `pw_cpd` 处理——缺键不会说谎，不给它加必填门槛。校验放在配置加载处，与其它非法配置值同一处收口；分发点因此不再有 `else` 分支。
- **配置与依赖一起收尾**：删 `headless`、`slow_mo_ms`、`browser_channel`、`profile_dir`、`use_system_profile` 五个只被旧路径读的键；目录名统一到 `user_data_path`（`ensure_dirs` 建它，README 改指它）。`DrissionPage` 从 `requirements.txt` 删除，测试用的 CSS 引擎 `lxml` + `cssselect` 改为显式声明的测试依赖——商品卡选择器那张验证网要留着。
- **唯一入口补一个装配级测试**：用假的 `open_session` / `close_session` 覆盖 `_run_pwcdp_round`——打开会话、榜单阶段拿到 `emit` 与 `deny_tracker`、榜单阶段异常时仍然收尾。删路径之后它是整轮采集的唯一入口，而今天的测试全部把它 patch 掉、函数体一行没跑过。
- **口径同步**：README 的「普通进程启动 + DrissionPage 接管」改为 Playwright 经调试端口接管（描述的是真实实现）；PRD 里 `slow_mo_ms` 的说明删除；CONTEXT.md 补「驱动」词条与一条对应规则。

## 结果

「推荐驱动」从文档承诺变成结构事实：代码里只有一条路径，配置里只有一个合法值，界面看到的事件、失败率与耗时口径不再有第二种来源。同时删掉一整套只被测试调用的旧入口、一个没人调用的列表实现，以及五把没人读的配置开关。

代价有四处。其一，**失去「换一个驱动再试」这个选项**——今天那个选项本来也抓不到榜单，真要再有第二条路得按点击式重写一份，等于多一条要维护的实现。其二，过渡期的 `driver` 键是一个只有单合法值的开关，看着冗余；留着它是为了拦住「老配置写着 drission、实际跑着 pw_cdp」这种静默偏离。其三，删掉 `headless` 之后「必须用有头浏览器」不再由配置表达，改由「用普通进程启动浏览器」这件事结构性保证。其四，旧路径的测试覆盖不可回收，删掉的测试与删掉的代码等量。

被否掉的方向：

- **留 DrissionPage 作降级备用**：它今天的榜单枚举就抓不到商品，又不写事件表；要能被当作备用，得先按点击式补一套枚举、埋点与 deny 追踪，成本与重写相同，却换来第二份要维护的语义。留着更像救生艇的诱饵。
- **三条都留，只补文档与测试**：给一条已经不通的路买保险，且把「哪条是唯一推荐」永久留在文字里而不是结构里。
- **删掉 `driver` 键，代码写死唯一驱动**：老配置里的 `driver = "drission"` 会变成「写了但不生效」，正是 IS-37 与 ADR-0007 那一类静默偏离。报错比沉默好。
- **缺键即报错**：把这个键变成必填只增加摩擦，拦不住任何真实的错误配置——错的配置是「写了别的值」，不是「什么都没写」。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「驱动」；实现见 `bestseller_monitor/pipeline.py`、`bestseller_monitor/browser_pw.py`、`bestseller_monitor/config.py`；工单为 IS-23。
