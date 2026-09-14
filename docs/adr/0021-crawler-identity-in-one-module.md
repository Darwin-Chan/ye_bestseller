# 0021. 采集进程身份收成一个 module：`crawler_identity.current()` / `is_running()`

- 状态：已接受
- 日期：2026-09-14

## 背景

「现在是谁在跑？他算不算在跑？」原先散在 `gui.Api` 上四处，外加采集端一处：

| 位置 | 它回答的那一块 |
| --- | --- |
| `Api._own_crawler_alive()` | 本界面拉起的子进程还活着吗 |
| `Api.any_crawler_running()` | 会话锁（权威）+ 自有子进程兜底 |
| `Api.crawler_identity(conn)` | 库里那行身份 + 「锁不在就顺手清残留」+「行还没写就用自有子进程凑占位」 |
| `Api._stop_target(conn)` | **自己写** `SELECT pid, started_at FROM crawler_process WHERE id=1` |
| `stop_request._mine()`（采集端） | 又读同一行、按 `identity["pid"]` 取键 |

同一个数据层事实因此有两条读法（`any_crawler_running()` 折身份 vs `_stop_target` 私写 SQL），
物理列名漏到界面与停止协议两处；不对称的旁证是：`StopWatch` 那边注入一个 `identity_of`
就能测，而「锁不在就清残留」这条规则只有构造一个 `Api` 才碰得到。

## 决策

- **新 module `bestseller_monitor/crawler_identity.py`**，两个来源合成一条判据：会话锁
  （在不在跑的权威）与库里那行身份（是谁、哪一轮、什么时候起）。interface：
  - `CrawlerProcess`（frozen：`pid` / `round_id` / `started_at` / `note`，外加 `to_payload()`）；
  - `is_running(*, own_alive=False) -> bool`；
  - `registered(conn) -> CrawlerProcess | None`：**纯读**身份行（采集端认领停止请求用）；
  - `current(conn, *, own_alive, own_pid, own_round_id) -> CrawlerProcess | None`：界面用——
    没人在跑返回 None；有身份行就给那一行；只有锁、行还没写就用界面自己的子进程凑一个占位
    身份；**判定「没人在跑」时顺手清掉残留行**（ADR-0008 的规则：判据与顺手清是一件事）。
- **界面自己的子进程不进 module**：那是界面会话事实，作为 `own_*` 参数传进去——module
  因此不认识界面，也不需要构造 `Api` 就能测清残留。
- **「按镜像名核过再杀」留在 `Api`**：那是动手，要认 Windows 进程细节（`browser_proc`）。
- **`Api` 只接线**：`any_crawler_running()` 与 `crawler_identity()` 各一行；`_stop_target()`
  删掉，暂停与中止两处直接调 `crawler_identity.registered(conn)`。
- **停止目标从无名 dict 变成 `CrawlerProcess`**：`StopInFlight.target` 与 `StopWatch._note_ack`
  改用属性访问，物理列名不再漏进停止协议。
- **界面 payload 仍是 dict**：`docs/ui_live.html` 读 `d.crawler.round_id`，跨 pywebview 走
  JSON，所以出界那一步用 `to_payload()`。

## 结果

- 「谁在跑」只剩一处判据；界面与停止协议都不再写这条 SQL，`crawler_process` 的物理列名只在
  `crawler_identity.py`（判据）、`db.py`（表定义与 `record_/clear_`）与 `tools/diag_stop_request.py`
  （诊断脚本有意直接看原始列，与 `check_orphans.py` 同一类）里出现。
- 新增 `tests/test_crawler_identity.py` 十条：锁是权威、子进程兜底窗口、纯读不清残留、
  残留清理、「只有锁没有行」的占位身份、「锁在别处、行没写」的未知身份、没有进程时为 None、
  身份 → 停止目标、payload 形状。其中「不构造 `Api` 也能测清残留」正是改前做不到的那一条。
- `test_gui` 那条残留用例保留，继续守界面路径；`test_stop_request` 的停止目标改用
  `stop_request.StopTarget`。
- **行为变化的唯一一处**：界面 payload 从「整行 dump」变成概念的四个字段，因此少了没人读的
  `id`（`docs/ui_live.html` 只读 `d.crawler.round_id`）。其余逐条照旧：锁权威、子进程兜底、
  占位身份、残留清理时机、停止目标字段。全套 388 条通过、1 条跳过。

**与 ADR-0011 的关系（显式推翻一条）**：0011 决策写「`Api` 保留两处写……`crawler_identity()`
仍在 `Api`（它要问会话锁、并会清掉残留身份行，不是纯读）」——本条把判据与清残留都搬进 module，
`Api` 只剩一行接线，0011 的状态行已注明取代。0011 另否掉过「读模型返回 dataclass 再由 `Api`
转 dict」：那条说的是 `views` 的返回形状，本条没动它（`views` 仍返回 dict）；换掉的是身份的
**入参**类型，出界时才用 `to_payload()` 转回 dict。

两轴审查（这次两个子 agent 都用最小上下文 spawn，正常交付）的收口：

- **Standards 轴**：`Api.crawler_identity` 与 module 同名易混 → 方法改名 `current_crawler`；
  「停止目标」与「谁在跑」共用一个类型（`StopInFlight.target` 只认 pid + 启动时刻）→ 新增
  `stop_request.StopTarget`（含 `of()` 把身份翻成目标，顺手把 0011 时代 `_stop_target` 那道
  「pid 与启动时刻都得有」的闸补回来）；`abort_run` 不再读同一行两次；`current()` 只在真要清
  残留时才建 `Database`；`registered()` 去掉 `pid NOT NULL` 下不可达的那次判空。
- **Spec 轴**：上面那条 payload 少一个 `id` 的差别原本被写成「形状照旧」，已改成如实描述。
- 留着的一条判断：`current()` 这个名字装不下「判定 + 顺手清」两件事（清理只在判定为没人在跑时
  发生）。ADR-0008 把这两件定成一件事，所以清残留留在它里面；名字取「现在是谁」。

被否掉的方向：

- **塞进 `single_instance.py` / `db.py` / `stop_request.py`**：这条规则横跨内核互斥体与数据
  一行，塞进任何一边都只剩半条（还要把 `db` 拖进一个纯 ctypes 文件，或让停止协议背上轮次）。
- **继续传无名 dict**（报告里的备选）：改动最小，但这条候选的名字就叫「身份」——返回无名字典
  等于这个概念还是没有名字，`identity["pid"]` 那种按列名取值也照旧漏出去。
- **把 `own_*` 也搬进 module**：那要 module 认识界面会话，正好违反它「两个来源 + 一个参数」
  的分工。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「采集进程」「界面实例」「停止请求」；来源是
`docs/reviews/architecture-review-2026-09-13-r2.html` 候选 05，规格在
`.scratch/crawler-identity/spec.md`。
