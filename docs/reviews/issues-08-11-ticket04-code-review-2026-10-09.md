# 票04 code-review：开始页专用Edge

日期：2026-10-09。固定基线 802ee29f993b0fe3e642ef5b6145315cb3296c3a；初审候选9a844081bed7d95fb27166c431f51a13a20941fc；安全修复复审候选faa2b0ca11f591fc9f8792e2a56a8ff156db7d36；最终兼容复审候选b861876a6b41d45c71c0e4dabf82affe953248f7。

两个最小上下文只读子agent并行按AGENTS文件认领执行，未改代码/提交。普通sandbox setup refresh失败后使用审批执行读取；失败不是审查结果。复审认领顺序与初审不同，按任务ID收集而不按agent名字猜轴。

## Standards
初审硬性违反0、可操作异味0；最终复审0。12项Fowler基线和适用ADR逐项检查。原报告如下。

# 票04 Standards 审查

范围：802ee29f993b0fe3e642ef5b6145315cb3296c3a → 9a844081bed7d95fb27166c431f51a13a20941fc。
实施仓：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller（下述文件均相对该绝对路径）。

## 硬性标准违反
无发现。

- ADR-0007 原句：“运行时只有一份代码，漂移不可表达。” 本次未把项目代码加入壳；gui.py 的子进程仍调用源码 run.py，冻结浏览器配置来源和根目录未替换解释器选择职责。
- ADR-0008 原句：“采集锁是「有没有采集在跑」的唯一权威判据”。新增手动入口复用 current_crawler 与停止状态门禁；pipeline.py:273–274 在采集锁内复核冻结设置，不把手动 Edge 的 Popen 当成采集身份。
- ADR-0028 原句：“真正的授权检查（缺 pid 或 proof 一律拒绝）一直在 browser_proc.close_browser 里。” dedicated_browser.py:45–133 的 OS 适配只观察/创建，不增加关闭浏览器或端口终止路径；browser_pw.py:142–148 拒绝非 OWNED 的 GUI 新建会话，在创建页面前退出，未授权接管日常实例。
- ADR-0025 最新补充原句：“仍有未清理资源或无法核实时继续 UNKNOWN，保留精确旧账本重试”。新增拒绝路径沿用原会话清理与发布；未将清理未知强制转换为安全成功。
- ADR-0048 原句：“不另造一套直接杀进程的关窗机制”。本次没有更改关窗停止归属机制，也没有关闭手动浏览器的隐式副作用。

## Fowler 启发式异味
无可操作发现。逐项判断：Mysterious Name、Duplicated Code、Feature Envy、Data Clumps、Primitive Obsession、Repeated Switches、Shotgun Surgery、Divergent Change、Speculative Generality、Message Chains、Middle Man、Refused Bequest 均未发现达到报告门槛的问题。Settings 已收束共同字段；多处 verify_gui_settings 是不同竞态边界的复核，不是重复实现；GUI/CLI/会话/页面的接线改动属于同一功能，不单凭文件数量判定 Shotgun Surgery。

只读检查 diff、标准、当前相关源码及测试；未运行测试或真实 Edge 验收，未把人工验收待办视作实现缺陷。工作区 status 输出为空。skill 的主代理派活流程不适用于本任务，依 task.md 未派活。
### 最终复审
# 票04 Standards 修复复审

固定范围：802ee29f993b0fe3e642ef5b6145315cb3296c3a → **faa2b0ca11f591fc9f8792e2a56a8ff156db7d36**。包含原实现、0d2a5544 同目录未知浏览器修复及 faa2b0c 配置优先拒绝修复。读取时 HEAD 匹配该候选，工作区干净。本任务实际认领轴为 Standards，不代替另一份 Spec 复审。

## 硬性标准违反
**无发现。**

