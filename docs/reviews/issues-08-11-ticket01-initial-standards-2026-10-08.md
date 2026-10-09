# Standards review — ticket 01

范围：`69066da421b255168884224a20a28fb20b6d4713...bc6cc8a296a165dc43b393ab0085f3a020403f5e`；仅 Standards，不评估产品规格。

## 已确认的硬性违例

未发现可由本次增量确认的硬性违例。新增回归主要通过实际 HTML、Api/views、临时 SQLite 和持真实采集锁的公开 `run_round` 验证；OS/browser/time 替身属于任务允许的边界。未把既有内部 mock 测试作为新增违例。

## 判断项（非硬违例）

### [P3] Duplicated Code：浏览器收尾资格在三个模块重复定义

- `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\crawler_identity.py:87`：`who.browser_state in ("NOT_STARTED", "BORROWED", "CLOSED")`。
- `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\pipeline.py:325–326`：`identity["browser_state"] in ("NOT_STARTED", "BORROWED", "CLOSED")`。
- `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\rounds.py:276`：同一集合的 `not in` 分支。

依据：任务的 Duplicated Code 基线要求“extract the shared shape, call it from both”。三处新增判定共同决定身份能否清理、浏览器债务是否阻止新建；未来改变一个状态的收尾含义，需同时修改三处，否则可能清掉另外一处仍依赖的证据。建议提取一个身份/浏览器事实的清理资格判定，保留各调用点不同的 None 与条件删除语义。不认定当前集合已经判断错误，也不要求将各调用方的不同职责合并。

## 审查边界

只读静态审查；未运行测试、真实采集、账号、联网或有声操作。红绿顺序、全量收尾与工单关闭材料不凭提交差异推断。结束时新增一个未跟踪交付文档 `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\docs\design\issues-08-11-ticket01-delivery-2026-10-08.md`；不在提交增量内，未改动或清理。
