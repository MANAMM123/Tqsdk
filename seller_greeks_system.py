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

卖方 Greeks 三阶管理（按微分阶数划分，非 DTE 阶段）：
  一阶 Greeks（Delta/Vega/Theta）：方向、波动率、时间价值风险
  二阶 Greeks（Gamma/Vanna/Charm）：凸性加速度，超限需减仓/对冲
  三阶 Greeks（Speed/Zomma/Color）：Gamma 变化率，爆炸即硬退出

DTE 时间窗口（与三阶 Greeks 维度配合）：
  建仓 35-15 DTE ：缠论二买 + IV>HV + OI递减 + 期权墙 → 开仓 + 尾部保险
  持仓 15-10 DTE ：二阶 Gamma 上升，一阶 Delta/Vega 监控 + 移动止盈
  硬退 ≤10 DTE ：三阶 Greeks 爆炸，无论盈亏强制平仓

【Greeks 阶数关注点】
  一阶：Theta(正收租) / Vega(负，IV升则亏) / Delta(方向漂移)
  二阶：Gamma(卖方为负，凸性风险) / Vanna(Delta对IV敏感)
  三阶：Speed(Γ的价格变化率) / Color(Γ的时间变化率，临近到期爆炸)

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

# --- 希腊字母三阶（按微分阶数划分，非 DTE 阶段）---
# 一阶 Greeks: Delta, Vega, Theta  → 方向/波动率/时间风险
# 二阶 Greeks: Gamma, Vanna, Charm → 加速度（凸性风险）
# 三阶 Greeks: Speed, Zomma, Color → 加加速度（Gamma 变化率，临近到期爆炸）
#
# DTE 窗口是时间触发条件，与三阶 Greeks 维度配合：
#   建仓: 35-15 DTE（Gamma 尚小，以一阶管理为主）
#   持仓: 15-10 DTE（Gamma 上升，二阶管理介入）
#   硬退: ≤10 DTE 或 三阶 Greeks 爆炸（Gamma 及其变化率失控）

# --- 建仓参数（DTE 时间窗口）---
ENTRY_DTE_MIN = 15          # 建仓最小到期天数
ENTRY_DTE_MAX = 35          # 建仓最大到期天数
IV_PREMIUM_MIN = 0.15       # IV 相对 HV 最低溢价
OI_DECLINE_DAYS = 2         # OI 连续减少天数阈值
WALL_DISTANCE_MAX = 0.05    # 距期权墙最大距离（5%）
TAIL_INSURANCE_DELTA = 0.10 # 尾部保险腿目标 Delta（风控建议1）

# --- 一阶 Greeks 管理参数 ---
DELTA_LIMIT = 0.50          # 一阶：Delta 漂移上限（绝对值，卖 Put 近 ATM 约 -0.4）
VEGA_LIMIT = 5000.0         # 一阶：Vega 暴露上限（元/IV 1%变动），超此需对冲
TP_RETRACE_RATIO = 0.50     # 移动止盈：权利金回撤不超过 50%

# --- 二阶 Greeks 管理参数 ---
GAMMA_LIMIT = 0.05          # 二阶：Gamma 上限（Γ×S 归一化后），超此需减仓或对冲
VANNA_LIMIT = 10.0          # 二阶：Vanna 上限（Delta 对 IV 敏感度）

# --- 三阶 Greeks 管理参数（硬退出触发）---
HARD_EXIT_DTE = 10          # 时间触发：DTE ≤ 10 强制平仓
# 三阶爆炸用「相对建仓基线的倍数」判定，不用绝对值：
#   不同品种/行权价/到期日的 Speed、Color 量纲差异极大（相差几个数量级），
#   任何单一绝对阈值都会要么误杀全部仓位，要么永不触发。
SPEED_EXPLOSION_MULT = 3.0  # |Speed| > 建仓时 |Speed| 的 3 倍 → Gamma 价格变化率失控
COLOR_EXPLOSION_MULT = 3.0  # Color < 建仓时 Color 的 3 倍（更负）→ Gamma 时间衰减加速爆炸

# --- 风控参数（塔勒布三条建议）---
WCD_RATIO = 0.02            # 建议2：最坏单日亏损不超过组合 2%（据此定仓位）
MAX_POS_PER_UL = 0.15       # 建议3：单品种保证金占比上限 15%
MAX_TOTAL_SELL_MARGIN = 0.40  # 总卖方保证金占比上限 40%
INSURANCE_BUDGET_RATIO = 0.0333  # 尾部保险预算占净值比例


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
    """计算一阶 Greeks: delta / gamma / vega / theta"""
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


