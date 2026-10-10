# Ticket 03 — Standards 独立审查

范围：固定增量 `67df12b98de00fe7a4910c8260eab58c27fccc4e...4bf7dfe2b6f05142830ed4f62a28fee534a09585`；不将 01/02 既有实现计为新发现。已核对实施仓 AGENTS、CONTEXT、domain.md、ADR-0005/0024/0048 与七个变更文件。仅静态审查，未跑测试或真实采集。

## 发现

**[P3] 新增续跑资格判定违反调用方注入时钟的模块边界。**

- Hunk：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\rounds.py:333–341`，新增 `open_resume(..., now=None)` 在 `current.resumable_on(now or utcnow())` 内读取挂钟。
- 依据：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\docs\adr\0005-round-identity-is-date-and-shop-scope.md` 的“决策”规定“时钟由调用方注入，模块不读挂钟”；rounds.py:3–4 也明确日期与现在由调用方传入。
- 实际路径：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\pipeline.py:285` 调用时未传 now，故正常明确续跑必走这个隐式依赖。相同数据库、request 和 intent 会因运行日期不同而改变资格结果；新增跨天测试已需 patch `rounds.utcnow` 才能控制此判定。这里报告的是明确的 Standards 违约，不声称生产跨天拒绝本身错误。
- 建议：将 now 作为领域入口的必传事实，由持采集锁的调用方在复核时获取并传入；不应使用预检旧时间替代启动时复核。

## 其他检查

未发现本增量另行破坏冻结范围、原目标停止归属或默认取消关窗契约的有依据 Standards 问题。Smell baseline：无另需列出的 possible 项；参数贯穿与新旧意图的互斥均对应本票已有需求，不据此认定 Shotgun Surgery 或 Speculative Generality。未重复列工具可覆盖的格式问题。

