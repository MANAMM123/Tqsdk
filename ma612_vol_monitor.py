#!/usr/bin/env python3
"""
MA612 甲醇期权：每日收盘波动率监控 + 卖方判定

用法:
    python3 ma612_vol_monitor.py                  # 跑一次并打印结果
    python3 ma612_vol_monitor.py --daemon         # 每个交易日 15:05 自动跑
    python3 ma612_vol_monitor.py --no-log         # 不写 CSV
    python3 ma612_vol_monitor.py --underlying CZCE.MA612 --log /workspace/vol.csv

凭据:
    优先读环境变量 TQ_USER / TQ_PASS，否则读 ~/.tqsdk_auth.env
    （文件内容两行：TQ_USER=xxx / TQ_PASS=xxx，权限建议 chmod 600）

每日自动:
    crontab -e 里加一行（周一至周五 15:05）：
    5 15 * * 1-5 cd /workspace && /usr/bin/python3 ma612_vol_monitor.py >> vol.log 2>&1
"""

import argparse
import csv
import datetime
import math
import os
import sys
import time

import numpy as np
from tqsdk import TqApi, TqAuth

TRADING_DAYS = 252

# 判定阈值（波动率点数，1 点 = 1 个 IV 百分点）
IV_RICH = 60.0        # 一档：IV >= 60 且有 RV 缓冲
IV_FAIR = 55.0        # 二档起点
IV_CHEAP = 50.0       # 低于此视为买方市场
VRP_MIN = 8.0         # 卖方最低要求：IV - RV20 >= 8 点
WING_MIN = 10.0       # 翼部相对 RV20 的溢价门槛
MIN_DAYS = 3.0        # 距到期少于该天数不再新开卖方


def load_auth(path="~/.tqsdk_auth.env"):
    user = os.environ.get("TQ_USER")
    pwd = os.environ.get("TQ_PASS")
    if user and pwd:
        return user, pwd
    path = os.path.expanduser(path)
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "TQ_USER":
                user = v.strip()
            elif k.strip() == "TQ_PASS":
                pwd = v.strip()
    if not (user and pwd):
        sys.exit("缺少快期账户：设置环境变量 TQ_USER/TQ_PASS，或写入 ~/.tqsdk_auth.env")
    return user, pwd


def _norm(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76(f, k, t, vol, is_call):
    if t <= 0 or vol <= 0:
        return max(0.0, (f - k) if is_call else (k - f))
    sd = vol * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * vol * vol * t) / sd
    d2 = d1 - sd
    if is_call:
        return f * _norm(d1) - k * _norm(d2)
    return k * _norm(-d2) - f * _norm(-d1)


def implied_vol(f, k, t, price, is_call):
    intrinsic = max(0.0, (f - k) if is_call else (k - f))
    if price <= intrinsic or t <= 0:
        return None
    lo, hi = 1e-4, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if black76(f, k, t, mid, is_call) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def realized_vol(closes, n):
    r = np.diff(np.log(closes[-(n + 1):]))
    if len(r) < 2:
        return None
    return float(r.std(ddof=1) * math.sqrt(TRADING_DAYS)) * 100


def parkinson(highs, lows, n):
    h = np.asarray(highs[-n:], dtype=float)
    l = np.asarray(lows[-n:], dtype=float)
    hl = np.log(h / l) ** 2
    return float(math.sqrt(hl.mean() / (4 * math.log(2)) * TRADING_DAYS)) * 100


def pick(quotes, target, cls):
    cand = [q for q in quotes if q.option_class == cls and q.strike_price]
    return min(cand, key=lambda q: abs(q.strike_price - target)) if cand else None


