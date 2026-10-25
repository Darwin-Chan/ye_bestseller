# A09 详情页未就绪：追加诊断（2026-10-25）

- Feature：`detail-readiness-recovery`；阶段仍为 grill，所有恢复策略待用户确认。
- 任务：继续查明 2026-10-24 16:40:53 轮次 #31 暂停原因，不实施修复、不提交代码。
- 关联：[grill 记录](grill.md)。本记录补充其现场证据边界，不批准重试、刷新或续跑新行为。

## 1. 本次新增结论

可以进一步限定**最终判读时读到的页面证据**，仍不能认定历史页面故障的物理原因。

现场版本的 `guard.intervention_detail` 只有在成功取得页面证据、未识别到人工介入，且满足 `identity_page or not business_page` 时，才抛出现场这句“页面上下文未知，不能确认业务页面可用”。因此，按现场版本的代码路径，最终一次判读：

1. 读取到 `document.readyState != 'loading'` 且 body 存在；
2. 成功读到非空正文，并完成后续验证容器等证据读取；
3. 没有匹配现有人工校验判据；
4. 页面身份标记为真，或者可见业务标记为假。

这些读取不是原子快照，也没有覆盖整个十秒窗口。不能进一步断言页面视觉正常、浏览器始终健康，或正文就是商品内容；“加载中”、未覆盖的拦截提示和漏识别的正常业务 DOM 都可能具有非空正文。若最终采样是空正文，代码会产生“页面尚未可靠加载”；若最终采样因关闭页签等原因读取失败，会产生“页面证据读取失败”。两者与现场最终文案不同，但不排除它们曾在窗口内更早发生。

还确认了误导性说明的来源：`PageEvidenceUnavailable` 属于 `RoundPauseRequired`，现场版本 `pipeline.stop_outcome` 的通用暂停映射将其店铺说明写为“人工介入未完成：{exc}”。这并不表示进入过正式校验等待，也不会凭这个异常自动触发校验响铃。

**尚未查明的是：该次真实页面为何没有获得可用业务页分类。** 本次没有把任何合成场景认作现场原因。

## 2. 使用现场代码，而非当前工作区推演

- 现场提交：`7261741680575bc393d39beefd304b0768616252`。
- 诊断副本由主仓本地 `git clone --no-hardlinks --no-checkout` 后 detached checkout 到该提交；未拷入主仓其他未提交改动。
- 实际调用现场 `detail_visit.begin_detail_visit`、`guard` 判读与 `pipeline.stop_outcome`；没有重写异常映射来制造结论。
- 代码定位：现场 `bestseller_monitor/guard.py` 的 `_page_evidence`（约 218 行）、`_intervention_detail`、`intervention_detail`（约 302 行）；`detail_visit.py` 的 `_settle_detail_page`（约 165 行）；`pipeline.py` 的停止映射（约 110 行）。行号仅对应该现场提交。
- 现场 Python 3.13.15、Playwright 1.63.0；本机探针 Python 3.14.7、Playwright 1.62.0、Edge 156.0.4314.42。浏览器版本及运行环境不等价，不能用探针排除现场浏览器特有故障。

## 3. 已运行的离线分类探针

使用真实无头 Edge 与 Playwright；导航由本地 route 返回固定 HTML，其他请求中止，禁用页面 JavaScript。以虚拟时钟推进就绪等待，动态场景在指定时刻替换 DOM。这样可以检查等待边界及异常分类，不能模拟真实网络、站点脚本执行或渲染性能。

九个输入各运行三次，27 次结果一致，实际耗时 68.591 秒；其中 gallery 是仓库既有业务页夹具，其余为明确构造的诊断输入。每例核验结果类、准确提示文案、导航次数、校验事件和响铃调用。暂停输入核验十秒虚拟等待。所有输入都仅有一次文档请求；未知页面没有额外导航、校验事件或响铃。

| 输入 | 结果 | 本次验证范围 |
| --- | --- | --- |
| 非空正文且有可见业务标记 | 就绪成功 | 正常对照 |
| 同一正文仅移除业务标记 | 页面上下文未知 | 业务标记缺失足以触发相同提示 |
| 非空“正在加载商品信息” | 页面上下文未知 | 有正文不等于业务就绪 |
| 身份验证标题、正文不匹配既有验证提示 | 页面上下文未知 | 即使存在业务标记，身份分类也能触发同一提示 |
| 空正文 | 页面尚未可靠加载 | 与现场最终提示不同 |
| 已关闭页签 | 页面证据读取失败 | 与现场最终提示不同 |
| 虚拟 9.75 秒补入业务标记 | 就绪成功 | 既有窗口内会继续判读 |
| 虚拟 10.25 秒才计划补入标记 | 页面上下文未知 | 到十秒出口即停止，不能等待该次计划变化 |
| 既有 gallery 业务页夹具 | 就绪成功 | 现场版本已包含该已知前端的判据 |

最后三类时间和 gallery 结果均不证明 A09 属于这些场景。特别是，10.25 秒只是合成边界输入，不是对现场页面实际恢复时间的测量。

完整命令（主仓 PowerShell）：

```powershell
python F:\AI\projects\bestseller\.scratch\tool-output\detail-readiness-recovery\00\20261025-diagnosis-01\probe_readiness.py --repo F:\AI\projects\bestseller\.scratch\sandboxes\detail-readiness-recovery\00\20261025-diagnosis-01\bestseller --out F:\AI\projects\bestseller\.scratch\logs\tickets\detail-readiness-recovery\00\20261025-diagnosis-01 --repeats 3
```

