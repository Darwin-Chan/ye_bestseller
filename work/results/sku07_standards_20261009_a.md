# Standards 审查：票07

- 起点：8e84f4c46198fbe5475c389141264f020342f9f6。
- 已两次复核 HEAD：67333fa9e851b2b847702062aabf1fbe844b0db7；工作树干净。审查三点差异全部8文件。
- 文档硬性标准违规：**0**；Fowler 判断性异味：**2**。这不是 Spec 验收通过结论。

## 逐文件结论

下列路径统一以 `F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\` 为绝对根。

| 文件 | 硬违例 | 判断 |
|---|---:|---|
| CONTEXT.md | 0 | 区分07已实现和08未开放 |
| bestseller_monitor/analysis.py | 0 | 可能 Duplicated Code，见下 |
| bestseller_monitor/pages/bestseller-analysis.html | 0 | 原因展示与重试入口，无额外标准问题 |
| bestseller_monitor/sku_lifecycle.py | 0 | 共享缓存生命周期方向合理 |
| bestseller_monitor/sku_matching.py | 0 | 可能 Feature Envy，见下 |
| bestseller_monitor/sku_recommendations.py | 0 | 使用共享生命周期，无额外标准问题 |
| tests/test_sku_drafts.py | 0 | 公开服务操作与终态断言 |
| tests/test_sku_recovery.py | 0 | 真实服务/存储及HTTP浏览器接缝；未另测私有装配函数 |

## 判断性异味（不是硬违例）

1. **[P3] 可能 Duplicated Code**：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\bestseller_monitor\analysis.py:518–532` 与同文件 `394–407` 重复“恢复人工约束、复制模型工作/选项、编码分组、外部调用期间重新读取并重放人工约束”的流程。后续改变人工重载规则需同时维护两处，容易让新分析和重试行为分叉。建议共享应用有效SKU决定的流程，保留 latest 与指定 analysis_id 的读取策略差异；不要直接把两种读取策略混同。

2. **[P3] 可能 Feature Envy**：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\bestseller_monitor\sku_matching.py:86–88` 在 execute 返回空结果后遍历 `work.tasks`、重新构造 context 并读取内部 task 字段取得失败原因。调用方依赖生命周期模块的索引与记录形状，结构重构会同时影响匹配逻辑，且每次失败都扫描已有任务。建议 execute 返回明确结果/失败原因，或提供按任务键查询的窄接口。

依据：`F:\AI\projects\bestseller\.codex\skills\code-review\SKILL.md` 的 Fowler 基线（Duplicated Code、Feature Envy），均属可讨论判断；仓库规则优先。

## 边界与已知证据

任务指定副本 ADR-0050 不存在，改读主仓 `F:\AI\projects\bestseller\docs\adr\0050-sku-level-grouping-and-merchant-codes.md`，不补写文件。未运行测试，不能由差异证明红先绿后或全量收尾完成。

已阅读主agent提供的 `F:\AI\projects\bestseller\.scratch\logs\tickets\sku-grouping\07\20261009-sku07-01\historical-safety-red.log`：已有有效归属在新候选失败后改变，公开组ID断言失败。属于 Spec 的“未完成保留有效归属”整改项，本轴不重复包装成编码标准违例，也不自行修复。未修改代码、提交或派活。