#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实盘行情 + TqSim 模拟交易验证
============================

用 15-35 DTE 窗口内的有效标的运行完整管线：
  缠论二买定方向 → 建仓筛选(DTE/IV溢价/Delta/期权墙) → WCD定仓 → 卖开 + 买尾部保险

运行：
  TQ_ACCOUNT=xxx TQ_PASSWORD=xxx python3 live_sim_trade.py [SHFE.cu]
  （默认 SHFE.cu；可传品种如 SHFE.ag 或具体合约如 SHFE.cu2612）
"""

import os
import sys
from datetime import datetime
from tqsdk import TqApi, TqAuth, TqSim

sys.path.insert(0, "/workspace")
from seller_greeks_system import (
    INIT_BALANCE, SellerGreeksSystem, chan_second_buy,
    EntryFilter, ENTRY_DTE_MIN, ENTRY_DTE_MAX,
    option_greeks_full, implied_vol, RISK_FREE_RATE, get_price,
    get_limit_price, wait, WCD_RATIO, PositionManager,
    estimate_short_option_margin, cap_volume_by_margin,
)


def main():
    account = os.environ.get("TQ_ACCOUNT", "")
    password = os.environ.get("TQ_PASSWORD", "")
    if not account or not password:
        print("错误：请设置 TQ_ACCOUNT 和 TQ_PASSWORD")
        sys.exit(1)

    print(f"[{datetime.now()}] 连接实盘行情 + TqSim(初始资金 {INIT_BALANCE:,.0f})")
    api = TqApi(account=TqSim(init_balance=INIT_BALANCE),
                auth=TqAuth(account, password))
    try:
        # 选标的：默认 SHFE.cu（当前 2612 系列期权 DTE≈22，在 15-35 建仓窗口）
        import re
        target = sys.argv[1] if len(sys.argv) > 1 else "SHFE.cu"
        if target.startswith("KQ.") or re.search(r'\d', target):
            quote_sym = target
        else:
            quote_sym = f"KQ.m@{target}"
        q = api.get_quote(quote_sym)
        api.wait_update(deadline=15)
        underlying = q.underlying_symbol
        ul_q = api.get_quote(underlying)
        api.wait_update(deadline=10)
        S = ul_q.last_price or ul_q.pre_settlement
        print(f"\n标的: {underlying}  最新价={S}")

        # ===== 1. 缠论二买定方向 =====
        print("\n" + "=" * 60)
        print("【步骤1】缠论二买方向判断（30min 分型→笔→段→中枢→二买）")
        print("=" * 60)
        direction = chan_second_buy(api, underlying)
        print(f"  缠论判定方向: {direction}")
        if direction is None:
            if "--demo" not in sys.argv:
                print("  无明确二买信号，按纪律不开仓（如需演示流程加 --demo）")
                return
            print("  --demo：无信号，仅演示流程，假设上涨方向卖Put")
            direction = "LONG"

        # ===== 2. 建仓筛选 =====
        print("\n" + "=" * 60)
        print(f"【步骤2】建仓筛选（DTE {ENTRY_DTE_MIN}-{ENTRY_DTE_MAX} / IV>HV / 期权墙）")
        print("=" * 60)
        entry_filter = EntryFilter(api)
        opts = api.query_options(underlying, expired=False)
        print(f"  在市期权数: {len(opts)}")

        # 按缠论方向选边：LONG(看涨)→卖Put，SHORT(看跌)→卖Call
        cp = "PUT" if direction == "LONG" else "CALL"
        print(f"  方向={direction} → 卖出 {cp}")
        candidates = []
        for o in opts:
            oq = api.get_quote(o)
            m = re.search(r'[CP](\d+\.?\d*)$', o)
            if not m or oq.option_class != cp:
                continue
            dte = oq.expire_rest_days or 0
            k = float(m.group(1))
            candidates.append((abs(k - S), o, oq, dte))
        candidates.sort()
        # 严格筛选 15-35 DTE（不 fallback，违规不开仓）
        in_range = [c for c in candidates if ENTRY_DTE_MIN <= c[3] <= ENTRY_DTE_MAX]
        if not in_range:
            dte_range = f"{min(c[3] for c in candidates):.0f}-{max(c[3] for c in candidates):.0f}" if candidates else "N/A"
            print(f"  ⚠️ 无 {ENTRY_DTE_MIN}-{ENTRY_DTE_MAX} DTE 的 {cp} 合约")
            print(f"  当前期权 DTE 范围: {dte_range}，不在建仓窗口内，不开仓")
            print("  （等待下一个到期月进入 15-35 DTE 区间）")
            return
        chosen = in_range[:8]
        print(f"  近ATM {cp}候选（{ENTRY_DTE_MIN}-{ENTRY_DTE_MAX} DTE）: {len(chosen)} 个")

        best = None
        for dist, o, oq, dte in chosen:
            ok, info = entry_filter.screen(o, direction)
            tag = "✅" if ok else "❌"
            print(f"  {tag} {o} K={oq.strike_price} DTE={dte:.0f} "
                  f"IV溢价={info.get('iv_premium', 'N/A')} 原因={info.get('reason', '')}")
            if ok and (best is None or info["iv_premium"] > best[1]["iv_premium"]):
                best = (o, info)

        if not best:
            print("  无满足全部条件的合约，不开仓")
            return

        sym, info = best
        print(f"\n  选中: {sym}")

        # ===== 2.5 三阶 Greeks 明细 =====
        print("\n" + "-" * 60)
        print("【选中合约三阶 Greeks】")
        print("-" * 60)
        sel_q = api.get_quote(sym)
        wait(api, deadline=5)
        S_sel = get_price(ul_q)
        K_sel = sel_q.strike_price
        T_sel = (sel_q.expire_rest_days or 0) / 365.0
        P_sel = get_price(sel_q)
        if all([S_sel, K_sel, P_sel, T_sel > 0]):
            iv_sel = implied_vol(P_sel, S_sel, K_sel, T_sel, RISK_FREE_RATE, sel_q.option_class)
            if iv_sel:
                g = option_greeks_full(S_sel, K_sel, T_sel, RISK_FREE_RATE, iv_sel, sel_q.option_class)
                print(f"  S={S_sel} K={K_sel} T={T_sel*365:.0f}DTE IV={iv_sel*100:.1f}%")
                print(f"  一阶: Δ={g['delta']:.3f}  V={g['vega']:.1f}  Θ={g['theta']:.1f}")
                print(f"  二阶: Γ={g['gamma']*S_sel:.6f}(×S归一)  Vanna={g['vanna']:.4f}")
                print(f"  三阶: Speed={g['speed']:.8f}  Color={g['color']:.6f}")

        # ===== 3. WCD 定仓位 + 卖开 + 买保险 =====
        print("\n" + "=" * 60)
        print("【步骤3】WCD定仓位 → 卖开 → 尾部保险（杠铃）")
        print("=" * 60)
        strategy = SellerGreeksSystem(api)
        # 直接调用 stage_one_entry 会重新跑缠论；这里手动执行核心逻辑
        oq = api.get_quote(sym)
        wait(api, deadline=8)
        entry_price = get_limit_price(api, sym, "SELL")
        vol_mult = oq.volume_multiple or 10
        volume = strategy._calc_wcd_volume(entry_price, vol_mult)
        print(f"  权利金={entry_price} 乘数={vol_mult}")
        print(f"  WCD手数={volume} (最坏日亏≤组合 {WCD_RATIO*100:.0f}%)")
        # 建议3：单品种保证金 ≤ 15%，用标的保证金估算单手卖方保证金
        per_lot = estimate_short_option_margin(
            entry_price, vol_mult, getattr(ul_q, "margin", None),
            oq.strike_price, S, cp)
        capped = cap_volume_by_margin(volume, per_lot, INIT_BALANCE)
        if per_lot:
            print(f"  单手卖方保证金≈{per_lot:,.0f} ({per_lot/INIT_BALANCE*100:.1f}%权益)")
        if capped != volume:
            print(f"  集中度上限(15%)：手数 {volume}→{capped}")
        volume = capped
        if volume <= 0:
            print("  集中度超限，不开仓")
            return
        print(f"  卖开 {sym} 手数={volume} @ {entry_price}")
        order = api.insert_order(sym, direction="SELL", offset="OPEN",
                                 volume=volume, limit_price=entry_price)
        wait(api, deadline=10)
        print(f"  委托状态: {order.status}")

        # 买尾部保险（卖Call→买远OTM Call，卖Put→买远OTM Put）
        ins_cp = "CALL" if cp == "CALL" else "PUT"
        print(f"\n  买入尾部保险（远OTM {ins_cp}, Delta≈0.10）...")
        ins = strategy._buy_tail_insurance(underlying, direction, volume,
                                           sold_strike=oq.strike_price)
        if ins:
            print(f"  保险腿: {ins}")
        else:
            print("  未找到合适保险腿")

        # ===== 4. 持仓阶段初始化 =====
        print("\n" + "=" * 60)
        print("【步骤4】持仓登记 & 阶段监控")
        print("=" * 60)
        if volume > 0:
            manager = PositionManager(api, entry_price)
            strategy.positions[sym] = {
                "entry_premium": entry_price,
                "manager": manager,
                "stage": 1,
                "direction": direction,
                "volume": volume,
            }
            # 记录三阶 Greeks 建仓基线（相对倍数爆炸判定）
            manager.capture_baseline(sym)
            # 运行一次持仓监控
            strategy.stage_two_manage()
            strategy.stage_three_hard_exit()

        # ===== 账户状态 =====
        print("\n" + "=" * 60)
        print("【账户状态】")
        print("=" * 60)
        acc = api.get_account()
        wait(api, deadline=8)
        print(f"  账户权益: {acc.balance:,.0f}")
        print(f"  可用资金: {acc.available:,.0f}")
        print(f"  保证金:   {acc.margin:,.0f}")
        print(f"  风险度:   {acc.risk_ratio*100:.1f}%")

        pos = api.get_position()
        print(f"\n  持仓:")
        for sym_p, p in pos.items():
            if p.pos_long or p.pos_short:
                print(f"    {sym_p}: 多{p.pos_long} 空{p.pos_short} "
                      f"浮盈={p.float_profit:,.0f}")

        # 交易日志
        print("\n" + "=" * 60)
        print("【交易学习日志】")
        print("=" * 60)
        for r in strategy.trade_log:
            print(f"  {r['time'][:19]} {r['action']:15s} {r['symbol']:25s} {r['detail']}")

        # ===== 5. 常驻监控：交易时段每 30 秒检查，非交易时段休眠 =====
        if strategy.positions:
            strategy.monitor_forever(interval=30)

    finally:
        api.close()
        print(f"\n[{datetime.now()}] 连接已关闭")


if __name__ == "__main__":
    main()
