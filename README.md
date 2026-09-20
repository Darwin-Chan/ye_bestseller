# Bestseller（1688 SKU 库存快照 MVP）

按 [docs/PRD-1688库存快照MVP.md](docs/PRD-1688库存快照MVP.md) 实现的路线 A MVP：
每天在若干 1688 店铺内按销量排序抓取前若干页商品，进入详情页采集 SKU 名称/价格/库存，
存入 SQLite，跨轮比较库存变化用于估算销量。（同步导出 CSV/Excel 已停用，数据呈现后置为异步。）

## 安装

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

- 界面依赖 `pywebview`（在 `requirements.txt` 里），Windows 上走 Edge WebView2 后端，需要本机有 WebView2 运行时（Win10/11 随 Edge 自带）。

## 配置

- 复制 `config/shops.example.csv` 为 `config/shops.csv`，填入真实店铺 key/名称/URL。
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
- 中断后可再次运行续跑；库存差分按日期口径计算（当前日期库存 − 最近一个更早日期的库存），已放弃或中断轮写入的数据照常参与。
- 结果写入 `F:/AI/bestseller_runtime/data/bestseller.db`；解析失败的原始页面存 `F:/AI/bestseller_runtime/data/raw_pages/round_<轮次>/`；不再自动生成 CSV/Excel。运行数据已移出工作区（路径见 `config/config.toml`）。

## 打包与运行形态

双击的 `dist\bestseller_gui.exe` 是一个**启动壳**，不是自带代码的程序（决定见 [ADR-0007](docs/adr/0007-gui-exe-is-a-shell.md)）：

- exe 里不含项目代码，它只推导项目根、找到本机 python、拉起 `<项目根>\gui.py`；
- 界面 `gui.py`、页面 `docs/ui_live.html`、采集包 `bestseller_monitor`、`run.py` 全部来自源码目录，改这些文件**不需要重新打包**；
- 项目根按 exe 位置推导（`dist` 的上一级），所以 exe 必须待在 `<项目根>\dist\`；可用环境变量 `BESTSELLER_PROJECT` 显式指定；
- 找不到 python、缺 pywebview、界面启动即崩这类失败会弹窗说明，并在 `<项目根>\logs\gui_launcher.log` 留底（自动化验证时设 `BESTSELLER_NO_DIALOG=1`，只落日志不弹窗；`--check --check-report <路径>` 可只做推导与校验并写出 JSON 报告）。

重新打包：

```bash
python tools/build_gui_exe.py
```

脚本会顺带断言产物里没有项目代码（`bestseller_monitor` / `gui`）。

## 校准说明

1688 页面结构会变化。首次实机运行时若详情页 SKU 或店铺列表解析失败，程序会：

- 把原始 HTML 存到 `F:/AI/bestseller_runtime/data/raw_pages/round_<轮次>/`；
- 在数据库（rounds/snapshots）与日志中标记「需人工/解析失败」；
- 根据存档页面调整 `bestseller_monitor/parse.py` 里的解析规则后重跑即可，其余模块无需改动。

## 免责声明

本项目属未获 1688 许可的自动化采集实验，仅供个人研究；使用真实账号存在被平台风控的风险，
请自行评估并仅将数据用于个人分析。

## 独立畅销品分析（第一阶段）

```bash
python analyze.py
```

分析使用独立的 `config/analysis.toml`：`database` 指定现有库存数据库，
`full_capture_weekday` 指定全量抓取提醒星期（1=周一，7=周日）。相对数据库路径以配置目录为准。
可通过 `python analyze.py --config <配置文件>` 切换数据源；`--serve` 只启动本机服务并打印浏览地址，
用于浏览器验收，按 Ctrl+C 退出。

当前支持选择起止日期、并列核对各店当日真实商品／SKU 数、固定库存后查看待确认单商品组和 SKU 观测。
日期使用北京时间，非配置星期会提示数据可能不完整；提示不触发采集或保证全量。
数据读取为只读一致事务，固定后释放连接，不迁移源库、不暂停正在运行的采集。
当前分析刷新页面仍使用原快照；返回日期页重新分析才读取新库存。

当前支持 SKU 销量、规格有效段、独立组人工确认及初步排名；模型同款、持久暂存与导出尚未开放。
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
