# 七项未完成 issues 诊断报告

- 日期：**2026-10-08，Asia/Shanghai**。编号沿用处理台账的 05–11。
- 本次请求：使用 diagnosing-bugs 分析全部未完成问题；**仅诊断，没有实施修复**。
- 已完成包保持 01–04；07 从“待确认”转为“未处理：测试缺陷已定位”。当前 **4 已处理 / 7 未处理 / 0 待确认**；诊断不是结案。
- [状态台账](F:/AI/projects/bestseller/docs/ops/issues处理状态台账-2026-10-08.md)（2026-10-11 该台账移至 `F:/AI/projects/bestseller/.scratch/tool-output/issues/issues处理状态台账-2026-10-08.md`，本机、不进版本库）。各包的 REPORT 及附带分析来自现场，不等于本次验证结论。

## 1. 隔离、基线与证据口径

所有实验基于独立本地克隆 **`75b13294b20332c24cdb22634146301813d1a712`**，未复制主工作区的未提交文件；配置仅由示例生成。主工作区有其他进行中的修改，本次没有暂存、提交、回退或代为收尾。本报告的源码行号指该提交；不能把结论扩大为对未提交工作或所有机器当前版本的验收。

- 隔离副本：`F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller`。
- 脚本及清单：[manifest.json](F:/AI/projects/bestseller/.scratch/tool-output/issues-diagnosis/00/20261008-124951/manifest.json)，目录 `F:/AI/projects/bestseller/.scratch/tool-output/issues-diagnosis/00/20261008-124951`。
- 证据日志：`F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951`；[环境记录](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/environment.json)。
- 环境：Python **3.14.7**，Playwright **1.62.0**，Edge **154.0.4258.62**，Git **2.55.0.windows.5**。浏览器实验均为 headless msedge。
- 未使用真实账号、登录 profile、业务数据库或交换远端；未运行真实采集；**未进行发声测试**。判断集和导出使用临时数据库、本地裸 Git 库。现场 HTML 回放关闭脚本、拦截全部请求，未向 1688 发请求。仅记录分类结果，不复制认证内容或账号文字到新日志。
- 原始 ZIP 不修改。隔离副本最终 `git status --porcelain` 为空；诊断脚本保留在明确的调试产物目录，不注入产品代码。
- 依照技能：先建立红能力并最小化，再展示排序假设，做单变量对照。**Git 历史静默退出、现场听觉失效、远端 deny 触发原因仍缺原始反馈闭环**，不编造根因。对这三项的辅助实验只证明特定机制/可观测性缺口。
- 本次不是实现收尾，未运行全量测试；下列红色退出是刻意保存的未修复复现，不是交付通过。

## 2. 总览

| 编号 | 问题 | 本次确认 | 仍未解决/缺少什么 |
| --- | --- | --- | --- |
| 05 | raw-m2 静默拉取失败 / 空导出被记失败 | 无新内容仍 push，push 前仍 pull；该 pull 失败会报导出失败。空流错误不记录 Git 返回码。 | 历史 Git 为什么无输出退出未知；“发布失败”与“无法再次确认”的政策待定。 |
| 06 | m1 未收 M4 判断集 | 接收端无 `judged-*` 本地克隆时没有来源；只恢复克隆即可导入。 | m1 尚无补克隆、真实收取、幂等及业务采纳的验收。 |
| 07 | e2e 换区间不落结果屏 | 测试直接修改后台，却隐含依赖页面稍后才读快照；读早就稳定失败。 | 测试流程需改正并回归；原 m2 环境仍需验收。不是已证实的长区间销量算法错误。 |
| 08 | 身份验证漏判 / 专用 profile 未登录仍继续 | 正文回放和真实页面守卫的离线 DOM 回放均漏判；现有 URL/文案/容器判据没有覆盖该状态。 | 判据与解除逻辑需成对修复；专用 profile 的开抓前验证和状态展示另需定义、验收。 |
| 09 | 人工校验声音提醒失效 | 注入发声异常时仅控制台 BEL + DEBUG，INFO 下不可见。 | 当时为何听不到未知；需可观测性和不依赖声音的可视提醒，真实触发验收。 |
| 10 | 第 177–180 次访问连续 deny | 配额为 180 时，第 181 次请求前才主动停顿；现场确实在四次 deny 后进入长停顿。 | 站点触发机理未知；调整节流/退避的效果不能离线保证。 |
| 11 | 只完 3 店、默认单店续跑被拒 | 已采够过滤使勾选成为原轮次子集，与严格范围门禁冲突；恢复全范围勾选即通过。 | 正常续跑入口、默认选择和文案需统一；不应删除范围保护来掩盖问题。 |

