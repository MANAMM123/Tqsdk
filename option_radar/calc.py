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


def black76_vanna(F: float, K: float, T: float, sigma: float, option_class: str, r: float = 0.0) -> float:
    """Vanna = dDelta/dVol，表征 Delta 对波动率的敏感度（无量纲，百分比变动）。"""
    if not _ok(F, K, T, sigma) or F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return NAN
    df = math.exp(-r * T)
    sqrt_t = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    # dN'(d1)/dd1 = -d1 * N'(d1)，vega 对 F 求导得到 vanna 的常用形式
    vanna = -df * norm_pdf(d1) * d2 / sigma
    return vanna


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


def rolling_hv(closes, window: int, trading_days: int = 252) -> np.ndarray:
    """返回滚动 window 日年化 HV 序列（长度 len-1-window+1）。"""
    arr = np.asarray([float(c) for c in closes if _ok(c) and c > 0], dtype=float)
    if len(arr) < window + 2:
        return np.array([])
    rets = np.diff(np.log(arr))
    if len(rets) < window:
        return np.array([])
    out = []
    for i in range(window, len(rets) + 1):
        seg = rets[i - window:i]
        out.append(float(np.std(seg, ddof=1) * math.sqrt(trading_days)))
    return np.asarray(out)


def vol_cone(closes, windows=(5, 10, 20, 30, 60, 120), current_iv: float = NAN) -> dict:
    """波动率锥：各窗口 HV 的分位数分布，并定位当前 IV 在锥中的位置。"""
    rows = []
    for w in windows:
        ser = rolling_hv(closes, w)
        if len(ser) < 5:
            continue
        rows.append({
            "window": w,
            "mean": float(np.mean(ser)),
            "p10": float(np.percentile(ser, 10)),
            "p25": float(np.percentile(ser, 25)),
            "p50": float(np.percentile(ser, 50)),
            "p75": float(np.percentile(ser, 75)),
            "p90": float(np.percentile(ser, 90)),
            "current": float(ser[-1]) if len(ser) else NAN,
        })
    df = pd.DataFrame(rows)
    iv_rank = NAN
    if not df.empty and _ok(current_iv):
        # 把 IV 与各窗口 HV 合并看分位
        all_hv = np.concatenate([rolling_hv(closes, w) for w in windows])
        if len(all_hv) > 0:
            iv_rank = float((all_hv <= current_iv).mean() * 100.0)
    return {"cone": df, "iv_rank_in_hv": iv_rank}