核对标准原句：
- ADR-0007：“exe 里不放项目代码”“项目根由 exe 自身位置推导”。新增模块/页面仍由源码部署目录加载，未引入第二份冻结实现。
- ADR-0008：“界面不持有采集锁”“采集锁是‘有没有采集在跑’的唯一权威判据”。GUI预检未替代采集进程互斥；最终核验仍在采集锁内。
- ADR-0028：“terminate 只对传进来的那个绑定动手”“close_browser…只看 BoundBrowser 上的 facts”。手动浏览器服务没有终止接口，修复未扩大停止授权。
- 票04工单：“新的测试注入集中在专用浏览器服务边界”；修复回归使用真实公开API，只替换OS查询/创建边界。

标准输入均位于 `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\` 下 AGENTS.md、CONTEXT.md、docs\adr；另核对0048、0049、0051适用边界，未发现关窗、试采或计时归属回退。

## 修复核对
同仓 `bestseller_monitor\dedicated_browser.py:87–101` 现对所有可读命令行解析同目录参数，不再按名称跳过；`tests\test_manual_edge.py:338–352` 经 get_start/open_login_browser/start_run 验证占用、零创建、零建轮。初审P2的具体漏判路径已关闭。

`dedicated_browser.py:163–170` 先检查配置、失败直接返回，故非法日常目录不进入observe；`test_manual_edge.py:281–295` 强化observe.assert_not_called。原后置配置复核保留，可覆盖查询期间配置变化，未发现新增授权或占用放行风险。

## Fowler启发式异味
逐项检查 Mysterious Name、Duplicated Code、Feature Envy、Data Clumps、Primitive Obsession、Repeated Switches、Shotgun Surgery、Divergent Change、Speculative Generality、Message Chains、Middle Man、Refused Bequest：**无可操作发现**。Settings已承载配置字段簇；服务/OS适配器分工符合实际接缝；前后配置检查属于边界复核，不能仅因重复调用就要求删去。

## 证据边界
只读核对主仓 `.scratch\logs\tickets\issues-08-11\04\20261009-ticket04-01\` 的23红/24绿、25红/26绿；最后绿日志为19项通过。未自行重跑测试。最终冻结候选分段全量及双轴闭环由父agent收尾，未将待办或人工登录待验列为实现缺陷。


### b861876 最终兼容复审
# 票04 Standards 最终兼容复审

范围：802ee29f993b0fe3e642ef5b6145315cb3296c3a → b861876a6b41d45c71c0e4dabf82affe953248f7；重点 faa2b0c → b861876。实施仓 HEAD 符合固定候选，读取时工作区干净。本报告仅 Standards 轴。

## 硬性违反
**无发现。**

- `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\tests\helpers.py:91` 增加 `gui_browser_settings=None`，与真实 Config 的中性默认一致，不另建配置替身。适用标准 `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\docs\adr\0022-crawler-cfg-in-one-fixture.md` 原句：“其余 `Config` 字段给中性默认，要用的用例自己传”；“替身与真 `Config` 同形”。
- `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\tools\bench_refresh.py:55–59` 只补 Api 初始化必需的浏览器配置字段；`start_browser=False`、空 executable 与独立 unused-profile 不授予启动能力。DedicatedBrowser 初始化只冻结 Settings，刷新基准仍测真实 `get_run()`，未加入创建浏览器入口。ADR-0022 原句：“`tools/bench_refresh.py` 是**工具**、不依赖 `tests/`”；本补丁没有引入 tests 依赖。
- 已核对前轮 faa2b0c 双轴报告与浏览器事实边界；本次两文件增量不改变0007源码部署、0008采集互斥、0028停止副作用归属及0048关窗政策，也未扩展票05–07。

## 判断性异味
固定12项 Fowler 启发式无新增可操作发现。工具的局部 Api 入参不是违反集中采集夹具规范的重复；新增字段有已发生兼容失败的具体需求，不属 Speculative Generality。未发现应新建抽象或扩大重构的理由。

## 证据边界
只读核对 `F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\04\20261009-ticket04-01\30-full-compat-green.log`：2、7、19项均 OK。本代理未重跑测试、未启动浏览器、未修改仓库。最终分段全量仍由父代理完成；既有 matching 用例首轮失败和独立复跑通过不等同最终全量通过，部署机人工验收也未被替代。

## Spec
初审1项P2：名称过滤漏掉未知Chromium同目录占用；0d2a554移除名称前置过滤、23红/24绿真实API回归关闭。主agent另补faa2b0c先配置验证再observe，25红/26绿保证日常profile零查询；最终Spec复审无新增、未解决0。

# 票04 Spec 审查

范围：802ee29f993b0fe3e642ef5b6145315cb3296c3a → 9a844081bed7d95fb27166c431f51a13a20941fc，仅票04；只读审查，未运行真实浏览器或采集。

## (a) 缺失或部分要求
未发现另行可操作的遗漏；全量收尾证据见下方说明。

## (b) 范围蔓延
无发现。未要求票05过程页入口或票07联合人工验收提前实现。

## (c) 看似实现但实际错误
**[P2] 同目录占用检查漏掉未知 Chromium 可执行文件。**
文件：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\dedicated_browser.py:85–89`（放行结果119行）。

