#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实盘行情 + TqSim 模拟交易验证
============================

用全市场扫描的顶级候选 SHFE.hc（IV 溢价 132%）运行完整管线：
  缠论二买定方向 → 建仓筛选(DTE/IV溢价/期权墙) → WCD定仓 → 卖开 + 买尾部保险

运行：
  TQ_ACCOUNT=xxx TQ_PASSWORD=xxx python3 live_sim_trade.py
"""

import os
import sys
from datetime import datetime
from tqsdk import TqApi, TqAuth, TqSim

sys.path.insert(0, "/workspace")
from seller_greeks_system import (
    INIT_BALANCE, SellerGreeksSystem, chan_second_buy,
    EntryFilter, ENTRY_DTE_MIN, ENTRY_DTE_MAX,
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
        # 选标的：SHFE.hc 主力（扫描中 IV 溢价最高之一）
        q = api.get_quote("KQ.m@SHFE.hc")
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
            print("  当前无明确二买信号，仅演示流程（仍继续后续步骤）")
            direction = "LONG"  # 演示用：假设上涨方向卖Put

        # ===== 2. 建仓筛选 =====
        print("\n" + "=" * 60)
        print(f"【步骤2】建仓筛选（DTE {ENTRY_DTE_MIN}-{ENTRY_DTE_MAX} / IV>HV / 期权墙）")
        print("=" * 60)
        entry_filter = EntryFilter(api)
        opts = api.query_options(underlying, expired=False)
        print(f"  在市期权数: {len(opts)}")

        # 选近 ATM Put（卖Put=看涨），优先 15-35 DTE，否则取最近的
        import re
        candidates = []
        for o in opts:
            oq = api.get_quote(o)
            m = re.search(r'[CP](\d+\.?\d*)$', o)
            if not m or oq.option_class != "PUT":
                continue
            dte = oq.expire_rest_days or 0
            k = float(m.group(1))
            candidates.append((abs(k - S), o, oq, dte))
        candidates.sort()
        # 优先 15-35 DTE，否则取 DTE 最接近区间的
        in_range = [c for c in candidates if ENTRY_DTE_MIN <= c[3] <= ENTRY_DTE_MAX]
        chosen = in_range[:8] if in_range else candidates[:8]
        print(f"  近ATM Put候选: {len(chosen)} 个 "
              f"(DTE范围 {min(c[3] for c in chosen):.0f}-{max(c[3] for c in chosen):.0f})")

        best = None
        for dist, o, oq, dte in chosen:
            ok, info = entry_filter.screen(o, direction)
            tag = "✅" if ok else "❌"
            print(f"  {tag} {o} K={oq.strike_price} DTE={dte:.0f} "
                  f"IV溢价={info.get('iv_premium', 'N/A')} 原因={info.get('reason', '')}")
            if ok and (best is None or info["iv_premium"] > best[1]["iv_premium"]):
                best = (o, info)

        if not best:
            print("  无满足全部条件的合约，用 DTE 最接近 15-35 的候选演示")
            chosen.sort(key=lambda c: abs(c[3] - 25))
            best = (chosen[0][1], {"iv_premium": 0, "symbol": chosen[0][1]})

        sym, info = best
        print(f"\n  选中: {sym}")

        # ===== 3. WCD 定仓位 + 卖开 + 买保险 =====
        print("\n" + "=" * 60)
        print("【步骤3】WCD定仓位 → 卖开 → 尾部保险（杠铃）")
        print("=" * 60)
        sys = SellerGreeksSystem(api)
        # 直接调用 stage_one_entry 会重新跑缠论；这里手动执行核心逻辑
        from seller_greeks_system import get_limit_price, wait, WCD_RATIO
        oq = api.get_quote(sym)
        wait(api, deadline=8)
        entry_price = get_limit_price(api, sym, "SELL")
        vol_mult = oq.volume_multiple or 10
        volume = sys._calc_wcd_volume(entry_price, vol_mult)
        print(f"  权利金={entry_price} 乘数={vol_mult}")
        print(f"  WCD手数={volume} (最坏日亏≤组合 {WCD_RATIO*100:.0f}%)")

        if volume > 0:
            print(f"  卖开 {sym} 手数={volume} @ {entry_price}")
            order = api.insert_order(sym, direction="SELL", offset="OPEN",
                                     volume=volume, limit_price=entry_price)
            wait(api, deadline=10)
            print(f"  委托状态: {order.status}")

        # 买尾部保险
        print(f"\n  买入尾部保险（远OTM Put, Delta≈0.10）...")
        ins = sys._buy_tail_insurance(underlying)
        if ins:
            print(f"  保险腿: {ins}")
        else:
            print("  未找到合适保险腿")

        # ===== 4. 持仓阶段初始化 =====
        print("\n" + "=" * 60)
        print("【步骤4】持仓登记 & 阶段监控")
        print("=" * 60)
        if volume > 0:
            from seller_greeks_system import PositionManager
            sys.positions[sym] = {
                "entry_premium": entry_price,
                "manager": PositionManager(api, entry_price),
                "stage": 1,
                "direction": direction,
                "volume": volume,
            }
            # 运行一次持仓监控
            sys.stage_two_manage()
            sys.stage_three_hard_exit()

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
        for r in sys.trade_log:
            print(f"  {r['time'][:19]} {r['action']:15s} {r['symbol']:25s} {r['detail']}")

    finally:
        api.close()
        print(f"\n[{datetime.now()}] 连接已关闭")


if __name__ == "__main__":
    main()
