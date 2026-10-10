ROLE: subagent（只做本文件的事；父的计划不是你的任务）
只读代码审查任务。禁止实现/修改仓库、git add/commit/stash、派活或处理票08。只允许认领本任务目录并写自己的结果报告。
源工作树：F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller
固定起点：8e84f4c46198fbe5475c389141264f020342f9f6
审查HEAD：67333fa（以 git rev-parse HEAD 复核）
命令：git -C 'F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller' diff 8e84f4c46198fbe5475c389141264f020342f9f6...HEAD
提交列表：
67333fa test(sku): preserve manual-only retry gate expectations
679bd16 feat(sku): ticket07 durable model recovery and selective retry
规范：F:\AI\projects\bestseller\AGENTS.md；F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\AGENTS.md；F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\CONTEXT.md；F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\docs\adr\0050-sku-level-grouping-and-merchant-codes.md
技能：F:\AI\projects\bestseller\.codex\skills\tdd\SKILL.md、tests.md、mocking.md；F:\AI\projects\bestseller\.codex\skills\code-review\SKILL.md
规格：F:\AI\projects\bestseller\.scratch\sku-grouping\issues\07-model-recovery-and-selective-retry.md；F:\AI\projects\bestseller\.scratch\sku-grouping\spec.md；F:\AI\projects\bestseller\docs\design\sku-grouping-spec-2026-10-08.md
主要接缝为公开AnalysisService + 真临时库存/决定/模型缓存；只控制外部模型边界；真实headless浏览器通过HTTP服务。
任务池在 F:\AI\projects\bestseller\work\tasks，按AGENTS原子认领最旧未领目录。没有目录则直接结束，不能发明新任务。报告完成直接final并退出，不等兄弟。返回 RECEIVED:<任务id> 和 DONE:<结果绝对路径>。
轴：Standards。逐文件报告文档标准违规（出处+规则）及Fowler异味（标为判断，不是硬违例）。仓库标准优先，忽略工具已检查项；400词以内。验收=完整审查差异，指出确切位置和风险；无问题则明确0。
异味基线全文：
- **Mysterious Name**: a function, variable, or type whose name doesn't reveal what it does or holds. → rename it; if no honest name comes, the design's murky.
- **Duplicated Code**: the same logic shape appears in more than one hunk or file in the change. → extract the shared shape, call it from both.
- **Feature Envy**: a method that reaches into another object's data more than its own. → move the method onto the data it envies.
- **Data Clumps**: the same few fields or params keep travelling together (a type wanting to be born). → bundle them into one type, pass that.
- **Primitive Obsession**: a primitive or string standing in for a domain concept that deserves its own type. → give the concept its own small type.
- **Repeated Switches**: the same `switch`/`if`-cascade on the same type recurs across the change. → replace with polymorphism, or one map both sites share.
- **Shotgun Surgery**: one logical change forces scattered edits across many files in the diff. → gather what changes together into one module.
- **Divergent Change**: one file or module is edited for several unrelated reasons. → split so each module changes for one reason.
- **Speculative Generality**: abstraction, parameters, or hooks added for needs the spec doesn't have. → delete it; inline back until a real need shows.
- **Message Chains**: long `a.b().c().d()` navigation the caller shouldn't depend on. → hide the walk behind one method on the first object.
- **Middle Man**: a class or function that mostly just delegates onward. → cut it, call the real target direct.
- **Refused Bequest**: a subclass or implementer that ignores or overrides most of what it inherits. → drop the inheritance, use composition.


产出：F:\AI\projects\bestseller\work\results\sku07_standards_20261009_a.md
