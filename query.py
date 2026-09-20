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


def print_table(data):
    accs = data.get("accounts") or []
    print()
    print("=" * 96)
    print("阶跃星辰多账号余额  (查询于 %s)" % fmt_ts(data.get("updated_at")))
    print("=" * 96)
    if not accs:
        print("（无账号）")
        return
    header = "%-14s %-13s %12s %10s %10s %10s  %-16s %10s" % (
        "备注", "手机号", "余额", "充值", "API赠送", "Plan赠送", "套餐", "本月消费")
    print(header)
    print("-" * 96)
    for a in accs:
        phone = (a.get("phone") or "")
        phone = phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else phone or "--"
        plan = "--"
        if a.get("plan_name"):
            plan = a["plan_name"]
            for b in a.get("plan_buckets") or []:
                if b.get("type") == 1 and b.get("total"):
                    used = (b["total"] or 0) - (b["residual"] or 0)
                    plan += " 已用%.2fM/%.0fM" % (used / 1e6, b["total"] / 1e6)
                    break
        status = "正常" if a.get("ok") else "异常"
        print("%-14s %-13s %12s %10s %10s %10s  %-16s %10s" % (
            (a.get("note") or "--")[:14], phone,
            fmt(a.get("balance")), fmt(a.get("cash")),
            fmt(a.get("voucher_api")), fmt(a.get("voucher_plan")),
            plan[:16], fmt(a.get("cost_month"))))
        for e in a.get("errors") or []:
            print("    ⚠ %s" % e)
    ok_n = sum(1 for a in accs if a.get("ok"))
    total = sum(a.get("balance") or 0 for a in accs if a.get("ok"))
    keys_n = sum(len(a.get("access_keys") or []) for a in accs)
    print("-" * 96)
    print("合计: %d 个账号, 成功 %d, 余额合计 ¥%.2f, 密钥 %d 个" % (len(accs), ok_n, total, keys_n))
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
