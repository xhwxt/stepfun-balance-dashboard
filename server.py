#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶跃星辰 (StepFun) 多账号余额看板 - 轻量后端 v2
纯标准库实现，无第三方依赖。结构参考 tokenrhythm-balance-dashboard（基元律动余额看板）。

双数据源：
  A. 官方公开 API（免 Cookie，凭 API Key）：
     GET https://api.stepfun.com/v1/accounts  (Authorization: Bearer <key>)
     → 余额/充值总额/赠送总额（无拆分）
  B. 控制台内部网关（需会话 Cookie + webid，connect 协议 JSON POST）：
     https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard/<Method>
       QueryAccountBalance → 余额拆分(API赠送/Step Plan)、昨日/本月/累计消费
       GetStepPlanStatus   → 套餐名称/状态/到期/自动续费
       QueryStepPlanUsages → Plan Credit 消耗记录
       UserInfo            → 手机号/昵称/UID
     拒绝 API Key（403 api key not permitted），只认会话 Cookie。
     凭据获取：控制台页面 F12 → Network → 任一 Dashboard 请求 → Copy as cURL → 粘贴导入。

金额单位：控制台返回分为单位的字符串，本服务统一转为元(float)。
"""
import json
import os
import re
import time
import hashlib
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")    # 账号与凭据 [{note, api_key, cookie, webid}]
LEGACY_ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.txt")  # v1 旧格式，启动时自动迁移
DATA_FILE = os.path.join(BASE_DIR, "data.json")           # 查询结果缓存
PORT = 9156
API_BASE = "https://api.stepfun.com"
CONSOLE_BASE = "https://platform.stepfun.com/api/step.openapi.devcenter.Dashboard"
MAX_WORKERS = 8               # 并发查询上限
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_lock = threading.Lock()
_refreshing = {"running": False, "started_at": None, "finished_at": None, "msg": ""}


# ---------- 账号存储 ----------

# 令牌特征：足够长的连续 ASCII 串（阶跃 Key 不带 sk- 前缀，是 64 位裸串）
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,}$")


def parse_account_line(line):
    """解析 v1 一行账号: 备注名 API_Key 或仅 API_Key。返回 {note, api_key} 或 None。"""
    note, token = "", ""
    parts = [p.strip() for p in re.split(r"[\s,，;；]+", line) if p.strip()]
    for p in parts:
        if _TOKEN_RE.match(p) and not token:
            token = p
        elif not note:
            note = p
    if not token:
        return None
    return {"note": note, "api_key": token}


def _read_accounts():
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def load_accounts():
    accounts = _read_accounts()
    # 老数据补固定 id：算一次后写回，之后 Cookie 续期/补 Key 都不影响账号身份
    changed = False
    for a in accounts:
        if not a.get("id"):
            a["id"] = account_id(a)
            changed = True
    if changed:
        save_accounts(accounts)
    # v1 accounts.txt 自动迁移（一次）
    if not accounts and os.path.exists(LEGACY_ACCOUNTS_FILE):
        migrated = []
        try:
            with open(LEGACY_ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    a = parse_account_line(line)
                    if a:
                        migrated.append({"note": a["note"], "api_key": a["api_key"],
                                         "cookie": None, "webid": None})
        except Exception:
            migrated = []
        if migrated:
            save_accounts(migrated)
            try:
                os.rename(LEGACY_ACCOUNTS_FILE, LEGACY_ACCOUNTS_FILE + ".migrated")
            except Exception:
                pass
            return migrated
    return accounts


def save_accounts(accounts):
    tmp = ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ACCOUNTS_FILE)


def account_id(entry):
    """稳定 ID：优先用条目里持久保存的 id 字段，永不变化。"""
    if entry.get("id"):
        return entry["id"]
    seed = entry.get("api_key") or entry.get("cookie") or entry.get("note") or ""
    return hashlib.sha256(seed.encode()).hexdigest()[:12]


def find_account(aid):
    for a in load_accounts():
        if account_id(a) == aid:
            return a
    return None


# ---------- Copy as cURL 解析 ----------

def parse_curl(text):
    """从浏览器 Copy as cURL 文本中解析控制台凭据。返回 (cookie, webid) 或 (None, None)。"""
    cookie, webid = None, None
    # 提取所有 -H/--header 参数值，兼容单引号/双引号/无引号/ bash $' '
    header_vals = []
    for m in re.finditer(r"(?:-H|--header)\s+(\$?'([^']*)'|\"([^\"]*)\"|(\S+))", text):
        header_vals.append(m.group(2) or m.group(3) or m.group(4) or "")
    # -b/--cookie 直接给 cookie
    for m in re.finditer(r"(?:-b|--cookie)\s+(\$?'([^']*)'|\"([^\"]*)\"|(\S+))", text):
        header_vals.append("Cookie: " + (m.group(2) or m.group(3) or m.group(4) or ""))
    for h in header_vals:
        if h is None:
            continue
        hl = h.strip()
        m = re.match(r"(?i)^cookie\s*:\s*(.+)$", hl)
        if m and m.group(1).strip():
            cookie = m.group(1).strip()
        m = re.match(r"(?i)^oasis-webid\s*:\s*(.+)$", hl)
        if m and m.group(1).strip():
            webid = m.group(1).strip().strip('"')
    return cookie, webid


def parse_import_text(text):
    """批量导入 API Key：每行同 parse_account_line。返回 (accounts, 无法识别行数)。"""
    accounts, bad = [], 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        a = parse_account_line(line)
        if a:
            e = {"note": a["note"], "api_key": a["api_key"],
                 "cookie": None, "webid": None}
            e["id"] = account_id(e)
            accounts.append(e)
        else:
            bad += 1
    return accounts, bad


# ---------- 数据源 A：官方公开 API ----------

def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _fen_to_yuan(v):
    """控制台金额（分，字符串）转元。"""
    n = _num(v)
    return None if n is None else round(n / 100.0, 6)


def fetch_apikey(api_key):
    """官方 /v1/accounts。返回 dict 或抛异常。"""
    req = urllib.request.Request(API_BASE + "/v1/accounts")
    req.add_header("Authorization", "Bearer " + api_key)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- 数据源 B：控制台网关 ----------

SESSION_LOG = os.path.join(BASE_DIR, "console_session.log")   # 会话寿命观测日志


def _session_log(event, note, extra=""):
    """记录会话事件（只记事件与状态码，绝不记凭据值）。"""
    try:
        with open(SESSION_LOG, "a", encoding="utf-8") as f:
            f.write("%s\t%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                          note or "?", event, extra))
    except Exception:
        pass


def _parse_set_cookies(headers_obj):
    """从 urllib 响应头提取 Set-Cookie 列表 → [(name, value)]。"""
    try:
        raw = headers_obj.get_all("Set-Cookie") or []
    except Exception:
        raw = []
    out = []
    for line in raw:
        first = line.split(";", 1)[0]
        if "=" in first:
            n, _, v = first.partition("=")
            out.append((n.strip(), v.strip()))
    return out


def merge_cookie(old_cookie, set_cookies):
    """把 Set-Cookie 合并回 Cookie 串（同名替换、新增追加）。返回新串。"""
    parts, order = {}, []
    for p in (old_cookie or "").split(";"):
        p = p.strip()
        if not p or "=" not in p:
            continue
        n, _, v = p.partition("=")
        n = n.strip()
        if n not in parts:
            order.append(n)
        parts[n] = v.strip()
    for n, v in set_cookies:
        if n not in parts:
            order.append(n)
        parts[n] = v
    return "; ".join("%s=%s" % (n, parts[n]) for n in order)


def passport_ping(cookie):
    """控制台浏览器靠这个心跳维持会话；服务端重放也定时发。返回 (status, set_cookies)。"""
    req = urllib.request.Request(
        "https://platform.stepfun.com/passport/proto.api.passport.v1.GlobalPassportService/Ping",
        data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("connect-protocol-version", "1")
    req.add_header("User-Agent", UA)
    req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, _parse_set_cookies(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, _parse_set_cookies(e.headers)
    except Exception:
        return -1, []


def console_call(method, cookie, webid, body=None):
    """connect 协议 JSON POST 到控制台网关。返回 (status, dict, set_cookies)。"""
    headers = {
        "Content-Type": "application/json",
        "connect-protocol-version": "1",
        "oasis-appid": "10300",
        "oasis-platform": "web",
        "oasis-webid": webid,
        "Cookie": cookie,
        "User-Agent": UA,
        "Origin": "https://platform.stepfun.com",
        "Referer": "https://platform.stepfun.com/account-overview",
    }
    req = urllib.request.Request(CONSOLE_BASE + "/" + method,
                                 data=json.dumps(body or {}).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8")), _parse_set_cookies(r.headers)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            detail = {}
        return e.code, detail, _parse_set_cookies(e.headers)


class ConsoleAuthError(Exception):
    pass


def console_selfheal(entry):
    """会话 401 时用整份 Cookie 调 passport RefreshToken 自愈。
    响应 Set-Cookie 会轮换 Oasis-Token/Oasis-Webid，合并后写回 entry。
    注意：刷新令牌一次性（rotation），只在 401 时调用，绝不连续刷。
    返回 True=自愈成功。"""
    cookie, webid = entry.get("cookie") or "", entry.get("webid") or ""
    note = entry.get("note") or "?"
    req = urllib.request.Request(
        "https://platform.stepfun.com/passport/proto.api.passport.v1.PassportService/RefreshToken",
        data=b"{}", method="POST")
    for k, v in {"Content-Type": "application/json",
                 "connect-protocol-version": "1",
                 "oasis-appid": "10300", "oasis-platform": "web",
                 "oasis-webid": webid, "Cookie": cookie,
                 "User-Agent": UA,
                 "Origin": "https://platform.stepfun.com",
                 "Referer": "https://platform.stepfun.com/account-overview"}.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            json.loads(r.read().decode("utf-8"))
            new_cookie = merge_cookie(cookie, _parse_set_cookies(r.headers))
    except urllib.error.HTTPError as e:
        _session_log("heal_fail", note, "st=%d" % e.code)
        return False
    except Exception as e:
        _session_log("heal_fail", note, str(e)[:60])
        return False
    if new_cookie == cookie:
        _session_log("heal_fail", note, "no set-cookie")
        return False
    entry["cookie"] = new_cookie
    _session_log("healed", note, "")
    return True


def console_query(entry):
    """用一条控制台凭据拉全量数据。entry 会被就地更新（Cookie 自动续期）。
    返回 dict；会话失效抛 ConsoleAuthError。"""
    cookie = entry.get("cookie") or ""
    webid = entry.get("webid") or ""
    note = entry.get("note") or "?"
    rotated = False

    def call(method):
        """发请求并即时合并 Set-Cookie（会话轮换自续期）。"""
        nonlocal cookie, rotated
        st, d, sc = console_call(method, cookie, webid)
        if sc:
            new = merge_cookie(cookie, sc)
            if new != cookie:
                cookie = new
                rotated = True
                entry["cookie"] = new
                _session_log("setcookie", note, method)
        return st, d

    # 心跳保活（浏览器以此维持会话；同时可能下发新 Cookie）
    pst, psc = passport_ping(cookie)
    if psc:
        new = merge_cookie(cookie, psc)
        if new != cookie:
            cookie = new
            rotated = True
            entry["cookie"] = new
    _session_log("ping", note, "st=%d" % pst)

    out = {}
    st, bal = call("QueryAccountBalance")
    if st == 401 or bal.get("code") in ("unauthenticated",):
        # 自愈：一次性刷新令牌换新会话后重试（仅此一次，不连环刷）
        if console_selfheal(entry):
            cookie = entry["cookie"]
            st, bal = call("QueryAccountBalance")
        if st == 401 or bal.get("code") in ("unauthenticated",):
            _session_log("auth401", note, "QueryAccountBalance")
            raise ConsoleAuthError("会话失效(401)，请重新导出 cURL")
    if st == 200 and "balance" in bal:
        out["balance"] = _fen_to_yuan(bal.get("balance"))
        out["cash"] = _fen_to_yuan(bal.get("payment"))
        out["voucher"] = _fen_to_yuan(bal.get("voucher"))
        out["voucher_api"] = _fen_to_yuan(bal.get("voucher_api"))
        out["voucher_plan"] = _fen_to_yuan(bal.get("voucher_plan"))
        out["cost_month"] = _fen_to_yuan(bal.get("cost_month"))
        out["cost_total"] = _fen_to_yuan(bal.get("cost_total"))
    st, plan = call("GetStepPlanStatus")
    sub = (plan.get("subscription") or {}) if isinstance(plan, dict) else {}
    if sub:
        out["plan_name"] = sub.get("name")
        out["plan_status"] = sub.get("status")          # 1=生效
        out["plan_expire_ts"] = int(sub.get("expired_at") or 0)
        out["plan_auto_renew"] = bool(sub.get("auto_renew"))
    st, usage = call("QueryStepPlanUsages")
    records = (usage.get("records") or []) if isinstance(usage, dict) else []
    if records:
        credits = sum(int(r.get("credit_consumed") or 0) for r in records)
        calls = sum(int(r.get("calls") or 0) for r in records)
        out["plan_credit_used"] = credits     # 今日（默认查询窗口为当天）
        out["plan_calls"] = calls
    # 本订阅周期 Credit 额度/剩余/重置（QueryStepPlanRateLimit）
    st, rl = call("QueryStepPlanRateLimit")
    pc = (rl.get("plan_credit_rate_limit") or {}) if isinstance(rl, dict) else {}
    buckets = []
    for b in pc.get("credit_buckets") or []:
        buckets.append({
            "type": b.get("type"),            # 1=订阅池，其余为加油包
            "total": _num(b.get("credit_total")),
            "residual": _num(b.get("credit_residual")),
            "expire_ts": int(b.get("expire_at") or 0),
            "reset_ts": int(b.get("next_reset_at") or 0),
        })
    if buckets:
        out["plan_buckets"] = buckets
        out["plan_reset_ts"] = int(pc.get("subscription_credit_reset_time") or 0)
    # API Key 列表（返回完整 Key，本机存储，用于看板展示/复制）
    st, keys = call("ListAccessKeys")
    aks = []
    for k in keys.get("accessKeys") or []:
        if str(k.get("isDeleted") or "0") == "1":
            continue
        try:
            last_used = int(int(k.get("lastUsedAt") or 0) / 1000)
        except (TypeError, ValueError):
            last_used = 0
        aks.append({
            "name": k.get("name") or "",
            "key": k.get("accessKey") or "",
            "key_id": str(k.get("keyId") or ""),
            "is_default": str(k.get("isDefault") or "0") == "1",
            "last_used_ts": last_used,
        })
    if aks:
        out["access_keys"] = aks
    st, me = call("UserInfo")
    if isinstance(me, dict) and me.get("uid"):
        out["uid"] = str(me.get("uid"))
        out["phone"] = me.get("mobile") or ""
        out["nickname"] = me.get("nickname") or ""
    return out


# ---------- 单账号查询（合并双源） ----------

def fetch_one(entry, prev_balance=None):
    token = entry.get("api_key") or ""
    cookie, webid = entry.get("cookie"), entry.get("webid")
    res = {
        "id": account_id(entry),
        "note": entry.get("note") or "",
        "key_tail": ("***" + token[-4:]) if token else None,
        "src_api": False,
        "src_console": False,
        "ok": False,
        "errors": [],
        "checked_at": None,
        "type": None,
        "balance": None, "cash": None, "voucher": None,
        "voucher_api": None, "voucher_plan": None,
        "cost_month": None, "cost_total": None,
        "plan_name": None, "plan_expire_ts": None,
        "plan_auto_renew": None, "plan_credit_used": None, "plan_calls": None,
        "plan_buckets": None, "plan_reset_ts": None,
        "access_keys": None,
        "uid": None, "phone": None, "nickname": None,
        "prev_balance": prev_balance,
    }
    # 源 A：API Key
    if token:
        try:
            d = fetch_apikey(token)
            res["type"] = d.get("type")
            res["balance"] = _num(d.get("balance"))
            res["cash"] = _num(d.get("total_cash_balance"))
            res["voucher"] = _num(d.get("total_voucher_balance"))
            res["src_api"] = True
        except urllib.error.HTTPError as e:
            msg = "凭据失效(401)，Key 已删除或填错" if e.code == 401 else "官方API HTTP %d" % e.code
            res["errors"].append(msg)
        except Exception as e:
            res["errors"].append("官方API异常: %s" % str(e)[:80])
    # 源 B：控制台（数据更全，覆盖同名字段；entry 的 Cookie 会被就地续期）
    if cookie and webid:
        try:
            c = console_query(entry)
            res.update({k: v for k, v in c.items() if v is not None})
            res["src_console"] = True
        except ConsoleAuthError as e:
            res["errors"].append(str(e))
        except Exception as e:
            res["errors"].append("控制台异常: %s" % str(e)[:80])
    elif cookie or webid:
        res["errors"].append("控制台凭据不完整（缺 cookie 或 webid）")

    res["ok"] = res["src_api"] or res["src_console"]
    res["checked_at"] = int(time.time())
    return res


def _load_prev_balances():
    prev = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                for a in json.load(f).get("accounts") or []:
                    if a.get("ok") and a.get("balance") is not None:
                        prev[a["id"]] = a["balance"]
        except Exception:
            pass
    return prev


def refresh_all():
    with _lock:
        if _refreshing["running"]:
            return {"error": "刷新进行中"}
        _refreshing["running"] = True
        _refreshing["started_at"] = int(time.time())
        _refreshing["msg"] = ""
    try:
        accounts = load_accounts()
        prev = _load_prev_balances()
        cookies_before = {id(a): a.get("cookie") for a in accounts}
        results = []
        if accounts:
            n = min(MAX_WORKERS, max(1, len(accounts)))
            with ThreadPoolExecutor(max_workers=n) as ex:
                futs = [ex.submit(fetch_one, a, prev.get(account_id(a))) for a in accounts]
                for fut in as_completed(futs):
                    results.append(fut.result())
        results.sort(key=lambda r: (r["note"] or "", r["id"]))
        # 控制台凭据可能被会话轮换更新，持久化
        cookie_changed = any(a.get("cookie") != cookies_before[id(a)] for a in accounts)
        # 控制台凭据可自动补全 API Key：条目缺 key 时用其默认密钥回填（幂等，回填后启用官方API源）
        try:
            by_id = {account_id(a): a for a in accounts}
            changed = False
            for r in results:
                entry = by_id.get(r["id"])
                if entry and not entry.get("api_key") and r.get("access_keys"):
                    dk = next((k["key"] for k in r["access_keys"] if k["is_default"] and k["key"]),
                              r["access_keys"][0].get("key") or "")
                    if dk:
                        entry["api_key"] = dk
                        changed = True
                        r["key_tail"] = "***" + dk[-4:]
                        r["src_api"] = True
            if changed or cookie_changed:
                with _lock:
                    save_accounts(accounts)
        except Exception:
            pass
        data = {"updated_at": int(time.time()), "accounts": results}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        ok_n = sum(1 for r in results if r["ok"])
        total = sum(r["balance"] or 0 for r in results if r["ok"])
        _refreshing["msg"] = "刷新完成: 成功 %d / 共 %d，合计余额 ¥%.2f" % (ok_n, len(results), total)
        return {"ok": True, "msg": _refreshing["msg"]}
    except Exception as e:
        _refreshing["msg"] = "刷新异常: %s" % e
        return {"error": str(e)}
    finally:
        _refreshing["finished_at"] = int(time.time())
        _refreshing["running"] = False


def bg_loop():
    # 已按用户要求移除后台自动刷新：数据更新只靠启动首轮 + 看板「立即刷新」按钮 + query.bat
    # 好处：不定期打上游、不无谓消耗一次性刷新令牌；自愈机制仍在 401 时兜底
    return


# ---------- HTTP ----------

def read_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def send_json(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _serve_index(self):
        path = os.path.join(BASE_DIR, "index.html")
        if not os.path.exists(path):
            send_json(self, {"error": "index.html 不存在"}, 500)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index.html"):
            self._serve_index()
        elif self.path == "/api/data":
            data = {"updated_at": None, "accounts": []}
            if os.path.exists(DATA_FILE):
                try:
                    with open(DATA_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    pass
            send_json(self, data)
        elif self.path == "/api/status":
            with _lock:
                st = dict(_refreshing)
            st["account_count"] = len(load_accounts())
            send_json(self, st)
        else:
            send_json(self, {"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/refresh":
            threading.Thread(target=refresh_all, daemon=True).start()
            send_json(self, {"ok": True, "msg": "已开始刷新"})
        elif self.path == "/api/console-import":
            # 导入控制台凭据：粘贴 Copy as cURL
            body = read_body(self)
            curl_text = body.get("curl", "")
            note = (body.get("note") or "").strip()
            cookie, webid = parse_curl(curl_text)
            if not cookie or not webid:
                send_json(self, {"error": "未从 cURL 中解析出 Cookie 和 oasis-webid。"
                                "请确认复制的是 platform.stepfun.com 上 Dashboard/ 开头的请求"}, 400)
                return
            # 先验证凭据有效性并取身份
            try:
                st, me, sc = console_call("UserInfo", cookie, webid)
                if sc:
                    cookie = merge_cookie(cookie, sc)
                if st != 200 or not me.get("uid"):
                    send_json(self, {"error": "凭据无效（UserInfo 返回 %s），会话可能已过期" % st}, 400)
                    return
            except Exception as e:
                send_json(self, {"error": "验证凭据失败: %s" % str(e)[:100]}, 500)
                return
            phone = me.get("mobile") or ""
            auto_note = note or (me.get("nickname") or "") or (phone[:3] + "****" + phone[-4:] if phone else "")
            with _lock:
                accounts = load_accounts()
                # 合并策略：① Cookie 相同 → 同一账号；② 用户填的备注名与已有条目相同 → 合并；
                # ③ 已有控制台凭据且手机号相同 → 同一账号；④ 都不匹配 → 新建
                target = None
                for a in accounts:
                    if a.get("cookie") == cookie:
                        target = a
                        break
                if target is None and note:
                    for a in accounts:
                        if (a.get("note") or "") == note:
                            target = a
                            break
                if target is None and phone:
                    for a in accounts:
                        if a.get("webid") and a.get("cookie"):
                            try:
                                _, m2, _ = console_call("UserInfo", a["cookie"], a["webid"])
                                if (m2.get("mobile") or "") == phone:
                                    target = a
                                    break
                            except Exception:
                                pass
                if target is None:
                    target = {"note": auto_note, "api_key": None, "cookie": None, "webid": None}
                    target["id"] = account_id(target)
                    accounts.append(target)
                target["cookie"] = cookie
                target["webid"] = webid
                if note:
                    target["note"] = note
                save_accounts(accounts)
            threading.Thread(target=refresh_all, daemon=True).start()
            masked = phone[:3] + "****" + phone[-4:] if phone else "未知号码"
            send_json(self, {"ok": True, "msg": "已导入控制台凭据: %s（%s），正在刷新" % (auto_note, masked)})
        elif self.path == "/api/accounts":
            body = read_body(self)
            action = body.get("action")
            with _lock:
                accounts = load_accounts()
                if action == "add":
                    a = parse_account_line(body.get("line", "").strip())
                    if not a:
                        send_json(self, {"error": "格式无法识别，请提供 API Key（长串 ASCII 令牌）"}, 400)
                        return
                    if any(x.get("api_key") == a["api_key"] for x in accounts):
                        send_json(self, {"error": "该 Key 已存在"}, 400)
                        return
                    accounts.append(e := {"note": a["note"], "api_key": a["api_key"],
                                          "cookie": None, "webid": None})
                    e["id"] = account_id(e)
                    save_accounts(accounts)
                    threading.Thread(target=refresh_all, daemon=True).start()
                    send_json(self, {"ok": True, "msg": "已添加: %s" % (a["note"] or a["api_key"][:8] + "…")})
                elif action == "remove":
                    aid = body.get("id", "")
                    accounts = [x for x in accounts if account_id(x) != aid]
                    save_accounts(accounts)
                    # 同步更新缓存，前端刷新后卡片立即消失（否则看起来像删除无效）
                    try:
                        if os.path.exists(DATA_FILE):
                            with open(DATA_FILE, "r", encoding="utf-8") as f:
                                d = json.load(f)
                            d["accounts"] = [x for x in d.get("accounts") or []
                                             if x.get("id") != aid]
                            with open(DATA_FILE, "w", encoding="utf-8") as f:
                                json.dump(d, f, ensure_ascii=False, indent=1)
                    except Exception:
                        pass
                    send_json(self, {"ok": True, "msg": "已删除"})
                elif action == "rename":
                    aid = body.get("id", "")
                    note = (body.get("note") or "").strip()[:30]
                    if not note:
                        send_json(self, {"error": "名字不能为空"}, 400)
                        return
                    hit = next((x for x in accounts if account_id(x) == aid), None)
                    if not hit:
                        send_json(self, {"error": "账号不存在"}, 404)
                        return
                    hit["note"] = note
                    save_accounts(accounts)
                    send_json(self, {"ok": True, "msg": "已改名为 %s" % note})
                elif action == "import":
                    mode = body.get("mode", "append")
                    new_list, bad = parse_import_text(body.get("text", ""))
                    if not new_list and bad:
                        send_json(self, {"error": "未识别到任何有效 Key（无法识别 %d 行）" % bad}, 400)
                        return
                    if mode == "replace":
                        save_accounts(new_list)
                        send_json(self, {"ok": True, "msg": "已清空并导入: %d 条，无法识别 %d" % (len(new_list), bad)})
                    else:
                        cur = load_accounts()
                        added = dup = 0
                        for a in new_list:
                            if any(x.get("api_key") == a["api_key"] for x in cur):
                                dup += 1
                                continue
                            cur.append(a)
                            added += 1
                        save_accounts(cur)
                        send_json(self, {"ok": True, "msg": "追加导入: 新增 %d，跳过已存在 %d，无法识别 %d" % (added, dup, bad)})
                    threading.Thread(target=refresh_all, daemon=True).start()
                else:
                    send_json(self, {"error": "unknown action"}, 400)
        else:
            send_json(self, {"error": "not found"}, 404)


if __name__ == "__main__":
    if not os.path.exists(ACCOUNTS_FILE):
        save_accounts([])
    threading.Thread(target=refresh_all, daemon=True).start()  # 启动即刷首轮，之后全靠手动
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("阶跃星辰多账号余额看板已启动: http://127.0.0.1:%d  (手动刷新模式)" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
