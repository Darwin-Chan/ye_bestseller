# 0057. 介入存证：落点收在一处、判据层不认识磁盘

- 状态：已接受（2026-10-13）
- 日期：2026-10-13

## 背景

issue 12（滑块拖不过）、issue 10（14 分钟 4 次 deny）、issue 13（6 次 deny 中止）三份现场记录
指向**同一处缺口的两半**：程序明明读到了拦截页/验证页，却把它丢掉，事后只剩口述——滑块页与
deny 拦截页都**零存证**（`data/screenshots/` 空、`data/raw_pages/` 里那些轮次连目录都没建）。

核代码后，丢掉的地方不止一处：点击路径的 `_LateDenied` 把现成的整页 HTML 随异常丢掉；
`ready_detail_page` 命中 deny 记账后一旦越过店铺/整轮阈值就**直接抛异常**，这一路连 HTML 都没读
（而它恰是「赔掉一家店 / 中止整轮」的那一页）；滑块/登录墙/身份验证只发一条事件就进等待。

唯一的存档入口要 `(cfg, round_id, 标识, html)`，而判据点 `guard.wait_for_resolution` 手里**只有
页面**——整条 seam（`begin_detail_visit → ready_detail_page → wait_for_resolution`）从头到尾没有
`round_id`，两个生产调用点（`click_listing`、`pipeline`）手里其实都有。这层身份怎么穿，两条路：

- **加形参**：给 `wait_for_resolution` 加 `cfg + round_id + 标识`，再从两个调用点、`ReadyDetailVisit`、
  `ready_detail_page` 一路补 `round_id`——让**判据层**认识「轮次与磁盘」。
- **回调**：`wait_for_resolution` 只多收一个回调，由调用方组闭包——判据层继续不认识磁盘。

## 决策

**一、命中拦截或人工介入的那一刻，把当时的原始页面留一份，供开发侧事后取证。**

- 覆盖三条「看到了却丢掉」的路：点击路径的 deny、触发店铺/整轮阈值的 deny、滑块/登录墙/身份验证；
  补采路径的 deny 与详情解析失败沿用既有 `save_raw_page`，不动。
- **每次命中都存**（「解决」的那次也是对照——12 现场正是「失败那次没解决、续跑那次 4.2 秒解决」）；
  存**判定命中的那一刻、那次 `page.reload()` 之前**的页面（刷新是程序自己的动作，不该改写证据）。
- **不引入截图**：`data/screenshots/` 是预留落点、生产无写入方；HTML + 事件（含 URL 与判据来源）
  已够比对判据。
- 只作**开发侧事后取证**：落 `data/raw_pages/`（本机运行期、已 gitignore），**不进交换集、不进证据
  版本、不上面**——与 [CONTEXT](../../CONTEXT.md)「完整证据（快照、事件日志、原始页面）不在交换集」
  一致。

**二、判据层不认识磁盘与轮次。**

`guard.wait_for_resolution`（与 `ready_detail_page`）只多收**一个**回调形参
`on_intervention(page, kind, criterion)`；调用方拿自己的 `cfg`/`round_id`/`offer_id` 组闭包，
闭包自己读 `page.content()` 再落盘。`guard` 继续不碰磁盘、不碰 `content()`——与它今天「只收页面、
分钟数、事件回调」的边界一致。这条与 [ADR-0030](0030-intervention-readings-from-one-evidence.md)（判据只有一份）、
[ADR-0056](0056-unread-is-not-absence.md)（读不成不得降级为否定事实）并列，是同一族纪律的第三条：
**软/纯的判定层只交出事实，动作交回调用方**。

**三、落点收在一处，命名按介入类型分。**

- 新建一个薄模块持有唯一约定（目录 `round_N/` ＋文件名前缀），所有写点走它；**不放进 `detail.py`
  或 `listing.py`**——避免引入 `listing → detail` 之类的模块耦合（现两者互不引用）。
- 命名：deny 用 `<offer_id>.html`（与既有补采路径一致、向后兼容）；滑块/登录墙/身份验证用
  `<offer_id>.<vtype>.html`（`vtype ∈ {slider, login, identity}`）；榜单首屏无商品身份时用
  `intervention_<safe_shop>.<vtype>.html`。**按类型分名**是为了不让「同一个商品先撞滑块、后吃
  deny」的两份证据互相覆盖。
- 元数据（URL、判据）**不写 sidecar**：它们已经在 `verification_appear` 等事件里，`event_log` 带
  `round_id`——避免第二份真相。事件备注回指存档路径（「原始页面：`<relpath>`」/`archive=<relpath>`）。

**四、存档绝不影响采集本身。**

空 HTML 不落盘、不建目录（与 `save_raw_page` 今天「非空才写」一致）；写盘失败只记日志、绝不中断
采集主流程（与「记录失败只写日志」同规）。行为面——等待上限、轮询节拍、刷新策略、判据标记表、
deny 阶梯——一律不动。

## 结果与代价

- 12/10/13 的「拦截页零存证」闭合：事后能拿真页面比对判据，「带设备复现一次」从唯一手段降为可选。
- **代价**：每次命中多一次 `page.content()` 读与一次落盘——只发生在异常/介入路径，量很小
  （全库 `verification_appear` 只有 12 次）。
- **被否决的替代方案**：① 加形参把 `round_id`/磁盘穿到判据层——把横切关注点渗进每一层签名，与
  「判据只有一份、判据层只读页面」的边界相悖；② 只补点击路径的 deny——留下「阈值越界那页」与
  「滑块页」两个洞，正是本 ADR 要一次收编的东西；③ 写 sidecar 元数据——制造第二份真相，与事件面
  已有的 `url`/`criterion` 重复。
- **一条判据**：今后凡是「判据层 / 纯函数层」要触碰外部世界（磁盘、进程、网络），先问一句——能不能
  把**动作**交回调用方、只把**事实**（页面 + 判据）传出去？能，就别让判定层认识磁盘。
