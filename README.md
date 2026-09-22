# Bestseller（1688 SKU 库存快照 MVP）

按 [docs/product/PRD-1688库存快照MVP.md](docs/product/PRD-1688库存快照MVP.md) 实现的路线 A MVP：
每天在若干 1688 店铺内按销量排序抓取前若干页商品，进入详情页采集 SKU 名称/价格/库存，
存入 SQLite，跨轮比较库存变化用于估算销量。（同步导出 CSV/Excel 已停用，数据呈现后置为异步。）

## 安装

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

- 界面依赖 `pywebview`（在 `requirements.txt` 里），Windows 上走 Edge WebView2 后端，需要本机有 WebView2 运行时（Win10/11 随 Edge 自带）。
- **双击入口随仓库发布**：clone 后 `dist\` 里已带三只壳（见「打包与运行形态」），装好上面两条依赖即可直接双击使用——壳本身不带代码，没有本机 Python 与依赖时只会提示「找不到本机 python」。

## 目录布局（运行时 / 设计时）

第一层文件夹要么运行时、要么设计时，两者不共用（见 [ADR-0033](docs/adr/0033-repo-layout-runtime-vs-design.md)）：

- **运行时侧**：`bestseller_monitor/`（含 `pages/` 三个程序页面）、`config/`、`dist/`（三只壳）、
  `logs/`（**只放运行时日志**）、`output/`（**只放分析程序生成的报告**），以及入口脚本
  `gui.py` / `run.py` / `analyze.py` / `exchange.py` / `login_chrome.py` 与 `start.bat`。
- **设计时侧**：`docs/`（**文档唯一去处**：`adr/` `reviews/` `agents/` `research/` `product/`
  `ops/` `history/` `design/` `out-of-scope/`）、`shells/`（壳源码与打包 spec）、`tests/`、
  `tools/`、`.scratch/`（工单与过程材料；开发/测试/票证据日志在 `.scratch/logs/`）。
- 根目录只留三份定论说明：README、CONTEXT、AGENTS。

## 配置

- 复制 `config/shops.example.csv` 为 `config/shops.csv`，填入真实店铺 key/名称/URL。
- 复制 `config/config.example.toml` 为 `config/config.toml`（采集配置）、
  `config/analysis.example.toml` 为 `config/analysis.toml`（分析配置）：
  **真配置含机器专属值（路径、端口、机器编号），每台机器自建、不进代码仓**；
  仓库只追踪含全部键与占位说明的示例。采集配置的 `[machine]` 一节声明本机编号
  `machine_id`、角色（`collector` 采集机 / `merge_only` 纯汇总机）、交换区根目录、
  图片桶名 `cos_bucket` 与 git / COS 的凭据档位（只记桶名与档位，密钥在仓库外）；
  缺 `machine_id` 或角色写错时启动报错点名。
- 编辑 `config/config.toml` 调整页数上限、延迟、上限与路径（全部延迟走配置）。

## 运行

```bash
python run.py
```

常用冒烟参数：

```bash
python run.py --pages-per-shop 2 --max-detail 20 --limit-shops A01
```

输入方式（仅店铺模式，商品URL清单抓取已移除）：

- 店铺模式：按 `config/shops.csv` 里的店铺翻页抓榜单（`shop_url` 为店铺首页，
  `offer_list_url` 可显式指定商品列表页 URL，留空则自动拼 `/page/offerlist.htm`）。

浏览器采用“普通进程启动 + Playwright 经调试端口接管”方式（见 `config/config.toml` 的
`start_browser` / `attach_port`），避免被判定为受控会话。价格/库存从页面内嵌 JSON
`skuInfoMap` 解析（见 `bestseller_monitor/parse.py`）。

运行说明：

- 必须使用真实登录过的浏览器 profile（`config/config.toml` 的 `user_data_path`，现为 `F:/AI/bestseller_runtime/profiles/account1_edge`），首次请运行
  `python login_chrome.py` 扫码登录一次。
- 出现滑块/登录墙时程序会暂停等待人工处理。
- 中断后可再次运行续跑；已放弃或中断轮写入的数据照常参与。
- 结果写入 `F:/AI/bestseller_runtime/data/bestseller.db`；解析失败的原始页面存 `F:/AI/bestseller_runtime/data/raw_pages/round_<轮次>/`；不再自动生成 CSV/Excel。运行数据已移出工作区（路径见 `config/config.toml`）。

## 销量分析（独立程序）

分析独立于采集：它只**只读**现有库存库，把一次分析固定成快照，再把人工整理进度单独存进
自己的草稿库。不要求抓取结束才出报表，也不把分析塞进采集流程。

### 启动

```bash
python analyze.py
```

- 打开桌面窗口（pywebview）并起一个只监听 `127.0.0.1` 随机端口的本地服务，页面是
  `bestseller_monitor/pages/bestseller-analysis.html`。
- 双击入口：`dist\bestseller_analysis.exe`（分析壳，见「打包与运行形态」）等价于本命令
  （缺省即开窗）；同一台机器同一时刻至多一次分析——第二次起壳会提示「分析已经打开」、
  尽量把那边的窗口叫到前面，然后安静退出（退出码 0，壳不弹错误框）。
- 只想起服务、不开窗口（调试或远程查看端口）：

```bash
python analyze.py --serve
```

- 换配置：`python analyze.py --config <路径>`，缺省 `config/analysis.toml`。
- 正式入口就是上面这条命令；**不需要开开发控制台调用页面里的隐藏函数**。

### 配置

复制 `config/analysis.example.toml` 为 `config/analysis.toml`（真配置含机器专属值，不进代码仓）。
相对路径以配置文件所在目录为基准。

| 键 | 作用 | 缺省 |
| --- | --- | --- |
| `analysis.database` | 要读的库存数据库 | 必填 |
| `analysis.store` | **分析草稿库**（暂存与「继续上次分析」） | 配置目录下的 `analysis-drafts.sqlite` |
| `analysis.output` | **离线报告落盘目录** | 项目的 `output/` |
| `analysis.full_capture_weekday` | 非全量抓取提醒日（ISO：1=周一） | `1` |
| `matching.mode` | `disabled` 不调模型 / `direct` 主模型直收图片 / `caption` 先由视觉模型提证据 | `disabled` |
| `matching.cache` | 同款判断缓存库 | 配置目录下的 `matching.sqlite` |
| `matching.model` / `matching.vision` | 服务地址、模型名、**密钥读环境变量**（`key_env`） | 见示例 |

模型密钥只从环境变量读，不写进页面也不进配置文件。

### 数据落在哪

- **草稿**（日期、固定库存、分组与人工确认/排除/撤回）：`analysis.store` 指向的 SQLite 文件。
  「暂时保存」与「保存分组并查看畅销品」各是一次完整版本提交；重开程序用
  「继续上次分析」恢复最近一次成功保存的版本。
- **离线报告**：导出写进 `analysis.output`（缺省 `output/`），文件名含日期区间与本次导出的
  时间标识，重名加序号不覆盖；页面会显示生成的文件位置。报告可离线打开，不含密钥，
  也提供不了人工分组编辑或采集控制。

### 走一遍

选开始/结束日期 → 核对两侧「当日真实抓取」数量 → 进入同款确认 → 处理模型建议（对比、
移除、组内新增）→ 筛选与批量确认 / 撤回 → 暂时保存 → 查看畅销品 → 导出 HTML 报告。

验收记录（环境、样本、A01—A46 逐条核对、未执行项）见
[docs/ops/畅销品分析验收记录.md](docs/ops/畅销品分析验收记录.md)。

## 打包与运行形态

三个入口程序各配一只**启动壳**（打包 exe；决定见 [ADR-0007](docs/adr/0007-gui-exe-is-a-shell.md)）：库存数据抓取
`dist\inventory_fetch.exe`（采集壳）、库存数据交换 `dist\inventory_exchange.exe`（交换台壳）、销量分析
`dist\bestseller_analysis.exe`（分析壳）。**三只壳随仓库发布**——clone 后 `dist\` 里就是它们；
壳正文改动极少，重建只在 `shells/` 改动后才需要（见本节末尾）。壳不是自带代码的程序：

- exe 里不含项目代码，它只推导项目根、找到本机 python、拉起源码目录里那一个脚本（`<项目根>\gui.py` / `<项目根>\exchange.py --window` / `<项目根>\analyze.py` 不带参数）；
- 界面 `gui.py`、交换台 `exchange.py`、分析 `analyze.py`、页面 `bestseller_monitor/pages/ui_live.html` / `bestseller_monitor/pages/ui_exchange.html` / `bestseller_monitor/pages/bestseller-analysis.html`、采集包 `bestseller_monitor`、`run.py` 全部来自源码目录，改这些文件**不需要重新打包**；
- 壳不向子进程转发参数：`--week`（补历史）、`--only`（只跑一半）、`--config` / `--serve`（换分析配置 / 只起服务）这类是脚本的参数，双击壳没有参数可传——交换台壳与分析壳收到会明确拒绝并提示走脚本；
- 项目根按 exe 位置推导（`dist` 的上一级），所以 exe 必须待在 `<项目根>\dist\`；可用环境变量 `BESTSELLER_PROJECT` 显式指定；
- 失败会弹窗说明并留底日志（`<项目根>\logs\gui_launcher.log` / `exchange_launcher.log` / `analysis_launcher.log`）：采集壳与分析壳「非零退出都弹」；交换台壳「退出码 0/1 静默（1 是正常结局——有需要人看一眼的），2 与启动失败才弹」。自动化验证时设 `BESTSELLER_NO_DIALOG=1` 只落日志不弹窗；`--check --check-report <路径>` 可只做推导与校验并写出 JSON 报告。

重建壳（只有 `shells/` 改动后才需要；建哪只就传哪个目标）：

```bash
python tools/build_exe.py --target gui
```

```bash
python tools/build_exe.py --target exchange
```

```bash
python tools/build_exe.py --target analysis
```

脚本会顺带断言产物里没有项目代码（`bestseller_monitor` / `gui` / `exchange` / `analyze`）。

壳的源码（4 个 launcher 与三份 `.spec`）都在 `shells/`；`build/` 只是打包的中间产物，可随时清空。
壳与本机无关：可直接把 `dist\` 下三个 exe 拷到别的机器用（壳自己找本机 python 与源码目录）；
重建壳才需要 `pyinstaller`（已在 `requirements.txt` 里）。

出发布包（把三只壳连同 sha256 清单收成一个目录，供拷到别的机器）：

```bash
python tools/release_shells.py
```

包落在 `.scratch\tool-output\release\shells-<日期>\`（设计时工具的落点，`dist\` 只留三只壳本身），
含三只 exe、`MANIFEST.json`（sha256 与来源提交）与
`发布说明.txt`（放置与校验步骤）；接受方把三只 exe 放进 `<项目根>\dist\` 即可。
程序改了**不需要**重发，只有壳正文（`shells/`）改了才要重出包。

## 多机分片采集（机制按实现票落地中）

三台采集机各自采集，通过**交换区**（git 私有库 + 对象存储图片）互递数据、各自汇总；
通道选型与容量测算见 [docs/research/三机汇总通道调研.md](docs/research/三机汇总通道调研.md)。

- **周计划**：打开程序时（命令行则开跑前）采集程序自动 pull `plan` 库 → 同步店铺清单 →
  确认（不存在就生成并发布）本周计划——各店归哪台机器、各给多少页；开始页默认勾选本机份额，
  人核对用 `plan` 库里的 `plan/<年>-W<周>.md`。生成算法与发布步骤在实现票落地中。
- **库存数据交换**（脚本 `exchange.py`，窗口标题用中文全名；双击入口 `dist\inventory_exchange.exe`，
  见「打包与运行形态」）：手工触发一次运行——检查 → 导出本机
  周包 → 拉取别人的包 → 汇总进本机库 → 写本机视角周报（`<交换区根>/报告/<年>-W<周>.md`，
  一周一份、同周重跑重写）；`--only export|merge` 只跑一半，`--week 2026-W37` 补历史，
  退出码 `0` 干净 / `1` 有需要人看一眼的 / `2` 本机没做成事；`--window` 开小窗口
  （导出&汇总 + 仅导出 / 仅汇总三个按钮；壳只开窗、不转发参数，补历史走脚本）。
  同一台机器同一时刻至多一次交换台运行（窗口与命令行同规：窗口开着时命令行会被拒绝）。
- **凭据**：每台机器自己的 SSH key 与 COS AK；纯汇总机只持只读档（push / 上传被拒即预期）。
  密钥都放仓库外，`config.toml` 只记档位。
- 上机步骤（m1 增量、m2/m3 接入、纯汇总机、三机验收）见
  [docs/ops/三机上机清单.md](docs/ops/三机上机清单.md)——四段都可执行（含演练取证与判据；验收
  核对用 `python tools/acceptance_check.py`）；机器本地全文在
  `.scratch/multi-machine-collection/spec.md` §12。

## 校准说明

1688 页面结构会变化。首次实机运行时若详情页 SKU 或店铺列表解析失败，程序会：

- 把原始 HTML 存到 `F:/AI/bestseller_runtime/data/raw_pages/round_<轮次>/`；
- 在数据库（rounds/snapshots）与日志中标记「需人工/解析失败」；
- 根据存档页面调整 `bestseller_monitor/parse.py` 里的解析规则后重跑即可，其余模块无需改动。

## 免责声明

本项目属未获 1688 许可的自动化采集实验，仅供个人研究；使用真实账号存在被平台风控的风险，
请自行评估并仅将数据用于个人分析。

## 独立销量分析（第一阶段）

```bash
python analyze.py
```

分析使用独立的 `config/analysis.toml`：`database` 指定现有库存数据库，
`full_capture_weekday` 指定全量抓取提醒星期（1=周一，7=周日）。相对数据库路径以配置目录为准。
可通过 `python analyze.py --config <配置文件>` 切换数据源；`--serve` 只启动本机服务并打印浏览地址，
用于浏览器验收，按 Ctrl+C 退出。

当前支持选择起止日期、并列核对各店当日真实商品／SKU 数、固定库存后按待确认／已确认／全部三个页签核对同款分组，
确认后在畅销品里展开看各 SKU 的库存与累计销量曲线。
日期使用北京时间，非配置星期会提示数据可能不完整；提示不触发采集或保证全量。
数据读取为只读一致事务，固定后释放连接，不迁移源库、不暂停正在运行的采集。
当前分析刷新页面仍使用原快照；返回日期页重新分析才读取新库存。

当前支持 SKU 销量、规格有效段、模型同款建议、人工确认及初步排名；持久暂存与导出尚未开放。
快照只保留在当前分析服务内存，关闭程序后需重新分析。

采集从现在起在统一详情提交链保存商品信息版本及主图原始内容，资产以 SHA-256 去重，
保存在库存数据库的 `product_image_assets` 中；备份数据库即可同时保留历史图片。
每次真实观测重新读取主图，即使 URL 不变也能识别内容变化；单图最多 8 MiB，
一次获取失败自动重试一次，每次连接超时 5 秒。最终失败原因保存在信息版本中，
不影响有效库存提交；下次真实观测会再次尝试，不绕过既有同日去重规则。
分析选取结束日及以前最近的信息版本；无历史图或当次失败时明确缺图，不借用旧图。
历史主图可放大；商品源图标在点击时读取当前详情地址。开库仅幂等建表，不回填旧图片。

独立重试最新失败图片（版本编号来自 `product_information_versions.id`）：

```bash
python -m bestseller_monitor.product_images --database <库存库路径> --retry-version <版本编号>
```

重试不改库存、不绕过同日去重。取得的内容按重试时间新增信息版本，保留原失败事实，
不会把后来取得的图片回填为旧日期证据；若已出现更新版本，需改用最新失败版本编号。
独立重试没有重新访问详情页，因此只记录图片证据，名称标记为待重新观测，
不把旧名称与当前图片组合成完整信息版本；下一次真实详情采集会形成新的完整版本。

### 模型同款与判断缓存

在 `config/analysis.toml` 的 `[matching]` 设置模式，默认 `disabled` 不调用外部服务。
`direct` 使用主模型直接理解图片；`caption` 先使用独立 `[matching.vision]` 视觉服务
读取真实图片、提取款式证据，再将证据和名称送至 DeepSeek。只有名称而没有完整历史图片
的商品不会进入模型判断。不要因接口兼容 chat/completions 就假定它支持图片；
程序会用随机色块图片核验视觉通路，失败时显示状态，不退化为只按名称判断。

`[matching.model]` 和 `[matching.vision]` 分别配置完整 chat/completions 地址、模型、
`key_env`（密钥所在环境变量名）及超时；密钥放在环境变量中，不写入配置、页面或报告。
配置变更后重启分析程序。`concurrency` 限制并发（1–8）；`candidates` 限制每商品召回数
（1–20）。召回使用名称双字片段和图片感知特征，不使用预设商品类别；召回不保证找全，
也不直接决定同款。模型低把握、接口异常及无候选均有状态，用户仍需核对并人工确认。

`matching.cache` 是独立 SQLite 文件，不能指向库存库。保存名称／图像证据、模型与规则来源、
商品对判断和候选／推荐关系；库存、价格或图片 URL 单独变化不触发重判。
图像描述按内容缓存，成功判断跨程序重启复用。“重试模型匹配”只请求未成功的判断，
并保留本分析中人工确认／调整的成员和排除关系。该缓存不保存人工草稿，关闭分析服务后
仍需重新分析；跨分析人工决策复用在后续工单实现。
低把握但格式有效的判断同样保留缓存、等待人工核对，不因重试自动改判。
模型请求不跟随重定向；后台需要配置最终 API 地址，避免密钥被转发到其他服务。

当前联网验收限制：本机未配置 DeepSeek 和视觉服务凭据，尚未用标注样本验证真实模型质量。
自动测试通过真实库存／分析／缓存／浏览器链路，外部模型使用可控响应；这不代表真实同款准确率。

### 人工核对与调整分组

确认页左侧按每页 20 组列出同款组，点击任一组在右侧核对全部成员。
“组内新增商品”可搜索全部商品的名称、编号或店铺，将选中商品从来源组移动到当前组。
“其他疑似归组”打开完整候选对比，支持逐件放大历史图片、打开当前商品源地址和选择加入。
两种人工加入均保留来源组、目标组各自的确认状态，不复制商品；空来源组会消失。

多商品组可通过垃圾箱或对比弹窗“移出当前组”移除成员。确认后保留原组状态，
记录被移商品与原组其他成员的排除关系，只自动加入唯一匹配的待确认组；
多个匹配或无匹配则保留独立待确认组。单商品组不提供移除入口。
明确人工加入仅覆盖与此次目标成员相冲突的排除，不影响其他排除。

这些调整只更新当前服务内的分析草稿；关闭服务会丢失本轮人工进度。
暂存／重启恢复由 08 实现，跨日期区间复用由 09 实现。
