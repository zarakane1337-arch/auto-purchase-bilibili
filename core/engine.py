"""抢票引擎：定时开抢 / 立刻开抢 / 蹲票。

核心是三件事：
1. 用服务器时间校准本机时间，把误差压到毫秒级；
2. 开抢前把 TLS 连接、实名信息、prepare token 全部预热好；
3. 到点后多线程并发打 createV2，直到拿到订单号。
"""

from __future__ import annotations

import threading
import time
import uuid

from .bili import (
    BiliError,
    build_buyer_info,
    build_deliver_info,
    classify_error,
    order_pay_url,
)
from .util import fmt_time, human_delta, now_ms

MODES = ("timed", "now", "monitor")

STATUS_TEXT = {
    "pending": "排队中",
    "preparing": "准备中",
    "waiting": "等待开抢",
    "firing": "正在抢票",
    "monitoring": "蹲票中",
    "success": "抢票成功",
    "failed": "失败",
    "stopped": "已停止",
}


class Task:
    def __init__(self, cfg: dict):
        self.id = uuid.uuid4().hex[:12]
        self.cfg = cfg
        self.status = "pending"
        self.detail = ""
        self.created_at = now_ms()
        self.fire_at = int(cfg.get("target_ms") or 0)
        self.attempts = 0
        self.last_error = ""
        self.order_id = None
        self.pay_url = ""
        self.finished_at = 0
        self.stop_flag = threading.Event()
        self.thread: threading.Thread | None = None

    def public(self) -> dict:
        return {
            "id": self.id,
            "mode": self.cfg.get("mode"),
            "status": self.status,
            "status_text": STATUS_TEXT.get(self.status, self.status),
            "detail": self.detail,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "order_id": self.order_id,
            "pay_url": self.pay_url,
            "fire_at": self.fire_at,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "project_id": self.cfg.get("project_id"),
            "screen_id": self.cfg.get("screen_id"),
            "sku_id": self.cfg.get("sku_id"),
            "count": self.cfg.get("count"),
            "buyer_name": (self.cfg.get("buyer") or {}).get("name", ""),
        }


