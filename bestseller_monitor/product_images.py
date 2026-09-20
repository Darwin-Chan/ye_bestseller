"""Immutable image evidence; network failures do not invalidate inventory."""
import hashlib
import io
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
        except (OSError, ValueError) as exc:
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
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('图片内容损坏或尺寸过大') from exc
    return {'content': content, 'mime': mime, 'hash': hashlib.sha256(content).hexdigest()}
