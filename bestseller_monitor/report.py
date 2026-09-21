"""离线报告的落盘：本地程序真正写入 output，绝不静默覆盖已有文件。"""
import os
from pathlib import Path


def report_name(start: str, end: str, stamp: str) -> str:
    """文件名含分析日期区间与本次导出的时间标识，重名由写入端加序号区分。"""
    compact = ''.join(character for character in stamp if character.isdigit())[:14]
    return f"bestseller-{start}_{end}-{compact}.html"


def write_report(directory: Path | str, name: str, html: str) -> Path:
    """把报告写进目录并返回实际路径；名字已占用就加序号，失败不留半份文件。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target, counter = directory / name, 2
    while True:
        try:
            # O_EXCL 一边占名一边建文件：并发导出也不会撞进同一份文件。
            handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            break
        except FileExistsError:
            target = directory / f"{Path(name).stem}-{counter}{Path(name).suffix}"
            counter += 1
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(html.encode('utf-8'))
    except BaseException:
        # fdopen 失败时描述符还开着；写入中途失败也先关再删，Windows 上才删得掉。
        try:
            os.close(handle)
        except OSError:
            pass
        # 失败不留半份文件：页面上不能出现被当成成功的报告。
        target.unlink(missing_ok=True)
        raise
    return target
