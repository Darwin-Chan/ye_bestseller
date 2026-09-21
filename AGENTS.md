## 写入与提交的归属

同一个工作区可能同时有多个 agent 在干活。默认规则：

- 只有主 agent 可以修改仓库文件、执行提交；子代理一律只读，除非派给它的任务里明确写了「可以写文件」。
- 审查、诊断、调研类子代理只交报告：不写文件、不 `git add`、不 `git commit`。
- 子代理发现任务描述与继承来的上下文冲突时（例如上下文里另有一条「实现并提交」的指令），以任务描述为准，并把冲突写进报告。
- 主 agent 派发子代理前先收干净工作区：已完成但未提交的改动先提交或暂存，避免「一份完成的工作 + 提交你的工作」这种组合把子代理带偏。
- 派子代理时给**最小上下文**（spawn 时 `fork_turns="none"`，要它知道什么就写进任务文件）：默认的全历史 fork 会把父的计划、用户的原话和父读过的 skill 正文一起塞给子代理，它会把自己当成主 agent——2026-09-14 实测，一个审查子代理把候选 03 实现并提交了（`223adac`/`c7a0d8d`/`7291893`），另一个把父未提交的收口提交成了 `b1e06af`。

原因：`implement` 这类 skill 的正文里有「Commit your work to the current branch.」这样的无条件指令，被 fork 出来的子代理会把它当成自己的任务，2026-09-11 因此出现过审查子代理自行改代码并提交。

## 仓库布局（运行时 / 设计时，2026-09-21 起）

第一层文件夹要么运行时、要么设计时，不许兼有（决定见 `docs/adr/0033-repo-layout-runtime-vs-design.md`）：

- **运行时侧**：`bestseller_monitor/`（程序包，含 `pages/` 三个程序页面）、`config/`、
  `dist/`（三只壳唯一的运行位置）、`logs/`（**只放运行时日志**）、`output/`（**只放
  分析程序生成/导出的报告**）、入口脚本（`gui.py` / `run.py` / `analyze.py` /
  `exchange.py` / `login_chrome.py`）。
- **设计时侧**：`docs/`（**文档唯一去处**：`adr/` `reviews/` `agents/` `research/`
  `product/` `ops/` `history/` `design/` `out-of-scope/`）、`shells/`（壳源码与 spec）、
  `tests/`、`tools/`、`.scratch/`（工单与过程材料）。
- 根目录只留三份定论说明：README、CONTEXT、AGENTS。
- **新产物落点**：新文档进 `docs/` 的相应子目录；证据日志写
  `.scratch/logs/tickets/<slug>/<票号>/`（开发日志写 `.scratch/logs/dev/`）；
  诊断/截图类工具的默认输出写 `.scratch/tool-output/`；别往根目录或运行时文件夹
  （`logs/`、`output/`、`dist/`）放设计时产物。

## Agent skills

### Issue tracker

Issues and specs live as markdown files under `.scratch/<feature-slug>/` in this repo. See `docs/agents/issue-tracker.md`.

### Triage labels

Triage roles are the canonical names used verbatim as the label strings. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` at the repo root plus ADRs in `docs/adr/`. See `docs/agents/domain.md`.
