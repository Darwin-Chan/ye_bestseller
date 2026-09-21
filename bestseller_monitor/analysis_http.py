"""仅监听本机的分析页面与 JSON 接口。"""
from __future__ import annotations
import json
import logging
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PAGE = Path(__file__).resolve().parent.parent / "docs" / "bestseller-analysis.html"
SAVE_ACTIONS = ('save', 'save_and_view', 'discard')
MAX_REQUEST = 1024 * 1024  # 分组与保存操作的正文上限
# 导出报告内嵌图片资产，正文可达数百 MB；页面在 POST 之前先做体积预检。
MAX_REPORT = 256 * 1024 * 1024
log = logging.getLogger(__name__)


def create_server(service, port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            log.debug(format, *args)

        def reply(self, value, status=200, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def dispatch(self):
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.reply({"error": "仅允许本机页面访问"}, 403)
                return
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            try:
                if self.command == "GET":
                    if route.path == "/":
                        return self.reply(PAGE.read_bytes(), content_type="text/html; charset=utf-8")
                    if route.path == "/api/settings":
                        return self.reply(service.settings())
                    if route.path == "/api/source":
                        return self.reply(service.source(query['offer'][0]))
                    if route.path == "/api/sources":
                        return self.reply(service.export_sources(query['analysis'][0]))
                    if route.path == "/api/coverage":
                        return self.reply(service.coverage(query["date"][0]))
                    if route.path == "/api/draft":
                        try:
                            return self.reply(service.draft())
                        except (sqlite3.Error, OSError):
                            log.exception("读取分析草稿失败")
                            return self.reply({"error": "读取分析草稿失败，请检查分析配置和数据库后重试"}, 503)
                    if route.path == "/api/analysis":
                        return self.reply(service.get(query["id"][0]))
                elif self.command == "POST" and route.path in ("/api/analysis", "/api/report"):
                    expected = f"http://127.0.0.1:{self.server.server_port}"
                    if self.headers.get("Origin") != expected:
                        return self.reply({"error": "请求来源无效"}, 403)
                    limit = MAX_REPORT if route.path == "/api/report" else MAX_REQUEST
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= limit:
                        raise ValueError("请求大小无效")
                    data = json.loads(self.rfile.read(length))
                    if route.path == "/api/report":
                        return self._export(service, data)
                    if data.get('action') in SAVE_ACTIONS:
                        return self._save_action(service, data)
                    if data.get('action') == 'confirm_groups':
                        return self.reply(service.confirm_groups(data['id'], data['groups']))
                    if data.get('action') == 'withdraw':
                        return self.reply(service.withdraw(data['id'], data['group']))
                    if data.get('action') == 'retry_matching':
                        return self.reply(service.retry_matching(data['id']))
                    if data.get('action') in ('move', 'remove'):
                        return self.reply(service.edit_group(data['id'], data['action'], data['group'],
                                                             data['member'], data.get('target')))
                    if data.get('action'):
                        raise ValueError('不支持的分组操作')
                    if "group" in data:
                        return self.reply(service.confirm(data["id"], data["group"]))
                    return self.reply(service.start(data["start"], data["end"], data.get("acknowledged") is True))
                self.reply({"error": "未找到该页面或操作"}, 404)
            except (ValueError, KeyError, TypeError) as exc:
                self.reply({"error": str(exc)}, 400)
            except ConnectionError:
                # Closing a page can cancel its outstanding source-link requests.
                return
            except (sqlite3.Error, OSError):
                log.exception("分析读取失败")
                self.reply({"error": "读取库存数据失败，请检查分析配置和数据库后重试"}, 503)

        def _save_action(self, service, data):
            # 保存失败要有专属文案：页面据此保持「未保存」并允许重试。
            try:
                if data['action'] == 'save':
                    return self.reply(service.save_draft(data['id']))
                if data['action'] == 'save_and_view':
                    return self.reply(service.save_and_view(data['id']))
                return self.reply(service.discard(data['id']))
            except (sqlite3.Error, OSError):
                log.exception("分析草稿操作失败")
                message = "恢复最近保存版本失败，请重试" if data['action'] == 'discard' else "保存分析草稿失败，请重试"
                return self.reply({"error": message}, 503)

        def _export(self, service, data):
            # 写盘失败要有专属文案：页面据此显示真实错误并允许重试。
            try:
                return self.reply(service.export(data['id'], data.get('html')))
            except OSError as exc:
                log.exception("导出报告失败")
                return self.reply({"error": f"导出报告失败（{exc}），请检查 output 目录后重试"}, 503)

        do_GET = dispatch
        do_POST = dispatch

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