末行输出：

```text
PASS 27 probes; deterministic repeats=3; elapsed=68.591s
Historical root-cause loop: NOT ESTABLISHED (failed-page input absent).
```

又将加载文字场景收窄为单命令，已经运行并退出 0，实际耗时 9.806 秒：

```powershell
python F:\AI\projects\bestseller\.scratch\tool-output\detail-readiness-recovery\00\20261025-diagnosis-01\probe_readiness.py --repo F:\AI\projects\bestseller\.scratch\sandboxes\detail-readiness-recovery\00\20261025-diagnosis-01\bestseller --out F:\AI\projects\bestseller\.scratch\logs\tickets\detail-readiness-recovery\00\20261025-diagnosis-01\focused --case nonempty_loading --repeats 1
```

```text
nonempty_loading: PageEvidenceUnavailable; virtual=10.00s; message=页面上下文未知，不能确认业务页面可用
PASS 1 probes; deterministic repeats=1; elapsed=9.806s
Historical root-cause loop: NOT ESTABLISHED (failed-page input absent).
```

这是分类特征探针，预期异常作为特征断言被捕获，所以 PASS 表示既有分类得到验证，不表示现场缺陷已修复。它不是历史故障的 red/green 回路，也不是已批准恢复方案的验收测试。

## 4. 继续寻找真实故障输入的结果

- 直接枚举原 ZIP：只有 `raw-pages/521268027384.slider.html` 一份 HTML，属于较早的 A08 滑块；与解压后的文件 SHA-256 一致（`4ccc62ec275b9e978f5211b51ef35503105a7dc1df8d0e75b10e59164aa24cbf`）。不存在因解压遗漏而未读到的 A09 HTML。
- `facts.json` 只记录同一份已复制的 231496 字节 HTML；`screenshots=[]`、`diag_files=[]`。`raw_pages_round` 指向的现场轮次目录存在，不将缺一页误写成整轮无存档目录。
- 同机较后的 `2026-10-24-2053-m2-数据完整性核算包` 共 15 个文件，未提供 HTML、截图、HAR、trace、转储或数据库文件；不能从核算 CSV 还原故障页面。
- 本机没有现场 `D:\bestseller`，主仓也没有可供补查的运行时 `data/raw_pages`、`data/screenshots`、`data/diag` 目录。
- 已核对打包运行日志，未找到故障时间的浏览器崩溃栈。旧日志中的 traceback 属于 10 月 12 日的测试/导出等记录，启动器旧栈属于 9 月 23 日；不拿旧异常解释 10 月 24 日现场。
- A09 第 32 次访问没有确切 offerID 或详情 URL；`1058077124521` 是第 31 个成功商品，不能拿它当成失败目标重放。

可重跑输入审计：

```powershell
python F:\AI\projects\bestseller\.scratch\tool-output\detail-readiness-recovery\00\20261025-diagnosis-01\audit_inputs.py
```

审计结果见下面主仓证据路径中的 `input-audit.json`。它检查包和清单的客观内容，不声明打包工具能覆盖机器上的所有历史现场。

## 5. 当前诊断边界与下一份必要输入

按 [diagnosing-bugs](../../../.codex/skills/diagnosing-bugs/SKILL.md) 的 Phase 1，仍未建立针对**真实 A09 页面故障**、能失败并在修复后变绿的回路。该 skill 要求：“Do **not** proceed to hypothesise without a loop.” 本次不进入物理原因排序、修复或提交阶段；上面的多个输入只是证明同一句日志不足以唯一识别原因。

继续锁定真实原因，需要故障当时的 HTML、HAR/trace、带时间截图等原始页面输入，或能在 m2 重现这次访问的环境入口。截图可帮助辨认页面类别，但仅有静态截图仍未必足以复现时序。若现场材料已不存在，可在下一次复现时采集；新增采集机制需另行明确范围，当前没有获批生产插桩。

grill 原有五个问题仍待回答。此次诊断没有批准刷新、次数、等待预算、系统暂停继续资格、截图格式等行为，也没有修改 deny、人工校验、浏览器归属、榜单完成与详情阶段语义。不处理“界面 deny 次数显示 0”这一独立 issue。

## 6. 目录与证据处置

`00` 仅作此次诊断材料的归档编号，不是 implementation ticket；没有提前创建实施票。

- 主仓：`F:\AI\projects\bestseller`。
- 隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\detail-readiness-recovery\00\20261025-diagnosis-01\bestseller`。
- 来源：现场提交 `7261741680575bc393d39beefd304b0768616252`；无工作改动补入；不复制生产配置、用户 profile、生产数据库；最小配置在探针进程内构造。
- 主仓诊断证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\detail-readiness-recovery\00\20261025-diagnosis-01`；含 `classification-probe.json`、`focused/classification-probe.json`、`input-audit.json`。JSON 还记录了受测三个模块的 SHA-256。
- 永久保留（不随副本清理）：上述三份 JSON、本记录、grill，以及主仓 `F:\AI\projects\bestseller\.scratch\tool-output\detail-readiness-recovery\00\20261025-diagnosis-01` 中的 `probe_readiness.py`、`audit_inputs.py`。`.scratch` 不入 Git，尚不代表已异地备份。
- 可删除候选：无；本次不申请删除。
- 暂留待确认：上述诊断副本，用于核查现场版本与后续输入回放；由本 feature 后续工作收尾时确认去留。
- 实际清理：未执行。
- 验证范围：仅上述离线诊断探针；没有生产代码/测试用例变更，没有执行实施阶段的分段全量或双轴实现审查，没有提交。
