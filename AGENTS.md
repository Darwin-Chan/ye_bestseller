## 写入与提交的归属

同一个工作区可能同时有多个 agent 在干活。默认规则：

- 只有主 agent 可以修改仓库文件、执行提交；子代理一律只读，除非派给它的任务里明确写了「可以写文件」。
- 审查、诊断、调研类子代理只交报告：不写文件、不 `git add`、不 `git commit`。
- 子代理发现任务描述与继承来的上下文冲突时（例如上下文里另有一条「实现并提交」的指令），以任务描述为准，并把冲突写进报告。
- 主 agent 派发子代理前先收干净工作区：已完成但未提交的改动先提交或暂存，避免「一份完成的工作 + 提交你的工作」这种组合把子代理带偏。

原因：`implement` 这类 skill 的正文里有「Commit your work to the current branch.」这样的无条件指令，被 fork 出来的子代理会把它当成自己的任务，2026-09-11 因此出现过审查子代理自行改代码并提交。

## Agent skills

### Issue tracker

Issues and specs live as markdown files under `.scratch/<feature-slug>/` in this repo. See `docs/agents/issue-tracker.md`.

### Triage labels

Triage roles are the canonical names used verbatim as the label strings. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` at the repo root plus ADRs in `docs/adr/`. See `docs/agents/domain.md`.
