#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复用扫描结果 CSV，仅执行杠铃组合构建与模拟下单"""
import os
import sys
import pandas as pd
from tqsdk import TqApi, TqAuth
from taleb_scan import build_portfolio, INSURANCE_BUDGET_RATIO, wait
from seller_greeks_system import create_account

account = os.environ.get("TQ_ACCOUNT", "")
password = os.environ.get("TQ_PASSWORD", "")

df = pd.read_csv("/workspace/taleb_scan_result.csv")
sellable = df[df["verdict"] == "SELL_WITH_HEDGE"].to_dict("records")
print(f"可收租候选 {len(sellable)} 个")

api = TqApi(account=create_account(), auth=TqAuth(account, password))
try:
    acc = api.get_account()
    wait(api)
    print(f"账户权益 {acc.balance:,.0f}  可用 {acc.available:,.0f}")
    insurance_budget = acc.balance * INSURANCE_BUDGET_RATIO
    print(f"保险预算≈{insurance_budget:,.0f}")
    print("=" * 70)
    build_portfolio(api, sellable, insurance_budget)
    print("=" * 70)
    wait(api)
    acc = api.get_account()
    pos = api.get_position()
    print(f"\n下单后账户权益 {acc.balance:,.0f}  可用 {acc.available:,.0f}  保证金 {acc.margin:,.0f}")
    print("持仓：")
    for sym, p in pos.items():
        if p.pos_long or p.pos_short:
            print(f"  {sym}: 多{p.pos_long} 空{p.pos_short}  浮盈{p.float_profit:,.0f}")
finally:
    api.close()