## 3. 05 · raw-m2 pull --rebase 静默失败

### 现场与退出码边界

2026-09-24 04:46 交换台导出失败，但同趟其他库成功；05:12–05:16 原命令复跑成功，未见锁或 rebase 残留。报告题目中的 **退出码 2 是交换台的整趟结论，不是已知的 Git 返回码**。不能把它当成 Git 的精确原因。

原始 Git 静默失败本次无法在离线环境重现，没有推断网络、代理、编码或进程终止原因。尝试与边界：核对原包两路空输出及原机成功重跑；开发机仅做本地通道故障注入，不连接原机远端。此前管道编码修复不构成此问题的关闭证据。

### 可复现的机制

1. 本地裸库首次导出成功；相同数据库再次导出。
2. 保持首次同步成功，只在 `push()` 前的第二次 `pull --rebase` 注入“Git 非零返回、stdout/stderr 均空”。
3. 真实导出结果：`unchanged=True, published=False, failed=True`，原样生成“本次没有新内容要发布 / 拉取失败 / git 没有输出”。
4. 无故障再次导出：`unchanged=True, published=True, failed=False`。
5. 单独错误格式探针注入返回码 **128**，消息没有 128。**128 只是实验值，绝不是历史返回码。**

排序假设（仅针对已复现的空导出分支）：①空导出仍需要再次确认远端；②其实有新包；③打包失败。对照保持包不变，只恢复拉取成功；`unchanged` 前后一致，首次及恢复导出正常，支持①、排除本实验中的②③。

源码：[GitChannel.push](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/git_channel.py):115、[_failure](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/git_channel.py):55、[_publish](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/export.py):695。空导出仍 push 是显式设计：可以补推上次遗留提交。**“无新内容”并不自动证明远端已经收到**，不能简单把所有拉取失败吞掉改成功。

### 后续与验收

- 优先补精确返回码、耗时、失败步骤、两路空/非空事实；只对相关环境取证并脱敏，别记录完整环境/认证头。
- 决策：区分“新内容发布失败”“遗留提交待补推”“内容已确认发布但本次复核失败”，是否可用警告退出需明确契约。
- 回归至少包括：空流、stdout-only/stderr-only、空导出、遗留未推提交，以及远端确认失败不伪报已发布。
- **需原机证据**：再现时同一调用的 Git 返回码、耗时、两路原始输出和父子进程状态；可申请临时精准埋点。现在不能给唯一根因。

证据：[实际空导出红复现](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/05-export-empty-red.log)、[恢复对照](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/05-export-empty-restored.log)、[错误信息探针](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/05-diagnostic-contrast.log)。

## 4. 06 · m1 没有收取 M4 判断集

### 反馈循环与最小化

真实发布端 m4 + 接收端 m1，临时缓存和本地裸库；包中只保留 **1 条判断 / 2 条证据**，无需现场的数千行或模型网络请求。

- m4 发布成功，但接收端交换区没有来源克隆：`local_sources=[]`、`results=[]`，明确说明“没有别的机器的判断库可收”。
- 只恢复 `judged-m4` 本地克隆，机器号与包内容不变：收进 m4 的 **1 条判断 / 2 条证据**，无失败。

排序假设：①缺来源克隆；②包损坏/解析失败；③m4 被当作接收端本机排除。对照只改变克隆有无，导入成功支持①，排除本实验中的②③。来源枚举不是从采集机器名册自动发现远端。

现场同样没有任何 `judged-*` 克隆；M4 在 2026-09-24 04:59:57 已发布，早于 m1 10:41 收取；包内有 4103 条判断及 590 条人工决定。**这是接收通道未启用，不是“远端没有上传”，也不是已证明的解析 bug。**

源码：[judged_repos / collect](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/judgment_set.py):458–491，相关设计为 ADR-0039。

### 后续与验收

