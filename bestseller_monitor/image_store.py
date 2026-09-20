"""图片通道的接缝（spec §3）：key 的推法与图片库的接口。

- **key 按内容寻址**：`img/<sha256 前 2 位>/<sha256>.<扩展名>`，扩展名由 mime 定。
  同一份字节在哪台机器上都是同一个 key，所以「只传新增」= 只需知道桶里已有哪些 key，
  「只拉缺失」= 只需知道本机还缺哪些内容哈希。
- **`ImageStore` 是图片库的接口**（`existing_keys` / `upload` / `fetch`）：导出侧与汇总
  导入侧都只对着它说话，真实现（coscli 驱动）与用例替身都走同一条缝。coscli 是外部
  二进制，算系统边界。
"""
from __future__ import annotations

from typing import Protocol

_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}

# 桶内前缀：key 的推法与「列哪些 key」共用这一处，口径不会分家。
IMAGE_PREFIX = "img/"


class ImageStoreError(RuntimeError):
    """图片库操作失败（列表、上传）；消息带命令与输出摘要，供上游告警引用。"""


def image_key(content_hash: str, mime: str) -> str:
    """内容寻址的图片 key。mime 是采集侧存下来的那四种之一（product_images.evidence）。"""
    ext = _MIME_EXT.get(mime)
    if not ext:
        raise ImageStoreError(f"不认识的图片 mime：{mime!r}（只存受支持的那四种）")
    return f"{IMAGE_PREFIX}{content_hash[:2]}/{content_hash}.{ext}"


class ImageStore(Protocol):
    """图片库：问「已有哪些 key」，按 key 传字节、按 key 取字节。"""

    def existing_keys(self) -> set[str]:
        """桶里现存的 key 集（限 `img/` 前缀）。"""

    def upload(self, key: str, data: bytes) -> None:
        """把字节传到 key 上；失败抛 ImageStoreError。"""

    def fetch(self, key: str) -> bytes:
        """取回 key 上的字节（汇总导入拉图用）；失败抛 ImageStoreError。"""
