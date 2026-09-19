# 0030. 介入的两种读法出自同一份证据

- 状态：已采用并实现
- 日期：2026-09-19

## 背景

`guard` 对**同一份页面证据**给了两套判据：

- `intervention_kind(page)` 读三样——地址、正文、可见验证容器——回答「要不要人工介入」；
- `resolved(page)` 读两样——地址、可见验证容器——回答「介入解除没有」。
  它的实现是 `return not captcha_visible(page)`，**正文这一路它看不见**。

于是「只由正文命中」的那一幕（地址正常 + 正文带标记 + 没有可见验证容器）里，同一次判定同时
给出「要介入」与「已解决」两个矛盾答案。三处后果，全部静态可推：

1. `detail_visit.ReadyDetailVisit.observe()` 的介入分支没有出口：`_guard()` →
   `ready_detail_page` 说页面可用（返回 `False`，不是 deny）→ 重置可读窗口 → `continue` →
   判据仍然为真。`:101` 的 `deadline` 兜底只在不含介入的那条路上被读，而这条路每圈把它推后
   10 秒。**真仓 HEAD 上实跑：40 秒不返回，约 43 万圈/秒空转**（这条路上没有 `sleep`）。
2. `wait_for_resolution` 的确认窗口第一句就是 `if resolved(page): return`，此刻恒为真 ——
   于是从不 `page.reload()`、从不发 `verification_appear`、从不响铃。
3. `listing.prepare` 走同一对调用，症状相反：登录墙既不等人工也不发事件，直接落成
   `ListingLoadFailed`。

### 生产里有案

206 份存档原页里 7 份（`round_10` 4 份、`round_21` 3 份）就是这一幕。它们是淘宝的
「亲，访问被拒绝」拦截页：正文 **813 字**（口径：去 `script` / `style`、把连续空白折成一个空格后取
长度；换一种空行口径会得 801，离 `<3000` 那道门都很远）、导航栏带「亲，请登录」、没有滑块标记、
没有验证容器。**地址是账本证实的**——这 7 个 offer 在 `event_log` 里的失败原因写着
「详情页未解析到 SKU：`https://detail.1688.com/offer/<id>.html?_t=…`」，没有 `bsop-punish`、
没有 `deny_pc`。它们的结局是解析失败、`sku_count=0`，**没有 `click_deny`、没有任何 verification 事件**。

全库 `kind='verification'` 的事件是 **721 条 `verification_appear` + 721 条 `verification_solved`，
`verification_type` 一条 `slider` 之外没有别的——`login` 是 0**。这不能单独当证据（本条描述的那个
机制正好也能解释它：一个只能由正文命中的登录型信号，在改前根本走不到发事件那一步），但它与本条
自洽。

（两处更正：初稿照抄审查报告，把长度写成 912、把这页说成「登录墙」。长度按统一口径是 813；
而它不是登录墙，是平台拦截页，只是导航栏里挂着「亲，请登录」。这个区别在「预期结果与代价」里要用。）

## 决策

**`resolved` 不再自己认一遍证据，改为同一次判定的第二种读数。** 证据只读一次、命名成一份，判定只有一份：

```python
@dataclass(frozen=True)
class _PageEvidence:          # 三样证据，各自有名
    url: str
    body: str
    captcha: bool

def _page_evidence(page) -> _PageEvidence:   # 读一次
def _intervention_of(evidence) -> str | None:   # 纯函数：证据 → 判定
def intervention_kind(page):   # = _intervention_of(_page_evidence(page))
def resolved(page):            # = _intervention_of(_page_evidence(page)) is None
```

证据三样不打包成一个裸三元组、而是给名字，是因为 ADR-0029 已经记过同形的
`emit` / `deny_tracker` / `shop_key` 三件套——**顺序传参在两处都读不出谁是谁**，
何况这里还要跨 module 边界（`guard` 判据 → 页面）。

- 证据集**一个字没动**：`is_punish_url` 的 tmd 排除、`SLIDER_MARKERS` / `LOGIN_MARKERS` 的内容、
  `captcha_visible` 的选择器、`<3000` 长度门，全部照旧。真机差分核过：206 份存档页上
  `intervention_kind` 取值**变化 0 页**。
- 判定抽成纯函数，是为了让「读一次、判一次」是字面事实而不是约定；顺带让它能脱离页面对象被
  直接检验（真机差分就是这么跑的，测试面也有一条直接喂证据的用例）。
- `guard` 的公开面**一个名字都不增不减**：两个新名字是私有的。
- `detail_visit` 一行都没改：判据收成一份之后，`observe()` 那条分支自然有出口。

读**不出**证据时 `resolved` 回 `False`（还没解除、继续等），与 `intervention_kind` 上抛**有意不同**：
一个是判据，异常交给调用方；一个是轮询谓词，「读不到」只说明还不能停。

