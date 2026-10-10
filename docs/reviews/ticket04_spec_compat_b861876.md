# 票04最终兼容复审 — Spec

固定范围：802ee29f993b0fe3e642ef5b6145315cb3296c3a → b861876a6b41d45c71c0e4dabf82affe953248f7；重点复核 faa2b0c → b861876。实施仓 HEAD 与最终候选一致，git status --porcelain 为空。

## 缺失 / 部分实现
无新增可操作发现。核对完整实现差异及上一轮 Spec 修复报告；本次两处兼容补齐不撤销既有配置快照、占用拒绝、窗口归属或 GUI/CLI 模式边界。

## 范围蔓延
无发现。仅基准配置替身和公共测试夹具补字段，没有扩展票05–07的暂停登录、异常旧轮处置或联合验收功能。

## 看似实现但实际错误
无新增发现。

- F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\tools\bench_refresh.py:55–59 补入构造专用浏览器服务所需配置，明确 start_browser=False。Api 构造只建立服务与设置快照；没有调用手动打开。规格原句“只读刷新不产生启动副作用”（F:\AI\projects\bestseller\.scratch\issues-08-11\spec.md:135）不被破坏；基准不会因此启动真实浏览器，亦未静默转换模式。
- F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\tests\helpers.py:91 的 gui_browser_settings=None 与真实 Config 默认值一致，保留非 GUI 的既有路径。规格原句“命令行既有外部连接能力不改”（F:\AI\projects\bestseller\.scratch\issues-08-11\spec.md:120）未被该补丁改变。

## 证据与边界
只读核验 F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\04\20261009-ticket04-01\30-full-compat-green.log：2 个基准测试、7 个公共夹具测试、19 个手动浏览器测试均 OK。29-matching-failure-recheck.log 仅证明既有硬结束用例独立复跑 1 项通过，不能推导全量通过。本代理未运行测试、真实 Edge 或采集；最终分段全量仍由父代理完成，部署机人工验收仍待反馈。未提交、未修改实施仓、未派活。