class Engine:
    def __init__(self, client, bus):
        self.client = client
        self.bus = bus
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    # ---------- 事件 ----------
    def _emit(self, task: Task, msg: str, level: str = "info") -> None:
        self.bus.publish({
            "type": "log",
            "task": task.id,
            "level": level,
            "msg": msg,
        })

    def _set_status(self, task: Task, status: str, detail: str = "") -> None:
        task.status = status
        if detail:
            task.detail = detail
        self.bus.publish({
            "type": "task",
            "task": task.id,
            "status": status,
            "status_text": STATUS_TEXT.get(status, status),
            "detail": task.detail,
            "attempts": task.attempts,
            "order_id": task.order_id,
            "pay_url": task.pay_url,
        })

    # ---------- 生命周期 ----------
    def start(self, cfg: dict) -> Task:
        cfg = dict(cfg or {})
        mode = str(cfg.get("mode") or "timed")
        if mode not in MODES:
            raise ValueError("未知模式：%s" % mode)
        cfg["mode"] = mode
        if not cfg.get("project_id"):
            raise ValueError("缺少 project_id，请先解析链接")
        if not cfg.get("screen_id"):
            raise ValueError("请选择场次")
        if not cfg.get("sku_id"):
            raise ValueError("请选择票档")
        cfg["count"] = max(1, int(cfg.get("count") or 1))
        cfg.setdefault("concurrency", 2)
        cfg["concurrency"] = max(1, min(5, int(cfg["concurrency"])))
        cfg.setdefault("retry_ms", 120)
        cfg.setdefault("lead_ms", 1500)
        cfg.setdefault("monitor_ms", 800)
        cfg.setdefault("max_seconds", {"timed": 300, "now": 120, "monitor": 1800}[mode])
        cfg.setdefault("fire_seconds", {"timed": 300, "now": 120, "monitor": 60}[mode])
        cfg.setdefault("single_order", True)

        if mode == "timed":
            if not cfg.get("target_ms"):
                raise ValueError("定时开抢必须选择开抢时间")
            if int(cfg["target_ms"]) < now_ms() - 60_000:
                raise ValueError("开抢时间已经过去了")
        else:
            cfg["target_ms"] = now_ms()

        with self._lock:
            for t in self._tasks.values():
                if t.status in ("pending", "preparing", "waiting", "firing", "monitoring"):
                    raise RuntimeError("已有任务在运行中，请先停止")
            task = Task(cfg)
            self._tasks[task.id] = task

        task.thread = threading.Thread(target=self._run, args=(task,), daemon=True, name="grab-%s" % task.id)
        task.thread.start()
        return task

    def stop(self, tid: str) -> bool:
        with self._lock:
            task = self._tasks.get(tid)
        if not task:
            return False
        task.stop_flag.set()
        return True

    def get(self, tid: str):
        with self._lock:
            return self._tasks.get(tid)

    def snapshot(self) -> list:
        with self._lock:
            tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [t.public() for t in tasks[:30]]

    def cleanup(self, keep: int = 20) -> None:
        with self._lock:
            done = [t for t in self._tasks.values()
                    if t.status in ("success", "failed", "stopped")]
            done.sort(key=lambda t: t.created_at, reverse=True)
            for t in done[keep:]:
                self._tasks.pop(t.id, None)

    # ---------- 主流程 ----------
    def _run(self, task: Task) -> None:
        cfg = task.cfg
        cli = self.client
        log = lambda m, lv="info": self._emit(task, m, lv)

        try:
            self._set_status(task, "preparing", "校准时间与账号状态")
            if not cli.is_logged_in():
                raise RuntimeError("未登录：请先粘贴 Cookie 并验证账号")

            log("正在与 bilibili 服务器对时…")
            off = cli.ts.sync(rounds=6)
            log("对时完成：本机快慢 %+.0f ms，RTT %.0f ms" % (off * 1000, cli.ts.rtt * 1000))

            log("拉取项目信息…")
            norm = cli.project_normalized(cfg["project_id"])
            sku = cli.find_sku(norm, cfg["sku_id"])
            if sku is None:
                raise RuntimeError("目标票档不存在，请重新解析链接后选择")
            price = int(sku.get("price") or 0)
            cfg["price"] = price
            log("目标：%s｜场次 %s｜票档 %s｜单价 %.2f 元｜库存 %s" % (
                norm.get("name") or cfg["project_id"],
                cfg.get("screen_id"),
                sku.get("desc") or cfg.get("sku_id"),
                price / 100.0,
                "-" if sku.get("stock") is None else sku.get("stock"),
            ))

            buyer = cli.resolve_buyer(cfg["project_id"], cfg.get("buyer") or {})
            if not buyer.get("personal_id") and not buyer.get("id"):
                raise RuntimeError("实名信息不完整：需要身份证号（或从账号同步观演人）")
            if not buyer.get("id"):
                log("警告：该实名信息不在账号观演人列表中，下单可能被拒绝", "warn")
            cfg["buyer"] = buyer
            log("观演人：%s %s" % (buyer.get("name") or "-", buyer.get("personal_id", "")[-4:].rjust(4, "*")))

            ctx = {
                "buyer_info": build_buyer_info(buyer),
                "deliver_info": build_deliver_info(cfg, buyer),
                "price": price,
                "sku": sku,
                "project": norm,
            }

            mode = cfg["mode"]
            if mode == "timed":
                self._run_timed(task, ctx, log)
            elif mode == "now":
                self._run_now(task, ctx, log)
            else:
                self._run_monitor(task, ctx, log)

        except BiliError as exc:
            self._finish(task, "failed", "接口错误：%s" % exc.msg, log)
        except RuntimeError as exc:
            self._finish(task, "failed", str(exc), log)
        except Exception as exc:  # noqa: BLE001
            self._finish(task, "failed", "异常：%s: %s" % (type(exc).__name__, exc), log)

    def _finish(self, task: Task, status: str, detail: str, log) -> None:
        if status == "failed" and task.stop_flag.is_set():
            status = "stopped"
        task.finished_at = now_ms()
        self._set_status(task, status, detail)
        log(detail, "error" if status == "failed" else "info")

    # ---------- 三种模式 ----------
    def _run_timed(self, task: Task, ctx: dict, log) -> None:
        cfg = task.cfg
        target_ms = int(cfg["target_ms"])
        target_server = target_ms / 1000.0
        lead = max(0, int(cfg.get("lead_ms", 1500))) / 1000.0
        fire_local = self.client.ts.to_local(target_server)
        remain = fire_local - time.time()

        log("开抢时间 %s（服务器时间），距今 %s" % (fmt_time(target_ms), human_delta(remain)))
        self._set_status(task, "waiting", "等待 %s 开抢" % fmt_time(target_ms))

        # 提前预热
        prewarm_at = fire_local - lead
        if time.time() < prewarm_at:
            if not self._sleep_until(task, prewarm_at, log):
                return self._finish(task, "stopped", "已手动停止", log)
        log("预热连接（%d 条）…" % (int(cfg["concurrency"]) * 3))
        try:
            n = self.client.show.prewarm(int(cfg["concurrency"]) * 3, path="/platform/detail.html?id=%s" % cfg["project_id"])
            log("预热完成 %d/%d" % (n, int(cfg["concurrency"]) * 3))
        except Exception:
            log("预热失败，继续（不影响开抢）", "warn")

        if not self._sleep_until(task, fire_local, log):
            return self._finish(task, "stopped", "已手动停止", log)

        self._set_status(task, "firing", "开抢")
        log("时间到，开始下单")
        self._fire(task, ctx, log)

    def _run_now(self, task: Task, ctx: dict, log) -> None:
        cfg = task.cfg
        log("立刻开抢模式，直接下单")
        try:
            self.client.show.prewarm(int(cfg["concurrency"]) * 2, path="/platform/detail.html?id=%s" % cfg["project_id"])
        except Exception:
            pass
        self._set_status(task, "firing", "立刻开抢")
        self._fire(task, ctx, log)

    def _run_monitor(self, task: Task, ctx: dict, log) -> None:
        cfg = task.cfg
        cli = self.client
        pid = cfg["project_id"]
        sku_id = cfg["sku_id"]
        interval = max(200, int(cfg.get("monitor_ms", 800))) / 1000.0
        deadline = time.time() + int(cfg.get("max_seconds", 1800))
        self._set_status(task, "monitoring", "蹲票中，检测库存…")
        log("蹲票模式启动，轮询间隔 %.1fs" % interval)

        while not task.stop_flag.is_set() and time.time() < deadline:
            try:
                stock, sku = cli.stock_of(pid, sku_id)
            except Exception as exc:
                log("查询库存失败：%s" % exc, "warn")
                time.sleep(interval)
                continue

            if sku is None:
                log("票档已不存在，结束蹲票", "error")
                return self._finish(task, "failed", "票档不存在", log)

            if stock is not None and stock <= 0:
                task.detail = "库存 0，等待回流"
                time.sleep(interval)
                continue

            log("检测到可购（库存 %s），开始下单" % ("未知" if stock is None else stock))
            self._set_status(task, "firing", "蹲到票，下单中")
            if self._fire(task, ctx, log, finish_on_fail=False):
                return
            if task.stop_flag.is_set() or task.status in ("failed", "stopped", "success"):
                # _fire 已经收尾（致命错误 / 被停止），不要把它改回运行态。
                return
            log("本轮未成功，回到蹲票", "warn")
            self._set_status(task, "monitoring", "继续蹲票")
            time.sleep(interval)

        if task.stop_flag.is_set():
            return self._finish(task, "stopped", "已手动停止", log)
        return self._finish(task, "failed", "蹲票超时，未抢到", log)

    # ---------- 时间等待 ----------
    def _sleep_until(self, task: Task, target_local: float, log) -> bool:
        """精确睡到 target_local（本机时钟），返回 False 表示被停止。"""
        cli = self.client
        last_sync = time.time()
        while not task.stop_flag.is_set():
            remain = target_local - time.time()
            if remain <= 0:
                return True
            if remain > 20:
                time.sleep(min(remain - 5, 8))
            elif remain > 1.0:
                time.sleep(remain - 0.6)
            elif remain > 0.12:
                time.sleep(0.01)
            else:
                # 最后一程自旋，避免 sleep 精度不足
                while not task.stop_flag.is_set() and time.time() < target_local:
                    pass
                return not task.stop_flag.is_set()
            if time.time() - last_sync > 45:
                try:
                    cli.ts.sync(rounds=3)
                except Exception:
                    pass
                last_sync = time.time()
        return False

    # ---------- 下单 ----------
    def _fire(self, task: Task, ctx: dict, log, finish_on_fail: bool = True) -> bool:
        cfg = task.cfg
        cli = self.client
        pid = cfg["project_id"]
        sid = cfg["screen_id"]
        sku_id = cfg["sku_id"]
        count = cfg["count"]
        price = ctx["price"]
        buyer_info = ctx["buyer_info"]
        deliver_info = ctx["deliver_info"]
        extra = cfg.get("extra_fields") or {}
        retry_s = max(0, int(cfg.get("retry_ms", 120))) / 1000.0
        # 单轮开抢的窗口，与整场任务的 max_seconds 分开：
        # 蹲票模式一轮抢不到要能回到轮询，不能被单轮吃满整个蹲票窗口。
        fire_seconds = max(1, int(cfg.get("fire_seconds", 60)))
        deadline = time.time() + fire_seconds
        concurrency = int(cfg.get("concurrency", 2))
        single = bool(cfg.get("single_order", True))
        winner = threading.Event()
        fatal = threading.Event()
        counters = {"ok": 0, "fail": 0}
        lock = threading.Lock()
        order_lock = threading.Lock()

        def worker(idx: int) -> None:
            while not task.stop_flag.is_set() and not winner.is_set() and not fatal.is_set():
                if time.time() > deadline:
                    return
                try:
                    prep = cli.prepare(pid, sid, sku_id, count, extra=extra)
                    token = str(prep.get("token") or prep.get("prepare_token") or "")
                except BiliError as exc:
                    kind = classify_error(exc.code, exc.msg)
                    with lock:
                        task.attempts += 1
                    if kind in ("auth", "buyer", "limit", "closed"):
                        task.last_error = exc.msg
                        log("prepare 终止：%s" % exc.msg, "error")
                        fatal.set()
                        return
                    time.sleep(retry_s)
                    continue
                except Exception as exc:
                    with lock:
                        task.attempts += 1
                        counters["fail"] += 1
                        nf = counters["fail"]
                    task.last_error = "prepare 异常：%s" % exc
                    if nf <= 3:
                        log("第 %d 次 prepare 异常：%s" % (nf, exc), "warn")
                    time.sleep(retry_s)
                    continue

                # createV2 是唯一会真的产生订单的一步。并发请求里只要有一单成功，
                # 其余在途请求必须立刻放弃，否则限购>1 时会下出两张票、扣两次款。
                if single:
                    order_lock.acquire()
                cerr = None
                res = None
                try:
                    blocked = winner.is_set() or fatal.is_set()
                    if not blocked:
                        try:
                            res = cli.create_order(
                                pid, sid, sku_id, count, price, buyer_info, deliver_info, token,
                                extra=extra,
                            )
                        except BiliError as exc:
                            cerr = exc
                finally:
                    if single:
                        order_lock.release()

                if blocked:
                    return
                if cerr is not None:
                    with lock:
                        task.attempts += 1
                    task.last_error = cerr.msg
                    if classify_error(cerr.code, cerr.msg) in ("auth", "buyer", "limit", "closed"):
                        log("下单终止：%s" % cerr.msg, "error")
                        fatal.set()
                        return
                    time.sleep(retry_s)
                    continue

                with lock:
                    task.attempts += 1
                    n = task.attempts
                code = res.get("errno", res.get("code"))
                msg = str(res.get("msg") or res.get("message") or "")

                if code in (0, "0"):
                    data = res.get("data") or {}
                    oid = data.get("order_id") or data.get("orderId") or data.get("order_no") or data.get("orderid")
                    if not oid:
                        # errno=0 但拿不到订单号：多半是风控静默拦截，不能当成功。
                        task.last_error = "返回成功但无订单号（疑似风控拦截）"
                        with lock:
                            counters["fail"] += 1
                            nf = counters["fail"]
                        if nf <= 3 or nf % 20 == 0:
                            log("第 %d 次异常返回：%s" % (nf, str(res)[:200]), "warn")
                        time.sleep(retry_s)
                        continue
                    task.order_id = oid
                    task.pay_url = order_pay_url(oid)
                    winner.set()
                    log("下单成功！订单号 %s（第 %d 次尝试）" % (oid, n), "success")
                    try:
                        pay_res = cli.pay(pid, oid, data.get("pay_money") or price * count)
                        pd = pay_res.get("data") or {}
                        if isinstance(pd, dict):
                            url = pd.get("pay_url") or pd.get("code_url") or pd.get("url")
                            if url:
                                task.pay_url = url
                    except Exception:
                        pass
                    return

                task.last_error = msg or ("错误码 %s" % code)
                kind = classify_error(code, msg)
                with lock:
                    counters["fail"] += 1
                if kind in ("auth", "buyer", "limit", "closed"):
                    log("下单终止：%s（%s）" % (task.last_error, code), "error")
                    fatal.set()
                    return
                if counters["fail"] <= 3 or counters["fail"] % 20 == 0:
                    log("第 %d 次未成功：%s" % (counters["fail"], task.last_error), "warn")
                time.sleep(retry_s)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if winner.is_set():
            self._finish(task, "success", "已生成订单，请尽快支付", log)
            return True
        if task.stop_flag.is_set():
            self._finish(task, "stopped", "已手动停止", log)
            return False
        if fatal.is_set():
            self._finish(task, "failed", task.last_error or "下单被拒绝", log)
            return False
        if not finish_on_fail:
            # 蹲票模式下这一轮没抢到不算任务结束，交给调用方决定继续轮询还是收尾。
            return False
        self._finish(task, "failed", "超时未抢到：%s" % (task.last_error or "无库存/接口繁忙"), log)
        return False
