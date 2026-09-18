# 0028. 冻结目标之后的处置归 adapter

- 状态：已采用并实现
- 日期：2026-09-19

## 背景

[ADR-0024](0024-stop-target-owns-the-whole-window.md) 把停止协议加深成「目标贯穿整个窗口」：
`StopWatch.begin()` 冻结目标，此后只对那个绑定动手。它的 Internal Runtime Seam 写明生产
adapter「在 begin 时冻结本 GUI 的 `Popen` handle 或打开 A 的 Win32 process handle」，并要求
「deadline 时不再读取可变的 `self.proc`」。

实现把**绑定**照做了，却把**处置**留在了调用方：`gui.Api` 构造 adapter 时注入 `terminate` /
`close_browser` 两个动作（`gui.py:132-137`），实现在 `Api._terminate_bound` /
`Api._close_bound_browser`。adapter 自己那两条缺省实现因此全仓零穿行——唯一构造点每次都把
回调给满。同一条规则于是有两份实现，且已经漂开三处：

- `_terminate_bound` 对 `hasattr(capability, "poll")` 的绑定调 `Api._kill_proc()`，读的是
  **可变的 `self.proc`**；该方法自己的 docstring 写的是 "Terminate only the process capability
  frozen by StopWatch.begin()"。ADR-0024 那条「deadline 时不再读取可变的 `self.proc`」在代码里
  不成立——今天碰不到它，只是因为 `start_run` / `resume_run` 在窗口未收口时会拒绝起新进程，
  那条窗口检查成了这条不变量的承重件。
- 同一条 `OSError`：缺省实现交 `EffectResult(False, "terminate_failed")`，StopWatch 据此进
  `VERIFYING` 并重试；`_kill_proc` 吞掉它、调用方回 `True`。走哪一条取决于接线，不是取决于决定。
- `browser_enabled`（从 GUI 当前配置来）是关闭与否的第二判据，与「由 A 的 browser facts 形成
  not-needed 或 exact `BoundBrowser`」相抵。

另有一处 `.scratch/stop-target/spec.md` 记下的旧挂账：`_close_bound_browser` 用
`browser.port is None` 把门，有精确 proof 却没有记录端口的绑定被报「已关闭」而根本不尝试。
端口不是所有权证据（`browser_proc.close_browser()` 的 `port` 参数如今只用于日志）。

### 一处更正的判断

2026-09-18 的架构审查报告把这条写成「自有子进程是一条按 PID 信任的平行授权通道」，并推测
PID 复用下会误判真正在跑的新执行。逐条走 `StopWatch._relation` 与 `_cleanup_target` 后
**不成立**：PID 复用意味着 identity 行的 `started_at` 不同，判 `REPLACED`；清理是
`(pid, started_at)` 条件删除，命中 0 行，B 保留。`bind` 让界面自己拉起的那个 handle 优先也不是绕过授权，
是 ADR-0024 明文允许的一种绑定。所以本条的危害是**潜伏的**（不变量由构造之外的东西守着），
不是活的线上故障。

## 决策

把「冻结目标 → 效果」的翻译整个收回 `ProcessStopRuntime`，外部 interface 收窄成一个事实输入：

```python
class ProcessStopRuntime:
    def __init__(self, *, own_process=None):   # 唯一注入：本界面拉起的那个子进程
        ...
    def terminate(self, bound) -> EffectResult      # 缺省分支转正，只碰 bound.capability
    def close_browser(self, browser) -> EffectResult  # 只看 BoundBrowser 上的 facts
```

- `terminate` 只对传进来的那个绑定动手：界面自己的 Popen 走 `capability.terminate()`（`OSError`
  → `terminate_failed`），外来目标走 `browser_proc.terminate_process_capability`。**不读
  `self.proc`，也不读当前配置。**
- `close_browser` 删掉 `browser_enabled` 门与 `port is None` 门。`launched_by_us=True` 不是
  调用方的声明而是 facts 推导：`observe()` 只在身份的 `browser_state == "OWNED"` 时给出
  `BoundBrowser`，`BORROWED` / `NOT_STARTED` 根本没有浏览器可传；真正的授权检查（缺 pid 或
  proof 一律拒绝）一直在 `browser_proc.close_browser` 里。
- `gui.Api` 只交 `own_process`，删 `_terminate_bound` / `_close_bound_browser` 与两个 lambda。
  `_kill_proc` 保留，唯一调用点是「暂停」的启动竞态——那条路没有停止目标、建不起窗口，
  按 ADR-0024 是有意的窄例外。

`bind` 的判据优先级不动：界面自己拉起的那个 handle 优先，比重新 `OpenProcess` 拿到的更强。

## 预期结果与代价

- **locality**：同一条规则从两处收成一处，`Api` 不再持有一份「怎么处置」的实现；两个注入点
  消失，接线上只剩一个事实。
- **不变量由构造保证**：被处置的必然是 begin 冻结的那个句柄，不再依赖 `start_run` /
  `resume_run` 的窗口检查替它挡着。
