"""bilibili 会员购（show.bilibili.com）接口封装。

覆盖链路：
    链接 → project_id → 项目详情（场次 / 票档 / 库存 / 开售时间）
         → 实名观演人 → prepare（拿 token）→ createV2（下单）→ pay（取支付参数）

接口字段以线上为准，解析层对字段做了容错（多种别名都能吃），
所以 B 站小改字段时通常不用改代码。
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import quote

from .httpc import DEFAULT_UA, HostClient, TimeSync
from .util import now_ms, to_ms

SHOW_HOST = "show.bilibili.com"
PASSPORT_HOST = "passport.bilibili.com"
API_HOST = "api.bilibili.com"

REFERER_HOME = "https://show.bilibili.com/"
REFERER_DETAIL = "https://show.bilibili.com/platform/detail.html?id=%s"

# 下单接口的请求体编码。浏览器走 form-urlencoded；但本机 HTTP 客户端发 form
# 可能被网关判成非浏览器请求（-412），默认用 JSON 提交。
#   json / jsonobj：application/json，buyer_info / deliver_info 为嵌套对象；
#   form          ：application/x-www-form-urlencoded，这两字段为 JSON 字符串（浏览器原生）。
ENCODE = os.environ.get("BILI_ENCODE", "json").strip().lower()


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
                # channel=3 + nomask=1：拿完整身份证号和手机号。
                # 不带这两个参数时接口只回打码值（4401**********1234），
                # 打码值既不能下单，也填不进联系人。
                params={"project_id": project_id, "channel": 3, "nomask": 1},
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

        try:
            lst = self.buyer_list(project_id)
        except BiliError:
            lst = []

        # 只给了观演人 id（roster 里存的就是 id）：从 nomask 列表补全姓名/手机号/证件号
        if buyer.get("id"):
            info["id"] = _int(buyer["id"])
            for b in lst:
                if _int(b.get("id")) == info["id"]:
                    info["name"] = name or b.get("name", "")
                    info["tel"] = tel or b.get("tel", "")
                    info["personal_id"] = pid or str(b.get("personal_id") or "")
                    return info
            return info

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

    def resolve_buyers(self, project_id: int, buyers: list) -> list:
        """逐个匹配任务观演人；只有完全没身份可查才报错，缺 id 交给上层警告。"""
        out = []
        for i, b in enumerate(buyers, 1):
            info = self.resolve_buyer(project_id, b or {})
            if not info.get("personal_id") and not info.get("id"):
                raise BiliError(-1, "第 %d 位观演人实名信息不完整：需要身份证号（或从账号同步观演人）" % i)
            out.append(info)
        return out

    # ---------- 下单 ----------
    def _order_post(self, path: str, body: dict, project_id, extra_params: dict | None = None):
        """按 ENCODE 选择 form/JSON 提交；form 模式下补 csrf。"""
        params = {"project_id": project_id}
        if extra_params:
            params.update(extra_params)
        if ENCODE in ("json", "jsonobj"):
            return self.show.post(path, params=params, body=body,
                                  headers=self._headers(REFERER_DETAIL % project_id))
        payload = dict(body)
        ck = self.csrf()
        if ck:
            payload["csrf"] = ck
        return self.show.post(path, params=params, body=payload,
                              headers=self._headers(REFERER_DETAIL % project_id),
                              form=True)

    def _ts_ms(self) -> int:
        """时间戳用校准后的服务器时间，避免本机钟差把请求打早/打晚。"""
        try:
            return int(self.ts.server_now() * 1000)
        except Exception:
            return now_ms()

    @staticmethod
    def _click_position(now_ms_value: int) -> dict:
        """模拟浏览器里的「点按」事件，坐标落在常见屏幕右侧按钮区。"""
        origin = now_ms_value - 8000
        return {"x": 1325, "y": 885, "origin": origin, "now": now_ms_value}

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
            resp = self._order_post("/api/ticket/order/prepare", body, project_id)
        except Exception as exc:
            raise BiliError(-1, "prepare 网络异常：%s" % exc) from exc
        data = resp.json()
        if not isinstance(data, dict):
            raise BiliError(-1, "prepare 返回非 JSON（HTTP %s）：%s" % (resp.status, resp.text()[:120]))
        code = data.get("errno", data.get("code", 0))
        if code not in (0, "0"):
            raise BiliError(code, str(data.get("msg") or "prepare 失败"), data)
        return data.get("data") or {}

    def create_order(self, project_id: int, screen_id: int, sku_id: int, count: int,
                     price: int, buyer_info: list, deliver_info: dict, token: str,
                     order_type: int = 1, again: int = 1, extra: dict | None = None) -> dict:
        first = (buyer_info or [{}])[0] if isinstance(buyer_info, list) else {}
        now = self._ts_ms()
        body = {
            "project_id": _int(project_id),
            "screen_id": _int(screen_id),
            "sku_id": _int(sku_id),
            "count": _int(count, 1),
            "pay_money": _int(price) * _int(count, 1),
            "order_type": _int(order_type, 1),
            "again": _int(again, 1),
            "token": token,
            # 服务端要求提交时间略晚于 now（浏览器点按时刻），提前会被风控盯上。
            "timestamp": now + 5000,
            # 新下单链路的风控字段：缺 newRisk / requestSource / clickPosition
            # 时会直接回 209001「本项目需要联系人信息」，与联系人本身无关。
            "deviceId": "",
            "newRisk": "true",
            "requestSource": "neul-next",
            "clickPosition": self._click_position(now),
            # 联系人（姓名/手机号）同时放在顶层 buyer / tel，
            # 电子票也走这两个字段校验；deliver_info 只管邮寄地址。
            "buyer": str(first.get("id") or ""),
            "tel": str(first.get("tel") or ""),
        }
        if extra:
            body.update(extra)
        # buyer_info / deliver_info 在 JSON 体里也必须序列化成字符串：
        # 传对象会被网关判成绑定错误（80110374 service error）。
        body["buyer_info"] = json.dumps(buyer_info, ensure_ascii=False, separators=(",", ":"))
        body["deliver_info"] = json.dumps(deliver_info, ensure_ascii=False, separators=(",", ":"))
        if ENCODE == "jsonobj":
            # 仅用于排查：老 jsonobj 模式把这两个字段还原成对象。
            body["buyer_info"] = buyer_info
            body["deliver_info"] = deliver_info
        try:
            resp = self._order_post(
                "/api/ticket/order/createV2", body, project_id,
                {"screen_id": screen_id, "sku_id": sku_id},
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
            resp = self._order_post("/api/ticket/order/pay", body, project_id)
        except Exception as exc:
            raise BiliError(-1, "支付参数获取失败：%s" % exc) from exc
        return resp.json() or {}


def build_deliver_info(cfg: dict, buyer: dict) -> dict:
    mode = str(cfg.get("deliver") or "electronic")
    name = cfg.get("addr_name") or buyer.get("name", "")
    tel = cfg.get("addr_tel") or buyer.get("tel", "")
    if mode == "express":
        return {
            "name": name,
            "tel": tel,
            "addr_id": _int(cfg.get("addr_id", 0)),
            "addr": cfg.get("addr", ""),
            "addr_detail": cfg.get("addr_detail", ""),
        }
    # 电子票也要联系人：B 站按项目要求 deliver_info.name / tel 必填，
    # 空串会被拒（"本项目需要联系人信息，请填写姓名及手机号"）。
    return {"name": name, "tel": tel, "addr_id": 0, "addr": "", "addr_detail": ""}


MAX_BUYERS = 6


def build_buyer_info(buyers) -> list:
    """任务观演人 → createV2 的 buyer_info 数组（每人 1 张，整单原子）。

    兼容单个 dict：只有一位观演人时直接传 dict 也可以。
    """
    if isinstance(buyers, dict):
        buyers = [buyers]
    return [{
        "id": _int(b.get("id", 0)),
        "name": b.get("name", ""),
        "tel": b.get("tel", ""),
        "personal_id": b.get("personal_id", ""),
        "id_type": _int(b.get("id_type", 0)),
    } for b in buyers if isinstance(b, dict)]


def order_pay_url(order_id) -> str:
    return "https://show.bilibili.com/platform/orderDetail.html?orderId=%s" % quote(str(order_id))
