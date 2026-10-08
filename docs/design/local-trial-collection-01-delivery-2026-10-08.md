# 本机试采 01：命令行闭环交付（2026-10-08）

## 来源与交付范围

- 用户授权：使用 implement，新建分支实现 01；需求与两票拆分已确认，不重新定义需求。
- 工单：`F:\AI\projects\bestseller\.scratch\local-trial-collection-impl\issues\01-cli-local-trial-loop.md`。
- 完整 spec：`F:\AI\projects\bestseller\.scratch\local-trial-collection\spec.md`；领域决定见 ADR-0049，正式判断共享继续遵守 ADR-0039。
- 仅交付命令行 01。GUI 显式入口、三页接线、跨入口监控与停止目标由 02 交付；父票、完整 spec 和 02 不关闭。
- 基线：`2b236ae9cf18c1d472f114a6ad335c8b49ae58ad`。
- 分支：`codex/local-trial-collection-01`。
- 实现提交：`173ca81b19682cd6d3ea38994fcf5cd9807a20fb`。
- 续跑审查修复提交：`a9370f411d6787016020a397da0b909812b44b0e`。
- 首次全量发现的日志隔离修复提交：`c708b4b029ab561aa763a8da8f29a4bd37ce227c`，也是最终分段全量已验证代码。
- 独立 clone：`F:\AI\projects\bestseller\.scratch\sandboxes\local-trial-collection-impl\01\20261008-163804-830\bestseller`；不切换主仓当前分支，不带入或提交其他会话未提交内容。未合并、未推送。

## 可使用的功能

```powershell
python run.py --local-trial --limit-shops A01 --pages-per-shop 1 --max-detail 10
```

以实际启用店号替换 A01；使用已有配置/profile/凭据，显式试采不改变机器身份或纯汇总机角色。
未带开关时正式采集仍拒绝，`--ignore-plan` 不绕过。操作细节见 [命令行说明](../ops/本机试采命令行.md)。

试采上下文统一派生稳定的 `data_dir` 上一级下 `local-trial` 空间：独立 SQLite、图片与SKU证据、原始页面、日志、轮次及停止请求。
保护正式写入目标、交换区、profile、配置/清单与正式分析目标；拒绝碰撞、空间不可用和既有子链接/硬链接，不回退正式库。
预检不是对运行时恶意更改文件系统的安全沙箱。截图配置隔离，但当前采集链不主动生成截图，本票不新增截图能力。

采集复用真实编排、浏览器、页数/预算、节流、人工介入及机器级互斥；不准备/同步/生成/发布计划，不写正式补采账。
同日去重只看试采历史；跳过不是库存成功。裸命令续跑当日原轮范围，停用或删除全部清单数据行不改变原范围（清单及有效表头保留）；新轮空范围仍拒绝。
同日变范围、人工中止、跨日、截止线和预算终态沿用既有规则，无清空/重采/转正式数据入口。

试采上下文不能传入导出/交换。试采不接入正式分析、不产生试采判断集。
普通汇总和正式分析不读取试采；**正式销量分析产生的判断集仍正常发布与收取**，没有关闭共享通道。

## 行为证据

接缝沿用工单已约定的 CLI、真实采集编排浏览器边界、真实 SQLite/实例锁、本地 Git 世界和假图片存储，不访问真实网站、远端 Git/COS 或真实模型。

| 场景 | 验证 |
| --- | --- |
| 默认拒绝、显式角色不变、稳定路径、缺清单/非法选店/空间拒绝 | test_local_trial_cli |
| 真实库存、图片与 SKU 证据；正式同日成功不阻止试采，试采去重不伪造新库存 | test_local_trial_cli |
| 原轮续跑、清单全停用/删行后续跑、变范围拒绝、中止后新轮、跨日和截止线 | test_local_trial_cli |
| 实际页数和详情预算、机器锁、采集身份及停止请求、原始HTML及隔离日志 | test_local_trial_cli |
| 无计划/Git/COS/正式模型副作用、试采导出与交换防误用 | test_local_trial_cli |
| 真本地 Git 交换仍汇总 m2/m3，纯汇总机 m4 发布/收取正式判断 | test_local_trial_exchange |
| 真正式 AnalysisService 输入不含试采，试采SQL dump不变，实际外发包/图片key只有正式集合 | test_local_trial_exchange |

最终功能测试 26 项 OK（24 CLI + 2 正式共享回归），见 `12-final-logging-feature-tests.log`。
相关最终回归：config 23、run_cli 17（日志修复后再次回归）、rounds 18 项均 OK；此前 helpers 7 项 OK。
续跑修复有独立两项 RED→GREEN 证据：`07-red-resume-review.log`、`08-green-resume-review.log`。
过程日志包含修复前失败，不将其伪称最终通过；失败页面测试有预期错误日志及浏览器替身重试噪声，以最终 unittest 结论为准。

