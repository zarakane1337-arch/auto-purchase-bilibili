#!/usr/bin/env python3
"""bilibili 会员购自动抢票 · 本地 Web 服务。

启动：python server.py [--port 8765] [--no-browser]
然后浏览器打开 http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.bili import BiliClient, BiliError, extract_project_id  # noqa: E402
from core.browser_login import BrowserLogin, find_browser  # noqa: E402
from core.engine import Engine, STATUS_TEXT  # noqa: E402
from core.store import Store  # noqa: E402
from core.util import EventBus, fmt_time, mask_id, now_ms  # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
DATA_DIR = os.path.join(ROOT, "data")

BUS = EventBus()
STORE = Store(os.path.join(DATA_DIR, "state.json"))
CLIENT = BiliClient(STORE, BUS)
ENGINE = Engine(CLIENT, BUS)
BROWSER = BrowserLogin(CLIENT, BUS, os.path.join(DATA_DIR, "browser-profile"))

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def log_console(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------
# 业务接口
# --------------------------------------------------------------------------

def api_health(h, m, q):
    user = STORE.get("user") or {}
    h.json(200, {
        "ok": True,
        "version": "1.0.0",
        "logged_in": CLIENT.is_logged_in(),
        "user": user,
        "server_time": now_ms(),
        "server_time_text": fmt_time(now_ms()),
    })


def api_session(h, m, q):
    user = STORE.get("user") or {}
    ck = CLIENT.cookies()
    h.json(200, {
        "ok": True,
        "logged_in": CLIENT.is_logged_in(),
        "user": user,
        "cookie_keys": sorted(ck.keys()),
    })


def api_login_cookie(h, m, q):
    body = h.body_json()
    raw = str(body.get("cookie") or "").strip()
    if not raw:
        raise ValueError("Cookie 不能为空")
    CLIENT.set_cookie_string(raw)
    try:
        user = CLIENT.nav()
    except BiliError as exc:
        CLIENT.logout()
        raise ValueError(str(exc.msg))
    log_console("账号已登录：%s" % user.get("uname"))
    h.json(200, {"ok": True, "user": user})


def api_logout(h, m, q):
    CLIENT.logout()
    h.json(200, {"ok": True})


def api_login_browser(h, m, q):
    h.json(200, {"ok": True, "status": BROWSER.start()})


def api_login_browser_status(h, m, q):
    h.json(200, {
        "ok": True,
        "status": BROWSER.status(),
        "available": bool(find_browser()),
        "logged_in": CLIENT.is_logged_in(),
        "user": STORE.get("user", {}) or {},
    })


def api_login_browser_cancel(h, m, q):
    h.json(200, {"ok": True, "status": BROWSER.cancel()})


def api_parse(h, m, q):
    body = h.body_json()
    raw = str(body.get("url") or "").strip()
    pid = extract_project_id(raw)
    norm = CLIENT.project_normalized(pid)
    if not norm.get("skus"):
        raise ValueError("没有解析到票档，可能是链接不对或项目已下架")
    h.json(200, {"ok": True, "project": norm})


def api_buyers(h, m, q):
    pid = int((q.get("project_id") or ["0"])[0])
    if not pid:
        raise ValueError("缺少 project_id")
    lst = CLIENT.buyer_list(pid)
    for b in lst:
        b.pop("raw", None)
        b["personal_id_masked"] = mask_id(b.get("personal_id"))
    h.json(200, {"ok": True, "list": lst})


def api_ntp(h, m, q):
    try:
        CLIENT.ts.sync(rounds=5)
    except Exception:
        pass
    h.json(200, {"ok": True, "sync": CLIENT.ts.snapshot()})


def api_task_start(h, m, q):
    body = h.body_json()
    cfg = body.get("cfg") if isinstance(body.get("cfg"), dict) else body
    task = ENGINE.start(cfg)
    ENGINE.cleanup()
    log_console("任务启动 %s mode=%s target=%s" % (
        task.id, task.cfg.get("mode"), fmt_time(task.cfg.get("target_ms"))))
    h.json(200, {"ok": True, "task": task.public()})


def api_task_stop(h, m, q):
    body = h.body_json()
    tid = str(body.get("id") or "")
    ok = ENGINE.stop(tid)
    h.json(200, {"ok": ok})


def api_tasks(h, m, q):
    h.json(200, {"ok": True, "tasks": ENGINE.snapshot(), "status_text": STATUS_TEXT})


def api_events(h, m, q):
    h.sse()


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AutoPurchase/1.0"

    def log_message(self, fmt, *args):  # 静音默认日志
        pass

    # ---------- 输出 ----------
    def json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _chunk(self, payload: bytes) -> None:
        self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
        self.wfile.flush()

    def sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = False

        sub = BUS.subscribe()
        try:
            hello = {"type": "hello", "ts": now_ms(), "status_text": STATUS_TEXT}
            self._chunk(("data: " + json.dumps(hello, ensure_ascii=False) + "\n\n").encode("utf-8"))
            for ev in BUS.recent(120):
                self._chunk(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
            while True:
                try:
                    ev = sub.get(timeout=15)
                    self._chunk(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                except queue.Empty:
                    self._chunk(b": ping\n\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(sub)
            self.close_connection = True

    def static(self, rel: str) -> None:
        rel = unquote(rel or "")
        if rel in ("", "/", "index.html"):
            rel = "index.html"
        rel = rel.lstrip("/")
        if rel.startswith("web/"):
            rel = rel[4:]
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            self.json(404, {"ok": False, "error": "文件不存在"})
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = CONTENT_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # ---------- 路由 ----------
    ROUTES = (
        ("GET", re.compile(r"/api/health"), api_health),
        ("GET", re.compile(r"/api/session"), api_session),
        ("POST", re.compile(r"/api/login/cookie"), api_login_cookie),
        ("POST", re.compile(r"/api/login/browser$"), api_login_browser),
        ("GET", re.compile(r"/api/login/browser/status"), api_login_browser_status),
        ("POST", re.compile(r"/api/login/browser/cancel"), api_login_browser_cancel),
        ("POST", re.compile(r"/api/logout"), api_logout),
        ("POST", re.compile(r"/api/parse"), api_parse),
        ("GET", re.compile(r"/api/buyers"), api_buyers),
        ("GET", re.compile(r"/api/ntp"), api_ntp),
        ("POST", re.compile(r"/api/task"), api_task_start),
        ("POST", re.compile(r"/api/task/stop"), api_task_stop),
        ("GET", re.compile(r"/api/tasks"), api_tasks),
        ("GET", re.compile(r"/api/events"), api_events),
    )

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        for m, pat, fn in self.ROUTES:
            if m != method:
                continue
            if pat.fullmatch(path):
                try:
                    fn(self, None, query)
                except BiliError as exc:
                    self.json(200, {"ok": False, "error": exc.msg, "code": exc.code})
                except ValueError as exc:
                    self.json(200, {"ok": False, "error": str(exc)})
                except RuntimeError as exc:
                    self.json(200, {"ok": False, "error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    self.json(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
                return
        if method == "GET":
            self.static(path)
            return
        self.json(404, {"ok": False, "error": "接口不存在"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许两个进程绑同一端口（谁收到连接看运气），
    # 关掉它才能在第二次启动时正确报"端口已被占用"。
    allow_reuse_address = os.name != "nt"


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    url = "http://%s:%d/" % (args.host, args.port)
    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as exc:
        # 端口被占用：大概率是已经开着一个了，直接把页面打开就行
        print("端口 %d 已被占用（%s）。" % (args.port, exc))
        print("如果本工具已经在运行，直接用这个地址：%s" % url)
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        try:
            input("按回车键关闭…")
        except (EOFError, KeyboardInterrupt):
            pass
        return

    print("=" * 56)
    print(" bilibili 会员购 · 自动抢票 WebUI")
    print(" 地址：%s" % url)
    print(" 数据：%s" % os.path.join(DATA_DIR, "state.json"))
    print(" 停止：Ctrl + C")
    print("-" * 56)
    print(" 本程序仅供学习参考使用，不得用于商业用途或实际抢票。")
    print(" 与 bilibili 官方无任何关联。后果由使用者自行承担。")
    print("=" * 56, flush=True)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
