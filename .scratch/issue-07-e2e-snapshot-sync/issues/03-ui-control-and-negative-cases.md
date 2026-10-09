# 03 补正常 UI 对照与同步失败反例

Status: resolved
Depends on: 01

## 目标
补充真实 UI 用户路径作为对照，并覆盖同步契约的可诊断反例，证明修复的是测试时序而非放宽产品门禁。

## 范围
- 通过真实 UI 完成“确认当前分组 → 保存分组并查看畅销品”，验证正常用户路径仍通过。
- 覆盖并区分：后台未保存、后台已保存但页面未刷新、刷新得到旧/错误分析响应、页面未应用新快照、结果页门禁失败。
- 校验错误信息包含分析 ID/状态或等价诊断上下文，避免把不同层级失败混为“页面没跳转”。

## 验收标准
1. 正常 UI 对照路径通过，并保留“畅销品”结果页断言。
2. 未保存状态、confirmed 仍为 false、旧响应、错误分析 ID 和页面未应用状态各有可识别失败表现；不得通过固定等待、盲目重试或删除断言掩盖。
3. 测试辅助仍限定在测试侧，不代办业务保存，不改变产品 API/页面行为。

## 阻塞
依赖 01；可与 02 并行实施。

## 证据
证据写入主仓 `.scratch/logs/tickets/issue-07-e2e-snapshot-sync/03/`，记录 UI 对照及各反例的失败层级。

## Comments

- 2026-10-10：票 03 在隔离副本收口。UI 对照（AC15）定为**只复用** `test_analysis.py` 三个既有无改动先例（最小「确认当前分组→保存分组并查看畅销品」路径、两组 UI 确认、未全确认门禁与失败保存可重试），三跑全绿；先例即 AC15 定义路径且独立于本线夹具，不造同义测试（决定与盘点见 verdict.md 第一节）。反例（AC03–AC08＋AC16 负例侧）新增 `tests/test_e2e_snapshot_sync_negatives.py`，六用例各钉一个失败层级（R(读取)×3：扣住未放行/同号旧正文回放/别的分析回放；S(保存)×2 子例：初始不脏无保存事实、脏状态；U(落屏)；U(呈现)），故障控制只在测试边界（`page.route`／`add_init_script`／直传快照），未增产品开关；绿 ×2（6/6，41.6–42.1s）。反证红重放六模式全部 red-confirmed（含 AC05 削弱后「别的分析被当成功」、AC07/AC08 吞掉门禁即假成功）。目标回归（票 01、票 02 目标用例、`test_analysis_reuse`、三模块组载 ×2）全 OK。提交 `f0146b1`（副本分支，仅新文件 +369；产品代码与 `snapshot_sync` 零改动；未 push、未合 main）。证据与判定：`.scratch/logs/tickets/issue-07-e2e-snapshot-sync/03/20261010-t03-01/`（verdict.md；red_replay.py 为保留的红能力重放方法）。未执行项：分段全量 / 双维审查 / 原 m2 复验归票 04。

## 目录与证据处置

- 主仓：`F:\AI\projects\bestseller`
- 隔离副本：`F:\AI\projects\bestseller\.scratch\sandboxes\issue-07-e2e-snapshot-sync\01\bestseller`（git worktree，分支 `claude/issue-07-e2e-snapshot-sync`）
- 来源：来源提交 `405c6a7`；副本分支提交 `f0146b1`（父 `449608b`；本票新增 `tests/test_e2e_snapshot_sync_negatives.py`）；测试配置由副本已追踪 examples 创建（登记见 `sandbox.md`）
- 验收证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\issue-07-e2e-snapshot-sync\03\20261010-t03-01\`（负例绿 ×2、UI 对照 ×3、目标回归与组载 ×5、反证红六模式、verdict.md）；分段全量未在本票执行（归票 04）
- 永久保留：上列证据目录全部日志、报告、重放脚本 `red_replay.py`（红能力以保留重放方法＋原始日志证明）
- 可删除候选：无（副本为票 01–04 共享树，随全线下线时统一处置）
- 暂留待确认：无
- 实际清理：未执行
