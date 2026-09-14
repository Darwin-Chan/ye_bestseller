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

- 「谁在跑」只剩一处判据；`crawler_process` 的物理列名只在 `crawler_identity` 里出现
  （`db.py` 的表定义与 `record_/clear_` 除外）。
- 新增 `tests/test_crawler_identity.py` 十条：锁是权威、子进程兜底窗口、纯读不清残留、
  残留清理、「只有锁没有行」的占位身份、「锁在别处、行没写」的未知身份、没有进程时为 None、
  payload 形状。其中「不构造 `Api` 也能测清残留」正是改前做不到的那一条。
- `test_gui` 那条残留用例保留，继续守界面路径；`test_stop_request` 的停止目标改用
  `CrawlerProcess`。
- **行为零变化**：锁权威、子进程兜底、占位身份、残留清理时机、停止目标字段、payload 形状
  逐条照旧。全套 388 条通过、1 条跳过。

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