在 m1 按上机清单补齐允许收取的判断库克隆，然后验证：收取日志点名来源和摘要、导入账、第二次收取幂等、与当前证据版本/模型署名相符的采纳，以及人工决定冲突如实暴露。**导入条数不等于全部判断都应被当前分析采纳**。本次没有替原机 clone、导入业务缓存或处理冲突，不能结案。

证据：[无克隆红复现](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/06-missing-contrast.log)、[恢复克隆对照](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/06-restored-contrast.log)。

## 5. 07 · e2e 换更长区间不落结果屏

### 原场景反馈循环

原用例：`test_new_range_reuses_confirmation_in_the_browser_and_recomputes_sales`。

- 原 unittest 调用 **3/3 通过**，耗时 5.373 / 4.760 / 4.148 秒。
- 同一方法无时序改动再跑 **8/8 通过**；累计自然执行 **11/11**。这不是修复证据。
- 只增加同步：让浏览器先拿到本次 `currentSnapshot`，再执行原用例的 `edit_group / confirm / save_draft`，**2/2 稳定失败**；断言仍是原用例第 377 行期望“畅销品”，DOM 仍为“确认同款”。额外仪表化重复也失败。
- 最小化：两个店各一商品、两个日期、一个同款组；页面先就绪，再在后台确认/保存，不需新区间或额外编辑，即可同样失败。

### 排序假设与区分结果

1. **测试绕过 UI 改后台，页面仍持旧快照** → 只重新读取快照应通过。
2. **确认或保存没生效** → 服务端应仍待确认/脏状态。
3. **落屏门禁有误** → 即使浏览器读到已保存全部确认快照，仍应失败。

实际失败同一分析 ID：浏览器 `confirmed=[false,false]`，服务端 `confirmed=[true]` 且 `dirty=false`。只在后台保存后 reload，完整原用例通过，包括更长区间的分组复用及销量重算；最小 reload 对照也通过；使用真实 UI 的“确认当前分组 → 保存分组并查看畅销品”通过。

再做反向时序控制：拦住本地 `/api/analysis?id=...` 的最终快照 GET，到后台保存后才放行，**不 reload，不改后台逻辑，完整原用例通过**。这与“先读旧快照就失败”形成直接的请求顺序对照。

### 诊断结论

**测试竞态已确认。** `dates()` 只等待 `location.hash`，而 hash 在遮罩入口设置，并不代表最终快照已读。用例随后绕过页面直接改服务端，并期望页面自行更新；终态进度轮询停止后并没有这项自动后台同步契约。绿色路径可以依赖最终 GET 恰好发生在服务端修改之后，红色路径则停留在旧快照。

失败发生在真正“换更长区间”之前；本次未证实长区间销量算法出错。此结论定位了基线中与原报告一致的失败机制，**没有还原原 m2 当时的调度及浏览器版本**；原机仍需复验。

源码：[dates 与原用例](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/tests/test_e2e_acceptance.py):86、345–397；[分析页面](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/pages/bestseller-analysis.html):847 `enterMask`、872 `pollMask`、900 `finishMask`（以函数名为准），终态读一次快照并执行落屏门禁。

### 后续与验收

- 若测试宣称 UI 端到端，确认/编辑/保存走真实 UI；若后台调用只是准备夹具，应显式重新读取快照，并把“夹具准备”与 UI 验收区分清楚。
- 不用延长超时、固定 sleep 或删除结果屏断言掩盖竞态。
- 保留新旧 GET 顺序的红能力回归，再跑完整换区间/销量重算场景；m2 留版本与独立复验记录。
- 原包附带的真实交换锁造成原生弹窗阻塞是另一环境问题，本次隔离无锁不代表它已修复。

证据：[原用例三次复跑](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/07-e2e-original-summary.json)、[增强循环汇总](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/07-timing-summary.json)、[浏览器/服务端分叉](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/07-instrumented-red.log)、[完整 reload 对照](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/07-full-reload-control.log)、[UI 对照](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/07-minimal-ui-control.log)、[GET 后读对照](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/07-held-get-after-save.log)。

## 6. 08 · 专用 Edge profile 与身份验证漏判

### 拆开两个问题

专用 profile 与桌面 Edge 登录态不同是隔离设计，不能通过共享桌面 profile 或绕过平台验证解决。**身份验证页没有停下**和**缺少开抓前登录状态可见性**才是需要分别验收的行为。

### 红能力与最小化

