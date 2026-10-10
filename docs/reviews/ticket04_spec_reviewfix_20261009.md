# 票04 Spec 修复复审

固定范围：802ee29f993b0fe3e642ef5b6145315cb3296c3a → faa2b0ca11f591fc9f8792e2a56a8ff156db7d36（含 9a84408、0d2a554、faa2b0c）。核验 HEAD 等于该候选，工作区干净。认领的是 Spec 任务，不沿用上一轮 Standards 身份。

实施仓根 R = F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller；以下 R\文件:行号指该绝对路径。
规格 S = F:\AI\projects\bestseller\.scratch\issues-08-11\issues\04-start-page-manual-edge.md，并核对联合 spec、grill 决策及交接文件。

## (a) 缺失或部分要求
无新增可操作发现。分段全量和双轴审查仍由父代理完成最终冻结版本收尾；不将待最终全量或票07部署机人工反馈作为代码缺陷。

## (b) 范围蔓延
无发现。两次修复只调整 OS 事实读取和非法配置的观察顺序，未交付票05暂停登录/继续，也未把本票自动化冒充原生登录验收。

## (c) 看似实现但实际错误
无新增发现；初审 P2 可关闭。

1. S 原句：“开始前重查实际目录占用”；“日常 Edge/未知浏览器/无关调试端口实例不关闭、不借用”。R\bestseller_monitor\dedicated_browser.py:87–101 已不在解析前按镜像名过滤。任何可读命令行明确指向同一绝对目录均返回 occupied；已知浏览器无命令行与孤儿子进程仍保守 unknown。R\tests\test_manual_edge.py:338–352 经真实公开 Api 验证未知 Chromium 同目录时不创建浏览器、不起采、不建轮。23 红日志实际 free/occupied 失败，24 绿日志 19 项通过。

2. S 原句：“Edge 缺失、配置错误和 start_browser=false 通过界面给出准确错误；GUI 手动打开及起采拒绝”。faa2b0c 的 R\bestseller_monitor\dedicated_browser.py:163–170 在 observe 前检查配置，非法配置直接 failed/can_start=false。R\tests\test_manual_edge.py:281–292 用真实 BrowserRuntime 验证日常目录拒绝且 observe 未调用；25 红显示曾调用，26 绿 19 项通过。后置复核仍保留，不放宽配置竞态安全性。

其余重复/并发预留、目录/端口安全、创建归属拒绝、规范化配置和 CLI 无 GUI 快照的外接路径未发现修复新增回归。

审查者另做无文件写入 python -B 离线复核：未知同目录 occupied、已知无cmd unknown、无关无cmd free、端口占用 port_busy、未知不同目录 free；非法配置 status/open/start 均拒绝且不 observe。全部符合预期。首次命令因引号 SyntaxError 未执行，重跑成功。未运行真实 Edge、采集或全量。27-final-source-manifest.json 明确 candidate 为上述 faa2b0c；未声称全量已通过。