# 0046. Python 管道编码契约由启动壳持有

- 状态：已接受（访谈 Q1–Q13 全部确认；单会话直接 implement）
- 日期：2026-10-05

## 背景

[2026-09-23 架构审查的编码候选](../reviews/architecture-review-2026-09-23.html#encoding)
指出：启动壳以 UTF-8 解码 Python 子进程 stderr，写方却没有共同约定。
分析入口自行 reconfigure，界面与交换台没有相同保护；本机启动器日志中已有中文乱码。
失败原因因此不能完整进入日志与弹窗，削弱了
[ADR-0007](0007-gui-exe-is-a-shell.md) 要求的失败可见性。

工具里也有同类选择，但必须区分三件事：Python 子进程的标准流、工具自身的标准流、
非 Python 外部命令的输出。release_shells 捕获 Git；issue_bundle 还捕获 tasklist，
不能用 Python 的环境变量替它们定义写方编码。

## 已确认的决策

### 1. 范围与环境优先级

契约只覆盖由壳或工具建立并捕获的 Python 子进程文本标准流；不是全机编码设置，
也不保证所有运行时入口直接执行时的输出。非 Python 命令、文件内容编码与 ZIP 名称编码
不纳入本次归拢。

写方在解释器启动前通过子进程环境取得 UTF-8 约定，以覆盖导入期间的失败输出。
环境从调用方传入的映射复制，保留其余变量，明确覆盖冲突的 PYTHONIOENCODING；
不修改父进程环境。启动壳必须把构造后的环境实际传给 Popen。

### 2. 契约的家与 interface

契约收在 shells/launcher_core.py；壳与设计时工具直接消费它。
不新增转发 module，不使运行时入口依赖设计时 shells/，不把 bestseller_monitor 冻进壳。
这一依赖方向沿用 [ADR-0033](0033-repo-layout-runtime-vs-design.md)。

公共 interface：

- PYTHON_TEXT_ENCODING="utf-8"：写方环境与读方编码共同使用。
- PYTHON_TEXT_DECODE_ERRORS="replace"：只表达读方的坏字节政策。
- python_text_env(base_env)：复制环境并应用写方约定。
- configure_tool_output(*, errors="replace")：工具自身输出的显式适配；errors=None
  沿用仅设置 encoding 的旧调用（issue_bundle 使用；Python 将 errors 重置为 strict），
  不复用读方坏字节常量。

不封装 run / Popen，不改变命令、参数、cwd、等待、终止或管道拓扑。
import module 本身不得修改标准流；工具在命令行执行路径显式配置自己的输出。

### 3. 正常文本与坏字节分开承诺

正常 Python 中文文本以 UTF-8 逐字读回。读方保留 errors="replace"，
避免坏字节使日志收集和失败通知中断；不承诺任意原始字节可恢复，也不承诺日志永无 U+FFFD。
子进程环境只设置 PYTHONIOENCODING=utf-8，不加 :replace。
工具自身输出保留现有容错行为；它不是读方错误政策的使用场景。

### 4. 工具迁移清单封口

现有自行 reconfigure 的 12 个工具改用共同输出适配：
acceptance_check、bench_refresh、check_orphans、diag_card_urls、diag_offer_id_presolve、
diag_verify_state、dump_plan_parity、issue_bundle、replay_groups、run_full_suite、summary、
sync_list_titles。

已核实的 Python 捕获／重定向点应用共同环境与读取约定：diag_stop_request、
diag_edge_cleanup、run_full_suite。diag_edge_cleanup 只改编码接线，验证不执行真实浏览器
清理动作；run_full_suite 不改分段、计数、测试集合或等待政策。

release_shells 的 Git 捕获与 issue_bundle 的 Git / tasklist 捕获保留命令特定读取政策，
不套用 Python 子进程契约；不机械替换全仓 UTF-8 字面量。

### 5. 入口与非 Python 引导层的兼容适配

analyze.py 保留现有直接运行 stderr 的 reconfigure 兼容保护，注释明确它不是启动壳
管道契约的定义处。不向 gui.py / exchange.py 复制，不为消除一个字面量添加运行时到
shells/ 的依赖。

issue_bundle.cmd、setup_wizard.sh、start.bat 保持现有行为。引导层在启动 Python 前
不能 import Python module，不新增编码查询进程。setup_wizard 的 py_utf8 与 issue_bundle
的 ASCII 引导／控制台配置作为明确的非 import 适配保留，不顺手更改采集与交互输入行为。

## 被否掉的方向

- 所有 subprocess 统一为 UTF-8：对不受 PYTHONIOENCODING 控制的外部写方作出假承诺。
- 只在三个运行时入口分别 reconfigure：知识继续复制，而且保护开始得晚于解释器启动。
- 用户环境优先或修改父环境：前者可破坏固定读方协议，后者影响不属于本契约的启动点。
- 新建共享小 module：当前调用方都在设计时侧，没有新增依赖方向上的收益。
- 包办进程寿命的通用启动器：现有启动点的管道与寿命差异真实存在，不是编码选择的一部分。
- 全面删除 analyze 兼容保护或改引导层：引入与本次受控管道契约无关的行为退步。

## 验收与交付边界（Q10–Q13）

1. 真 Python 子进程的 stdout / stderr 中文逐字往返；以冲突的 GBK 环境证明契约覆盖，
   并验证其它环境变量保留、调用方映射与父环境不变。
2. 契约 module 与迁移工具 import 不改变标准流；工具 CLI 自身在 GBK 环境仍输出 UTF-8。
   pythonw 无控制台与 StringIO 替身有明确兼容行为。
3. 三个源壳捕获入口导入失败；失败通知保留中文。用原始坏字节验证 replace 仍让后续
   中文证据可见，退出码 0/1/2 的既有通知政策不变。
4. 重建三只真实 exe，TOC 与 --check 证明仅打包壳代码。真 exe 在隔离目录、
   BESTSELLER_NO_DIALOG=1 下用本机 python / pythonw 各跑退出码 0/1/2（共 18 个用例）；
   以临时入口在导入前输出中文，核对固定参数、环境、日志和通知政策。仅检查新写证据，
   不把历史日志内 U+FFFD 数量归零当作验收。保留 stdout 原有丢弃政策。
5. m4 保持 merge_only；不改真实配置，不启动业务界面、浏览器、采集或交换任务。
   本机隔离验收不代表 m1/m2/m3 的部署验收。

执行顺序：实现及定向测试 → 三壳重建和自检 → 真 exe 隔离实跑 → 隔离副本内分段全量
（不 fail-fast）→ Standards / Spec 双轴审查 → 按路径将源码、测试、文档、三只 dist exe
一起提交 → release_shells.py --no-build 出包并核对 SHA256 和 MANIFEST 的提交来源。
发布包使用新目录，不触发默认重建，不改变已验收的二进制。

本任务以本机可验证交付、发布包和接收说明为完成界限；m1/m2/m3 人工更新源码、替换
三只 exe、校验清单并执行本机 --check / 编码探针，部署结果另行记录。
不拆 spec / tickets，不新增其它候选的工作。执行记录见
[编码契约交付与接收](../ops/python-pipe-encoding-delivery-2026-10-05.md)。

## 明确不处理

不重报 2026-09-20 的 ReadFailedVisit、诊断工具抄点击语义、停止窗口 rebind 探针；
不重开 [ADR-0027](0027-2026-09-14-review-candidates-05-07-suspended.md) 挂起的候选 05–07。
不触碰其它会话的未提交文件；提交必须按本次路径限定。
