# SKU票06 Standards审查

审查提交：`9c9ccba6543af0b40426b6ab15b6f8e08f8b31a8`；基线：`9abf9c0c6d77c69d6b3a2b187dd7044792c140ca`。工作树干净。范围覆盖完整9文件差异（含旧compare路径、组ID变化、推荐缓存、人工决定恢复、UI及测试），未实施07–08，未运行测试或付费模型。

## 文档硬违例：1项

**[P2] 浏览器验收绕过约定接缝读取内部存储。**
位置：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\tests\test_sku_recommendations.py:423–424`（新增文件hunk）。
`self.service.get(self.service.store.latest()["id"])`直接导航到AnalysisService内部store，以内部持久化接口确定当前分析；这使“人工移动后保存成功”的行为测试依赖服务的存储组织方式。
标准：`F:\AI\projects\bestseller\.codex\skills\tdd\SKILL.md`的“Tests live at seams, never against internals”及禁止side-channel验证；同目录`tests.md`要求“Uses public API only”、禁止绕过接口验证；`mocking.md`不支持把自有内部协作者作为测试边界。本票交付记录亦声明公开AnalysisService操作为主要接缝。
建议：保存后刷新页面，通过公开页面断言人工归属及恢复结果；若需服务断言，使用公开操作已返回的分析ID调用`get`，不要从`service.store.latest()`获取ID。

## Fowler启发式异味：0项

已逐项考虑任务列出的12类异味，未发现值得单独报告的新增异味。候选组合法性与成对判断合法性语义不同，不把两者相似的排除检查直接定性为重复代码；推荐/缓存所需字典语境也不单凭其使用原始类型判为Primitive Obsession。

## 结论与限制

建议修复上述测试接缝违例后收口。领域术语、运行时/设计时文件落点及本地缓存不进入旧共享载荷的边界未发现硬冲突。交付记录明确全量与双轴审查尚待补齐，不能据此宣称工单已完成；本报告仅静态Standards审查，不替代Spec或最终测试验收。仅写本报告，无代码修改、提交或代理派发。
