#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
塔勒布视角商品期权全市场扫描 + 模拟下单
======================================

【关于 TqSdk】
TqSdk 是信易科技开源的 Python 量化交易开发包，面向国内期货、期权、股票市场，
提供实时行情、K 线数据、历史回测、模拟交易和实盘交易等能力。
本脚本使用 TqApi 获取行情，用 TqSim 模拟账户（初始资金 100,000 元）演示扫描与下单逻辑。
运行前需把 TQ_ACCOUNT / TQ_PASSWORD 环境变量替换为自己的快期账户信息。

【策略思路】
基于塔勒布（Nassim Nicholas Taleb）思维框架对商品期权进行扫描并构建杠铃组合：

  模型1 凸性优先：先看赔付曲线弯向。卖期权=负凸性=脆弱，必须配正凸性保险腿。
  模型2 脆弱性而非概率：不测事件概率，测对离散度的敏感性。
  模型3 遍历性与生存优先：用最坏单日定仓位，不用胜率/VaR；破产即出局。
  模型4 杠铃+减法：90% 现金 + 卖 ATM 收租(线性、贵得可证) + 买远 OTM 保险。
  模型5 Skin in the Game：自有资金承担全部下行。
  模型6 领域依赖/游戏谬误：BSM 仅作定价插值，复制/对冲不可信。

扫描维度（每个期权）：
  1. 波动率与 IV 状态：IV 相对历史已实现波动率(HV)的溢价——只有"贵得可证"才卖。
  2. 赔付结构与凸性：行权价距离(OTM 程度)、卖方负凸性暴露。
  3. 尾部与生存：是否深 OTM(尾部赔付倍数大)、IV 折价(卖了更危险)。
  4. 历史类比：低 IV 环境是脆弱信号(calm before storm)。

杠铃组合（100,000 元）：
  - 安全端：约 93,000 元留作现金/保证金缓冲
  - 收租端：卖出 IV 显著溢价、接近 ATM 的期权（线性、贵得可证）
  - 保险端：买入同标的远 OTM put（Delta≈0.10）做尾部对冲
  - 单笔收租名义保证金不超过组合 5%；保险预算约 3.3%

【运行说明】
1. pip install tqsdk scipy numpy pandas
2. export TQ_ACCOUNT=你的快期账号 TQ_PASSWORD=你的快期密码
3. 先使用模拟账户运行，不建议未经验证直接用于实盘

