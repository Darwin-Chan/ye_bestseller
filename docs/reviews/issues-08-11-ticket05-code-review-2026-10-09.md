# 票05 两轴审查：暂停后手动处理登录，再明确继续原轮

日期：2026-10-09（Asia/Shanghai）。基线固定为本票开工 HEAD `a18f18e946faaa180c23cb66ab1e67dfae010624`；被审diff为 `git diff a18f18e...ca8dd6d`（票05首个候选提交）。Standards 与 Spec 两轴由两个只读子代理并行审、各自独立成文，**不合并排序**。

审查发现的整改提交：`1363aa9`（判据收口、性能、Spec 文案与补测）、`17649b1`（尾空行）、`bb781b0`（基准 config 替身同形）。

## Standards（仓库文档化标准 + Fowler 异味基线）

来源：仓库 `AGENTS.md`、`CONTEXT.md`（无 `CODING_STANDARDS.md`）。无硬性文档标准违反；改动读起来与周围模块一致（共用门禁、中文 docstring 密度、ADR 式注释）。以下为基线异味（判断项）。

**（a）文档化标准**

- 没有任何东西强制「不许重复」。最接近的文档化原则是 `CONTEXT.md` 反复出现的「判据只有一处」（对 `crawler_identity`、会话归属都引用过）。新代码让「手动浏览器能不能开」这一判据出现在两处（见下），故这是靠着该原则的判断项，不是硬规则。

**（b）基线异味**

1. Duplicated Code — `gui.py` `get_run`（新增，约 532–535 行）把 `get_start`（第 450 行）已有的「可开状态」判断又写了一遍：
   ```python
   browser["can_open"] = (
       refusal is None
       and browser["state"] not in ("failed", "unknown", "port_busy", "opening"))
   ```
   对 `get_start`：
   ```python
   view["browser"]["can_open"] = self._login_refusal(conn) is None and view["browser"]["state"] not in ("failed", "unknown", "port_busy", "opening")
   ```
   同一形状 + 同一字面状态集出现在两个调用点。抽一个谓词（最好放在浏览器服务里、紧挨 `status()`），让「开放态」清单只定义一次。

2. Primitive Obsession / Data Clumps — 裸元组 `("failed", "unknown", "port_busy", "opening")` 代表一个领域概念（「手动浏览器不可开」），并在两处逐字出现。给它一个名字（如 `BrowserService.can_open()`），顺带解决（1）。注意 `status()` 已经算了 `can_start`，两者容易各自漂移。

3. Mysterious Name（次要，跨页不一致）——同一事实在开始页 payload 叫 `can_open`，在过程页 payload 叫 `can_open_login`（`view["can_open_login"] = browser["can_open"]`），过程页视图把同一件事存了两份。一个概念一个名字会更清楚；未文档化，仅属判断。

`ui_live.html`：过程页卡片对任何进行中的轮次都显示 `"block"`，而文案写的是「暂停后处理登录」；采集中按钮保持禁用，所以这是文案/UX 不匹配而不是标准违反——留给 Spec 轴，不在此处记。没有 shotgun surgery / divergent change 模式：GUI+页面的改动属于一个 UI 特性，符合预期。

## Spec（工单与联合 spec）

对照工单 `05-paused-manual-login-resume.md`（ID 08-A10/11/12/13/14、11-A01/03）与父 `spec.md`。

**（a）缺失 / 部分**

1. **08-A13 未新增验证。** 工单列了「08-A13：不误关或接管日常 Edge、手动实例及未知浏览器；采集只建立并处置可证明属于本次采集的实例」。diff 未在暂停语境下新增归属安全测试，完全依赖票04未改的服务。作为继承可接受，但该 ID 被声称却无新证据。
2. **11-A01「停止/收尾期间继续被拒」此处未演练。** 工单原话：「11-A01…停止/收尾期间继续被拒」。新测试只断言停止/收尾期间登录**入口**被禁（不是继续被拒）；继续于停止中的门禁是既有的（gui.py:662）。票05没有测试在停止中驱动 `resume_run()`。
3. **「验证自行消失 / 登录失败 / 取消继续」另一半未测。** 工单：「明确暂停后即使验证自行消失也不自动恢复；手动登录失败或用户取消继续不影响原轮及保存进度」以及「取消关窗保留暂停」。当前只覆盖了「关窗不自动继续」与确认关窗（`test_confirm_close_still_ends_the_paused_round`）；验证自行消失那一半本票明确不依赖 02 而缓办，但手动打开失败 / 取消关窗未测。

**（b）范围蔓延** — 无。diff 严格留在票05：放开暂停页入口（gui.py 移除 `user_paused` 拒绝）、在 `get_run` 暴露 `browser`/`can_open_login`/`continue_gate`、用 `_browser.start_error()` 给 `resume_run` 加门禁、加页面卡片。未触碰身份验证生命周期（02）或异常/跨天处置（06），符合要求。

**（c）实现了但不对（次要）**

- **ui_live.html 的门禁说明会误导。** 暂停横幅在 `d.continue_gate` 前硬编码了前缀「继续前请先关闭专用 Edge 并确认目录已释放：」。但 `continue_gate = self._browser.start_error()` 也会承载**配置已变化**的拒绝（`test_changed_browser_config_refuses_login_and_continue` 已证 gate 含「配置」）。spec 08-A10 要求「说明处理方式」；配置变动时页面却先叫用户去关 Edge——错误的补救——再附上真正原因。按 gate 成因拆分前缀可修。

对 08-A10/11/12/14、11-A03 及配置/构建路径的覆盖是扎实的，含真实调度（`test_manual_login_then_explicit_continue_reaches_real_scheduling` 验证 A/B/C 被跳过、D 的 501/502 实际执行、成员保留、轮次 COMPLETED）。

## 整改落实（主 agent 记录）

- **Standards 1+2**：新增 `Api._browser_entry(conn)` 一处给出事实、入口门禁与拦停原因；被拦状态收成具名常量 `_BROWSER_ENTRY_BLOCKED`；开始页与过程页共用。`1363aa9`。
- **Standards 3**：过程页不再另存 `can_open_login`，页面与开始页同读 `browser.can_open`（无浏览器事实＝不可点）。`1363aa9`。
- **性能（审查外、自查发现）**：过程页每 2 秒轮询，真实浏览器事实要跑一次秒级进程枚举；整改后只在「明确暂停且收尾完成」这一格查一次，采集中不查，并加回归用例把「采集中零查询、暂停态每次刷新恰一次」钉住。`1363aa9`。
- **Spec（c）**：暂停横幅去掉硬编码补救前缀，原因原样说明。`1363aa9`。
- **Spec（a）2、3**：补 `test_continue_is_refused_while_the_pause_is_still_stopping`、`test_failed_manual_open_leaves_the_paused_round_untouched`，并让关窗用例覆盖「手动窗口开着时取消关窗仍保留暂停与原窗口」。`1363aa9`。
- **Spec（a）1（08-A13）**：判为票04服务的继承覆盖，本票不新增归属测试；在交付文档「未在本票新验」中点名，不冒充新证据。
- **全量暴露的兼容回归**：`get_run` 现在也走 `_login_refusal`（读 `cfg.role`），基准脚本的合成 config 替身缺该项，`test_bench_refresh` 真实红；按票04先例把替身补成与真 Config 同形（`bb781b0`），基准口径不变。
- `git diff --check` 尾空行（`17649b1`）。

整改后重跑：本票 13 项、`test_manual_edge` 19 项、`test_bench_refresh` 2 项、`test_gui` 刷新成本用例，均绿；最终来源证据见票证据目录。
