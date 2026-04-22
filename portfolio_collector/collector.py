#!/usr/bin/env python3
"""
Portfolio Collector — Home Assistant Add-on
============================================
Monitors a Trading 212 portfolio of up to 20 holdings, computes drift
from target weights, scores momentum, benchmarks against major indices,
and suggests rebalance trades for manual approval in Home Assistant.

API endpoints
─────────────
GET  /api/health               Liveness probe
GET  /api/latest-snapshot      Latest data (consumed by HA REST sensors)
GET  /api/snapshots?limit=N    History (default 90 records)
GET  /api/benchmarks?days=N    Benchmark history
POST /api/collect              Run a full snapshot now
POST /api/approve/{as_of}      Approve rebalance; add ?execute=true to place orders
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests
import uvicorn
import yfinance as yf
from fastapi import FastAPI, HTTPException

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Runtime constants (never change at runtime) ───────────────────────────────
DB_PATH = os.getenv("PORT_DB", "/data/portfolio.db")
PORT    = int(os.getenv("PORT", "8000"))

# ── ETF group definitions ──────────────────────────────────────────────────────
# Group allocations define the neutral portfolio split.
# Within each group holdings are weighted equally unless use_group_weights=false.
GROUP_ALLOCATIONS: dict[str, float] = {
    "momentum_core":       25.0,   # Alpha engine: IWFM, XDEM, XWEM/XMOM
    "global_beta":         40.0,   # Broad market: VWRL, SSAC
    "regional_satellite":  20.0,   # Regional: VUSA, IMEU, IJPN, VFEM
    "defensive":           10.0,   # Bonds: VAGP, IGLS
    "optional_factor":      5.0,   # Quality/MinVol: IWFQ, MVOL
}

GROUP_LABELS: dict[str, str] = {
    "momentum_core":       "Momentum Core",
    "global_beta":         "Global Beta",
    "regional_satellite":  "Regional Satellite",
    "defensive":           "Defensive",
    "optional_factor":     "Optional Factor",
}

# ── Default holdings (used when options.json is absent / first run) ───────────
# Format: yahoo_symbol, t212_ticker, target_weight, purchase_price, purchase_qty, group
DEFAULT_HOLDINGS = [
    {"yahoo_symbol": "VWRL.L",  "t212_ticker": "VWRL_EQ_XLON",  "target_weight": 18.00, "purchase_price": 123.67, "purchase_qty":  7.278609, "group": "global_beta"},
    {"yahoo_symbol": "IWFM.L",  "t212_ticker": "IWFM_EQ_XLON",  "target_weight": 14.01, "purchase_price":  72.41, "purchase_qty":  9.671180, "group": "momentum_core"},
    {"yahoo_symbol": "VAGP.L",  "t212_ticker": "VAGP_EQ_XLON",  "target_weight": 12.00, "purchase_price":  22.50, "purchase_qty": 26.666670, "group": "defensive"},
    {"yahoo_symbol": "XDEM.L",  "t212_ticker": "XDEM_EQ_XLON",  "target_weight": 10.00, "purchase_price":  60.86, "purchase_qty":  8.214227, "group": "momentum_core"},
    {"yahoo_symbol": "SSAC.L",  "t212_ticker": "SSAC_EQ_XLON",  "target_weight": 10.00, "purchase_price":  81.47, "purchase_qty":  6.137982, "group": "global_beta"},
    {"yahoo_symbol": "VUSA.L",  "t212_ticker": "VUSA_EQ_XLON",  "target_weight":  8.00, "purchase_price":  94.25, "purchase_qty":  4.243244, "group": "regional_satellite"},
    {"yahoo_symbol": "XWEM.DE", "t212_ticker": "XWEM_EQ_XETA",  "target_weight":  5.99, "purchase_price":  42.32, "purchase_qty":  7.079663, "group": "momentum_core"},
    {"yahoo_symbol": "IMEU.L",  "t212_ticker": "IMEU_EQ_XLON",  "target_weight":  6.00, "purchase_price":  32.57, "purchase_qty":  9.212345, "group": "regional_satellite"},
    {"yahoo_symbol": "IJPN.L",  "t212_ticker": "IJPN_EQ_XLON",  "target_weight":  4.00, "purchase_price":  16.83, "purchase_qty": 11.883540, "group": "regional_satellite"},
    {"yahoo_symbol": "VFEM.L",  "t212_ticker": "VFEM_EQ_XLON",  "target_weight":  4.00, "purchase_price":  58.03, "purchase_qty":  3.447384, "group": "regional_satellite"},
    {"yahoo_symbol": "IGLS.L",  "t212_ticker": "IGLS_EQ_XLON",  "target_weight":  4.00, "purchase_price": 126.45, "purchase_qty":  1.581903, "group": "defensive"},
    {"yahoo_symbol": "IWFQ.L",  "t212_ticker": "IWFQ_EQ_XLON",  "target_weight":  3.00, "purchase_price":  59.67, "purchase_qty":  2.512984, "group": "optional_factor"},
    {"yahoo_symbol": "MVOL.L",  "t212_ticker": "MVOL_EQ_XLON",  "target_weight":  1.00, "purchase_price":  55.92, "purchase_qty":  0.892925, "group": "optional_factor"},
]

BENCHMARKS = {
    "msci_world": "URTH",
    "ftse_100":   "^FTSE",
    "sp500":      "^GSPC",
    "dow":        "^DJI",
    "vix":        "^VIX",
}

app = FastAPI(title="Portfolio Collector", version="1.4.0")


# ── Options / config loader ───────────────────────────────────────────────────

def _read_options() -> dict:
    """Read the HA add-on options from /data/options.json."""
    path = "/data/options.json"
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as exc:
            log.error(f"Failed to read options.json: {exc}")
    return {}


def _group_based_weights(holdings: list, group_allocs: dict) -> dict[str, float]:
    """
    Derive individual target weights from group allocations.
    Each holding within a group receives an equal share of that group's allocation.
    Example: global_beta=40% with 2 ETFs → each gets 20%.
    """
    counts: dict[str, int] = {}
    for h in holdings:
        g = h.get("group", "global_beta")
        counts[g] = counts.get(g, 0) + 1
    weights = {}
    for h in holdings:
        g    = h.get("group", "global_beta")
        alloc = group_allocs.get(g, 5.0)
        weights[h["yahoo_symbol"]] = alloc / counts[g]
    return weights


def _round_weights_to_integers(raw: dict[str, float]) -> dict[str, int]:
    """
    Round a dict of {symbol: float_%} to whole-number integers that still
    sum to exactly 100, using the largest-remainder (Hamilton) method.
    Prevents fractional targets triggering pointless micro-trades.
    """
    floors     = {sym: int(w) for sym, w in raw.items()}
    remainders = {sym: w - int(w) for sym, w in raw.items()}
    deficit    = 100 - sum(floors.values())
    # Give the remaining 1s to whichever symbols have the largest fractional parts
    for sym in sorted(remainders, key=remainders.__getitem__, reverse=True)[:deficit]:
        floors[sym] += 1
    return floors


def load_config() -> dict:
    """
    Build the runtime config dict from options.json (or defaults).
    Called at the start of every snapshot so weight changes take effect
    immediately without restarting the add-on.
    """
    opts = _read_options()

    holdings_raw = opts.get("holdings", DEFAULT_HOLDINGS)
    if not holdings_raw:
        log.warning("No holdings in options — using built-in defaults")
        holdings_raw = DEFAULT_HOLDINGS

    # Normalise weights to sum to exactly 100
    total = sum(float(h.get("target_weight", 0)) for h in holdings_raw)
    if total <= 0:
        total = 100.0
    if abs(total - 100.0) > 0.5:
        log.warning(f"Target weights sum to {total:.2f}% — normalising to 100%")

    holdings = []
    for h in holdings_raw[:20]:   # hard cap at 20
        holdings.append({
            "yahoo_symbol":   h["yahoo_symbol"].strip(),
            "t212_ticker":    h["t212_ticker"].strip(),
            "target_weight":  float(h["target_weight"]) / total * 100,
            "purchase_price": float(h.get("purchase_price", 0)),
            "purchase_qty":   float(h.get("purchase_qty", 0)),
            "group":          h.get("group", "global_beta"),
        })

    # Build group_allocations (allow per-key overrides from options)
    group_allocs = {
        k: float(opts.get("group_allocations", {}).get(k, v))
        for k, v in GROUP_ALLOCATIONS.items()
    }

    cfg_use_group_weights = bool(opts.get("use_group_weights", False))

    # Round target weights to whole numbers (largest-remainder method keeps sum = 100).
    # Drift and rebalance logic is then relative to clean integer targets, so
    # sub-1% positional noise never triggers unnecessary trades.
    fractional_weights = {h["yahoo_symbol"]: h["target_weight"] for h in holdings}

    if cfg_use_group_weights:
        fractional_weights = _group_based_weights(holdings, group_allocs)

    rounded_weights    = _round_weights_to_integers(fractional_weights)
    log.debug(f"Rounded target weights: {rounded_weights}")

    return {
        "t212_token":                  opts.get("t212_token",  os.getenv("T212_TOKEN", "")).strip(),
        "t212_base":                   opts.get("t212_base",   os.getenv("T212_BASE", "https://demo.trading212.com")).strip(),
        "purchase_date":               opts.get("purchase_date", "2026-04-07"),
        "drift_threshold_pct":         float(opts.get("drift_threshold_pct",         15)),
        "vix_high_threshold":          float(opts.get("vix_high_threshold",           25)),
        "vix_extreme_threshold":       float(opts.get("vix_extreme_threshold",        35)),
        "min_days_between_rebalance":  int(opts.get("min_days_between_rebalance",    21)),
        "use_group_weights":           cfg_use_group_weights,
        "max_cvar_pct":                float(opts.get("max_cvar_pct", 5.0)),
        "cost_rate_pct":               float(opts.get("cost_rate_pct", 0.1)),
        "group_allocations":           group_allocs,
        "holdings":                    holdings,
        # target_weights are whole-number ints — no fractional drift noise
        "target_weights":    rounded_weights,
        "yahoo_to_t212":     {h["yahoo_symbol"]: h["t212_ticker"]    for h in holdings},
        "t212_to_yahoo":     {h["t212_ticker"]:  h["yahoo_symbol"]   for h in holdings},
        "purchase_prices":   {h["yahoo_symbol"]: h["purchase_price"] for h in holdings},
        "purchase_qtys":     {h["yahoo_symbol"]: h["purchase_qty"]   for h in holdings},
        "symbol_groups":     {h["yahoo_symbol"]: h.get("group", "global_beta") for h in holdings},
    }


# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            as_of                TEXT PRIMARY KEY,
            portfolio_value      REAL,
            invested_value       REAL,
            cash                 REAL,
            portfolio_return_pct REAL,
            positions_json       TEXT,
            benchmarks_json      TEXT,
            drift_json           TEXT,
            momentum_json        TEXT,
            rebalance_needed     INTEGER DEFAULT 0,
            rebalance_reason     TEXT,
            suggested_actions    TEXT,
            approved             INTEGER DEFAULT 0,
            approved_at          TEXT,
            executed             INTEGER DEFAULT 0,
            executed_at          TEXT
        );
    """)
    conn.commit()
    conn.close()
    log.info(f"Database ready: {DB_PATH}")


