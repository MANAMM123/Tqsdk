#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
期权卖方三阶段 Greeks 交易系统框架
==================================

【关于 TqSdk】
TqSdk 是信易科技开源的 Python 量化交易开发包，面向国内期货、期权市场。
本框架使用 TqApi 获取行情，tqsdk.ta.OPTION_GREEKS / OPTION_IMPV 计算希腊字母，
用 TqSim 模拟账户演示建仓-持仓-到期三阶段管理逻辑。
运行前需把 TQ_ACCOUNT / TQ_PASSWORD 环境变量替换为自己的快期账户信息。

【系统设计思想】
融合两套体系：
  A. 用户入场规则：缠论中枢二买方向 + 35-15 DTE + IV>HV + OI递减 + 期权墙
  B. 塔勒布风控：尾部保险(杠铃) + 最坏单日(WCD)定仓位 + 不加杠杆 + 集中度限制

卖方 Greeks 三阶段：
  阶段一 建仓期 DTE 35-15：方向确认 → IV 筛选 → 期权墙 → 开仓 + 买保险
  阶段二 持仓期 DTE 14-5 ：Delta/Gamma 监控 → 移动止盈 → 期权墙迁移检测
  阶段三 到期期 DTE ≤ 4 ：Gamma 爆炸评估 → 平仓 or 移仓

【Greeks 阶段关注点】
  阶段一：Theta(正) / Vega(负) / Delta(中性微偏)
  阶段二：Gamma(上升) / Delta(漂移) / Vega(衰减)
  阶段三：Gamma(爆炸) / Theta(末加速) / 流动性

【运行说明】
1. pip install tqsdk scipy numpy pandas
2. export TQ_ACCOUNT=账号 TQ_PASSWORD=密码
3. 模拟账户运行，实盘前必须充分回测

