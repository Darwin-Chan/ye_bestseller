RECEIVED:issues0811_01_spec_5e1775b
DONE:F:\AI\projects\bestseller\work\results\issues0811_01_spec_5e1775b.md

## Spec 独立最终复核

首末 HEAD 均为 5e1775b601adf8f806b6ef44f27f9395b8da8fb4，status --porcelain 均为空。范围：69066da421b255168884224a20a28fb20b6d4713...HEAD 的完整三提交增量；仅 Spec，不审 Standards。

### 有效发现／硬契约违例

**[P2] 启动失败终态只覆盖 open_session，浏览器建立前的初始化失败仍留下空进行中轮。**

位置：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\pipeline.py:267–289,307–331,435–446`。

静态可达路径：open_new 已提交新轮后，:289 的 round_shops 再读 cfg.shop_csv（同文件 :136–139）。清单若在先前选店读取后被删除、变为不可读或写入非法内容，此处抛 FileNotFoundError/OSError/ValueError，尚未调用 open_session，故不会进入新增 STARTUP_FAILED 分支。外层只捕获 STOP_WITH_OUTCOME；finally 清掉 NOT_STARTED 身份并释放锁，却不结束已提交的空轮。修复清单后，下一次明确开始仍被未终态轮拒绝，且此轮没有明确暂停资格。

依据：`F:\AI\projects\bestseller\.scratch\issues-08-11\issues\01-abort-cleanup-new-round.md:23` 要求“新建启动失败不制造空进行中轮次”；`F:\AI\projects\bestseller\.scratch\issues-08-11\spec.md:188` 要求起采失败不留下空进行中轮。这是本票启动失败覆盖部分实现，不是票06遗留处置遗漏。应覆盖新轮提交后、实际采集前的初始化失败，同时保留先提交终态和真实清理欠账。未运行复现，以上为代码路径判断。

### 判断项与已处理线索

(a) 部分实现：上述启动失败边界；(b) scope creep：无有效发现；(c) 实现错误：上述 P2。未发现其他有效问题。

真实 open_session 零资源或全部清理成功发布 CLOSED；未清理成功仍 UNKNOWN。新建门禁没有泛化放行 UNKNOWN。确认绑定、首 tick 前原浏览器欠账冻结、浏览器建立失败终态、停止副作用归属及首提交终态保护未见静态回退。

票02–07及08-H01/11-H01不计遗漏；待全量草稿不视为未实现，也不替代票07联合收尾。未测试、联网、登录或采集；未派代理、等待兄弟、修改源码、暂存提交或清理材料，仅认领并写本结果。