从现场文字去掉记录头并进一步脱敏，262 字正文 + 普通详情 URL + `captcha=False`，`_intervention_of` 返回 `None`。缩减到 17 字“为确认是你本人操作，请完成以下验证”仍返回 `None`。原回放多次稳定失败。

随后将随包 `listing_A03.html` 在无网络、无脚本的浏览器中回放，经过真实 `body_text / captcha_visible / intervention_kind`：正文包含该验证提示，`captcha_visible=False`，判定仍为 `None`。不是仅凭人工阅读 marker 表下结论。**离线 DOM 不还原现场 JavaScript 动态状态，也不证明所有未登录页面都如此。**

排序与单变量对照：

| 排序假设 | 只改一个变量 | 结果 |
| --- | --- | --- |
| 1 · 正文判据没有覆盖该提示 | “请完成以下验证”改成已有“请完成验证” | 从 `None` 变“滑块” |
| 2 · 普通商品 URL 无身份验证信号 | 正文不变，只换明确登录 URL | 从 `None` 变“登录墙” |
| 3 · 现场容器不符合当前选择器 | 正文/URL 不变，仅设 `captcha=True` | 从 `None` 变“滑块”；离线实际 DOM 则为 False |

确认的是**普通商品地址下的二次身份验证没有被现有证据分类覆盖**；不足以进一步断言动态容器为何缺失。匹配替换成“滑块”只用来验证分支，不是建议把短信/身份验证一概叫滑块。

源码：[captcha_visible / _intervention_of / resolved](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/guard.py):115、153、186 起；LOGIN_MARKERS 不含身份验证，已有 `请完成验证` 不是 `请完成以下验证` 的子串。依据 ADR-0030/0036，检测及解除必须读同一份证据。

### 后续与验收

- 为此身份验证状态定义准确类型和判据；同时覆盖 URL 保持普通详情/列表地址的场景。
- 判入等待、持续等待、验证真正解除、超时暂停成对回归；不能只补“进入等待”却在 `resolved()` 立刻判解除。
- 普通商品页、没有商品页、商品描述中偶然出现验证词等作为负例，避免把单一宽泛词当登录结论。
- 明确专用 profile 单独登录的引导；开抓前检查采用可证明的页面/账号状态，不以“有 cookie”“桌面 Edge 已登录”或“没有商品”推定已登录/未登录。
- 开抓前与运行中状态展示是后续设计及实现，当前纯页面回放不构成完整登录门禁验收。

证据：[正文复现](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/08-original-contrast.log)、[最小复现](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/08-minimal-contrast.log)、[真实守卫离线 DOM](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/08-dom.log)、[单变量汇总](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/contrasts-summary.json)。

## 7. 09 · 人工校验期间没有听到声音

### 原始根因仍停留在反馈循环阶段

现场 2026-10-04 13:35/14:27 用户未感知提示；14:31/14:33 听音探针也听不到，15:33 A/B/C 及 15:34 真实启动链均能听见。日志里的“持续响铃”只是等待状态文案，不是每次 Beep 成功的证据。

本次核对现场链路/配置/后续听音记录，**没有重放物理声音**，不能用后来的可听探针给原事件结案，也没有新假设将原因归给用户、静音、设备或启动链。必须有原现场可观测反馈才能区分。

### 已证实的辅助缺口

用假的 `winsound`（绝不出声）注入 `Beep` 异常，启用警报并捕获 stdout 到内存：

```text
beep_calls=1
fallback_bel_chars=1
INFO_or_higher_events=[]
```

这是真实 `play_alarm` 的异常分支；异常日志仅 DEBUG，在运行默认 INFO 下无法区分失败和成功。BEL 也只是向输出流写字符，不保证发声；采集子进程的输出被重定向时更不能把它当可靠提示。

源码：[sound.play_alarm](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/sound.py):22、[人工介入等待](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/guard.py):241–275、[子进程输出](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/gui.py)。现场请求的可视提醒也尚无交付验收；此实验只验证声音失败的日志缺口。

### 后续与验收 / 需要的证据

- 记录警报启用状态、首次尝试/失败/恢复、结束及汇总，失败至少可在默认日志看见；避免每 3 秒刷屏。API 返回成功仍不等于用户听见。
- 人工介入等待应有可见状态、发生时间/超时信息及浏览器操作指引；不能只靠声音。
- 自动测试用替身覆盖关闭、异常、正常调用、解除和超时，不吵用户。
- **原机需用户确认的受控真实触发**：同步的等待时间线、发声调用结果、当前进程/输出会话及听到/没听到；先获用户许可再发声。若不能提供现场，可申请补精准观测后等下一次发生。

