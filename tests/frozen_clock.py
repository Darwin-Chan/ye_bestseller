"""测试时钟替身：把采集链读到的「现在」钉在 FROZEN_NOW，夹具建轮的「今天」同源。

轮次判停（`Round.stops_work`）拿「现在」和轮次日期、当日截止线（23:55）比，而夹具
默认按运行当天建轮次：于是每天 23:55–24:00 跑套件，凡走采集路径的用例都撞上
DayBoundaryReached（2026-09-20 深夜实测 583 项里 64 errors + 3 failures 全是它）。
钉住之后任何时刻跑都等价于「白天跑」；要测截止线本身，用例照常在更内层
`patch.object(detail, "utcnow", ...)` 给更具体的替身——内层优先，不受这里影响。

复现手法（任何时刻都行）：PYTHONPATH 里放一个 sitecustomize.py，把
`bestseller_monitor.db.utcnow` / `cst_date` 钉在 23:56 再跑套件——修前大片红、修后绿。
"""
from contextlib import ExitStack, contextmanager
import sys
from unittest.mock import patch

from bestseller_monitor.db import cst_date

# 测试里「现在」的唯一取值：北京时间 2026-09-14 12:00——白天，远离 23:55 的当日截止线，
# 也不与任何用例手写的日期字面量撞车。
FROZEN_NOW = "2026-09-14T04:00:00+00:00"
FROZEN_DATE = "2026-09-14"

# 冻结要替换 cst_date 这个名字，替身里只能用替换前取好的原函数，否则递归。
_real_cst_date = cst_date


@contextmanager
def frozen_clock():
    """把采集链读到的「现在」全部钉在 FROZEN_NOW，夹具建轮的「今天」同源（FROZEN_DATE）。"""

    def frozen_utcnow() -> str:
        return FROZEN_NOW

    def frozen_cst_date(iso_utc: str | None = None) -> str:
        return FROZEN_DATE if not iso_utc else _real_cst_date(iso_utc)

    with ExitStack() as stack:
        for module in _clock_holders():
            if hasattr(module, "utcnow"):
                stack.enter_context(patch.object(module, "utcnow", frozen_utcnow))
            if hasattr(module, "cst_date"):
                stack.enter_context(patch.object(module, "cst_date", frozen_cst_date))
        yield FROZEN_NOW


def _clock_holders():
    """已经 import 过这两个时钟函数的模块（生产、用例与测试支撑模块都算）。

    `from .db import utcnow` 会把函数绑进各模块自己的命名空间，只换 db 一处盖不住已经
    绑好的副本；两个名字各换各的（有的模块只拿了其中一个）。
    """
    holders = []
    for name, module in list(sys.modules.items()):
        top = name.split(".")[0]
        if module is None or not (top in ("bestseller_monitor", "helpers", "tests")
                                  or top.startswith("test_")):
            continue
        if hasattr(module, "utcnow") or hasattr(module, "cst_date"):
            holders.append(module)
    return holders
