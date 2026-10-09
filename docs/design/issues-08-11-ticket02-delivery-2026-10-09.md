# 票 02：身份验证等待生命周期（2026-10-09）

## 范围与来源

仅票 02；实施分支 `codex/issues-08-11`，前置基线 `dbdb3180d8ba35f4b8cc4adb5ef9a7c426800c8a` 已包含票 01。未实施 03–07、手动登录入口或主仓集成。已确认决策不重启澄清；自动中止不是自动暂停。

- 主仓/工单/永久证据根：`F:\AI\projects\bestseller`。
- 实施仓：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller`。
- 本票测试副本：`F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\02\20261009-ticket02\bestseller`。
- 证据：`F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\02\20261009-ticket02`。
- 新回放脚本：`F:\AI\projects\bestseller\.scratch\tool-output\issues-08-11\02\20261009-ticket02\identity_dom_replay.py`。

## 公共接缝与行为验收映射

`tests/test_identity_lifecycle.py` 使用真实禁脚本 DOM、全网络拦截、真实 `waiting.until`、公共 `pipeline.run_round`、临时 SQLite、真实轮次/调度/API/公开视图和 shipped 页面。仅控制浏览器建立/关闭副作用、发声、时钟及 native GUI/OS 边界；不把整个判定或等待替换成预设答案。

| 验收 | 真实测试行为 |
|---|---|
| 08-A01 | 原 HTML 脱敏 fixture：普通商品 URL、captcha false，identity 且未解除；原件另建新回放 |
| 08-A02 | 短确认后出现事件、实际 .1 分钟上限、无 reload；公开视图和 shipped 页面准确提示 |
| 08-A03 | 空/非空未知、读取失败、关闭页面均不解；首次未知显式安全停止 |
| 08-A04 | 登录墙→滑块→未知，不发 solved；提示和响铃耗时不重置 deadline |
| 08-A05 | 原目标恢复、其他目标拒绝；公共 run_round 实际处理剩余三商品、同轮完成；合法空业务结果正向证明 |
| 08-A06 / 11-A06 | 真实等待超时写专属终态、不访问下一任务/店铺、保留数据、不可续跑且无自动新建 |
| 08-A07 | 真实 API 暂停、页面点击暂停、native 关窗确认/取消；用户停止先于后来页面解除 |
| 08-A08 | 普通商品引用提示、装饰 URL、合法空结果、反馈/deny；残留验证表单不解；deny 优先和其他类型刷新/超时回归 |
| 08-A15 | 写终态失败传播且新建拒绝；关闭异常门禁 UNKNOWN 保留确切 PID/创建证明；时长收尾失败不掩盖超时 |
| 11-A14 / 11-A16 | 真实结果/导出说明专属自动中止；先提交 ABANDONED 不被超时覆盖 |

首次读取有效性补足 ADR-0030 的同源证据原则，判定与解除都使用同一证据结构。普通浏览器启动、Cookie 或单纯没有商品不当作业务恢复。其他验证仍走既有刷新/普通 InterventionTimeout 分类。

## TDD 与新回放

逐行为红→绿日志 `02`–`37`；复审修复红/绿为 `44`/`45`、`47`/`48`、`50`/`51`、`53`/`54`。`40`–`43`、`46`、`49` 为重定向夹具探索/错误，不充作产品红灯：HTTP 302 路由未覆盖跟随跳转（测试 .example 入口连接失败），后来改用全拦截浏览器 History API 模拟验证地址变化；补正确 UTF-8 和真实弹窗超时。；`14` 和 `24` 是明确记录的夹具错误，不充作产品红灯。旧单元 FakePage/MagicMock 显式补可读业务 DOM 协议，原件真实 DOM 验收不走这些替身。

`38-source-manifest.json` 逐文件核对实施仓与测试副本；`39-original-replay.json` 记录原件 hash、待测 guard hash、禁脚本/零外网与真实读数：正文 251 字、captcha false、identity、resolved false。原件 SHA-256：`249fbbad5507f60ca8c8f7946ede59e976e6461aaaa4ee68affdb111b6ca12b7`。历史诊断原件和旧脚本均保留且未改。

## 收尾状态（待填最终结果）

- 身份生命周期 28 项（含详情导航、榜单、真实弹窗及就地跳转四条重定向回归）与守卫 24 项通过；详情访问、浏览器、榜单、P1、公开视图、停止规则、中止/新建及点击榜单相关单文件回归已执行。
- 语法检查不是类型检查：仓库无配置类型检查器；使用 compileall 和 diff --check 检查。
- 首次独立分段全量：1561 项（预期 1561，计数对账一致），7/7 段 OK，结论 OK，退出 0；证据 full-suite-01。审查修复后须另跑最终全量，不能复用此结果或票 01 的结果。
- 第一轮 Standards 无发现；Spec 发现 P1：验证入口误当原业务目标。已逐行为红→绿修复，访问 adapter 传递重定向前目标；点击卡片在 guard 后读恢复后的商品身份。四项新增回归通过，复审与最终全量待记录；当前不宣布票 02 完成。

## 保留与未完成

实施仓、测试副本、全量与逐行为日志、输入/源码清单、新回放脚本和结果永久保留本次证据；副本暂留以供审查/复核，无可删除候选，未执行清理。任何删除仍需另行授权和边界核验。

部署机真实二次验证/原生人工验收未做（不使用真实账号或日常 Edge/profile）；主仓集成、票 07 联合验收、issue 08/11 父事项结案均未做。单票交付不能替代这些工作。
