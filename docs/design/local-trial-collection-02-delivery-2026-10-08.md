# 本机试采 02：界面接入交付记录

日期：2026-10-08；分支：`codex/local-trial-collection-01`；基线：`58a513167a340bf701ae97c2bfb23348311b1388`。

## 实施范围

现有 GUI 的显式试采入口、三页同一隔离上下文、手选店与 CLI 模式/配置传递；
跨入口身份核验、暂停/中止和历史续跑，机器锁与关窗机制沿用既有规则。
新增只读跨空间身份查询，避免附着试采时创建/迁移正式库。试采 GUI 日志隔离并恢复原 handler。
正式分析和判断集共享不关闭，不新增试采分析或清空历史入口。

使用方法见 [本机试采界面](../ops/本机试采界面.md)。

## 验证与处置

候选实现已有 16 项 GUI 试采测试及正式 GUI/关闭/视图/共享回归；
实际 HTML 离线 Edge 验证及截图已保存。最终单文件日志、两轴审查与分段全量结果收尾后补入本记录。

主仓：`F:\AI\projects\bestseller`。
02 隔离副本：`.scratch/sandboxes/local-trial-collection-impl/02/20261008-gui/bestseller/`。
主仓证据：`.scratch/logs/tickets/local-trial-collection-impl/02/20261008-gui/`。
副本暂留作为分支交付源，证据保留；未授权或执行副本删除。未创建真实运行数据、远程发布或新浏览器正式 profile。
主仓其他会话未提交文件不纳入本票；未合并 main。
