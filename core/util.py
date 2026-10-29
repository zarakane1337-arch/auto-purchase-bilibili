"""通用工具：时间处理、事件总线。"""

from __future__ import annotations

import collections
import datetime as dt
import queue
import threading
import time

TZ = dt.timezone(dt.timedelta(hours=8))

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
)


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_datetime(text: str) -> float:
    """把多种时间写法解析成 epoch 秒（无时区信息时按 UTC+8 处理）。"""
    s = str(text or "").strip().replace("Z", "")
    if not s:
        raise ValueError("时间为空")
    if s.replace(".", "", 1).isdigit():
        v = float(s)
        return v / 1000.0 if v > 1e11 else v

    try:
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=TZ)
        return d.timestamp()
    except ValueError:
        pass

    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=TZ).timestamp()
        except ValueError:
            continue

    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = dt.datetime.strptime(s, fmt).time()
            d = dt.datetime.now(TZ).replace(
                hour=t.hour, minute=t.minute, second=t.second, microsecond=0
            )
            return d.timestamp()
        except ValueError:
            continue

    raise ValueError("无法解析时间: %s" % text)


def to_ms(value) -> int:
    """把任意时间戳（秒/毫秒/字符串/None）统一成毫秒。"""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        try:
            return int(parse_datetime(s) * 1000)
        except ValueError:
            return 0
    if isinstance(value, (int, float)):
        n = float(value)
        if n <= 0:
            return 0
        return int(n) if n > 1e11 else int(n * 1000)
    return 0


def fmt_time(ms) -> str:
    ms = int(ms or 0)
    if ms <= 0:
        return "-"
    return dt.datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S")


def human_delta(sec: float) -> str:
    sec = abs(float(sec or 0))
    if sec < 1:
        return "%d ms" % int(sec * 1000)
    if sec < 60:
        return "%.1f s" % sec
    if sec < 3600:
        return "%d 分 %d 秒" % (int(sec // 60), int(sec % 60))
    if sec < 86400:
        return "%d 时 %d 分" % (int(sec // 3600), int(sec % 3600 // 60))
    return "%d 天 %d 时" % (int(sec // 86400), int(sec % 86400 // 3600))


def mask_id(text: str) -> str:
    """身份证/手机号脱敏。"""
    s = str(text or "")
    if len(s) <= 6:
        return s
    return s[:3] + "*" * (len(s) - 6) + s[-3:]


class EventBus:
    """进程内事件总线：任务日志 / 状态推送，供 SSE 消费。"""

    def __init__(self, history: int = 600):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._history: collections.deque = collections.deque(maxlen=history)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=4000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("ts", now_ms())
        with self._lock:
            self._history.append(event)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def recent(self, n: int = 200) -> list:
        with self._lock:
            return list(self._history)[-n:]
