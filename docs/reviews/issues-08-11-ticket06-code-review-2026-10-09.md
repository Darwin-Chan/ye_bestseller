# issues-08-11 票 06 双轴代码审查（2026-10-09）

- 固定基线：`3b950c7e4096de8071f324028ff8b394442a79d9`（本票开工 HEAD，含票 01–05）。
- 初审候选：`e5bf7d23bfb255215029542f5b5103c1c885527b`（实现提交）。
- 整改候选：`f40c2ff31c99539b065829891d68903b04f5465c`（复审整改提交）。
- 方式：两个独立、最小上下文、只读子代理并行执行（Standards / Spec 各一），互不可见；只交报告，不写文件、不提交。两轴结果分列，不合并、不排序。主仓票证据原文：`13-review-standards.txt`、`14-review-spec.txt`、`15-review-reverify.txt`。

## Standards（初审：e5bf7d2）

硬性违例（文档化标准）：无——逐 hunk 对照 AGENTS.md、CONTEXT.md、ADR-0005/0009/0024（含替代段）/0028/0048 与 docs/agents/ 后无违例。

判断项（Fowler 基线 smell）：

1. Duplicated Code（较重）：「条件删身份行+停止请求」原语在 `crawler_identity.settle_dead_registration` 与 `stop_request.StopWatch._delete_target_in_transaction` 双实现；「进程已死/被 PID 接管」判定与 `_registration_may_be_live` 及浏览器侧第三份拷贝重复（与 ADR-0028 归口取向相抵）。
2. Duplicated Code（轻）：`views._pending_round_text` 与 `_pending_hint` 各写一份状态→文案规则、措辞已漂；gui 拒绝文案字面重复。
3. Doc 归属不实（轻）：`CloseSnapshot` docstring 把 `Api.close_snapshot()` 写作「本 module 的」（Api 在 gui.py）。
4. Primitive Obsession（沿旧，不建议本票动）：`CloseSnapshot.kind` 弦化、`pending_rounds` 公共包装。

## Spec（初审：e5bf7d2）

（a）缺失/半做：
1. 11-A12「进程/浏览器事实未知→保留可解释错误」半做：状态完整但进程核不实时静默 ok、无说明（gui.py:885 附近）。
2. 「可稍后重试」无产品内入口：欠账保留后轮次已终态 → pending 卡消失、`prepare_abort` 对已结束轮拒绝，`check_new` 永久拦新建而无可执行动作（重启亦然）。
3. `run_view` 跨天分支按「最新未结束轮」取轮再比 `state.round_id`——本界面暂停轮不是最新遗留时 Q10 提示不显示。

（b）范围蔓延：未发现。

（c）方式不对：
1. 终态保留 note 与收尾欠账 note 互相覆盖（两事实只剩一条）。
2. `settle_dead_registration` 的 `BEGIN IMMEDIATE` 在 try 之外（行为安全，仅风格）。

六项验收（11-A05/06/11/12/14/15）与三专项（多个遗留逐个、两类重开、取消不写终态）其余项均判通过。

## 整改落地（f40c2ff）

- Standards 1 → 新增 `crawler_identity.delete_registration`（唯一实现，`StopWatch._delete_target_in_transaction` 复用）；「PID 已被接管」统一为 `_taken_over(pid, registered_proof)`（settle 两处、`_registration_may_be_live` 同比复用）。
- Standards 3 → `CloseSnapshot` docstring 归属更正（kind 产出方写明 gui.Api 三个冻结入口）。
- Spec a1 → 进程核不实且状态完整时给出「残留登记已保留；不影响开始新轮」解释，不加 cleanup 标记。
- Spec a2 → 新增 `Api.prepare_cleanup_retry` / `confirm_abort` kind="cleanup" 分支（只清可核实的残留登记与精确绑定浏览器、不回写终态）与开始页 `#residueCard`「重试收尾核验」入口（`views.start_view` 暴露 `residue`）；欠账 note 指向该入口。轮次已终态后仍有产品内重试闭环。
- Spec a3 → `run_view` 按 `rounds.load(db, state.round_id)` 取本界面认领轮并校验未终态。
- Spec c1 → 两类 note 合并保留（notes 列表拼接）。
- Spec c2 → `BEGIN IMMEDIATE` 移入 try；「行本已不在」按已结清返回 True。
- 未动项（各轴判断项/可选）：`_pending_round_text`/`_pending_hint` 文案分载体、拒绝文案沿先例、kind 弦化（Standards 复审接受取舍）；守卫前导块 4 份（Standards 复审标判断项、可延后）。

## 复验（f40c2ff）

- Standards：三项整改逐一核实落实，无新增文档化标准违例；未动项取舍接受；唯一新生长点为守卫前导块增至 4 份（判断项、不阻合并）。
- Spec：五项整改全部落实，六项验收与三专项复扫无回归、无新范围蔓延；11-A12「解释重试」由「解释 + 可执行入口 + 核实后放行新建」闭环。

两轴复验均无应阻断候选的缺口。本报告只归档审查事实；落地情况以整改提交与交付文档为准。
