# Standards review — issues0811 ticket 01

- ROLE: read-only subagent; claimed `issues0811_01_standards_ce4de6f` only in the specified isolated clone.
- Scope: entire `69066da421b255168884224a20a28fb20b6d4713...ce4de6f9ea835d0a4a95e7392dc0a9b90fb33225` increment (34 changed files), including startup/cleanup repairs and the bounded mutex-exit synchronization; not merely the last commit. HEAD verified. Working tree contained only untracked handoff material.

## Findings

**Documented violations: none found.**

**Actionable heuristic smells: none found.** This is a judgment, not a claim that the implementation has no possible refactoring opportunities. The cross-file edits carry one end-to-end domain change rather than unrelated responsibilities; existing `StopWatch`, runtime adapter, browser session and round boundaries remain intact. `NewRoundIntent` bundles the preflight facts instead of proliferating independent parameters. Safety-sensitive differences between stop phases do not establish an actionable duplication finding by themselves.

## Standards checked

Sources in `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\`: `AGENTS.md`, `CONTEXT.md`, `docs\agents\domain.md`, `docs\ops\隔离副本与证据收尾规范.md`, and ADRs 0024/0025/0028/0048.

- Domain vocabulary and terminal immutability are consistent with CONTEXT; late or repeated terminal writes retain the actual committed result.
- Browser effects remain inside the target-bound adapter. Original cleanup debt is retained; replacement identities, elapsed deadlines, missing original creation proof and released collection locks are not treated as cleanup proof.
- ADR-0024/0025 explicitly supersede the previous cleanup-timeout and establishment-failure projections, rather than silently contradicting them. Original decision text is retained as history.
- New tracked design/review documents use `docs/`; delivery records distinguish preserved failed full-suite evidence, targeted checks and pending final validation.
- `tests\test_single_instance.py` changes only the existing child-exit test: monotonic two-second bound, real mutex observation, final disappearance assertion. Production mutex code is unchanged, consistent with the explicit authorization.

## Limits

Static review only: no tests, browser launches, accounts, collection, sound or network actions executed. No source changes, staging, commits, stashes, task-pool cleanup or agent dispatch. This report does not certify a successful final full suite, native Windows acceptance, ticket closure, or completion of tickets 02–07 / issues 08 and 11.
