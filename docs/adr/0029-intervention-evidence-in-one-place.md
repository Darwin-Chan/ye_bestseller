# 0029. 验证判据只认页面上的证据

- 状态：已采用并实现
- 日期：2026-09-19

## 背景

点击式列表的 adapter 从 ADR-0020 起在页面响应流上挂了一份监听：xhr/fetch 的地址是真 punish
地址就把 `self.punished` 置真，随后这个布尔被当成形参穿过五个 module，最终交给
`guard.intervention_kind(page, punished)`。

而判据里读它的只有兜底一行（`guard.py:150`）：

```python
if is_punish_url(url):                 # :136 —— 对任何 punish 地址早退，不看 punished
    return "滑块"
...
if punished and is_punish_url(url):    # :150 —— 与 :136 是同一条谓词
    return "滑块"
```

`punished=True` 时 `:136` 已经返回；`punished=False` 时 `:150` 的条件不成立。**这条分支对
任何输入都到不了**，整条通道于是写而不读：`ProcessStopRuntime` 式的「缺省实现零穿行」在这里
的对应物是「形参零影响」。真机差分核对（同一批页面把旧判据的 `punished` 两个取值都跑一遍）
16 个组合零处不等。

同源的死重还有两处：`click_listing.click_card` 的 `punished` 形参在函数体里从未被用过（响应
监听是另一个形参 `on_response` 传进去的）；`browser_pw.navigate_detail` 交的
`OpenedDetail(page, punished=True)` 是个字面量，只喂给那条不可达分支。

### 一处更正：ADR-0020 / ADR-0023 记的缺口，前提不成立

两条 ADR 把这件事记成「补采缺响应监听」的独立缺口：

- ADR-0020：「补采这条的 punish 信号：…`punished=True` 交给 `is_punish_url(url)` 按地址认
  ——**比改前（写死为假）多认一种**」；「已知没做完的一件事：…要补的话得给 `open_detail` 挂
  一个与点击路径同款的监听（并记得摘掉）」。
- ADR-0023（范围外）：「补采的响应流 punish 识别仍是 ADR-0020 的独立缺口」。

两句的前提都是「点击路径那份监听在工作，只是补采没接上」。**不成立**：点击路径这份监听从未
参与过任何判断，所以「多认一种」是假的，给补采挂上同款监听也判不出它们说的那个场景
（「响应里出现 punish 而后台地址正常」）——判据里没有任何一条路认这个证据。

## 决策

把响应流信号整个撤掉，判据只留它认的三样证据——地址、正文、可见验证容器：

- `guard.intervention_kind(page)`：少一个形参，删掉 `:149-151` 那条兜底（与它上面的注释）。
- `guard.ready_detail_page(page, cfg, *, emit, deny_tracker, shop_key)`：少一个形参。
- `listing.prepare(page, url, cfg, human, *, describe, emit)`：少一个形参（它的 docstring 曾把
  `punished` 说成「驱动在响应监听里看到的验证据号」，一并删）。
- `detail_visit.OpenedDetail(page)`：少一个字段；`ReadyDetailVisit._punished` 与三处转交一并
  删。`begin_detail_visit` 末尾那处构造**改成关键字传参**——正是位置传参让一个没人认领的字段
  混进了一行七个实参里。
- `click_listing`：删 `PlaywrightListing.punished`、`_on_punish_response`、`page.on("response", …)`
  与 `click_card` 里给弹窗挂的同一份监听；`click_card(page, image, cfg, emit=None)` 少两个形参；
  `is_punish_url` 的 import 随之不再需要。
- `browser_pw.navigate_detail`：删 `punished=True` 及其注释。
- `tools/sync_list_titles.py` 与 `tools/diag_verify_state.py`：跟着改调用形状。

**不给它留替代物**：不留事件、不留日志、不留一个「只是记录不参与判断」的布尔。没有读方的
事件是与形参同形的死物。

## 已知的盲区

**响应流里出现真 punish 请求、而页面地址与 DOM 都没有信号时，本程序认不出来，不会响铃等人。**

这条盲区改前就存在，只是没有名字——那个形参让人以为它被覆盖了。现在它被写下来：这是**明说
的**盲区，不是**没说**的。判定：接受，不复工单。理由是判据认的三样证据覆盖的是「人真要做点
什么」的情形——需要人工解决的验证必然在页面上有个可操作的落点；只有请求、没有落点的信号，
响铃等人在语义上也无从解除（见「被否掉的方向」第二条）。

真机上要重开这个问题，出现的现象是：浏览器里明明弹出了要人处理的验证，程序却不在
`verification_appear` 上报、继续往下走。

## 预期结果与代价

- **interface 少一个无人认领的形参**：五个 module + 一个工具 + 一个数据类，签名上不再有一条
  指向不存在判断的线索。
- **行为逐字不变**：那条分支对任何输入都到不了；真机差分 16 个组合零处不等。
- **代价**：上面那条盲区不再有任何（哪怕是从未生效的）代码痕迹。要再捡起来，得重写一份监听
  ——诊断工具 `tools/diag_offer_id_presolve.py` 里那份就是现成的形状（它有自己的局部旗标，且
  真的用它：检测到就暂停等人）。

## 与既有 ADR 的关系

