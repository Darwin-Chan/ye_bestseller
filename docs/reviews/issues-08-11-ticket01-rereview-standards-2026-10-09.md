RECEIVED:issues0811_01_standards_daaf0e2
DONE:F:\AI\projects\bestseller\work\results\issues0811_01_standards_daaf0e2.md

# Standards 独立复审

范围：69066da421b255168884224a20a28fb20b6d4713...daaf0e287830918391266f4d00bbc721bf2e79e7，完整两提交增量（18 文件），不评产品 Spec。开始及结束 HEAD 均符合固定候选，git status --porcelain 均为空。

## 有效问题

**[P2] 为新收尾政策补齐现行领域说明与 ADR 的替代关系。**

- F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\CONTEXT.md:226 新增“新进程出现不能替代原浏览器安全收尾证明”，但同文件 :69 仍宣称“目标已被替换就结束旧停止”。
- F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\stop_request.py:491–498、621–639 实际保留被替换目标的核验/欠账，并取消收尾重试超时放行。
- F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\docs\adr\0024-stop-target-owns-the-whole-window.md:3、22–24、33–34 仍标“已采用并实现”，规定有限重试及 gone/replaced 结束旧 operation。

依据：F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\docs\agents\domain.md:41–51 要求遵循领域词汇并显式揭示 ADR 冲突。目前读者会得到相反的停止收口规则；交付记录虽解释测试预期迁移，未标明替代上述 ADR 决策。建议同步“强制停止”词条，并新增/追加有日期的 ADR 替代说明，保留历史正文。不要求恢复不安全的超时放行，也不判断新产品政策是否正确。

## 硬违例与判断项

未发现另有明确依据的源码硬违例。初审 P3 收尾资格重复已由 browser_cleanup_complete 统一；逐项考虑全部 12 类 smell，未新增需报告的判断项。既有白盒测试模式未作为新增违例。

## 边界

仅只读静态审查；未运行测试、全量、浏览器、真实账号、采集、有声或联网操作。证据路径及待验收声明不等于验收通过；未关闭工单。除认领改名和本结果文件外无写入、暂存、提交、清理、派活或等待。