证据：[无声音的异常探针](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/09-observability-contrast.log)；现场依据位于 09 包 `sound/analysis-for-developer.md`、`sound/hearing-tests.txt`。

## 8. 10 · 第 177–180 次详情访问出现四次 deny

### 对齐现场与策略，而非臆测站点原因

现场第 15 轮共 180 次详情访问：176 成功 + 4 deny。177–179 是 A09 同商品三次，30/60 秒退避后仍拦，店铺阈值 3 触发跳店；180 是 A10 首次 deny。整轮 4 次未达阈值 6。14:37:00 已进入 **2084.5 秒**主动停顿；14:39 用户主动放弃，**不是程序自动崩溃、也不是程序完全没执行停顿**。

本次对真实 `Pacing` 做 181 次访问、冻结休息依赖不真等待，记录每次请求前的停顿：

| 访问序号 | 请求前长停顿 | 累计模拟休息 |
| --- | --- | --- |
| 176 / 177 / 178 / 179 / 180 | 无 | 0 秒 |
| 181 | 有 | 1500 秒 |

这验证 **180 是累计阈值，停顿在下一次请求之前**，与现场在四次 deny 之后才停一致。该惰性执行是 ADR-0038 的既定决策，不是计数少一的偶发实现错误；deny 页也占一次详情访问。店铺/整轮 deny 阶梯与主动停顿是不同账。

源码：[Pacing](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/pacing.py):68/80、[统一详情访问接缝](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/detail_visit.py)、[deny 门禁](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/guard.py)，ADR-0037/0038。

### 未确认与后续

- **不能离线重现站点限流触发**，未尝试对真实站点增加压力。访问次数相关性不证明限流只由次数决定，也不能承诺改为某个新阈值即可避免 deny。
- 可确定的策略讨论：主动冷却未覆盖该次首次 deny；同商品重试及跳店后仍拦，需要评估退避/冷却在跨店或跨进程范围的语义。
- 需要脱敏的 deny 页面/响应特征和按进程的精确访问时间线、配额配置、进程重启信息；不要采集账号凭据。次数/成功数/deny 数/冷却事件和完成率要用一致口径衡量。
- 接下来先确定策略及验收指标，再用受控正常采集比较，不以一趟无 deny 当永久解决。保存现场证据以区分 deny、人工验证码和登录验证。

证据：[180 配额时序](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/10-timing-contrast.log)；现场依据为 10 包 `evidence/01-第15轮时间轴与deny序号.txt`、`evidence/03-deny账目与节奏配额口径.txt`。本实验只验证主动停顿时机，没有回放远端的四次限流。

## 9. 11 · 三店完成后默认续跑被拒

### 红能力与最小化

使用真实开始视图、GUI API、临时库、固定当天计划和已存在的轮次；仅替换进程启动并隔离系统锁，不真的采集。

- 四店轮次，三店达到当日预算：视图只默认选择第四店；`start_run` 返回范围不一致，**spawn_calls=0**，两次稳定失败。
- 缩减到两店、一店已采够，同样失败。
- 只把勾选改回两店全部范围，其余条件不变：`ok=True`，**spawn_calls=1**。

排序假设：①默认预算过滤与固定轮次范围冲突；②URL/身份不一致；③计划门禁或残留锁。相同 URL、计划、锁环境下仅改变范围即通过，支持①；拒绝文字和 spawn=0 定位在启动前的范围检查，不是子进程启动失败。

真实数据/接口链：`default_checked = 本机份额且今日商品数 < 页数×30` → 开始按钮把默认子集构成 `RoundRequest` → `check_scope` 要求与原轮次全范围一致 → 提示先中止。横幅同时承诺“按它的店铺范围续跑”，形成内部契约冲突。

源码：[默认勾选](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/views.py):267、[start_run](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/gui.py):411–454、[范围约束](F:/AI/projects/bestseller/.scratch/sandboxes/issues-diagnosis/00/20261008-124951/bestseller/bestseller_monitor/rounds.py):223–239；固定日期+范围的轮次是 ADR-0005 的设计。

