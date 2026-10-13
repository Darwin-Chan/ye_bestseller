"""原始页面存档的落点约定（唯一持有者）。

命中拦截 / 人工介入的那一刻、以及既有的详情与榜单失败页，都落进
`cfg.raw_page_dir/round_{round_id}/`。文件名有三种前缀，各自的含义与来历见
[ADR-0057](../../docs/adr/0057-intervention-archive-landing-and-boundary.md)：

- `<offer_id>.html`                —— 详情页失败 / deny（既有命名，补采路径一直用它）
- `<offer_id>.<vtype>.html`        —— 有商品身份的拦截 / 介入（滑块 / 登录墙 / 身份验证）
- `intervention_<safe_shop>.<vtype>.html` —— 榜单首屏命中（没有商品，只有店铺）

**目录怎么拼、文件名怎么起只有这一处**：别的模块不再各自 `mkdir` 或拼字符串，改一处即可
（此前 `detail.save_raw_page` 与 `listing.save_raw_listing_page` 各写了一份，见 ADR-0057）。

调用方负责「html 为空就不落盘」：本模块假定拿到的 `html` 非空；写盘失败按调用方的规矩
处理（介入存证这一路只记日志、绝不中断采集）。
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import Config


def _round_dir(cfg: Config, round_id: int) -> Path:
    return cfg.raw_page_dir / f"round_{round_id}"


def _safe(text: str) -> str:
    """文件名里可用的标识串：只留 ASCII 字母数字与 `_.-`，其余折成下划线。"""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


def _write(path: Path, html: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def relpath(cfg: Config, path: Path) -> str:
    """存档相对 `raw_page_dir` 的路径（事件备注里回指证据文件用的那一份）。"""
    try:
        return path.relative_to(cfg.raw_page_dir).as_posix()
    except ValueError:
        return path.as_posix()


def save_detail_page(cfg: Config, round_id: int, offer_id: str, html: str) -> Path:
    """详情页失败 / deny 的存档：`round_N/<offer_id>.html`。"""
    return _write(_round_dir(cfg, round_id) / f"{offer_id}.html", html)


def save_listing_page(cfg: Config, round_id: int, shop_key: str, html: str) -> Path:
    """榜单失败页的存档：`round_N/listing_<safe_shop>.html`。"""
    return _write(_round_dir(cfg, round_id) / f"listing_{_safe(shop_key)}.html", html)


def save_intervention_page(cfg: Config, round_id: int, offer_id: str, vtype: str,
                           html: str) -> Path:
    """有商品身份的拦截 / 介入存档：`round_N/<offer_id>.<vtype>.html`。

    `vtype` 是 `guard.vtype()` 那一档（`slider` / `login` / `identity`）——按类型分名，是为了
    不让「同一个商品先撞滑块、后吃 deny」的两份证据撞同一个文件名互相覆盖（ADR-0057）。
    """
    return _write(_round_dir(cfg, round_id) / f"{offer_id}.{vtype}.html", html)


def save_listing_intervention_page(cfg: Config, round_id: int, shop_key: str, vtype: str,
                                   html: str) -> Path:
    """榜单首屏命中拦截 / 介入（无商品身份）的存档：
    `round_N/intervention_<safe_shop>.<vtype>.html`。"""
    return _write(_round_dir(cfg, round_id) / f"intervention_{_safe(shop_key)}.{vtype}.html", html)
