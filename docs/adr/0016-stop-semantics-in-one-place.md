# 0016. 停止语义只有一处定义：分类表 + 界面端的窗口状态机

- 状态：已接受
- 日期：2026-09-13

## 背景

ADR-0009 的停止协议横跨两个进程，但「这个停止该怎么收尾」的知识散在三处：

- **采集侧**：`STOP_EXCEPTIONS` 元组 + 两套 except 阶梯——`_run_listing_pw` 六段（决定本店
  备注）、`_run_round_locked` 五段（决定轮次终态）。其中有一段**承重顺序没有声明**：
  `RoundDenyExceeded` 继承 `RoundPauseRequired`，所以整轮 deny 那一段必须排在暂停那一段
  之前——两段对调，整轮中止会静默降级成「暂停、可续跑」。
- **界面侧**：`gui.Api` 上的 `self._stop` 一个字典 + 七个方法（`_begin_stop` / `_stop_state` /
  `_enforce_stop_deadline` / `_note_stop_ack` / `_force_stop` / `pause_run` / `abort_run`）。
  窗口读挂钟又藏在私有字典里，于是测试只能**改私有状态**来推进窗口：
  `tests/test_gui.py` 有七处 `self.api._stop["deadline"] = time.time() - 1`。

## 决策

- **采集侧收成一张表**：`_STOP_OUTCOMES: dict[type, StopOutcome]`，每条写清「轮次写不写终态、
  终态说明怎么写、本店备注怎么写、给操作者看哪一句、用哪个日志级别、要不要跳过整店」。
  两套阶梯各收成一条「查表 + 兜底」：
  - `_run_listing_pw`：记本店未完成 → `skip_shop` 就 continue，否则上抛；
  - `_run_round_locked`：有终态就 `finish_if_open`，没有就按「保持可续跑」提示。
- **查法是「自己 → 父类」**（`stop_outcome()` 走 `type(exc).__mro__`）：登记父类等于登记
  它的子类（`InterventionTimeout` 就是这样落进「人工介入未完成」的），而子类自己登记的条目
  优先——`RoundDenyExceeded` 与 `RoundPauseRequired` 各占一行，谁也不遮谁，
  **那个靠 except 先后顺序保证的隐性约束从结构上消失了**。没登记的停止异常会显式抛
  `KeyError`，而不是被静默归错类。
- **界面侧收进 `stop_request.py`**：新增 `StopInFlight`（不可变：kind / target / round_id /
  deadline / acked）与 `StopWatch`——窗口、回执、超时强杀、收尾都在这里；**时间与副作用都从
  构造进来**（`now` / `kill_child` / `stop_foreign` / `close_browser` / `is_running` /
  `identity_of`），所以整条协议可以在没有进程、没有浏览器的情况下走一遍。
  `stop_request.py` 本来就是 ADR-0009 的采集端（`install/consume/check`），这样一来
  **协议两端只在同一个文件里**。
- **`Api` 只负责接线**：把世界的四个口子用 **lambda 晚绑定**交给 `StopWatch`（测试 patch
  类方法时要打到实际调用点，直接传绑定方法会在构造那一刻定死）；`Api.__init__` 多一个
  `stop_clock`（默认挂钟），用例给它一个能推进的时钟即可。

## 结果

- 界面侧：`gui.Api` 不再有 `self._stop` 与五个内部方法（`_begin_stop` / `_stop_state` /
  `_enforce_stop_deadline` / `_note_stop_ack` / `_force_stop`）；`pause_run` / `abort_run`
  仍是入口，只是改成「写请求/写终态 → `watch.begin(...)`」。`tests/test_gui.py` 里七处改私有
  状态的写法变成 `self.clock.advance(20)`——**推动窗口用的是 interface，不是私有字段**。
- 采集侧：两套阶梯各剩「查表 + 兜底」，文案与分类同处一行；「顺序承重」不再存在。
- 新增 `tests/test_stop_request.py::StopWatchTests` 七条，把整条协议在没有进程的情况下走完：
  窗口内不动手、到点强杀并清理、回执放宽窗口、进程已走只清理、强杀没落到实处就把请求留给
  采集进程、中止才去停别处起的进程、`forget()` 丢掉状态。

代价有三处。其一，`StopWatch` 的构造要五个回调，接线读起来比原来啰嗦；好处是这条协议终于
能脱离进程测试。其二，`StopOutcome` 里带着给操作者看的文案——数据里含呈现，换来的是「改文案
不必再翻两套阶梯」。其三，新增停止异常必须记得登记，否则 `stop_outcome()` 会抛 `KeyError`
（这是有意的：静默归错类比开库时崩一下更难查）。

被否掉的方向：

- **让每个停止异常自带 scope 与文案**：异常定义散在四个 module（guard / click_listing / db /
  stop_request），分类会跟着散开；表把它们聚在一处。
- **只在注释里写下那个承重顺序**：隐患还在，只是被写下来了。
- **界面侧只把 dict 换成 dataclass**：窗口仍旧读挂钟，用例仍旧只能改私有状态。

两轴审查（成文标准硬违规 0）另抓到五处，都已收口：

1. 我在把 `_run_listing_pw` 的六段阶梯收成查表时，**把整轮 deny 那一段的
   `log.error("整轮 deny 超过阈值，中止本轮：…")` 一起删掉了**——日志行为变了，不算逐字等价。
   现在这条日志跟着那条表项走（`ladder_log` 字段）。
2. `STOP_WITH_OUTCOME` 把只跳过单店的 `ShopDenyExceeded` 也收进来，而它在轮次级阶梯里
   `round_end`/`notice` 全空——万一漏进去就是「空 INFO + 空提示、轮次静默留进行中」。现在用
   一个 `scope`（`ROUND` / `SHOP`）说清「影响谁」，两条阶梯各按 scope 派生，不再靠
   `skip_shop` 反推、也不再让一个字段担两个意思。
3. 身份匹配规则（「这条请求指向的是不是那个进程」）在 `stop_request.py` 里写了两遍
   （采集端的 `_mine` 与界面端放宽窗口时的判断），合并成 `targets(request, pid=…, started_at=…)`。
4. `StopInFlight.round_id` 只写不读（旧 dict 里也一样），删掉。
5. `StopWatch` 的五个口子原来带 no-op 默认值，漏接线时静默什么都不做——与这份 ADR 自己主张的
   「未登记就显式报错」相反，改成必填。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「停止请求」「协作停止」「强制停止」「界面会话事实」；
来源是 `docs/reviews/architecture-review-2026-09-13.html` 候选 06，规格在
`.scratch/stop-semantics/spec.md`。
