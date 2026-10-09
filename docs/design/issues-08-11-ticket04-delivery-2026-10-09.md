# 票04交付：开始页手动打开专用Edge，关闭后才能开始

日期：2026-10-09（Asia/Shanghai）。仅票04隔离分支交付；不是主仓集成、真实登录成功或issue08/11关闭。

## 来源与范围

- 主仓/本地工单/spec/永久证据根：F:\AI\projects\bestseller。
- 实施仓：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller。
- 分支codex/issues-08-11，开工HEAD 802ee29f993b0fe3e642ef5b6145315cb3296c3a、干净，含票01–03交付；未从main另建分支。
- 实现9a844081bed7d95fb27166c431f51a13a20941fc；审查修复0d2a5544b757a98d38bb53efdb09c62b8c680374；浏览器安全补强faa2b0ca11f591fc9f8792e2a56a8ff156db7d36；最终代码b861876a6b41d45c71c0e4dabf82affe953248f7（刷新基准与公共Config夹具兼容修复）。
- 不实施票05–07，不重设计票02身份验证，不自动合main，不修改历史诊断/manifest/原始HTML/active-run，不把父issue标为处理完成。

## 逐项自动验收

|验收|结果及公开接缝|
|---|---|
|08-A09|真实ui_live.html点击open_login_browser，配置对应Edge命令和1688主页；不采集、不建轮，刷新无启动。|
|08-A10配置一致|Config规范化exe/profile/root，GUI快照传实际run CLI；配置变化在GUI、子进程加载、采集锁内及浏览器建立前重新核验；GUI新建必须OWNED。|
|08-A10部署|含空格/中文临时迁移根与cwd变化；壳resolve_project_root→child_command→真实配置→实际页面点击回放。沿用源码部署壳，无新打包体系。|
|08-A11重复并发|API锁内预约、锁外创建；opening可读取；重复或已有同目录实例只提示已有窗口，不另开标签；Popen退出不作为释放证明。|
|08-A11/12占用|真实OS适配器解析进程参数和监听端口；目录锁、独占元数据句柄、后台进程、读取/权限错误/未知拒绝开始；未知exe明确同目录也occupied。|
|08-A12门禁|collecting、真实identity等待、停止/收尾、关闭、纯汇总角色及明确暂停时页面/API均拒绝；票01原终态/归属/锁门禁保留。|
|08-A13归属|手动浏览器由用户关闭，GUI关闭不关它；重开观察；日常profile配置先拒绝、零observe；未知端口不接管；CDP PID/proof不一致零页面、零导航、不关闭foreign Browser，仅清本次创建资源。|
|08-A14错误/CLI|缺Edge、坏配置、false模式可见准确拒绝，不改toml；普通CLI无GUI快照仍沿用原外部连接模式。|
|状态事实|free/opening/occupied/closed/failed/unknown/port_busy；显示配置源和profile，说明独立于日常Edge不复制Cookie，不显示账号登录结论。|
|部署依赖|模块/HTML处于既有源码依赖，壳从exe位置认根，无需新控制台登录命令。原生部署真实双击及账号登录待用户验，不以模拟exe路径冒充原生验收。|

## TDD、定向测试和新回放

永久证据 E = F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\04\20261009-ticket04-01。

- 01–17为原纵向红绿、并发opening与夹具调整过程（失败日志均保留，不冒称通过）。18跨票回归发现试采CLI需新OS接缝；19修复后串行定向回归通过。不得把旧票结果冒充本票。
- 20日常profile保护红→21绿：本票18项、身份生命周期28项通过，实际identity等待中追加API/page拒绝断言，不再用循环label冒充身份事件。
- 23审查P2红→24绿19项：未知镜像同目录原返回free；现经公开get_start/open_login_browser/start_run验证occupied、不创建、不建轮。
- 25非法日常配置仍observe红→26绿19项：配置错误先拒绝、不查询profile。
- 首轮全量发现刷新基准缺chrome_path与公共Config夹具缺gui_browser_settings两处真实兼容回归，b861876修复；30-full-compat-green.log记录基准2、夹具7、本票19项通过。`test_matching_commit`既有硬结束用例首轮未完成判断，29日志独立复跑1项通过；该结果不等于全量通过，最终全量另核。
- 原有test_config 23项、test_browser_pw 4项以及test_gui/试采/elapsed/abort/pause/identity/CLI/session单文件离线回归；确切日志见E。compileall与git diff --check通过，项目无单独类型检查配置，本次不伪称静态类型检查。
- 22初候选页面截图/JSON；34-final-replay.json与34-replay.py为最终b861876的可复查离线回放：实际HTML/API/Config/views/临时SQLite，仅OS创建/事实适配，free→occupied→closed；round_counts=[0,0,0]、creations=1、crawler_started=false。截图34-final-occupied.png/34-final-closed.png；28版本保留为前一候选回放。
- 迁移exe定位测试仅构造exe位置并执行真实壳定位/命令接口，不执行生产exe、不使用真实账号。真实机器人工验收尚未完成。

