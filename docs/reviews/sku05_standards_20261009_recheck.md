# 票05 Standards 复审

任务：sku05_standards_20261009_recheck。
工作树：`F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller`。
固定基线：`5c6cc694d434bb13470b589d5f3d5dc31c7bf1f0`。
最终 HEAD：`6c3f0c2d33f5d2ba4553e590b1a5b2fad0eb8817`。

已执行完整 `git diff 5c6cc694d434bb13470b589d5f3d5dc31c7bf1f0...HEAD`（780行），核实 log 包含 afe20b3、744219c、6c3f0c2；结束前工作树干净、HEAD未变。

## Standards

**0项明确标准违反；0项达到实际维护风险报告门槛的启发式异味。**

依据：主仓与副本 AGENTS.md、副本 CONTEXT.md、主仓 `F:\AI\projects\bestseller\docs\ops\隔离副本与证据收尾规范.md`，以及任务给出的 Fowler baseline。

- 新运行时代码在程序包，交付文档在 docs/design；未把设计时材料放入运行时目录。
- SKU图文逻辑保留局部规则／缓存边界，沿用历史证据、既有召回与模型接口，没有重新实现采集或库存销量算法。
- 缓存表、证据版本和旧商品共享路径隔离；失败记录不进入确定否缓存。
- 新服务及浏览器测试沿用真实历史数据库与决定存储夹具，只控制外部模型请求；没有用手造归属替代服务验收。
- group_of/protected 的局部重复、测试夹具方法复用、延迟导入等已考虑；没有查实足以单列发现的维护故障，不把样式偏好当成标准违反。

## 定向修复核实（不另开 Spec 轴）

初次Spec P2 的修复在 `F:\AI\projects\bestseller\.scratch\sandboxes\sku-grouping\02\bestseller\bestseller_monitor\sku_matching.py`：模型异常写入 failed_pairs；人工决定重读后重新检查合法性并恢复 pending；失败成员不能提出自动增员，目标含失败成员也不接受增员。因此混合成功／失败及失败／确定否不再由最后一个完成判断覆盖待处理状态。

新增真实服务回归检查三个独立组、pending及失败原文、唯一计量；两种结果顺序分别覆盖上述情境。已读取 `F:\AI\projects\bestseller\.scratch\logs\tickets\sku-grouping\05\20261009-sku-image-01\mixed-state-green.log`：15 tests，OK。此为父代理已有证据，本复审没有重跑测试，亦不宣称最终全量已完成。

旧草稿断言改为 missing_image＋缺图说明符合现行状态，未削弱旧商品结论不得继承为SKU结论的测试目的。

结论：Standards 0项发现；指定P2修复经代码及既有定向证据核实。未派活、未提交、未修改仓库；仅写本指定报告，不继续后票。
