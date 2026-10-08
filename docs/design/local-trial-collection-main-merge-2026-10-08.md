# 本机试采分支合并 main

日期：2026-10-08。main 起点 `8b37b81`；试采分支 `codex/local-trial-collection-01`，输入 `a611d79`。

## 冲突与处理

唯一内容冲突为 `gui.py` 的暂停请求。保留试采跨入口身份核验及成功后才记录暂停意图；
保留 main 从停止监督获取 `_paused_round_id` 的关窗轮次跟踪。
移除旧分支带回的 GUI 计时字段和结账，沿用 main 采集执行持久化时长；
模式切换清除暂停轮次引用。未整文件选择 ours/theirs，未新增生命周期定义。
页面、pipeline、views 自动合并，并通过回归核验双方行为。

测试期间主仓两次 main→main checkout 清除了 MERGE_HEAD，但HEAD/暂存清单/全部测试输入未变。
重新核验后从相同暂存树创建双父合并提交，以原main为预期旧值原子更新main引用，
避免丢失分支祖先关系或覆盖并行提交；不纳入其他会话未提交文档与未跟踪文件。

## 验证

隔离副本单文件：试采GUI20、原GUI67、elapsed9、关窗51、views27、CLI24、正式共享2项全部通过。
语法 compileall 通过；mypy/pyright未安装，未执行静态类型检查。
最终完整串行分段全量（不加 -f），进程退出0：

```text
分段全量测试 2026-10-08 20:19:57
仓库：F:\AI\projects\bestseller\.scratch\sandboxes\local-trial-collection-impl\02\20261008-main-merge\bestseller
传参：python tools/run_full_suite.py --log-dir F:\AI\projects\bestseller\.scratch\logs\tickets\local-trial-collection-impl\02\20261008-main-merge\full-suite

test_exchange：65 项，OK，87.4s；日志 seg-01-test_exchange.log
test_judgment_set：50 项，OK，43.4s；日志 seg-02-test_judgment_set.log
test_plan_step：54 项，OK，24.4s；日志 seg-03-test_plan_step.log
test_export：30 项，OK，25.1s；日志 seg-04-test_export.log
test_git_channel：17 项，OK，11.1s；日志 seg-05-test_git_channel.log
test_shops_sync：17 项，OK，9.6s；日志 seg-06-test_shops_sync.log
其余段（89 文件）：1262 项，OK，448.8s；日志 seg-07-其余段(89_文件).log

合计 1495 项（预期 1495，计数对账一致），总耗时 649.6s，段结果 7/7 段 OK，结论 OK
```

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`。
- 隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\local-trial-collection-impl\02\20261008-main-merge\bestseller`。
- 来源：main `8b37b81`独立clone（无硬链接），仅覆盖本次已暂存合并文件；`merge-input.json`记录输入SHA-256。
- 测试配置：example config/analysis TOML与shops.csv；所有写入路径在副本内，Edge绝对路径仅可执行文件。
- 主仓证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\local-trial-collection-impl\02\20261008-main-merge`；含summary、7段日志、7个单文件日志、冲突决策、静态检查边界、输入和证据哈希。
- 永久保留：主仓证据，不随副本清理；`.scratch`不进Git，未宣称异地备份。
- 暂留待确认：验收副本保留现场，待用户确认归档/清理后另行核验；此前01/02副本未改动或删除。
- 可删除候选：无。实际清理：无。
- 未创建/未复制：真实配置、真实采集数据库、登录profile、凭据、新虚拟环境和远程发布；未启动真实采集。