# ── Trading 212 API ───────────────────────────────────────────────────────────

def _t212_headers(cfg: dict) -> dict:
    token = cfg["t212_token"]
    # Accept a raw Base64(keyId:secret) string and add the Basic prefix automatically.
    # If the user accidentally includes the prefix themselves, don't double-add it.
    if token and not token.lower().startswith(("basic ", "bearer ")):
        token = f"Basic {token}"
    return {"Authorization": token, "Content-Type": "application/json"}


def fetch_t212_portfolio(cfg: dict) -> list:
    if not cfg["t212_token"]:
        log.warning("t212_token not set — using cost-basis fallback")
        return _fallback_positions(cfg)
    try:
        r = requests.get(
            f"{cfg['t212_base']}/api/v0/equity/portfolio",
            headers=_t212_headers(cfg), timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        log.info(f"T212 portfolio: {len(data)} position(s)")
        return data
    except Exception as exc:
        log.error(f"T212 portfolio fetch failed: {exc} — using fallback")
        return _fallback_positions(cfg)


def fetch_t212_cash(cfg: dict) -> dict:
    if not cfg["t212_token"]:
        return {"free": 0.0, "total": 0.0, "invested": 0.0, "ppl": 0.0}
    try:
        r = requests.get(
            f"{cfg['t212_base']}/api/v0/equity/account/cash",
            headers=_t212_headers(cfg), timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.error(f"T212 cash fetch failed: {exc}")
        return {"free": 0.0}


def _fallback_positions(cfg: dict) -> list:
    """Synthetic positions from cost-basis data when the API is unreachable."""
    return [
        {
            "ticker":       cfg["yahoo_to_t212"][sym],
            "quantity":     cfg["purchase_qtys"][sym],
            "averagePrice": cfg["purchase_prices"][sym],
            "currentPrice": cfg["purchase_prices"][sym],
            "ppl":          0.0,
        }
        for sym in cfg["target_weights"]
    ]


def place_market_order(cfg: dict, t212_ticker: str, quantity: float) -> dict:
    """Place a market order. Only called after explicit approval."""
    if not cfg["t212_token"]:
        return {"error": "No t212_token configured"}
    try:
        r = requests.post(
            f"{cfg['t212_base']}/api/v0/equity/orders/market",
            headers=_t212_headers(cfg),
            json={"ticker": t212_ticker, "quantity": round(abs(quantity), 6), "timeValidity": "DAY"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


# ── Yahoo Finance ─────────────────────────────────────────────────────────────

def fetch_yahoo_history(symbols: list, period: str = "13mo") -> pd.DataFrame:
    try:
        yf.set_tz_cache_location("/data/yf_cache")
    except Exception:
        pass

    # Attempt 1: batch download (fast)
    try:
        raw = yf.download(symbols, period=period, interval="1d",
                          auto_adjust=True, progress=False, threads=False)
        if not raw.empty:
            closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
            if isinstance(closes, pd.Series):
                closes = closes.to_frame(name=symbols[0])
            result = closes.dropna(how="all")
            if not result.empty:
                log.info(f"Batch Yahoo download OK ({len(result.columns)} symbols)")
                return result
    except Exception as exc:
        log.warning(f"Batch Yahoo download failed ({exc}) — falling back to per-ticker")

    # Attempt 2: one ticker at a time with back-off
    log.info(f"Fetching {len(symbols)} tickers individually...")
    frames = {}
    for sym in symbols:
        for attempt in range(3):
            try:
                hist = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=True)
                if not hist.empty:
                    frames[sym] = hist["Close"].rename(sym)
                    log.info(f"  {sym}: {len(hist)} bars")
                else:
                    log.warning(f"  {sym}: no data returned")
                break
            except Exception as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    log.error(f"  {sym}: failed after 3 attempts — {exc}")
        time.sleep(0.4)

    if not frames:
        log.error("Yahoo Finance returned no data")
        return pd.DataFrame()

    result = pd.DataFrame(frames).dropna(how="all")
    log.info(f"Per-ticker complete: {len(frames)}/{len(symbols)} symbols")
    return result


def _to_gbp(yahoo_price: float, symbol: str, avg_price_gbp: float, eurgbp: float) -> float:
    """
    Normalise a Yahoo Finance price to GBP.

    LSE (.L) securities: Yahoo returns pence (GBp).  We detect this by
    comparing against the known GBP cost-basis: if the Yahoo price is
    more than 50× the GBP avg price it must be in pence → divide by 100.

    Xetra (.DE) securities: Yahoo returns EUR → multiply by EUR/GBP rate.

    All other exchanges: assumed to already be in GBP (or USD-denominated
    instruments held in a GBP account where T212 handles conversion).
    """
    if symbol.endswith(".L"):
        if avg_price_gbp > 0 and yahoo_price > avg_price_gbp * 50:
            log.debug(f"{symbol}: pence detected ({yahoo_price:.1f}p) → £{yahoo_price/100:.2f}")
            return yahoo_price / 100.0
    elif symbol.endswith(".DE"):
        if eurgbp > 0:
            return yahoo_price * eurgbp
    return yahoo_price


def _period_return(series: pd.Series, bars: int) -> Optional[float]:
    s = series.dropna()
    return float((s.iloc[-1] / s.iloc[-bars - 1] - 1) * 100) if len(s) >= bars + 1 else None


def _return_since_date(series: pd.Series, purchase_date_str: str) -> Optional[float]:
    """
    Return % gain from the first available price on-or-after purchase_date_str
    through to the most recent price in the series.
    Used to anchor benchmark comparisons to the portfolio's actual purchase date.
    """
    s = series.dropna()
    if s.empty:
        return None
    try:
        ts = pd.Timestamp(purchase_date_str)
        # Align timezone if index is tz-aware
        if s.index.tz is not None:
            ts = ts.tz_localize(s.index.tz)
        after = s[s.index >= ts]
        if after.empty:
            return None
        return round(float((s.iloc[-1] / after.iloc[0] - 1) * 100), 2)
    except Exception as exc:
        log.warning(f"_return_since_date({purchase_date_str}): {exc}")
        return None


def _momentum(series: pd.Series, lookback_bars: int, skip_bars: int = 21) -> Optional[float]:
    s = series.dropna()
    if len(s) < lookback_bars + skip_bars:
        return None
    return float((s.iloc[-skip_bars] / s.iloc[-lookback_bars - skip_bars] - 1) * 100)


def _ema_trend(series: pd.Series, fast: int = 20, slow: int = 50) -> str:
    s = series.dropna()
    if len(s) < slow:
        return "neutral"
    f = float(s.ewm(span=fast, adjust=False).mean().iloc[-1])
    sl = float(s.ewm(span=slow, adjust=False).mean().iloc[-1])
    if f > sl * 1.005:
        return "bullish"
    if f < sl * 0.995:
        return "bearish"
    return "neutral"


def _rs_vs_world(holding: pd.Series, world: pd.Series, bars: int = 63) -> Optional[float]:
    common = holding.dropna().index.intersection(world.dropna().index)
    if len(common) < bars:
        return None
    h = holding.loc[common]
    b = world.loc[common]
    return round(float(h.iloc[-1] / h.iloc[-bars] - 1) * 100 - float(b.iloc[-1] / b.iloc[-bars] - 1) * 100, 2)


def _wma_trend_score(price_series: pd.Series, lookback: int = 126) -> float:
    """
    Linearly-weighted moving average of returns divided by volatility.
    Gives a dimensionless trend signal: positive = uptrend, negative = downtrend.
    Lookback of 126 bars ≈ 6 months (optimal for momentum persistence).
    """
    s = price_series.dropna()
    if len(s) < lookback + 2:
        return 0.0
    rets = s.pct_change().dropna().iloc[-lookback:]
    if len(rets) < 10:
        return 0.0
    vol = float(rets.std())
    if vol <= 0:
        return 0.0
    weights = np.linspace(1.0, 2.0, len(rets))
    weights /= weights.sum()
    return round(float(np.dot(rets.values, weights)) / vol, 4)


def _portfolio_cvar(weights_pct: dict, hist_df: pd.DataFrame, alpha: float = 0.95) -> float:
    """
    Historical CVaR (Expected Shortfall) at alpha confidence level.
    weights_pct: {symbol: float_%} — need not sum to 100.
    Returns the expected daily loss in the worst (1-alpha)% of trading days.
    A value of 0.02 means the portfolio loses ≥2% on its worst days on average.
    """
    syms = [s for s in weights_pct if s in hist_df.columns and weights_pct.get(s, 0) > 0]
    if not syms:
        return 0.0
    rets = hist_df[syms].pct_change().dropna()
    if len(rets) < 30:
        return 0.0
    w = np.array([weights_pct[s] for s in syms], dtype=float)
    w /= w.sum()
    port_rets = rets[syms].values @ w
    cutoff = max(int((1.0 - alpha) * len(port_rets)), 1)
    tail = np.sort(port_rets)[:cutoff]
    return round(float(-tail.mean()), 6)


# ── Core snapshot ─────────────────────────────────────────────────────────────

def compute_snapshot() -> dict:
    # Reload config on every run so UI changes take effect without restart
    cfg = load_config()
    log.info(f"=== Snapshot started — {len(cfg['target_weights'])} holdings, "
             f"T212={cfg['t212_base']} ===")

    raw_positions  = fetch_t212_portfolio(cfg)
    cash_data      = fetch_t212_cash(cfg)
    t212_by_ticker = {p["ticker"]: p for p in raw_positions if isinstance(p, dict)}

    # Include EUR/GBP rate for any Xetra (.DE) holdings
    all_symbols = list(cfg["target_weights"].keys()) + list(BENCHMARKS.values()) + ["EURGBP=X"]
    hist = fetch_yahoo_history(all_symbols)

    # EUR/GBP spot rate for converting Xetra prices
    eurgbp = 1.0
    if "EURGBP=X" in hist.columns:
        s = hist["EURGBP=X"].dropna()
        if not s.empty:
            eurgbp = float(s.iloc[-1])
            log.info(f"EUR/GBP rate: {eurgbp:.4f}")

    # Build positions
    positions   = []
    total_value = 0.0

    for sym, target_wt in cfg["target_weights"].items():
        t212_tick = cfg["yahoo_to_t212"][sym]
        t212_pos  = t212_by_ticker.get(t212_tick, {})

        qty       = float(t212_pos.get("quantity",     cfg["purchase_qtys"][sym]))
        avg_price = float(t212_pos.get("averagePrice", cfg["purchase_prices"][sym]))

        # ── Current price: T212 first (always in account currency = GBP),
        #    Yahoo as fallback with pence/EUR normalisation.
        #
        #    WHY: Yahoo Finance returns LSE (.L) prices in pence (GBp),
        #    not pounds — VWRL.L shows ~12,500 not ~125.  T212's API
        #    already converts to the account currency so it is always correct.
        t212_current = float(t212_pos["currentPrice"]) if t212_pos.get("currentPrice") else 0.0
        if t212_current > 0:
            current_price = t212_current
        elif sym in hist.columns and not hist[sym].dropna().empty:
            current_price = _to_gbp(float(hist[sym].dropna().iloc[-1]), sym, avg_price, eurgbp)
        else:
            current_price = avg_price

        market_value = qty * current_price
        cost_basis   = qty * avg_price
        pnl_pct      = (current_price / avg_price - 1) * 100 if avg_price else 0.0

        log.debug(f"{sym}: qty={qty:.4f}  avg=£{avg_price:.4f}  "
                  f"cur=£{current_price:.4f}  val=£{market_value:.2f}")

        total_value += market_value
        positions.append({
            "symbol":        sym,
            "t212_ticker":   t212_tick,
            "quantity":      round(qty, 6),
            "avg_price":     round(avg_price, 4),
            "current_price": round(current_price, 4),
            "market_value":  round(market_value, 2),
            "cost_basis":    round(cost_basis, 2),
            "pnl_pct":       round(pnl_pct, 2),
            "group":         cfg["symbol_groups"].get(sym, "global_beta"),
            "target_wt":     round(target_wt, 2),
        })

    cash = float(cash_data.get("free", 0.0))
    total_value += cash

    for p in positions:
        actual_wt      = p["market_value"] / total_value * 100 if total_value else 0.0
        drift_abs      = actual_wt - p["target_wt"]
        drift_rel      = drift_abs / p["target_wt"] * 100 if p["target_wt"] else 0.0
        p["actual_wt"] = round(actual_wt, 2)
        p["drift_abs"] = round(drift_abs, 2)
        p["drift_rel"] = round(drift_rel, 2)

    max_drift_rel = max(abs(p["drift_rel"]) for p in positions) if positions else 0.0
    total_cost    = sum(p["cost_basis"] for p in positions)
    portfolio_return_pct = (total_value - total_cost) / total_cost * 100 if total_cost else 0.0

    # Group allocation summary
    group_summary: dict[str, dict] = {}
    for p in positions:
        g = p.get("group", "global_beta")
        if g not in group_summary:
            group_summary[g] = {
                "label":     GROUP_LABELS.get(g, g),
                "actual_wt": 0.0,
                "target_wt": 0.0,
            }
        group_summary[g]["actual_wt"] = round(group_summary[g]["actual_wt"] + p["actual_wt"], 2)
        group_summary[g]["target_wt"] = round(group_summary[g]["target_wt"] + p["target_wt"], 2)

    # Benchmarks
    benchmarks    = {}
    world_series  = hist.get("URTH")
    purchase_date = cfg.get("purchase_date", "2026-04-07")
    for name, ticker in BENCHMARKS.items():
        if ticker not in hist.columns:
            continue
        s = hist[ticker].dropna()
        if s.empty:
            continue
        since_purchase = _return_since_date(s, purchase_date)
        benchmarks[name] = {
            "ticker":               ticker,
            "latest":               round(float(s.iloc[-1]), 2),
            "return_1d":            round(_period_return(s, 1)   or 0.0, 2),
            "return_1w":            round(_period_return(s, 5)   or 0.0, 2),
            "return_1m":            round(_period_return(s, 21)  or 0.0, 2),
            "return_3m":            round(_period_return(s, 63)  or 0.0, 2),
            "return_6m":            round(_period_return(s, 126) or 0.0, 2),
            "return_since_purchase": since_purchase if since_purchase is not None else 0.0,
        }
        log.info(f"Benchmark {name}: since_purchase={since_purchase}%  1m={benchmarks[name]['return_1m']}%")

    vix = benchmarks.get("vix", {}).get("latest", 0.0)

    # Momentum
    momentum = {}
    for sym in cfg["target_weights"]:
        if sym not in hist.columns:
            momentum[sym] = {}
            continue
        s  = hist[sym].dropna()
        rs = _rs_vs_world(s, world_series, 63) if world_series is not None else None
        momentum[sym] = {
            "momentum_12m":  round(v, 2) if (v := _momentum(s, 252))    is not None else None,
            "momentum_9m":   round(v, 2) if (v := _momentum(s, 189))    is not None else None,
            "momentum_6m":   round(v, 2) if (v := _momentum(s, 126))    is not None else None,
            "momentum_3m":   round(v, 2) if (v := _momentum(s, 63, 0))  is not None else None,
            "trend":         _ema_trend(s),
            "trend_score":   _wma_trend_score(s, 126),   # WMA signal for weight tilting
            "return_1m":     round(_period_return(s, 21) or 0.0, 2),
            "return_3m":     round(_period_return(s, 63) or 0.0, 2),
            "rs_vs_world_3m": rs,
            "group":         cfg["symbol_groups"].get(sym, "global_beta"),
        }

    # Blended score: 50% WMA trend (scaled to % range), 30% 6m momentum, 20% 12m momentum.
    # WMA trend score is dimensionless (~±0.05); multiply by 100 to match % momentum scale.
    mom_scores: dict[str, float] = {}
    for sym, m in momentum.items():
        if not m:
            mom_scores[sym] = 0.0
            continue
        trend = (m.get("trend_score") or 0.0) * 100
        m6    =  m.get("momentum_6m")  or 0.0
        m12   =  m.get("momentum_12m") or 0.0
        mom_scores[sym] = round(trend * 0.5 + m6 * 0.3 + m12 * 0.2, 2)

    rebalance_needed, rebalance_reason, suggested_actions = _compute_rebalance(
        cfg, positions, mom_scores, momentum, vix, total_value, max_drift_rel, hist
    )

    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn  = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO snapshots
          (as_of, portfolio_value, invested_value, cash, portfolio_return_pct,
           positions_json, benchmarks_json, drift_json, momentum_json,
           rebalance_needed, rebalance_reason, suggested_actions, approved, executed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)
    """, (
        as_of,
        round(total_value, 2),
        round(total_cost, 2),
        round(cash, 2),
        round(portfolio_return_pct, 2),
        json.dumps(positions),
        json.dumps(benchmarks),
        json.dumps([{"symbol": p["symbol"], "drift_abs": p["drift_abs"], "drift_rel": p["drift_rel"]} for p in positions]),
        json.dumps(momentum),
        1 if rebalance_needed else 0,
        rebalance_reason,
        json.dumps(suggested_actions),
    ))
    conn.commit()
    conn.close()

    log.info(f"Snapshot saved: £{total_value:.2f}  return={portfolio_return_pct:.2f}%  "
             f"max_drift={max_drift_rel:.1f}%  rebalance={rebalance_needed}  vix={vix}")

    return {
        "as_of": as_of, "portfolio_value": round(total_value, 2),
        "invested_value": round(total_cost, 2), "cash": round(cash, 2),
        "portfolio_return_pct": round(portfolio_return_pct, 2),
        "positions": positions, "benchmarks": benchmarks, "momentum": momentum,
        "rebalance_needed": rebalance_needed, "rebalance_reason": rebalance_reason,
        "suggested_actions": suggested_actions, "vix": vix, "approved": False,
        "group_summary": group_summary,
    }


def _compute_rebalance(cfg, positions, mom_scores, momentum, vix, total_value, max_drift_rel, hist=None):
    vix_high      = cfg["vix_high_threshold"]
    vix_extreme   = cfg["vix_extreme_threshold"]
    cooldown_days = cfg["min_days_between_rebalance"]

    # Cooldown
    conn = get_db()
    row  = conn.execute("SELECT approved_at FROM snapshots WHERE executed=1 ORDER BY executed_at DESC LIMIT 1").fetchone()
    conn.close()
    if row and row["approved_at"]:
        try:
            days = (datetime.now(timezone.utc) - datetime.fromisoformat(row["approved_at"].replace("Z", "+00:00"))).days
            if days < cooldown_days:
                return False, f"Cooldown: {days}d since last rebalance (min {cooldown_days}d)", []
        except Exception:
            pass

    if vix > vix_extreme:
        return False, f"VIX={vix:.1f} — extreme volatility, rebalancing frozen", []

    # Integer-rounding rule: only trade holdings whose rounded actual weight
    # differs from the integer target. This avoids micro-trades caused by
    # sub-1% positional noise.
    #   Normal market  — trade if round(actual) ≠ target  (any integer mismatch)
    #   VIX elevated   — trade only if gap ≥ 2 whole points (more conservative)
    def _int_gap(p):
        return abs(round(p["actual_wt"]) - int(p["target_wt"]))

    # Require ≥2 integer points in normal markets; ≥3 when VIX is elevated.
    # A single rounding tick (e.g. 18%→19%) is noise — wait for genuine drift.
    if vix > vix_high:
        min_gap = 3
        drifted = [p for p in positions if _int_gap(p) >= min_gap]
        if not drifted:
            return False, f"VIX={vix:.1f} elevated — no holding is ≥3 pts off target, holding", []
    else:
        min_gap = 2
        drifted = [p for p in positions if _int_gap(p) >= min_gap]

    if not drifted:
        return False, "No holding is ≥2 integer points from target — no trade needed", []

    adj_weights = _momentum_adjusted_weights(cfg["target_weights"], mom_scores, vix, vix_high)

    # ── CVaR constraint ───────────────────────────────────────────────────────
    # If portfolio tail risk exceeds the configured limit, scale back non-defensive
    # holdings and redistribute weight to defensive ETFs.
    max_cvar = cfg.get("max_cvar_pct", 5.0) / 100.0
    if hist is not None and not hist.empty and max_cvar > 0:
        cvar = _portfolio_cvar(adj_weights, hist)
        if cvar > max_cvar:
            defensive_syms = {sym for sym, g in cfg["symbol_groups"].items() if g == "defensive"}
            scale = max_cvar / cvar
            scaled = {sym: (w if sym in defensive_syms else w * scale)
                      for sym, w in adj_weights.items()}
            total_s = sum(scaled.values())
            adj_weights = {sym: v / total_s * 100 for sym, v in scaled.items()}
            log.info(f"CVaR={cvar:.4f} > limit={max_cvar:.4f} — defensive tilt applied")

    traded_syms = set()

    def _make_action(p, delta_val, balancing=False):
        sym         = p["symbol"]
        delta_units = delta_val / p["current_price"] if p["current_price"] else 0.0
        return {
            "symbol":              sym,
            "t212_ticker":         p["t212_ticker"],
            "action":              "BUY" if delta_val > 0 else "SELL",
            "current_wt":          p["actual_wt"],
            "target_wt":           round(adj_weights[sym], 2),
            "original_target_wt":  int(cfg["target_weights"][sym]),
            "delta_value":         round(delta_val, 2),
            "delta_units":         round(abs(delta_units), 6),
            "current_value":       round(p["market_value"], 2),
            "target_value":        round(total_value * adj_weights[sym] / 100, 2),
            "drift_rel":           p["drift_rel"],
            "momentum_score":      mom_scores.get(sym, 0.0),
            "balancing_trade":     balancing,
        }

    actions = []
    # Primary trades: holdings that have crossed an integer boundary
    cost_rate = cfg.get("cost_rate_pct", 0.1) / 100.0
    for p in positions:
        if _int_gap(p) < min_gap:
            continue
        sym       = p["symbol"]
        delta_val = total_value * adj_weights[sym] / 100 - p["market_value"]
        if abs(delta_val) < 10.0:
            continue
        # Transaction cost filter: skip trades where benefit ≤ cost
        trade_cost       = abs(delta_val) * cost_rate
        expected_benefit = abs(delta_val) * abs(p["drift_rel"]) / 100.0
        if expected_benefit <= trade_cost:
            log.debug(f"{sym}: cost filter — benefit £{expected_benefit:.2f} ≤ cost £{trade_cost:.2f}")
            continue
        actions.append(_make_action(p, delta_val))
        traded_syms.add(sym)

    # Balancing pass: the drifted trades may net to a cash surplus (all sells) or
    # deficit (all buys). Add one counterpart trade from the untouched holding
    # that is furthest from its target in the opposite direction, sized to absorb
    # exactly the imbalance so total buys ≈ total sells.
    net = sum(a["delta_value"] for a in actions)
    if abs(net) >= 10.0:
        untouched = [p for p in positions if p["symbol"] not in traded_syms]
        if net < 0:
            # Net sell — find the most under-weighted holding to absorb freed cash
            best = max(untouched, key=lambda p: p["target_wt"] - p["actual_wt"], default=None)
        else:
            # Net buy — find the most over-weighted holding to fund the purchase
            best = max(untouched, key=lambda p: p["actual_wt"] - p["target_wt"], default=None)
        if best:
            # Size the balancing trade to cancel the net exactly (not to full target)
            bal_delta = -net
            actions.append(_make_action(best, bal_delta, balancing=True))

    actions.sort(key=lambda x: (x["action"] != "SELL", -abs(x["delta_value"])))
    drifted_summary = ", ".join(
        f"{p['symbol']} ({p['actual_wt']:.1f}%→{int(p['target_wt'])}%)"
        for p in drifted
    )
    reason = (f"{len(drifted)} holding(s) crossed integer target: {drifted_summary}"
              + (f"; VIX elevated ({vix:.1f}) — ≥2pt gap required" if vix > vix_high else ""))
    return True, reason, actions


def _momentum_adjusted_weights(target_weights, mom_scores, vix, vix_high):
    dampen = 0.5 if vix > vix_high else 1.0
    raw = {}
    for sym, base_wt in target_weights.items():
        score = mom_scores.get(sym, 0.0)
        if   score > 10: mult = 1.0 + 0.20 * dampen
        elif score >  5: mult = 1.0 + 0.10 * dampen
        elif score < -10: mult = 1.0 - 0.20 * dampen
        elif score <  -5: mult = 1.0 - 0.10 * dampen
        else:             mult = 1.0
        raw[sym] = base_wt * mult
    total = sum(raw.values())
    return {sym: v / total * 100 for sym, v in raw.items()}


# ── FastAPI routes ────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    cfg = _read_options()
    return {
        "status":      "ok",
        "utc":         datetime.now(timezone.utc).isoformat(),
        "t212_base":   cfg.get("t212_base", "https://demo.trading212.com"),
        "demo_mode":   "demo" in cfg.get("t212_base", "demo"),
        "holdings":    len(cfg.get("holdings", DEFAULT_HOLDINGS)),
        "version":     "1.4.0",
    }


@app.get("/api/latest-snapshot")
def latest_snapshot():
    conn = get_db()
    row  = conn.execute("SELECT * FROM snapshots ORDER BY as_of DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "No snapshots yet. POST /api/collect to run the first one.")
    return _row_to_dict(row)


@app.get("/api/snapshots")
def list_snapshots(limit: int = 90):
    conn = get_db()
    rows = conn.execute("SELECT * FROM snapshots ORDER BY as_of DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


@app.post("/api/collect")
def trigger_collect():
    """Trigger a full snapshot. Called daily by HA automation at market close."""
    return compute_snapshot()


@app.post("/api/approve/{as_of}")
def approve_rebalance(as_of: str, execute: bool = False):
    """
    Approve the rebalance plan for the given snapshot timestamp.
    Pass ?execute=true to also submit orders to T212 (live mode only).
    """
    conn = get_db()
    row  = conn.execute("SELECT * FROM snapshots WHERE as_of=?", (as_of,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"Snapshot {as_of} not found")

    approved_at = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE snapshots SET approved=1, approved_at=? WHERE as_of=?", (approved_at, as_of))
    conn.commit()

    execution_results = []
    if execute:
        cfg     = load_config()
        actions = json.loads(row["suggested_actions"] or "[]")
        for action in actions:
            result = place_market_order(cfg, action["t212_ticker"], action["delta_units"])
            execution_results.append({"action": action, "result": result})
            log.info(f"Order: {action['action']} {action['delta_units']} {action['t212_ticker']} → {result}")
        conn.execute("UPDATE snapshots SET executed=1, executed_at=? WHERE as_of=?",
                     (datetime.now(timezone.utc).isoformat(), as_of))
        conn.commit()

    conn.close()
    return {"approved": True, "approved_at": approved_at,
            "executed": execute, "execution_results": execution_results}


@app.get("/api/benchmarks")
def benchmark_history(days: int = 90):
    conn = get_db()
    rows = conn.execute("SELECT as_of, benchmarks_json FROM snapshots ORDER BY as_of DESC LIMIT ?", (days,)).fetchall()
    conn.close()
    return [{"as_of": r["as_of"], "benchmarks": json.loads(r["benchmarks_json"] or "{}")} for r in rows]


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for f in ["positions_json", "benchmarks_json", "drift_json", "momentum_json", "suggested_actions"]:
        key = f.replace("_json", "")
        d[key] = json.loads(d.pop(f) or ("[]" if f == "suggested_actions" else "{}"))
    # Derive group_summary from positions (avoids DB schema change)
    if "positions" in d and isinstance(d["positions"], list):
        gs: dict = {}
        for p in d["positions"]:
            g = p.get("group", "global_beta")
            if g not in gs:
                gs[g] = {"label": GROUP_LABELS.get(g, g), "actual_wt": 0.0, "target_wt": 0.0}
            gs[g]["actual_wt"] = round(gs[g]["actual_wt"] + p.get("actual_wt", 0.0), 2)
            gs[g]["target_wt"] = round(gs[g]["target_wt"] + p.get("target_wt", 0.0), 2)
        d["group_summary"] = gs
    return d


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    cfg = load_config()
    log.info(f"Portfolio Collector v1.4.0 — {len(cfg['target_weights'])} holdings — "
             f"DB: {DB_PATH} — T212: {cfg['t212_base']}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
