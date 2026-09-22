# 1688 畅销品监控（bestseller）

采集若干家固定 1688 店铺的商品榜单与 SKU 库存快照，存进本机 SQLite；多台机器各采各的份额，
经**交换区**互递数据、各自汇总；再只读库存库做**畅销品分析**。

```
1688 店铺（榜单页 / 商品详情页）
  │   ③ 库存数据抓取：gui.py（界面）或 run.py（命令行）
  ▼
本机库存库 bestseller.db ──────────► ② 销量分析：analyze.py（只读）──► 离线 HTML 报告
  │   ① 库存数据交换：exchange.py
  ▼
交换区（git 私有库 + COS 图片桶）──► 别的机器跑 ① 把数据汇总进各自的库
```

三个入口程序各配一只**双击壳**（打包 exe；壳里不含代码，双击后跑的还是源码目录里那一份）：

| 程序（显示名带前缀「1688 畅销品监控 · 」） | 源码入口 | 双击壳 | 干什么 |
| --- | --- | --- | --- |
| 库存数据抓取（短名「界面」） | `python gui.py`（开窗）、`python run.py`（命令行） | `dist\inventory_fetch.exe` | 按店铺榜单翻页，逐个进商品详情页采 SKU 名称/价格/可售库存，写进本机库 |
| 库存数据交换（短名「交换台」） | `python exchange.py` | `dist\inventory_exchange.exe` | 手工跑一趟：检查 → 导出本机周包 → 发布/收取判断集 → 收别人的包 → 汇总进本机库 → 出周报 |
| 销量分析（短名「分析」） | `python analyze.py` | `dist\bestseller_analysis.exe` | 只读库存库，把一次分析固定成快照，人工核对同款后看畅销品，导出 HTML 报告 |

术语与不变量（店铺 / 商品 / SKU / 轮次 / 同款 / 计划外采集…）：[CONTEXT.md](CONTEXT.md)。
设计期的决定与调研：[docs/adr/](docs/adr/)、[docs/research/](docs/research/)。

## 速查

| 我要… | 去哪 |
| --- | --- |
| 装依赖、把一台新机器接进来 | 「安装与上机」一节；逐步骤操作单在 [docs/ops/三机上机清单.md](docs/ops/三机上机清单.md) |
| 改配置 | 「配置」一节；键与占位说明看 `config/*.example.toml` 与 `config/shops.example.csv` |
| 采一轮 | 「库存数据抓取」一节 |
| 跑一趟数据交换 | 「库存数据交换」一节 |
| 做畅销品分析 | 「销量分析」一节 |
| 找库、日志、报告、截图 | 「数据落在哪」一节 |
| 重建壳 / 出壳的发布包 | 「打包与运行形态」一节 |
| 机器上出了问题：记录并打包，交给 Claude 排查 | 双击 `tools\issue_bundle.cmd`；规范在 [docs/ops/问题记录与打包.md](docs/ops/问题记录与打包.md) |
| 找某个文档 | 「文档地图」一节 |

## 安装与上机

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

- 界面依赖 `pywebview`（在 `requirements.txt` 里），Windows 上走 Edge WebView2 后端，需要本机有
  WebView2 运行时（Win10/11 随 Edge 自带）。
