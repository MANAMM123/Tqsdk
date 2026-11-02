"""期权波动率与 GEX 计算核心。

- 定价模型：Black-76（以标的期货/远期价格为 Forward，默认 r=0）
- 隐含波动率：对中间价做二分法反解
- HV：标的日线对数收益率年化标准差（20 / 60 日）
- 偏度：25Delta 风险逆转（Put IV - Call IV）
- GEX：Gamma × 持仓量 × 合约乘数 × F² × 1%（Call 为正、Put 为负）
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

SQRT2 = math.sqrt(2.0)
SQRT_2PI = math.sqrt(2.0 * math.pi)
NAN = float("nan")


def _ok(*values) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / SQRT2)


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def black76_price(F: float, K: float, T: float, sigma: float, option_class: str, r: float = 0.0) -> float:
    """Black-76 理论价（商品期权 / 以期货为标的的期权）。"""
    if not _ok(F, K, T, sigma) or F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return NAN
    df = math.exp(-r * T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_class.upper().startswith("C"):
        return df * (F * norm_cdf(d1) - K * norm_cdf(d2))
    return df * (K * norm_cdf(-d2) - F * norm_cdf(-d1))


def black76_greeks(F: float, K: float, T: float, sigma: float, option_class: str, r: float = 0.0):
    """返回 (delta, gamma, vega)，vega 为每 1 个波动率点的价格变化。"""
    if not _ok(F, K, T, sigma) or F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return NAN, NAN, NAN
    df = math.exp(-r * T)
    sqrt_t = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
    pdf = norm_pdf(d1)
    gamma = df * pdf / (F * sigma * sqrt_t)
    vega = df * F * pdf * sqrt_t / 100.0
    if option_class.upper().startswith("C"):
        delta = df * norm_cdf(d1)
    else:
        delta = -df * norm_cdf(-d1)
    return delta, gamma, vega


def implied_vol(price: float, F: float, K: float, T: float, option_class: str,
                r: float = 0.0, lo: float = 1e-4, hi: float = 5.0) -> float:
    """由期权价格反解隐含波动率；无解或价格低于内在价值时返回 nan。"""
    if not _ok(price, F, K, T) or price <= 0 or F <= 0 or K <= 0 or T <= 0:
        return NAN
    is_call = option_class.upper().startswith("C")
    intrinsic = max(F - K, 0.0) if is_call else max(K - F, 0.0)
    if price <= intrinsic + 1e-8:
        return NAN
    if black76_price(F, K, T, hi, option_class, r) < price:
        return NAN
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if black76_price(F, K, T, mid, option_class, r) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return (lo + hi) / 2.0


def hv_from_closes(closes, window: int = 20, trading_days: int = 252) -> float:
    """由日收盘价序列计算年化历史波动率。"""
    arr = np.asarray([float(c) for c in closes if _ok(c) and c > 0], dtype=float)
    if len(arr) < 4:
        return NAN
    if len(arr) > window + 1:
        arr = arr[-(window + 1):]
    rets = np.diff(np.log(arr))
    if len(rets) < 3:
        return NAN
    return float(np.std(rets, ddof=1) * math.sqrt(trading_days))


def compute_chain(df: pd.DataFrame, F: float, T: float, r: float = 0.0) -> pd.DataFrame:
    """为期权链补充 IV、Delta、Gamma、Vega、单合约 GEX。

    输入列：strike, option_class, price, open_interest, volume_multiple
    """
    out = df.copy()
    ivs, deltas, gammas, vegas, gex = [], [], [], [], []
    for row in out.itertuples(index=False):
        cp = str(row.option_class).upper()
        strike = float(row.strike)
        price = float(row.price)
        iv = implied_vol(price, F, strike, T, cp, r)
        delta, gamma, vega = black76_greeks(F, strike, T, iv, cp, r)
        oi = float(row.open_interest) if _ok(row.open_interest) else 0.0
        mult = float(row.volume_multiple) if _ok(row.volume_multiple) and row.volume_multiple > 0 else 1.0
        sign = 1.0 if cp.startswith("C") else -1.0
        contract_gex = gamma * oi * mult * F * F * 0.01 * sign if _ok(gamma) else NAN
        ivs.append(iv)
        deltas.append(delta)
        gammas.append(gamma)
        vegas.append(vega)
        gex.append(contract_gex)
    out["iv"] = ivs
    out["delta"] = deltas
    out["gamma"] = gammas
    out["vega"] = vegas
    out["gex"] = gex
    return out


def smile_by_strike(df: pd.DataFrame) -> pd.Series:
    """按行权价聚合 Call/Put 平均 IV（用于 ATM 与微笑插值）。"""
    valid = df[df["iv"].notna() & np.isfinite(df["iv"]) & (df["iv"] > 0)]
    if valid.empty:
        return pd.Series(dtype=float)
    return valid.groupby("strike")["iv"].mean().sort_index()


def atm_iv(df: pd.DataFrame, F: float) -> float:
    """平值隐含波动率（对行权价线性插值，落在区间外取端点）。"""
    curve = smile_by_strike(df)
    if curve.empty or not _ok(F):
        return NAN
    return float(np.interp(F, curve.index.values, curve.values))


def iv_at_delta(df: pd.DataFrame, target: float, side: str) -> float:
    """按 Delta 插值取 IV。side: CALL / PUT（Put 使用 |Delta|）。"""
    sub = df[df["iv"].notna() & np.isfinite(df["iv"])].copy()
    if sub.empty:
        return NAN
    if side.upper().startswith("C"):
        sub = sub[sub["delta"] > 0]
        x = sub["delta"].values
    else:
        sub = sub[sub["delta"] < 0]
        x = -sub["delta"].values
    if len(x) < 1:
        return NAN
    order = np.argsort(x)
    x = x[order]
    y = sub["iv"].values[order]
    return float(np.interp(target, x, y))


def skew_stats(df: pd.DataFrame, F: float) -> dict:
    """25Delta 偏度：Put25Δ IV - Call25Δ IV（正数代表看跌偏斜）。"""
    atm = atm_iv(df, F)
    c25 = iv_at_delta(df, 0.25, "CALL")
    p25 = iv_at_delta(df, 0.25, "PUT")
    skew = p25 - c25 if _ok(p25, c25) else NAN
    skew_pct = (skew / atm * 100.0) if _ok(skew, atm) and atm > 0 else NAN
    return {
        "atm_iv": atm,
        "iv_25d_call": c25,
        "iv_25d_put": p25,
        "skew_25d": skew,
        "skew_pct": skew_pct,
    }


def gex_table(df: pd.DataFrame) -> pd.DataFrame:
    """按行权价汇总 GEX，并给出占比、累计占比与累计净 GEX。"""
    valid = df[df["gex"].notna() & np.isfinite(df["gex"])]
    if valid.empty:
        return pd.DataFrame(columns=["strike", "call_gex", "put_gex", "net_gex",
                                     "share_pct", "cum_share_pct", "cum_net_gex", "oi"])
    rows = []
    for strike, g in valid.groupby("strike"):
        call_gex = float(g.loc[g["option_class"].str.upper().str.startswith("C"), "gex"].sum())
        put_gex = float(g.loc[g["option_class"].str.upper().str.startswith("P"), "gex"].sum())
        rows.append({
            "strike": float(strike),
            "call_gex": call_gex,
            "put_gex": put_gex,
            "net_gex": call_gex + put_gex,
            "oi": float(g["open_interest"].fillna(0).sum()),
        })
    table = pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)
    abs_net = table["net_gex"].abs()
    total_abs = abs_net.sum()
    table["share_pct"] = (abs_net / total_abs * 100.0).fillna(0) if total_abs > 0 else 0.0
    table["cum_share_pct"] = table["share_pct"].cumsum()
    table["cum_net_gex"] = table["net_gex"].cumsum()
    return table


def zero_gamma_level(gex_df: pd.DataFrame) -> float:
    """累计净 GEX 由负转正的零伽马价位（线性插值）。"""
    if gex_df.empty:
        return NAN
    strikes = gex_df["strike"].values
    cum = gex_df["cum_net_gex"].values
    for i in range(1, len(cum)):
        if (cum[i - 1] < 0 <= cum[i]) or (cum[i - 1] > 0 >= cum[i]):
            x0, x1, y0, y1 = strikes[i - 1], strikes[i], cum[i - 1], cum[i]
            if y1 == y0:
                return float(x1)
            return float(x0 + (0 - y0) * (x1 - x0) / (y1 - y0))
    return NAN


def summary_metrics(df: pd.DataFrame, F: float, hv20: float = NAN, hv60: float = NAN) -> dict:
    """单条期权链的汇总指标。"""
    sk = skew_stats(df, F)
    curve = smile_by_strike(df)
    gex_df = gex_table(df)
    calls = df[df["option_class"].str.upper().str.startswith("C")]
    puts = df[df["option_class"].str.upper().str.startswith("P")]
    call_oi = float(calls["open_interest"].fillna(0).sum())
    put_oi = float(puts["open_interest"].fillna(0).sum())
    net_gex = float(gex_df["net_gex"].sum()) if not gex_df.empty else NAN
    call_gex = float(gex_df["call_gex"].sum()) if not gex_df.empty else NAN
    put_gex = float(gex_df["put_gex"].sum()) if not gex_df.empty else NAN
    max_gex_strike = NAN
    call_wall = NAN
    put_wall = NAN
    if not gex_df.empty:
        max_gex_strike = float(gex_df.loc[gex_df["net_gex"].abs().idxmax(), "strike"])
        call_wall = float(gex_df.loc[gex_df["call_gex"].idxmax(), "strike"])
        put_wall = float(gex_df.loc[gex_df["put_gex"].idxmin(), "strike"])
    iv_pct = NAN
    if not curve.empty and _ok(sk["atm_iv"]):
        iv_pct = float((curve <= sk["atm_iv"]).mean() * 100.0)
    iv_hv_pct = ((sk["atm_iv"] - hv20) / hv20 * 100.0) if _ok(sk["atm_iv"], hv20) and hv20 > 0 else NAN
    return {
        **sk,
        "hv20": hv20,
        "hv60": hv60,
        "iv_hv_pct": iv_hv_pct,
        "iv_percentile": iv_pct,
        "net_gex": net_gex,
        "call_gex": call_gex,
        "put_gex": put_gex,
        "max_gex_strike": max_gex_strike,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "zero_gamma": zero_gamma_level(gex_df),
        "pcr_oi": (put_oi / call_oi * 100.0) if call_oi > 0 else NAN,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "iv_min": float(curve.min()) if not curve.empty else NAN,
        "iv_max": float(curve.max()) if not curve.empty else NAN,
        "contracts": int(len(df)),
    }


# ---------------------------------------------------------------- self test
if __name__ == "__main__":
    # 构造微笑：25Δ Put 比 25Δ Call 高 3 个波动率点，验证反解与偏度
    F, T, r = 100.0, 30 / 365, 0.0
    rows = []
    strikes = [80, 85, 90, 95, 100, 105, 110, 115, 120]
    for k in strikes:
        for cp in ("CALL", "PUT"):
            moneyness = math.log(F / k)
            iv = 0.20 + 0.05 * moneyness * moneyness * 10 + (0.03 if cp == "PUT" else 0.0) * max(moneyness, 0)
            iv = max(0.08, min(iv, 1.2))
            price = black76_price(F, k, T, iv, cp, r)
            rows.append({"strike": k, "option_class": cp, "price": price,
                         "open_interest": 1000 if cp == "CALL" else 800,
                         "volume_multiple": 10, "iv_true": iv})
    chain = compute_chain(pd.DataFrame(rows), F, T, r)
    print(chain[["strike", "option_class", "price", "iv", "iv_true", "delta", "gamma"]].to_string(index=False))
    print("\nATM IV:", round(atm_iv(chain, F), 4))
    print("skew:", {k: round(v, 4) if isinstance(v, float) else v for k, v in skew_stats(chain, F).items()})
    gex = gex_table(chain)
    print("\nGEX table:\n", gex.to_string(index=False))
    print("netgex:", summary_metrics(chain, F, 0.18, 0.17)["net_gex"])
