# 0020. 详情访问只有一处规则：deny 账目并进 guard，两条路共用同一次「打开之后怎么安顿」

- 状态：已接受（一处结论被 [ADR-0029](0029-intervention-evidence-in-one-place.md) 更正：「补采的 punish 信号比改前多认一种」不成立，那条兜底分支对任何输入都到不了，`punished` 形参已整个撤掉；文末「已知没做完的一件事」随之撤回，改记为有意的盲区）
- 日期：2026-09-14

## 背景

点击式列表与逐店补采都在访问详情页，但只有点击那条有反爬应对：

| | 点击式列表 | 逐店补采（`browser_pw.open_detail`） |
| --- | --- | --- |
| 打开 | `click_card`：等弹窗 → `intervention_kind(popup, punished)` → 等人工解决 | `goto` → **固定 `time.sleep(2)`** → `intervention_kind(page, False)`（punish 写死为假） |
| deny 账目 | 有：`ShopWalk.capture` 记账 + 退避重试 3 次 + 两个阈值 | **完全没有**：`deny_tracker` 根本没传进来 |
| 用到 `human` | 有（退避） | 形参传进来从头到尾没用过 |

落到 deny 页时，补采现在的表现只是「html 读回来解析失败」，记一次普通失败——同一台机器正在
被限流这个可观测事实，走哪条路居然两种待遇。`guard.deny_resolved()`（deny 页是否已解除）
全仓零调用，是这条账目从没接上的旁证。

## 决策

- **deny 账目与「打开之后怎么安顿」都归 `guard.py`**：`DenyTracker`、`ShopDenyExceeded`、
  `RoundDenyExceeded` 从 `click_listing.py` 搬过去（那本来就是「是否 deny 限流」判定的家），
  新增

  ```python
  ready_detail_page(page, cfg, *, emit=None, punished=False,
                    deny_tracker=None, shop_key=None) -> bool
  ```

  两条路打开页面之后都先走它：**先认 deny**（记账 → 整轮阈值 → 店铺阈值，越界抛那两个异常），
  **不是 deny 才判人工介入**并等人解决。顺序是有意的：deny 是自动限流，该退避，不该当成滑块
  去响铃等人（`is_deny_url` 的注释一直这么写着，只是点击路径过去先等人工、再判 deny）。
- **补采命中 deny 算进阈值**（本轮 grill 先拍的板）：`DenyTracker` 由 `_run_listing_pw` 一路
  传到 `_capture_one_pw`，与点击路径是同一个实例。阈值越界时：店铺那级 → 该店记为未完成、
  这一轮接着跑下一家；整轮那级 → 中止本轮（`_STOP_OUTCOMES` 的收尾原样复用）。
- **补采的等待改成条件等待**：`browser_pw.wait_until_detail_readable()` 轮询到「html 能读出
  SKU 行」（`detail.readable()`），上限 10 秒，落在 deny / punish 页立刻返回。改前的固定
  `time.sleep(2)` 是拿时间赌渲染完了。
- **命中 deny 的那一单写清理由**：`detail.Observation.denied(html)`（「详情页被反爬拦截
  （deny）」+ 留原始页），而不是含混的「解析失败」。
- **删掉两处死东西**：`guard.deny_resolved()`（零调用）；卡片句柄的 `denied()`（改由
  `ready_detail_page` 认，卡片不再自己判 deny）。
- **补采这条的 punish 信号**：没有点击路径那份响应监听，`punished=True` 交给
  `is_punish_url(url)` 按地址认——比改前（写死为假）多认一种，但响应里出现 punish 而后台
  地址正常的那种仍看不到（见下）。

## 结果

有意的行为变化：

1. **补采的 deny 计入 `deny_shop_limit` / `deny_round_limit`**：补采期间命中的 deny 现在可能
   让这家店提前被跳过、或让整轮提前中止。那正是「正在被限流」该有的后果。
2. **deny 页不再去响铃等人**：改前点击路径先 `intervention_kind` 再判 deny，而 deny 页正文
   常带 `punish` 字样，于是会先响铃等人（最长 `human_pause_minutes` 分钟）才走退避。现在
   deny 先判，直接退避。
3. **补采的详情页等待不再固定 2 秒**：通常第一个轮询就命中（快）；慢渲染最多等 10 秒（稳）；
   deny / punish 页立刻返回。
