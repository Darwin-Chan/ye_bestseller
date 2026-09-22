"""库存数据交换（spec §7）：独立小工具——命令行一次运行 + 小窗口并存。

用法：
    python exchange.py                     # 整趟：检查 → 导出 → 发布判断集 → 拉取 → 汇总 → 收取判断集 → 报告
    python exchange.py --only export       # 只跑导出（检查 + 导出 + 报告）
    python exchange.py --only merge        # 只跑汇总（检查 + 拉取 + 汇总 + 报告）
    python exchange.py --only publish      # 只发布判断集（检查 + 发布 + 报告）
    python exchange.py --only collect      # 只收取判断集（检查 + 收取 + 报告）
    python exchange.py --week 2026-W37     # 指定周窗口（补历史用）
    python exchange.py --window            # 开小窗口（整趟 + 只跑某一个动作）

退出码（spec §7）：`0` 干净 / `1` 有需要人看一眼的（含判断集这趟新记下的冲突）/ `2` 本机
没做成事（含判断集没发出去、没收进来）。逐次流水在 `logs/exchange.log`；周报在
`<交换区根>/报告/<年>-W<周>.md`。窗口标题与快捷方式名用中文全名「1688 畅销品监控 ·
库存数据交换」；窗口不挂进现有采集界面（独立进程、独立锁）。

**运行互斥**（票 14）：同一台机器同一时刻至多一次交换台运行——窗口与命令行同规，
锁在这里取（不放启动壳里，改脚本不用重打包）：窗口开着时锁在窗口进程手里，第二个
实例（双击第二次壳、或命令行）拿不到锁——窗口模式弹 MessageBox 后退出 0（照 ADR-0008
的约定让启动壳保持安静），命令行打一行说明后退出 2。锁在每次运行结束时释放。

**壳与参数**：`dist/inventory_exchange.exe` 只负责开窗（等价 `--window`），不转发参数；
`--week`（补历史）、`--only`、`--config` 走本脚本。窗口不接周入口：`--window` 与
`--week` 同传明确拒绝。

窗口里「导出&汇总 / 仅导出 / 仅汇总 / 发布判断集 / 收取判断集」五个按钮与命令行 `--only`
是同一入口；一次运行结束显示 **结局行**（与命令行收尾同一句：报告路径 + 退出码口径）。
纯汇总机上「仅导出」入口保留
并写明跳过（不藏掉），按钮置灰；点了就照「本机是纯汇总机，跳过」如实记。
运行中关窗不拦、只记录（重跑能修：导出幂等、导入有幂等账、报告同周重写）。
**窗口起不来按「本机没做成事」（退出码 2）退出**：缺 pywebview、页面读不到、WebView2
起不来这类启动失败都折成 2，启动壳的弹窗政策（2 才弹）因此也管得住子进程侧的启动失败。
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT))

try:
    import webview  # noqa: E402
except ImportError as _exc:                # 缺 pywebview：命令行还能跑，开窗时点名报错
    webview = None
    _WEBVIEW_IMPORT_ERROR: ImportError | None = _exc
else:
    _WEBVIEW_IMPORT_ERROR = None

from bestseller_monitor import exchange as console  # noqa: E402
from bestseller_monitor import single_instance  # noqa: E402
from bestseller_monitor.config import ROLE_GLOSS, ROLE_MERGE_ONLY, Config  # noqa: E402
from bestseller_monitor.weekly_plan import PlanError  # noqa: E402

WINDOW_TITLE = "1688 畅销品监控 · 库存数据交换"

_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "config.toml",
                        help="配置路径（默认 <项目根>/config/config.toml）")
    parser.add_argument("--only", choices=[console.ONLY_EXPORT, console.ONLY_MERGE,
                                           console.ONLY_PUBLISH, console.ONLY_COLLECT],
                        default=None,
                        help="只跑一个动作：export（导出）/ merge（拉取+汇总）/ "
                             "publish（发布判断集）/ collect（收取判断集）")
    parser.add_argument("--week", default=None, help="指定周窗口，如 2026-W37（补历史用）")
    parser.add_argument("--window", action="store_true",
                        help="开小窗口（整趟 + 只跑某一个动作），不直接跑一次")
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

    def close_note(self) -> str | None:
        """关窗时的一句话：运行还没结束就如实记一笔（只记录、不拦）。"""
        with self._lock:
            if not self._running:
                return None
        return ("运行还没结束就关窗了：这一趟可能半途而废；重跑能修"
                "（导出是幂等的、导入有幂等账、报告同周重写）。")

    def _append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def _work(self, only: str | None) -> None:
        try:
            outcome = self._run(self.cfg, only=only, emit=self._append)
            code = outcome.exit_code
            report = outcome.report_path
        except Exception as exc:              # 窗口不能因为一次运行出错就死掉
            logging.getLogger(__name__).exception("一次运行出错了")
            self._append(f"运行出错了：{exc}")
            code, report = console.EXIT_NOTHING_DONE, None
        # 结局行：与命令行收尾同一句话（报告路径 + 退出码口径），窗口里也要看得到
        if report is not None:
            self._append(f"报告：{report}")
        self._append(f"退出码 {code}：{console.VERDICT[code]}")
        with self._lock:
            self.exit_code = code
            self._running = False


def _notify(text: str, title: str = WINDOW_TITLE) -> None:
    """一句给人看的提示；弹不出来也只落日志，不算错。

    与 gui.py 那份同规：`BESTSELLER_NO_DIALOG=1` 只落日志不弹窗（自动化验证用）。
    窗口模式的第二个实例没人看控制台，靠它说话。
    """
    logging.getLogger(__name__).info(text)
    if os.name != "nt" or (os.environ.get("BESTSELLER_NO_DIALOG") or "").strip() == "1":
        return
    _message_box(text, title)


def _message_box(text: str, title: str = WINDOW_TITLE) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(
            None, text, title, _MB_ICONINFORMATION | _MB_SETFOREGROUND | _MB_TOPMOST)
    except OSError as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("弹窗失败（%s）：%s", exc, text)


def open_window(cfg) -> int:
    """开小窗口；关窗后返回最后一次运行的退出码（没跑过是 0）。"""
    api = Api(cfg)
    html = (ROOT / "bestseller_monitor" / "pages" / "ui_exchange.html").read_text(encoding="utf-8")
    window = webview.create_window(WINDOW_TITLE, html=html, js_api=api, width=640,
                                   height=540, min_size=(540, 420))
    window.events.closing += lambda: _note_closing(api)
    webview.start(debug=False)
    return api.exit_code if api.exit_code is not None else console.EXIT_CLEAN


def _note_closing(api) -> None:
    """关窗：运行还在跑只记一笔（不拦、不弹）——重跑能修。"""
    note = api.close_note()
    if note:
        logging.getLogger(__name__).warning(note)


def _refuse_when_running(window: bool) -> int:
    """同一台机器同一时刻至多一次交换台运行：窗口与命令行同规（票 14）。"""
    text = "交换台已经在运行（窗口开着，或另一次运行还没结束）：等它结束再跑。"
    if window:
        _notify(text + "\n\n这次不再开第二个窗口。", title=f"{WINDOW_TITLE}（已经在运行）")
        return console.EXIT_CLEAN          # 退出 0：照 ADR-0008 的约定让启动壳保持安静
    logging.getLogger(__name__).info(text)
    print(text)
    return console.EXIT_NOTHING_DONE


def _open_window_or_fail(cfg) -> int:
    """开窗；起不来（缺 pywebview、页面读不到、WebView2 之类）按「本机没做成事」退出 2。

    折成 2 是有意的：启动壳的弹窗政策是「0/1 静默、2 与启动失败才弹」——子进程侧的
    启动失败只有这样才够得着那条政策（不然双击壳只会什么都不说）。
    """
    if webview is None:
        return _startup_failure(
            f"窗口开不起来：缺 pywebview（{_WEBVIEW_IMPORT_ERROR}）。"
            "请在项目根目录运行 python -m pip install -r requirements.txt。")
    try:
        return open_window(cfg)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("窗口起不来")
        return _startup_failure(f"窗口起不来：{exc}（详见日志）")


def _startup_failure(text: str) -> int:
    """启动失败：写 stderr（启动壳收着这条管道，弹窗里会带出来）+ 落日志。"""
    logging.getLogger(__name__).error(text)
    print(f"\n>>> {text}\n", file=sys.stderr)
    return console.EXIT_NOTHING_DONE


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.window and args.week:
        print("\n>>> --window 与 --week 不能同时用：窗口不接周入口（补历史走脚本）。"
              "\n>>> 要指定周窗口请直接运行：python exchange.py --week 2026-W37\n")
        return console.EXIT_NOTHING_DONE
    try:
        cfg = Config.from_file(args.config)
    except (FileNotFoundError, ValueError) as exc:
        # 窗口模式没人看控制台：写 stderr——启动壳收着这条管道，弹窗里会带出来
        print(f"\n>>> {exc}\n", file=sys.stderr if args.window else sys.stdout)
        return console.EXIT_NOTHING_DONE
    cfg.ensure_dirs()
    _configure_logging(cfg)                # 抢锁被拒这类也要落账（exchange.log 里查得到原因）
    lock = single_instance.acquire(single_instance.EXCHANGE_LOCK)
    if lock is None:
        return _refuse_when_running(args.window)
    try:
        if args.window:
            return _open_window_or_fail(cfg)
        try:
            outcome = console.run_once(cfg, only=args.only, week=args.week, emit=print)
        except PlanError as exc:              # --week 写错这类：点名说清楚，不吐栈
            print(f"\n>>> {exc}\n")
            return console.EXIT_NOTHING_DONE
        if outcome.report_path is not None:   # 检查没过（交换区根不在）时没有报告可指
            print(f"\n报告：{outcome.report_path}")
        print(f"退出码 {outcome.exit_code}：{console.VERDICT[outcome.exit_code]}")
        return outcome.exit_code
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
