# Bestseller（1688 SKU 库存快照 MVP）

按 [docs/PRD-1688库存快照MVP.md](docs/PRD-1688库存快照MVP.md) 实现的路线 A MVP：
每天在若干 1688 店铺内按销量排序抓取前若干页商品，进入详情页采集 SKU 名称/价格/库存，
存入 SQLite 并导出 CSV/Excel，跨轮比较库存变化用于估算销量。

## 安装

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

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

浏览器采用“普通进程启动 + DrissionPage 接管”方式（见 `config/config.toml` 的
`start_browser` / `attach_port`），避免被判定为受控会话。价格/库存从页面内嵌 JSON
`skuInfoMap` 解析（见 `bestseller_monitor/parse.py`）。

运行说明：

- 必须使用真实登录过的浏览器 profile（`profiles/account1_edge`），首次请运行
  `python login_chrome.py` 扫码登录一次。
- 出现滑块/登录墙时程序会暂停等待人工处理。
- 中断后可再次运行续跑；每轮只有完整完成后才参与库存差分。
- 结果位于 `data/`（CSV 与原始页面）与 `output/`（Excel 日报）；运行数据均已加入 `.gitignore`。

## 校准说明

1688 页面结构会变化。首次实机运行时若详情页 SKU 或店铺列表解析失败，程序会：

- 把原始 HTML 存到 `data/raw_pages/round_<轮次>/`；
- 在 Excel「失败清单」与日志中标记「需人工/解析失败」；
- 根据存档页面调整 `bestseller_monitor/parse.py` 里的解析规则后重跑即可，其余模块无需改动。

## 免责声明

本项目属未获 1688 许可的自动化采集实验，仅供个人研究；使用真实账号存在被平台风控的风险，
请自行评估并仅将数据用于个人分析。
