# 八项测试失败诊断（2026-10-17）

状态：定向诊断、修复与分段全量验证完成；全量计数对账一致但有两项独立复跑通过的偶发失败；未提交、未推送。

## 范围与来源

主仓 `F:\AI\projects\bestseller`，开始时 HEAD 为
`4e56f770dfa2a5605f7737cc0578c293f2b95134`，工作区干净。
交接来自 `C:\Users\Darwin\AppData\Local\Temp\bestseller-eight-test-failures-handoff-20261017.md`。
八项清单以旧证据 `selective-retry.json` 的 `baseline.failed_ids` 为准，未将单独通过的 SKU ranking 算入。
旧版本同样失败只用于排除交换台补图改动的新回归，不作为根因或结案依据。

本次证据根目录（下文所有日志名相对此目录）：
`F:\AI\projects\bestseller\.scratch\logs\tickets\eight-test-failures\01\20261017-diagnosis`。
隔离副本：
`F:\AI\projects\bestseller\.scratch\sandboxes\eight-test-failures\01\20261017-diagnosis\bestseller`。
短 TEMP/TMP：`F:\AI\projects\bestseller\.scratch\t8`。
副本来源及配置见 `source.txt`；仅使用已审查的示例配置，不使用真实数据库或登录态。
未提交改动点名同步到副本，四个文件的 SHA-256 见 `tested-files.json`。

## 红循环、最小化与假设

复现命令（从主仓运行）：

```powershell
python -X utf8 .scratch\logs\tickets\eight-test-failures\01\20261017-diagnosis\loop.py red-elevated-1 --isolated
python -X utf8 .scratch\logs\tickets\eight-test-failures\01\20261017-diagnosis\loop.py red-combined-1
```

日志以独占新建方式写入；重跑必须换 label，不能覆盖已有证据。
独立进程中 4 项未通过（2、3、5、8），其余 4 项暂时通过；同一进程组合中
6 failures + 1 error。第 2 项在组合中通过，而独立运行失败，显示保存延迟有波动，
不能把偶尔通过当成无问题。两组并集覆盖全部八项实际失败。
原保存失败日志的 WinError 10053 出现在断言超时、浏览器清理关闭连接之后的服务端回写，
不能倒因为果地视为网络根因；独立的 SQLite 耗时与单变量修复已直接验证持久化边界。

`min_save.py` 将保存问题缩至真实 HTTP/浏览器入口、一个商品、一个分组、无模型：
开始分析→确认当前分组→保存并查看→断言结果可见。
`min-save-valid-1.log` 保留了此最小流程的实际失败。
最初直接调用服务后导航的探针未携带分析号，不是同一症状，已否决，未用于推导。
沙箱内首次浏览器启动阻塞也不计为产品失败；后续浏览器测试经正规提权运行。

验证前已展示的排序假设与结果：

| 假设及预测 | 单变量验证及结果 |
| --- | --- |
| 保存后端某个额外边界阻塞；若成立，延迟应发生在 `_save` 内 | 边界计时定位到首次 SQLite 建表；保存无模型/外网调用仍复现 |
| SQLite 锁或写入成本；若成立，提前初始化同一路径的库应明显降低保存耗时 | 无外部持锁连接时逐表 DDL 已累计数秒；预建库及批量事务分别消除这部分成本，支持持久化成本而非锁竞争 |
| 前端异步覆盖；若成立，后端应先完成而 DOM 仍停留 | 原后端保存约 4.64 秒且波动越过原等待上限；只改后端事务即恢复原 DOM 断言，不支持此假设 |
| 执行顺序/共享状态污染；若成立，干净独立进程应不失败 | 第 2、5 项在全新独立进程仍失败，最小独立流程也失败；顺序影响负载与波动，非必需触发条件 |
| 两项标题测试与契约不一致；若成立，真实产品名与切换状态应正确，而源码字符串/模拟桥状态有差异 | DOM 直接文本等于精确产品名；模拟桥返回旧 label，修正其唯一状态值后原断言立即满足；真实 API 往返切换三页均正确 |

`probe-save-1.log` 的 cProfile 受浏览器 greenlet/selector 统计混合影响，未用于定量结论。
采用 `probe_sqlite.py` 单边界计时结果：`probe-sqlite-baseline.log` 首次保存
4.6393 秒、十次建表各自自动提交；`probe-sqlite-precreated.log` 同一流程仅提前建库后
保存 0.6026 秒；`probe-sqlite-batched.log` 仅合并建库事务后保存 1.3686 秒。
这是本机样本，不是对所有磁盘的延迟保证；确定性回归检查事务边界，避免以机器速度作为唯一判据。

## 已验证根因与最小修复

### 保存：产品性能缺陷，由磁盘持久化延迟触发（六项）

Python sqlite3 的连接上下文并不会自动为 DDL 开启事务。`DraftStore._connect`
连续创建十张表，首次保存因此逐表自动提交，累积持久化等待；商品和 SKU 均使用此路径。
SKU `write_sku` 内按需创建草稿表还有一次独立的 DDL 提交。

修复 `bestseller_monitor/analysis_store.py`：开库建表和旧库补列由一个显式事务统一拥有；
三个补列辅助方法不再提前提交；初始化异常时关闭连接。SKU 草稿表的按需创建与该次保存
一起提交。没有更换 journal/synchronous 策略，没有更改数据格式、等待时间或 UI 断言。

