# Spec review — ticket 01

基线：69066da421b255168884224a20a28fb20b6d4713；HEAD：bc6cc8a296a165dc43b393ab0085f3a020403f5e。仅静态审查，未执行测试。

## 发现

1. **[P1] 页面中止确认没有冻结原轮次/目标，旧授权可以中止新进程。** `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\pages\ui_live.html:530–540` 的弹窗只保存文案/动作，确定时无参数调用 `abort_run`。`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\gui.py:704–717` 仅在提供 expected_target 时核验，随后优先选当前身份所属轮次。A 的确认框打开后，A 完成、CLI B 启动，再确认会把 B 写为 ABANDONED 并停止 B；关窗快照的保护未覆盖页面中止。依据：`F:\AI\projects\bestseller\.scratch\issues-08-11\issues\01-abort-cleanup-new-round.md:22`：“原授权不能作用于后来出现的进程”；`F:\AI\projects\bestseller\.scratch\issues-08-11\spec.md:194`：“确认时冻结轮次及存在的采集目标，执行前复核”。需由服务端签发/校验原确认目标。

2. **[P2] 子进程已启动但浏览器建立失败仍留下空进行中轮次。** `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\pipeline.py:267–268` 先调用 open_new；`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\rounds.py:381–389` 已提交轮次。随后 pipeline.py:301–305 建立浏览器；例如 browser_pw.py:105–106 的 Edge 路径不存在会直接抛 FileNotFoundError，不属于 STOP_WITH_OUTCOME，finally 不结束/撤销新轮。此次零采集仍留 terminal_reason=NULL，并阻挡下次明确新建。新增失败测试只覆盖 Popen 抛错。依据：`F:\AI\projects\bestseller\.scratch\issues-08-11\issues\01-abort-cleanup-new-round.md:23`：“新建启动失败不制造空进行中轮次”；`F:\AI\projects\bestseller\.scratch\issues-08-11\spec.md:188` 同契约。

3. **[P1] 首次清理前发生身份替换会丢弃原浏览器欠账。** `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\bestseller_monitor\stop_request.py:468–471` 对 REPLACED 直接 _settle，627–634 随即结束窗口；begin 未冻结原浏览器，只有关闭失败后才保存 pending_browser。A 中止后退出但浏览器清理失败、CLI B 在下次 tick 前登记时，原 OWNED 绑定被覆盖，旧停止窗口被当作完成；B 结束清身份后 GUI 可新建，A 的浏览器仍可能存活。新增替换测试仅覆盖已经进入 CLEANUP_PENDING 的情况。依据：`F:\AI\projects\bestseller\.scratch\issues-08-11\issues\01-abort-cleanup-new-round.md:16,22`：“浏览器及必要身份清理…全部完成后”且“清理失败…不放行新建”。替换身份不应成为原浏览器已清理的证明。

未发现需单独报告的范围扩张；未将票02–07交付列为遗漏。末次 status 出现未跟踪 docs/design/issues-08-11-ticket01-delivery-2026-10-08.md，未读取或修改；可能由父/兄弟产生。
