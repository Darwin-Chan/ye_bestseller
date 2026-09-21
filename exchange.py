"""数据交换台（spec §7）：独立小工具——命令行一次运行 + 小窗口并存。

用法：
    python exchange.py                     # 一次完整运行：检查 → 导出 → 拉取 → 汇总 → 报告
    python exchange.py --only export       # 只跑导出这半（检查 + 导出 + 报告）
    python exchange.py --only merge        # 只跑汇总这半（检查 + 拉取 + 汇总 + 报告）
    python exchange.py --week 2026-W37     # 指定周窗口（补历史用）
    python exchange.py --window            # 开小窗口（完整运行 + 两个次要按钮）

退出码（spec §7）：`0` 干净 / `1` 有需要人看一眼的 / `2` 本机没做成事。逐次流水在
`logs/exchange.log`；周报在 `<交换区根>/报告/<年>-W<周>.md`。窗口标题与快捷方式名用
中文「数据交换台」；窗口不挂进现有采集界面（独立进程、独立锁）。

窗口里两个次要按钮（只导出 / 只汇总）与命令行 `--only` 是同一入口；纯汇总机上
「导出」入口保留并写明跳过（不藏掉），点了就照「本机是纯汇总机，跳过」如实记。
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT))

import webview  # noqa: E402

from bestseller_monitor import exchange as console  # noqa: E402
from bestseller_monitor.config import ROLE_GLOSS, ROLE_MERGE_ONLY, Config  # noqa: E402
from bestseller_monitor.weekly_plan import PlanError  # noqa: E402

WINDOW_TITLE = "数据交换台"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "config.toml",
                        help="配置路径（默认 <项目根>/config/config.toml）")
    parser.add_argument("--only", choices=[console.ONLY_EXPORT, console.ONLY_MERGE],
                        default=None, help="只跑一半：export（检查+导出）或 merge（检查+拉取+汇总）")
    parser.add_argument("--week", default=None, help="指定周窗口，如 2026-W37（补历史用）")
    parser.add_argument("--window", action="store_true",
                        help="开小窗口（完整运行 + 两个次要按钮），不直接跑一次")
    return parser.parse_args(argv)


def _configure_logging(cfg) -> None:
    """逐次流水落 exchange.log（报告是一周一份的状态快照，流水在这里事后可查）。

    `force=True`：同一进程里重复配置（测试、或嵌进别的入口）时换掉旧 handler，
    不然第二次的日志还写在上一次的目录里。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                cfg.logs_dir / "exchange.log", maxBytes=5_000_000, backupCount=3,
                encoding="utf-8"),
        ],
    )


class Api:
    """暴露给窗口前端的方法：状态查询，以及在后台线程里跑一次（轮询取进度行）。

    `run` 可注入（测试用）；真实窗口走 `console.run_once`。一次只允许一个运行在跑。
    """

    def __init__(self, cfg, *, run=console.run_once):
        self.cfg = cfg
        self._run = run
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._running = False
        self.exit_code: int | None = None

    def state(self) -> dict:
        merge_only = self.cfg.role == ROLE_MERGE_ONLY
        return {
            "machine_id": self.cfg.machine_id,
            "role_gloss": ROLE_GLOSS.get(self.cfg.role, self.cfg.role),
            "merge_only": merge_only,
            # 纯汇总机：导出入口保留着，不藏——点之前就把话说在前面。
            "export_note": "本机是纯汇总机：导出这半会跳过" if merge_only else "",
            "report_dir": str(Path(self.cfg.exchange_root) / console.REPORT_DIR),
            "running": self._running,
            "exit_code": self.exit_code,
        }

    def start(self, only: str | None = None) -> dict:
        """开始一次运行（后台线程）；已经有一次在跑时不重复开。"""
        with self._lock:
            if self._running:
                return {"ok": False, "reason": "已经在跑了，等它结束"}
            self._running = True
            self._lines = []
            self.exit_code = None
        threading.Thread(target=self._work, args=(only,), name="exchange-run",
                         daemon=True).start()
        return {"ok": True}

    def poll(self) -> dict:
        """当前进度：是否在跑、到目前为止的流水、上一次运行的退出码。"""
        with self._lock:
            return {"running": self._running, "lines": list(self._lines),
                    "exit_code": self.exit_code}

    def _append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def _work(self, only: str | None) -> None:
        try:
            outcome = self._run(self.cfg, only=only, emit=self._append)
            code = outcome.exit_code
        except Exception as exc:              # 窗口不能因为一次运行出错就死掉
            logging.getLogger(__name__).exception("一次运行出错了")
            self._append(f"运行出错了：{exc}")
            code = console.EXIT_NOTHING_DONE
        with self._lock:
            self.exit_code = code
            self._running = False


def open_window(cfg) -> int:
    """开小窗口；关窗后返回最后一次运行的退出码（没跑过是 0）。"""
    api = Api(cfg)
    html = (ROOT / "docs" / "ui_exchange.html").read_text(encoding="utf-8")
    webview.create_window(WINDOW_TITLE, html=html, js_api=api, width=640, height=540,
                          min_size=(540, 420))
    webview.start(debug=False)
    return api.exit_code if api.exit_code is not None else console.EXIT_CLEAN


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = Config.from_file(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"\n>>> {exc}\n")
        return console.EXIT_NOTHING_DONE
    cfg.ensure_dirs()
    _configure_logging(cfg)
    if args.window:
        return open_window(cfg)
    try:
        outcome = console.run_once(cfg, only=args.only, week=args.week, emit=print)
    except PlanError as exc:                  # --week 写错这类：点名说清楚，不吐栈
        print(f"\n>>> {exc}\n")
        return console.EXIT_NOTHING_DONE
    if outcome.report_path is not None:       # 检查没过（交换区根不在）时没有报告可指
        print(f"\n报告：{outcome.report_path}")
    print(f"退出码 {outcome.exit_code}：{console.VERDICT[outcome.exit_code]}")
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