- **更正 [ADR-0020](0020-detail-visit-shared-across-paths.md) 的一处结论**：它记的「补采这条
  的 punish 信号比改前多认一种」与「已知没做完的一件事」都不成立（见上），本条把那个待办
  **撤回**，改为「已知的盲区」。它其余的决策（deny 优先、账目进 guard、条件等待、两条路共用
  一次安顿）一概不动。
- **更正 [ADR-0023](0023-detail-visit-returns-fresh-observation.md) 范围外那一句**：响应流
  punish 识别不再是「独立缺口」，而是有意的盲区；`OpenedDetail(page, punished=…)` 的字段随之
  消失，其余 interface（四类结果、一次性 capability、`observe` 语义）不动。
- **[ADR-0012](0012-listing-module-and-single-page-walk.md) 的 `prepare` 签名**是描述性的，
  状态行注明。
- 三条受影响 ADR 的状态行均已注明失效或更正。

## 挂账

- 无新增工单。撤回 ADR-0020 那条待办（不转成工单，理由见「已知的盲区」）。
- 诊断工具各自的响应监听（`diag_offer_id_presolve` / `diag_list_dom` / `diag_verify_state`）不
  动：它们的旗标自己用，不走 `intervention_kind` 的形参。

## 实现边界

- 只动 `guard.py` / `listing.py` / `detail_visit.py` / `click_listing.py` / `browser_pw.py` 的
  上述签名与转交面，加两个工具调用点与测试。
- 不改判据认的三样证据本身（`is_punish_url` 的 tmd 排除、`SLIDER_MARKERS`、`captcha_visible`
  的选择器、`LOGIN_MARKERS` 的长度门）；不改 `wait_for_resolution` 的确认窗口；不改 deny 记账
  与阈值；不增加数据库字段或迁移。

## 收口与验收

- 全套 `python -m unittest discover -s tests`：**459 项通过、0 跳过**（改前 454）。新增 5 条：
  `test_guard.py::InterventionEvidenceTests` 3 条（判据只收页面、安顿入口不收 punish 信号、
  响应流有 punish 而地址正常时判 None）、`test_detail_visit.py` 1 条（`OpenedDetail` 只带页面）、
  `test_click_listing.py` 1 条（adapter 不再挂响应监听）。
- 先红后绿 4 条，红的原因都对：三个 `TypeError` 断言改前都「未抛」，`page.on` 那条的失败信息
  里就是被挂上去的 `_on_punish_response` 本身。
- **真机差分**（把 HEAD 上的旧 `guard.py` 与现在的并排跑同一批页面）：

  | 页面 | punished=False | punished=True |
  | --- | --- | --- |
  | 普通详情页 | None | None |
  | 真 punish 页 | 滑块 | 滑块 |
  | tmd 装饰的 punish | None | None |
  | punishTextFetch | 滑块 | 滑块 |
  | 登录墙 / 滑块文案 / 点我反馈 / 空页 | 各自判定 | 逐字相同 |

  16 个组合零处不等；「普通详情页 + punished=True → None」正是 ADR-0020 那条盲区的现场。
- 过 `compileall` 全仓（含 `tools/`、`gui.py`、`run.py`）；两个改过的工具能 import，`click_card`
  三实参调用形状实测可跑，`browser_pw.intervention_kind(page)` 单实参实测可跑。
- 未做：真机（真实浏览器）上制造一次「响应流有 punish、地址正常」的场景——这条盲区本来就是
  难得自然复现的那类，也正是它被记成盲区而不是待办的原因。

## 被否掉的方向

- **补一条「见到 punish 响应、地址正常」的真判据**（报告给的另一条路）：要动的不止一个形参。
  - `wait_for_resolution` 的确认窗口存在的目的就是过滤「短暂出现又自行消失的信号（如
    tmd/x5sec 上报）」——一条没有 DOM 落点的请求信号正是这一类，`resolved(page)` 会立刻返回
    True，判真之后当场被判成误报忽略。要让它生效，就得先拆掉这层防误报。
  - `detail_visit.ReadyDetailVisit.observe()` 的循环接不住它：`intervention_kind` 为真 →
    `_guard()` → `ready_detail_page` 说页面可用（不是 deny）→ 重置可读窗口 → `continue` → 再判
    还是真（`_punished` 在 capability 里是冻结的，`resolved()` 又看不见它）→ **不终止**。所以
    还得同时造一个「这条信号什么时候算解除」的判据。
  - 结论：这是一条新判据的设计（判真条件 + 解除条件 + 时效），没有现场证据支撑——它在本库的
    全部历史里没有产生过一次判断。留着它比删掉它更贵。
- **只删 `browser_pw` 的 `punished=True` 和 `listing.prepare` 的形参、判据不动**：留着的形参
  继续暗示一条不存在的证据链，正是本条要拆的东西。
- **留一个「只记录、不判断」的事件或日志**：没有读方的事件是与形参同形的死物，还要占事件
  表与数据库模型。
- **把诊断工具那份监听也一起删**：那是它们自己的判断（检测到就暂停等人），不走
  `intervention_kind` 的形参，不在本条范围内。

完整设计、验收与差分记录见 [`.scratch/intervention-evidence/spec.md`](../../.scratch/intervention-evidence/spec.md)。
