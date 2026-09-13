# 0013. 详情观测收成一个 module：接缝从 fetch() 挪到「读一次详情观测」

- 状态：已接受
- 日期：2026-09-13

## 背景

同一组详情规则写了两份，接缝切在了拿不到商品编号的那一侧。

`pipeline._capture_offer_detail` 自称「同日去重与补采 module」，但它的 adapter 约定是
「给我 URL、取一次详情、返回 payload」。点击式列表要先点开卡片才知道是哪个商品，还要自己管
弹窗、卡片→编号的绑定与遍历内的 `seen` 去重，于是 `browser_pw._ingest_detail` 把同一组规则
又写了一遍，两处已经漂移：

- 同日跳过在补采是「进详情前查，跳过不占机会」，在点击是「先占机会，跳过时退还」；
- 尝试额度在补采「用尽就不进详情」，点击路径根本不查；
- 失败行的字段一份填全、一份只填一半；失败文案两份逐字相同，但点击路径另有两条补采没有的
  分支（「详情页读取失败」「详情页解析异常」）；
- 重试一份在详情里原地重试并退避，另一份不重试（失败交给随后的补采）；
- 提交后的停止判定一份走 `ensure_workable`（会认领暂停），一份只看 `stops_work`。

## 决策

- **module 落在 `detail.py`**（它的名字就是「商品详情页抓取」，失败异常与原始页存档本来就在
  那儿），interface 只有一道：

  ```python
  capture_observation(db, cfg, human, round_id, target, read, *,
                      attempts=None, on_attempt_failed=None) -> CaptureResult
  ```

  `target: DetailTarget`（哪个店铺、哪条榜单行、机会挂在哪个标识上、**商品编号**、是否重复），
  `read` 是 adapter（打开页面或读弹窗内容，返回 `Observation`；页面/弹窗由开它的那一侧关），
  `CaptureResult(outcome, offer_id, note, sku_count)`。
- **编号总是已知**：点击路径从弹窗地址读出来（卡片已经点开，但还没读内容），补采本来就知道。
  于是两条路都是「先算账、再读页面」——同日采过就不读，额度用尽（`attempts=None` 时）就不读。
  这一点比初版设计更简单：`read` 负责「读」，点开卡片不是读的一部分。
- **机会的账归 module**：申请、绑定（卡片位置 → 商品编号）、同日跳过时退还都在这里。
  点击路径仍由遍历在打开卡片前先占一次（预算就是用来限制详情访问的），module 的申请是幂等的。
- **重试参数化**：`attempts=None` = 试到本轮额度上限（逐店补采）；`attempts=1` = 读一次就够
  （点击路径：这张卡已经点开了，失败交给随后的补采接手）。差异写下来，不埋在两份代码里。
- **module 不发事件、只返回结果**：`event_log` 逐行不变，事件由两条调用方按结果记
  （`analyze_click.py` / `analyze_delay.py` 按事件名统计，让补采路径突然发 `click_*` 会悄悄改掉
  它们的口径）。失败文案的用词收在 `Observation` 的四个构造子上
  （读不到页 / 解析失败 / 解析崩了 / 访问异常），adapter 只挑一个，不各写一份前缀。
- **失败记录与文案统一**：一行失败快照、字段填全、原因由 module 拼（有原始页时存档并附路径）。
  主图字段异常从前在补采路径会抛到外层记成裸异常，现在与点击路径一样记成「详情页解析异常」
  （取主图因此留在 adapter 里：它是「这次读到了什么」的一部分）。

## 结果

`_capture_offer_detail` 与 `_ingest_detail` 里的第二份规则没了：两条路共用一份同日去重、
额度、机会账本、失败记录与提交；`_ingest_detail` 只剩「读弹窗 → 按结果记事件 → 关弹窗」。
新增 `tests/test_detail.py` 11 条走 module 的 interface（脚本化 `read` + 真实内存库），
`tests/test_round_stop.py` 里原本冲着 `pipeline._capture_offer_detail` 写的四条改走同一个
interface。全套 `python -m unittest discover`：329 条通过、1 条跳过。

代价与行为变化有四处，都是小的：

1. 失败行在补采路径开始填 `shop_url` / `shop_name` / `product_url` / `product_name`
   （那本来就该填，缺了还会让 `mark_failure` 在没有榜单行时抛错）。
2. 补采路径「主图字段异常」的失败文案从裸异常变成「详情页解析异常：…；原始页面：…」。
3. 点击路径在**提交那一刻正好跨过截止线**时，不再补发 `click_ok`（快照已提交、弹窗照常关掉，
   采集进程随即按跨天收尾）。事件少一条，数据不少一行。
4. 点击路径的详情入口现在自己先问一次轮次：过期轮次在读页面之前就被拦下。生产路径本来就是
   `_capture_card` 在点开卡片前拦的，行为不变；但直接调这个入口的人（例如用例）会看到更早的
   拒绝，而不是「读完、提交、再宣布跨天」。

另外，点击路径的**事件内容与行序**刻意保持与改前一致，并新增三条用例把它钉住
（成功、解析失败、读不到页三种序列）。这是评审逮到的：第一版把关窗放进 `finally`、
让失败回调无条件补一条 `detail_parse`，于是成功路径的 `popup_close` 跑到 `click_ok` 之前、
读不到页时多了一条事件——而承诺是「逐行不变」，当时却没有任何用例守着它。

评审的其余收口：`Observation.offer_id` 这个没人读的字段删掉；`slot_key == offer_id`
这种用字符串相等表达语义的写法换成 `dedupe.claim_slot()` 一个统一入口；
`CaptureResult.note`（没有生产调用方）删掉，`Outcome.EXHAUSTED` 改名为 `ATTEMPTS_EXHAUSTED`
以免与术语「详情预算耗尽」混同；补采 adapter 的停止判定守卫补了一条用例。

被否掉的方向：

- **只把规则抽成纯函数**、两条流程仍各写一遍：重复摊薄了，但「改一处漏一处」还在。
- **只统一同日跳过这一条**：最小，但放过了额度、失败记录与提交后的判定。
- **让 module 顺带发事件**：会把点击路径的事件名灌进补采路径，改掉既有工具的统计口径。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「详情观测」「补采」「详情机会」「跳过」；来源是
`docs/reviews/architecture-review-2026-09-13.html` 候选 02，规格在
`.scratch/detail-observation/spec.md`。