- **授权只看 facts**：界面当前配置与端口的有无都不再能否决一次有精确归属的关闭。
- **代价**：`ProcessStopRuntime` 的构造签名变窄，任何想从外面替换处置的地方都得改；错误路径的
  语义变了——`OSError` 现在如实报 `terminate_failed` 并进 `VERIFYING` 重试，而不是被吞掉后
  由紧随其后的观察去救。后者是有意的：失败该被说出来，不该靠下一步兜。

## 与既有 ADR 的关系

- 把 ADR-0024 的 Internal Runtime Seam 与「deadline 时不再读取可变的 `self.proc`」执行到底；
  binding 判据、四态 relation、CAS 清理、窗口数值一概不动。
- ADR-0024 的删除清单里还有三样没走完（`_LegacyRuntime` / `forget()` / `begin(target=None)`），
  本条**没有**顺手做——那是同一 seam 的另一件事，已开成 [IS-55](../../.scratch/1688-inventory-snapshot/issues/55-is-55.md)。
- `browser_proc.close_browser` 的签名（`launched_by_us` 参数）保持不变：它仍是 ADR-0019 的
  会话收尾与这里的强制关闭共用的那一个入口。

## 挂账

- **IS-55**：ADR-0024 的删除清单还剩三样没走完（`_LegacyRuntime` / `forget()` /
  `begin(target=None)`）。它们是同一个 `StopRuntime` seam 的第二形状，与本条同源但性质是
  「旧决定没落地完」，混做会让改动面翻倍，故拆出单开：
  [IS-55](../../.scratch/1688-inventory-snapshot/issues/55-is-55.md)。
- **`hasattr(capability, "poll")` 的分派写了两处**（`observe()` 里两处、`terminate()` 一处）：
  Popen 与 Win32 capability 两种句柄的形状判断散在 adapter 内部。这是本条之前就有的形状，
  不是本条引入的；收成一个小 helper 是纯内部整理，等下次动这个 adapter 时顺手做。

## 实现边界

- 只动 `bestseller_monitor/stop_request.py` 的 `ProcessStopRuntime` 与 `gui.py` 的接线；
  `StopWatch` 的状态机、`crawler_identity`、`browser_proc` 与 `views` 的文案均不改。
- 不增加数据库字段或迁移；不改窗口数值与事件。

## 收口与验收

- 全套 `python -m unittest discover -s tests`：**454 项通过、0 跳过**（改前 447）。
  新增 `ProcessStopRuntimeTests` 5 条（处置只认绑定 facts、无端口仍尝试关闭、缺 proof 仍交下去、
  唯一注入是自有子进程、终止失败如实上报）、`GuiStopRequestTests` 2 条（强杀只动 begin 冻结的
  句柄、identity 说 `OWNED` 时界面配置不能否决关闭）、`test_stop_target.py` 1 条（强杀失败留在
  `VERIFYING`、不清事实、不关浏览器），搬走 1 条（原钉 `Api._close_bound_browser` 的用例移成
  adapter 的契约）。
- **真机抽查**（真实子进程，三条分支都跑）：

  | 分支 | 结果 |
  | --- | --- |
  | 界面自己的 Popen（调用方随后把可变字段清成 `None`） | `effect.ok=True`，进程确实退出 |
  | 外来目标 + creation proof | 绑定成功，`effect.ok=True`，进程确实退出 |
  | proof 对不上 | 拒绝（`process_identity_mismatch`），目标仍在跑 |

  第一行是本条要买的那条不变量：绑定认得住，与调用方此刻的字段无关。
- **两轴审查后的一处更正**：规格把「`OSError` 被吞掉」写成了一条红→绿的缺陷，实现时发现
  改前也会进 `VERIFYING`（吞掉之后紧随的观察看见进程还活着，给出 `process_still_running`），
  两边的收口相同，差别只在原因码与「要不要靠下一步来救」。所以那是**覆盖缺口**而不是行为缺陷，
  窗口层的用例本来就没写；现已补上。逐条偏离记在 spec 的「两轴审查后的收口」。

## 被否掉的方向

- **只删 `browser_enabled`、保留两个回调**：处置仍在调用方，`self.proc` 那条不变量照旧由
  窗口检查替它守着，等于只修了漂移的一处。
- **给 `terminate` 回调传入「冻结的句柄」让调用方自己拿**：等于把 adapter 的私有绑定形状
  公开出去，seam 反而变浅。
- **把 `_kill_proc` 也删掉、窄例外一并走 runtime**：窄例外没有停止目标（identity 还没登记，
  没有 `started_at`），建不起窗口也就没有绑定可交；硬塞进去要新造一个无目标的 begin，
  正是 ADR-0024 明确删掉的那个形状。
- **顺手把 `_LegacyRuntime` / `forget()` / `begin(target=None)` 一起删**：见 IS-55，混做会让
  改动面翻倍，且性质是旧决定没落地完。

完整设计、测试与删除清单见 [`.scratch/stop-effect-ownership/spec.md`](../../.scratch/stop-effect-ownership/spec.md)。
