# issues-08-11 票 08：试采入口与开始页原因 · 两轴 code-review（2026-10-10）

- 评价对象：`e2cee2c`（`6b31059` + 1，3 文件：`gui.py`、`bestseller_monitor/pages/ui_live.html`、`tests/test_manual_edge.py`）；固定起点 `6b31059`。
- 方式：两轴独立只读子代理（最小上下文、文件交接、不写不改不提交、两轴不合并排序）；任务文件与报告原文归档在票证据 `.scratch/logs/tickets/issues-08-11/08/20261010-ticket08-01/review-r1/`。静态审查不替代测试；本候选的收尾全量 1946/1946、7/7 段 OK、退出 0。

## Standards

0 硬违例（门禁单点改动、拒绝文案单源两页共用、测试沿该文件体例均核过）。判断项 3 条（均为 Duplicated Code）：

1. `ui_live.html:361` 与 `:565`：`login_refusal` 展示片段逐字同款（文件已有 `stoppingText`/`fmtDur` 小函数体例，可抽 `browserFactsText(d)`）；
2. `gui.py:456-457/562/565-566`：`if refusal is not None: view["login_refusal"] = refusal` 同式三处；
3. `tests/test_manual_edge.py` 三处 merge_only 重写单行句逐字同（可抽 helper）。

处置：**接受、不整改**——0 硬违例、均为两行级体例重复，与既有审查「拒绝文案沿先例、守卫前导块延后」的处置一致；位点如上，后续票顺路整理时可直接引用。全量/回归证据与候选一致（不为体例重整打断已测来源）。

## Spec

0 阻断。(a) 覆盖小注两条：① 过程页按钮的同步可用未在新增用例里直接断言（两页同走 `_browser_entry` 同一 `can_open`，行为随行）；② 验收「邻域不变」由既有用例与全量回归承担而非新用例直接判别。(b) 范围蔓延 0。(c) 与规格相悖 0：豁免仅 `is_merge_only(self.cfg) and not self.cfg.local_trial`（只动 merge_only 分支）；正式文案与行为不变；试采下其余门禁（关闭中/停止收尾/采集互斥/收尾欠账）与 `reserve_open` 去重全保留；`for_collection` 不改 profile 目录（用例已断言）；开始页与过程页同一判词来源、禁用与显示同源。

处置：接受两条覆盖小注（判据同源 + 全量覆盖，不新增冗余用例）。

## 结论

两轴均无未解决硬性项/阻断项；接受 5 条判断项/小注并记录了位点；最终来源 `e2cee2c`（其后仅本文档提交）。
