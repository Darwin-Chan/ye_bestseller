# 本机试采 02：界面接入交付记录

日期：2026-10-08；分支：`codex/local-trial-collection-01`；基线：`58a513167a340bf701ae97c2bfb23348311b1388`。

## 实施范围

现有 GUI 的显式试采入口、三页同一隔离上下文、手选店与 CLI 模式/配置传递；
跨入口身份核验、暂停/中止和历史续跑，机器锁与关窗机制沿用既有规则。
新增只读跨空间身份查询，避免附着试采时创建/迁移正式库。试采 GUI 日志隔离并恢复原 handler。
正式分析和判断集共享不关闭，不新增试采分析或清空历史入口。

使用方法见 [本机试采界面](../ops/本机试采界面.md)。

## 验证与处置

实施提交：`49607b6`；审查修复提交：`3d703c0`。

单文件验证全部通过：GUI 试采 20 项、只读 OS 进程存活查询 3 项、既有 GUI 67 项、
视图 27 项、关窗 22 项、采集身份 11 项、01 CLI 24 项、正式交换与判断集共享 2 项。
测试使用公开 GUI API、实际 CLI 编排、真实 SQLite 和本地 Git；仅浏览器/HTTP/进程/OS
边界使用替身。Windows 进程查询额外验证真实当前 PID 与不存在 PID。
实际 HTML 离线 Edge 验证：入口、开始、暂停态、人工中止、结果、返回正式、重开历史均 PASS，
HTTP/HTTPS 外部访问阻断，无页面 JS 错误；开始/过程/结果截图留在主仓证据。

语法编译检查通过；mypy/pyright 未安装，静态类型检查未执行，不以语法检查替代。

### Standards

首轮 1 项 P2：GUI 新增第二套跨空间身份判据，违反 ADR-0021 接线边界。
已将模式附着与页面/停止目标统一纳入 crawler_identity；修复后独立复核无剩余发现。

### Spec

首轮 1 项 P2：正式死亡身份残留阻挡合法 CLI 试采接管。
已补先红后绿回归并以只读 OS 存活/创建证明识别确定死亡残留，不清理正式库；
活或不可核验竞争目标仍拒绝，外部目标必须存活且 proof 匹配。修复后独立复核无剩余发现。

两轴报告及任务正文分别保存于证据区 `review-*-initial.md` / `review-*-final.md`，
SHA-256 对账见 `review-sha256.txt`。两轴分别为 0 项未解决，均无剩余最高严重度问题。
主 agent 截图复核另修正试采开始页误用正式周计划说明，实际页面失败断言后修复并通过。
分段全量已完成，最终合计/对账/结论见下节，审查报告不代替全量验收。

主仓：`F:\AI\projects\bestseller`。
02 隔离副本：`.scratch/sandboxes/local-trial-collection-impl/02/20261008-gui/bestseller/`。
主仓证据：`.scratch/logs/tickets/local-trial-collection-impl/02/20261008-gui/`。
副本暂留作为分支交付源，证据保留；未授权或执行副本删除。未创建真实运行数据、远程发布或新浏览器正式 profile。
主仓其他会话未提交文件不纳入本票；未合并 main。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`。
- 隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\local-trial-collection-impl\02\20261008-gui\bestseller`；继承的 01 副本未改动、未清理。
- 来源：从 01 已验收提交 `58a513167a340bf701ae97c2bfb23348311b1388` 独立 clone（`--no-hardlinks`），同分支继续；代码/测试/说明修改见上述两提交。
- 测试配置：副本 `config` 内 example → config/analysis TOML 与 shops.csv，写入路径在副本；未复制真实数据库、profile、凭据或旧 .venv。
- 验收证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\local-trial-collection-impl\02\20261008-gui`。
- 永久保留：主仓证据中的逐段日志/summary、单文件日志、审查任务与报告、SHA-256、实际页面脚本及三页截图；不随副本删除。`.scratch` 不进 Git，不宣称已异地备份。
- 可删除候选：无；本票不授权自动清理隔离副本。
- 暂留待确认：02 副本作为分支交付源，待用户确认合并/归档后另行核验处置；继承 01 副本继续按 01 记录保留，不重写其处置。
- 实际清理：隔离副本未执行；仅本票六个点名只读审查任务及结果在证据保存/哈希校验后按交接规范清除，未清共享任务池。
- 未创建：新虚拟环境、真实运行数据库、正式浏览器 profile、远程发布/上传及额外程序；无需处置上述未创建项。

## 最终分段全量与补充复核

首轮全量 1457/1457 对账一致，但仅 6/7 段 OK，结论 FAILED：刷新基准的简化配置缺少
`local_trial` 字段，出现 1 项 ERROR。失败现场保留在证据区 `full-suite/`，不计为通过。
提交 `a906fdb` 为基准简化配置补 `local_trial=False`，仍走真实 Api/SQLite 和原断言；
`bench-red.log` 复现、`bench-green.log` 原有 2 项全部通过。补充独立 Standards/Spec
复核各 0 项发现，报告 `review-standards-bench.md` / `review-spec-bench.md` 与哈希已归档。

修复后重新完整串行运行，不使用 -f；证据 `full-suite-final/summary.txt` 及同目录 7 个逐段日志。
进程退出码 **0**；summary 原文：

```text
分段全量测试 2026-10-08 19:10:25
仓库：F:\AI\projects\bestseller\.scratch\sandboxes\local-trial-collection-impl\02\20261008-gui\bestseller
传参：python tools/run_full_suite.py --log-dir F:\AI\projects\bestseller\.scratch\logs\tickets\local-trial-collection-impl\02\20261008-gui\full-suite-final

test_exchange：65 项，OK，86.2s；日志 seg-01-test_exchange.log
test_judgment_set：50 项，OK，42.6s；日志 seg-02-test_judgment_set.log
test_plan_step：54 项，OK，24.5s；日志 seg-03-test_plan_step.log
test_export：30 项，OK，25.2s；日志 seg-04-test_export.log
test_git_channel：17 项，OK，12.6s；日志 seg-05-test_git_channel.log
test_shops_sync：17 项，OK，11.8s；日志 seg-06-test_shops_sync.log
其余段（88 文件）：1224 项，OK，455.4s；日志 seg-07-其余段(88_文件).log

合计 1457 项（预期 1457，计数对账一致），总耗时 658.3s，段结果 7/7 段 OK，结论 OK
```

因此收尾口径为：1457 项（预期 1457），计数对账一致，总耗时 658.3s，7/7 段 OK，结论 OK。
证据完整性清单见主仓证据 `evidence-sha256.json`，不含清单本身；证据未随 Git 提交或异地备份。
本次只交付本地分支，不合并 main、不启动真实采集、不发布远程。