def compute_chain(df: pd.DataFrame, F: float, T: float, r: float = 0.0) -> pd.DataFrame:
    """为期权链补充 IV、Delta、Gamma、Vega、Vanna、单合约 GEX。

    输入列：strike, option_class, price, open_interest, volume_multiple
    """
    out = df.copy()
    ivs, deltas, gammas, vegas, vannas, gex = [], [], [], [], [], []
    for row in out.itertuples(index=False):
        cp = str(row.option_class).upper()
        strike = float(row.strike)
        price = float(row.price)
        iv = implied_vol(price, F, strike, T, cp, r)
        delta, gamma, vega = black76_greeks(F, strike, T, iv, cp, r)
        vanna = black76_vanna(F, strike, T, iv, cp, r)
        oi = float(row.open_interest) if _ok(row.open_interest) else 0.0
        mult = float(row.volume_multiple) if _ok(row.volume_multiple) and row.volume_multiple > 0 else 1.0
        sign = 1.0 if cp.startswith("C") else -1.0
        contract_gex = gamma * oi * mult * F * F * 0.01 * sign if _ok(gamma) else NAN
        ivs.append(iv)
        deltas.append(delta)
        gammas.append(gamma)
        vegas.append(vega)
        vannas.append(vanna)
        gex.append(contract_gex)
    out["iv"] = ivs
    out["delta"] = deltas
    out["gamma"] = gammas
    out["vega"] = vegas
    out["vanna"] = vannas
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
    """按行权价汇总 GEX 与 Vanna，并给出占比、累计占比与累计净 GEX。"""
    valid = df[df["gex"].notna() & np.isfinite(df["gex"])]
    if valid.empty:
        return pd.DataFrame(columns=["strike", "call_gex", "put_gex", "net_gex",
                                     "call_vanna", "put_vanna", "net_vanna",
                                     "share_pct", "cum_share_pct", "cum_net_gex",
                                     "oi", "call_oi", "put_oi"])
    has_vanna = "vanna" in valid.columns
    upper = valid["option_class"].astype(str).str.upper()
    call_mask = upper.str.startswith("C").values
    put_mask = upper.str.startswith("P").values

    # 向量化聚合：避免逐档 Python 循环（链档量大时明显更快）
    call_df = valid[call_mask].groupby("strike").agg(
        call_gex=("gex", "sum"), call_oi=("open_interest", "sum"),
        **({"call_vanna": ("vanna", "sum")} if has_vanna else {}))
    put_df = valid[put_mask].groupby("strike").agg(
        put_gex=("gex", "sum"), put_oi=("open_interest", "sum"),
        **({"put_vanna": ("vanna", "sum")} if has_vanna else {}))
    table = pd.concat([call_df, put_df], axis=1).fillna(0.0).sort_index().reset_index()
    table = table.rename(columns={"index": "strike"})
    if "strike" not in table.columns:
        table = table.rename(columns={table.columns[0]: "strike"})
    for col in ("call_gex", "put_gex", "call_oi", "put_oi"):
        if col not in table.columns:
            table[col] = 0.0
    table["net_gex"] = table["call_gex"] + table["put_gex"]
    table["oi"] = table["call_oi"] + table["put_oi"]
    if has_vanna:
        for col in ("call_vanna", "put_vanna"):
            if col not in table.columns:
                table[col] = 0.0
        table["net_vanna"] = table["call_vanna"] + table["put_vanna"]
    abs_net = table["net_gex"].abs()
    total_abs = abs_net.sum()
    table["share_pct"] = (abs_net / total_abs * 100.0).fillna(0) if total_abs > 0 else 0.0
    table["cum_share_pct"] = table["share_pct"].cumsum()
    table["cum_net_gex"] = table["net_gex"].cumsum()
    return table


def vol_trigger_level(gex_df: pd.DataFrame) -> float:
    """波动切换位：累计净 Vanna 由负转正的行权价（线性插值）。

    意义：Vanna 由负转正处，价格对波动率的反馈机制切换，常对应波动由收敛转放大。
    """
    if gex_df.empty or "net_vanna" not in gex_df.columns:
        return NAN
    strikes = gex_df["strike"].values
    cum = gex_df["net_vanna"].cumsum().values
    for i in range(1, len(cum)):
        if (cum[i - 1] < 0 <= cum[i]) or (cum[i - 1] > 0 >= cum[i]):
            x0, x1, y0, y1 = strikes[i - 1], strikes[i], cum[i - 1], cum[i]
            if y1 == y0:
                return float(x1)
            return float(x0 + (0 - y0) * (x1 - x0) / (y1 - y0))
    return NAN


def zero_gamma_level(gex_df: pd.DataFrame, F: float = NAN) -> float:
    """累计净 GEX 的零伽马价位（线性插值）。传入现价时取离现价最近的一次穿越。"""
    if gex_df.empty:
        return NAN
    strikes = gex_df["strike"].values
    cum = gex_df["cum_net_gex"].values
    crossings = []
    for i in range(1, len(cum)):
        if (cum[i - 1] < 0 <= cum[i]) or (cum[i - 1] > 0 >= cum[i]):
            x0, x1, y0, y1 = strikes[i - 1], strikes[i], cum[i - 1], cum[i]
            if y1 == y0:
                crossings.append(float(x1))
            else:
                crossings.append(float(x0 + (0 - y0) * (x1 - x0) / (y1 - y0)))
    if not crossings:
        return NAN
    if _ok(F):
        return min(crossings, key=lambda s: abs(s - F))
    return crossings[0]


