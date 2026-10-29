"""一键登录：拉起本机 Chrome / Edge，让用户在里面正常登录 B 站，
然后通过 DevTools 协议把 Cookie 直接读出来，自动存进配置。

原理：
  1. 用独立的 --user-data-dir 启动浏览器（不碰用户日常配置），
     同时开 --remote-debugging-port=0，端口由浏览器写进 DevToolsActivePort 文件。
  2. 通过 HTTP /json/list 找到登录页那个 target 的 webSocketDebuggerUrl。
  3. 连上 WebSocket，反复调 Storage.getCookies 轮询，
     直到出现 SESSDATA + bili_jct（说明用户已经登录成功）。
  4. 拼成 Cookie 串交给 BiliClient 校验，通过就保存。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request

from .util import now_ms
from .ws import WebSocket, WSError

LOGIN_URL = "https://show.bilibili.com/"

CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
]

# 只保留这些 Cookie 名，够用且不至于把一大堆无关项塞进配置
KEEP = {
    "SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5",
    "sid", "buvid3", "buvid4", "b_nut", "buvid_fp", "_uuid",
    "CURRENT_FNVAL", "CURRENT_QUALITY", "home_feed_column",
}


def find_browser() -> str | None:
    for path in CANDIDATES:
        if path and os.path.isfile(path):
            return path
    for name in ("chrome.exe", "msedge.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


class BrowserLogin:
    """管理「打开浏览器 → 等登录 → 抓 Cookie」这一条链路。"""

    def __init__(self, client, bus, profile_dir: str):
        self.client = client
        self.bus = bus
        self.profile_dir = profile_dir
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = "idle"      # idle | launching | waiting | logged_in | error
        self.message = ""
        self.browser = ""
        self.port = 0
        self.user: dict = {}

    # ---------- 对外 ----------

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "message": self.message,
                "browser": os.path.basename(self.browser) if self.browser else "",
                "port": self.port,
                "user": self.user,
                "running": bool(self._thread and self._thread.is_alive()),
            }

    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            browser = find_browser()
            if not browser:
                raise RuntimeError("本机没找到 Chrome 或 Edge，请手动粘贴 Cookie")
            self.browser = browser
            self.state = "launching"
            self.message = "正在启动浏览器…"
            self.user = {}
            self.port = 0
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="browser-login", daemon=True)
            self._thread.start()
        self._log("info", "已启动浏览器，请在弹出的窗口里登录 B 站（扫码或账号密码都行）")
        return self.status()

    def cancel(self) -> dict:
        self._stop.set()
        self._kill_browser()
        with self._lock:
            self.state = "idle"
            self.message = "已取消"
        return self.status()

    # ---------- 内部 ----------

    def _log(self, level: str, msg: str) -> None:
        if self.bus:
            self.bus.publish({"type": "log", "level": level, "msg": msg, "ts": now_ms()})

    def _kill_browser(self) -> None:
        proc = self._proc
        self._proc = None
        if not proc:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                )
            else:
                proc.terminate()
        except Exception:
            pass

    def _read_port(self, deadline: float) -> int:
        port_file = os.path.join(self.profile_dir, "DevToolsActivePort")
        while time.time() < deadline:
            if self._stop.is_set():
                raise RuntimeError("已取消")
            try:
                with open(port_file, "r", encoding="utf-8") as fh:
                    first = fh.readline().strip()
                if first.isdigit():
                    return int(first)
            except OSError:
                pass
            time.sleep(0.2)
        raise RuntimeError("浏览器没有开放调试端口（可能被安全软件拦住了）")

    def _pick_target(self, port: int, deadline: float) -> str:
        url = "http://127.0.0.1:%d/json/list" % port
        while time.time() < deadline:
            if self._stop.is_set():
                raise RuntimeError("已取消")
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    items = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception:
                time.sleep(0.3)
                continue
            pages = [it for it in items if it.get("type") == "page" and it.get("webSocketDebuggerUrl")]
            for it in pages:
                if "bilibili.com" in (it.get("url") or ""):
                    return it["webSocketDebuggerUrl"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
            time.sleep(0.3)
        raise RuntimeError("找不到可用的浏览器页面")

    def _collect(self, ws: WebSocket) -> dict:
        result = ws.call("Storage.getCookies")
        out = {}
        for ck in result.get("cookies") or []:
            domain = str(ck.get("domain") or "").lstrip(".").lower()
            if domain != "bilibili.com" and not domain.endswith(".bilibili.com"):
                continue
            name = ck.get("name")
            if name in KEEP and ck.get("value"):
                out[name] = ck["value"]
        return out

    def _run(self) -> None:
        try:
            self._launch()
        except Exception as exc:
            with self._lock:
                self.state = "error"
                self.message = str(exc)
            self._log("error", "打开浏览器失败：%s" % exc)
            self._kill_browser()

    def _launch(self) -> None:
        os.makedirs(self.profile_dir, exist_ok=True)
        port_file = os.path.join(self.profile_dir, "DevToolsActivePort")
        try:
            os.remove(port_file)
        except OSError:
            pass

        args = [
            self.browser,
            "--user-data-dir=%s" % self.profile_dir,
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--new-window",
            LOGIN_URL,
        ]
        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )

        port = self._read_port(time.time() + 30)
        with self._lock:
            self.port = port
            self.state = "waiting"
            self.message = "浏览器已打开，等你登录…"
        self._log("info", "浏览器调试端口 %d 就绪，等待登录" % port)

        ws_url = self._pick_target(port, time.time() + 20)
        ws = WebSocket(ws_url, timeout=10)
        try:
            try:
                ws.call("Network.enable", timeout=10)
            except WSError:
                pass

            deadline = time.time() + 600  # 最多等 10 分钟
            last = {}
            while time.time() < deadline:
                if self._stop.is_set():
                    raise RuntimeError("已取消")
                if self._proc and self._proc.poll() is not None:
                    with self._lock:
                        self.state = "error"
                        self.message = "浏览器窗口被关闭了"
                    return
                try:
                    cookies = self._collect(ws)
                except WSError:
                    time.sleep(0.5)
                    continue
                last = cookies or last
                if cookies.get("SESSDATA") and cookies.get("bili_jct"):
                    self._finish(cookies)
                    return
                time.sleep(1.0)

            with self._lock:
                self.state = "error"
                self.message = "等待登录超时，请重新点击"
        finally:
            try:
                ws.close()
            except Exception:
                pass
            self._kill_browser()

    def _finish(self, cookies: dict) -> None:
        header = "; ".join("%s=%s" % (k, v) for k, v in cookies.items())
        with self._lock:
            self.message = "已抓到 Cookie，正在校验账号…"
        old = dict(self.client.cookies() or {})
        try:
            self.client.set_cookie_string(header)
            user = self.client.nav()
        except Exception as exc:
            # 校验失败：把原来能用的登录态还原，别把用户已经登好的账号搞掉
            try:
                if old:
                    self.client.store.set("cookies", old)
                else:
                    self.client.logout()
            except Exception:
                pass
            with self._lock:
                self.state = "error"
                self.message = "抓到 Cookie 但校验失败：%s" % exc
            self._log("error", "浏览器登录失败：%s" % exc)
            return
        with self._lock:
            self.state = "logged_in"
            self.user = user or {}
            self.message = "登录成功：%s" % ((user or {}).get("uname") or "")
        self._log("info", "已通过浏览器自动登录：%s（UID %s）"
                  % ((user or {}).get("uname") or "-", (user or {}).get("mid") or "-"))