def run_once(args):
    user, pwd = load_auth()
    api = TqApi(auth=TqAuth(user, pwd), web_gui=False)
    try:
        fut = api.get_quote(args.underlying)
        opts = api.query_options(args.underlying, expired=False)
        ql = api.get_quote_list(opts)
        kl = api.get_kline_serial(args.underlying, 86400, data_length=45)
        info = api.query_symbol_info(opts[:1]).iloc[0]

        deadline = time.time() + 20
        while time.time() < deadline:
            api.wait_update(deadline=time.time() + 3)
            if fut.last_price and all(q.datetime for q in ql) and len(kl):
                break

        spot = fut.last_price or fut.pre_close
        expire = float(info["expire_datetime"])
        dte = (expire - time.time()) / 86400.0
        t_years = max(dte, 0.0) / 365.0

        closes = [c for c in kl["close"].tolist() if c > 0]
        if len(closes) < 21:
            print("日K不足 21 根，无法算 20 日 RV", flush=True)
            return
        rv5 = realized_vol(closes, 5)
        rv10 = realized_vol(closes, 10)
        rv20 = realized_vol(closes, 20)
        park = parkinson(kl["high"].tolist()[-20:], kl["low"].tolist()[-20:], 20)

        strikes = sorted({q.strike_price for q in ql if q.strike_price})
        atm = min(strikes, key=lambda s: abs(s - spot))
        atm_c = next((q for q in ql if q.strike_price == atm and q.option_class == "CALL"), None)
        atm_p = next((q for q in ql if q.strike_price == atm and q.option_class == "PUT"), None)
        call_wing = pick(ql, spot * 1.07, "CALL")
        put_wing = pick(ql, spot * 0.93, "PUT")

        def iv_of(q):
            if q is None:
                return None
            price = q.last_price or q.pre_close
            return implied_vol(spot, q.strike_price, t_years, price, q.option_class == "CALL")

        iv_c, iv_p = iv_of(atm_c), iv_of(atm_p)
        ivs = [v for v in (iv_c, iv_p) if v]
        iv_atm = sum(ivs) / len(ivs) * 100 if ivs else None
        iv_call_w, iv_put_w = iv_of(call_wing), iv_of(put_wing)
        wings = [v * 100 for v in (iv_call_w, iv_put_w) if v]
        iv_wing = sum(wings) / len(wings) if wings else None

        straddle = ((atm_c.last_price or atm_c.pre_close) + (atm_p.last_price or atm_p.pre_close)) \
            if (atm_c and atm_p) else None
        vrp = (iv_atm - rv20) if iv_atm else None
        wing_vrp = (iv_wing - rv20) if iv_wing else None

        if iv_atm is None:
            verdict = "数据不足：平值 IV 无法计算"
        elif dte < MIN_DAYS:
            verdict = "临近到期：不新开卖方仓位，只做平仓/移仓"
        elif iv_atm >= IV_RICH and rv20 < 45:
            verdict = "一档：IV 够贵，可半仓铁鹰（卖翼 + 买更外翼）"
        elif iv_atm >= IV_FAIR and vrp >= VRP_MIN:
            verdict = "二档：有溢价，只小仓卖翼，等回踩再挂；不平值裸卖"
        elif iv_atm >= IV_CHEAP:
            verdict = "三档：edge 不够付手续费与对冲成本，不做"
        else:
            verdict = "四档：IV 偏低，卖方不参与，考虑买方"

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        print("=" * 56, flush=True)
        print(f"[{now}] {args.underlying} 收盘波动率评估", flush=True)
        print(f"标的 {spot:.1f}  到期 {time.strftime('%Y-%m-%d %H:%M', time.localtime(expire))}  剩 {dte:.1f} 天", flush=True)
        print(f"RV5 {rv5:.2f}%  RV10 {rv10:.2f}%  RV20 {rv20:.2f}%  Parkinson20 {park:.2f}%", flush=True)
        if iv_atm:
            print(f"平值 {atm}  跨式 {straddle:.1f} 点  IV(C) {iv_c*100:.2f}%  IV(P) {iv_p*100:.2f}%  IV(均) {iv_atm:.2f}%", flush=True)
        if iv_wing:
            c_txt = f"{call_wing.strike_price}C {iv_call_w*100:.2f}%" if iv_call_w else "认购翼缺失"
            p_txt = f"{put_wing.strike_price}P {iv_put_w*100:.2f}%" if iv_put_w else "认沽翼缺失"
            print(f"翼部 IV: {c_txt}  {p_txt}  均值 {iv_wing:.2f}%", flush=True)
        if vrp is not None:
            print(f"VRP: 平值 {vrp:+.2f} 点  |  翼部 {wing_vrp:+.2f} 点", flush=True)
        print(f"判定: {verdict}", flush=True)
        if wing_vrp is not None and wing_vrp < WING_MIN:
            print(f"提示: 翼部溢价不足 {WING_MIN:.0f} 点，卖翼性价比低", flush=True)
        print("=" * 56, flush=True)

        if args.log:
            new = not os.path.exists(args.log)
            with open(args.log, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time", "symbol", "spot", "dte", "rv5", "rv10", "rv20",
                                "parkinson20", "iv_atm", "iv_call_wing", "iv_put_wing",
                                "vrp", "wing_vrp", "straddle", "verdict"])
                w.writerow([now, args.underlying, round(spot, 1), round(dte, 1),
                            round(rv5, 2), round(rv10, 2), round(rv20, 2), round(park, 2),
                            round(iv_atm, 2) if iv_atm else "",
                            round(iv_call_w * 100, 2) if iv_call_w else "",
                            round(iv_put_w * 100, 2) if iv_put_w else "",
                            round(vrp, 2) if vrp is not None else "",
                            round(wing_vrp, 2) if wing_vrp is not None else "",
                            round(straddle, 1) if straddle else "", verdict])
            print(f"已写入 {args.log}", flush=True)
    finally:
        api.close()


def next_close_epoch():
    now = datetime.datetime.now()
    target = now.replace(hour=15, minute=5, second=0, microsecond=0)
    if now >= target or now.weekday() >= 5:
        target += datetime.timedelta(days=1)
    while target.weekday() >= 5:
        target += datetime.timedelta(days=1)
    return time.mktime(target.timetuple())


def main():
    ap = argparse.ArgumentParser(description="MA612 期权每日收盘 IV/RV 监控")
    ap.add_argument("--underlying", default="CZCE.MA612")
    ap.add_argument("--log", default="/workspace/ma612_vol_log.csv")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--daemon", action="store_true", help="每个交易日 15:05 自动运行")
    args = ap.parse_args()
    if args.no_log:
        args.log = None

    if not args.daemon:
        run_once(args)
        return

    print("守护模式：每个交易日 15:05 运行，Ctrl+C 退出", flush=True)
    while True:
        wait = max(0, next_close_epoch() - time.time())
        print(f"下次运行：{datetime.datetime.fromtimestamp(time.time() + wait):%Y-%m-%d %H:%M}", flush=True)
        time.sleep(wait)
        try:
            run_once(args)
        except Exception as exc:  # 单日失败不终止守护
            print(f"本次运行失败：{type(exc).__name__}: {exc}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
