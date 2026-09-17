# 0026. 点击事件的语义由一处持有

- 状态：已接受并实现
- 日期：2026-09-18

## 背景

「点击式列表里一张商品卡的一次详情尝试」在生产侧留下一条 `click_*` 事件，在消费侧被三个地方
读：本轮失败率（`db.click_card_failures` → `pipeline._finalize_round`）、分析工具
（`tools/analyze_click.py`）、界面的 deny 计数（`views.py`）。这些事件名、卡片身份的两套编码
与结果分类，此前散在四处各写一份，结果是**生产者与消费者已经不相认**，而且两侧同时失真、报表
看起来仍然正常：

- `click_parse_empty` 在 `58c7b55`（2026-09-09，`popup未解析到SKU` 分支重构为
  `DetailParseFailed` 时顺带换名）改成 `click_parse_error`，而 `tools/analyze_click.py` 冻结在
  `5b0f35a`（2026-09-05）从未跟着改。它只认那个不再发出的名字，于是解析失败这一类**从报表里
  整条消失**，「解析空」一列恒为 0；测试因为自己造 `click_parse_empty` 行而照样通过。
- `click_parse_error` 也不在 `click_card_failures` 的事件集合里。这次它**被正确排除**——那张卡
  拿到了商品编号，`detail.capture_observation` 已写了失败快照，经
  `failed_offers = discovered - handled` 计入了失败率，再算一次就是双计——但这个理由没有任何
  一处写明，是静默缺席而不是显式论证。
- 卡片位置有两套编码：事件备注是 `page={P}&idx={I}`（写完由 `db.py` 的正则读回），详情机会账本
  上的标识是 `card:p{P}:i{I}`。两套格式只有各自的两端知道；测试替身又各复刻一份。
- 同一批数据在两个消费点里的归类不同：`click_card_failures` 把 `click_skipped` 当成功，分析工具
  把它从成功率分母里踢出去；界面数 `click_deny` 的**事件条数**（同卡第 1/2/3 次各一条），失败率
  那边去重成**卡片数**。这些差别都成立，但没有一处写明，读代码的人只能靠推。

## 决定

新增 `bestseller_monitor/click_events.py`，持有这条协议的唯一定义：卡片身份的编解码、结果种类的
分类、旧事件名的解释。模块是纯函数式的——不持状态、不做 I/O、不自己发事件。

- **结果种类**是一个 `ClickOutcome` 枚举，取值就是写进 `event_log` 的事件名；有没有拿到商品编号
  做成它的属性 `has_offer_id`，而不是一条独立字段（`click_deny` 即使知道编号也不写 `offer_id`
  列，所以这个属性由种类唯一决定）。跳过另带 `SkipReason`：同日已有库存、本轮重复。
- **卡片身份**是一个 `CardRef` 值对象，`note` 与 `ref` 是它的两个投影。两份字符串编码都保留：
  两边都已经落进持久数据（`event_log.note` 只增不删；`detail_opportunities.identity` 存 `ref`），
  改任一份都要面对存量行，换来的只是少一个字符串格式。要收的是编解码的归属，不是字符串本身。
- **生产者**拿 `(事件名, kwargs)`：`click_events.ok(...)` / `skipped(...)` / `unreadable(...)` /
  `not_opened(...)` / `no_offer(...)` / `denied(...)`，遍历侧 `self.record(spec)` 转给自己的
  `emit`。事件名与备注由协议产出，调用方只决定**什么时候**记录。deny 阶梯（`&n=1/2/3` 与终结
  标记 `skip`/`round_abort`/`shop_skip`）继续留在备注里，同时暴露成分类字段——备注是事实流水，
  人翻库定位时丢掉的细节补不回来。
- **消费者**拿 `classify(event, note) -> ClickEvent | None`，不认识的名字交回 `None`。三个消费点
  保留各自的计数单位与算式，模块只交分类：卡片口径只数没拿到商品编号的那些卡，事件口径照直数
  事件条数。
- **历史兼容**只做读取侧：`click_parse_empty` 解释成与 `click_parse_error` 同一个种类。真实库里
  两个名字都有数据且不重叠（轮次 10–12 有 9 条旧名，轮次 15–33 有 185 条新名）。`event_log` 是
  事实流水，不迁移历史行。

## 预期结果与代价

三个消费点从此认同一套语义，卡片的两种编码与事件名只有一处定义；`analyze_click` 修掉那条结构性
失明（解析失败重新进入成功率分母），`click_card_failures` 里那条静默排除变成写明理由的排除，界面
的 deny 单位从「碰巧」变成「有意」。代价是模块多了一层间接：读遍历侧的事件要跳到 `click_events`
才知道发的是什么；`CardRef.from_note` 的失败路径（位置读不出来）退化成整条备注作键，行为照旧
保留，没有借机改成更严格的键——那会让失败率的数值变化，属于判据变化，须单独论证。