规格原句：`F:\AI\projects\bestseller\.scratch\issues-08-11\issues\04-start-page-manual-edge.md`：“开始前重查实际目录占用”；“日常 Edge/未知浏览器/无关调试端口实例不关闭、不借用”。

实现枚举全部进程，却在解析命令行前仅保留 msedge.exe、chrome.exe 和配置可执行文件名。若 brave.exe/其他 Chromium 使用同一专用目录且未监听配置端口，即使 OS 查询明确返回其绝对 `--user-data-dir`，仍跳过并返回 free。目录探针只检查少数锁/元数据文件，不能保证此种进程必被挡住；随后手动打开/起采门禁会放行，违反同目录实际占用要求。

离线最小复现：python -B，模拟可读取目录与 OS 返回 `brave.exe --user-data-dir=F:\deploy\profiles\account1_edge`、无监听端口，调用真实 BrowserRuntime.observe，实得 `Facts(state='free', ...)`。未启动浏览器、未写仓库。建议先跨进程名称检查可读取的明确同目录参数，再对无法确认的浏览器归属保守拒绝；补未知 Chromium 同目录回归。

## 验收/证据边界
其余配置快照、最终启动复核、重复点击预留、GUI关闭不杀手动窗口、CLI无快照外接模式及壳源码部署接缝未发现额外缺陷。读取的票04 `20261009-ticket04-01` 证据有定向测试及 source-manifest，审查时未见分段全量 summary.txt；父agent需确认本票全量及双轴审查收尾，不能用旧票全量替代。此为证据待核，不列为实现缺陷；真实登录人工待验也不列缺陷。

### 最终复审
# 票04 Spec 修复复审

固定范围：802ee29f993b0fe3e642ef5b6145315cb3296c3a → faa2b0ca11f591fc9f8792e2a56a8ff156db7d36（含 9a84408、0d2a554、faa2b0c）。核验 HEAD 等于该候选，工作区干净。认领的是 Spec 任务，不沿用上一轮 Standards 身份。

实施仓根 R = F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller；以下 R\文件:行号指该绝对路径。
规格 S = F:\AI\projects\bestseller\.scratch\issues-08-11\issues\04-start-page-manual-edge.md，并核对联合 spec、grill 决策及交接文件。

## (a) 缺失或部分要求
无新增可操作发现。分段全量和双轴审查仍由父代理完成最终冻结版本收尾；不将待最终全量或票07部署机人工反馈作为代码缺陷。

## (b) 范围蔓延
无发现。两次修复只调整 OS 事实读取和非法配置的观察顺序，未交付票05暂停登录/继续，也未把本票自动化冒充原生登录验收。

## (c) 看似实现但实际错误
无新增发现；初审 P2 可关闭。

