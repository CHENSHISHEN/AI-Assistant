#!/usr/bin/env python3
"""验收用的假 Workbench：只实现 GET /api/status 和 GET /api/entries，状态从 JSON 文件读，
这样可以在不碰真实 Workbench 的情况下"停掉/恢复"一个应用。只监听 127.0.0.1；任何非 GET 请求返回 405 并记录。

用法: mock_workbench.py --port 18400 --state-file /tmp/wb.json [--allow-writes]
默认只读：任何非 GET 请求返回 405。加 --allow-writes 后，POST /api/entries/{id}/{start|stop|restart}
会改写状态文件（stop -> stopped，start/restart -> running）并把 "WRITE <方法> <路径>" 打到 stderr，
用来验收"应用启停审批流"，只在测试里用。
状态文件: {"states": {"demo": "running"}, "names": {"demo": "示例应用"}}
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


def make_handler(state_file: str, allow_writes: bool = False):
    class H(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            st = json.load(open(state_file, encoding="utf-8"))
            if self.path == "/api/status":
                self._json({"states": st["states"], "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            elif self.path == "/api/entries":
                self._json({"entries": [{"id": k, "name": v} for k, v in st.get("names", {}).items()], "registry_error": None})
            else:
                self._json({"detail": "not found"}, 404)

        def _reject(self):
            print(f"WRITE-ATTEMPT {self.command} {self.path}", file=sys.stderr, flush=True)
            self._json({"detail": "mock is read-only"}, 405)

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            if allow_writes and len(parts) == 4 and parts[:2] == ["api", "entries"] and parts[3] in ("start", "stop", "restart"):
                st = json.load(open(state_file, encoding="utf-8"))
                if parts[2] not in st["states"]:
                    return self._json({"detail": "not found"}, 404)
                st["states"][parts[2]] = "stopped" if parts[3] == "stop" else "running"
                json.dump(st, open(state_file, "w", encoding="utf-8"), ensure_ascii=False)
                print(f"WRITE {self.command} {self.path}", file=sys.stderr, flush=True)
                return self._json({"ok": True})
            self._reject()

        do_PUT = do_PATCH = do_DELETE = _reject

        def log_message(self, *a):
            pass
    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18400)
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--allow-writes", action="store_true")
    a = ap.parse_args()
    HTTPServer(("127.0.0.1", a.port), make_handler(a.state_file, a.allow_writes)).serve_forever()
