"""票据 03 的实现侧对照导出：把原型六组样例跑过 `bestseller_monitor.weekly_plan`，
打印与原型 dump_plans.py 同形的 JSON，供 check_plan_parity.mjs 与原型 JS 模块逐格对照。

样例表与跑法在 `tests/plan_samples.py`（与验收测试共用同一份，避免两处漂移）。

用法：python tools/dump_plan_parity.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from tests.plan_samples import SCENARIOS, parity_view, run_scenario


def main() -> None:
    out = {
        name: [parity_view(doc) for doc in run_scenario(shops, machines, c, b, steps)]
        for name, shops, machines, c, b, steps in SCENARIOS
    }
    print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=1))


if __name__ == "__main__":
    main()
