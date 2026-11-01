---
name: "czce-option-vol-monitor"
description: "Computes 20-day realized volatility plus live ATM and wing implied volatility for CZCE options, then returns a short-premium sell/no-sell verdict. Invoke when the user asks whether option IV is expensive, wants an IV-vs-RV check, or plans to sell options."
---

# CZCE 期权波动率监控

单次 TqApi 连接，循环多个标的；计算 RV5/10/20、Parkinson20、平值与 ±7% 翼部 IV、VRP，输出卖方四档判定并落 CSV。

## 何时使用

- 用户问「某合约 IV 贵不贵 / 现在能不能卖期权」
- 需要把 IV 与 20 日已实现波动率对比（VRP）
- 卖波动率前的例行扫描，或收盘后记录日志
- 需要同时看多个合约（甲醇、PTA、白糖等 CZCE 期权）

## 前提

- 解释器：`/workspace/.venv/bin/python`（已装 tqsdk 3.10.2 + numpy；重建见下）
- 凭据：`/workspace/.secrets/tqsdk.env`（chmod 600，已在 .gitignore），格式：

```
TQ_USER=xxx
TQ_PASS=xxx
```

不要把凭据写进技能目录或提交到 git。

重建环境：

```bash
/root/.pyenv/versions/3.12.15/bin/python3 -m venv /workspace/.venv
/workspace/.venv/bin/pip install -U tqsdk numpy
```

## 用法

```bash
# 单合约
/workspace/.venv/bin/python \
  /workspace/.trae/skills/czce-option-vol-monitor/scripts/vol_monitor.py \
  --underlying CZCE.MA612 --auth-file /workspace/.secrets/tqsdk.env

# 多合约，一次 TqApi 连接
... --underlying CZCE.MA612,CZCE.TA612 --auth-file /workspace/.secrets/tqsdk.env

# 不写日志
... --no-log
```

标的格式：`CZCE.<合约>`，如 `CZCE.MA612`、`CZCE.TA612`、`CZCE.SR701`。spread 合约、非期权品种会报「无期权合约」。

## 输出字段

| 字段 | 含义 |
|---|---|
| RV5 / RV10 / RV20 | 收盘对收盘已实现波动率，252 日年化 |
| Parkinson20 | 高低价法，对跳空更稳健 |
| IV(C) / IV(P) / IV(均) | 用 Black-76 从最新价反解的平值隐含波动率 |
| 翼部 IV | 距现价约 ±7% 的认购/认沽 IV 均值 |
| VRP | IV − RV20；翼部 VRP 同理 |
| 跨式 | 平值 C + P 最新价（点） |
| 判定 | 四档结论 |

## 判定阈值（脚本顶部常量）

| 条件 | 结论 |
|---|---|
| 距到期 < 3 天 | 临到期，不新开卖方 |
| IV ≥ 60 且 RV20 < 45 | 一档：半仓铁鹰 |
| IV ≥ 55 且 VRP ≥ 8 | 二档：小仓卖翼，不平值裸卖 |
| IV ≥ 50 | 三档：不做 |
| IV < 50 | 四档：卖方不参与，考虑买方 |

另有翼部溢价 < 10 点时提示「卖翼性价比低」。

## 自动化（crontab）

已安装并验证：每个交易日 15:05（Asia/Shanghai）跑 MA612 + TA612。

```cron
TZ=Asia/Shanghai; 5 15 * * 1-5 cd /workspace && TZ=Asia/Shanghai HTTP_PROXY=http://127.0.0.1:18080 HTTPS_PROXY=http://127.0.0.1:18080 /workspace/.venv/bin/python /workspace/.trae/skills/czce-option-vol-monitor/scripts/vol_monitor.py --underlying CZCE.MA612,CZCE.TA612 --auth-file /workspace/.secrets/tqsdk.env >> /workspace/vol.log 2>&1
```

注意：

- 本沙箱 systemd offline，cron 是手动启动的 `/usr/sbin/cron`；容器重启后需重新启动 cron，或改用真实主机（systemd 正常时会自动拉起）。
- 代理地址是当前沙箱的 `127.0.0.1:18080`；换环境时删掉或改掉两个 PROXY 变量。
- 增删 job 用 `crontab -e`。

## 注意

- 到期日来自 `query_symbol_info` 的 `expire_datetime`，不是按月份猜。
- IV 依赖期权最新价；盘口薄时误差约 1–2 个波动率点，ATM 报价宽时优先看买卖中间价。
- 15:05 跑拿到的是日盘收盘快照；夜盘会继续变动，需要实时评估就盘中重跑。
- 该脚本只做波动率定价，不含墙位/持仓量；墙位需要另算。
- 结果不构成投资建议，卖出期权风险无限（本脚本建议的结构均要求带翼）。
