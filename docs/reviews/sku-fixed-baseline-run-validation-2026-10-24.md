# 固定配置 SKU 基线运行工具：第二步验证记录

2026-10-24。本轮实现独立研究工具及离线验证，不执行真实 LLM、不写运行数据库、不调参、不开始人工真值标注。真实输入未接入；第一步事实与下一步选择见 [运行条件核验](../research/SKU样本运行条件核验-2026-10-24.md)、[近期正式数据核验](../research/SKU近期正式数据运行条件核验-2026-10-24.md) 和 [使用说明](../ops/sku-fixed-baseline-run.md)。

## 交付与版本范围

新增 `tools/run_sku_baseline.py`、`tools/sku_baseline_io.py`、`tools/sku_baseline_transport.py`、`tests/test_run_sku_baseline.py` 及 `tools/examples/sku_baseline/` 五份合成夹具。未修改生产模块、共享 README/CONTEXT、第一窗口材料。新增设计和运行说明只属于本任务。

开发/全量隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-fixed-baseline-run\05\20261024-step2\bestseller`，独立 clone `--no-hardlinks`，来源提交 `d7a772046968207e8ab839154a08941e8b532086`，补入本任务四份工具/测试文件和五份示例。源码点名清单和 SHA256 在主仓证据 `source-manifest.json`。交回主仓时逐字节核对一致。提交前diff检查发现四份Python文件末尾多余空行，仅移除这些空行并同步两副本；source-manifest.json同时保留全量受测摘要和最终交付摘要。全量为清理前版本，执行语句未变，清理后另跑25项定向复核。

测试配置来自该提交的 example，未复制主仓真实配置：config.toml 设置测试机器 `sku-step2-test`、空COS桶/Chrome路径、禁止浏览器启动，所有写路径均在副本；analysis.toml 设置 disabled、offline.invalid 和专属无真实密钥的环境变量名，数据库/缓存解析到副本 data；shops.csv 仅一行 synthetic inactive shop。示例配置及测试临时配置同样使用 offline.invalid；研究工具不使用运行配置加载器。

并行任务随后提交了停止原因改动。另建当前版本隔离副本 `F:\AI\projects\bestseller\.scratch\sandboxes\sku-fixed-baseline-run\05\20261024-current-integration\bestseller`，来源 `3a1975e089fdb09789fddcb255cd368e5a17a1ef`，只补入同九份本任务文件，未复制运行配置或数据。在该版本对本工具补跑25项入口测试全部通过。主仓随后到79c14a46d92acdcfa5b32a93ebbdf87a088115c9，其相对3a1975e仅重建三只壳exe，没有生产Python源码变化。收尾分段全量的版本仍是 d7a7720 加本任务文件，不能将其描述为已对3a1975e或其他并行改动完成全量验证。

## 定向与演示结果

- 开发副本入口测试25项通过（4.786s），日志 `targeted-tests.log`。
- 当前代码兼容复核25项通过（9.896s），日志 `current-integration-tests.log`。
- 最终仅清理文件末尾空行后，在当前集成副本复核25项全部通过（4.512s），日志 `final-targeted-tests.log`。
- 三个工具 py_compile 通过，CLI --help 正常。
- 合成 CLI 演示退出0：一集两成员，一次生产视觉核验和一次生产判断，共两次模拟HTTP请求；归为一组，分母2，完整集1。模拟输入token200、输出40、总240，估算0.00050 TEST，supplier_bill=null。没有真实费用。
- 演示产物：`F:\AI\projects\bestseller\.scratch\tool-output\sku-fixed-baseline-run\demo-offline-20261024`；CLI输出存 `demo-offline.log`。合成日期、图像、名称、响应、token和价格不是真实研究证据。

测试由研究入口驱动，仅模拟HTTP传输。覆盖生产编码、召回、实际视觉核验/判断/描述/推荐解析、ModelWork缓存与组装；同码/异码/无码桥接、人工约束、缺图分母、推荐null完成、逐集隔离；费用估算/预留/上限/缺usage；中断与未知请求不重发；完成结果复用、输入/配置/源码漂移拒绝；图片摘要/证据版本篡改拒绝；真实原包边界与live授权门禁；覆盖拒绝、锁和运行路径隔离。

## 分段全量

最终收尾已完成；全量计数对账一致，原始结论FAILED，唯一失败及定向复核见下。证据根为 `F:\AI\projects\bestseller\.scratch\logs\tickets\sku-fixed-baseline-run\05\20261024-step2`。

首次按仓库命令启动后，交换台段停在 `test_missing_pywebview_names_the_fix`，日志长时间不推进。单用例诊断在禁弹窗环境下返回0而非期待的2，表明实际走了“已有实例”分支，受到全局实例锁干扰。仅停止本次已核实的测试进程树，保留原段日志，没有关闭真实程序。该次中止运行不构成全量成功证据。

重启收尾使用 `BESTSELLER_NO_DIALOG=1`、`tools/run_full_suite.py --timeout 600 --log-dir <主仓证据>/full-no-dialog`，不加-f。禁弹窗避免真实提示框阻塞，但既有断言弹窗调用数的用例会受环境变量影响。已发现交换台 `test_a_second_window_pops_and_exits_zero` 在该环境失败；单独移除环境变量再执行该用例通过（0.616s），其 `_message_box` 已被测试自身模拟，不会打开真实窗口。分别保留失败与复核日志，不把复核替换成全量全绿。

上述有时限一轮的其余段在600秒被中止，运行器汇总为“合计288项（预期2184，计数对账不一致），总耗时878.0s，段结果5/7段OK，结论FAILED”，退出码1；该轮不能作为用例集合完整执行证据。保留 `full-no-dialog/summary.txt`。随后以相同源码和安全配置、不加-f、不设置单段时限重新执行仓库分段运行器，证据在 `full-final/`，结果另见下列最终汇总。


最终一轮为 `full-final/summary.txt`，2026-10-24 18:20:26完成，退出码1。不设置单段时限、不加-f，逐段集合仍为全部2184项，1896项其余段全部通过（1054.6s）。运行器原文：

```text
合计 2184 项（预期 2184，计数对账一致），总耗时 1359.1s，段结果 6/7 段 OK，结论 FAILED
```

六个通过段为 judgment_set（72）、plan_step（60）、export（30）、git_channel（35）、shops_sync（17）及其余段（1896）。交换台74项中唯一失败仍是 `test_a_second_window_pops_and_exits_zero`：BESTSELLER_NO_DIALOG=1让弹窗调用数为0，与用例期待1冲突。此前去掉该环境变量的单用例复核通过，未修改该生产入口或测试。没有其他失败；不能将本轮描述为全量全绿，也不以这个环境失败推断本研究工具失败。工具25项入口测试在两个明确基线上均通过。

## Standards / Spec 两轴自查

依 implement 的 code-review 要求，比较基线 d7a7720 与本任务新增文件；spec 为主仓 `.scratch/sku-fixed-baseline-run/spec.md` 及01–05工单。未派发子代理，采用主代理分别进行两轴自查，**不宣称完成独立双代理审查**。交接明确限制派发且共享工作区存在并行修改，未为审查暂存、stash或清理他人工作。

| 轴 | 核对及结论 |
| --- | --- |
| Standards | 仅新增设计时工具、测试、文档；不改生产模块；使用既有 unittest/Pillow，无新增依赖；配置/运行库隔离；产物默认设计时目录；副本和主仓证据符合仓库规范。输入、HTTP账与编排分三文件；没有新增业务算法。 |
| Spec | 显式输入与人工约束；复用真实编码/召回/判断/推荐/组装；冻结配置和实际源码；分母与缺证据保留；请求先记intent；完成检查点复用；未知请求不自动重发；预算/调用门禁与未知费用；合成离线覆盖及第三步方案均已实现。真实输入和真实执行清楚标为待完成。 |

自查没有发现需要修改生产行为的阻塞问题。具体限制已写入使用说明：本地预算预留不保证最终账单绝不超限；预算/上限也属于续跑冻结指纹；未知请求无自动解锁；complete不等于穷举全对或分组正确；工具不代替完整观测及库存区间资格审计；当前真实边界仍限定旧包34/536。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`。
- 永久保留：本任务源码、版本化示例与文档；主仓票证据根及演示目录，不随副本清理。`.scratch`不进Git，跨机器保留需另外归档。
- 暂留待确认：上述两个完整隔离副本，供真实输入接入和故障复核；本任务负责人待用户确认无复核需要后再处理。
- 可删除候选：无；本轮不申请删除。
- 实际清理：未执行；未更动他人未提交文件、暂存区或任务目录。