def option_greeks_full(S, K, T, r, sigma, opt_type):
    """计算一阶 + 二阶 + 三阶 Greeks（完整微分阶数）

    一阶 Greeks（价格/波动率/时间的一阶导数）：
      delta, vega, theta
    二阶 Greeks（一阶 Greeks 的变化率 = 加速度）：
      gamma  = ∂Delta/∂S = ∂²V/∂S²       凸性/加速度
      vanna  = ∂Delta/∂σ = ∂Vega/∂S       Delta对波动率的敏感度
      charm  = ∂Delta/∂t                    Delta随时间的衰减
      vomma  = ∂Vega/∂σ = ∂²V/∂σ²        Vega对波动率的敏感度
    三阶 Greeks（Gamma 的变化率 = 加加速度）：
      speed  = ∂Gamma/∂S = ∂³V/∂S³       Gamma随价格的变化
      zomma  = ∂Gamma/∂σ                  Gamma随波动率的变化
      color  = ∂Gamma/∂t                  Gamma随时间的变化（临近到期爆炸）

    返回: dict 包含全部 Greeks
    """
    if T is None or T <= 0 or sigma is None or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
                "vanna": 0.0, "charm": 0.0, "vomma": 0.0,
                "speed": 0.0, "zomma": 0.0, "color": 0.0}
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    phi = norm.pdf(d1)
    sign = opt_type_sign(opt_type)

    # 一阶
    delta = norm.cdf(d1) if opt_type == "CALL" else norm.cdf(d1) - 1.0
    vega = S * phi * sqrtT
    theta = (-(S * phi * sigma) / (2 * sqrtT)
             - sign * r * K * math.exp(-r * T) * norm.cdf(sign * d2))

    # 二阶
    gamma = phi / (S * sigma * sqrtT)
    vanna = -phi * d2 / sigma                       # ∂Delta/∂σ
    charm = -phi * (2 * r * T - d2 * sigma * sqrtT) / (2 * T * sigma * sqrtT)  # ∂Delta/∂t
    vomma = vega * d1 * d2 / sigma                  # ∂Vega/∂σ

    # 三阶
    speed = -gamma / S * (d1 / (sigma * sqrtT) + 1.0)        # ∂Gamma/∂S
    zomma = gamma * (d1 * d2 - 1.0) / sigma                   # ∂Gamma/∂σ
    color = -gamma * (r * d2 / (sigma * sqrtT) + (1.0 + d1 * d2) / (2 * T))  # ∂Gamma/∂t

    return {
        "delta": delta, "gamma": gamma, "vega": vega, "theta": theta,
        "vanna": vanna, "charm": charm, "vomma": vomma,
        "speed": speed, "zomma": zomma, "color": color,
    }


def opt_type_sign(t):
    return 1.0 if t == "CALL" else -1.0


def greeks_exploded(speed, color, base_speed, base_color,
                    speed_mult=SPEED_EXPLOSION_MULT,
                    color_mult=COLOR_EXPLOSION_MULT):
    """三阶 Greeks 是否相对建仓基线爆炸（量纲无关）

    Speed 为负/正都可能，取绝对值比较；Color 为负，越负代表 Gamma 随时间加速放大。
    基线缺失（None / 0 / NaN）时不判定为爆炸，避免无依据地误杀仓位。
    返回 (bool, 说明)
    """
    def _ok(v):
        return v is not None and not (isinstance(v, float) and math.isnan(v)) and abs(v) > 0

    reasons = []
    if _ok(speed) and _ok(base_speed):
        ratio = abs(speed) / abs(base_speed)
        if ratio > speed_mult:
            reasons.append(f"Speed|{speed:.3g}|={ratio:.2f}x基线(>{speed_mult}x)")
    if _ok(color) and _ok(base_color) and color < 0 and base_color < 0:
        ratio = color / base_color  # 两者为负，比值>1 表示更负
        if ratio > color_mult:
            reasons.append(f"Color {color:.3g}={ratio:.2f}x基线(>{color_mult}x)")
    return bool(reasons), "; ".join(reasons)


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


