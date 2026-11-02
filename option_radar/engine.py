"""TQSDK 数据引擎：在后台线程内完成登录、合约发现、行情与日线拉取、指标计算。

UI 线程只通过 submit() 提交任务并轮询 Future，所有 TqApi 调用都发生在同一个后台线程内。
"""

from __future__ import annotations

import math
import queue
import re
import threading
import time
from concurrent.futures import Future
from datetime import datetime

import pandas as pd

from tqsdk import TqApi, TqAuth

import calc

EXCHANGES = {
    "SHFE": "上期所",
    "DCE": "大商所",
    "CZCE": "郑商所",
    "CFFEX": "中金所",
}

# 中金所股指期权没有期货标的，使用指数（优先）或对应股指期货作为现货代理
CFFEX_INDEX = {"IO": "SSE.000300", "HO": "SSE.000016", "MO": "SSE.000852"}
CFFEX_FUTURE = {"IO": "CFFEX.IF", "HO": "CFFEX.IH", "MO": "CFFEX.IM"}

_OPTION_RE = re.compile(
    r"^(?P<prod>[A-Za-z]+)(?P<ym>\d{3,4})(?:MS)?(?:-)?(?P<cp>[CP])(?:-)?(?P<strike>\d+(?:\.\d+)?)$"
)


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _num(x, default=float("nan")) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _timestamp_seconds(value) -> float:
    """兼容秒 / 纳秒两种时间戳。"""
    v = _num(value, 0.0)
    if v <= 0:
        return float("nan")
    if v > 1e11:  # 纳秒
        v = v / 1e9
    return v


