"""Immutable image evidence; network failures do not invalidate inventory."""
import hashlib
import io
from http.client import HTTPException
from PIL import Image
from urllib.request import Request, urlopen

MAX_BYTES = 8 * 1024 * 1024


def acquire(url):
    if not url:
        return {'error': '未提供主图地址'}
    if not url.startswith(('https://', 'http://')):
        return {'error': '主图地址必须是 HTTP 或 HTTPS'}
    errors = []
    for _ in range(2):
        try:
            with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=5) as response:
                content = response.read(MAX_BYTES + 1)
            return evidence(content)
        except (OSError, ValueError, HTTPException) as exc:
            errors.append(str(exc))
    return {'error': '；'.join(errors)}


def evidence(content):
    if len(content) > MAX_BYTES:
        raise ValueError('图片超过 8 MiB')
    try:
        with Image.open(io.BytesIO(content)) as image:
            mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'GIF': 'image/gif',
                    'WEBP': 'image/webp'}.get(image.format)
            if not mime:
                raise ValueError('未取得支持的图片内容')
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('图片内容损坏或尺寸过大') from exc
    return {'content': content, 'mime': mime, 'hash': hashlib.sha256(content).hexdigest()}


def main():
    import argparse
    from pathlib import Path
    from .db import connect, Database
    parser = argparse.ArgumentParser(
        description='重试最新失败的图片记录：商品主图按版本行、SKU 图按流水行；'
                    '不重抓库存，不回填历史日期')
    parser.add_argument('--database', type=Path, required=True)
    retry = parser.add_mutually_exclusive_group(required=True)
    retry.add_argument('--retry-version', type=int,
                       help='商品主图版本行编号（product_information_versions.id）')
    retry.add_argument('--retry-sku-image', type=int,
                       help='SKU 图流水行编号（sku_image_versions.id）')
    args = parser.parse_args()
    conn = connect(args.database)
    try:
        database = Database(conn)
        if args.retry_sku_image is not None:
            print(database.retry_sku_image(args.retry_sku_image))
        else:
            print(database.retry_product_image(args.retry_version))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
