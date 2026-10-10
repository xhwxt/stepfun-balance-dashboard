#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶跃星辰多账号余额查询 - 一次性 CLI 模式
不需要常驻服务：运行 → 查询全部账号 → 控制台打印表格 → 生成静态报表 report.html → 退出。

用法：
    python query.py          # 查询并生成报表
双击 query.bat 亦可。

控制台会话过期时：重新在控制台 Copy as cURL 导入（start-dashboard.bat 打开看板导入，
或直接编辑 accounts.json），然后重跑本脚本。
"""
import io
import json
import os
import sys
import time
import unicodedata

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import server  # 复用账号存储与双源查询逻辑（server.py 的 HTTP 服务仅在自身 __main__ 时启动）


def fmt(v, prefix=""):
    if v is None:
        return "--"
    return prefix + format(v, ",.2f")


def fmt_ts(ts):
    if not ts:
        return "--"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def quota_text(a):
    """额度摘要。两种套餐族都支持：
    Token Plan(plan_family=2/3) → Credit 月池剩余；老 Coding Plan(1) → 5 小时/周 双限额。"""
    parts = []
    if a.get("five_hour_reset_ts"):
        parts.append("5h剩%d%%" % round((a.get("five_hour_left_rate") or 0) * 100))
    if a.get("weekly_reset_ts"):
        parts.append("周剩%d%%" % round((a.get("weekly_left_rate") or 0) * 100))
    if a.get("plan_buckets"):
        b = next((x for x in a["plan_buckets"] if x.get("type") == 1), a["plan_buckets"][0])
        total, left = b.get("total") or 0, b.get("residual") or 0
        if total:
            parts.append("月池%.0fM/%.0fM(剩%.0f%%)" % (left / 1e6, total / 1e6, left / total * 100))
    return " ".join(parts) or "--"


def reset_text(a):
    ts = a.get("plan_reset_ts") or a.get("weekly_reset_ts") or a.get("five_hour_reset_ts")
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "--"


REAL_NAME = {1: "未实名", 2: "已实名", 3: "已注销"}

# 中文是双宽字符，%s 的对齐会把表头和数据错开 —— 自己按显示宽度补齐
_WIDE = "WF"


def _disp_w(s):
    return sum(2 if unicodedata.east_asian_width(ch) in _WIDE else 1 for ch in s)


def pad(s, width, align="l"):
    """按终端显示宽度截断 + 补齐（中文按 2 列算）。"""
    s = "" if s is None else str(s)
    if _disp_w(s) > width:
        out, w = "", 0
        for ch in s:
            cw = 2 if unicodedata.east_asian_width(ch) in _WIDE else 1
            if w + cw > width - 1:      # 留 1 列放省略号
                break
            out += ch
            w += cw
        s = out + "…"
    end = max(0, width - _disp_w(s))
    return (" " * end + s) if align == "r" else (s + " " * end)


COLUMNS = [("备注", 12, "l"), ("手机号", 13, "l"), ("余额", 9, "r"), ("充值", 8, "r"),
           ("API赠送", 9, "r"), ("Plan赠送", 9, "r"), ("套餐", 9, "l"), ("额度", 27, "l"),
           ("重置", 12, "l"), ("实名", 6, "l")]


def table_line(cells):
    return " ".join(pad(c, w, al) for (_, w, al), c in zip(COLUMNS, cells))


def print_table(data):
    accs = data.get("accounts") or []
    width = sum(w for _, w, _ in COLUMNS) + len(COLUMNS) - 1
    print()
    print("=" * width)
    print("阶跃星辰多账号余额  (查询于 %s)" % fmt_ts(data.get("updated_at")))
    print("=" * width)
    if not accs:
        print("（无账号）")
        return
    print(table_line([c[0] for c in COLUMNS]))
    print("-" * width)
    for a in accs:
        phone = (a.get("phone") or "")
        phone = phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone or "--"
        print(table_line([
            a.get("note") or "--", phone,
            fmt(a.get("balance")), fmt(a.get("cash")),
            fmt(a.get("voucher_api")), fmt(a.get("voucher_plan")),
            a.get("plan_name") or "--", quota_text(a),
            reset_text(a), REAL_NAME.get(a.get("real_name_status"), "--")]))
        for e in a.get("errors") or []:
            print("    ⚠ %s" % e)
    ok_n = sum(1 for a in accs if a.get("ok"))
    total = sum(a.get("balance") or 0 for a in accs if a.get("ok"))
    keys_n = sum(len(a.get("access_keys") or []) for a in accs)
    cost = sum(a.get("cost_month") or 0 for a in accs if a.get("ok"))
    print("-" * width)
    print("合计: %d 个账号, 成功 %d, 余额合计 ¥%.2f, 本月消费 ¥%.2f, 密钥 %d 个" % (
        len(accs), ok_n, total, cost, keys_n))
    print("报表已生成: report.html (双击即可在浏览器查看，无需服务)")


def write_report(data):
    """把查询数据烤进 index.html 生成静态报表（浏览器直接打开，零依赖）。"""
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    tag = ("<script id=\"embedded-data\" type=\"application/json\">" + payload + "</script>")
    if 'id="embedded-data"' in html:
        # 已有旧数据时替换
        import re
        html = re.sub(r'<script id="embedded-data"[^>]*>.*?</script>', tag, html, flags=re.S)
    else:
        html = html.replace("</title>", "</title>\n" + tag, 1)
    out = os.path.join(BASE_DIR, "report.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


def main():
    accounts = server.load_accounts()
    if not accounts:
        print("还没有账号。先运行 start-dashboard.bat 打开看板导入控制台凭据或添加 API Key。")
        return 1
    print("正在查询 %d 个账号…" % len(accounts))
    r = server.refresh_all()
    if r.get("error"):
        print("查询失败: %s" % r["error"])
        return 1
    with open(server.DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    write_report(data)
    print_table(data)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    code = 0
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n已取消。")
        code = 1
    except Exception as e:
        print("\n[脚本异常] %r" % e)
        code = 1
    finally:
        try:
            if sys.stdin.isatty() and sys.stdout.isatty():
                input("\n按回车键退出...")
        except Exception:
            pass
    sys.exit(code)
