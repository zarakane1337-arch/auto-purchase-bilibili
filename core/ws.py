"""极简 WebSocket 客户端（RFC 6455 子集）。

只用标准库，目的是跟本机 Chrome / Edge 的 DevTools 协议（CDP）对话，
从浏览器里直接把 Cookie 抓出来，免掉用户手工复制。

只实现本项目需要的部分：
  * 明文 ws:// 与 TLS wss://
  * 客户端帧掩码、文本帧、分片重组、ping/pong 自动应答
  * call(method, params) 把 CDP 的请求/响应配上号
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlparse


class WSError(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 10.0):
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        self.url = url
        self._buf = b""
        self._lock = threading.Lock()
        self._id = 0

        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        if u.scheme == "wss":
            ctx = ssl._create_unverified_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        self.sock = sock

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (path, host, port, key)
        )
        sock.sendall(req.encode())

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise WSError("WebSocket 握手时连接被关闭")
            head += chunk
            if len(head) > 65536:
                raise WSError("WebSocket 握手响应异常")
        raw_head, _, rest = head.partition(b"\r\n\r\n")
        status = raw_head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status:
            raise WSError("WebSocket 握手失败：%s" % status)
        self._buf = rest

    # ---------- 帧收发 ----------

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WSError("WebSocket 连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self):
        b1, b2 = self._recv_exact(2)
        fin = bool(b1 & 0x80)
        op = b1 & 0x0F
        masked = bool(b2 & 0x80)
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(ln) if ln else b""
        if mask:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return fin, op, payload

    def _send_frame(self, op: int, data: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(data[i] ^ mask[i % 4] for i in range(len(data)))
        head = bytearray([0x80 | op])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        head += mask
        self.sock.sendall(bytes(head) + masked)

    def send_text(self, text: str) -> None:
        with self._lock:
            self._send_frame(0x1, text.encode("utf-8"))

    def recv_message(self, timeout: float | None = None) -> str:
        if timeout is not None:
            self.sock.settimeout(timeout)
        frag = b""
        while True:
            fin, op, payload = self._read_frame()
            if op == 0x9:  # ping
                with self._lock:
                    self._send_frame(0xA, payload)
                continue
            if op == 0xA:  # pong
                continue
            if op == 0x8:
                raise WSError("WebSocket 被对端关闭")
            frag += payload
            if fin:
                return frag.decode("utf-8", "replace")

    # ---------- CDP ----------

    def call(self, method: str, params: dict | None = None, timeout: float = 15.0) -> dict:
        with self._lock:
            self._id += 1
            mid = self._id
        self.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise WSError("DevTools 调用超时：%s" % method)
            msg = self.recv_message(timeout=max(0.05, remain))
            try:
                obj = json.loads(msg)
            except Exception:
                continue
            if obj.get("id") != mid:
                continue  # 事件推送，跳过
            if "error" in obj:
                err = obj["error"] or {}
                raise WSError("%s 失败：%s" % (method, err.get("message") or err))
            return obj.get("result") or {}

    def close(self) -> None:
        try:
            with self._lock:
                self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
