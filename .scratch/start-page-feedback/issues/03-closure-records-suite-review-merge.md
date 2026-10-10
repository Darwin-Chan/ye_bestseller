# 03：收口：验收语言与记录、收尾全量、双轴审查、按授权并入 main

Status: ready-for-agent
Type: implementation
Feature: start-page-feedback
Date: 2026-10-10（Asia/Shanghai）
Blocked by: 01、02、04（04 与 02 同文件顺排）
Spec: [../spec.md](../spec.md)（版本 2）

**What to build:** 本批收口。① issues-08-11 验收语言按新口径更新：08-A12/A14 的「入口禁用」表述改为「点击报因＋服务端拒绝」；母 spec 可用性表相关脚注同步（正式模式格不变；试采格以票 08 口径为准），两份文档不得互相矛盾。v2 补充：该措辞更新的范围扩至过程页入口（检测/配置类点击报因、忙/资格类维持禁用）；另在母 spec 描述改范围路径处与《issues 处理状态台账》issues-08-11 小节**各补一句**「改范围按钮已删（快捷方式；路径语义不变，来自 start-page-feedback 票 04）」。② 交付/台账记录按本仓惯例更新（issues 处理状态台账补记等）。③ 隔离副本分段全量：主仓绝对路径 `--log-dir`、不加 `-f`，抄「合计/对账/结论」三行与退出码。④ 双轴独立审查（Standards/Spec，最小上下文只读子代理，两轴不合并排序），报告归档。⑤ 经用户明确授权后用 resolving-merge-conflicts 流程并入 main——未授权不合并、不 push。

## 可验证验收

- [ ] 验收语言更新落盘：08-A12/A14 及母 spec 相关脚注与新行为一致、无互相矛盾；母 spec 与台账各含「改范围按钮已删（快捷方式；路径语义不变）」一句。
- [ ] 交付/台账记录更新（日期、提交、验证结果、证据路径）。
- [ ] 隔离副本分段全量：合计/对账/结论三行齐全、7/7 段 OK、退出码 0；证据落 `.scratch/logs/tickets/start-page-feedback/03/<run>/`。
- [ ] 两轴审查报告归档，0 未解决硬性项/阻断项（或整改后复审通过、记录整改提交）。
- [ ] 并入 main 仅在用户明确授权后执行；合并后按惯例留 main 侧验证证据。

## 接缝

- 验收语言：`.scratch/issues-08-11/spec.md`（含描述改范围路径的行区）与 `.scratch/issues-08-11/issues/08-trial-login-entry-and-start-page-reason.md` 的验收表/措辞；台账 `docs/ops/issues处理状态台账-2026-10-08.md`。
- 审查报告落 `docs/reviews/`（受跟踪）；全量证据落主仓 `.scratch/logs/tickets/`。
- 合流按本仓惯例：分支在隔离副本验证后并入，主仓侧验证与记录提交。

## 非目标

- 无产品代码改动（若审查/全量暴露缺陷，另开票或经用户确认后在本批整改并留痕）；不关闭 issue 08/11（人工项 08-H01/11-H01 仍待用户现场反馈）。