### 现场恢复不等于修复

A11 人工介入超时导致前三店完成、最后一店失败；轮次仍可续跑。2026-10-04 15:50 用户已经续跑，这是临时恢复，不能据此关闭默认入口 bug。09 的提醒失效与本项存在因果链，但提醒修好也不能替代范围契约修复。

### 后续与验收

明确“续跑原轮次”与“开始新范围”两个意图：已有轮次时展示原范围/完成情况，默认续跑应无损且不会被隐式子集拒绝；显式改变范围仍需要明确放弃旧轮次的确认。**不建议删除 check_scope 或默默终止旧轮次。**

回归覆盖：部分完成、预算已满、越权补采、配置店铺 URL 变化、已有采集进程、停止仍在核验、明确中止后开新轮。验证默认返回开始页一步可续跑，并确认已完成店铺被正常跳过；本次 spawn 替身只证明启动门禁，不证明真实采集已完成。

证据：[四店复现](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/11-original-1.log)、[两店最小复现](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/11-minimal-contrast.log)、[全范围对照](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/11-all-selected-contrast.log)。

## 10. 后续处理顺序建议

这是建议，不是本次已实施动作。

1. **08、11**：修身份验证及解除一致性、修正常续跑入口。都有真实模式的稳定红能力。
2. **06**：原机补部署并验收，不能用开发机导入结果代替 m1 完成。
3. **07**：修测试同步契约；保留全部换区间/销量断言，原机复验。
4. **09**：补可观测性/可视提醒，物理声音另收用户确认的现场证据。
5. **05**：先补返回码与精准上下文；空导出失败政策由产品契约定，不吞真正未发布的错误。
6. **10**：补留证、定冷却/重试策略与衡量口径，然后在允许的正常采集范围验收；不承诺固定阈值万能。

05/09/10 的原始根因仍需要原机事件反馈。没有这些输入时，下一步只能先讨论/实现可观测性，不能声称已经定位或修复原故障。

## 11. 可直接重跑的诊断命令

以下脚本固定读取同目录 `manifest.json`；不要用新版本副本替换清单再声称重复了本实验。所有命令仅诊断；带“red”的断言失败是预期结果。

```powershell
# 身份验证最小红；相同代码的判据控制；离线 DOM 红
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\diagnostic_replay.py" 08 minimal
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\diagnostic_replay.py" 08 marker
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\identity_dom_replay.py"
# 默认续跑红 / 仅恢复全部勾选的绿控制
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\diagnostic_replay.py" 11 minimal
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\diagnostic_replay.py" 11 all-selected
# 缺克隆红 / 克隆存在的绿控制（临时数据）
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\diagnostic_replay.py" 06 missing
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\diagnostic_replay.py" 06 restored
# 页面先读则红 / 保存后读取则绿，均运行完整原方法
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\e2e_replay.py" ready-before-mutation
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\e2e_replay.py" read-after-save
# 空导出拉取故障辅助红 / 恢复控制，不是历史 Git 根因复现
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\export_empty_probe.py" red
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\export_empty_probe.py" restored
# 无声音的日志缺口探针；真实主动停顿机制
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\diagnostic_replay.py" 09 observability
python "F:\AI\projects\bestseller\.scratch\tool-output\issues-diagnosis\00\20261008-124951\diagnostic_replay.py" 10 timing
```

[初始红循环汇总](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/red-loops-summary.json)、[单变量汇总](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/contrasts-summary.json)、[最终快照/DOM 对照汇总](F:/AI/projects/bestseller/.scratch/logs/tickets/issues-diagnosis/00/20261008-124951/final-contrasts-summary.json)。保留日志内的具体命令和进程退出码，避免把运行器退出 0 误认为子复现通过。

## 12. 本次收尾边界

- 输出本报告、更新台账、保留可重跑脚本和红/绿证据；没有产品修复或提交。
- 隔离副本无产品改动，主工作区已有修改未处理；失败的首次克隆目录在 manifest 中登记，未盲目清理共享目录。
- 07 因稳定复现而从“待确认”转“未处理”；其余未完成项不因根因明确、配置控制成功或辅助探针通过而改为已处理。
- 修复阶段必须先写正确调用接缝的回归，红→改实现→绿，再重跑原场景；实现收尾按仓库规定分段跑全量。本次未假装完成这些阶段。
