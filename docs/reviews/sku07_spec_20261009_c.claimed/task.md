ROLE: subagent（只做本文件的事；父的计划不是你的任务）
只读代码审查任务。禁止实现/修改仓库、git add/commit/stash、派活或处理票08。只允许认领本任务目录并写自己的结果报告。
源工作树：F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller
固定起点：8e84f4c46198fbe5475c389141264f020342f9f6
审查HEAD：a12fcfa9ae88731e0bb1b0161324c20c5b0eca03（以 git rev-parse HEAD 复核）
命令：git -C 'F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller' diff 8e84f4c46198fbe5475c389141264f020342f9f6...HEAD
提交列表：
67333fa test(sku): preserve manual-only retry gate expectations
679bd16 feat(sku): ticket07 durable model recovery and selective retry
规范：F:\AI\projects\bestseller\AGENTS.md；F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\AGENTS.md；F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\CONTEXT.md；F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\docs\adr\0050-sku-level-grouping-and-merchant-codes.md
技能：F:\AI\projects\bestseller\.codex\skills\tdd\SKILL.md、tests.md、mocking.md；F:\AI\projects\bestseller\.codex\skills\code-review\SKILL.md
规格：F:\AI\projects\bestseller\.scratch\sku-grouping\issues\07-model-recovery-and-selective-retry.md；F:\AI\projects\bestseller\.scratch\sku-grouping\spec.md；F:\AI\projects\bestseller\docs\design\sku-grouping-spec-2026-10-08.md
主要接缝为公开AnalysisService + 真临时库存/决定/模型缓存；只控制外部模型边界；真实headless浏览器通过HTTP服务。
任务池在 F:\AI\projects\bestseller\work\tasks，按AGENTS原子认领最旧未领目录。没有目录则直接结束，不能发明新任务。报告完成直接final并退出，不等兄弟。返回 RECEIVED:<任务id> 和 DONE:<结果绝对路径>。轴：Spec。报告缺失/部分、范围膨胀、实现错误，引用规格要求，400词以内；重点找实际错误，不只看测试名。
用户扩展要求（与票07同等权威）：
1 模型未配置/不可用/失败/主动停止都不能改变同码异码规则或有效人工归属/排除。失败不能覆盖有效历史判断、移走合法成员；无归属才独立；未完成不是非同款。
2 草稿与页面分别持久化缺证据、需配置、模型不可用、调用失败、停止、成功无法唯一推荐、确定非同款、有效同款；成员完成/未完成原因、继续处理能力、当前归属来源可辨。
3 图文和推荐部分成功保留理由和候选；失败/停止只能补未完成，不能重复调用/付费；无法唯一推荐属于成功不得重试；完成不能被未完成覆盖。
4 重试前重验身份、商品/SKU名称、图片/来源、候选组/成员/证据、人工排除/绑定/拆组、规则、缓存语境、分析区间与快照。变化时只做当前需要调用，旧结果不可强套新语境。
5 特别覆盖失败/停止后排除、移组、建立边界、改变候选和证据。重试不能放回排除或移出的成员。
6 使用已有运行锁/停止令牌，迟到结果不绕过停止/当前人工决定/分析边界，不写错快照，旧运行不改变新分析。两条分支都需要生命周期闭环。
7 真实页面可开始判断、观察部分失败/停止/需配置、恢复后选择性重试，归属/任务与成员数量/逐项原因/可重试/缓存与新增调用与公开快照一致；合法分组仍标未确认。
8 禁止改门槛、召回预算、人工优先级；不得提前实现票08共享或新格式；不新增伪置信度或百分比；不全量重试掩盖缓存问题；不大重构或真实付费验收。
9 测试至少覆盖两条分支的不可用、部分成功、停止、存取草稿、只补未完成调用计数、成功不确定不重试、原因区分、人工排除/绑定/移组、候选/证据/规则/人工约束变化失效、迟到保护，以及真实浏览器路径。父映射T18/T14/T19/T29。
产出：F:\AI\projects\bestseller\work\results\sku07_spec_20261009_c.md

环境：普通exec初始化失败，必须用functions.exec_command sandbox_permissions=require_escalated附窄范围justification；父agent和初审已成功，禁止用Node绕过权限。
复审当前完整固定基线差异，不能只看整改提交。附加提交：a387d27 fix(sku): retain valid historical membership and allow evidence recheck
67333fa test(sku): preserve manual-only retry gate expectations
679bd16 feat(sku): ticket07 durable model recovery and selective retry
初审报告与整改依据：F:\AI\projects\bestseller\.scratch\logs\tickets\sku-grouping\07\20261009-sku07-01\reviews\sku07_spec_20261009_a.md；F:\AI\projects\bestseller\.scratch\logs\tickets\sku-grouping\07\20261009-sku07-01\reviews\remediation.md。确认本轴整改，继续找其它问题。
ADR正确路径：F:\AI\projects\bestseller\docs\adr\0050-sku-level-grouping-and-merchant-codes.md（副本无此编号，不补文件）。


本轮为最终完整复审，先读上一轮结果 F:\AI\projects\bestseller\.scratch\logs\tickets\sku-grouping\07\20261009-sku07-01\reviews\sku07_spec_20261009_b.md。新增整改：有效历史推荐审计通过完整旧候选语境及当前任务签名核验后复用；不请求新模型。新增服务与真实浏览器回归。settings添加member_kind供SKU/旧商品停止文案分流；保持旧产品行为。需审查全部固定基线差异，并重点核验整改的候选/人工/模型变更失效边界。运行 git log 获取准确提交列表。前述副本ADR不存在，请读取主仓 F:\AI\projects\bestseller\docs\adr\0050-sku-level-grouping-and-merchant-codes.md。普通 exec 如sandbox初始化失败，请用require_escalated和明确只读justification，不尝试绕过审批。不得改实现文件或任务状态，报告写 F:\AI\projects\bestseller\work\results\sku07_spec_20261009_c.md。