1. S 原句：“开始前重查实际目录占用”；“日常 Edge/未知浏览器/无关调试端口实例不关闭、不借用”。R\bestseller_monitor\dedicated_browser.py:87–101 已不在解析前按镜像名过滤。任何可读命令行明确指向同一绝对目录均返回 occupied；已知浏览器无命令行与孤儿子进程仍保守 unknown。R\tests\test_manual_edge.py:338–352 经真实公开 Api 验证未知 Chromium 同目录时不创建浏览器、不起采、不建轮。23 红日志实际 free/occupied 失败，24 绿日志 19 项通过。

2. S 原句：“Edge 缺失、配置错误和 start_browser=false 通过界面给出准确错误；GUI 手动打开及起采拒绝”。faa2b0c 的 R\bestseller_monitor\dedicated_browser.py:163–170 在 observe 前检查配置，非法配置直接 failed/can_start=false。R\tests\test_manual_edge.py:281–292 用真实 BrowserRuntime 验证日常目录拒绝且 observe 未调用；25 红显示曾调用，26 绿 19 项通过。后置复核仍保留，不放宽配置竞态安全性。

其余重复/并发预留、目录/端口安全、创建归属拒绝、规范化配置和 CLI 无 GUI 快照的外接路径未发现修复新增回归。

审查者另做无文件写入 python -B 离线复核：未知同目录 occupied、已知无cmd unknown、无关无cmd free、端口占用 port_busy、未知不同目录 free；非法配置 status/open/start 均拒绝且不 observe。全部符合预期。首次命令因引号 SyntaxError 未执行，重跑成功。未运行真实 Edge、采集或全量。27-final-source-manifest.json 明确 candidate 为上述 faa2b0c；未声称全量已通过。
## 分轴结论
Standards：发现0、未解决0；Spec：初审1项P2，修复复审关闭，未解决0。静态审查不代替分段全量、真实登录或票07联合验收。原报告永久保存于主仓票04证据目录。

### b861876 最终兼容复审
# 票04最终兼容复审 — Spec

固定范围：802ee29f993b0fe3e642ef5b6145315cb3296c3a → b861876a6b41d45c71c0e4dabf82affe953248f7；重点复核 faa2b0c → b861876。实施仓 HEAD 与最终候选一致，git status --porcelain 为空。

## 缺失 / 部分实现
无新增可操作发现。核对完整实现差异及上一轮 Spec 修复报告；本次两处兼容补齐不撤销既有配置快照、占用拒绝、窗口归属或 GUI/CLI 模式边界。

## 范围蔓延
无发现。仅基准配置替身和公共测试夹具补字段，没有扩展票05–07的暂停登录、异常旧轮处置或联合验收功能。

## 看似实现但实际错误
无新增发现。

- F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\tools\bench_refresh.py:55–59 补入构造专用浏览器服务所需配置，明确 start_browser=False。Api 构造只建立服务与设置快照；没有调用手动打开。规格原句“只读刷新不产生启动副作用”（F:\AI\projects\bestseller\.scratch\issues-08-11\spec.md:135）不被破坏；基准不会因此启动真实浏览器，亦未静默转换模式。
- F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\tests\helpers.py:91 的 gui_browser_settings=None 与真实 Config 默认值一致，保留非 GUI 的既有路径。规格原句“命令行既有外部连接能力不改”（F:\AI\projects\bestseller\.scratch\issues-08-11\spec.md:120）未被该补丁改变。

## 证据与边界
只读核验 F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\04\20261009-ticket04-01\30-full-compat-green.log：2 个基准测试、7 个公共夹具测试、19 个手动浏览器测试均 OK。29-matching-failure-recheck.log 仅证明既有硬结束用例独立复跑 1 项通过，不能推导全量通过。本代理未运行测试、真实 Edge 或采集；最终分段全量仍由父代理完成，部署机人工验收仍待反馈。未提交、未修改实施仓、未派活。

## 最终分轴汇总
Standards：硬性违反0、可操作异味0；Spec：初审P2已修复，最终未解决0。两轴不合并排序。最终全量结论只以交付文档与full-suite-02/summary.txt为准；子代理未运行全量，不推导其通过。
