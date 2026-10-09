# issues-08-11 票 07 联合收尾：两轴 code-review（2026-10-09）

- 固定基线：`69066da421b255168884224a20a28fb20b6d4713`（票 01 固定来源；覆盖整个联合交付，30 个提交）。
- r1 评价对象（先行候选）：`0e2fee3e97dad6d744df2704b645951002d36df8`。
- 整改：`be8fda6`；复审补强：`fe36da7`（仅新增一个覆盖余量用例，产品代码在 `be8fda6` 后不再变化）。**最终候选：`fe36da7`**。
- 方式：两轴各自独立只读子代理（最小上下文、文件交接、不写代码不提交、两轴不合并排序）；任务文件与报告原文归档在票证据 `review-r1/`、`review-r2/`。静态审查不替代测试与人工验收。

## r1 · Standards（固定点 69066da4 → 0e2fee3）

- 硬性违例 **0 条**（文档落点、ADR 替代节体例、CONTEXT 术语、证据处置清单、真实接缝与夹具复用惯例均核过）。
- 基线 smell 8 组（判断项）：① `stop_request.close_browser` 与 `crawler_identity.settle_dead_registration` 的「已退出/已被接管」结账判据两处重复；② `gui.py` 四份逐字守卫前导块（票 06 复审已接受延后）；③ 验证事件 note 键值格式无单一持有者（`guard.py` 写、`views.py` 反解析）；④ `vtype_name == "identity"` 五连判；⑤ `PausedBrowserOS`/`BrowserOS` 测试夹具三份近逐字复制；⑥ 两处小重复；⑦ 部分新增注释中英混排（沿袭性）；⑧ `test_manual_edge.py` 排版体例。
- 报告结论：①、②、⑤ 最值得收；无硬性违例。

## r1 · Spec（固定点 69066da4 → 0e2fee3）

- **无阻断项**；(c) 与 spec 条款相悖 0 条（抽查身份验证生命周期、先提交者胜、持锁复核、四店原范围、abort 冻结、非目标等均一致）。
- (a) 缺失/半做 1 条：spec「可用性」表行 59「已有终态但停止/浏览器收尾仍未完成 → 手动登录入口禁用」在「界面重开后只剩收尾欠账」态未落实（入口仍可点；新建/继续仍被拦，影响有限；无入口禁用断言）。
- (b) 范围蔓延 1 条：新终态 `STARTUP_FAILED` 未在 spec §9 枚举（服务 spec §7「起采失败不留下虚假的运行/可续跑状态」，与 ADR-0025 补充一致，属可辩护落地，需收口确认口径）。

## 整改与复审

### `be8fda6`（r1 整改）

- Spec (a)：`gui.Api._login_refusal` 末尾补「停止/浏览器收尾未结清」门禁——复用 `crawler_identity.registered` + `browser_cleanup_complete`（与开始页 residue 判据同谓词），开始页与过程页按钮同读 `can_open`，结清后放行；新增红→绿用例 `test_unsettled_cleanup_debt_keeps_the_manual_login_entry_disabled`（`tests/test_leftover_rounds.py`）。
- Standards ①：判据单点化为 `crawler_identity.taken_over`（公开），`StopWatch` 的浏览器结账改走它，不再内联重写；`alive` 不可读但创建证明确证 PID 复用时由「留欠账」改为「按已退出结账」（与 `settle_dead_registration` 既有语义一致）。
- Standards ⑤：`tests/test_joint_acceptance.py` 改为跨模块复用 `test_paused_manual_login` 的 `PausedBrowserOS`（复制 3 份 → 2 份；`test_manual_edge` 的 `BrowserOS` 副本维持接受，不属本票改动面）。

### r2 复审（只看 `0e2fee3..be8fda6`）

- **Standards 轴**：① 消解（判「修正非回归」：证明分歧即原进程不在的直接证据；缺证明/登记值一律不结账不变）；⑤ 部分消解（3→2，剩余一份维持接受）；欠账门禁无新硬违例/smell；②③④⑥⑧ 维持接受（本轮 diff 未触及）；⑦ 新增注释为中文。标注一处覆盖余量：`alive=None + 证明分歧` 分支无用例。
- **Spec 轴**：(a) 通过——判据顺序无回退（closing→纯汇总机→停止相位→采集/验证→欠账），票 05「明确暂停且收尾完成后过程页入口放行」不破（NOT_STARTED/BORROWED/CLOSED 或行已删均放行），页面与服务端同读 `can_open`，测试判别成立（未改前 can_open 必为 True）；无冲突、无蔓延。(b) 同意「`STARTUP_FAILED` 可接受为已确认口径」，枚举接缝齐（`rounds.py` / `views.py` / `exchange.py`，符 spec 行 205），建议 spec §7 补一行说明——该建议的实质记入本票收口记录（spec 版本 1 是已发布的固定件，不回溯改写）。

### `fe36da7`（复审补强）

- 新增 `test_an_unreadable_pid_with_a_diverged_proof_still_settles`（`tests/test_stop_request.py`）：`alive=None`、创建证明与登记值不符时按已退出结账。该用例在整改前候选 `0e2fee3` 的副本上为红（证据 `11-red-taken-over-settle-on-0e2fee3.log`：以同一测试文件仅跑该用例，Ran 1 FAILED；跑毕副本已还原并核验干净），整改后绿（`test_stop_request` 23 项全过）。

## 结论

两轴均无未解决阻断/硬性发现；整改与复审补强后，最终候选 `fe36da7` 的联合链、回放、分段全量与审查证据一致（见 `docs/design/issues-08-11-ticket07-delivery-2026-10-09.md`）。审查过程未把报告存在当通过；部署机人工验收（08-H01、11-H01）仍待用户现场反馈。
