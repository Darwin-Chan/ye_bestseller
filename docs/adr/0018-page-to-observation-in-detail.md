# 0018. 「页面 → 观测」的翻译收进 `detail.observe_page()`

- 状态：已接受
- 日期：2026-09-13

## 背景

ADR-0013 把「一次详情观测的规则」收进了 `detail`，并把失败文案放进 `Observation` 的四个
构造子（读不到页 / 解析失败 / 解析崩了 / 访问异常），**由 adapter 自己挑一个**。这留下了一
条没主人的判据：「读到什么算失败、留不留原始页」，于是两条路径各写一遍，当场走歪：

| | 点击式列表（`click_listing.DetailPage.read`） | 逐店补采（`pipeline._capture_one_pw.observe`） |
| --- | --- | --- |
| 读不到内容 | `read_failed`（READ） | 落进下面那条兜底 |
| `DetailParseFailed` | `parse_failed`（PARSE，带原始页） | `parse_failed`（PARSE，带原始页） |
| 其它异常（主图字段异常等） | `parse_crashed`（PARSE，**带原始页**，有用例钉住） | `access_failed`（ACCESS，**丢原始页**，无用例） |

同一个失败，两条路两种待遇——而 ADR-0013 的承诺是「两条路共用同一份失败记录与文案」。这是
「为可测性抽出来的 adapter 把判据推到调用处」的活例子：接缝挪到了 `read`，判断却跟着散开。

## 决策

- **一条翻译**：`detail.observe_page(read_html, product_url, *, reraise=()) -> Observation`。
  读 html → 解析 → 取主图 → 按失败种类造 `Observation`。两条路径只交「怎么拿到 html」：
  点击路径给 `self._detail_page.content`，补采路径给
  `lambda: browser_pw.open_detail(page, url, cfg, human, emit=...)`。
- **失败种类收成两种**：
  - 读不到 html → `READ`（「详情页读取失败：…」，没有原始页可留）；
  - 解析失败 / 解析崩了（含取主图时崩）→ `PARSE`，**都留原始页**供校准。

  `FailureKind.ACCESS` 与 `Observation.access_failed()` 因此没有生产者，也从来没有消费者
  （全仓零处读 `kind` 的 ACCESS 分支），一并删掉。「导航失败」与「取内容失败」对调用方是
  同一件事：这次没读到页面。
- **`reraise=` 显式传**：停止判定那一族（`STOP_EXCEPTIONS`，ADR-0009）必须原样上抛，不许被
  当成读取失败。它定义在 `pipeline`，而 `pipeline` import 了 `detail`——放进 `detail` 会成环，
  所以由调用方点名传入，默认空元组（解析段没有这类异常，不用圈）。
- **补采 adapter 只负责把页面读成 html**：`browser_pw.capture_detail`（顺手解析，正是走歪的
  那一半）改成 `browser_pw.open_detail(...) -> str`：导航、等可读、人工介入窗口、发
  `detail_nav`，返回 `page.content()`。`detail_parse`（`sku_count`）由调用方在观测成功后记，
  与点击路径一致。

## 结果

- **补采路径的两处行为变化**：主图字段异常从「访问异常：…」（无原始页）变成
  「详情页解析异常：…；原始页面：<路径>」；导航/取内容失败从「访问异常：…」变成
  「详情页读取失败：…」。点击路径逐字不变（它的两条用例继续钉着）。
  `detail_nav`、`detail_parse`、`detail_fail` 三条事件的**名字、顺序与归属**不变。
- 失败分类只剩一处：`detail.observe_page()`；`click_listing` 不再 import
  `parse_detail_html` / `extract_main_image` / `DetailParseFailed`，`pipeline` 也不再 import
  后两者，「两条路一致」从注释变回结构。
- 用例：`tests/test_detail.py::ObservePageTests` 五条（成功带主图 / 读不到页 / 解析失败留原始页 /
  主图异常算解析崩溃且留原始页 / `reraise` 原样上抛），`tests/test_p1.py` 新增一条
  「补采的主图异常也留原始页」；`test_p1.py` 里七处、`test_round_stop.py` 一处 adapter 桩从
  `capture_detail` 改成 `open_detail`（返回 html），`test_click_listing.py` 两处打桩目标从
  `click_listing.extract_main_image` 改成 `detail.extract_main_image`。全套 356 条通过、1 条跳过。

被否掉的方向：

- **留着 `ACCESS`**：没有生产者也没有消费者，而它正是两条路走歪的那一格；留着只会再分叉。
- **让 `browser_pw` 继续顺手解析**：那正是这次要修的漂移（补采的判定看不到「解析崩了」）。
- **把 `STOP_EXCEPTIONS` 搬进 `detail`**：为一个参数倒过来改依赖方向，代价比显式传参大。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「详情观测」「补采」「跳过」（本条不动词汇表）；
来源是 `docs/reviews/architecture-review-2026-09-13-r2.html` 候选 02，规格在
`.scratch/page-to-observation/spec.md`。
