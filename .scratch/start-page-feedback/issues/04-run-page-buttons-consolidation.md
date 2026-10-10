# 04：过程页按钮收整：入口上移＋点击报因＋删「改范围」

Status: ready-for-agent
Type: implementation
Feature: start-page-feedback
Date: 2026-10-10（Asia/Shanghai）
Blocked by: 02（同一文件顺排；复用其单按钮信息框壳）
Spec: [../spec.md](../spec.md)（版本 2）
决策记录: F:/AI/projects/bestseller/.scratch/start-page-feedback/grill-decisions-run-page.md（工作材料，未跟踪；本机主仓）

**What to build:** 过程页的「打开专用 Edge」上移页头（与开始版同槽、按页签互斥显示；过程页签有轮次才显，结果页签不显），「暂停后处理登录」整卡删除（含浏览器事实行与说明文字）。入口与「继续」跟随票 02 的点击报因口径：非浏览器检测类（采集中/验证等待/停止收尾中/未具备暂停资格/收尾欠账等服务端拒绝类）保持现状置灰、原因挂按钮悬浮提示；浏览器检测类与配置错误类（占用/无法确认/打开失败/端口被占/正在打开/缺 Edge/坏配置）亮起，点击弹单按钮「知道了」信息框报因（复用票 02 的壳）。「继续」can_resume 时显示、不再因 continue_gate 置灰；点击直调既有 resume_run（服务端为权威检查），失败弹信息框原样原因；暂停横幅照旧讲状态，「关闭窗口不会自动继续本轮」句迁入横幅。删「改范围（中止后重选）」前端整链（按钮、禁用行、changeScope 与自动回开始页机制、doAbort 的 changeRange 形参、绑定），替代动线＝中止本轮 →（等收尾）→ 结果页「返回开始」→ 重选 → 明确开始；不加替代按钮与教学旁注。服务端一行不改（改范围＝确认中止＋收尾＋重选＋明确开始的 Q8 语义原样）。

## 可验证验收

- [ ] 过程页签有轮次时，页头右侧依序「打开专用 Edge」「进入本机试采」；无轮次时过程版隐藏；结果页签两版都不显；「暂停后处理登录」整卡与事实行（`runBrowserFacts`）不再存在；开始页入口与事实行保持票 01 交付原样。
- [ ] 忙/资格类（采集中/验证等待/停止收尾中/未具备暂停资格/收尾欠账）置灰照旧且悬浮提示给出原因；检测/配置类在暂停资格具备后亮起、点击弹信息框（同源原因）；被拒绝时不误开（启动计数为零）、不显示假的「已关闭」、不接管无关实例。
- [ ] 「继续」：can_resume 时显示；带 continue_gate（占用/配置等）时不再置灰，点击弹信息框原样原因且 spawn 计数为零；无门禁时照旧恢复原轮、不重复已完成任务；「关闭窗口不会自动继续本轮」在暂停横幅可见。
- [ ] 删「改范围」：按钮与前端机械（含自动跳回开始页）消失；取消确认无副作用；「中止本轮」→ ABANDONED → 结果页 →「返回开始」→ 改勾选 → 开始抓取，新轮意图携带新范围、不再触发范围冲突；Q8 路径语义与服务端拒绝逐项一致。
- [ ] 开始页不受影响：票 01 交付原样，票 02 的投影与信息框不回归。
- [ ] 红→绿：新增/改写页面用例先红后绿（沿用本线第一组高层接缝：实际页面事件 → 真实 Api/配置/视图）；成对更新按下方清单；邻域单文件回归绿。

## 接缝

- `bestseller_monitor/pages/ui_live.html`（行区以落地时现状为准）：页头容器与 `showTab`（两枚元素同槽互斥显隐）；过程页按钮行（删 `changeScopeBtn`）；`runLoginCard` 整卡删除及其显隐行；过程页禁用行与绑定（`runLoginBrowserBtn`、`resumeBtn`、删除 `changeScopeBtn` 绑定）；信息框壳复用票 02 交付的 `#confirmModal` 单按钮变体。
- API 消费：`gui.Api.get_run` 的 `browser` 事实与 `continue_gate`、`open_login_browser` 的 `ok/error/reused`、`resume_run` 的拒绝原因均原样消费；`prepare_abort`/`confirm_abort` 保留（中止本轮/遗留轮共用）；服务端零改动。
- 成对更新测试：`tests/test_paused_manual_login.py`（`runBrowserFacts` 断言改信息框断言）、`tests/test_joint_acceptance.py`（J-A01 同）、`tests/test_explicit_pause_resume.py`（change-scope 用例改走 `#abortBtn`＋`#backBtn`，断言意图不变）、`tests/test_manual_edge.py`（页头槽位与显隐接缝扩展）；`tests/test_ui_live.py` 视信息框接线补页面事件断言。

## 非目标

- 服务端授权/拒绝判据与文案；开始页行为（票 01/02 的交付）；关窗/暂停/继续语义本身；机器角色档位；其它按钮（暂停/中止/试采切换/重试准备/中止此轮）；母 spec/台账补句与验收语言同步（归票 03）。