- **三只双击壳随仓库发布**：clone 后 `dist\` 里就有它们，装好上面两条依赖即可双击使用。
  壳不含代码，没有本机 Python 与依赖时只会提示「找不到本机 python」。
- 一台新机器成为采集机或纯汇总机的完整步骤（运行根、浏览器与账号、机器编号、凭据档位、
  名册、首次界面、首次交换、验收）在 [docs/ops/三机上机清单.md](docs/ops/三机上机清单.md)——
  四段都可执行，逐屏带做的向导是 `bash tools/setup_wizard.sh`（Windows 双击 `tools\setup_wizard.cmd`）。
  COS 桶与密钥的详细版在 [docs/ops/腾讯云COS上机手册.md](docs/ops/腾讯云COS上机手册.md)。

## 配置

三份配置都在 `config/`，**真配置每台机器自建、不进代码仓**（`.gitignore` 里），仓库只追踪
含全部键与占位说明的示例。复制示例、改成机器自己的值：

| 配置文件 | 缺省来源 | 作用 |
| --- | --- | --- |
| `config/config.toml` | `config.example.toml` | 采集：机器身份、路径、节奏与阈值、浏览器 |
| `config/shops.csv` | `shops.example.csv` | 本机要采的店铺：`shop_key, shop_name, shop_url, pages, active, offer_list_url` |
| `config/analysis.toml` | `analysis.example.toml` | 分析：读哪个库、草稿库、报告目录、模型同款 |

`config.toml` 里几件按机器改的事：

- `[machine]` 声明**本机身份**：`machine_id`（编号一经定下不改，包、周计划与合并账都按它认机器）、
  `role`（`collector` 采集机 = 采集 + 导出 + 汇总；`merge_only` 纯汇总机 = 只汇总与展示，
  采集入口会被拒绝）、`exchange_root`（交换区根）、`cos_bucket`（图片桶名）；
  `git_access` / `cos_access` 只记**凭据档位**，密钥都在仓库外（SSH 在 `~/.ssh`，
  COS 走 coscli 配置或本地 `secrets/`），不进配置、不进日志、不进报告。
- 缺 `machine_id` 或角色写错时启动即报错点名。
- `[paths]` 建议全写本机运行根下的绝对路径：`db_file`、`data_dir`、`logs_dir`、
  `screenshot_dir`、`raw_page_dir`。相对路径以仓库根为基准。
- `[browser]` 的 `user_data_path` 指本机专用浏览器 profile（每台机器一个 1688 账号）；
  首次运行 `python login_chrome.py` 扫码登录一次。采集驱动只有一条：`driver = "pw_cdp"`
  （普通进程拉起浏览器 + Playwright 经调试端口接管），写别的值启动即报错。
- 页数、详情预算、各种延迟与 deny 阈值都在 `config.toml` 里，改完重启程序生效。

`analysis.toml` 的键见「销量分析」一节；模型密钥只从环境变量读（`key_env` 指名变量），
不写进配置、页面或报告。

## 目录布局（运行时 / 设计时）

第一层文件夹要么运行时、要么设计时，两者不共用（决定见 [ADR-0033](docs/adr/0033-repo-layout-runtime-vs-design.md)）：

- **运行时侧**：`bestseller_monitor/`（程序包，含 `pages/` 三个程序页面）、`config/`、
  `dist/`（三只壳唯一的运行位置）、`logs/`（**只放运行时日志**：三个壳的启动器日志在这里，
  程序自身的日志按 `paths.logs_dir`）、`output/`（分析报告缺省落点），
  以及入口脚本 `gui.py` / `run.py` / `analyze.py` / `exchange.py` / `login_chrome.py` 与 `start.bat`。
- **设计时侧**：`docs/`（文档唯一去处）、`shells/`（壳源码与打包 spec）、`tests/`、`tools/`、
  `.scratch/`（工单与过程材料，含工具输出；不进代码仓）。
- 根目录只留三份定论说明：README、CONTEXT、AGENTS。
- 新产物落点：新文档进 `docs/` 相应子目录；证据日志进 `.scratch/logs/`；工具输出进
  `.scratch/tool-output/`（诊断与问题记录包同此）；**别往运行时文件夹（`logs/`、`output/`、
  `dist/`）放设计时产物**。

## 库存数据抓取（采集）

**入口**：双击 `dist\inventory_fetch.exe`，或 `python gui.py`（开窗）；命令行是 `python run.py`。

```bash
python run.py                                        # 开始或续跑一轮（店铺范围与页数按本周计划）
python run.py --pages-per-shop 2 --max-detail 20 --limit-shops A01   # 冒烟：压小页数/预算/店铺
python run.py --ignore-plan                          # 逃生口：拉不到计划库且本地没有本周计划时照开
```

入口参数：`--config`（换配置）、`--shops`（换店铺清单）、`--pages-per-shop`、`--max-detail`
（单轮详情预算）、`--limit-shops`（逗号分隔的店铺白名单）、`--no-shuffle`、`--ignore-plan`。
`run.py` 是唯一带参数的入口；界面与三只壳都不转发参数。

**一轮怎么跑**：

1. 开轮前先走一次准备：拉计划库 → 同步店铺清单 → 确认/生成/发布本周计划 → 落库。
   准备落定前界面不显示窗口；超过 30 秒没落定，有本地已落库计划就标「未能确认最新」照常可用，
   没有就停在闸门（本地没有本周计划时界面不开轮，开始页给出理由与「重试准备」；
   逃生口只在命令行）。
2. 榜单阶段：按 `config/shops.csv` 的店铺逐家翻页（`shop_url` 为店铺首页，`offer_list_url`
   留空则自动拼 `/page/offerlist.htm`），最多 `max_pages_per_shop` 页（缺省 30）。
3. 详情阶段：逐个进商品详情页，价格/库存从页面内嵌 JSON `skuInfoMap` 解析
   （解析规则在 [bestseller_monitor/parse.py](bestseller_monitor/parse.py)）。
   同日已有成功观测的商品默认跳过（同日去重）；点击采集、同名补采与失败补采共享详情预算。
4. 结束：写轮次终态。中断后可再次运行续跑（同一轮、同日期、同店铺范围）；已写入的数据一律保留。
   跨到次日则旧轮按跨天终态收尾、新建轮次。

**人在场时要应答的事**：出现滑块/登录墙等反爬拦截时程序暂停并响铃，等人处理完点继续；
deny（拦截页）会按 `deny_backoff_sec` 自动退避，窗口内同店 deny 到 `deny_shop_limit` 次弃店、
整轮到 `deny_round_limit` 次中止本轮；详情失败率超过 `fail_rate_limit` 时整轮停下等人决策。
界面上的「暂停」＝写下停止请求（轮次保持进行中、可续跑），「中止」＝先把轮次收尾为人工放弃
再停止进程；两者都先请求协作停止，8 秒（进程已回执则再 10 秒）没停下才强制停止。

**每台机器**同一时刻至多一个界面、至多一个采集进程（各持一把内核锁）。抢不到采集锁的进程
以退出码 4 收场；界面与命令行是同一个入口，互斥口径一致。界面关掉不影响正在跑的采集。

**命令行一轮的退出码**：`0` 正常；`2` 没有有效店铺 / 纯汇总机被拒绝采集；`3` 续跑的店铺
范围不符；`4` 已有采集在跑；`5` 计划闸门拒绝开轮（`--ignore-plan` 可越过）；`130` 被 Ctrl+C
打断。这些是命令行入口（`run.py`）的码；界面入口不发退出码，它以窗口里的提示与日志汇报。

## 库存数据交换（交换台）

**入口**：双击 `dist\inventory_exchange.exe`（＝ `python exchange.py --window`，窗口里五个按钮：
导出&汇总 / 仅导出 / 仅汇总 / 发布判断集 / 收取判断集），或命令行 `python exchange.py`。

```bash
python exchange.py                     # 一趟完整交换（检查 → 导出 → 发布判断集 → 拉取 → 汇总 → 收取判断集 → 报告）
python exchange.py --only export       # 只跑一个动作：export / merge / publish / collect
python exchange.py --only publish      # 只发布判断集（--only collect 只收取判断集）
python exchange.py --week 2026-W37     # 补历史：按指定周窗口导出与写报告
```

**一趟做什么**：检查本机与交换区的就位情况 → 把本机这一周的数据导出成周包发进本机那条
`raw-<机器>` 库 → 把本机判断集发进 `judged-<机器>` 库 → 拉取别的 `raw-*` 库与 `plan` 库 →
把别人的包汇总进本机库（不只收本周，缺席机器的历史包会被自动收进来）→ 收取别家发布的判断集
（人工决定并进本机账本：不冲突即生效、冲突进冲突账）→ 写本机视角的周报。导出幂等、导入有
幂等账，判断集同内容重发与重复收取也是空操作，同周重跑是安全的（报告重写同一份，开头
「本次运行」写明这次做了什么）。判断缓存与人工决定账本的位置从 `config/analysis.toml` 读；
没配分析配置时判断集那半跳过并写明原因，其余步骤照常。纯汇总机跳过的是采集包的导出，
**判断集照常发布**（判断库与角色档位无关）。

**退出码**：`0` 干净；`1` 有需要人看一眼的（缺口、冲突、别人的包还没来、判断集本周记下的
冲突——报告里逐条说明）；`2` 本机没做成事（判断集没发出去或没收进来也在这一档，多半是
clone、凭据或 judged 库没就位，看输出第一行）。窗口模式跑完会显示结局行。

**报告与数据**：周报落在 `<交换区根>/报告/<年>-W<周>.md`，固定六节——本机发布 / 收进来的包 /
判断集 / 缺口与还没来的 / 冲突 / 下次该做什么。结构化数据走 git 私有库，图片走对象存储
（`cos_bucket`）。同一台机器同一时刻至多一次交换台运行（窗口与命令行同规；窗口开着时第二个
实例提示后退出 0，命令行被拒打一行说明后退出 2）。

## 销量分析（分析）

**入口**：双击 `dist\bestseller_analysis.exe`（缺省即开窗），或 `python analyze.py`。

```bash
python analyze.py                      # 开窗（本地服务只监听 127.0.0.1 的随机端口）
python analyze.py --serve              # 只起服务并打印地址，浏览器验收用（Ctrl+C 退出）
python analyze.py --config <路径>      # 换配置（缺省 config/analysis.toml）
```

分析独立于采集：**只读**现有库存库，把一次分析固定成快照（只读一致事务，固定后释放连接，
不迁移源库、不暂停正在运行的采集），再把人工整理进度单独存进自己的草稿库。
页面是 [bestseller_monitor/pages/bestseller-analysis.html](bestseller_monitor/pages/bestseller-analysis.html)。
同一台机器同一时刻至多一次分析运行（窗口与 `--serve` 共用一把锁；抢不到锁的实例把已有窗口
叫到前面后退出 0）。

**走一遍**：选开始/结束日期 → 核对两侧「当日真实抓取」数量 → 进入同款确认 → 处理模型建议
（对比、移除、组内新增）→ 筛选与批量确认/撤回 → 暂时保存 → 查看畅销品 → 导出 HTML 报告。
日期用北京时间；非 `full_capture_weekday` 的日子会提示数据可能不完整（只是提醒，不调度采集）。

**配置键**（`config/analysis.toml`，相对路径以配置文件所在目录为基准）：

| 键 | 作用 | 缺省 |
| --- | --- | --- |
| `analysis.database` | 要读的库存数据库 | 必填 |
| `analysis.store` | 分析草稿库（「暂时保存」与「继续上次分析」） | 配置目录下的 `analysis-drafts.sqlite` |
| `analysis.output` | 离线报告落盘目录 | 项目的 `output/` |
| `analysis.full_capture_weekday` | 非全量抓取提醒日（ISO：1 = 周一） | `1` |
| `matching.mode` | `disabled` 不调模型 / `direct` 主模型直收图片 / `caption` 先由视觉模型提证据 | `disabled` |
| `matching.cache` | 同款判断缓存库（独立 SQLite，**不能指向库存库**） | 配置目录下的 `matching.sqlite` |
| `matching.concurrency` / `matching.candidates` | 模型并发（1–8）/ 每商品召回数（1–20） | `2` / `6` |
| `matching.min_score` | 判断预算下限：召回分低于它的对不交模型判断（0 = 不设限；跨机分组对一眼一致要求各机同值，与 `matching.candidates` 同规） | `4` |
| `matching.model` / `matching.vision` | 完整 chat/completions 地址、模型名、`key_env`（密钥所在环境变量名）、超时 | 见示例 |

**数据落在哪**：草稿（日期、固定库存、分组与人工确认/排除/撤回）在 `analysis.store` 指向的
SQLite 文件里；「暂时保存」与「保存分组并查看畅销品」各是一次完整版本提交，重开程序用
「继续上次分析」恢复最近一次成功保存的版本。离线报告写进 `analysis.output`（文件名含日期区间
与导出时间标识，重名加序号不覆盖，可离线打开，不含密钥）。

### 模型同款与判断缓存

在 `matching.mode` 里开：`direct` 用主模型直接理解图片；`caption` 先用独立的
`[matching.vision]` 视觉服务读真实图片、提取款式证据，再把证据和名称送主模型。
只有名称而没有完整历史图片的商品不会进入模型判断。不要因为接口兼容 chat/completions
就假定它支持图片——程序会用随机色块图片核验视觉通路，失败时显示状态，不退化为只按名称判断。
模型请求不跟随重定向（后台要配置最终 API 地址，避免密钥被转发到别处）；配置变更后重启分析程序生效。

召回用名称双字片段和图片感知特征，不预设商品类别；召回不保证找全，也不直接决定同款。
召回分（共享 token 数）低于 `matching.min_score` 的对不交模型判断——这是**预算规则不是判定**：
被挡下的对保持「没判过」＝未知，不产生任何边（分只决定判不判，不在判定里说话），已判过的
照旧命中缓存、不受影响。被挡下的商品状态写「候选低于判断下限，未判断」，与低把握、未召回
同档：重试改变不了它们，放开来要调低下限重启分析程序，控件的「已判断」把它们一并计入。
上限与下限并存：每商品召回数仍由 `matching.candidates` 管，下限只管判不判。缺省 4 的依据是
实测（≤3 分的对占判断预算 42.8%、产 0 条正边；ADR-0040）。
同款组是**由已判边与人工约束推出的划分**，不是「成员两两判过」的完全图：判过同款且高把握的
对把商品连起来（判过但低把握的按负边算：方向保守，与 E2 标定口径一致），没判过的对是未知、
不挡成组——有正边但没判全的商品能进同一组，不再被缺边拆散。
判非同款却被并进一组比拆散真同款更贵（权重随程序版本走、不进各机配置）；已确认／已整理组与
排除对是硬约束，与判断边冲突时约束赢，模型不向已确认组增员。同一判断集＋同一账本＋同一配置，
两次装配逐组一致（ADR-0040）。
判断结果分五态（`未启用` / `缺少证据` / `已判断` / `失败` / `需配置`），确认页的
「模型匹配同款」按钮常驻，只对「存在可重试的失败」可用，其余置灰并在悬停说明原因；
「模型密钥未配置」「模型地址发生重定向」「未配置可用图像能力」属于要改配置的一类，
重试无效，按提示检查配置后重启分析程序。每次点击后按钮旁给出本次结果（已判断数、
仍未成功的原因与下一步建议），结果只属于这一次点击。

`matching.cache` 保存名称/图像证据、模型与规则来源、商品对判断与候选关系；判断按
（版本对，署名摘要）并存——署名是供应商、模型、视觉模型、规则版本四者，同一对证据版本
在不同配置下各有各的结论、互不覆盖；本机只把与自己当前配置同署名的判断当缓存命中，
异署名只作参考、不改本机的分组。每条判断另带来源机器（本机产生的记本机编号，只作显示）。
库存、价格或图片 URL 单独变化不触发重判，图像描述按内容缓存、跨程序重启复用。
该缓存不保存人工草稿。

### 人工核对与调整分组

确认页左侧按每页 20 组列出同款组，点击任一组在右侧核对全部成员。「组内新增商品」可搜索
全部商品的名称、编号或店铺，把选中商品从来源组移动到当前组；「其他疑似归组」打开完整候选
对比，支持逐件放大历史图片、打开当前商品源地址与选择加入。两种人工加入都不复制商品，
空来源组会消失。多商品组可通过垃圾箱或对比弹窗「移出当前组」移除成员；确认后保留原组状态，
并记录被移商品与原组其他成员的排除关系。这些调整只更新当前服务内的分析草稿。

### 商品主图资产

采集在详情提交链里一并保存商品信息版本与主图原始内容，资产按内容 SHA-256 去重，存在库存库的
`product_image_assets` 表里（备份数据库即同时保留历史图片）。每次真实观测都重新读主图，
URL 不变也能识别内容变化；单图最多 8 MiB，一次失败自动重试一次、每次连接超时 5 秒，
最终失败原因记在信息版本里，不影响有效库存提交。分析取结束日及以前**最近一次**信息版本；
无历史图或当次失败时明确缺图，不借用旧图。开库只幂等建表，不回填旧图片。

独立重试某个失败版本的主图（版本编号取自 `product_information_versions.id`）：

```bash
python -m bestseller_monitor.product_images --database <库存库路径> --retry-version <版本编号>
```

重试不改库存、不绕过同日去重；取得的内容按重试时间新增信息版本，保留原失败事实。
它没有重新访问详情页，因此只记录图片证据，名称标记为待重新观测；下一次真实详情采集
会形成新的完整版本。

### 同款分组回放器

`tools/replay_groups.py`：在判断缓存上一次运行、零模型调用的离线复算——召回、读边与装配都
import 生产实现（同源，与重跑一遍分析同数），判断缓存只读打开（打不开时拷一份临时副本、在
副本上读）、不回写缓存、不发任何模型请求。用来给负边分歧权重定参（ADR-0040 挂账③：12 店
全量数据到位后重跑标定）与出验收判据的数。

```bash
python tools/replay_groups.py --cache <matching.cache> --config config/analysis.toml
```

每档装配一行（缺省扫 1／1.5／2／3／4，2 是发行值），首行是退役的团装配冻本、只作对照：

```text
装配 团装配（2026-09-23 退役，只作对照）：558 组（≥2 件 183 · 最大 5） · 规模 2人×137 3人×43 4人×1 5人×2 · 覆盖 89.8% · 纯度 100.0% · 负边入组 0 · 与 payload 相同 558/558 · 0.00 秒
装配 w1.0：554 组（≥2 件 182 · 最大 7） · 规模 2人×136 3人×41 4人×2 5人×2 7人×1 · 覆盖 94.1% · 纯度 98.1% · 负边入组 6 · 与 payload 相同 550/554 · 0.02 秒
```

`--json` 落一份完整报告（分数→同款率曲线、下限挡下的对数与被挡下的正边、每个档位的逐组
对照）；`--store` 给分析草稿库就用真账本当硬约束（缺省按 payload 的已确认组推导），
`--weights` 改扫描档位，`--min-score` 缺省读配置；覆盖／纯度按「命中判断展开到商品对」算，
与 E2 标定实测同口径。

## 数据落在哪

路径以本机 `config/config.toml` 的 `[paths]` 为准（下表是键名与缺省习惯）。
程序不产出 CSV/Excel：数据的对外形态就是库存库与交换包，报告只有分析导出的 HTML。

| 内容 | 位置 |
| --- | --- |
| 库存库（轮次、榜单、快照、事件、计划、导入账…） | `paths.db_file`，习惯放运行根的 `data/bestseller.db` |
| 采集运行日志 | `paths.logs_dir`：`gui.log`（界面，轮转）、`run.log`（命令行一轮） |
| 交换台运行日志 | `paths.logs_dir`：`exchange.log`（逐次流水；周报是一周一份的状态快照，在交换区里） |
| 三个壳的启动器日志 | `<项目根>/logs/`：`gui_launcher.log`、`exchange_launcher.log`、`analysis_launcher.log` |
| 分析服务日志 | 命令行直接看终端；经分析壳起时抄进 `logs/analysis_launcher.log` |
| 人工介入/告警截图 | `paths.screenshot_dir` |
| 解析失败的原始页面 | `paths.raw_page_dir/round_<轮次>/<商品编号>.html` |
| 分析草稿库 / 匹配缓存库 | `config/analysis.toml` 的 `analysis.store` / `matching.cache` |
| 离线分析报告 | `analysis.output`（缺省项目的 `output/`） |
| 交换区周报 | `<交换区根>/报告/<年>-W<周>.md` |

## 打包与运行形态

三个入口程序各配一只**启动壳**（打包 exe；决定见 [ADR-0007](docs/adr/0007-gui-exe-is-a-shell.md)）。
**三只壳随仓库发布**——clone 后 `dist\` 里就是它们；壳正文改动极少，重建只在 `shells/` 改动后
才需要。壳不是自带代码的程序：

- exe 里不含项目代码：它只推导项目根、找到本机 python、拉起源码目录里那一个脚本
  （`gui.py` / `exchange.py --window` / `analyze.py`）；
- 界面、交换台、分析与全部页面、采集包、`run.py` 都来自源码目录，改这些**不需要重新打包**；
- 壳不向子进程转发参数：`--week`、`--only`、`--config`、`--serve` 这类是脚本的参数，
  双击壳没有参数可传——交换台壳与分析壳收到会明确拒绝并提示走脚本；
- 项目根按 exe 位置推导（`dist` 的上一级），所以 exe 必须待在 `<项目根>\dist\`；
  可用环境变量 `BESTSELLER_PROJECT` 显式指定；
- 失败会弹窗说明并留底日志；采集壳与分析壳「非零退出都弹」，交换台壳「退出码 0/1 静默
  （1 是正常结局：有需要人看一眼的），2 与启动失败才弹」。自动化验证时设
  `BESTSELLER_NO_DIALOG=1` 只落日志不弹窗；`--check --check-report <路径>` 可只做推导与校验
  并写出 JSON 报告（用来确认「这只壳认到的项目根与 python 对不对」）。

重建壳（只有 `shells/` 改动后才需要；建哪只传哪个目标）：

```bash
python tools/build_exe.py --target gui
```

```bash
python tools/build_exe.py --target exchange
```

```bash
python tools/build_exe.py --target analysis
```

脚本会顺带断言产物里没有项目代码。壳的源码（launcher 与 `.spec`）都在 `shells/`；
`build/` 只是打包中间产物，可随时清空。壳与机器无关：可以把 `dist\` 下三只 exe 直接拷到别的
机器用（壳自己找本机 python 与源码目录）；重建壳才需要 `pyinstaller`（在 `requirements.txt` 里）。

出发布包（三只壳连同 sha256 清单收成一个目录，供拷到别的机器）：

```bash
python tools/release_shells.py
```

包落在 `.scratch\tool-output\release\shells-<日期>\`，含三只 exe、`MANIFEST.json`（sha256 与
来源提交）与 `发布说明.txt`；接受方把三只 exe 放进 `<项目根>\dist\` 即可。程序改了**不需要**
重发，只有壳正文（`shells/`）改了才要重出包。

## 多机分片采集

每台采集机各采一份份额，通过**交换区**（git 私有库 + 对象存储图片）互递数据、各自汇总。
谁在哪台采、各自采多少页，由**周计划**定。机制与口径全文（交换区布局与包格式、周计划与分配算法、
合并语义、验收口径、上机步骤编号）：[docs/ops/多机分片采集机制.md](docs/ops/多机分片采集机制.md)；
机器身份与副本周计划的决定见 [ADR-0034](docs/adr/0034-machine-identity-and-role-change.md) 与
[ADR-0031](docs/adr/0031-cross-machine-merge-decided-by-package-claim.md)，
通道选型与容量测算见 [docs/research/三机汇总通道调研.md](docs/research/三机汇总通道调研.md)。

- **周计划**：打开采集程序时（命令行则开跑前）自动 pull `plan` 库 → 同步店铺清单 →
  确认（不存在就生成并发布）本周计划——各店归哪台机器、各给多少页；开始页默认勾选本机份额里
  今天还没采够的店，
  人核对用 `plan` 库里的 `plan/<年>-W<周>.md`。
- **库存数据交换**：手工触发，见「库存数据交换」一节；任何一台机器（含纯汇总机）都能跑，
  没有固定的汇总机。补历史用 `--week`，只跑一个动作用 `--only export|merge|publish|collect`。
- **判断库**：每台生产机一本 `judged-<机器>` 私有库（与 `raw-<机器>`、`plan` 并列），
  判断集（模型判断与人工决定）的发布／收取通道——两个动作挂在交换台的整趟里、也能单独跑；
  **各写各的、与角色档位无关**——纯汇总机也照常发布判断。收取名单 = 交换区里实际存在的
  `judged-*` 库，与机器名册解耦。语义与验收口径见
  [ADR-0039](docs/adr/0039-cross-machine-judgment-set.md)。
- **凭据**：每台机器自己的 SSH key 与 COS AK；纯汇总机只持 **COS** 只读档（上传被拒即预期）——
  Gitee 凭据保留可写（判断库要发布判断），`raw-*` 的「不写」由角色闸门承接（导出这半跳过）。
  名册（`plan` 库的 `machines.json`）在对方就绪后才加；越权与计划外采集放行并上报，
  汇总侧按冲突处置。
- **上机与验收**：逐步骤操作单 [docs/ops/三机上机清单.md](docs/ops/三机上机清单.md)
  （采集机接入、纯汇总机、三机验收；核对工具 `python tools/acceptance_check.py`）；
  本机 m4 的过渡期与转角色见 [docs/ops/m4过渡期操作单.md](docs/ops/m4过渡期操作单.md)。
- **每周常态**：每台采集机至少跑一次交换台（导出 + 汇总 + 周报）；报告里「还没来的包」
  「缺口」「冲突」三节照实读，缺口补不了就如实记。

## 出问题怎么办

**先按症状找线索**：

| 症状 | 先看 |
| --- | --- |
| 采集界面报错 / 弹窗 / 拒绝启动 | `<项目根>/logs/gui_launcher.log`（壳侧）与 `paths.logs_dir` 下 `gui.log`（程序侧） |
| 命令行一轮失败 | 终端输出 + `paths.logs_dir` 下 `run.log` |
| 滑块 / 登录墙 / deny | 程序会暂停响铃；原始证据在 `paths.screenshot_dir` 与 `paths.raw_page_dir` |
| 解析不出 SKU / 榜单（页面改版） | 本节末「校准」一段 |
| 交换台退出码 1 / 2 | 终端或窗口的结局行；周报「缺口 / 冲突 / 还没来的包」三节 |
| 交换区收不到别人的数据 | 本机那条 `raw-<机器>` 与 `plan` 库有没有 clone、凭据档位对不对 |
| 分析打不开 / 「分析已经打开」 | 同一时刻只允许一次分析：先找到已有窗口，或看 `logs/analysis_launcher.log` |
| 模型同款按钮置灰 | 悬停看原因；「需配置」一类的按提示改配置后重启分析程序 |

**记录并打包一个问题**（把这台机器上的现场收成一份可以整个拷走的包，交给 Claude 排查）：

```bash
python tools/issue_bundle.py --title "交换台报错：raw-m1 push 被拒"   # Windows 双击 tools\issue_bundle.cmd
python tools/issue_bundle.py --pack                                  # 填好 REPORT.md 后打包成 zip
```

包落在 `<项目根>/.scratch/tool-output/issues/`，含 REPORT.md（人填的描述）、facts.json
（机器、版本、配置、缺项）、日志尾部、库与交换区状态、截图与原始页面，**不含任何密钥**。
规范与「收到包怎么读」：[docs/ops/问题记录与打包.md](docs/ops/问题记录与打包.md)。

**校准**：1688 页面结构会变化。首次实机运行时若详情页 SKU 或店铺列表解析失败，程序会把原始
HTML 存进 `paths.raw_page_dir/round_<轮次>/`，并在数据库与日志里标记「解析失败 / 需人工」；
照存档页面调整 [bestseller_monitor/parse.py](bestseller_monitor/parse.py) 里的解析规则后重跑即可，
其余模块无需改动。

## 文档地图

| 目录 / 文件 | 放什么 | 什么时候看 |
| --- | --- | --- |
| [README.md](README.md)（本文） | 操作层：装、配、跑、落点、排错 | 第一次上手；每次动手前 |
| [CONTEXT.md](CONTEXT.md) | 术语表与不变量（每条都有权威定义） | 写代码、写票、对口径 |
| [AGENTS.md](AGENTS.md) | 多 agent 协作与仓库布局的约定 | 开新会话干活前 |
| [docs/adr/](docs/adr/) | 架构决定（一条一个文件；被取代的也保留） | 想知道「为什么是这样」 |
| [docs/product/](docs/product/) | 需求文档（PRD） | 产品口径 |
| [docs/ops/](docs/ops/) | 操作单与手册：三机上机、**多机机制**、COS、过渡期、验收记录、**问题记录与打包** | 上机、验收、出问题、查多机口径 |
| [docs/research/](docs/research/) | 调研：反爬、账号、通道、滑块等可行性 | 评估路线与风险 |
| [docs/agents/](docs/agents/) | 给 agent 的约定：工单规范、triage 标签、领域文档入口 | agent 建票/检索时 |
| [docs/reviews/](docs/reviews/) | 架构审查报告（归档即终稿，不再改） | 回看某次审查结论 |
| [docs/history/](docs/history/) | 实现进展的历史快照 | 只作考古，别当现状 |
| [docs/out-of-scope/](docs/out-of-scope/) | 明确不做的事（含理由） | 想「加个功能」之前先查 |
| `.scratch/` | 工单、spec、证据日志、工具输出（不进代码仓） | 本机开发会话内部 |
| [tests/](tests/) | 测试（`python -m unittest discover -s tests`） | 改代码后 |

## 免责声明

本项目属未获 1688 许可的自动化采集实验，仅供个人研究；使用真实账号存在被平台风控的风险，
请自行评估并仅将数据用于个人分析。
