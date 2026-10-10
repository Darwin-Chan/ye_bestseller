# 票04 Standards 修复复审

固定范围：802ee29f993b0fe3e642ef5b6145315cb3296c3a → **faa2b0ca11f591fc9f8792e2a56a8ff156db7d36**。包含原实现、0d2a5544 同目录未知浏览器修复及 faa2b0c 配置优先拒绝修复。读取时 HEAD 匹配该候选，工作区干净。本任务实际认领轴为 Standards，不代替另一份 Spec 复审。

## 硬性标准违反
**无发现。**

核对标准原句：
- ADR-0007：“exe 里不放项目代码”“项目根由 exe 自身位置推导”。新增模块/页面仍由源码部署目录加载，未引入第二份冻结实现。
- ADR-0008：“界面不持有采集锁”“采集锁是‘有没有采集在跑’的唯一权威判据”。GUI预检未替代采集进程互斥；最终核验仍在采集锁内。
- ADR-0028：“terminate 只对传进来的那个绑定动手”“close_browser…只看 BoundBrowser 上的 facts”。手动浏览器服务没有终止接口，修复未扩大停止授权。
- 票04工单：“新的测试注入集中在专用浏览器服务边界”；修复回归使用真实公开API，只替换OS查询/创建边界。

标准输入均位于 `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\` 下 AGENTS.md、CONTEXT.md、docs\adr；另核对0048、0049、0051适用边界，未发现关窗、试采或计时归属回退。

## 修复核对
同仓 `bestseller_monitor\dedicated_browser.py:87–101` 现对所有可读命令行解析同目录参数，不再按名称跳过；`tests\test_manual_edge.py:338–352` 经 get_start/open_login_browser/start_run 验证占用、零创建、零建轮。初审P2的具体漏判路径已关闭。

`dedicated_browser.py:163–170` 先检查配置、失败直接返回，故非法日常目录不进入observe；`test_manual_edge.py:281–295` 强化observe.assert_not_called。原后置配置复核保留，可覆盖查询期间配置变化，未发现新增授权或占用放行风险。

## Fowler启发式异味
逐项检查 Mysterious Name、Duplicated Code、Feature Envy、Data Clumps、Primitive Obsession、Repeated Switches、Shotgun Surgery、Divergent Change、Speculative Generality、Message Chains、Middle Man、Refused Bequest：**无可操作发现**。Settings已承载配置字段簇；服务/OS适配器分工符合实际接缝；前后配置检查属于边界复核，不能仅因重复调用就要求删去。

## 证据边界
只读核对主仓 `.scratch\logs\tickets\issues-08-11\04\20261009-ticket04-01\` 的23红/24绿、25红/26绿；最后绿日志为19项通过。未自行重跑测试。最终冻结候选分段全量及双轴闭环由父agent收尾，未将待办或人工登录待验列为实现缺陷。

