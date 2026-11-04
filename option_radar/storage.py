"""SQLite 持久化：把每次查询到的期权链与关键指标写入本地库。

设计要点：
- 以“分钟”为粒度去重：同一合约同一分钟只保留最新一条，避免自动刷新刷爆表。
- 链级指标单独一张表，便于回看 Gamma Flip / 三墙的历史漂移。
- WAL 模式 + 批量插入，写入不影响读取。
"""

import os
import sqlite3
import time

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS option_snapshot (
    minute      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    product     TEXT NOT NULL,
    expiry      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    underlying  TEXT,
    strike      REAL,
    cp          TEXT,
    last_price  REAL,
    bid         REAL,
    ask         REAL,
    volume      REAL,
    open_interest REAL,
    days        REAL,
    iv          REAL,
    delta       REAL,
    gamma       REAL,
    vega        REAL,
    vanna       REAL,
    gex         REAL,
    gex_share   REAL,
    F           REAL,
    PRIMARY KEY (minute, symbol)
);
CREATE INDEX IF NOT EXISTS idx_snap_chain
    ON option_snapshot(exchange, product, expiry, minute);
CREATE INDEX IF NOT EXISTS idx_snap_symbol
    ON option_snapshot(symbol, minute);

CREATE TABLE IF NOT EXISTS chain_metrics (
    minute      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    product     TEXT NOT NULL,
    expiry      TEXT NOT NULL,
    days        REAL,
    F           REAL,
    underlying  TEXT,
    contracts   INTEGER,
    atm_iv      REAL,
    hv20        REAL,
    hv60        REAL,
    net_gex     REAL,
    call_gex    REAL,
    put_gex     REAL,
    zero_gamma  REAL,
    call_wall   REAL,
    put_wall    REAL,
    gamma_wall  REAL,
    vol_trigger REAL,
    max_pain    REAL,
    pcr_oi      REAL,
    iv_percentile REAL,
    PRIMARY KEY (minute, exchange, product, expiry)
);
CREATE INDEX IF NOT EXISTS idx_metrics_chain
    ON chain_metrics(exchange, product, expiry, minute);
"""

SNAP_COLUMNS = ["symbol", "underlying", "strike", "cp", "last_price", "bid", "ask",
                "volume", "open_interest", "days", "iv", "delta", "gamma", "vega",
                "vanna", "gex", "gex_share", "F"]


def _f(x):
    try:
        v = float(x)
        return v if v == v and abs(v) != float("inf") else None
    except Exception:
        return None


class OptionStore:
    def __init__(self, path: str, retention_days: int = 30):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.rows_written = 0
        self.chains_written = 0
        self.last_write = ""
        self.last_error = ""
        self._stats_cache = None
        self._stats_ts = 0.0
        self.retention_days = retention_days
        try:
            self.purge(retention_days)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)

    # ---------------------------------------------------------------- write
    def save_chain(self, res: dict):
        """写入一次查询结果：合约快照 + 链级指标。失败不影响主流程。"""
        try:
            chain = res.get("chain")
            if chain is None or not len(chain):
                return
            now = time.localtime()
            ts = time.strftime("%Y-%m-%d %H:%M:%S", now)
            minute = time.strftime("%Y-%m-%d %H:%M", now)
            ex, prod, exp = res["exchange"], res["product"], res["expiry"]
            F = _f(res.get("F"))

            rows = []
            for _, r in chain.iterrows():
                rows.append((
                    minute, ts, ex, prod, exp,
                    str(r.get("instrument_id", "")),
                    str(r.get("underlying", "") or ""),
                    _f(r.get("strike")),
                    "C" if str(r.get("option_class", "")).upper().startswith("C") else "P",
                    _f(r.get("last_price")),
                    _f(r.get("bid_price1")),
                    _f(r.get("ask_price1")),
                    _f(r.get("volume")),
                    _f(r.get("open_interest")),
                    _f(r.get("days")),
                    _f(r.get("iv")),
                    _f(r.get("delta")),
                    _f(r.get("gamma")),
                    _f(r.get("vega")),
                    _f(r.get("vanna")),
                    _f(r.get("gex")),
                    _f(r.get("gex_share_pct")),
                    F,
                ))
            self.conn.executemany(
                "INSERT OR REPLACE INTO option_snapshot "
                "(minute, ts, exchange, product, expiry, symbol, underlying, strike, cp, "
                "last_price, bid, ask, volume, open_interest, days, iv, delta, gamma, vega, "
                "vanna, gex, gex_share, F) VALUES (" + ",".join(["?"] * 23) + ")",
                rows)

            m = res.get("metrics") or {}
            kl = m.get("key_levels") or {}

            def lv(name):
                return _f((kl.get(name) or {}).get("strike"))

            self.conn.execute(
                "INSERT OR REPLACE INTO chain_metrics "
                "(minute, ts, exchange, product, expiry, days, F, underlying, contracts, "
                "atm_iv, hv20, hv60, net_gex, call_gex, put_gex, zero_gamma, call_wall, "
                "put_wall, gamma_wall, vol_trigger, max_pain, pcr_oi, iv_percentile) "
                "VALUES (" + ",".join(["?"] * 23) + ")",
                (minute, ts, ex, prod, exp, _f(res.get("days")), F,
                 str(res.get("spot_symbol") or ""), int(m.get("contracts") or 0),
                 _f(m.get("atm_iv")), _f(m.get("hv20")), _f(m.get("hv60")),
                 _f(m.get("net_gex")), _f(m.get("call_gex")), _f(m.get("put_gex")),
                 lv("zero_gamma"), lv("call_wall"), lv("put_wall"), lv("gamma_wall"),
                 lv("vol_trigger"), lv("max_pain"), _f(m.get("pcr_oi")),
                 _f(m.get("iv_percentile")))
            )
            self.conn.commit()
            self.rows_written += len(rows)
            self.chains_written += 1
            self.last_write = ts
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)

    # ---------------------------------------------------------------- read
    def chain_history(self, exchange: str, product: str, expiry: str, limit: int = 500) -> pd.DataFrame:
        cur = self.conn.execute(
            "SELECT * FROM chain_metrics WHERE exchange=? AND product=? AND expiry=? "
            "ORDER BY minute DESC LIMIT ?", (exchange, product, expiry, limit))
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols).iloc[::-1].reset_index(drop=True)

    def stats(self) -> dict:
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        now = time.time()
        if self._stats_cache is None or now - self._stats_ts > 3:
            try:
                rows = self.conn.execute("SELECT COUNT(*) FROM option_snapshot").fetchone()[0]
                chains = self.conn.execute("SELECT COUNT(*) FROM chain_metrics").fetchone()[0]
            except Exception:
                rows, chains = self.rows_written, self.chains_written
            self._stats_cache = (int(rows), int(chains))
            self._stats_ts = now
        rows, chains = self._stats_cache
        return {"size": size, "rows": rows,
                "chains": chains, "last": self.last_write}

    # ---------------------------------------------------------------- upkeep
    def purge(self, days: int):
        if days <= 0:
            return
        cutoff = time.strftime("%Y-%m-%d %H:%M",
                               time.localtime(time.time() - days * 86400))
        self.conn.execute("DELETE FROM option_snapshot WHERE minute < ?", (cutoff,))
        self.conn.execute("DELETE FROM chain_metrics WHERE minute < ?", (cutoff,))
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