事件名与事件顺序逐字不变，遵守 ADR-0014 / ADR-0020 / ADR-0023 的承诺；两个指标的公式与界面
显示的数字也都不动。完整设计、测试 seam 与残留边界见
[`click-events` 规格](../../.scratch/click-events/spec.md)。

## 与既有 ADR 的关系

- 承接 ADR-0017「本轮计数收成一处」的同一种做法：判据集中，调用方各算各的指标。
- 不改写 ADR-0020 / ADR-0023 关于「失败只翻译一次、deny 共用账目」的决定。
- 本次**不**重开 ADR-0013 的取舍（事件仍由调用方发），模块不替调用方发事件。

## 实现边界

不增加数据库字段或迁移，`event_log` 的写入内容（事件名、备注形状）逐字不变。删除
`ShopWalk._card_note()`、`db.py` 的 `_CARD_POS_RE` / `_card_pos()`、`analyze_click.py` 的三张
事件名常量表，以及测试替身里手拼编码的两行。`tools/bench_refresh.py` 的合成数据仍写
`"click_deny"` 字面量——它是压测造数而不是这条协议的生产者或消费者，本次不动。

## 收口与挂账

验收 2026-09-18：全套 `python -m unittest discover -s tests` **447 项通过、0 跳过**
（改前 425 项）。新增 `tests/test_click_events.py`（19 项，协议自身的往返与边界）与
`tests/test_click_events_wiring.py`（1 项跨模块：脚本化页面 adapter 跑一次真遍历，事件落临时库，
再由数据层与分析工具各自消费——断言卡片口径 1、事件口径 attempted 3 / 成功率 1/3）。改前的
复现（1 条 `click_ok` + 1 条 `click_parse_error` 交回 `attempted=1`、成功率 1.0）已变成回归用例。

工作期间一次全套运行报出 39 个错误，与本次改动无关：那次跑到本地时钟跨过午夜，而
`tests/helpers.py:new_round` 用的是 `cst_date()`，套件跑到一半北京时间换了日，`rounds.open`
按跨天逻辑收尾旧轮、新建一轮，一批用例因此失败。这正是 2026-09-14 审查候选 07
（「让轮次时刻从采集入口进入」）记录的那条测试债务，跨日前后各跑一次都是 447 项通过。

**真库手工抽查**（2026-09-18，只读；运行库 `F:/AI/bestseller_runtime/data/bestseller.db`）。
因为默认库路径不在本次范围，两次都显式带 `--db`，并与改动前的工具（`git show HEAD:tools/analyze_click.py`）
对同一轮做了对照：

| 轮次 | 改动前 | 改动后 |
| --- | --- | --- |
| 33（`click_parse_error`，轮次 15–33 共 185 条） | A01 点击 187、解析失败 **0**、成功率 95.7% | 点击 188、解析失败 **1**、成功率 95.2% |
| 12（`click_parse_empty`，轮次 10–12 共 9 条） | A01 解析空 2、A03 解析空 1 | 完全相同 |

第 33 轮那条解析失败原来整条不进分母（点击列少算一次、成功率结构性偏高），现在进来了；第 12 轮
那 9 条历史行的读数一字未变，说明读取侧的历史解释没有改变既有口径。表头那一列也从「解析空」改名
为「解析失败」——它一直指的是 `UNREADABLE` 这个结果种类，旧名字是从已消失的事件名照抄来的。

**两条挂账，各自单开票**（`.scratch/` 不入库，能穿过一次 clone 的只有本 ADR）：

1. **事件写入保证**：`Database.event_logger` 的 `emit` 吞掉一切写失败（`db.py` 里那句
   `log.debug`），而 `click_card_failures` 的结果直接参与「失败率超阈值即暂停」。分类再准，行写
   不进去也白搭——这是本候选收益的上限。要动它得先分清「承重事件 / 遥测事件」两类，会波及所有
   `emit` 调用点。
2. **分析工具的默认库路径**：`tools/analyze_click.py` 的默认 `<repo>/data/bestseller.db` 不存在，
   且用可写的 `sqlite3.connect` 打开，手工跑一次会新建空库而不是读真库；`tools/analyze_delay.py`
   同病。`tools/check_orphans.py` 已经改对（从 `Config.from_file(...).db_file` 取路径 + `mode=ro`），
   照它改即可。
