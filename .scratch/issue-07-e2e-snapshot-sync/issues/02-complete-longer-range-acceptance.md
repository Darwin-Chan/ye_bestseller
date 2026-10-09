# 02 接入完整换更长区间业务验收

Status: resolved
Depends on: 01

## 目标
将工单 01 的保存后同步接缝接入完整 e2e 业务场景，恢复并稳定验证：换到更长日期区间后，适用的已保存人工确认可以复用、销量重新计算，并进入“畅销品”结果页。

## 场景与不变量
1. 短区间准备两商品并合组、确认并保存。
2. 保存后经 S/R/U 同步核对，页面进入“畅销品”；短区间商品销量分别为 5、组销量为 10。
3. 通过真实页面流程切换到 `2026-09-21` 的更长区间。
4. 新区间复用适用的已保存确认；商品销量为 25、35，组销量为 60；页面最终进入“畅销品”。
5. 原短区间分析仍保持组销量 10，证明重算不污染旧分析。

## 验收标准
- 保留原测试的“换更长区间、确认复用、销量重算、畅销品”断言，不以更宽断言或删除门禁变绿。
- 后台保存完成、浏览器拿到对应新快照、页面应用新状态三者可诊断区分。
- 不引入固定 sleep、偶然重试或产品逻辑改动。

## 阻塞
依赖 01 提供稳定同步辅助及旧快照红证据。

## 证据
证据写入主仓 `.scratch/logs/tickets/issue-07-e2e-snapshot-sync/02/`，包含目标用例红→绿回放及短/长区间数值核对。

## Comments

- 2026-10-09：票 02 在隔离副本收口。红：起点红 ×1（接线前树 `b1c6ae64`，票 01 方法重放原用例，不补刷新）+ 反证红 ×2（接线后树 `449608b0`，唯一差异＝`sync_after_save` 换不刷新页面的空操作）；三趟红均落在原「畅销品」断言处，区分三真（后台已保存 / 页面未刷新 / 结果页门禁未通过）。绿：目标用例 ×2 OK（4.796s / 4.557s）＋数值见证 ×1（numbers_ok 7 项全 True：短 5/5/10、长 25/35/60、旧分析 09-14/10 长区间后复读不变、页面详情含 25/35/60）＋双模块组载 ×2 OK ＋ `test_analysis_reuse` 10/10 OK ＋ 草稿四态邻接 OK。提交 `449608b`（副本分支 `claude/issue-07-e2e-snapshot-sync`，仅 `tests/test_e2e_acceptance.py` +32/−2；原断言一条未弱化；产品代码零改动；未 push、未合 main）。证据与判定：`.scratch/logs/tickets/issue-07-e2e-snapshot-sync/02/20261009-t02-01/`（verdict.md 为判定全文；red_replay.py 与 green_witness.py 为保留方法）。未执行项：分段全量 / 双维审查 / 原 m2 复验归票 04；正常 UI 对照与对抗性反例归票 03。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`
- 隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\issue-07-e2e-snapshot-sync\01\bestseller`（git worktree，分支 `claude/issue-07-e2e-snapshot-sync`；票 01–04 共用树）
- 来源：来源提交 `405c6a7`；副本分支提交 `b1c6ae6`（票 01）→ `449608b`（本票，`tests/test_e2e_acceptance.py`）；测试配置由副本已追踪 examples 创建（登记见 `sandbox.md`）
- 验收证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\issue-07-e2e-snapshot-sync\02\20261009-t02-01\`（红 ×3、绿 ×2、数值见证 ×1、邻接 ×4、verdict.md）；分段全量未在本票执行（归票 04）
- 永久保留：上列证据目录全部日志、报告、`red_replay.py` 与 `green_witness.py`（红能力与数值核对以保留方法＋原始日志/JSON 证明）
- 可删除候选：无（副本为票 01–04 共享树，随全线下线时统一处置）
- 暂留待确认：无
- 实际清理：未执行