4. **一条补采撞上店铺阈值会把该店记为未完成**（`list_status='失败'`，备注写清是 deny）。
   代价是这一轮的收尾会按「榜单阶段未完成」要求续跑——续跑接着补这家店没补完的商品，正是
   限流之后想要的。注意这条店其实拿到了完整榜单，所以「未完成」在这里指「这一轮这家店没跑完」，
   不是「残缺榜单」那个意思。
5. **诊断工具拿到的详情页不再被隐式等待挡住**：`click_card` 只负责把页面打开（等待与 deny
   判定归调用方），`tools/diag_card_urls.py` 这类工具看到的是页面原始状态。

另外记一笔细节：补采命中 deny 的那一单按失败记一行（`Observation.denied`），因此**消耗一次
尝试额度**——限流期间同一个商品被连打三次就会用完本轮额度，等下一轮再补。这是「记下来」与
「别在限流里反复敲门」之间的取舍，选的是后者。

其余照旧：事件名与顺序不变（点击路径的 `click_deny` 备注 `&n=/&shop_skip/&round_abort`
逐字保留）；点击路径的退避重试阶梯、`_STOP_OUTCOMES` 的收尾文案与两条阈值一个都没动；
`ShopDenyExceeded` 的店铺备注改成「deny 超过阈值，这家店本轮到此为止：…」——它现在两条路
都到得了，原来那句只讲「榜单」。

用例：`tests/test_guard.py::ReadyDetailPageTests` 七条（deny 记账、deny 不走人工介入、两个阈值、
滑块等人、超时上抛、没账目也认 deny）、`tests/test_browser_pw.py` 五条（可读判据、条件等待、
超时、deny / punish 立刻返回）、`tests/test_p1.py` 新增三条（补采 deny 记账且理由写清、补采
撞店铺阈值后接着跑下一家、补采事件顺序不变）；`tests/helpers.py::FakeCard` 改交 `page` 句柄。
全套 378 条通过、1 条跳过。

已知没做完的一件事：补采这条没有响应监听，所以「响应流里出现 punish、但当前地址正常」这种
情况仍判不出来；要补的话得给 `open_detail` 挂一个与点击路径同款的监听（并记得摘掉）。

两轴审查（由主 agent 自己走完：这次派出去的两个审查子 agent 的任务目录一直没被认领，同一
工作区里另一个会话占着并发位）：

- **成文标准 0 条硬违规**；两处 ADR 描述漂移已标注失效（ADR-0014 的卡片句柄清单、ADR-0013 的
  `Observation` 构造子个数）。
- 基线气味收拾了三处**本轮自己带进来的**：`click_listing` 不再用的 `import time` 与
  `is_deny_url`、`browser_pw` 不再用的 `wait_for_resolution`、`test_p1` 里一条失效的打桩
  （`open_detail` 不再自己判人工介入）。
- 留着的两处判断（写在这里备查，不再动）：`detail.observe_page(lambda: html, …)` 这个调用点上
  「读失败」那一支是死路（html 已经拿到手），但接缝不能删——点击路径要靠它读弹窗内容；
  `guard.py` 现在是三个 module 里职责最宽的一个（判定 / 账目 / 异常 / 安顿），它的 docstring
  已经改成这份清单，先这样。
- 规格轴：要求逐条在场、无范围蔓延；两处「实现了但没写进文档」补上了——deny 那一单会消耗
  尝试额度，以及「补采撞阈值把该店记为未完成」在词汇上不是「残缺榜单」的意思。

被否掉的方向：

- **只给补采加一个 deny 判据、规则仍写在 pipeline 里**：deny 判定、账目、阈值会散在三个
  module（guard 的谓词、click_listing 的阶梯、pipeline 的新判据），正是这条要收的东西。
- **补采的 deny 只记事件、不进阈值**：断路器会因为走哪条路而两种待遇，补采恰好发生在「刚跑
  完榜单还要继续打同一个站点」这个最容易被限流的时刻。
- **把 `sleep(2)` 换成更长的固定等待**：还是拿时间赌，慢一拍就丢单、快一拍白等。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「详情观测」「补采」「跳过」；来源是
`docs/reviews/architecture-review-2026-09-13-r2.html` 候选 04，规格在
`.scratch/detail-access/spec.md`。