新增 `test_first_save_batches_schema_durability_for_product_and_sku`，
在真实服务首次保存边界观察真实 SQLite DDL 是否处于事务，并重启读回完整草稿。
`schema-regression-red.log` 在原实现的两种粒度上均红（各十条自动提交 DDL）；
初次修复商品路径后测试继续抓出 SKU 的剩余建表提交；补齐后
`schema-regression-green-2.log` 通过。没有依赖 sleep 或墙钟阈值。

### 产品显示名：测试断言过时（一项）

页面在 h1 内新增了 `collectionMode` 状态 span，产品名本身、窗口标题、title 均未变。
旧测试要求完整 `<h1>产品名</h1>` 字符串，排斥了合法的状态节点。
`probe-titles.log` 验证真实 DOM 的 h1 直接文本仍为精确产品名。

修复 `tests/test_gui.py` 的语义断言：HTMLParser 精确核对 title 与 h1 直接文本等于
`gui.WINDOW_TITLE`，并核对唯一状态节点在 h1 中、id 为 `collectionMode`、role 为 `status`。
继续保留精确产品名常量断言，没有仅改成模糊子串匹配。

### 试采标题：测试夹具状态不一致（一项）

`MOCK_API.set_local_trial` 只更新 `window.__localTrial`，返回正确的新标签，却未更新
`window.__collectionLabel`；页面按真实流程随后 `get_start`，读到旧标签。
`probe-titles.log` 显示 bool 已 false、按钮已“进入本机试采”，但查询和渲染 label 仍为旧值；
只修正夹具 label 并重新查询后即为“正式采集”。

修复 `tests/test_ui_live.py`：模拟桥切换时同步更新共享 label，返回值复用同一状态。
原“正式采集”精确断言保留，新增反向切回试采及两次查询状态与标签一致性断言。
`probe-real-trial.log` 另通过真实 Python API 连续 True/False 两轮，三页模式与标签均一致，
排除真实产品状态切换缺陷。

## 逐项对照

所有完整 ID 保留如下；“独立证据”中的序号对应 `green-isolated-1-NN.log`。

| # | 完整失败用例 | 已验证根因 / 修复 | 独立复验 |
| --- | --- | --- | --- |
| 1 | `test_analysis.AnalysisBrowserTests.test_save_and_view_needs_no_pending_and_failed_saves_can_retry` | 首次建库多次持久化；合并 schema 事务 | 通过，01 |
| 2 | `test_analysis_stage_legend.StageLegendTests.test_the_legend_tracks_the_three_screens_and_resets_on_the_way_back` | 首次建库迟到，结果页未及时出现；合并 schema 事务 | 通过，02 |
| 3 | `test_gui.GuiSingleInstanceTests.test_the_window_and_its_page_carry_the_product_display_name` | 过时完整 h1 字符串；改为精确语义断言 | 通过，03 |
| 4 | `test_sku_drafts.SkuDraftBrowserTests.test_real_entry_displays_sku_evidence_and_save_reload_keeps_review` | 首次建库多次持久化；批量 schema 与 SKU 表事务 | 通过，04 |
| 5 | `test_sku_matching.SkuMatchingBrowserTests.test_real_analysis_addition_reconfirmation_save_and_reload` | 同上；保存状态文字迟到 | 通过，05 |
| 6 | `test_sku_recovery.SkuRecoveryBrowserTests.test_page_missing_picture_only_can_recheck_after_evidence_restored` | 同上；保存状态文字迟到 | 通过，06 |
| 7 | `test_sku_switch_note.SwitchNoteBrowserTests.test_sku_rows_drop_the_segment_label_and_switchless_products_add_no_note` | 同上；结果页迟到 | 通过，07 |
| 8 | `test_ui_live.UiLiveErrorFeedbackTests.test_local_trial_mode_keeps_its_status_in_the_title` | 模拟桥保留旧 label；统一夹具状态并验证双向切换 | 通过，08 |

`green-isolated-1.json` 汇总八项独立复验及日志；`green-combined-1.log` 八项组合通过。
`focused-modules-1.log` 五模块 82 项全部通过（analysis_drafts、matching_signature、
analysis_conflicts、ui_live、local_trial_gui），含旧库补列、保存失败回滚及冲突裁决。
`green-reverse-1.log` 倒序八项也全部通过，排除修复后依赖顺序的假阳性。
`probe-sqlite-fixed.log` 在实际修复版本重新驱动原最小浏览器流程，保存 1.693 秒、原断言通过。
分段全量已完成：2146/2146 项计数对账一致；6/7 段通过。
其余段 1858 项中有 2 项失败，均为 `test_matching` / `test_matching_control`，与本次改动无调用关系。
这两项随后分别独立进程、组合顺序、以及两个完整模块复跑均通过，证据见 `post-full-1-01.log`、
`post-full-1-02.log`、`post-full-combined-1.log`、`post-full-modules-1.log`。
因此全量运行器结论按事实保留为 FAILED，不能宣称全量全绿；原八项及相关回归均已通过。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`。
- 隔离副本：上文 `20261017-diagnosis\bestseller`，来源 HEAD 与四个补入文件见 `tested-files.json`。
- 永久保留：本报告与主仓上述证据目录的红/绿日志、探针、文件清单、最终全量 summary 与逐段日志；不随副本清理。`.scratch` 忽略文件未随 Git 备份。
- 可删除候选：无（本轮未申请删除）。
- 暂留待确认：上述隔离副本和短 TEMP/TMP，保留供结果复核；复核及材料保全后另获授权再清理。
- 实际清理：未执行；旧交接副本和日志均保留。
- 提交与推送：未执行。

