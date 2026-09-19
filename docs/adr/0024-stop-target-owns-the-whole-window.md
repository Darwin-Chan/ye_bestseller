# 0024. 停止目标拥有整个停止窗口

- 状态：已采用并实现（**删除清单已走完**，2026-09-19：`_LegacyRuntime`、`forget()`、`begin(kind, target)` 与 `begin` 的 `conn=None` 跳过校验通道都已删；见 [IS-55](../../.scratch/1688-inventory-snapshot/issues/55-is-55.md) 与 [规格](../../.scratch/stop-runtime-shape/spec.md)。`_kill_proc` 那条「暂停」窄例外按本条保留。**规格里的强制序列第 5 步此前没有落点**，2026-09-19 补齐：`CLEANUP_PENDING` 声明了「只在没有 B/未知接手、且原 browser binding 仍精确时重试」，实际却从不重试——走到这一步时进程已被证明退出，下一次 tick 必然判 `GONE`，而 `GONE` 的短路在 deadline 之前。现在把还欠着的那个浏览器冻结进 `StopInFlight.pending_browser`，由 `tick` 在短路之前重试；见 [规格](../../.scratch/cleanup-retry/spec.md)）
- 日期：2026-09-15

## 背景

ADR-0009 规定停止请求按 `(PID, 启动时刻)` 认领；ADR-0016 把 GUI 的 8 秒 / ACK 后 10 秒窗口
收进 `StopWatch`；ADR-0021 又显式区分“当前采集进程身份”与“停止目标”。但实现只在请求与 ACK
阶段使用冻结目标。窗口完成问的是“有没有任何采集在跑”，强制阶段重新选择 current identity，
child 与 browser 回调还读取截止时的 `Api.proc` 和 GUI 配置，最后无条件清单行事实。

隔离复现中，`begin(A)` 后让 A 退出、B 接手，再推进普通或 ACK deadline，旧 watch 都会对 B
调用进程/浏览器动作；同 PID、新启动时刻也不能幸免。目标语义因此在协作停止有效、在强制停止
失效。这违反了 ADR-0009“身份对不上一律无视”的本意。

## 决策

保留并加深 `stop_request.StopWatch`，不另建拥有 pause/abort 产品语义的 coordinator：

- `begin(conn, StopCommand)` 只接受完整 `StopTarget`，并让 A 在整个 operation 内不可替换。
- 每次 `tick()` 从一份 observation 把世界分类为 exact A、A gone、replaced by B、unverifiable。
  gone/replaced 都结束 A 的旧 operation；只有 exact A 可进入强制序列；unverifiable 保持可见并
  重试，不破坏、不清事实、不丢 watch。
- 五个无目标 callback 收成 production/recording 两个 `StopRuntime` adapter。begin 时绑定 A 的
  process handle/OS creation proof；强制只接受这个 opaque capability，不再读取截止时的
  `Api.proc` 或 current identity 选择对象。
- A 的 browser session 在身份行上条件发布 `NOT_STARTED / BORROWED / STARTING / OWNED / CLOSED`
  及启动时 port、browser PID、OS proof。GUI 当前配置与“端口占用者看起来是浏览器”都不是跨进程
  强制授权。强制前必须得到 not-needed 或 exact A browser binding。
- 强制阶段用短 SQLite 写事务重验 A、隔离 A→B 交接，并保持既有顺序：结束 A -> 收尾 A 的
  browser -> 清 A 的事实。采集侧在 Popen 前先发布 browser phase；B 仍须先登记身份再启动
  browser，所以不能插入旧事务。browser 收尾失败可以持精确 binding 重试；B 一旦接手，旧
  operation 就停止重试并留警告，不能关闭 B 已在使用的实例。
- pause 请求写入、browser facts 更新、identity/request 清理全部 compare-and-* A。运行路径不再
  使用无条件清理；CAS miss 是“那一行已经属于 B”的安全结果。
- `StopWatch.begin(target=None)` 与 `forget()` 删除。外来 crawler 身份未知时，abort 返回可重试
  错误且不先写终态；本 GUI 刚 Popen、尚未登记身份的 pause 是唯一窄例外，只结束入口当刻冻结的
  child handle。pipeline 已保证身份登记早于任何 browser side effect。
- GUI 的 start/resume 在 spawn 前推进旧 watch，未收口则拒绝本界面启动；外部 B 仍由 replaced
  分支保护。窗口数值、轮询驱动和产品停止语义不变。

`crawler_identity` 继续提供“现在是谁”的事实；“现在与冻结 A 的关系”只在 StopWatch。SQLite
直接使用临时库测试，不增加 repository port。稳定状态码由 `views` 翻译成中文，底层 module 与
adapter 不拥有展示文案。

## 结果

目标知识从“请求认 A、强制认 current”变成一条贯穿规则。A→B、ACK 后替换、同 PID 复用、身份
暂缺、配置漂移和单行覆盖都从同一个 StopWatch interface 验证；GUI 不再掌握五个动作的顺序。

安全性高于强行收尾：无法形成 A 的 process/browser binding 时不按 PID、镜像名或端口猜。极端
崩溃可能留下一个需人工处理的浏览器，但不会把 B 或用户浏览器当作 A。旧库里缺 proof 的活动
identity 也按 unverifiable 处理。

代价是 crawler identity 多一组不出 UI payload 的 browser facts，Windows adapter 要提供稳定
process proof，force 期间要持有一个很短的 SQLite 写事务；DB busy 或 proof 暂缺会让 UI 多出
`verifying` / `cleanup_pending` 状态。换来的不是更多功能，而是“旧停止不能处置新执行”这条
可证明的不变量。

## 被否掉的方向

- **只给五个 callback 补 `target` 参数**：仍有两个时刻的 `is_running/current`、请求写入竞态、
  browser publication 与进程/浏览器之间的交接空隙；错误只是更难复现，没有被封死。
- **新建意图级 `StopControl.pause/abort/advance`**：调用最短，但把轮次选择、终态、today fallback
  和界面会话事实一起拖进停止协议，破坏已有 module 分工。
- **完整 `StopCoordinator` + operation id/action history/可插拔 policy**：能支持未来多 runtime，
  但当前没有第二种运行载体或算法，是为假设扩展预付 schema 与 interface。
- **A gone/replaced 后按保存端口继续关 browser**：端口不是 ownership proof，且 B 可能已经接手；
  旧 operation 应结束，不用 liveness 名义扩大破坏范围。
- **身份未知仍先写 abort 终态**：不知道运行者就也不能可靠选择它的轮次；重试前写终态会把
  “没有目标”伪装成成功停止。

## 与既有 ADR 的关系

- 延续 ADR-0009 的协作优先、8/10 秒窗口与“请求按目标认领”；把其中“身份不一致一律无视”补到
  强制与清理阶段。ADR-0009 承认的“GUI 当前端口”缺口由 target-bound browser facts 关闭。
- 延续 ADR-0016 的 StopWatch seam，但以一个 target-aware runtime adapter 取代五个无目标回调；
  不把轮次终态搬进 StopWatch。
- 延续 ADR-0021：`crawler_identity` 只提供 current fact，StopTarget 仍是独立概念。本条给 identity
  增加运行时 proof/browser facts，但 UI payload 仍只有原来的身份字段。
- ADR-0019 的正常 owner-process 收尾语义不变；GUI 跨进程强制路径提高授权门槛，只使用已发布的
  exact binding，不用当前 GUI 配置或裸端口回退。

完整状态矩阵、interface、测试与删除清单见
[`.scratch/stop-target/spec.md`](../../.scratch/stop-target/spec.md)。
