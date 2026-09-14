# 0023. 详情访问用一次性 capability 交回验证后的当前观测

- 状态：已接受
- 日期：2026-09-14

## 背景

ADR-0018 用 `detail.observe_page(read_html, product_url)` 统一了「读 HTML → 解析 → 取主图 →
失败分类」，ADR-0020 又让点击与逐店补采在打开详情后共用 deny 账目和人工介入 guard。两条决定
分别正确，但组合后的 interface 仍把承重顺序泄漏给调用方。

补采当前先由 `browser_pw.open_detail()` 导航、等待可读并读取 HTML `t0`，随后
`pipeline._capture_one_pw()` 才调用 `guard.ready_detail_page()`。如果 `t0` 是验证页，人工解决后
页面成为商品页 `t1`，调用方仍用 `detail.observe_page(lambda: html)` 解析缓存的 `t0`。验证明明
已经完成，本次观测却失败。导航和首次内容读取还在 `capture_observation()` 的读取 adapter 外，
普通导航异常因此绕开 `Observation.READ` 与既有尝试重试，落入逐商品的裸异常兜底。

点击路径不缓存 guard 前的 HTML，但 guard 成功后没有等待现有的 `detail.readable()` 条件。根因
不是少一次重读，而是「取得页面 → guard → 等可读 → 读取当前内容」没有成为一个 module 的
interface；两条调用方必须各自拼对顺序。

## 决策

### 一个共享的详情访问 seam

新增 `bestseller_monitor/detail_visit.py`。两条路径只提供取得页面的 adapter，访问 module 负责
从取得页面到翻译当前观测的完整顺序：

```text
acquire
  → 初次 guard（deny / 人工介入）
  → ReadyDetailVisit
  → 调用方可先登记点击得到的商品身份
  → observe：等可读
      → 晚到 deny / 人工介入则重新进入 guard
      → 人工解决后重置 10 秒可读窗口
  → 读取当前 HTML
  → detail.observe_html
```

开始访问的 interface 为：

```python
begin_detail_visit(
    acquire, cfg, *, emit=None, deny_tracker=None, shop_key=None, reraise=()
) -> NotOpenedVisit | ReadFailedVisit | DeniedVisit | ReadyDetailVisit
```

`acquire()` 返回 `OpenedDetail(page, punished=False)`；只有点击 adapter 可以返回 `None`。生产 adapter
分别是 `browser_pw.navigate_detail(...) -> OpenedDetail` 与
`PlaywrightCard.acquire() -> OpenedDetail | None`。前者只导航，后者只打开卡片并保留响应流 punish
信号；它们都不等待可读、不翻译观测。

`ReadyDetailVisit` 是一次性 capability。`observe(product_url)` 至多调用一次，也允许不调用：点击
路径必须先从弹窗地址得到商品编号，之后 `capture_observation()` 仍可能因同日去重、遍历内重复或
停止而短路。第二次调用属于程序错误，不能降级成普通读取失败。

### 等待的是 guard 后的当前页面

`ReadyDetailVisit.observe()` 用既有 `detail.readable()` 轮询当前页面，上限 10 秒。页面可读或
超时后才读取交给 parser 的当前 HTML，不保存 guard 前的副本。

等待期间出现 deny 时重新进入 guard，让现有账目与阈值先发生，再返回 `DeniedVisit`；补采把它
变成 `Observation.denied()` 并消耗一次尝试，点击把它送回既有的三段 deny 处理。等待期间出现
登录墙或滑块也重新进入 guard；人工解决后重新给页面一个完整的 10 秒可读窗口，而不是沿用验证
消耗掉的剩余时间。

已确认 deny 后读取原始 HTML 若又失败，结果仍是 `DeniedVisit(raw_html="")`。deny 是已经观察到的
事实，附带 HTML 只是校准材料，不能因材料读取失败而改写事实。

### 结果显式，后果仍归调用方

访问开始显式返回四类结果：

- `NotOpenedVisit`：点击没有打开弹窗；补采不产生。
- `ReadFailedVisit(error, observation)`：取得页面时的普通浏览器 I/O；保留原异常，同时提供
  `Observation.READ`。已知商品身份的补采消费 observation 进入尝试重试；身份未知的点击原样重抛
  error。
- `DeniedVisit(raw_html)`：初次或晚到 deny，guard 已完成记账和阈值判断。
- `ReadyDetailVisit`：已经通过初次 guard、可按需读取一次的 capability。