【风险提示】
本脚本只用于学习和研究，不构成投资建议。期权卖方存在尾部巨亏风险，
历史规律会失效，跳空、流动性不足和保证金螺旋都可能导致亏损。
"""

import os
import sys
import math
import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.optimize import brentq
from datetime import datetime
from tqsdk import TqApi, TqAuth, TqSim

# ============ 配置 ============
# 交易所列表（品种动态发现，不硬编码）
SCAN_EXCHANGES = ["SHFE", "DCE", "CZCE", "GFEX"]

INIT_BALANCE = 100000.0          # 模拟账户初始资金
HV_LOOKBACK = 20                 # 历史波动率回看天数
MAX_OPTIONS_PER_UL = 12          # 每个标的扫描最接近 ATM 的期权数
SELL_IV_PREMIUM_THRESHOLD = 0.15  # IV 溢价阈值，高于此才考虑卖
SELL_MAX_OTM = 0.08              # 卖方最大 OTM 程度（接近 ATM，线性区）
INSURANCE_DELTA = 0.10           # 保险腿目标 Delta（远 OTM）
SAFE_RATIO = 0.93                # 安全端现金比例
INSURANCE_BUDGET_RATIO = 0.0333  # 尾部保险预算占净值比例
RISK_FREE_RATE = 0.025           # 无风险利率（BS 用）
DEADLINE = 20                    # wait_update 超时秒数

# ============ Black-Scholes 工具 ============
def _bs_price(S, K, T, r, sigma, opt_type):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if opt_type == "CALL" else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt_type == "CALL":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def implied_vol(price, S, K, T, r, opt_type):
    """由期权市价反推隐含波动率"""
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if opt_type == "CALL" else (K - S))
    if price < intrinsic:
        return None
    try:
        f = lambda sig: _bs_price(S, K, T, r, sig, opt_type) - price
        return brentq(f, 1e-6, 5.0, maxiter=100)
    except Exception:
        return None


def option_greeks(S, K, T, r, sigma, opt_type):
    """计算 delta / gamma / vega"""
    if T <= 0 or sigma <= 0:
        return 0.0, 0.0, 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    delta = norm.cdf(d1) if opt_type == "CALL" else norm.cdf(d1) - 1.0
    gamma = norm.pdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * norm.pdf(d1) * math.sqrt(T)
    return delta, gamma, vega


# ============ 行情辅助 ============
def wait(api, deadline=DEADLINE):
    """带超时的 wait_update，避免周末/休市永久阻塞"""
    api.wait_update(deadline=deadline)


def get_main_contract(api, exch_prod):
    """获取主力合约代码"""
    q = api.get_quote(f"KQ.m@{exch_prod}")
    wait(api)
    return q.underlying_symbol


def discover_option_products(api, exchanges=SCAN_EXCHANGES):
    """动态发现各交易所所有有期权的品种（不硬编码）

    对每个交易所：
      1. query_quotes 取全部在市期货
      2. 提取 product_id
      3. 对每个品种取主力合约（KQ.m@），查 query_options
      4. 有在市期权的品种纳入扫描
    """
    result = {}
    for exch in exchanges:
        try:
            futs = api.query_quotes(ins_class='FUTURE', exchange_id=exch, expired=False)
        except Exception as e:
            print(f"  [discover] {exch} 期货查询失败: {e}")
            continue
        products = sorted(set(f.split('.')[1].rstrip('0123456789') for f in futs))
        opt_products = []
        for p in products:
            ul = None
            try:
                q = api.get_quote(f"KQ.m@{exch}.{p}")
                wait(api, deadline=8)
                ul = q.underlying_symbol
            except Exception:
                ul = None
            if not ul:
                # 回退：取该品种持仓量最大的合约
                matching = [f for f in futs if f.split('.')[1].rstrip('0123456789') == p]
                best, best_oi = None, -1
                for m in matching:
                    try:
                        mq = api.get_quote(m)
                        wait(api, deadline=5)
                        oi = mq.open_interest or 0
                        if oi > best_oi:
                            best_oi, best = oi, m
                    except Exception:
                        continue
                ul = best
            if not ul:
                continue
            try:
                opts = api.query_options(ul, expired=False)
                if opts:
                    opt_products.append(p)
            except Exception:
                pass
        result[exch] = opt_products
        print(f"  [discover] {exch}: {len(opt_products)} 个有期权品种 -> {opt_products}")
    return result


def compute_hv(api, underlying, lookback=HV_LOOKBACK):
    """标的历史波动率（日收益年化）"""
    try:
        kl = api.get_kline_serial(underlying, 86400, data_length=lookback + 5)
        wait(api)
    except Exception:
        return None
    closes = kl["close"].dropna().values
    if len(closes) < lookback:
        return None
    rets = np.diff(np.log(closes[-lookback:]))
    if len(rets) < 2:
        return None
    return float(np.std(rets) * np.sqrt(252))


def get_option_price(q):
    """取期权有效价格：最新价 -> 买一 -> 卖一 -> 昨结"""
    for f in ("last_price", "bid_price1", "ask_price1", "pre_settlement"):
        v = getattr(q, f, None)
        if v is not None and not (isinstance(v, float) and math.isnan(v)) and v > 0:
            return float(v)
    return None


# ============ 扫描 ============
def scan_option(api, opt_symbol, hv):
    q = api.get_quote(opt_symbol)
    wait(api)
    ul_sym = q.underlying_symbol
    ul_q = api.get_quote(ul_sym)
    wait(api)
    S = ul_q.last_price or ul_q.pre_settlement
    K = q.strike_price
    T = (q.expire_rest_days or 0) / 365.0
    opt_type = q.option_class  # CALL / PUT
    price = get_option_price(q)
    if not S or not K or not price or T <= 0:
        return None
    iv = implied_vol(price, S, K, T, RISK_FREE_RATE, opt_type)
    if iv is None:
        return None
    delta, gamma, vega = option_greeks(S, K, T, RISK_FREE_RATE, iv, opt_type)
    moneyness = (K - S) / S  # 正=CALL OTM / PUT ITM；负=CALL ITM / PUT OTM
    otm_dist = abs(moneyness)
    iv_premium = (iv - hv) / hv if hv and hv > 0 else None
    # 尾部风险评分：深 OTM + IV 折价 → 卖方危险
    tail_risk = 0.0
    if iv_premium is not None and iv_premium < -0.05:
        tail_risk += 1.0
    if otm_dist > 0.12:
        tail_risk += 1.5
    elif otm_dist > 0.08:
        tail_risk += 0.8
    return {
        "symbol": opt_symbol,
        "type": opt_type,
        "strike": K,
        "ul": ul_sym,
        "ul_price": S,
        "price": price,
        "iv": iv,
        "hv": hv,
        "iv_premium": iv_premium,
        "moneyness": moneyness,
        "otm_dist": otm_dist,
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "tail_risk": tail_risk,
    }


def taleb_verdict(rec):
    """塔勒布判定：只对卖方逻辑给出建议"""
    ivp = rec["iv_premium"]
    otm = rec["otm_dist"]
    if ivp is None:
        return "SKIP", "无 HV 对照"
    if ivp > SELL_IV_PREMIUM_THRESHOLD and otm <= SELL_MAX_OTM:
        # IV 显著溢价且接近 ATM：线性、贵得可证 → 可卖，但必须配保险
        return "SELL_WITH_HEDGE", f"IV 溢价 {ivp*100:.1f}%，OTM {otm*100:.1f}%"
    if otm > 0.12 and ivp < 0:
        return "AVOID_TAIL_SELL", "深 OTM 且 IV 折价，尾部赔付倍数大"
    if ivp < -0.10:
        return "AVOID_SELL", f"IV 折价 {ivp*100:.1f}%，卖方不划算"
    return "HOLD", f"IV 溢价 {ivp*100:.1f}%"


def select_insurance_put(api, ul_sym, target_delta=INSURANCE_DELTA):
    """在同标的上选远 OTM put 做尾部保险（Delta 最接近 target_delta）"""
    opts = api.query_options(ul_sym, expired=False)
    if not opts:
        return None
    ul_q = api.get_quote(ul_sym)
    wait(api)
    S = ul_q.last_price or ul_q.pre_settlement
    best = None
    best_diff = 1.0
    for o in opts:
        if "P" not in o.split(".")[-1]:
            continue
        q = api.get_quote(o)
        wait(api)
        K = q.strike_price
        T = (q.expire_rest_days or 0) / 365.0
        price = get_option_price(q)
        if not S or not K or not price or T <= 0:
            continue
        iv = implied_vol(price, S, K, T, RISK_FREE_RATE, "PUT")
        if iv is None:
            continue
        delta, _, _ = option_greeks(S, K, T, RISK_FREE_RATE, iv, "PUT")
        diff = abs(abs(delta) - target_delta)
        if diff < best_diff:
            best_diff = diff
            best = {"symbol": o, "strike": K, "price": price, "iv": iv,
                    "delta": delta, "expire_rest_days": q.expire_rest_days}
    return best


def get_limit_price(api, symbol, direction):
    """获取限价单价格：卖出用买一价，买入用卖一价；缺失时用昨结"""
    q = api.get_quote(symbol)
    wait(api, deadline=5)
    if direction == "SELL":
        p = q.bid_price1
    else:
        p = q.ask_price1
    if not p or (isinstance(p, float) and math.isnan(p)) or p <= 0:
        p = q.pre_settlement or q.last_price
    return float(p) if p and p > 0 else None


# ============ 下单逻辑 ============
def build_portfolio(api, sell_candidates, insurance_budget):
    """根据扫描结果构建杠铃组合并下达模拟单"""
    orders = []
    # 收租端：选 IV 溢价最高的 1-2 个，卖 1 手（限价单，上期所等不支持市价单）
    sell_candidates = sorted(sell_candidates, key=lambda x: x["iv_premium"], reverse=True)
    seen_ul = set()
    for rec in sell_candidates:
        if rec["ul"] in seen_ul:
            continue
        seen_ul.add(rec["ul"])
        limit = get_limit_price(api, rec["symbol"], "SELL")
        if not limit:
            print(f"  [跳过] {rec['symbol']} 无可用限价")
            continue
        try:
            order = api.insert_order(rec["symbol"], direction="SELL",
                                     offset="OPEN", volume=1, limit_price=limit)
            wait(api, deadline=8)
            orders.append(("SELL_OPEN", rec["symbol"], rec["ul"], rec["iv_premium"], order))
            print(f"  [下单] 卖开 {rec['symbol']}  限价={limit}  IV溢价={rec['iv_premium']*100:.1f}%")
        except Exception as e:
            print(f"  [下单失败] {rec['symbol']}: {e}")
        if len([o for o in orders if o[0] == "SELL_OPEN"]) >= 2:
            break
    # 保险端：为每个卖方标的买远 OTM put（用 rec 中已存的 ul，避免字符串拆分）
    for o in [x for x in orders if x[0] == "SELL_OPEN"]:
        _, sym, ul_sym, ivp, _ = o
        ins = select_insurance_put(api, ul_sym)
        if not ins:
            print(f"  [保险] {ul_sym} 无合适远 OTM put")
            continue
        vol_mult = api.get_quote(sym).volume_multiple or 10
        qty = max(1, int(insurance_budget / max(ins["price"] * vol_mult, 1)))
        limit = get_limit_price(api, ins["symbol"], "BUY")
        if not limit:
            print(f"  [跳过保险] {ins['symbol']} 无可用限价")
            continue
        try:
            order = api.insert_order(ins["symbol"], direction="BUY",
                                     offset="OPEN", volume=qty, limit_price=limit)
            wait(api, deadline=8)
            orders.append(("BUY_INSURANCE", ins["symbol"], ul_sym, ins["delta"], order))
            print(f"  [下单] 买保 {ins['symbol']}  限价={limit}  Delta={ins['delta']:.3f}  手数={qty}")
        except Exception as e:
            print(f"  [保险下单失败] {ins['symbol']}: {e}")
    return orders


# ============ 主流程 ============
def main():
    account = os.environ.get("TQ_ACCOUNT", "")
    password = os.environ.get("TQ_PASSWORD", "")
    if not account or not password:
        print("错误：请设置 TQ_ACCOUNT 和 TQ_PASSWORD 环境变量")
        sys.exit(1)

    print(f"[{datetime.now()}] 连接天勤，模拟账户初始资金 {INIT_BALANCE:,.0f} 元")
    api = TqApi(account=TqSim(init_balance=INIT_BALANCE),
                auth=TqAuth(account, password))
    results = []
    try:
        # 动态发现全部有期权的品种（不硬编码）
        print("[discover] 动态发现可交易期权品种...")
        exch_products = discover_option_products(api)
        total_products = sum(len(v) for v in exch_products.values())
        print(f"[discover] 共发现 {total_products} 个有期权品种")

        for exch, products in exch_products.items():
            for prod in products:
                try:
                    ul_symbol = f"{exch}.{prod}"
                    try:
                        main_sym = get_main_contract(api, ul_symbol)
                    except Exception:
                        main_sym = ul_symbol
                    hv = compute_hv(api, main_sym)
                    if hv is None:
                        print(f"  跳过 {exch}.{prod}：无法计算 HV")
                        continue
                    opts = api.query_options(main_sym, expired=False)
                    if not opts:
                        print(f"  跳过 {exch}.{prod}：无在市期权")
                        continue
                    # 取最接近 ATM 的若干
                    ul_q = api.get_quote(main_sym)
                    wait(api)
                    ul_price = ul_q.last_price or ul_q.pre_settlement
                    if not ul_price:
                        continue
                    near = []
                    for o in opts:
                        oq = api.get_quote(o)
                        wait(api, deadline=5)
                        k = oq.strike_price
                        if k:
                            near.append((abs(k - ul_price), o))
                    near.sort()
                    near = [o for _, o in near[:MAX_OPTIONS_PER_UL]]
                    print(f"  扫描 {exch}.{prod}({main_sym}) HV={hv*100:.1f}% 期权{len(opts)}个 取近ATM {len(near)}个")
                    for o in near:
                        rec = scan_option(api, o, hv)
                        if rec:
                            rec["exchange"] = exch
                            rec["product"] = prod
                            results.append(rec)
                except Exception as e:
                    print(f"  [跳过] {exch}.{prod} 出错: {repr(e)}")
                    continue

        df = pd.DataFrame(results)
        # 扫描完立即保存，避免后续下单异常丢失数据
        out = "/workspace/taleb_scan_result.csv"
        try:
            df.to_csv(out, index=False, encoding="utf-8-sig")
        except Exception as e:
            print(f"保存CSV失败: {e}")
        if df.empty:
            print("未扫描到有效期权")
            return
        df["verdict"] = ""
        df["reasons"] = ""
        for i, r in df.iterrows():
            v, rs = taleb_verdict(r)
            df.at[i, "verdict"] = v
            df.at[i, "reasons"] = rs
        try:
            df.to_csv(out, index=False, encoding="utf-8-sig")
        except Exception:
            pass

        print("\n" + "=" * 80)
        print(f"扫描完成：{len(df)} 个期权，{df['exchange'].nunique()} 个交易所")
        print("=" * 80)
        print("\n【判定分布】")
        print(df["verdict"].value_counts().to_string())
        sellable = df[df["verdict"] == "SELL_WITH_HEDGE"].sort_values("iv_premium", ascending=False)
        print("\n【可收租候选（需配保险）—— IV 溢价从高到低】")
        cols = ["exchange", "ul", "symbol", "type", "iv", "iv_premium", "otm_dist", "tail_risk"]
        print(sellable[cols].head(15).to_string(index=False))

        # 构建杠铃组合并下单
        print("\n" + "=" * 80)
        print("【杠铃组合构建与模拟下单】")
        print("=" * 80)
        acc = api.get_account()
        wait(api)
        print(f"账户权益 {acc.balance:,.0f}  可用 {acc.available:,.0f}")
        insurance_budget = acc.balance * INSURANCE_BUDGET_RATIO
        print(f"安全端(现金+缓冲)≈{acc.balance*SAFE_RATIO:,.0f}  保险预算≈{insurance_budget:,.0f}")
        build_portfolio(api, sellable.to_dict("records"), insurance_budget)

        # 再次查询账户
        wait(api)
        acc = api.get_account()
        pos = api.get_position()
        print(f"\n下单后账户权益 {acc.balance:,.0f}  可用 {acc.available:,.0f}  保证金 {acc.margin:,.0f}")
        print("持仓：")
        for sym, p in pos.items():
            if p.pos_long or p.pos_short:
                print(f"  {sym}: 多{p.pos_long} 空{p.pos_short}")

        out = "/workspace/taleb_scan_result.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n完整结果已保存: {out}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