**这说的是「读证据出错」**：`page.url` 抛错、或页面已关这类。而 `body_text` / `captcha_visible`
自己把异常吞成了空证据，那种情况判据给 `None`、`resolved` 便是 `True`，与「页面确实没有信号」
不可分——确认窗口会因此把一次真的介入判成误报。这层区分**先于本条存在**（改前的 `resolved`
也这样），本条没有动它，记进挂账。

## 预期结果与代价

- 详情读取循环的每一圈都有界：要么判据变假（走出去读页面），要么 `wait_for_resolution` 抛
  `InterventionTimeout`（每一轮等待的上限是 `human_pause_minutes`）。**没有「一圈什么都没变、
  还带 43 万圈/秒空转」那种转了**。
  严格说整段不是全局有界的：`observe()` 里判据是**三次独立读**（`observe()` 那次、
  `ready_detail_page` 那次、确认窗口里 `resolved` 那些次），所以一个在「有信号 / 没信号」之间
  反复翻的页面理论上还能再入这条分支。今天的页面不是那样，加全局超时则会在人正解决到一半时
  把等待掐断——所以这里只保证「每圈有界且每圈之间页面必须真的变了」，不做全局截断。
- 确认窗口回到它声明的职责：过滤**没有 DOM 落点**的瞬时信号（ADR-0029 原话是 tmd/x5sec 上报那一类）。
  正文是 DOM 落点，它本来就该挺过窗口、响铃、等人。

**代价要说在明处：那 7 份拦截页的处置变了。**

| | 改前 | 改后 |
| --- | --- | --- |
| 确认窗口 | 当场判成误报 | 正常走完，先试一次 `page.reload()` |
| 刷新后仍被拦 | —（从不刷新） | 响铃等人，最长 `human_pause_minutes`（10 分钟） |
| 结局 | 静默继续 → 解析不出 SKU → `detail_fail` | 无人解决 → `InterventionTimeout` → 本轮暂停（可续跑） |

接受它的三条理由：改前的「静默」不是中立选项，它把一次真实拦截记成「这个商品没有 SKU」；
改后先试刷新，而平台拦截页常靠刷新解除，这一支改前根本不会执行；剩下真要人看一眼的，
响铃与暂停是这套系统对人工介入既有的表达方式。

**没有顺手修的是判据本身**：这一页含「点我反馈」（`_intervention_of` 里「纯反爬拦截页」的记号），
但那个豁免写在**验证容器那一支**里 —— 而这 7 页连容器都没有（DOM 里一个 captcha 选择器都没命中），
于是豁免根本没轮到它：页面从**登录标记那一支**被判成「登录墙」。所以就算把豁免挪到两支共用也
不解决问题（这一支照样认得它），真要豁免得是一条在判定开头就早退的规则。那是改判据、不是收读数，
而且手里**0 份真登录墙样本**做对照——据此设计「什么时候登录墙不是登录墙」等于凭空造判据。
单列挂账，并把今天的判定用一条用例钉住（`test_the_judgment_is_a_pure_function_of_the_evidence`），
免得将来改这条规则时没人知道它牵着什么。

## 与既有 ADR 的关系

- **ADR-0029（验证判据只认页面证据）**：不是冲突，是它的后半。那条 ADR 在「被否掉的方向」里
  **写下过这个不终止机制**，但把它归因于它正在删的响应流信号（`_punished` 冻结、`resolved()`
  看不见它），于是选择删信号。删掉 `punished` 关掉了一个入口，正文这个还开着——它自己写下的
  「缺的那条判据」就是本条。0029 的「实现边界」明说不改三样证据、不碰 `LOGIN_MARKERS` 的长度门，
  所以它没走到这一步。**准确说法：这是 0029 之后的缺口，机制与它当时描述的那条同源。**
- **ADR-0023（详情访问返回新观测）**：`observe()` 的循环是它引进的（2026-09-14）。本条不改它，
  只是让那个循环第一次有出口。
- **ADR-0016 / 0009（停止语义）**：`InterventionTimeout` 本来就登记在 `_STOP_OUTCOMES` 里，
  落到 `RoundPauseRequired`，「本轮暂停、可续跑」。本条没碰这张表。

## 挂账

- **拦截页被判成「登录墙」**：「点我反馈」这个「纯反爬拦截页」的记号写在容器那一支里，
  而这类页面（可能没有容器）是从登录标记那一支进来的。本条之后，此类页面会响铃并等满
  `human_pause_minutes`。要动它得先有**真登录墙的样本**。
- **这一页也没被认成 deny**：正文里有指向 `bsop-punish-test-webapp/deny_pc.html` 的链接，
  但它自己的地址是正常详情 URL，`is_deny_url` 为假，进不了「不响铃、自动退避」那条路。与上一条同源。
