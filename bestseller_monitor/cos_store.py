"""图片库的真实现：coscli（spec §3）。

- 桶里已有哪些 key：`coscli ls -r cos://<桶>/img/` 列一遍（内容寻址：key 在 = 字节在）。
- 上传：`coscli cp <本地临时文件> cos://<桶>/<key>`——coscli 只吃文件路径，字节先落临时文件。
- **凭据不进本仓也不进日志**：走 coscli 自己的配置（如 `~/.cos.yaml`）或仓库外凭据目录；
  本模块的命令行里只有桶名与 key，没有密钥。

coscli 是外部二进制，它的输出格式是对外契约；解析只在这一处（`parse_listing`），
认不出来的行当「不是 key」（宁可多传一张，不猜）。已知两种形态（表格、URL 行）与
「形态变了就报错」的闸都在 `parse_listing` 的 docstring 里（真机实测见 ADR-0041）。
前缀口径与 key 的推法共用
`image_store.IMAGE_PREFIX`——分开写迟早分家。
"""
from __future__ import annotations

import pathlib
import subprocess
import tempfile

from bestseller_monitor.image_store import IMAGE_PREFIX, ImageStoreError

COSCLI = "coscli"
_TIMEOUT_SEC = 300


def parse_listing(stdout: str) -> set[str]:
    """从 `coscli ls -r` 的输出里取 `img/` 前缀的 key 集。认两种已知形态：

    - **表格形态**（coscli v1.0.9 实测，2026-09-23 m4）：数据行首列是裸 key，形如
      `  img/ab/<sha>.jpg | MAZ_STANDARD | 2026-09-23T00:51:12+08:00 | "<etag>" | …`；
      表头、分隔行、`TOTAL OBJECTS` 汇总行的首列都够不着 `img/`，自然落空。
    - **URL 行形态**（票 08 起保留的兼容面，无实测样本）：形如
      `cos://<桶>/img/ab/<sha>.jpg   12345   2026-09-20 19:41:00 +0800 CST`。

    两种都不认的行当「不是 key」（宁可多传一张，不猜）。但表格汇总行说有对象、却一个
    key 都没认出（= 输出形态又变了）时报 ImageStoreError——宁可按「图片这半没做成事」
    停下，也不悄悄把全部图片重传一遍（2026-09-23 的缺陷，ADR-0041）。
    """
    keys: set[str] = set()
    total: int | None = None
    for line in stdout.splitlines():
        key = _url_row_key(line)
        if key is None:
            key = _table_row_key(line)
        if key is not None and key.startswith(IMAGE_PREFIX):
            keys.add(key)
        listed = _listed_total(line)
        if listed is not None:
            total = listed
    if total and not keys:
        raise ImageStoreError(
            f"coscli ls 的输出认不出：汇总行说有 {total} 个对象，却一个 key 都没解析出来"
            "——输出形态可能变了，比照 tests/test_image_store.py 的夹具核对 parse_listing。")
    return keys


def _url_row_key(line: str) -> str | None:
    """URL 行形态的 key：`cos://<桶>/` 之后那段（含前缀，留给调用侧筛）；不是这种行返回 None。"""
    marker = line.find("cos://")
    if marker < 0:
        return None
    url = line[marker:].split(maxsplit=1)[0]
    return url[len("cos://"):].partition("/")[2]


def _table_row_key(line: str) -> str | None:
    """表格形态的 key：`|` 分栏行里第一个 `|` 之前的那格就是裸 key；别的行返回 None。"""
    if "|" not in line:
        return None
    head = line.split("|", 1)[0].strip()
    return head or None


def _listed_total(line: str) -> int | None:
    """表格汇总行 `TOTAL OBJECTS:  |  855` 里的对象数；别的行返回 None。"""
    marker = line.find("TOTAL OBJECTS:")
    if marker < 0:
        return None
    cell = line[marker + len("TOTAL OBJECTS:"):].strip().lstrip("|").strip()
    return int(cell) if cell.isdigit() else None


class CosCliImageStore:
    """coscli 驱动的图片库（spec §3 的私有桶）。"""

    def __init__(self, bucket: str):
        self.bucket = bucket

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            done = subprocess.run(
                [COSCLI, *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=_TIMEOUT_SEC)
        except FileNotFoundError as exc:
            raise ImageStoreError(
                f"找不到 {COSCLI}：按上机清单第 10 步装好 coscli 并配好本机 AK"
                f"（只限桶内 {IMAGE_PREFIX}* 前缀）。") from exc
        except subprocess.TimeoutExpired as exc:
            raise ImageStoreError(f"{COSCLI} {args[0]} 超时（{exc.timeout} 秒）") from exc
        if done.returncode != 0:
            detail = done.stderr.strip() or done.stdout.strip() or "（coscli 没有输出）"
            raise ImageStoreError(f"{COSCLI} {args[0]} 失败：\n{detail}")
        return done

    def existing_keys(self) -> set[str]:
        done = self._run("ls", "-r", f"cos://{self.bucket}/{IMAGE_PREFIX}")
        return parse_listing(done.stdout)

    def upload(self, key: str, data: bytes) -> None:
        with tempfile.TemporaryDirectory(prefix="bestseller-image-") as tmp:
            local = pathlib.Path(tmp) / pathlib.Path(key).name
            local.write_bytes(data)
            self._run("cp", str(local), f"cos://{self.bucket}/{key}")

    def fetch(self, key: str) -> bytes:
        """取回 key 的字节：coscli 只吃文件路径，先 cp 到临时文件再读。"""
        with tempfile.TemporaryDirectory(prefix="bestseller-image-") as tmp:
            local = pathlib.Path(tmp) / pathlib.Path(key).name
            self._run("cp", f"cos://{self.bucket}/{key}", str(local))
            try:
                return local.read_bytes()
            except OSError as exc:
                raise ImageStoreError(
                    f"{COSCLI} cp 说成功了，但临时文件读不到（{key}）：{exc}") from exc
