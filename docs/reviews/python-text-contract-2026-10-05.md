# 编码契约候选 ④：实现审阅（2026-10-05）

## 范围与方法

- 需求来源：[架构审查](architecture-review-2026-09-23.html#encoding)，以及已确认 Q1–Q13
  并定稿的 [ADR-0046](../adr/0046-python-pipe-encoding-owned-by-launcher.md)。
- 固定实现基线：b8e010f7a50647979283464815ee0de8282811a3；审查基线到本次限定路径的
  working-tree diff（新增文件直接阅读），不把同时推进的世界模板测试改动纳入审查或提交。
- 标准来源：AGENTS.md（写入归属、布局、分段全量）、CONTEXT.md（术语）、ADR-0007
  （失败可见性）、ADR-0033（运行时/设计时），另按 code-review 的 Fowler smell baseline 检查。
- 本会话分别按 Standards / Spec 两轴审阅，**不是独立双 agent 审查**。共享工作区有其它
  会话的在途文件；不为派审查 agent 而 stash 它们，也不打破已确认的源码与产物一起提交顺序。

## Standards

无未解决的文档标准违例或需要修改的 smell 项。

- 设计时输出与证据均在 .scratch 下；ADR、操作说明、审阅记录在 docs 对应子目录，
  dist 只更新三只运行产物，不新增运行时到 shells 的依赖。
- 导入不修改标准流；十二处输出知识已收到同一显式 helper，重复的 sys.path 接线是
  约定的设计时 import 适配，不再包含编码政策。
- 没有新增通用进程框架；三个捕获点只改变 env / decoder 接线。stderr 泵仅在 EOF
  后关闭已耗尽的管道，消除真进程测试的 ResourceWarning，不改等待、终止或通知政策。
- 发现并修正：errors=None 的初稿说明曾误称“保留原流 errors”。实际省略 errors 时，
  reconfigure(encoding=...) 会重置 strict。文档已纠正，并新增独立输出政策用例证明
  issue_bundle 保留旧调用行为；不与读方 replace 常量混用。

## Spec

无未解决的漏项、越界或错误实现。

- 家在 launcher_core；写方以复制后的环境在解释器启动前设置 UTF-8，覆盖 GBK 冲突，
  保留其它环境变量。读方 UTF-8 / replace；不强制 stdout 捕获、不修改父环境。
- 保留 analyze 直接运行的兼容保护。非 Python Git/tasklist 与 .cmd/.sh/.bat 的适配
  未被机械并入契约；未重开排除候选。
- 真子进程覆盖双流、pythonw、导入副作用、CLI、坏字节、通知文本与退出政策；三只
  真 exe 的隔离探针以独立的政策期待值验证 18 个目标/解释器/退出码组合。
- 源码与三只重建 exe 将按限定路径同提交；发布只用 --no-build，提交后核对 MANIFEST
  来源与 SHA256。m4 配置仍是 merge_only；m1/m2/m3 人工部署另行记录，未冒充已部署。

## 验证依据

- 定向回归：9 文件，166 项 OK；编码契约新用例 10 项。
- 三壳构建、TOC、--check 全通过；最终真 exe 探针 18/18。
- 分段全量：合计 1383 项（预期 1383，计数对账一致），总耗时 677.7s，段结果 7/7 段 OK，结论 OK
- Python 语法编译与 diff 空白检查通过；未宣称静态类型检查。
- 证据、产物完整 SHA256 与接收步骤见
  [交付与接收](../ops/python-pipe-encoding-delivery-2026-10-05.md)。

结论：Standards 未解决项 0；Spec 未解决项 0。独立审查不足作为方法限制明确保留。