- **「读不出证据」与「没有证据」今天不可分**：`body_text` / `captcha_visible` 把异常吞成空证据，
  于是读失败与「页面干净」都让判据给 `None`、`resolved` 给 `True`——确认窗口会因此把一次真的介入
  判成误报。**这层区分先于本条存在**（改前的 `resolved` 也这样），本条只把 `resolved` 上抛的
  那一半保持原样（回 `False`）。要分开得先定「读不出正文时算不算需要人看一眼」，没有样本支撑，
  不在本条里猜。
- **两个页面替身**：`tests/helpers.FakePage`（证据型：地址/正文/容器）与 `FakeCard.acquire` 里
  内联的那个 `SimpleNamespace`（内容型：`url` + 脚本化 `content`）并存，各服务各自的脚本。
  并成一个是独立的测试面工作，本条没碰。
- **`listing.prepare` 与 `ready_detail_page` 是同一对调用的第二个接线点**：本条只修判据，
  没把 `prepare` 收进共享的安顿入口（那要先有 deny 那一半的取舍）。改完 `prepare` 的行为自动正确。
- **`ready_detail_page` 的 `emit` / `deny_tracker` / `shop_key` 三件套**：ADR-0029 已记为
  「一个想出生的 guard 上下文类型」，本条没碰。

## 实现边界

- 只动 `bestseller_monitor/guard.py` 里 `intervention_kind` / `resolved` 附近那段，抽两个私有名字。
- 不动：三样证据本身、`ready_detail_page`、`wait_for_resolution` 的窗口与时限、
  `detail_visit`、`listing`、`click_listing`、`browser_pw` 的兼容层、事件、数据库、迁移、`CONTEXT.md`。
- 测试面新增两个共用替身（`tests/helpers.py` 的 `FakePage` / `GuardClock`）。

## 收口与验收

- 全套 `python -m unittest discover -s tests`：**470 项通过、0 跳过**（改前 463）。
  七条新用例（`test_guard.py` 六条、`test_detail_visit.py` 一条），五条先红后绿、两条是锚。
- 原报告那一幕重跑：改前 40 秒不返回；改后「刷新后恢复」照常读出观测（`sku_count=1`）、
  「一直不解决」有界地以 `InterventionTimeout` 收场。
- **真机差分**（206 份存档原页，HEAD 的旧 `guard` 与现在并排跑同一批页面对象）：
  `intervention_kind` 变化 **0/206**；`resolved` 变化且该页判据非 `None` 的 **7 页**，
  正是上面那 7 份拦截页；另有 41 页 `resolved` 变化但判据为 `None`（`wait_for_resolution`
  进不去，`resolved` 不会被问）。
- 未做：真机上制造一次现场（要真被平台拦一次）。差分用的是存档原始页，不是活浏览器。

**两轴审查后的收口**（2026-09-19）：

- **Spec 轴抓到一处真问题**：`_intervention_of` 原先放在 `try` 之外，于是 `page.url` 非字符串时
  旧 `resolved` 回 `False`、新的抛出去——「对全部输入等价」不成立（审查者实测复现）。
  已把判定并回 `try`，并补一条直接喂证据的用例把「纯函数」这条理由兑现。
- **Standards 轴抓到一处体例违规**：给 ADR-0029 加的标注原先写成独立的 `- 后续：` 列表项，
  而本仓一律是 `- 状态：已接受（…更新：…）` 的括号注（0020 / 0023 / 0012 / 0016 都这么写）。
  已改回状态行。
- 两轴各自点到的基线气味（裸三元组、`_intervention_of` 的 `of` 指代不清）已用
  `_PageEvidence` 具名类型一并收掉；另外两条（两个页面替身、`body_text` 把读失败吞成空证据）
  按上面的挂账处理，没有在本条里动。
- 顺带更正两处数字：verification 事件是 **721 appear + 721 solved**（不是「721 条」，那个数只数了
  appear），存档页正文按统一口径是 **813**（审查者按另一种空行口径得 801）。

## 被否掉的方向

- **反过来缩小证据集**（`resolved` 不动，让 `intervention_kind` 也只认地址与容器）：分叉是没了，
  代价是丢掉正文这一路检测——而它正是生产里唯一真正命中过的信号（那 7 份存档）。
- **给 `observe()` 的循环加超时**：治不好。正文命中的介入仍然既等不到、也解不开，只是从
  「挂住」变成「按时挂住」。
- **两套判据各自补上正文、但保持两份实现**：还能再漂开，正是本条要拆的东西。
- **顺手把「点我反馈」的豁免用到登录那一支**：见「预期结果与代价」末段 —— 那是改判据，
  且没有真登录墙样本做对照。单列挂账。

完整设计、验收与差分记录见 [`.scratch/intervention-resolution/spec.md`](../../.scratch/intervention-resolution/spec.md)；
来源是 2026-09-19 的架构审查候选 01。