`ReadyDetailVisit.observe()` 的普通内容读取异常直接成为 `Observation.READ`；晚到 deny 则返回
`DeniedVisit`。调用方传 `reraise=STOP_WITH_OUTCOME`，因此 acquisition、guard、readiness/content
中任何已登记停止异常都以同一对象原样穿透，不写商品失败行。只捕获这些页面 I/O：未登记的 guard
异常、capability 误用和其它程序错误继续暴露；`_capture_pending_offers()` 的意外异常兜底保留，
但正常浏览器 I/O 不再走到那里。

### 翻译只接收 HTML

`detail.observe_page(read_html, ...)` 改为 `detail.observe_html(html, ...)`。`detail` 继续统一解析、
主图提取及 READ/PARSE 观测语义；页面何时读取由 `detail_visit` 保证。实现时删除
`browser_pw.open_detail()`、`browser_pw.wait_until_detail_readable()`、`detail.observe_page()` 与
`PlaywrightCard.read()`，不留兼容 wrapper。

## 与既有 ADR 的关系

- **显式取代 [ADR-0018](0018-page-to-observation-in-detail.md) 的
  `observe_page(read_html, ...)` interface**：保留其「HTML 只翻译一次、解析与主图失败统一分类」
  决策，但读取时机移入详情访问 seam，`detail` 改接已经取得的 HTML。
- **保留 [ADR-0013](0013-detail-observation-module.md) 的职责归属**：同日去重、遍历内去重、详情
  机会、尝试、失败持久化、成功提交和提交后停止判定仍在 `detail.capture_observation()`；事件仍由
  调用方发。成功提交继续清除同轮失败快照，但已发生的失败事件保留。
- **保留 [ADR-0020](0020-detail-visit-shared-across-paths.md) 的 guard 语义和路径后果**：deny 判据、
  账目、阈值与人工介入仍归 `guard`；点击保留三段退避/跳过，补采 deny 保留一次一尝试，店铺级与
  整轮级后果不变。本条只把调用方显式拼装的访问顺序收进共享 seam，并补上晚到 deny/人工介入。

页面/弹窗仍由打开它的调用方关闭；guard 或停止异常不引入新的清理动作。`detail_nav`、
`detail_parse`、`click_*`、`popup_close` 的事件名字、内容、顺序和归属均不变。

## 结果

- 验证页 `t0` 变成商品页 `t1` 后，观测只解析 `t1`；补采不再把已解决的验证页记成商品失败。
- 普通导航、等待和取内容错误进入 `Observation.READ`，已知商品的补采可以使用原有尝试额度重试。
- 点击与补采共享同一份访问次序，并都等待页面满足同一个可读判据；删除任何一条调用方拼装逻辑，
  复杂度都会重新出现在两条路径，新的 module 因而通过 deletion test。
- 代价是 interface 多了几个显式结果和一次性状态，但它们表达的是调用方确实需要区分的事实：
  未开页、取得失败、deny，以及已就绪但可能因身份/去重而不读的访问。

测试从旧 helper 的逐层打桩移到共享 interface：用脚本化页面 adapter + 真实临时 SQLite 覆盖
验证页变商品页、导航失败后重试、点击慢渲染、两条 deny 后果、全部停止异常原样穿透、deny 原始
HTML 读取失败、capability 二次使用、晚到 deny，以及晚到人工介入后重置可读窗口。纯 parser、
guard 谓词和调用方事件测试保留。完整验收清单见
[规格](../../.scratch/detail-visit-observation/spec.md)。

## 被否掉的方向

- **只在补采 guard 后再读一次 HTML**：能补当前漏洞，却让导航错误继续绕开重试，点击路径也继续
  自己拼等待；访问顺序仍有两个所有者。
- **只抽一个导航函数**：移动代码而不隐藏顺序知识，module 仍然浅。
- **在 `detail.capture_observation()` 里直接操作 Playwright 页面**：会混入浏览器、deny 和页面
  生命周期，破坏它现在对机会、尝试与持久化规则的 locality。
- **让访问 module 统一 deny 后果、事件或关页**：两条路径的合法后果不同，会改动既有统计口径与
  页面所有权。
- **保留旧 helper 做兼容层**：产生两套可用顺序，测试仍可绕过新 seam，无法兑现「interface 就是
  测试面」。

## 范围外

补采的响应流 punish 识别仍是 ADR-0020 的独立缺口，本条不增加监听器；也不改变停止 taxonomy、
deny 阈值与退避、详情预算、尝试次数、事件格式或数据库模型。沿用 [CONTEXT.md](../../CONTEXT.md)
已有的「详情访问」「详情观测」「补采」「详情机会」词汇，不修改词汇表。