# ============ 缠论工程化：分型 → 笔 → 段 → 中枢 → 二买 ============
def _find_fractals(kl):
    """识别顶分型 / 底分型

    顶分型：中间K线高点 > 左右两根高点
    底分型：中间K线低点 < 左右两根低点
    返回: list of (index, type, price)  type in ('top','bottom')
    """
    highs = kl["high"].values
    lows = kl["low"].values
    fractals = []
    for i in range(1, len(kl) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            fractals.append((i, "top", float(highs[i])))
        elif lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            fractals.append((i, "bottom", float(lows[i])))
    return fractals


def _build_strokes(fractals):
    """由分型构建笔

    规则：
      1. 相邻分型类型必须相反（顶-底 或 底-顶）
      2. 两分型间至少有 1 根独立K线（index 差 ≥ 2）
      3. 同方向连续分型取极值合并
    返回: list of dict {start_idx, end_idx, type, high, low, start_price, end_price}
    """
    fr = list(fractals)
    if len(fr) < 2:
        return []
    strokes = []
    i = 0
    while i < len(fr) - 1:
        f1, f2 = fr[i], fr[i + 1]
        # 同类型合并：取极值
        if f1[1] == f2[1]:
            if f1[1] == "top":
                fr[i] = f2 if f2[2] > f1[2] else f1
            else:
                fr[i] = f2 if f2[2] < f1[2] else f1
            fr.pop(i + 1)
            continue
        # 独立K线检查
        if f2[0] - f1[0] < 2:
            fr.pop(i + 1)
            continue
        stroke_type = "up" if f1[1] == "bottom" else "down"
        strokes.append({
            "start_idx": f1[0], "end_idx": f2[0],
            "type": stroke_type,
            "high": max(f1[2], f2[2]),
            "low": min(f1[2], f2[2]),
            "start_price": f1[2],
            "end_price": f2[2],
        })
        i += 1
    return strokes


def _build_segments(strokes):
    """由笔构建段

    段至少 3 笔，且第 3 笔突破第 1 笔端点（确认趋势成立）。
    返回: list of dict {start_stroke, end_stroke, type, high, low}
    """
    if len(strokes) < 3:
        return []
    segments = []
    i = 0
    while i < len(strokes) - 2:
        s1, s2, s3 = strokes[i], strokes[i + 1], strokes[i + 2]
        seg_dir = s1["type"]
        if seg_dir == "up" and s3["end_price"] > s1["end_price"]:
            segments.append({
                "start_stroke": i, "end_stroke": i + 2, "type": "up",
                "high": max(s["high"] for s in strokes[i:i + 3]),
                "low": min(s["low"] for s in strokes[i:i + 3]),
            })
            i += 3
            continue
        if seg_dir == "down" and s3["end_price"] < s1["end_price"]:
            segments.append({
                "start_stroke": i, "end_stroke": i + 2, "type": "down",
                "high": max(s["high"] for s in strokes[i:i + 3]),
                "low": min(s["low"] for s in strokes[i:i + 3]),
            })
            i += 3
            continue
        i += 1
    return segments


def _find_zhongshu(segments):
    """识别中枢

    中枢 = 连续 3 个段的价格重叠区间：
      zg（上沿）= min(各段高点)
      zd（下沿）= max(各段低点)
    仅当 zg > zd 时有重叠，返回 (zg, zd)，否则 None。
    """
    if len(segments) < 3:
        return None
    segs = segments[-3:]
    zg = min(s["high"] for s in segs)
    zd = max(s["low"] for s in segs)
    return (zg, zd) if zg > zd else None


def _macd_zero_axis(closes):
    """MACD 零轴辅助确认

    缠论中 MACD 零轴对应中枢位置。
    返回: ('above_zero','below_zero','cross_up','cross_down')
    """
    ema12 = pd.Series(closes).ewm(span=12).mean().values
    ema26 = pd.Series(closes).ewm(span=26).mean().values
    dif = ema12 - ema26
    dea = pd.Series(dif).ewm(span=9).mean().values
    hist = (dif - dea) * 2
    if len(hist) < 3:
        return "unknown"
    if hist[-1] > 0 and hist[-3] <= 0:
        return "cross_up"
    if hist[-1] < 0 and hist[-3] >= 0:
        return "cross_down"
    return "above_zero" if dif[-1] > 0 else "below_zero"


def chan_second_buy(api, underlying):
    """工程化缠论中枢二买判断

    完整管线：30min K线 → 分型 → 笔 → 段 → 中枢 → 二买
    二买定义：
      上涨二买(LONG)：最后一段向上突破中枢上沿 zg 后，回踩不破 zg，
                       且 MACD 在零轴上方或金叉。
      下跌二卖(SHORT)：最后一段向下跌破中枢下沿 zd 后，反弹不破 zd，
                        且 MACD 在零轴下方或死叉。
    返回: 'LONG' / 'SHORT' / None
    """
    kl = api.get_kline_serial(underlying, 1800, data_length=200)  # 30min
    wait(api)
    if len(kl) < 100:
        return None
    closes = kl["close"].dropna().values
    if len(closes) < 100:
        return None
    # 1. 分型 → 笔 → 段 → 中枢
    fractals = _find_fractals(kl)
    if len(fractals) < 4:
        return None
    strokes = _build_strokes(fractals)
    if len(strokes) < 3:
        return None
    segments = _build_segments(strokes)
    if len(segments) < 3:
        return None
    zs = _find_zhongshu(segments)
    if zs is None:
        return None
    zg, zd = zs
    last_close = float(closes[-1])
    last_seg = segments[-1]
    # 2. MACD 零轴确认
    macd_state = _macd_zero_axis(closes)
    # 3. 二买判定
    # 上涨二买：最后段向上，段高点 > zg，当前价回踩在 zg 上方
    if (last_seg["type"] == "up" and last_seg["high"] > zg
            and last_close > zg
            and macd_state in ("above_zero", "cross_up")):
        return "LONG"
    # 下跌二卖：最后段向下，段低点 < zd，当前价反弹在 zd 下方
    if (last_seg["type"] == "down" and last_seg["low"] < zd
            and last_close < zd
            and macd_state in ("below_zero", "cross_down")):
        return "SHORT"
    return None


# ============ 建仓筛选（35-15 DTE） ============
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


# ============ 持仓 Greeks 管理（一阶→二阶→三阶） ============
class PositionManager:
    """持仓 Greeks 管理器（按微分阶数：一阶→二阶→三阶）"""

    def __init__(self, api, entry_premium, entry_speed=None, entry_color=None):
        self.api = api
        self.entry_premium = entry_premium  # 开仓时权利金
        self.max_profit = 0.0               # 持仓期间最大浮盈（权利金减少量）
        # 三阶 Greeks 建仓基线（爆炸判定以相对倍数比较，不用绝对阈值）
        self.base_speed = entry_speed
        self.base_color = entry_color

    def _set_baseline(self, speed, color):
        if self.base_speed is None and self.base_color is None:
            self.base_speed, self.base_color = speed, color
            return True
        return False

    def capture_baseline(self, option_symbol, S=None):
        """建仓时记录三阶 Greeks 基线（爆炸判定以相对倍数比较）"""
        q = self.api.get_quote(option_symbol)
        wait(self.api, deadline=5)
        if S is None:
            ul_q = self.api.get_quote(q.underlying_symbol)
            wait(self.api, deadline=5)
            S = get_price(ul_q)
        K = q.strike_price
        T = (q.expire_rest_days or 0) / 365.0
        price = get_price(q)
        if not all([S, K, price, T > 0]):
            return None
        iv = implied_vol(price, S, K, T, RISK_FREE_RATE, q.option_class)
        if not iv:
            return None
        g = option_greeks_full(S, K, T, RISK_FREE_RATE, iv, q.option_class)
        self.base_speed, self.base_color = g["speed"], g["color"]
        return self.base_speed, self.base_color

    def monitor(self, option_symbol):
        """按三阶 Greeks 维度监控，返回 (动作, 原因)

        动作: HOLD / TP / STOP / ADJUST / HARD_EXIT
        维度:
          一阶: Delta, Vega（方向/波动率风险）
          二阶: Gamma（凸性加速度，超限→减仓/对冲）
          三阶: Speed, Color（Gamma 变化率，爆炸→硬退出）
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
        g = option_greeks_full(S, K, T, RISK_FREE_RATE, iv, q.option_class)
        delta, gamma = g["delta"], g["gamma"]
        vega, speed, color = g["vega"], g["speed"], g["color"]
        gamma_norm = gamma * S  # 归一化 Gamma（消除价格量纲）

        # 三阶 Greeks：相对建仓基线爆炸 → 硬退出（无论 DTE）
        if self._set_baseline(speed, color):
            print(f"  [基线] {option_symbol} Speed={speed:.3g} Color={color:.3g} 已记录")
        else:
            exploded, why = greeks_exploded(speed, color, self.base_speed, self.base_color)
            if exploded:
                return "HARD_EXIT", f"三阶爆炸 {why}"

        # 二阶 Greeks：Gamma 超限 → 减仓或对冲
        if gamma_norm > GAMMA_LIMIT:
            return "ADJUST", f"二阶Γ×S={gamma_norm:.5f} 超阈值 {GAMMA_LIMIT}"

        # 一阶 Greeks：Delta 漂移 / Vega 暴露
        if abs(delta) > DELTA_LIMIT:
            return "ADJUST", f"一阶Δ={delta:.2f} 超阈值 {DELTA_LIMIT}"
        if abs(vega) > VEGA_LIMIT:
            return "ADJUST", f"一阶V={vega:.1f} 超阈值 {VEGA_LIMIT}"

        # 移动止盈：权利金从最高点回撤超过 50%
        profit = self.entry_premium - price  # 卖方浮盈 = 权利金下跌
        if profit > self.max_profit:
            self.max_profit = profit
        if self.max_profit > 0 and (self.max_profit - profit) > TP_RETRACE_RATIO * self.entry_premium:
            return "TP", f"权利金回撤超 {TP_RETRACE_RATIO*100:.0f}%"
        # 硬止损：亏损超过 1 倍权利金
        if profit < -self.entry_premium:
            return "STOP", f"亏损超 1 倍权利金"

        return ("HOLD",
                f"一阶Δ={delta:.2f} V={vega:.1f} | 二阶Γ×S={gamma_norm:.5f} | "
                f"三阶Spd={speed:.6f} Col={color:.6f} | 浮盈={profit:.2f}")


# ============ 硬退出管理器 ============
class HardExitManager:
    """硬退出触发条件（时间 + 三阶 Greeks 双维度）

    触发硬退出的两个独立条件（满足其一即平仓）：
      1. 时间触发：DTE ≤ HARD_EXIT_DTE
      2. Greeks 触发：三阶 Greeks 爆炸（Speed/Color 失控）
    """

    @staticmethod
    def decide(q, entry_premium, speed=None, color=None,
               base_speed=None, base_color=None):
        """双触发硬退出：DTE ≤ HARD_EXIT_DTE 或 三阶 Greeks 相对基线爆炸

        两个条件独立评估，满足其一即平仓；同时满足时原因会合并显示。
        返回 (动作, 原因)，动作: HARD_TP / HARD_STOP / HOLD
        """
        dte = q.expire_rest_days
        if dte is None or (isinstance(dte, float) and math.isnan(dte)):
            time_trigger = False          # 无到期天数数据时不误触发，交给 Greeks 条件
            dte = None
        else:
            time_trigger = dte <= HARD_EXIT_DTE

        greeks_trigger, why = greeks_exploded(
            speed, color, base_speed, base_color)

        if not (time_trigger or greeks_trigger):
            return "HOLD", (f"DTE={dte} Speed={speed} Color={color} "
                            f"未达硬退出条件（DTE>{HARD_EXIT_DTE} 且三阶未爆炸）")

        price = get_price(q)
        if price is None:
            return "HOLD", "已触发但无可用价格，无法平仓"

        src = []
        if time_trigger:
            src.append(f"DTE={dte}≤{HARD_EXIT_DTE}")
        if greeks_trigger:
            src.append(f"三阶爆炸({why})")
        profit = entry_premium - price  # 卖方浮盈
        reason = " + ".join(src)
        if profit >= 0:
            return "HARD_TP", f"{reason} → 硬止盈，浮盈={profit:.2f}"
        return "HARD_STOP", f"{reason} → 硬止损，浮亏={profit:.2f}"


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

    def _calc_wcd_volume(self, entry_price, volume_multiple):
        """建议2：按最坏单日亏损(WCD)定仓位

        卖方最坏单日亏损近似 = 权利金 × 2（跳空穿止损）× 乘数。
        使该亏损 ≤ 组合 WCD_RATIO，反推手数。
        若 1 手已超限，返回 0（跳过该笔交易）。
        """
        wcd_loss_per_lot = entry_price * 2 * volume_multiple  # 单腿最坏日亏
        max_allowed = INIT_BALANCE * WCD_RATIO
        if wcd_loss_per_lot > max_allowed:
            return 0  # 单笔风险超限，不开仓
        vol = int(max_allowed / wcd_loss_per_lot)
        return max(1, vol)

    def stage_one_entry(self, underlying, direction):
        """阶段一：建仓（35-15 DTE）"""
        print(f"\n=== 阶段一 建仓: {underlying} 方向={direction} ===")
        # 风控前置：集中度检查（建议3）
        acc = self.api.get_account()
        wait(self.api)
        if acc.margin / max(acc.balance, 1) > MAX_TOTAL_SELL_MARGIN:
            print(f"  总卖方保证金占比超限，不开新仓")
            return
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
        # 3. 按 WCD 定手数（建议2）
        oq = self.api.get_quote(sym)
        wait(self.api, deadline=5)
        vol_mult = oq.volume_multiple or 10
        volume = self._calc_wcd_volume(entry_price, vol_mult)
        if volume <= 0:
            print(f"  WCD超限：权利金={entry_price} 乘数={vol_mult} 单笔风险>{WCD_RATIO*100:.0f}%，跳过")
            return
        # 4. 开仓（卖方）
        order = self.api.insert_order(sym, direction="SELL", offset="OPEN",
                                      volume=volume, limit_price=entry_price)
        wait(self.api, deadline=8)
        self._log("ENTRY_SELL", sym,
                  f"手数={volume} 权利金={entry_price} IV溢价={best['iv_premium']*100:.1f}% WCD手数")
        # 5. 买尾部保险（杠铃，建议1）
        ins = self._buy_tail_insurance(underlying)
        if ins:
            self._log("ENTRY_INSURANCE", ins, "尾部对冲")
        # 6. 记录持仓
        manager = PositionManager(self.api, entry_price)
        self.positions[sym] = {
            "entry_premium": entry_price,
            "manager": manager,
            "stage": 1,
            "direction": direction,
            "volume": volume,
        }
        # 7. 记录三阶 Greeks 建仓基线（用于相对倍数爆炸判定）
        base = manager.capture_baseline(sym, S)
        if base:
            self._log("GREEKS_BASELINE", sym, f"Speed={base[0]:.3g} Color={base[1]:.3g}")

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
        """持仓管理：按三阶 Greeks 维度持续监控（不区分 DTE 阶段）"""
        for sym, pos in list(self.positions.items()):
            action, reason = pos["manager"].monitor(sym)
            q = self.api.get_quote(sym)
            wait(self.api)
            dte = q.expire_rest_days or 0
            # DTE 阶段标记（仅用于日志，风控以 Greeks 阶数为准）
            if dte <= HARD_EXIT_DTE:
                pos["stage"] = 3
            elif dte <= ENTRY_DTE_MIN:
                pos["stage"] = 2
            # 三阶 Greeks 爆炸 → 立即硬退出
            if action == "HARD_EXIT":
                self._close_position(sym, "HARD_STOP", reason)
                continue
            if action == "HOLD":
                continue
            if action in ("TP", "STOP", "ADJUST"):
                self._close_position(sym, action, reason)

    def stage_three_hard_exit(self):
        """硬退出检查：DTE ≤ 10 或 三阶 Greeks 爆炸"""
        for sym, pos in list(self.positions.items()):
            q = self.api.get_quote(sym)
            wait(self.api)
            ul_q = self.api.get_quote(q.underlying_symbol)
            wait(self.api)
            S = get_price(ul_q)
            K = q.strike_price
            T = (q.expire_rest_days or 0) / 365.0
            price = get_price(q)
            speed, color = None, None
            if all([S, K, price, T > 0]):
                iv = implied_vol(price, S, K, T, RISK_FREE_RATE, q.option_class)
                if iv:
                    g = option_greeks_full(S, K, T, RISK_FREE_RATE, iv, q.option_class)
                    speed, color = g["speed"], g["color"]
            action, reason = HardExitManager.decide(
                q, pos["entry_premium"], speed, color,
                pos["manager"].base_speed, pos["manager"].base_color)
            if action in ("HARD_TP", "HARD_STOP"):
                self._close_position(sym, action, reason)

    def _close_position(self, sym, action, reason):
        lp = get_limit_price(self.api, sym, "BUY")  # 卖方平仓=买
        if not lp:
            return
        vol = self.positions.get(sym, {}).get("volume", 1)
        self.api.insert_order(sym, direction="BUY", offset="CLOSE",
                              volume=vol, limit_price=lp)
        wait(self.api, deadline=8)
        self._log(action, sym, reason)
        del self.positions[sym]

    def run_cycle(self, underlying, direction):
        """运行一个完整周期：建仓(35-15DTE) → 管理(15-10DTE) → 硬止盈止损(≤10DTE)"""
        self.stage_one_entry(underlying, direction)
        # 持仓循环
        for _ in range(10):
            wait(self.api)
            self.stage_two_manage()
            self.stage_three_hard_exit()
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