## 分段全量

初候选9a84408：full-suite-01退出1，合计1599/1599、6/7段OK、701.9s、FAILED（failures=2, errors=1）；完整失败日志保留。两处真实兼容回归已修复；硬结束时序用例单独复跑通过，但不能抹去首轮失败。

最终b861876候选：full-suite-02退出0，无-f，六重段与93文件其余段串行覆盖全量，包括首轮硬结束失败用例。本票结果不替代票07联合验收。原始summary：

```text
分段全量测试 2026-10-09 14:55:20
仓库：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\04\20261009-ticket04-final-02\bestseller
传参：python tools/run_full_suite.py --log-dir F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\04\20261009-ticket04-01\full-suite-02

test_exchange：65 项，OK，90.5s；日志 seg-01-test_exchange.log
test_judgment_set：50 项，OK，42.1s；日志 seg-02-test_judgment_set.log
test_plan_step：54 项，OK，24.5s；日志 seg-03-test_plan_step.log
test_export：30 项，OK，25.5s；日志 seg-04-test_export.log
test_git_channel：17 项，OK，9.8s；日志 seg-05-test_git_channel.log
test_shops_sync：17 项，OK，10.2s；日志 seg-06-test_shops_sync.log
其余段（93 文件）：1367 项，OK，505.7s；日志 seg-07-其余段(93_文件).log

合计 1600 项（预期 1600，计数对账一致），总耗时 708.3s，段结果 7/7 段 OK，结论 OK
```

永久证据：E/full-suite-02/summary.txt及七段日志；首轮失败分类见E/37-first-full-failure-analysis.md。人工真实登录与原生exe双击仍待用户，不由1600项自动验收替代。

## 双轴审查

- Standards：初审、faa2b0c复审及b861876最终兼容复审，硬性违反0、可操作Fowler异味0。
- Spec：初审1项P2，0d2a554修复；faa2b0c额外配置查询安全补强及b861876兼容补齐复审通过，未解决0。
- 原报告和分轴汇总见docs/reviews/issues-08-11-ticket04-code-review-2026-10-09.md，报告原件永久留E。只读最小上下文、独立轴、不合并重排；清理仅六个已完成本票交接目录，未删共享池其他任务。

## 目录与证据处置

- 实施副本：上述01/20261008-postmerge/bestseller，保留用于后续串行票。
- 测试副本一：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\04\20261009-ticket04-01\bestseller，从802ee29独立clone并点名补本票候选；source-manifest.json记录9a84408内容，后续修复未覆盖运行中的副本。
- 最终副本二：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\04\20261009-ticket04-final-02\bestseller，独立clone faa2b0c；27-final-source-manifest.json核验319份跟踪文件，保留双hash（既有文本CRLF规范化比较）；点名补tools/bench_refresh.py及tests/helpers.py构成b861876，33-final-compat-manifest.json记录哈希，测试运行时未覆盖文件。
- 两副本config.toml/analysis.toml从examples派生、报警false、相对路径全部副本内、无真实exe/DB/profile/凭据/venv复制；shops.csv离线测试店。沿用现有安装依赖。
- 永久保留：E全部红绿/回放/源码清单/分段日志/审查报告；初回放工具截图同时在主仓.scratch/tool-output/issues-08-11-ticket04-20261009。证据不随Git交付，跨机器需显式携带/归档。
- 可删除候选：无。
- 暂留待确认：两票04测试副本均保留，待人工/后续集成核验且用户明确授权后再评估。
- 实际副本清理：未执行。

## 部署机人工步骤与局限

1. 使用准备好的完整部署目录（源码、config、依赖、WebView2与dist/inventory_fetch.exe），本机配置独立profile及正确Edge路径，start_browser=true；不复制日常Cookie/profile。
2. 双击dist/inventory_fetch.exe；开始页核对配置来源/专用目录，点击“打开专用Edge / 登录1688”。此时不采集也不建轮。
3. 用户在新专用窗口手动登录/核对账号/处理验证。GUI只显示进程事实，不宣称账号已登录。
4. 自行关闭该专用窗口及仍持有专用目录的后台窗口；不要结束日常Edge或未知实例。
5. 点刷新，确认未占用/已关闭；若unknown/权限不足/占用，按提示安全退出和重试，不以端口或启动命令退出判成功。配置修改后重开GUI。
6. 原有周计划等门禁满足后，由用户明确选择店铺再开始。实际采集/身份等待和联合验证仍留用户/票07验收，本次自动化未启动真实采集。

已知局限：Windows OS事实查询失败保守拒绝（含不可读浏览器命令行、孤儿后台进程）；这会宁可拒绝也不猜测释放。未提供账号登录检测/自动验证、暂停后手动登录衔接（票05）、完整异常旧轮恢复或票07联合验收。未合main、未部署原生真实登录，不把issue08/11标已处理。
