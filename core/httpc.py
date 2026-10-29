"""极简 HTTPS 客户端：连接复用、连接预热、服务器时间校准。

抢票场景下请求延迟就是命门，所以这里不用 requests 的默认行为，而是自己
维护 keep-alive 连接池，并在开抢前把 TLS 握手、DNS 解析全部提前做掉。
"""

from __future__ import annotations

import email.utils
import http.client
import json
import socket
import ssl
import statistics
import threading
import time
from urllib.parse import urlencode

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SSL_CTX = ssl.create_default_context()


class Resp:
    __slots__ = ("status", "headers", "raw_headers", "body", "elapsed")

    def __init__(self, status, headers, raw_headers, body, elapsed):
        self.status = status
        self.headers = headers
        self.raw_headers = raw_headers
        self.body = body
        self.elapsed = elapsed

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8", "replace"))
        except ValueError:
            return None

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def cookies(self) -> dict:
        out = {}
        for k, v in self.raw_headers:
            if k.lower() != "set-cookie":
                continue
            first = v.split(";", 1)[0]
            if "=" in first:
                name, val = first.split("=", 1)
                out[name.strip()] = val.strip()
        return out


class HostClient:
    """单个 host 的 keep-alive 连接池。"""

    def __init__(self, host: str, port: int = 443, pool_size: int = 16, timeout: float = 12.0):
        self.host = host
        self.port = port
        self.pool_size = pool_size
        self.timeout = timeout
        self._pool: list = []
        self._lock = threading.Lock()

    # ---------- 连接管理 ----------
    def _connect(self):
        if self.port == 443:
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout, context=SSL_CTX
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _acquire(self):
        with self._lock:
            if self._pool:
                return self._pool.pop()
        return self._connect()

    def _release(self, conn) -> None:
        with self._lock:
            if len(self._pool) < self.pool_size:
                self._pool.append(conn)
                return
        try:
            conn.close()
        except Exception:
            pass

    @staticmethod
    def _discard(conn) -> None:
        try:
            conn.close()
        except Exception:
            pass

    def close_all(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, []
        for c in pool:
            self._discard(c)

    # ---------- 请求 ----------
    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body=None,
        headers: dict | None = None,
        timeout: float | None = None,
    ) -> Resp:
        if params:
            sep = "&" if "?" in path else "?"
            path = path + sep + urlencode(params)
        hdrs = {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
        }
        if headers:
            hdrs.update(headers)
        if body is not None:
            if isinstance(body, str):
                body = body.encode("utf-8")  # http.client 默认按 latin-1 编码 str，中文会直接报错
            if "Content-Type" not in hdrs:
                hdrs["Content-Type"] = "application/json; charset=utf-8"

        conn = self._acquire()
        if timeout:
            try:
                conn.timeout = timeout
            except Exception:
                pass
        t0 = time.time()
        try:
            conn.request(method, path, body=body, headers=hdrs)
            raw = conn.getresponse()
            data = raw.read()
            elapsed = time.time() - t0
            h = {}
            rawh = []
            for k, v in raw.getheaders():
                rawh.append((k, v))
                h[k.lower()] = v
            if raw.will_close:
                self._discard(conn)
            else:
                self._release(conn)
            return Resp(raw.status, h, rawh, data, elapsed)
        except Exception:
            self._discard(conn)
            raise

    def get(self, path, params=None, headers=None, timeout=None) -> Resp:
        return self.request("GET", path, params=params, headers=headers, timeout=timeout)

    def post(self, path, body=None, params=None, headers=None, timeout=None) -> Resp:
        payload = None
        if body is not None:
            payload = body if isinstance(body, (str, bytes)) else json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            )
        return self.request("POST", path, params=params, body=payload, headers=headers, timeout=timeout)

    def prewarm(self, count: int = 4, path: str = "/") -> int:
        """提前建立 TLS 连接，减少开抢首包耗时。返回成功预热条数。"""
        ok = 0
        for _ in range(max(1, count)):
            conn = self._connect()
            try:
                conn.request("GET", path, headers={"User-Agent": DEFAULT_UA, "Connection": "keep-alive"})
                raw = conn.getresponse()
                raw.read()
                if raw.will_close:
                    self._discard(conn)
                else:
                    self._release(conn)
                    ok += 1
            except Exception:
                self._discard(conn)
        return ok


class TimeSync:
    """基于 HTTP Date 头校准本机与服务器的时间差。

    Date 头只有秒级精度，所以额外做"秒边界探测"：连续采样时记录 Date 值
    跳变那一刻的本机时间，跳变点即服务器整秒边界，精度可以压到 RTT/2。
    """

    def __init__(self, client: HostClient, probe_path: str = "/api/ticket/project/getV2?version=134&id=1&project_id=1"):
        self.client = client
        self.probe_path = probe_path
        self.offset = 0.0          # server_epoch - local_epoch
        self.rtt = 0.0
        self.synced_at = 0.0
        self.rounds = 0
        self._lock = threading.Lock()

    # ---------- 换算 ----------
    def server_now(self) -> float:
        return time.time() + self.offset

    def to_local(self, server_epoch: float) -> float:
        return server_epoch - self.offset

    def to_server(self, local_epoch: float) -> float:
        return local_epoch + self.offset

    def age(self) -> float:
        return time.time() - self.synced_at if self.synced_at else 1e9

    # ---------- 校准 ----------
    def sync(self, rounds: int = 6) -> float:
        samples = []
        prev = None
        boundaries = []
        for _ in range(max(2, rounds)):
            t0 = time.time()
            try:
                resp = self.client.get(self.probe_path, timeout=5)
            except Exception:
                continue
            t1 = time.time()
            date_hdr = resp.headers.get("date")
            if not date_hdr:
                continue
            try:
                srv = email.utils.parsedate_to_datetime(date_hdr).timestamp()
            except Exception:
                continue
            mid = (t0 + t1) / 2.0
            rtt = t1 - t0
            samples.append((srv, mid, rtt))
            if prev is not None:
                p_srv, p_mid, p_rtt = prev
                if srv - p_srv == 1 and (mid - p_mid) < 0.95 and max(rtt, p_rtt) < 1.2:
                    # 服务器整秒边界落在两次采样之间
                    boundaries.append(srv - (p_mid + mid) / 2.0)
            prev = (srv, mid, rtt)

        if not samples:
            return self.offset

        offsets = [s - m for s, m, _ in samples]
        rtts = [r for _, _, r in samples]
        if boundaries:
            offset = statistics.median(boundaries)
        else:
            offset = statistics.median(offsets)

        with self._lock:
            self.offset = float(offset)
            self.rtt = float(statistics.median(rtts))
            self.synced_at = time.time()
            self.rounds = len(samples)
        return self.offset

    def snapshot(self) -> dict:
        return {
            "offset_ms": round(self.offset * 1000, 1),
            "rtt_ms": round(self.rtt * 1000, 1),
            "samples": self.rounds,
            "age_s": round(self.age(), 1) if self.synced_at else None,
            "server_time": int(self.server_now() * 1000),
        }