def max_pain_level(gex_df: pd.DataFrame) -> float:
    """Max Pain：使全部期权买方合计内在价值损失最小的到期结算价。

    直觉：做市商/卖方仓位最舒服的价位，价格到期时容易被"吸"过去。
    """
    if gex_df.empty or "call_oi" not in gex_df.columns:
        return NAN
    strikes = gex_df["strike"].values.astype(float)
    call_oi = gex_df["call_oi"].fillna(0).values.astype(float)
    put_oi = gex_df["put_oi"].fillna(0).values.astype(float)
    if len(strikes) == 0:
        return NAN
    best_k, best_loss = NAN, None
    for k in strikes:
        loss = float(np.sum(call_oi * np.maximum(k - strikes, 0.0)) +
                     np.sum(put_oi * np.maximum(strikes - k, 0.0)))
        if best_loss is None or loss < best_loss:
            best_loss, best_k = loss, float(k)
    return best_k


def summary_metrics(df: pd.DataFrame, F: float, hv20: float = NAN, hv60: float = NAN,
                    gex_df: pd.DataFrame | None = None) -> dict:
    """单条期权链的汇总指标。gex_df 可传入已算好的表，避免重复计算。"""
    sk = skew_stats(df, F)
    curve = smile_by_strike(df)
    gex_df = gex_table(df) if gex_df is None else gex_df
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
    gamma_wall = NAN
    if not gex_df.empty:
        max_gex_strike = float(gex_df.loc[gex_df["net_gex"].abs().idxmax(), "strike"])
        call_wall = float(gex_df.loc[gex_df["call_gex"].idxmax(), "strike"])
        put_wall = float(gex_df.loc[gex_df["put_gex"].idxmin(), "strike"])
        gamma_wall = float(gex_df.loc[gex_df["net_gex"].abs().idxmax(), "strike"])
    zero_gamma = zero_gamma_level(gex_df, F)
    vol_trigger = vol_trigger_level(gex_df)
    max_pain = max_pain_level(gex_df)
    iv_pct = NAN
    if not curve.empty and _ok(sk["atm_iv"]):
        iv_pct = float((curve <= sk["atm_iv"]).mean() * 100.0)
    iv_hv_pct = ((sk["atm_iv"] - hv20) / hv20 * 100.0) if _ok(sk["atm_iv"], hv20) and hv20 > 0 else NAN

    def _dist(strike):
        if not _ok(strike) or not _ok(F) or F <= 0:
            return NAN
        return (strike - F) / F * 100.0

    def _val(strike):
        if not _ok(strike) or gex_df.empty:
            return NAN
        # 该价位的净 GEX
        row = gex_df.iloc[(gex_df["strike"] - strike).abs().argsort()[:1]]
        if row.empty:
            return NAN
        return float(row["net_gex"].iloc[0])

    key_levels = {
        "call_wall": {"strike": call_wall, "value": _val(call_wall), "dist_pct": _dist(call_wall),
                      "label": "看涨阻力位 Call Wall", "hint": "价格向上易遇天花板"},
        "put_wall": {"strike": put_wall, "value": _val(put_wall), "dist_pct": _dist(put_wall),
                     "label": "看跌支撑位 Put Wall", "hint": "价格向下易托住地板"},
        "gamma_wall": {"strike": gamma_wall, "value": _val(gamma_wall), "dist_pct": _dist(gamma_wall),
                       "label": "伽马关键位 Gamma Wall", "hint": "价格容易被钉住波动收敛"},
        "vol_trigger": {"strike": vol_trigger, "value": NAN, "dist_pct": _dist(vol_trigger),
                        "label": "波动变化位 Vol Trigger", "hint": "市场可能从收敛切换为放大"},
        "zero_gamma": {"strike": zero_gamma, "value": NAN, "dist_pct": _dist(zero_gamma),
                       "label": "零伽马位 Gamma Flip", "hint": "做市商对冲方向反转"},
        "max_pain": {"strike": max_pain, "value": NAN, "dist_pct": _dist(max_pain),
                     "label": "最大痛点 Max Pain", "hint": "到期引力中心"},
    }
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
        "gamma_wall": gamma_wall,
        "vol_trigger": vol_trigger,
        "zero_gamma": zero_gamma,
        "max_pain": max_pain,
        "key_levels": key_levels,
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
