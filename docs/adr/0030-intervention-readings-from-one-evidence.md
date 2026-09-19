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
「亲，访问被拒绝」拦截页：正文 **813 字**、导航栏带「亲，请登录」（`<3000` 的门过得去）、
没有滑块标记、没有验证容器。**地址是账本证实的**——这 7 个 offer 在 `event_log` 里的失败原因
写着「详情页未解析到 SKU：`https://detail.1688.com/offer/<id>.html?_t=…`」，没有 `bsop-punish`、
没有 `deny_pc`。它们的结局是解析失败、`sku_count=0`，**没有 `click_deny`、没有任何 verification 事件**；
全库 721 条 verification 全是 `slider`，一条 `login` 都没有。

（两处更正：初稿照抄审查报告，把长度写成 912、把这页说成「登录墙」。长度按统一口径是 813；
而它不是登录墙，是平台拦截页，只是导航栏里挂着「亲，请登录」。这个区别在「预期结果与代价」里要用。）

## 决策

**`resolved` 不再自己认一遍证据，改为同一次判定的第二种读数。** 证据只读一次，判定只有一份：

```python
def _page_evidence(page):   # 读一次：地址、正文、可见验证容器
def _intervention_of(url, body, captcha):   # 纯函数：三样证据 → 判定
def intervention_kind(page):   # = _intervention_of(*_page_evidence(page))
def resolved(page):            # = _intervention_of(*_page_evidence(page)) is None
```

- 证据集**一个字没动**：`is_punish_url` 的 tmd 排除、`SLIDER_MARKERS` / `LOGIN_MARKERS` 的内容、
  `captcha_visible` 的选择器、`<3000` 长度门，全部照旧。真机差分核过：206 份存档页上
  `intervention_kind` 取值**变化 0 页**。
- 判定抽成纯函数，是为了让「读一次、判一次」是字面事实而不是约定；顺带让它能脱离页面对象被
  直接检验（上面那份差分就是这么跑的）。
- `guard` 的公开面**一个名字都不增不减**：两个新名字是私有的。
- `detail_visit` 一行都没改：判据收成一份之后，`observe()` 那条分支自然有出口。

读不到证据时 `resolved` 回 `False`（还没解除、继续等），与 `intervention_kind` 上抛**有意不同**：
一个是判据，异常交给调用方；一个是轮询谓词，「读不到」只说明还不能停。

## 预期结果与代价

- 详情读取循环的每一圈都有界：要么判据变假（走出观测），要么 `wait_for_resolution` 抛
  `InterventionTimeout`。不存在「一圈什么都没变、再转一圈」。
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
但那个豁免只长在**验证容器那一支**上，走登录标记那一支时不生效，于是拦截页被判成登录墙、
现在会响铃等人。修法不难猜，但那是改判据、不是收读数，而且手里**0 份真登录墙样本**做对照 ——
据此设计「什么时候登录墙不是登录墙」等于凭空造判据。单列挂账。

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

- **拦截页被判成「登录墙」**：「点我反馈」这个「纯反爬拦截页」的记号只在容器那一支生效。
  本条之后，此类页面会响铃并等满 `human_pause_minutes`。要动它得先有**真登录墙的样本**。
- **这一页也没被认成 deny**：正文里有指向 `bsop-punish-test-webapp/deny_pc.html` 的链接，
  但它自己的地址是正常详情 URL，`is_deny_url` 为假，进不了「不响铃、自动退避」那条路。与上一条同源。
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

- 全套 `python -m unittest discover -s tests`：**469 项通过、0 跳过**（改前 463）。
  六条新用例（`test_guard.py` 五条、`test_detail_visit.py` 一条），五条先红后绿、一条是锚。
- 原报告那一幕重跑：改前 40 秒不返回；改后「刷新后恢复」照常读出观测（`sku_count=1`）、
  「一直不解决」有界地以 `InterventionTimeout` 收场。
- **真机差分**（206 份存档原页，HEAD 的旧 `guard` 与现在并排跑同一批页面对象）：
  `intervention_kind` 变化 **0/206**；`resolved` 变化且该页判据非 `None` 的 **7 页**，
  正是上面那 7 份拦截页；另有 41 页 `resolved` 变化但判据为 `None`（`wait_for_resolution`
  进不去，`resolved` 不会被问）。
- 未做：真机上制造一次现场（要真被平台拦一次）。差分用的是存档原始页，不是活浏览器。

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
