"""bilibili 会员购（show.bilibili.com）接口封装。

覆盖链路：
    链接 → project_id → 项目详情（场次 / 票档 / 库存 / 开售时间）
         → 实名观演人 → prepare（拿 token）→ createV2（下单）→ pay（取支付参数）

接口字段以线上为准，解析层对字段做了容错（多种别名都能吃），
所以 B 站小改字段时通常不用改代码。
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from .httpc import DEFAULT_UA, HostClient, TimeSync
from .util import now_ms, to_ms

SHOW_HOST = "show.bilibili.com"
PASSPORT_HOST = "passport.bilibili.com"
API_HOST = "api.bilibili.com"

REFERER_HOME = "https://show.bilibili.com/"
REFERER_DETAIL = "https://show.bilibili.com/platform/detail.html?id=%s"


class BiliError(Exception):
    def __init__(self, code, msg, raw=None):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.raw = raw


# --------------------------------------------------------------------------
# 链接 / 数据解析
# --------------------------------------------------------------------------

def extract_project_id(text: str) -> int:
    """从会员购链接或纯数字里提取 project_id。"""
    s = str(text or "").strip()
    if not s:
        raise ValueError("请输入会员购商品链接或项目 ID")
    if s.isdigit():
        return int(s)

    patterns = (
        r"[?&](?:id|project_id|projectId|projectid)=(\d+)",
        r"/detail/(\d+)",
        r"/item/(\d+)",
        r"/platform/detail\.html\?id=(\d+)",
        r"/mall/(\d+)",
    )
    for pat in patterns:
        m = re.search(pat, s)
        if m:
            return int(m.group(1))
    m = re.search(r"(\d{5,})", s)
    if m:
        return int(m.group(1))
    raise ValueError("未能从链接中识别项目 ID，请检查链接是否完整")


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _id_match(plain: str, masked: str) -> bool:
    """接口返回的身份证是打码的（441************010）。

    手填明文只能和打码值按位对齐：长度必须一致，打码位（*）放行，其余位必须相同。
    这样既不会把明文身份证误判给别的观演人，也能在同一个人的前提下命中。
    """
    plain = (plain or "").strip().upper()
    masked = (masked or "").strip().upper()
    if not plain or not masked:
        return False
    if plain == masked:
        return True
    if len(plain) != len(masked):
        return False
    return all(m == "*" or m == p for p, m in zip(plain, masked))


def money(cents) -> str:
    """分 → ¥30.00。"""
    c = _int(cents)
    return "¥%d.%02d" % (c // 100, c % 100)


def normalize_sku(raw: dict, screen_id=None, screen_name: str = "") -> dict:
    """票档。线上字段：id / price(分) / desc / num(余票) / saleStart(秒) / static_limit.num(限购)。"""
    sl = raw.get("static_limit")
    limit = _int(_first(sl, "num", "limit_option", default=0)) if isinstance(sl, dict) else 0
    stock_raw = _first(raw, "num", "stock", "inventory", "remain", "sale_num", "left", default=None)
    price = _int(_first(raw, "price", "sale_price", "origin_price", default=0))
    flag = raw.get("sale_flag")
    flag_text = str(_first(flag, "display_name", default="")) if isinstance(flag, dict) else ""
    return {
        "id": _int(_first(raw, "id", "sku_id", "skuId")),
        "screen_id": _int(_first(raw, "screen_id", "screenId", default=screen_id)),
        "screen_name": str(_first(raw, "screen_name", default=screen_name)),
        "desc": str(_first(raw, "desc", "sku_desc", "name", "sku_name", default="")),
        "price": price,
        "price_text": money(price),
        "market_price": _int(_first(raw, "market_price", default=0)),
        "stock": None if stock_raw in (None, "") else _int(stock_raw),
        "sale_start": to_ms(_first(raw, "saleStart", "sale_start", "sale_begin", "start_time", default=0)),
        "sale_end": to_ms(_first(raw, "saleEnd", "sale_end", "sale_end_time", default=0)),
        "limit": limit,
        "is_sale": _int(_first(raw, "is_sale", default=0)),
        "clickable": bool(raw.get("clickable", True)),
        "sale_flag": flag_text,
        "raw": raw,
    }


def normalize_project(data: dict) -> dict:
    """把 getV2 的返回整理成前端友好的结构。

    真实结构：data.screen_list[].ticket_list[] —— 票档嵌在场次里，没有顶层 sku_list。
    """
    if not isinstance(data, dict):
        data = {}
    project = data.get("project") if isinstance(data.get("project"), dict) else data

    screens_raw = _first(project, "screen_list", "screens", default=None)
    if screens_raw is None:
        screens_raw = _first(data, "screen_list", "screens", default=None)
    screens_raw = screens_raw if isinstance(screens_raw, list) else []

    screens = []
    skus = []
    seen = set()

    for s in screens_raw:
        if not isinstance(s, dict):
            continue
        sid = _int(_first(s, "id", "screen_id", "screenId"))
        sname = str(_first(s, "name", "screen_name", "title", default=""))
        tickets_raw = _first(s, "ticket_list", "sku_list", "skus", "tickets", default=None)
        tickets = []
        for t in (tickets_raw if isinstance(tickets_raw, list) else []):
            if not isinstance(t, dict):
                continue
            sku = normalize_sku(t, screen_id=sid, screen_name=sname)
            if not sku["id"]:
                continue
            if sku["id"] in seen:
                continue
            seen.add(sku["id"])
            tickets.append(sku)
            skus.append(sku)
        flag = s.get("saleFlag") or s.get("sale_flag")
        screens.append({
            "id": sid,
            "name": sname,
            "date": str(_first(s, "start_time_str", "show_date", default="")),
            "start_time": to_ms(_first(s, "start_time", "startTime", default=0)),
            "end_time": to_ms(_first(s, "end_time", "endTime", default=0)),
            "sale_start": to_ms(_first(s, "sale_start", "sale_begin", default=0)),
            "sale_end": to_ms(_first(s, "sale_end", default=0)),
            "sale_flag": str(_first(flag, "display_name", default="")) if isinstance(flag, dict) else "",
            "clickable": bool(s.get("clickable", True)),
            "tickets": tickets,
            "raw": s,
        })

    # 兼容：万一某天又回到顶层 sku_list
    for k in (_first(project, "sku_list", default=None) or []):
        if not isinstance(k, dict):
            continue
        sku = normalize_sku(k)
        if not sku["id"] or sku["id"] in seen:
            continue
        seen.add(sku["id"])
        skus.append(sku)

    venue = project.get("venue_info") if isinstance(project.get("venue_info"), dict) else {}
    place = project.get("place_info") if isinstance(project.get("place_info"), dict) else {}
    dates = []
    for d in (_first(project, "sales_dates", default=None) or []):
        if isinstance(d, dict) and d.get("date"):
            dates.append(str(d["date"]))

    return {
        "project_id": _int(_first(project, "id", "project_id", default=0)),
        "name": str(_first(project, "name", "project_name", "title", default="")),
        "cover": str(_first(project, "cover", "banner", "image", default="")),
        "sale_start": to_ms(_first(project, "sale_start", "sale_begin", default=0)),
        "sale_end": to_ms(_first(project, "sale_end", default=0)),
        "start_time": to_ms(_first(project, "start_time", default=0)),
        "end_time": to_ms(_first(project, "end_time", default=0)),
        "city": str(_first(venue, "city_name", default="") or _first(project, "city", "city_name", default="")),
        "venue": str(_first(venue, "name", default="") or _first(project, "venue", "venue_name", default="")),
        "address": str(_first(venue, "address_detail", default="")),
        "place": str(_first(place, "name", default="")),
        "sales_dates": dates,
        "need_contact": bool(project.get("need_contact", False)),
        "prefill": bool(project.get("preFillSupport", False)),
        "pick_seat": _int(project.get("pick_seat", 0)) == 1,
        "screens": screens,
        "skus": skus,
    }


# --------------------------------------------------------------------------
# 错误分类
# --------------------------------------------------------------------------

_FATAL_AUTH = ("未登录", "登录失效", "请先登录", "token", "csrf")
_FATAL_BUYER = ("实名", "观演人", "证件")
_FATAL_LIMIT = ("限购", "已达上限", "超过购买")
_FATAL_CLOSED = ("已结束", "不存在", "已下架", "已取消", "无法购买")


def classify_error(code, msg: str) -> str:
    """把下单错误分成 retry（继续抢）和 fatal（立刻停）。"""
    m = str(msg or "")
    if code in (-101, -400, 100001, 2):
        return "auth"
    for kw in _FATAL_AUTH:
        if kw in m:
            return "auth"
    for kw in _FATAL_BUYER:
        if kw in m:
            return "buyer"
    for kw in _FATAL_LIMIT:
        if kw in m:
            return "limit"
    for kw in _FATAL_CLOSED:
        if kw in m:
            return "closed"
    return "retry"


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

class BiliClient:
    def __init__(self, store, bus):
        self.store = store
        self.bus = bus
        self.show = HostClient(SHOW_HOST, pool_size=24)
        self.passport = HostClient(PASSPORT_HOST, pool_size=4)
        self.api = HostClient(API_HOST, pool_size=4)
        self.ts = TimeSync(self.show)

    # ---------- 登录态 ----------
    def cookies(self) -> dict:
        return self.store.get("cookies", {}) or {}

    def cookie_header(self) -> str:
        ck = self.cookies()
        return "; ".join("%s=%s" % (k, v) for k, v in ck.items())

    def csrf(self) -> str:
        return self.cookies().get("bili_jct", "")

    def is_logged_in(self) -> bool:
        ck = self.cookies()
        return bool(ck.get("SESSDATA") and ck.get("bili_jct"))

    def set_cookie_string(self, raw: str) -> dict:
        """解析从浏览器复制出来的 Cookie 串。"""
        cookies = {}
        for part in str(raw or "").replace("\n", ";").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip().strip('"')
            if k:
                cookies[k] = v
        if not cookies:
            raise ValueError("没有解析到任何 Cookie，请重新复制")
        self.store.set("cookies", cookies)
        return cookies

    def logout(self) -> None:
        self.store.drop("cookies")
        self.store.drop("user")

    def _headers(self, referer: str | None = None, extra: dict | None = None) -> dict:
        h = {
            "User-Agent": DEFAULT_UA,
            "Referer": referer or REFERER_HOME,
            "Origin": "https://show.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        ck = self.cookie_header()
        if ck:
            h["Cookie"] = ck
        if extra:
            h.update(extra)
        return h

    # ---------- 账号信息 ----------
    def nav(self) -> dict:
        try:
            resp = self.api.get("/x/web-interface/nav", headers=self._headers(REFERER_HOME))
            data = resp.json() or {}
        except Exception as exc:
            raise BiliError(-1, "网络异常：%s" % exc) from exc
        d = data.get("data") or {}
        if not d.get("isLogin"):
            raise BiliError(data.get("code", -101), "Cookie 无效或已过期，请重新登录")
        user = {
            "mid": d.get("mid"),
            "uname": d.get("uname"),
            "face": d.get("face"),
            "vip": (d.get("vipStatus") == 1),
            "level": (d.get("level_info") or {}).get("current_level"),
        }
        self.store.set("user", user)
        return user

    # ---------- 项目 / 票档 ----------
    def project_detail(self, project_id: int) -> dict:
        path = "/api/ticket/project/getV2"
        params = {"version": "134", "id": project_id, "project_id": project_id}
        try:
            resp = self.show.get(path, params=params, headers=self._headers(REFERER_DETAIL % project_id))
        except Exception as exc:
            raise BiliError(-1, "获取项目信息失败（网络）：%s" % exc) from exc
        data = resp.json()
        if not isinstance(data, dict):
            raise BiliError(-1, "获取项目信息失败（返回非 JSON，HTTP %s）" % resp.status)
        code = data.get("errno", data.get("code", 0))
        if code not in (0, "0"):
            raise BiliError(code, str(data.get("msg") or data.get("message") or "接口返回异常"), data)
        return data.get("data") or {}

    def project_normalized(self, project_id: int) -> dict:
        norm = normalize_project(self.project_detail(project_id))
        norm["project_id"] = norm.get("project_id") or project_id
        return norm

    def find_sku(self, project: dict, sku_id) -> dict | None:
        for sku in project.get("skus", []):
            if str(sku.get("id")) == str(sku_id):
                return sku
        return None

    def stock_of(self, project_id: int, sku_id) -> tuple:
        """返回 (库存, 原始 sku)。库存拿不到时返回 None。"""
        norm = self.project_normalized(project_id)
        sku = self.find_sku(norm, sku_id)
        if sku is None:
            return None, None
        return sku.get("stock"), sku

    # ---------- 实名观演人 ----------
    def buyer_list(self, project_id: int) -> list:
        path = "/api/ticket/buyer/list"
        try:
            resp = self.show.get(
                path,
                params={"project_id": project_id},
                headers=self._headers(REFERER_DETAIL % project_id),
            )
        except Exception as exc:
            raise BiliError(-1, "获取实名信息失败（网络）：%s" % exc) from exc
        data = resp.json() or {}
        code = data.get("errno", data.get("code", 0))
        if code not in (0, "0"):
            raise BiliError(code, str(data.get("msg") or "获取实名信息失败"), data)
        d = data.get("data") or {}
        lst = d.get("list") or d.get("buyer_list") or []
        out = []
        for b in lst if isinstance(lst, list) else []:
            if not isinstance(b, dict):
                continue
            out.append({
                "id": b.get("id") or b.get("buyer_id"),
                "name": b.get("name") or b.get("buyer_name") or "",
                "tel": b.get("tel") or b.get("phone") or "",
                "personal_id": b.get("personal_id") or b.get("id_card") or "",
                "id_type": b.get("id_type", 0),
                "raw": b,
            })
        return out

    def resolve_buyer(self, project_id: int, buyer: dict) -> dict:
        """把用户填的实名信息匹配成下单用的 buyer_info。"""
        name = str(buyer.get("name") or "").strip()
        tel = str(buyer.get("tel") or "").strip()
        pid = str(buyer.get("personal_id") or "").strip().upper()
        id_type = _int(buyer.get("id_type", 0))
        info = {"name": name, "tel": tel, "personal_id": pid, "id_type": id_type, "id": 0}

        if buyer.get("id"):
            info["id"] = _int(buyer["id"])
            return info

        try:
            lst = self.buyer_list(project_id)
        except BiliError:
            lst = []

        for b in lst:
            if pid and _id_match(pid, str(b.get("personal_id") or "")):
                info["id"] = _int(b.get("id"))
                info["name"] = name or b.get("name", "")
                info["tel"] = tel or b.get("tel", "")
                info["personal_id"] = str(b.get("personal_id") or pid)
                return info
        for b in lst:
            if name and b.get("name") == name:
                info["id"] = _int(b.get("id"))
                info["tel"] = tel or b.get("tel", "")
                info["personal_id"] = str(b.get("personal_id") or pid)
                return info
        return info

    # ---------- 下单 ----------
    def _ts_ms(self) -> int:
        """时间戳用校准后的服务器时间，避免本机钟差把请求打早/打晚。"""
        try:
            return int(self.ts.server_now() * 1000)
        except Exception:
            return now_ms()

    def prepare(self, project_id: int, screen_id: int, sku_id: int, count: int = 1,
                order_type: int = 1, extra: dict | None = None) -> dict:
        body = {
            "project_id": _int(project_id),
            "screen_id": _int(screen_id),
            "sku_id": _int(sku_id),
            "count": _int(count, 1),
            "order_type": _int(order_type, 1),
            "timestamp": self._ts_ms(),
        }
        if extra:
            body.update(extra)
        try:
            resp = self.show.post(
                "/api/ticket/order/prepare",
                params={"project_id": project_id},
                body=body,
                headers=self._headers(REFERER_DETAIL % project_id),
            )
        except Exception as exc:
            raise BiliError(-1, "prepare 网络异常：%s" % exc) from exc
        data = resp.json() or {}
        code = data.get("errno", data.get("code", 0))
        if code not in (0, "0"):
            raise BiliError(code, str(data.get("msg") or "prepare 失败"), data)
        return data.get("data") or {}

    def create_order(self, project_id: int, screen_id: int, sku_id: int, count: int,
                     price: int, buyer_info: list, deliver_info: dict, token: str,
                     order_type: int = 1, again: int = 1, extra: dict | None = None) -> dict:
        body = {
            "project_id": _int(project_id),
            "screen_id": _int(screen_id),
            "sku_id": _int(sku_id),
            "count": _int(count, 1),
            "pay_money": _int(price) * _int(count, 1),
            "order_type": _int(order_type, 1),
            "buyer_info": json.dumps(buyer_info, ensure_ascii=False, separators=(",", ":")),
            "deliver_info": json.dumps(deliver_info, ensure_ascii=False, separators=(",", ":")),
            "again": _int(again, 1),
            "token": token,
            "timestamp": self._ts_ms(),
        }
        if extra:
            body.update(extra)
        try:
            resp = self.show.post(
                "/api/ticket/order/createV2",
                params={"project_id": project_id, "screen_id": screen_id, "sku_id": sku_id},
                body=body,
                headers=self._headers(REFERER_DETAIL % project_id),
            )
        except Exception as exc:
            raise BiliError(-1, "createV2 网络异常：%s" % exc) from exc
        data = resp.json()
        if not isinstance(data, dict):
            raise BiliError(-1, "createV2 返回非 JSON（HTTP %s）" % resp.status)
        return data

    def pay(self, project_id: int, order_id, pay_money: int, order_type: int = 1) -> dict:
        body = {
            "orderId": order_id,
            "project_id": _int(project_id),
            "pay_money": _int(pay_money),
            "order_type": _int(order_type, 1),
            "timestamp": self._ts_ms(),
        }
        ck = self.csrf()
        if ck:
            body["csrf"] = ck
        try:
            resp = self.show.post(
                "/api/ticket/order/pay",
                params={"project_id": project_id},
                body=body,
                headers=self._headers(REFERER_DETAIL % project_id),
            )
        except Exception as exc:
            raise BiliError(-1, "支付参数获取失败：%s" % exc) from exc
        return resp.json() or {}


def build_deliver_info(cfg: dict, buyer: dict) -> dict:
    mode = str(cfg.get("deliver") or "electronic")
    if mode == "express":
        return {
            "name": cfg.get("addr_name") or buyer.get("name", ""),
            "tel": cfg.get("addr_tel") or buyer.get("tel", ""),
            "addr_id": _int(cfg.get("addr_id", 0)),
            "addr": cfg.get("addr", ""),
            "addr_detail": cfg.get("addr_detail", ""),
        }
    return {"name": "", "tel": "", "addr_id": 0, "addr": "", "addr_detail": ""}


def build_buyer_info(buyer: dict) -> list:
    return [{
        "id": _int(buyer.get("id", 0)),
        "name": buyer.get("name", ""),
        "tel": buyer.get("tel", ""),
        "personal_id": buyer.get("personal_id", ""),
        "id_type": _int(buyer.get("id_type", 0)),
    }]


def order_pay_url(order_id) -> str:
    return "https://show.bilibili.com/platform/orderDetail.html?orderId=%s" % quote(str(order_id))