class Engine(threading.Thread):
    def __init__(self, config: dict, notify):
        super().__init__(daemon=True)
        self.config = dict(config)
        self.notify = notify
        self.api: TqApi | None = None
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._meta: dict[str, pd.DataFrame] = {}
        self._products: dict[str, list[dict]] = {}
        self._closes: dict[str, list[float]] = {}

    # ---------------------------------------------------------------- runtime
    def submit(self, fn, *args, **kwargs) -> Future:
        fut: Future = Future()
        self._queue.put((fn, args, kwargs, fut))
        return fut

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                fn, args, kwargs, fut = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self.api is not None:
                    try:
                        self.api.wait_update(deadline=time.time() + 0.1)
                    except Exception:
                        pass
                else:
                    self._stop.wait(0.1)
                continue
            try:
                if not fut.set_running_or_notify_cancel():
                    continue
                fut.set_result(fn(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001
                fut.set_exception(exc)
        if self.api is not None:
            try:
                self.api.close()
            except Exception:
                pass

    def _require_api(self) -> TqApi:
        if self.api is None:
            raise RuntimeError("尚未登录天勤，请先点击「账户」填写快期账号。")
        return self.api

    # ---------------------------------------------------------------- connect
    def connect(self, username: str, password: str) -> bool:
        self.notify("status", "正在连接天勤服务器…")
        if self.api is not None:
            try:
                self.api.close()
            except Exception:
                pass
            self.api = None
        self._meta.clear()
        self._products.clear()
        self._closes.clear()
        self.api = TqApi(auth=TqAuth(username, password), disable_print=True, web_gui=False)
        self.notify("status", f"已登录：{self._mask(username)}")
        return True

    @staticmethod
    def _mask(name: str) -> str:
        if len(name) >= 7:
            return name[:3] + "****" + name[-3:]
        return name

    # ---------------------------------------------------------------- metadata
    def load_products(self, exchange: str, refresh: bool = False) -> dict:
        api = self._require_api()
        if not refresh and exchange in self._products:
            return {"exchange": exchange, "products": self._products[exchange]}

        self.notify("status", f"读取 {EXCHANGES.get(exchange, exchange)} 期权合约列表…")
        symbols = list(api.query_quotes(ins_class="OPTION", exchange_id=exchange, expired=False))
        if not symbols and exchange == "CFFEX":
            symbols = self._cffex_option_symbols()
        if not symbols:
            raise RuntimeError(f"{EXCHANGES.get(exchange, exchange)} 未返回任何未下市期权合约。")

        self.notify("status", f"加载 {len(symbols)} 个合约的静态信息…")
        frames = []
        for i in range(0, len(symbols), 3000):
            chunk = symbols[i:i + 3000]
            frames.append(pd.DataFrame(api.query_symbol_info(chunk)))
        info = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        df = self._normalize_info(info)
        if df.empty:
            raise RuntimeError("合约信息为空，无法构建期权链。")

        self._meta[exchange] = df
        self._products[exchange] = self._build_product_summary(df)
        self.notify("status", f"{EXCHANGES.get(exchange, exchange)}：{len(self._products[exchange])} 个品种，"
                              f"{len(df)} 个期权合约")
        return {"exchange": exchange, "products": self._products[exchange]}

    def _cffex_option_symbols(self) -> list[str]:
        api = self._require_api()
        out: list[str] = []
        for index_symbol in CFFEX_INDEX.values():
            try:
                out.extend(list(api.query_options(index_symbol, expired=False)))
            except Exception:
                continue
        return sorted(set(out))

    def _normalize_info(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        out = pd.DataFrame()
        out["instrument_id"] = df.get("instrument_id", pd.Series(dtype=str)).astype(str)
        out["instrument_name"] = df.get("instrument_name", pd.Series(dtype=str)).fillna("").astype(str)
        out["product_id"] = df.get("product_id", pd.Series(dtype=str)).fillna("").astype(str)
        out["underlying"] = df.get("underlying_symbol", pd.Series(dtype=str)).fillna("").astype(str)
        out["strike"] = pd.to_numeric(df.get("strike_price"), errors="coerce")
        out["multiplier"] = pd.to_numeric(df.get("volume_multiple"), errors="coerce").fillna(1.0)
        out["days"] = pd.to_numeric(df.get("expire_rest_days"), errors="coerce")
        out["last_exercise"] = pd.to_numeric(df.get("last_exercise_datetime"), errors="coerce")
        out["expire"] = pd.to_numeric(df.get("expire_datetime"), errors="coerce")
        out["exercise_year"] = pd.to_numeric(df.get("exercise_year"), errors="coerce")
        out["exercise_month"] = pd.to_numeric(df.get("exercise_month"), errors="coerce")

        raw_cp = df.get("option_class", pd.Series(dtype=str)).astype(str).str.upper()
        parsed = out["instrument_id"].map(self._parse_symbol)
        cp = raw_cp.map(lambda s: "CALL" if s.startswith("C") else ("PUT" if s.startswith("P") else ""))
        cp = cp.where(cp != "", parsed.map(lambda d: ("CALL" if d["cp"] == "C" else "PUT") if d else ""))
        out["cp"] = cp
        out["product_id"] = out["product_id"].where(
            out["product_id"] != "", parsed.map(lambda d: d["prod"] if d else ""))
        fallback_under = [
            f"{str(row['instrument_id']).split('.', 1)[0]}.{info['prod']}{info['ym']}" if info else ""
            for row, info in zip(out.to_dict("records"), parsed)
        ]
        out["underlying"] = out["underlying"].where(out["underlying"] != "", pd.Series(fallback_under, index=out.index))

        expiry = out.apply(self._row_expiry, axis=1)
        out["expiry_key"] = [x[0] for x in expiry]
        out["expiry_sort"] = [x[1] for x in expiry]
        out = out[(out["strike"] > 0) & (out["cp"].isin(["CALL", "PUT"])) & (out["expiry_sort"] > 0)]
        out.reset_index(drop=True, inplace=True)
        return out

    @staticmethod
    def _parse_symbol(symbol: str):
        body = symbol.split(".", 1)[-1]
        m = _OPTION_RE.match(body)
        if not m:
            return None
        return {"prod": m.group("prod"), "ym": m.group("ym"), "cp": m.group("cp"),
                "strike": float(m.group("strike"))}

    @staticmethod
    def _row_expiry(row) -> tuple[str, int]:
        y = row.get("exercise_year")
        m = row.get("exercise_month")
        if _finite(y) and _finite(m) and 1 <= int(m) <= 12:
            return f"{int(y) % 100:02d}{int(m):02d}", int(y) * 100 + int(m)
        ts = _timestamp_seconds(row.get("last_exercise"))
        if not _finite(ts):
            ts = _timestamp_seconds(row.get("expire"))
        if _finite(ts):
            dt = datetime.fromtimestamp(ts)
            return f"{dt.year % 100:02d}{dt.month:02d}", dt.year * 100 + dt.month
        info = Engine._parse_symbol(str(row.get("instrument_id", "")))
        if info:
            ym = info["ym"]
            if len(ym) == 4:
                year, month = 2000 + int(ym[:2]), int(ym[2:])
            else:
                # 3 位年月：YMM（如 601 -> 2026-01）
                year = (datetime.now().year // 10) * 10 + int(ym[0])
                month = int(ym[1:])
                if year < datetime.now().year - 5:
                    year += 10
            if 1 <= month <= 12:
                return f"{year % 100:02d}{month:02d}", year * 100 + month
        return "", 0

    @staticmethod
    def _build_product_summary(df: pd.DataFrame) -> list[dict]:
        products = []
        for pid, g in df.groupby("product_id"):
            if not pid:
                continue
            exps = []
            for key, eg in g.groupby("expiry_key"):
                sort_val = int(eg["expiry_sort"].iloc[0])
                days = float(eg["days"].min()) if eg["days"].notna().any() else float("nan")
                exps.append({
                    "key": key,
                    "sort": sort_val,
                    "days": days,
                    "count": int(len(eg)),
                    "underlying": str(eg["underlying"].iloc[0]),
                })
            exps.sort(key=lambda e: e["sort"])
            products.append({
                "product": str(pid),
                "count": int(len(g)),
                "expiries": exps,
            })
        products.sort(key=lambda p: p["product"].lower())
        return products

    # ---------------------------------------------------------------- quotes
    def _wait_quotes(self, quotes: list, timeout: float, min_ratio: float = 0.5) -> bool:
        api = self._require_api()
        deadline = time.time() + timeout
        need = max(1, int(len(quotes) * min_ratio))
        while time.time() < deadline:
            ready = sum(1 for q in quotes if str(getattr(q, "datetime", "") or "").strip())
            if ready >= need:
                return True
            try:
                api.wait_update(deadline=min(time.time() + 0.4, deadline))
            except Exception:
                time.sleep(0.1)
        return False

    @staticmethod
    def _quote_price(q) -> tuple[float, str]:
        bid, ask = _num(getattr(q, "bid_price1", None)), _num(getattr(q, "ask_price1", None))
        if _finite(bid) and _finite(ask) and bid > 0 and ask >= bid:
            return (bid + ask) / 2.0, "中间价"
        for field, label in (("last_price", "最新价"), ("pre_close", "昨收"),
                             ("settlement", "结算价"), ("pre_settlement", "昨结算")):
            v = _num(getattr(q, field, None))
            if _finite(v) and v > 0:
                return v, label
        return float("nan"), ""

    def _resolve_spot(self, underlying: str, exchange: str, product: str,
                      expiry_key: str) -> tuple[str, float]:
        api = self._require_api()
        candidates = []
        if underlying:
            candidates.append(underlying)
        if exchange == "CFFEX":
            if product in CFFEX_INDEX:
                candidates.append(CFFEX_INDEX[product])
            if product in CFFEX_FUTURE:
                candidates.append(f"{CFFEX_FUTURE[product]}{expiry_key}")
                candidates.append(f"KQ.m@{CFFEX_FUTURE[product]}")
        elif underlying == "":
            candidates.append(f"{exchange}.{product}{expiry_key}")

        seen = set()
        for symbol in candidates:
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            try:
                quote = api.get_quote(symbol)
            except Exception:
                continue
            deadline = time.time() + 4
            while time.time() < deadline:
                price, _ = self._quote_price(quote)
                if _finite(price) and price > 0:
                    return symbol, price
                try:
                    api.wait_update(deadline=min(time.time() + 0.4, deadline))
                except Exception:
                    break
        return "", float("nan")

    def _closes_for(self, symbol: str, timeout: float = 10) -> list[float]:
        if not symbol:
            return []
        if symbol in self._closes:
            return self._closes[symbol]
        api = self._require_api()
        try:
            klines = api.get_kline_serial(symbol, 86400, data_length=140)
        except Exception:
            self._closes[symbol] = []
            return []
        deadline = time.time() + timeout
        closes: list[float] = []
        while time.time() < deadline:
            closes = [float(c) for c in klines["close"].tolist() if _finite(c)]
            if len(closes) >= 25:
                break
            try:
                api.wait_update(deadline=min(time.time() + 0.4, deadline))
            except Exception:
                break
        self._closes[symbol] = closes
        return closes

    # ---------------------------------------------------------------- chain
    def query_chain(self, exchange: str, product: str, expiry_key: str,
                    wait: float = 15) -> dict:
        api = self._require_api()
        if exchange not in self._meta:
            self.load_products(exchange)
        meta = self._meta[exchange]
        sub = meta[(meta["product_id"] == product) & (meta["expiry_key"] == expiry_key)]
        if sub.empty:
            raise RuntimeError(f"未找到 {product} {expiry_key} 期权合约。")

        symbols = sub["instrument_id"].tolist()
        self.notify("status", f"订阅 {product} {expiry_key} 行情（{len(symbols)} 个合约）…")
        quotes = {}
        for symbol in symbols:
            try:
                quotes[symbol] = api.get_quote(symbol)
            except Exception:
                continue
        if not quotes:
            raise RuntimeError("行情订阅失败。")
        self._wait_quotes(list(quotes.values()), timeout=wait)

        row0 = sub.iloc[0]
        underlying = str(row0.get("underlying") or "")
        spot_symbol, F = self._resolve_spot(underlying, exchange, product, expiry_key)

        rows = []
        for symbol, quote in quotes.items():
            mrow = sub[sub["instrument_id"] == symbol].iloc[0]
            price, source = self._quote_price(quote)
            strike = _num(getattr(quote, "strike_price", None))
            if not _finite(strike) or strike <= 0:
                strike = float(mrow["strike"])
            cp = str(getattr(quote, "option_class", "") or mrow["cp"]).upper()
            cp = "CALL" if cp.startswith("C") else "PUT"
            if not _finite(price) or price <= 0 or not _finite(strike) or strike <= 0:
                continue
            oi = _num(getattr(quote, "open_interest", None), 0.0)
            oi = oi if _finite(oi) and oi > 0 else 0.0
            mult = _num(getattr(quote, "volume_multiple", None))
            if not _finite(mult) or mult <= 0:
                mult = float(mrow["multiplier"])
            rows.append({
                "instrument_id": symbol,
                "name": str(mrow["instrument_name"]) or symbol.split(".", 1)[-1],
                "option_class": cp,
                "strike": float(strike),
                "price": float(price),
                "price_source": source,
                "bid_price1": _num(getattr(quote, "bid_price1", None)),
                "ask_price1": _num(getattr(quote, "ask_price1", None)),
                "last_price": _num(getattr(quote, "last_price", None)),
                "open_interest": oi,
                "volume": _num(getattr(quote, "volume", None), 0.0),
                "volume_multiple": float(mult),
                "days": float(mrow["days"]) if _finite(mrow["days"]) else float("nan"),
                "underlying": str(getattr(quote, "underlying_symbol", "") or underlying),
            })
        if not rows:
            raise RuntimeError("没有可用的期权报价（休市时可稍后再试）。")

        chain_raw = pd.DataFrame(rows)

        # 现货价格缺失时用 Put-Call Parity 反推 Forward
        if not _finite(F) or F <= 0:
            F = self._infer_forward(chain_raw)
            spot_symbol = spot_symbol or "平价隐含"

        days = float(chain_raw["days"].min()) if chain_raw["days"].notna().any() else 30.0
        if not _finite(days):
            days = 30.0
        T = max(days, 0.5) / 365.0

        chain = calc.compute_chain(chain_raw, F, T, float(self.config.get("risk_free_rate", 0.0)))
        chain = chain.sort_values(["strike", "option_class"]).reset_index(drop=True)

        spot_for_hv = spot_symbol
        closes = self._closes_for(spot_for_hv)
        if not closes and exchange == "CFFEX":
            closes = self._closes_for(f"KQ.m@{CFFEX_FUTURE.get(product, '')}")
        hv20 = calc.hv_from_closes(closes, 20)
        hv60 = calc.hv_from_closes(closes, 60)
        metrics = calc.summary_metrics(chain, F, hv20, hv60)
        gex = calc.gex_table(chain)

        # 合约级 GEX 占比（按行权价净 GEX 份额）
        share_map = {}
        if not gex.empty:
            for r in gex.itertuples(index=False):
                share_map[float(r.strike)] = float(r.share_pct)
        chain["gex_share_pct"] = chain["strike"].map(share_map)

        expiry_days = int(round(days)) if _finite(days) else 0
        return {
            "exchange": exchange,
            "product": product,
            "expiry": expiry_key,
            "days": expiry_days,
            "T": T,
            "F": F,
            "spot_symbol": spot_symbol or "—",
            "underlying": underlying or spot_symbol,
            "chain": chain,
            "gex": gex,
            "metrics": metrics,
        }

    @staticmethod
    def _infer_forward(chain: pd.DataFrame) -> float:
        best = None
        for strike, g in chain.groupby("strike"):
            calls = g[g["option_class"] == "CALL"]["price"]
            puts = g[g["option_class"] == "PUT"]["price"]
            if calls.empty or puts.empty:
                continue
            gap = abs(float(calls.mean()) - float(puts.mean()))
            if best is None or gap < best[0]:
                best = (gap, float(strike) + float(calls.mean()) - float(puts.mean()))
        return best[1] if best else float("nan")

    # ---------------------------------------------------------------- scan
    def scan_exchange(self, exchange: str, refresh: bool = False) -> pd.DataFrame:
        summary = self.load_products(exchange, refresh=refresh)
        products = summary["products"]
        if not products:
            raise RuntimeError("该交易所没有可用期权品种。")
        rows = []
        errors = []
        total = len(products)
        for i, prod in enumerate(products):
            pid = prod["product"]
            exps = [e for e in prod["expiries"] if _finite(e["days"]) and e["days"] >= 0] or prod["expiries"]
            if not exps:
                continue
            expiry = exps[0]
            self.notify("progress", f"扫描 {pid} 到期 {expiry['key']}（{i + 1}/{total}）", i / total)
            try:
                res = self.query_chain(exchange, pid, expiry["key"], wait=10)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{pid}: {exc}")
                continue
            m = res["metrics"]
            rows.append({
                "product": pid,
                "expiry": expiry["key"],
                "days": res["days"],
                "contracts": m["contracts"],
                "F": res["F"],
                "atm_iv": m["atm_iv"],
                "hv20": m["hv20"],
                "hv60": m["hv60"],
                "iv_hv_pct": m["iv_hv_pct"],
                "skew_25d": m["skew_25d"],
                "skew_pct": m["skew_pct"],
                "iv_25d_put": m["iv_25d_put"],
                "iv_25d_call": m["iv_25d_call"],
                "net_gex": m["net_gex"],
                "call_gex": m["call_gex"],
                "put_gex": m["put_gex"],
                "max_gex_strike": m["max_gex_strike"],
                "zero_gamma": m["zero_gamma"],
                "pcr_oi": m["pcr_oi"],
                "iv_percentile": m["iv_percentile"],
                "iv_min": m["iv_min"],
                "iv_max": m["iv_max"],
            })
        self.notify("progress", "扫描完成", 1.0)
        if not rows:
            raise RuntimeError("扫描失败：" + ("；".join(errors[:3]) if errors else "无数据"))
        out = pd.DataFrame(rows)
        total_abs = out["net_gex"].abs().sum()
        out["gex_share_pct"] = (out["net_gex"].abs() / total_abs * 100.0).fillna(0) if total_abs > 0 else 0.0
        out["gex_rank"] = out["net_gex"].abs().rank(ascending=False, method="min").astype(int)
        out = out.sort_values("gex_rank").reset_index(drop=True)
        return out

    def term_structure(self, exchange: str, product: str) -> pd.DataFrame:
        if exchange not in self._products:
            self.load_products(exchange)
        prod = next((p for p in self._products[exchange] if p["product"] == product), None)
        if prod is None:
            raise RuntimeError(f"未找到品种 {product}。")
        exps = prod["expiries"]
        rows = []
        for i, e in enumerate(exps):
            self.notify("progress", f"计算 {product} {e['key']} 偏度（{i + 1}/{len(exps)}）",
                        i / max(len(exps), 1))
            try:
                res = self.query_chain(exchange, product, e["key"], wait=8)
            except Exception:
                continue
            m = res["metrics"]
            rows.append({
                "expiry": e["key"],
                "days": res["days"],
                "F": res["F"],
                "atm_iv": m["atm_iv"],
                "hv20": m["hv20"],
                "iv_hv_pct": m["iv_hv_pct"],
                "skew_25d": m["skew_25d"],
                "skew_pct": m["skew_pct"],
                "iv_25d_put": m["iv_25d_put"],
                "iv_25d_call": m["iv_25d_call"],
                "net_gex": m["net_gex"],
            })
        self.notify("progress", "期限结构完成", 1.0)
        if not rows:
            raise RuntimeError("未获取到任何到期日的期权数据。")
        return pd.DataFrame(rows).sort_values("days").reset_index(drop=True)
