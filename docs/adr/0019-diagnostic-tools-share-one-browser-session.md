# 0019. 诊断工具共用一个浏览器会话

- 状态：已接受
- 日期：2026-09-13

## 背景

ADR-0008 末尾留了一句没做的话：「`tools/diag_*.py` 也抢 9222……另行处理。」这句拖到现在的
后果是：每个诊断脚本自己写一遍「起浏览器 → 等 → 接管 → 收尾」，同一段代码逐字重复。

`diag_card_selector.py:22-30` 与 `diag_list_dom.py:22-30` 一字不差（含硬编码的 Edge 路径、
`PORT = 9222`、`time.sleep(9)`），`diag_offer_id_presolve.py:96-104` 是第三份。收尾一律是：

```python
br.close(); pw.stop()
subprocess.run(["taskkill", "/IM", "msedge.exe", "/F"], capture_output=True)
```

最后这行**会把机器上所有 Edge 窗口一起关掉**，包括用户自己开的。与此同时，`browser_pw` 里
已经有现成的一对实现（`open_session` / `close_session`，附 14 条收尾用例）：端口、用户数据
目录、Edge 路径从 `Config` 来，「就绪」以调试端口能连上为准（上限 25 秒），收尾按 CDP 的
browser PID / 端口占用者归属。`diag_card_urls` / `diag_verify_state` / `sync_list_titles`
已经在用它，只有那三个还在手搓。

## 决策

- **诊断工具不再自己起浏览器**：三个手搓的改走
  `browser_pw.open_session(cfg)` → `try: … finally: browser_pw.close_session(pw, br)`。
  端口 / profile / Edge 路径 / 就绪等待 / 收尾归属这五个事实只在 `browser_pw` 定义。
- **删掉固定 `time.sleep(9)`**：`open_session` 已经用「能连上调试端口」做条件等待（`_WAIT_LAUNCH_SEC`
  25 秒兜底），通常几百毫秒就能连上，连接失败才等满。
- **删掉 `taskkill /IM msedge.exe /F`**：这是本条最实的一处修正。收尾改由 `close_session`
  按归属关——先问 CDP 要真实 browser PID，取不到再退回调试端口占用者，且只关本次启动的那个。
- **补上会话没建起来那一支的收尾**：`open_session` 连不上调试端口时，原来只 `pw.stop()` 就上抛，
  它自己 Popen 起的那个 msedge.exe 留在后台占着共享 profile。新增
  `_abandon_launched_browser()`：只结束「我们拉起、而且现在还活着」的那一个
  （`proc.poll() is None` 才动手）；同一 profile 已有实例时本次进程交接后立刻退出，
  `poll()` 有值，端口上那个是用户自己的浏览器，绝不去动它。这条对采集进程与界面同样生效。
- **加一条结构护栏**：`tests/test_tool_sessions.py` 断言 `tools/` 下不再出现
  `remote-debugging-port` 与 `"taskkill", "/IM"` 两个指纹。工具是手动脚本，行为测不了，但
  「调试端口只准有一处」这条结构判据测得了。

## 结果

- **四处有意的行为变化**：
  1. 启动等待从固定 9 秒变成最多 25 秒的**条件等待**——正常情况更快，端口始终连不上时更慢
     （但那种情况本来也是失败）。
  2. 收尾不再杀光所有 Edge，只关本任务启动的那个；`start_browser=false` 接管既有实例时
     明确跳过关闭，并留下可见记录。诊断脚本此前会顺手关掉用户正在用的 Edge 窗口。
  3. `diag_card_selector` / `diag_list_dom` 的页面默认超时从 Playwright 缺省 30 秒变成
     `cfg.timeout_ms`（本机配置 45 秒）——它们此前没设过，`open_session` 统一设。
     `diag_offer_id_presolve` 本来就写 45000，不变。
  4. 收尾搬到 `finally`：旧码把 `br.close(); pw.stop(); taskkill` 写在脚本末尾，中途异常就
     跳过了收尾；现在任何异常退出都会关掉本次启动的浏览器。
- `diag_card_selector` / `diag_list_dom` 的主流程收成 `main()`（会话）+ `_diagnose(page)`
  （读什么、打印什么）；`diag_offer_id_presolve` 同理收成 `main()` + `_diagnose(page, url)`。
  各工具自己的页面节拍（等滑块 40 秒、滚轮懒加载、`sleep(6)`）原样保留——那些等的是页面
  内容，不是浏览器就绪。

不动的部分：

- `tools/_shot_live.py` / `_shot_mockup.py` 用 `chromium.launch(headless=True)` 渲染本地
  HTML 截图，不接调试端口、不碰用户的 profile，与这里的「会话」不是一回事。
- `diag_edge_cleanup.py` 已经在用 `open_session`，它自己的 `tasklist` 只读计数，不动。

被否掉的方向：

- **给 `browser_pw` 加 context manager**（`with session(cfg) as page:`）：能让工具更短，但要动
  `browser_pw` 的公开面，收益抵不上——候选 05 才动这类形状。
- **把工具内部的页面等待也收走**：它们在等滑块、懒加载和固定观察窗口，属于各工具自己的
  诊断语义，收进会话层会改变诊断行为。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「浏览器会话」；来源是
`docs/reviews/architecture-review-2026-09-13-r2.html` 候选 03，规格在
`.scratch/diag-session/spec.md`。
