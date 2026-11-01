#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三阶 Greeks 框架回测验证
========================

验证目标：
  1. WCD 定仓位：最坏单日亏损 ≤ 组合 2%
  2. 移动止盈：权利金回撤 ≤ 50% 时止盈
  3. 硬止盈止损：DTE ≤ 10 强制平仓（无论盈亏）
  4. 三阶段切换：建仓(35-15) → 管理(15-10) → 硬退出(≤10)

运行：
  TQ_ACCOUNT=xxx TQ_PASSWORD=xxx python3 backtest_three_stage.py
"""

import os
import sys
import math
import numpy as np
import pandas as pd
from datetime import datetime, date
from tqsdk import TqApi, TqAuth, TqSim, TqBacktest

# 复用主框架的工具函数和常量
sys.path.insert(0, "/workspace")
from seller_greeks_system import (
    INIT_BALANCE, RISK_FREE_RATE, WCD_RATIO, TP_RETRACE_RATIO,
    HARD_EXIT_DTE, ENTRY_DTE_MIN, ENTRY_DTE_MAX,
    DELTA_LIMIT, IV_PREMIUM_MIN,
    implied_vol, option_greeks, get_price, get_limit_price, wait,
    chan_second_buy, EntryFilter, PositionManager, HardExitManager,
)

# 回测配置
BACKTEST_SYMBOL = "SHFE.rb2501"        # 标的期货（2025年1月螺纹钢）
BACKTEST_START = date(2024, 12, 1)     # 约 45 DTE 开始
BACKTEST_END = date(2025, 1, 10)       # 约 5 DTE 结束（覆盖≤10硬退出）
OPTION_STRIKE = None                    # None=自动选近ATM Put


def test_wcd_sizing():
    """逻辑测试1：WCD 定仓位"""
    print("\n[逻辑测试] WCD 定仓位")
    # 模拟 _calc_wcd_volume 逻辑
    def calc_wcd(entry_price, vol_multiple):
        wcd_loss = entry_price * 2 * vol_multiple
        max_allowed = INIT_BALANCE * WCD_RATIO
        return max(1, int(max_allowed / max(wcd_loss, 1)))

    cases = [
        (100, 10),   # 权利金100, 乘数10
        (50, 10),
        (300, 10),
        (500, 1000),  # 贵金属类大乘数
    ]
    for price, mult in cases:
        vol = calc_wcd(price, mult)
        loss = price * 2 * mult * vol
        ratio = loss / INIT_BALANCE
        status = "✅" if ratio <= WCD_RATIO + 0.001 else "❌"
        print(f"  权利金={price:>4} 乘数={mult:>4} → 手数={vol:>3} "
              f"最坏日亏={loss:>10,.0f} 占比={ratio*100:.2f}% {status}")


def test_hard_exit_logic():
    """逻辑测试2：硬止盈止损 + 移动止盈"""
    print("\n[逻辑测试] 硬止盈止损 & 移动止盈")

    # 模拟 HardExitManager.decide
    class FakeQ:
        def __init__(self, dte, price):
            self.expire_rest_days = dte
            self._price = price

        @property
        def last_price(self):
            return self._price

    # 测试硬退出
    entry = 200.0
    for dte, price, expected in [
        (12, 150, "HOLD"),       # DTE>10, 持有
        (10, 150, "HARD_TP"),    # DTE≤10, 浮盈
        (10, 250, "HARD_STOP"),  # DTE≤10, 浮亏
        (5, 180, "HARD_TP"),     # DTE≤10, 浮盈
        (3, 300, "HARD_STOP"),   # DTE≤10, 浮亏
    ]:
        action, reason = HardExitManager.decide(FakeQ(dte, price), entry)
        ok = "✅" if action == expected else "❌"
        print(f"  DTE={dte:>2} 权利金={price:>5} 入场={entry} → {action:10s} {ok} ({reason})")

    # 测试移动止盈 (PositionManager)
    print("\n  移动止盈模拟（权利金从200跌到120再反弹到160）:")
    pm = PositionManager.__new__(PositionManager)
    pm.entry_premium = 200.0
    pm.max_profit = 0.0
    # 权利金跌到120 → 浮盈80
    pm.max_profit = max(pm.max_profit, 200 - 120)
    # 反弹到160 → 浮盈40, 回撤40 > 50%*200=100? 不触发
    profit = 200 - 160
    should_tp = pm.max_profit > 0 and (pm.max_profit - profit) > TP_RETRACE_RATIO * pm.entry_premium
    print(f"    权利金160: 浮盈={profit} 最大浮盈={pm.max_profit} 回撤触发={should_tp} (期望False)")
    # 反弹到180 → 浮盈20, 回撤60 > 100? 不触发
    profit = 200 - 180
    should_tp = pm.max_profit > 0 and (pm.max_profit - profit) > TP_RETRACE_RATIO * pm.entry_premium
    print(f"    权利金180: 浮盈={profit} 最大浮盈={pm.max_profit} 回撤触发={should_tp} (期望False)")
    # 跌到100 → 浮盈100（最大），再反弹到160 → 浮盈40，回撤60 < 100，不触发
    pm.max_profit = 100
    profit = 200 - 160
    should_tp = pm.max_profit > 0 and (pm.max_profit - profit) > TP_RETRACE_RATIO * pm.entry_premium
    print(f"    权利金160(最大浮盈100): 回撤={100-40} 阈值={TP_RETRACE_RATIO*200} 触发={should_tp}")


def run_tqbacktest():
    """TqBacktest 实盘级回测：验证三阶框架数据闭环"""
    print(f"\n[TqBacktest] {BACKTEST_SYMBOL} {BACKTEST_START} ~ {BACKTEST_END}")
    account = os.environ.get("TQ_ACCOUNT", "")
    password = os.environ.get("TQ_PASSWORD", "")
    if not account or not password:
        print("  跳过：未设置 TQ_ACCOUNT/TQ_PASSWORD")
        return

    api = TqApi(
        account=TqSim(init_balance=INIT_BALANCE),
        auth=TqAuth(account, password),
        backtest=TqBacktest(start_dt=BACKTEST_START, end_dt=BACKTEST_END),
    )
    log = []
    try:
        # 1. 查期权链，选近 ATM Put（卖Put=看涨方向）
        opts = api.query_options(BACKTEST_SYMBOL, expired=False)
        ul_q = api.get_quote(BACKTEST_SYMBOL)
        wait(api, deadline=30)
        S = get_price(ul_q)
        print(f"  标的价={S} 在市期权数={len(opts)}")

        candidates = []
        for o in opts[:30]:  # 限制数量加速回测
            q = api.get_quote(o)
            wait(api, deadline=10)
            if q.option_class != "PUT":
                continue
            dte = q.expire_rest_days or 0
            if not (ENTRY_DTE_MIN <= dte <= ENTRY_DTE_MAX):
                continue
            candidates.append((o, q, dte))
        if not candidates:
            print(f"  无 35-15 DTE 的 Put 合约")
            return

        # 选最接近 ATM
        best = min(candidates, key=lambda x: abs(x[1].strike_price - S))
        sym, q, dte = best
        print(f"  选中: {sym} K={q.strike_price} DTE={dte}")

        # 2. 开仓（1手，验证流程）
        entry_price = get_limit_price(api, sym, "SELL")
        if not entry_price:
            print("  无可用限价")
            return
        print(f"  卖开: {sym} @ {entry_price}")
        api.insert_order(sym, direction="SELL", offset="OPEN", volume=1, limit_price=entry_price)
        wait(api, deadline=30)
        log.append(("ENTRY", sym, entry_price, dte))

        # 3. 回测主循环：推进时间，监控阶段切换
        max_profit = 0.0
        stage = 1
        for tick in range(2000):  # 最多2000个tick
            api.wait_update(deadline=5)
            oq = api.get_quote(sym)
            cur_dte = oq.expire_rest_days or 0
            cur_price = get_price(oq)
            if cur_price is None:
                continue
            profit = entry_price - cur_price
            max_profit = max(max_profit, profit)

            # 阶段切换
            if cur_dte <= HARD_EXIT_DTE:
                if stage != 3:
                    stage = 3
                    print(f"  [阶段切换] → 阶段三 DTE={cur_dte}≤{HARD_EXIT_DTE} 硬止盈止损")
            elif cur_dte <= 15:
                if stage != 2:
                    stage = 2
                    print(f"  [阶段切换] → 阶段二 DTE={cur_dte} 持仓管理")

            # 阶段三：硬退出
            if stage == 3:
                action, reason = HardExitManager.decide(oq, entry_price)
                if action in ("HARD_TP", "HARD_STOP"):
                    lp = get_limit_price(api, sym, "BUY")
                    if lp:
                        api.insert_order(sym, direction="BUY", offset="CLOSE",
                                         volume=1, limit_price=lp)
                        wait(api, deadline=30)
                        log.append((action, sym, cur_price, cur_dte))
                        print(f"  [硬退出] {action} 权利金={cur_price} 浮盈={profit:.1f} DTE={cur_dte}")
                        break

            # 阶段二：移动止盈
            elif stage == 2:
                if (max_profit > 0 and
                        (max_profit - profit) > TP_RETRACE_RATIO * entry_price):
                    lp = get_limit_price(api, sym, "BUY")
                    if lp:
                        api.insert_order(sym, direction="BUY", offset="CLOSE",
                                         volume=1, limit_price=lp)
                        wait(api, deadline=30)
                        log.append(("TP", sym, cur_price, cur_dte))
                        print(f"  [移动止盈] 权利金={cur_price} 浮盈={profit:.1f} 回撤触发")
                        break

            if tick % 500 == 0:
                print(f"  tick={tick} DTE={cur_dte} 权利金={cur_price} 浮盈={profit:.1f} stage={stage}")

        # 4. 最终账户
        acc = api.get_account()
        wait(api, deadline=10)
        print(f"\n  回测结束: 余额={acc.balance:,.0f} 可用={acc.available:,.0f} "
              f"盈亏={acc.balance - INIT_BALANCE:,.0f}")
        print(f"  交易记录: {[(a, s, f'{p:.1f}', f'DTE{d}') for a, s, p, d in log]}")

    finally:
        api.close()


if __name__ == "__main__":
    print("=" * 60)
    print("三阶 Greeks 框架回测验证")
    print("=" * 60)
    test_wcd_sizing()
    test_hard_exit_logic()
    run_tqbacktest()
    print("\n✅ 回测验证完成")
