RECEIVED:issues0811_01_standards_5e1775b
DONE:F:\AI\projects\bestseller\work\results\issues0811_01_standards_5e1775b.md

# Standards 独立最终复核

范围：固定增量 69066da421b255168884224a20a28fb20b6d4713...HEAD，覆盖 bc6cc8a、daaf0e2、5e1775b。开始及结束 HEAD 均为 5e1775b601adf8f806b6ef44f27f9395b8da8fb4，status --porcelain 均为空。已读取任务指定规范；没有运行测试或副作用。

## 硬性违例

- **[P2] 强制事务重读空身份可能丢掉冻结的浏览器欠账。** `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\stop_request.py:556–561,575–591`：tick 先观察并冻结 OWNED A，随后在 BEGIN IMMEDIATE 前身份行消失；生产 observe（262–272）返回 identity_missing、browser_state=None。_force 不检查该错误，也不使用新增 _original_browser_facts，因 None 不在拒绝状态集合而继续终止 A；进程退出后跳过浏览器收尾并 _finish，清空仍有欠账的 watch。锁内应重验关系；无法核验保留门禁，不能忽略冻结归属。依据：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\CONTEXT.md:69` 与 `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\docs\adr\0024-stop-target-owns-the-whole-window.md:95–100`。这是静态分支发现，未执行竞态复现。

## 判断项（非硬违例）

- **[P3] possible Duplicated Code，非阻断。** `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\tests\test_abort_new_round.py:260–274,290–304` 两处逐行重复 SQLite trace、竞争身份发布及锁释放夹具。可提取仅负责竞态注入的 context/helper，保留两个公共入口及各自断言，避免后续边界修订只更新其中一处。依据：task.md 的完整 smell baseline；不是仓库强制规则。

## 已收口与边界

CONTEXT 的旧“替换即结束”已撤除；ADR-0024:3、89–104 明确 2026-10-09 替代范围并保留历史，文档冲突已收口。未发现其他有效文档硬违例或 smell。交付草稿明确全量待做，不据此判功能未实现；本报告不授权集成、不替代票07联合收尾或人工08-H01/11-H01。技能派发步骤与只读任务冲突，已遵守 task.md，不派发、不暂存、不提交。
