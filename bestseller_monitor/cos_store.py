"""图片库的真实现：coscli（spec §3）。

- 桶里已有哪些 key：`coscli ls -r cos://<桶>/img/` 列一遍（内容寻址：key 在 = 字节在）。
- 上传：`coscli cp <本地临时文件> cos://<桶>/<key>`——coscli 只吃文件路径，字节先落临时文件。
- **凭据不进本仓也不进日志**：走 coscli 自己的配置（如 `~/.cos.yaml`）或仓库外凭据目录；
  本模块的命令行里只有桶名与 key，没有密钥。

coscli 是外部二进制，它的输出格式是对外契约；解析只在这一处（`parse_listing`），
认不出来的行当「不是 key」（宁可多传一张，不猜）。前缀口径与 key 的推法共用
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
    """从 `coscli ls` 的输出里取 key 集：认带 `cos://` 的行，取桶名之后的那段。

    形如 `cos://<桶>/img/ab/….jpg   12345   2026-09-20 19:41:00 +0800 CST` 一行一条；
    汇总行、报错行这类不含 `cos://` 的行直接跳过；前缀不是 `img/` 的（别的用途的对象）不算。
    """
    keys: set[str] = set()
    for line in stdout.splitlines():
        marker = line.find("cos://")
        if marker < 0:
            continue
        url = line[marker:].split(maxsplit=1)[0]
        key = url[len("cos://"):].partition("/")[2]
        if key.startswith(IMAGE_PREFIX):
            keys.add(key)
    return keys


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
