# 0011. 界面取数走 views module，时刻与连接从入参来

- 状态：已接受
- 日期：2026-09-13

## 背景

界面的三个页面各自拼同一批查询。过程页与结果页里，deny 计数、完成店铺、店铺总数、未完成店铺、
用同一份店铺指标拼 done 这五段查询逐字重复（`gui.py:364/:791`、`:368/:805`、`:372/:809`、
`:375/:812`、`:383-393/:816-826`）；同一份「本轮计数」在 `tools/summary.py:30-50` 还有第三
种写法。改一处漏一处是迟早的事。

取数还和进程控制挤在同一个类里，`Api` 因此没有可用的构造 interface：测试与基准靠
`Api.__new__(Api)` 逐个塞十个私有属性，共十份（`tests/test_gui.py` 九处、
`tools/bench_refresh.py:46-63`）。基准要量「刷新一次不扫整表」，只能让连接的 `close()`
空转（`_ReusableConnection`）。

更麻烦的是「今天」直读挂钟（`gui.py` 的 `cst_date()` 与 `resumable_on(utcnow())`）。任何
想造「今天这一轮」的 fixture 都得猜真实日期：`tools/bench_refresh.py:72-73` 把合成轮次日期
写死成 `"2026-09-12"`，2026-09-13 跑套件时 `gui.py:352` 找不到当天轮次、走早退分支，
`tests/test_gui.py:885` 与 `tools/bench_refresh.py:158` 双双 KeyError——两条用例在它写下的
那天是绿的，之后每天都是红的。

## 决策

- **三个页面的取数收进 `bestseller_monitor/views.py`**，interface 是三个函数：
  `start_view(conn, *, cfg, shops, state, crawler, now)`、
  `run_view(conn, *, state, now)`、`result_view(conn, *, state, now)`。返回页面直接要的原始
  dict，键就是 `docs/ui_live.html` 读的那些，不新增一层视图对象——pywebview 最终也是
  序列化成 JSON，键就是契约，测试断言的东西和页面拿到的东西是同一个。
- **渲染文案随之下移**：`_fmt_hhmm` / `_fmt_dur` / `_fmt_minutes` / `_terminal_text`
  与终态文案表、以及「采集进程正在跑」那句提示，都归 `views.py`；`gui.py` 不再有任何
  格式化 helper。
- **界面会话事实收成一个值对象**（`views.UiState`，frozen）：本界面认领的轮次编号、
  采集进程是否在跑、是否被用户暂停、停止阶段、已抓时长、子进程被拒的原因。它不是
  数据层事实，也不是采集进程身份（后者由会话锁与身份行回答，见 ADR-0008）。
- **时刻由调用方给**：界面不再读挂钟。「今天」一律 `cst_date(now())`；构造
  `Api(cfg=None, *, now=utcnow, open_conn=None, shops=None)`，`main()` 仍写 `Api()`，
  测试与基准注入固定时刻与自己的库。「今天」是判定轮次身份的输入，判据本身也要时刻
  （`Round.resumable_on()` / `stops_work()` 收 ISO 时刻），所以注入的是时刻而不是日期字符串。
- **连接生命周期留在 `Api`**：读模型只接 `conn`，不负责开合；`Api` 把「开连接」做成构造时
  可注入的可调用对象（默认 `connect(cfg.db_file)`，它建目录、建表并执行迁移，见 IS-37）。
  `start_run` / `resume_run` / `pause_run` / `abort_run` 本来就要自己开连接，把连接搬进
  读模型只会多一个持有者。
- **拒绝形状只有一个出处**：子进程因「已有采集在跑」被拒（退出码 4）这件事折进
  `UiState.start_error`，由 `views.run_view` 统一出那个形状；这条路径不连数据库。
- **`Api` 保留两处写**：`get_run` 取数返回后按 `view["round_id"]` 认领本轮；
  `crawler_identity()` 仍在 `Api`（它要问会话锁、并会清掉残留身份行，不是纯读）。

## 结果

重复的五段查询与第三种计数写法在界面上消失，改一次就够了；界面用例与基准共用一个构造器，
`_ReusableConnection` 随之删掉（基准改为经 `open_conn` 注入裸连接）。日期炸弹拆除：全套
`python -m unittest discover` 从「299 条、2 条 error」变成 307 条全绿。测试迁移是原地换
fixture——`test_gui.py` 的断言没有搬家，新增的 `tests/test_views.py` 只补今天没有面的几件事
（三个页面共用同一份店铺指标、入参决定「今天」、被拒时的拒绝形状，以及过程页要带出的会话事实
和「轮次未完成时用已抓时长」）。

两轴审查（Standards / Spec）在提交之后抓到一处硬违规：`resume_run()` 里的
`resumable_on(utcnow())` 是这次改动漏掉的一处挂钟直读，「今天」在那条路径上仍然由本机时间
决定。已改为 `self._now()`，并补了一条「用固定时刻续跑昨日轮次」的用例——把那行改回
`utcnow()` 这条用例即红。

代价有三处。其一，`Api` 的构造签名长了两个口子（`now`、`open_conn`），读模型也多两个入参，
不读这份 ADR 会以为多此一举。其二，视图返回的是无类型保护的 dict，键名靠
`docs/ui_live.html` 与测试两侧钉住，拼错键不会被静态检查发现。其三，「界面会话事实」是
新词，读代码的人要先认它，才能理解为什么 `elapsed_sec` 这类值是从外面传进 `views` 的。

被否掉的方向：

- **保留 `_today()`、只让它可替换**（模块函数 + monkeypatch）：测试能过，但「什么时候读挂钟」
  这件事仍然散在实现里，fixture 依旧要靠约定而不是接口；而且判据需要的时刻还是拿不到。
- **读模型自己开连接**：`Api` 的四个控制入口本来就要连接，多一个持有者只会让「谁负责关」变模糊。
- **返回 dataclass 再由 `Api` 转 dict**：多一次转换，页面用例还要多一层心智，换来的类型保护在
  这个 JSON 边界上并不成立。
- **把 `test_gui.py` 的纯读断言整批搬进 `test_views.py`**：`Api` 就是那道 interface 的持有者，
  「从 `Api` 进去、得到页面要的 dict」本来就是对的测试面，整批搬迁只是换个地方重写覆盖。
  唯一的例外是终态文案表那条枚举断言——它跟着自己所在的 function 走，`gui._terminal_text`
  已经不存在了。

相关词汇见 [CONTEXT.md](../../CONTEXT.md) 的「界面刷新」「界面会话事实」；来源是
`docs/reviews/architecture-review-2026-09-13.html` 候选 04，规格与工单在
`.scratch/ui-views/spec.md`。