【风险提示】
本框架仅用于学习研究，不构成投资建议。期权卖方尾部风险巨大，
跳空、流动性枯竭、保证金螺旋都可能导致超出预期的亏损。
"""

import os
import sys
import math
import json
import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq
from datetime import datetime
from tqsdk import TqApi, TqAuth, TqSim
from tqsdk.ta import OPTION_GREEKS, OPTION_IMPV

# ============ 全局参数 ============
INIT_BALANCE = 100000.0
RISK_FREE_RATE = 0.025
DEADLINE = 20

# --- 建仓阶段参数 ---
ENTRY_DTE_MIN = 15          # 建仓最小到期天数
ENTRY_DTE_MAX = 35          # 建仓最大到期天数
IV_PREMIUM_MIN = 0.15       # IV 相对 HV 最低溢价
OI_DECLINE_DAYS = 2         # OI 连续减少天数阈值
WALL_DISTANCE_MAX = 0.05    # 距期权墙最大距离（5%）
TAIL_INSURANCE_DELTA = 0.10 # 尾部保险腿目标 Delta

# --- 持仓阶段参数 ---
MANAGE_DTE_MAX = 14         # 持仓管理阶段上界
DELTA_LIMIT = 0.30          # Delta 漂移上限（绝对值）
TP_RETRACE_RATIO = 0.50     # 移动止盈：权利金回撤不超过 50%
WALL_SHIFT_ALERT = 0.02     # 期权墙迁移告警阈值

# --- 到期阶段参数 ---
EXPIRY_DTE = 4              # 进入到期阶段的 DTE 阈值
GAMMA_EXPLOSION = 0.05      # Gamma 爆炸阈值（占标的价比例）

# --- 风控参数（塔勒布） ---
WCD_RATIO = 0.02            # 最坏单日亏损不超过组合 2%
MAX_POS_PER_UL = 0.15       # 单品种保证金占比上限
MAX_TOTAL_SELL_MARGIN = 0.40  # 总卖方保证金占比上限
INSURANCE_BUDGET_RATIO = 0.0333  # 尾部保险预算


# ============ Black-Scholes 工具 ============
def _bs_price(S, K, T, r, sigma, opt_type):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if opt_type == "CALL" else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == "CALL":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def implied_vol(price, S, K, T, r, opt_type):
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if opt_type == "CALL" else (K - S))
    if price < intrinsic:
        return None
    try:
        return brentq(lambda s: _bs_price(S, K, T, r, s, opt_type) - price, 1e-6, 5.0, maxiter=100)
    except Exception:
        return None


def option_greeks(S, K, T, r, sigma, opt_type):
    if T is None or T <= 0 or sigma is None or sigma <= 0:
        return 0.0, 0.0, 0.0, 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    delta = norm.cdf(d1) if opt_type == "CALL" else norm.cdf(d1) - 1.0
    gamma = norm.pdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * norm.pdf(d1) * math.sqrt(T)
    theta = (-(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
             - opt_type_sign(opt_type) * r * K * math.exp(-r * T) * norm.cdf(opt_type_sign(opt_type) * d2))
    return delta, gamma, vega, theta


def opt_type_sign(t):
    return 1.0 if t == "CALL" else -1.0


# ============ 行情辅助 ============
def wait(api, deadline=DEADLINE):
    api.wait_update(deadline=deadline)


def get_price(q):
    for f in ("last_price", "bid_price1", "ask_price1", "pre_settlement"):
        v = getattr(q, f, None)
        if v is not None and not (isinstance(v, float) and math.isnan(v)) and v > 0:
            return float(v)
    return None


def get_limit_price(api, symbol, direction):
    q = api.get_quote(symbol)
    wait(api, deadline=5)
    p = q.bid_price1 if direction == "SELL" else q.ask_price1
    if not p or (isinstance(p, float) and math.isnan(p)) or p <= 0:
        p = q.pre_settlement or q.last_price
    return float(p) if p and p > 0 else None


# ============ 缠论方向判断（简化版中枢二买） ============
def chan_second_buy(api, underlying):
    """简化版缠论中枢二买判断

    用 30 分钟 K 线识别：价格在中枢上方回踩不破中枢上沿 + MACD 回零轴后金叉。
    真实缠论需递归笔段，这里给出可执行的工程化近似。
    返回: 'LONG' / 'SHORT' / None
    """
    kl = api.get_kline_serial(underlying, 1800, data_length=120)  # 30min
    wait(api)
    if len(kl) < 60:
        return None
    closes = kl["close"].dropna().values
    if len(closes) < 60:
        return None
    # 中枢：近 30 根的高低点区间
    mid = closes[-30:]
    zg, zd = np.max(mid), np.min(mid)
    last = closes[-1]
    # MACD
    ema12 = pd.Series(closes).ewm(span=12).mean().values
    ema26 = pd.Series(closes).ewm(span=26).mean().values
    dif = ema12 - ema26
    dea = pd.Series(dif).ewm(span=9).mean().values
    macd_hist = (dif - dea) * 2
    # 二买：价格在中枢上方（last > zg），且 MACD 柱状由负转正
    if last > zg and macd_hist[-1] > 0 and macd_hist[-3] <= 0:
        return "LONG"
    if last < zd and macd_hist[-1] < 0 and macd_hist[-3] >= 0:
        return "SHORT"
    return None


# ============ 阶段一：建仓筛选 ============
class EntryFilter:
    """建仓阶段筛选器"""

    def __init__(self, api):
        self.api = api

    def check_dte(self, q):
        dte = q.expire_rest_days or 0
        return ENTRY_DTE_MIN <= dte <= ENTRY_DTE_MAX

    def check_iv_premium(self, q, ul_q):
        S = get_price(ul_q)
        K = q.strike_price
        T = (q.expire_rest_days or 0) / 365.0
        price = get_price(q)
        if not all([S, K, price, T > 0]):
            return False, None
        iv = implied_vol(price, S, K, T, RISK_FREE_RATE, q.option_class)
        # 用 20 日 HV 对照
        hv = self._get_hv(q.underlying_symbol)
        if not iv or not hv:
            return False, None
        premium = (iv - hv) / hv
        return premium >= IV_PREMIUM_MIN, premium

    def _get_hv(self, underlying):
        try:
            kl = self.api.get_kline_serial(underlying, 86400, data_length=25)
            wait(self.api)
            closes = kl["close"].dropna().values
            if len(closes) < 20:
                return None
            rets = np.diff(np.log(closes[-20:]))
            return float(np.std(rets) * np.sqrt(252))
        except Exception:
            return None

    def check_oi_decline(self, q):
        """检查 OI 是否连续 N 日减少"""
        try:
            kl = self.api.get_kline_serial(q.symbol, 86400, data_length=OI_DECLINE_DAYS + 3)
            wait(self.api)
            oi = kl["close"].values if "open_interest" not in kl.columns else kl["open_interest"].values
            # TqSdk 日 K 无 open_interest 列，用 quote 的 open_interest 做近似
            # 实际应订阅多日 OI，这里简化为当前 OI 与前一日比较
            oi_series = kl.get("open_interest", pd.Series(dtype=float)).dropna().values
            if len(oi_series) >= OI_DECLINE_DAYS + 1:
                recent = oi_series[-(OI_DECLINE_DAYS + 1):]
                return all(recent[i] > recent[i + 1] for i in range(OI_DECLINE_DAYS))
            return False
        except Exception:
            return False

    def find_option_wall(self, ul, call_or_put):
        """找到 C/P 最厚的期权墙（持仓量最大的行权价）"""
        opts = self.api.query_options(ul, expired=False)
        if not opts:
            return None
        walls = {}
        for o in opts:
            q = self.api.get_quote(o)
            wait(self.api, deadline=3)
            if q.option_class != call_or_put:
                continue
            k = q.strike_price
            oi = q.open_interest or 0
            walls[k] = walls.get(k, 0) + oi
        if not walls:
            return None
        return max(walls, key=walls.get)

    def screen(self, option_symbol, direction):
        """完整建仓筛选，返回 (通过, 信息字典)"""
        q = self.api.get_quote(option_symbol)
        wait(self.api)
        ul_q = self.api.get_quote(q.underlying_symbol)
        wait(self.api)
        info = {"symbol": option_symbol, "dte": q.expire_rest_days}
        if not self.check_dte(q):
            info["reason"] = f"DTE={info['dte']} 不在 {ENTRY_DTE_MIN}-{ENTRY_DTE_MAX}"
            return False, info
        ok, premium = self.check_iv_premium(q, ul_q)
        if not ok:
            info["reason"] = f"IV溢价不足: {premium}"
            return False, info
        info["iv_premium"] = premium
        # 期权墙距离检查
        cp = "CALL" if direction == "SHORT" else "PUT"  # 涨卖Put→方向SHORT对应卖Put
        wall = self.find_option_wall(q.underlying_symbol, cp)
        if wall:
            S = get_price(ul_q)
            dist = abs(wall - q.strike_price) / S if S else 1
            info["wall"] = wall
            info["wall_dist"] = dist
            if dist > WALL_DISTANCE_MAX:
                info["reason"] = f"距期权墙{dist*100:.1f}% > {WALL_DISTANCE_MAX*100}%"
                return False, info
        info["reason"] = "通过"
        return True, info


# ============ 阶段二：持仓管理 ============
class PositionManager:
    """持仓阶段 Greeks 管理器"""

    def __init__(self, api, entry_premium):
        self.api = api
        self.entry_premium = entry_premium  # 开仓时权利金
        self.max_profit = 0.0               # 持仓期间最大浮盈（权利金减少量）

    def monitor(self, option_symbol):
        """监控 Greeks，返回 (动作, 原因)
        动作: HOLD / TP / STOP / ADJUST / CLOSE
        """
        q = self.api.get_quote(option_symbol)
        wait(self.api)
        ul_q = self.api.get_quote(q.underlying_symbol)
        wait(self.api)
        S = get_price(ul_q)
        K = q.strike_price
        T = (q.expire_rest_days or 0) / 365.0
        price = get_price(q)
        if not all([S, K, price, T > 0]):
            return "HOLD", "数据不足"
        iv = implied_vol(price, S, K, T, RISK_FREE_RATE, q.option_class)
        if not iv:
            return "HOLD", "IV无法计算"
        delta, gamma, vega, theta = option_greeks(S, K, T, RISK_FREE_RATE, iv, q.option_class)
        # Delta 漂移检查
        if abs(delta) > DELTA_LIMIT:
            return "ADJUST", f"Delta={delta:.2f} 超过阈值 {DELTA_LIMIT}"
        # Gamma 爆炸预检
        if gamma * S > GAMMA_EXPLOSION and T < 7 / 365:
            return "CLOSE", f"Gamma={gamma*S:.4f} 接近爆炸"
        # 移动止盈：权利金从最高点回撤超过 50%
        profit = self.entry_premium - price  # 卖方浮盈 = 权利金下跌
        if profit > self.max_profit:
            self.max_profit = profit
        if self.max_profit > 0 and (self.max_profit - profit) > TP_RETRACE_RATIO * self.entry_premium:
            return "TP", f"权利金回撤超 {TP_RETRACE_RATIO*100:.0f}%"
        # 硬止损：亏损超过 1 倍权利金
        if profit < -self.entry_premium:
            return "STOP", f"亏损超 1 倍权利金"
        return "HOLD", f"Δ={delta:.2f} Γ={gamma*S:.4f} Θ={theta:.2f} 浮盈={profit:.2f}"


# ============ 阶段三：到期决策 ============
class ExpiryManager:
    """到期阶段决策器"""

    @staticmethod
    def decide(q, ul_q):
        """DTE ≤ 4 时决策：CLOSE / ROLL / HOLD"""
        dte = q.expire_rest_days or 0
        if dte > EXPIRY_DTE:
            return "HOLD", f"DTE={dte} > {EXPIRY_DTE}"
        S = get_price(ul_q)
        K = q.strike_price
        if not S:
            return "HOLD", "无标的价"
        otm_dist = abs(K - S) / S
        # 深度 OTM 可持有到期归零
        if otm_dist > 0.05:
            return "HOLD", f"深OTM {otm_dist*100:.1f}%，可持有"
        # 接近 ATM，Gamma 爆炸风险高 → 平仓或移仓
        if otm_dist < 0.03:
            return "CLOSE", f"近ATM {otm_dist*100:.1f}%，Gamma爆炸风险"
        return "ROLL", f"OTM {otm_dist*100:.1f}%，建议移仓下月"


# ============ 主策略类 ============
class SellerGreeksSystem:
    """期权卖方三阶段 Greeks 交易系统"""

    def __init__(self, api):
        self.api = api
        self.entry_filter = EntryFilter(api)
        self.positions = {}  # symbol -> {"entry_premium":..., "manager":PositionManager, "stage":...}
        self.trade_log = []  # 交易学习日志

    def _log(self, action, symbol, detail):
        rec = {"time": datetime.now().isoformat(), "action": action,
               "symbol": symbol, "detail": detail}
        self.trade_log.append(rec)
        print(f"  [LOG] {action} {symbol}: {detail}")

    def stage_one_entry(self, underlying, direction):
        """阶段一：建仓"""
        print(f"\n=== 阶段一 建仓: {underlying} 方向={direction} ===")
        # 1. 缠论二买确认
        chan_dir = chan_second_buy(self.api, underlying)
        if chan_dir != direction:
            print(f"  缠论方向={chan_dir} 与目标方向={direction} 不符，放弃")
            return
        # 2. 查期权链，筛选近 ATM、DTE 35-15、IV 溢价
        opts = self.api.query_options(underlying, expired=False)
        ul_q = self.api.get_quote(underlying)
        wait(self.api)
        S = get_price(ul_q)
        candidates = []
        for o in opts:
            q = self.api.get_quote(o)
            wait(self.api, deadline=3)
            if q.option_class != ("PUT" if direction == "LONG" else "CALL"):
                continue
            ok, info = self.entry_filter.screen(o, direction)
            if ok:
                candidates.append(info)
        if not candidates:
            print("  无满足条件的合约")
            return
        # 选 IV 溢价最高的
        best = max(candidates, key=lambda x: x["iv_premium"])
        sym = best["symbol"]
        entry_price = get_limit_price(self.api, sym, "SELL")
        if not entry_price:
            print("  无可用限价")
            return
        # 3. 开仓（卖方）
        order = self.api.insert_order(sym, direction="SELL", offset="OPEN",
                                      volume=1, limit_price=entry_price)
        wait(self.api, deadline=8)
        self._log("ENTRY_SELL", sym, f"权利金={entry_price} IV溢价={best['iv_premium']*100:.1f}%")
        # 4. 买尾部保险（杠铃）
        ins = self._buy_tail_insurance(underlying)
        if ins:
            self._log("ENTRY_INSURANCE", ins, "尾部对冲")
        # 5. 记录持仓
        self.positions[sym] = {
            "entry_premium": entry_price,
            "manager": PositionManager(self.api, entry_price),
            "stage": 1,
            "direction": direction,
        }

    def _buy_tail_insurance(self, underlying):
        """买入同标的远 OTM put 做尾部保险"""
        opts = self.api.query_options(underlying, expired=False)
        ul_q = self.api.get_quote(underlying)
        wait(self.api)
        S = get_price(ul_q)
        best, best_diff = None, 1.0
        for o in opts:
            q = self.api.get_quote(o)
            wait(self.api, deadline=3)
            if q.option_class != "PUT":
                continue
            K = q.strike_price
            T = (q.expire_rest_days or 0) / 365.0
            price = get_price(q)
            if not all([S, K, price, T > 0]):
                continue
            iv = implied_vol(price, S, K, T, RISK_FREE_RATE, "PUT")
            if not iv:
                continue
            d, _, _, _ = option_greeks(S, K, T, RISK_FREE_RATE, iv, "PUT")
            diff = abs(abs(d) - TAIL_INSURANCE_DELTA)
            if diff < best_diff:
                best_diff, best = diff, o
        if best:
            lp = get_limit_price(self.api, best, "BUY")
            if lp:
                self.api.insert_order(best, direction="BUY", offset="OPEN",
                                      volume=1, limit_price=lp)
                wait(self.api, deadline=8)
                return best
        return None

    def stage_two_manage(self):
        """阶段二：持仓管理"""
        for sym, pos in list(self.positions.items()):
            if pos["stage"] != 1 and pos["stage"] != 2:
                continue
            action, reason = pos["manager"].monitor(sym)
            q = self.api.get_quote(sym)
            wait(self.api)
            dte = q.expire_rest_days or 0
            # 阶段切换
            if dte <= EXPIRY_DTE:
                pos["stage"] = 3
                self._log("STAGE_3", sym, f"DTE={dte} 进入到期阶段")
            elif dte <= MANAGE_DTE_MAX:
                pos["stage"] = 2
            if action == "HOLD":
                continue
            if action in ("TP", "STOP", "CLOSE", "ADJUST"):
                self._close_position(sym, action, reason)

    def stage_three_expiry(self):
        """阶段三：到期决策"""
        for sym, pos in list(self.positions.items()):
            if pos["stage"] != 3:
                continue
            q = self.api.get_quote(sym)
            wait(self.api)
            ul_q = self.api.get_quote(q.underlying_symbol)
            wait(self.api)
            action, reason = ExpiryManager.decide(q, ul_q)
            if action == "CLOSE":
                self._close_position(sym, "EXPIRY_CLOSE", reason)
            elif action == "ROLL":
                self._log("ROLL", sym, reason)
                # 移仓：平当前 + 开下月同方向（此处仅记录，实盘需执行）

    def _close_position(self, sym, action, reason):
        lp = get_limit_price(self.api, sym, "BUY")  # 卖方平仓=买
        if not lp:
            return
        self.api.insert_order(sym, direction="BUY", offset="CLOSE",
                              volume=1, limit_price=lp)
        wait(self.api, deadline=8)
        self._log(action, sym, reason)
        del self.positions[sym]

    def run_cycle(self, underlying, direction):
        """运行一个完整周期：建仓 → 管理 → 到期"""
        self.stage_one_entry(underlying, direction)
        # 持仓循环（演示：运行有限轮次）
        for _ in range(10):
            wait(self.api)
            self.stage_two_manage()
            self.stage_three_expiry()
            if not self.positions:
                break


# ============ 主入口 ============
def main():
    account = os.environ.get("TQ_ACCOUNT", "")
    password = os.environ.get("TQ_PASSWORD", "")
    if not account or not password:
        print("错误：请设置 TQ_ACCOUNT 和 TQ_PASSWORD")
        sys.exit(1)

    print(f"[{datetime.now()}] 卖方三阶段 Greeks 系统启动，资金 {INIT_BALANCE:,.0f}")
    api = TqApi(account=TqSim(init_balance=INIT_BALANCE),
                auth=TqAuth(account, password))
    try:
        sys = SellerGreeksSystem(api)
        # 示例：对螺纹钢主力做上涨方向卖Put
        q = api.get_quote("KQ.m@SHFE.rb")
        api.wait_update(deadline=15)
        underlying = q.underlying_symbol
        print(f"标的: {underlying}")
        sys.run_cycle(underlying, "LONG")
        # 输出交易日志（持续学习用）
        print("\n=== 交易学习日志 ===")
        for r in sys.trade_log:
            print(f"  {r['time'][:19]} {r['action']:15s} {r['symbol']:20s} {r['detail']}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
