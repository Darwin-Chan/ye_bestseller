"""独立分析入口；不启动或控制库存采集。"""
import argparse
import threading
from pathlib import Path
from bestseller_monitor.analysis import AnalysisConfig, AnalysisService
from bestseller_monitor.analysis_http import create_server


def main():
    parser = argparse.ArgumentParser(description="独立畅销品分析")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent / "config" / "analysis.toml")
    parser.add_argument("--serve", action="store_true", help="仅启动本地 HTTP 服务，不打开桌面窗口")
    args = parser.parse_args()
    server = create_server(AnalysisService(AnalysisConfig.from_file(args.config)))
    url = f"http://127.0.0.1:{server.server_port}"
    if args.serve:
        print(url, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    else:
        import webview
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            webview.create_window("畅销品分析", url, width=1280, height=900)
            webview.start()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    main()
