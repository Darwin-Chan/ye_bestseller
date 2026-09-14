# 0022. 测试的采集配置替身收成 `helpers.crawler_cfg()`

- 状态：已接受
- 日期：2026-09-14

## 背景

「一个采集配置包含什么」在测试里抄了九份：`test_click_listing` / `test_listing` /
`test_detail` / `test_listing_resume` / `test_p1` / `test_round_stop` 各一份模块级 `_cfg`
（外加 `test_round_wiring` 里两份同名方法、`test_gui.gui_api` 与 `test_views` 各一份）。
每份只写自己关心的那几个键，重叠却不完整：

| 文件 | 键数 |
| --- | --- |
| `test_detail._cfg` | 3 |
| `test_click_listing._cfg` | 13 |
| `test_listing_resume._cfg` / `test_round_stop._cfg` | 17 |
| `test_listing._cfg` / `test_p1._cfg` | 21–22 |

改一个键（例如候选 04 新增的 deny 项、候选 05 的 `raw_page_dir`）要翻好几处；而「一个采集配置
到底有哪些键」这个问题，任何一份都答不全——`record_params()` 会把 `PARAMS_KEYS` 逐键取出来，
只有 `test_round_wiring` 那一份凑齐了。

## 决策

- **`tests/helpers.crawler_cfg(**overrides)`**：全套默认值只在这里写一遍，用例只覆盖自己关心
  的键。默认值都取「不拖慢测试」的那一档（超时 1 毫秒、各种延迟 0、deny 阈值按 `config.toml`
  的量级）；`raw_page_dir` 给一个共享临时目录（失败路径真会往里写原始页，用例要断言就覆盖成
  自己的 tmp）；`db_file` / `shop_csv` / `driver` / `ensure_dirs` 给中性默认，要用的用例自己传。
- **六个模块级 `_cfg` 与 `test_round_wiring` 的两份方法都换成它**；`test_gui.gui_api` 与
  `test_views` 那两处单键 cfg 也换成它（多出来的键没人读，没有代价）。
- **边界写在这里**：真读配置文件的那类用例（`test_config` / `test_run_cli`）照样走
  `Config.from_file`——它们测的是解析，不是替身；`tools/bench_refresh.py` 是**工具**、不依赖
  `tests/`，它的两键 cfg 是 `Api` 的入参，不是采集配置的替身。

## 结果

- 九份 → 一处：新增/改一个键只动 `helpers.crawler_cfg`（本次净删 69 行测试代码）。
- 新增 `tests/test_helpers.py` 两条把替身自己钉住：**它要覆盖 `db.PARAMS_KEYS` 的每一个键**
  （缺一个，跑一整轮的用例就会在 `getattr` 上炸——这正是 `test_round_wiring` 那份手工凑齐的
  原因），以及覆盖项优先。
- 行为零变化：各文件原来靠默认值的那几个键逐一对齐（`test_click_listing` 的
  `max_pages_per_shop=2`、`test_detail` 的 `max_detail_opportunities_per_round=1000` 等），
  少数原本不同的默认值由调用点显式覆盖（`test_round_wiring` 的 `max_pages_per_shop=3`）。
  全套 391 条通过、1 条跳过。

相关词汇见 [CONTEXT.md](../../CONTEXT.md)（本条不动词汇表）；来源是
`docs/reviews/architecture-review-2026-09-13-r2.html` 候选 06，规格在
`.scratch/crawler-cfg/spec.md`。
