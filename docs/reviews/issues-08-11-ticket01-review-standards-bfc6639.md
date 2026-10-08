RECEIVED:issues0811_01_standards_bfc6639
DONE:F:\AI\projects\bestseller\work\results\issues0811_01_standards_bfc6639.md

# Standards 独立复核

范围：固定基线 `69066da421b255168884224a20a28fb20b6d4713` 至候选 `bfc6639a4d560b9e3333d524f28aec591579dd1f` 的完整增量。首末核验工作区均干净，HEAD 均匹配。未执行测试、采集、浏览器或联网。

## 硬性违例

### [P2] 显式同步 ADR-0025 的建立失败状态契约

变更位置：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\browser_pw.py:159–165`。

建立失败的补偿清理成功后现在发布 `CLOSED`。但仍标“已接受并实现”的 `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\docs\adr\0025-browser-session-owns-establishment.md:3,32–34` 明确要求建立失败保持 `UNKNOWN`，`CLOSED` 只用于成功交付的会话。ADR-0024:92–93 的新说明仅替代其本文规则，没有显式撤销0025这一契约。

规范依据：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\docs\agents\domain.md:47–51` 要求显式揭示 ADR 冲突。可达影响：浏览器路径/CDP 建立失败但补偿清理成功时，新实现和有效 ADR 给出相反状态；后续维护依旧契约恢复 UNKNOWN 会重新阻塞安全重试。应在0025补带日期的明确替代说明：建立失败但全部资源已清理也可 CLOSED，未核实仍 UNKNOWN；保留历史，不回滚本次安全修复。

无其他有效硬性违例。

## 判断项（smell）

无新的有效发现。身份行消失时的 fail-closed、冻结原浏览器欠账、CSV/参数登记失败分类及状态型OS替身已复核；竞争夹具重复已提取，不重复报告历史已修问题。

## 证据与边界

既有证据记录相关回归363条通过，不代表分段全量通过；本票全量待此次复核后运行，不将待收尾草稿判为功能缺失，亦不代替票07联合收尾或部署机人工验收。

临时交接中的主agent表述、技能派发/提交要求与本任务冲突；以 task.md 的子agent只读边界执行，仅写本报告。
