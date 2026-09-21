# 0033. 仓库布局：第一层区分运行时与设计时

- 状态：已接受（2026-09-21 落地）
- 日期：2026-09-21

## 背景

仓库里一直混着两类东西，且从根目录看不出分界：

- **运行时需要的**：程序包与三个页面、`config/`、`dist/` 的三只壳、运行时日志、分析报告；
- **设计程序时产生的**：文档（PRD、调研、ADR、评审、验收记录）、工单与过程材料、
  开发/测试/票证据日志、QA 截图与原型产物、打包中间产物。

混用有四处实证：根目录散着 49 个开发日志；设计方案一度打算把开发日志收进 `logs/dev/`
（正是"共用文件夹"本身）；4 个设计时工具（`tools/analyze_click.py`、`analyze_delay.py`、
`_shot_live.py`、`_shot_mockup.py`）默认把产物写进运行时 `output/`；`work/`（38MB 的
多代理交接目录）与 `.scratch/` 分家。找东西要先猜"这东西属于哪段过程"，新机器上的
人（与 agent）没有稳定预期。

## 决策

**第一层文件夹要么运行时、要么设计时，不许兼有；约定如下：**

1. 运行时日志只在 `logs/`（`config` 的 `logs_dir` 与三只壳写死的落点）；设计时日志
   （开发/测试/票证据）只在 `.scratch/logs/`（`dev/` 与 `tickets/<slug>/<票号>/`）。
2. 文档只在 `docs/`，按其子目录分类（`research/` `product/` `ops/` `history/`
   `design/` `out-of-scope/`，加上原有的 `adr/` `reviews/` `agents/`）；根目录只留
   定论的 README / CONTEXT / AGENTS。
3. 程序资产属于程序包：三个页面在 `bestseller_monitor/pages/`，不混在文档里。
4. 设计时工具不许往运行时文件夹写：`tools/` 的诊断与截图类脚本默认输出
   `.scratch/tool-output/`。
5. `output/` 只放分析程序生成（或从分析界面导出）的报告。
6. 壳（4 个 launcher 与 3 份 `.spec`）收在 `shells/`，是构建输入；`dist/` 仍是壳唯一的
   运行位置（ADR-0007），`build/` 只是打包中间产物、可随时清空。

## 被否掉的方向

- **伞形归拢**（新建 `runtime/` 与 `design/` 两个总目录把一切收进去）：与 ADR-0007
  （exe 必须在 `<项目根>\dist\`）、`config` 的相对根路径（`logs_dir = "logs"`）、
  上机清单里的裸命令全部冲突，为目录美感付出的兼容代价不成比例。
- **把开发日志塞进 `logs/dev/`**：正是"共用文件夹"本身，违反本条。
- **顺带把入口脚本（`gui.py` 等）挪进子目录**：壳按固定路径 `<项目根>\gui.py` 拉脚本、
  `LauncherSpec.markers` 也认它的名字，且上机清单与 README 的裸命令都按根路径写；
  改名的收益小于散布的成本。

## 结果与代价

**收益**：任一条目按"运行时还是设计时"两分即可定位；新产物的落点有唯一答案；
`logs/` 与 `output/` 的含义可以对外直说——前者是运行记录，后者是报告。

**代价，说在明处**：

- 旧路径引用一次性修正（约 30 处：README、docs 互链、测试与工具）；已归档的 ADR 与
  评审报告正文按"终稿不改"保留历史路径。
- 壳的源码运行（非打包）定位多认一级：`<项目根>\shells\` 的上一级才是项目根，
  `launcher_core.resolve_project_root` 因此改为"两级都试、项目根标记说话"；
  exe（打包态）的定位行为不变，回归用例见 `tests/test_launcher_core.py`。
- `.scratch/` 仍是 gitignored 的本地过程区：新克隆/新机器看不到工单与设计时日志，
  这是既有约定（见 `docs/agents/issue-tracker.md`），本条不改变它。

约定同步写进了 [AGENTS.md](../../AGENTS.md)（agent 面向）与 README 的「目录布局」一节。