compileall 与 git diff --check 通过；本机未安装 mypy/pyright且无配置好的类型检查入口，**未执行静态类型检查**，不以语法检查冒充类型检查。未安装新依赖。

### 首次收尾全量发现与修复

首次收尾代码 a9370f4：1434 项（预期1434，计数对账一致），6/7 段 OK，结论 FAILED（1 项失败）。
`full-suite/summary.txt` 及全部7段日志保留，不能称该趟通过。
失败是本票 `test_repeated_cli_logging_never_writes_formal_handler`：已有INFO级正式handler时，首次迁移日志写入正式日志。
独立复现命令 `python -m unittest discover -s tests -p test_local_trial_cli.py -k repeated_cli_logging`；显式INFO级后稳定RED。
原因：`trial_shops()` 打开并迁移SQLite先于采集日志切换，默认WARNING级的单文件测试掩盖了此路径。
以 c708b4b 提前切换试采handler，覆盖首次库准备及后续采集，异常收尾恢复既有handler与级别；不放宽断言。
RED/GREEN证据：`10-red-migration-logging.log`、`11-green-migration-logging.log`；最终26功能+17正式CLI回归OK。
修复之后才重新执行完整分段收尾；不是日常重复全量，不覆盖首次失败证据。

### 最终隔离分段全量

配置仅从本副本已追踪 examples 创建 config/config.toml、analysis.toml、shops.csv；写入路径核验在副本内。
未复制真实配置、数据库、profile或 .venv，未创建新虚拟环境。Python 3.14.7、SQLite 3.50.4、Git 2.55.0.windows.5。

```powershell
python tools/run_full_suite.py --log-dir F:\AI\projects\bestseller\.scratch\logs\tickets\local-trial-collection-impl\01\20261008-163804-830\full-suite-final
```

完整执行7段，不带 `-f`、不提前停；退出码0。验收原文：

```text
合计 1434 项（预期 1434，计数对账一致），总耗时 713.0s，段结果 7/7 段 OK，结论 OK
```

summary及7份逐段日志在上述主仓证据目录。最终代码 c708b4b；后续提交只包含交付文档，不改变已验证程序/测试。

## Standards

初审与修复后独立复审均 0 项发现，阻塞/非阻塞建议均无。按 AGENTS、布局/隔离规范、领域约定与 smell baseline 检查。
最终审查代码固定为基线到 c708b4b；静态审查不替代运行验收。
报告：`F:\AI\projects\bestseller\.scratch\logs\tickets\local-trial-collection-impl\01\20261008-163804-830\local_trial_01_standards_20261008_1745.md`。

## Spec

初审发现 1 项阻塞：当日暂停轮次的原店铺全停用或删行后，裸命令被当前空清单提前拦截。
以 a9370f4 最小修复：恢复原轮优先；空表头清单仅允许试采恢复，正式 load_shops 默认拒绝保持；新轮仍拒绝空集合。
两项真实 CLI/SQLite 回归 RED→GREEN，修复后续跑独立复审 0 项剩余发现。最后日志修复独立复审亦无发现，见下方报告。
报告：`F:\AI\projects\bestseller\.scratch\logs\tickets\local-trial-collection-impl\01\20261008-163804-830\local_trial_01_spec_20261008_1745.md`。

双轴最终计数：Standards 0 项，最严重项无；Spec 0 项剩余，最严重项无（初审续跑 1 项已修复，首次全量日志缺陷另行修复）。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`。
- 隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\local-trial-collection-impl\01\20261008-163804-830\bestseller`；来源基线、补入 spec/01票、测试配置与提交如上。
- 验收证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\local-trial-collection-impl\01\20261008-163804-830`；环境与文件哈希单独保存，逐段日志与summary独立于副本。
- 永久保留：该证据目录全部过程/最终日志、帮助输出、环境哈希、六份审查报告及任务记录；不随副本清理。
  `.scratch` 不进Git，永久保留不是已异地备份；跨机器长期备份需另行打包。
- 可删除候选：无；不将仍有独有提交的副本列为可自动删除。
- 暂留待确认：本次副本，保留供02与后续集成；只有完成集成、确认无独有提交/数据且取得清理授权后再处置。
- 未创建：新虚拟环境、诊断截图、其他诊断输出目录。未运行真实采集。
- 实际清理：未删除任何隔离副本、配置或证据；仅按AGENTS清理本次六个已完成交接任务目录，task.md/报告已归档到主仓证据。
- 集成：分支已通过本地fetch注册到主仓，主仓仍在main，不checkout、不合并、不push；交付文档收口提交也仅更新该分支引用。
