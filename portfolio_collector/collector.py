#!/usr/bin/env python3
"""
Portfolio Collector — Home Assistant Add-on
============================================
Monitors a Trading 212 portfolio of up to 20 holdings, computes drift
from target weights, scores momentum, benchmarks against major indices,
and suggests rebalance trades for manual approval in Home Assistant.

API endpoints
─────────────
GET  /api/health                     Liveness probe
GET  /api/latest-snapshot            Latest data (consumed by HA REST sensors)
GET  /api/snapshots?limit=N          History (default 90 records)
GET  /api/benchmarks?days=N          Benchmark history
POST /api/collect                    Run a full snapshot now
POST /api/approve/{as_of}            Approve rebalance; add ?execute=true to place orders
POST /api/reset-cooldown             Clear rebalance cooldown after a failed execution
POST /api/sync-from-t212             Sync holdings from live T212 portfolio
POST /api/sync-from-t212?preview=true  Dry-run: see what would change without writing
POST /api/set-phase                  Apply a named portfolio phase preset {"phase": "..."}
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
from fastapi import Body, FastAPI, HTTPException

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Runtime constants (never change at runtime) ───────────────────────────────
DB_PATH        = os.getenv("PORT_DB", "/data/portfolio.db")
PORT           = int(os.getenv("PORT", "8000"))
OPTIONS_PATH   = "/data/options.json"

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

# Display order (0 = first). Used as a sort key in templates and the DB.
GROUP_ORDER: dict[str, int] = {
    "momentum_core":      0,
    "global_beta":        1,
    "regional_satellite": 2,
    "defensive":          3,
    "optional_factor":    4,
}

# ── Phase presets ─────────────────────────────────────────────────────────────
# Each phase defines all guard-rail and allocation settings as a single bundle.
# Selecting a phase (via the HA dashboard or POST /api/set-phase) applies the
# full preset; individual options.json values for these keys are overridden.
# To use fully custom settings, set portfolio_phase to any unrecognised string
# (e.g. "Custom") and configure each option individually.
PHASE_SETTINGS: dict[str, dict] = {
    "Momentum-Max": {
        # Early accumulation — maximise growth, accept volatility, long horizon
        "group_allocations": {
            "momentum_core":       35.0,
            "global_beta":         40.0,
            "regional_satellite":  15.0,
            "defensive":            5.0,
            "optional_factor":      5.0,
        },
        "max_cvar_pct":               6.5,   # only fires in genuine crisis
        "cost_rate_pct":              0.10,  # execute momentum tilts freely
        "min_days_between_rebalance": 21,
        "vix_high_threshold":         25.0,
    },
    "Momentum-Chill": {
        # Original portfolio pattern — strong momentum with meaningful regional
        # diversification and a real defensive sleeve; weights derived directly
        # from the default 13-ETF holdings (IWFM+XDEM+XWEM=30, VWRL+SSAC=28,
        # VUSA+IMEU+IJPN+VFEM=22, VAGP+IGLS=16, IWFQ+MVOL=4).
        "group_allocations": {
            "momentum_core":       30.0,
            "global_beta":         28.0,
            "regional_satellite":  22.0,
            "defensive":           16.0,
            "optional_factor":      4.0,
        },
        "max_cvar_pct":               5.5,   # between Max and Balanced Growth
        "cost_rate_pct":              0.10,
        "min_days_between_rebalance": 21,
        "vix_high_threshold":         25.0,
    },
    "Balanced Growth": {
        # Mid-career — meaningful growth with growing volatility cushion
        "group_allocations": {
            "momentum_core":       25.0,
            "global_beta":         38.0,
            "regional_satellite":  17.0,
            "defensive":           15.0,
            "optional_factor":      5.0,
        },
        "max_cvar_pct":               4.5,   # fires in elevated markets
        "cost_rate_pct":              0.10,
        "min_days_between_rebalance": 21,
        "vix_high_threshold":         25.0,
    },
    "Pre-Retirement": {
        # Capital preservation — bonds dominate, momentum bets shrink
        "group_allocations": {
            "momentum_core":       10.0,
            "global_beta":         33.0,
            "regional_satellite":  12.0,
            "defensive":           35.0,
            "optional_factor":     10.0,
        },
        "max_cvar_pct":               3.0,   # proactive defensive tilt
        "cost_rate_pct":              0.20,  # reduce churn near retirement
        "min_days_between_rebalance": 28,    # slower cadence
        "vix_high_threshold":         20.0,  # tighter elevated-market rule
    },
}

# ── T212 ticker → Yahoo Finance symbol mapping ────────────────────────────────
# Ordered longest-suffix-first so more specific rules win.
_T212_EXCHANGE_MAP: list[tuple[str, str]] = [
    ("_EQ_XLON", ".L"),    # London Stock Exchange
    ("_EQ_XETA", ".DE"),   # XETRA Germany
    ("_EQ_XAMS", ".AS"),   # Amsterdam (Euronext NL)
    ("_EQ_XPAR", ".PA"),   # Paris (Euronext FR)
    ("_EQ_XMIL", ".MI"),   # Milan (Borsa Italiana)
    ("_EQ_XMAD", ".MC"),   # Madrid (BME)
    ("_EQ_XSTO", ".ST"),   # Stockholm (Nasdaq Nordic)
    ("_EQ_XCSE", ".CO"),   # Copenhagen
    ("_EQ_XHEL", ".HE"),   # Helsinki
    ("_EQ_XOSL", ".OL"),   # Oslo
    ("_EQ_XNYS", ""),      # NYSE
    ("_EQ_XNAS", ""),      # NASDAQ
    ("_EQ_ARCX", ""),      # NYSE Arca (US ETFs)
    ("_US_EQ",   ""),      # Generic US equity
]

VALID_GROUPS = list(GROUP_ORDER.keys())   # used for sync-from-t212 docs


def _t212_ticker_to_yahoo(t212_ticker: str) -> str:
    """Derive a Yahoo Finance symbol from a Trading 212 instrument ticker.

    Examples:
        VWRL_EQ_XLON  → VWRL.L
        XWEM_EQ_XETA  → XWEM.DE
        AAPL_US_EQ    → AAPL
    """
    for t212_suffix, yahoo_suffix in _T212_EXCHANGE_MAP:
        if t212_ticker.endswith(t212_suffix):
            return t212_ticker[: -len(t212_suffix)] + yahoo_suffix
    # Fallback: strip the final _EXCHANGE segment
    parts = t212_ticker.rsplit("_", 1)
    return parts[0] if len(parts) == 2 else t212_ticker


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

app = FastAPI(title="Portfolio Collector", version="1.6.1")


# ── Options / config loader ───────────────────────────────────────────────────

def _read_options() -> dict:
    """Read the HA add-on options from /data/options.json."""
    if os.path.exists(OPTIONS_PATH):
        try:
            with open(OPTIONS_PATH) as f:
                return json.load(f)
        except Exception as exc:
            log.error(f"Failed to read options.json: {exc}")
    return {}


def _write_options(opts: dict) -> None:
    """Write back to /data/options.json (preserves all non-holdings keys)."""
    with open(OPTIONS_PATH, "w") as f:
        json.dump(opts, f, indent=2)


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

    # Build a lookup so holdings that were saved before v1.4.0 (no "group" field)
    # automatically inherit the correct group from DEFAULT_HOLDINGS.
    _default_group_map = {d["yahoo_symbol"]: d["group"] for d in DEFAULT_HOLDINGS}

    holdings = []
    for h in holdings_raw[:20]:   # hard cap at 20
        sym = h["yahoo_symbol"].strip()
        holdings.append({
            "yahoo_symbol":   sym,
            "t212_ticker":    h["t212_ticker"].strip(),
            "target_weight":  float(h["target_weight"]) / total * 100,
            "purchase_price": float(h.get("purchase_price", 0)),
            "purchase_qty":   float(h.get("purchase_qty", 0)),
            "group":          h.get("group") or _default_group_map.get(sym, "global_beta"),
        })

    # ── Phase preset — drives group allocations and all guard-rail settings ──────
    # When portfolio_phase matches a known preset the full bundle applies.
    # Individual options.json values for the same keys are ignored.
    # Set portfolio_phase to any other string (e.g. "Custom") to fall back to
    # the per-key options values.
    phase        = opts.get("portfolio_phase", "Momentum-Max")
    preset       = PHASE_SETTINGS.get(phase)

    if preset:
        log.info(f"Phase '{phase}' active — applying preset guard-rails and allocations")
        group_allocs   = dict(preset["group_allocations"])
        cfg_max_cvar   = preset["max_cvar_pct"]
        cfg_cost_rate  = preset["cost_rate_pct"]
        cfg_cooldown   = preset["min_days_between_rebalance"]
        cfg_vix_high   = preset["vix_high_threshold"]
    else:
        log.info(f"Phase '{phase}' — using individual options settings")
        group_allocs   = {
            k: float(opts.get("group_allocations", {}).get(k, v))
            for k, v in GROUP_ALLOCATIONS.items()
        }
        cfg_max_cvar   = float(opts.get("max_cvar_pct",  5.0))
        cfg_cost_rate  = float(opts.get("cost_rate_pct", 0.1))
        cfg_cooldown   = int(opts.get("min_days_between_rebalance", 21))
        cfg_vix_high   = float(opts.get("vix_high_threshold", 25))

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
        "portfolio_phase":             phase,
        "drift_threshold_pct":         float(opts.get("drift_threshold_pct", 15)),
        "vix_high_threshold":          cfg_vix_high,
        "vix_extreme_threshold":       float(opts.get("vix_extreme_threshold", 35)),
        "min_days_between_rebalance":  cfg_cooldown,
        "use_group_weights":           cfg_use_group_weights,
        "max_cvar_pct":                cfg_max_cvar,
        "cost_rate_pct":               cfg_cost_rate,
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
    """Place a market order. Only called after explicit approval.
    Positive quantity = BUY, negative quantity = SELL (T212 convention).
    """
    if not cfg["t212_token"]:
        return {"error": "No t212_token configured"}
    payload = {"ticker": t212_ticker, "quantity": round(quantity, 6)}
    log.info(f"T212 order payload: {payload}  base={cfg['t212_base']}")
    try:
        r = requests.post(
            f"{cfg['t212_base']}/api/v0/equity/orders/market",
            headers=_t212_headers(cfg),
            json=payload,
            timeout=20,
        )
        if not r.ok:
            body = r.text[:500]
            log.warning(f"T212 order rejected {r.status_code}: {body}")
            return {"error": f"{r.status_code}", "detail": body}
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
            "group_order":   GROUP_ORDER.get(cfg["symbol_groups"].get(sym, "global_beta"), 9),
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
            "group_order":   GROUP_ORDER.get(cfg["symbol_groups"].get(sym, "global_beta"), 9),
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
    log.info(f"Rebalance: {rebalance_needed} — {rebalance_reason}")
    if suggested_actions:
        for a in suggested_actions:
            flag = " [BALANCING]" if a.get("balancing_trade") else ""
            log.info(f"  Trade: {a['action']:4s} {a['symbol']:10s}  "
                     f"cur={a['current_wt']:.1f}%  tgt={a['target_wt']:.1f}%  "
                     f"£{a['delta_value']:+.0f}  {a['delta_units']:+.6f} units{flag}")

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

    # Require ≥1 integer point in normal markets; ≥2 when VIX is elevated.
    # Trades whenever the rounded actual weight differs from the integer target.
    if vix > vix_high:
        min_gap = 2
        drifted = [p for p in positions if _int_gap(p) >= min_gap]
        if not drifted:
            return False, f"VIX={vix:.1f} elevated — no holding is ≥2 pts off target, holding", []
    else:
        min_gap = 1
        drifted = [p for p in positions if _int_gap(p) >= min_gap]

    if not drifted:
        return False, "No holding is ≥1 integer point from target — no trade needed", []

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
            "delta_units":         round(delta_units, 6),   # signed: +ve=buy, -ve=sell
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
            log.info(f"  Skipped {sym}: cost filter — benefit £{expected_benefit:.2f} ≤ cost £{trade_cost:.2f}, threshold={cost_rate*100:.2f}%")
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
              + (f"; VIX elevated ({vix:.1f}) — ≥2pt gap required" if vix > vix_high else "")
              )
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
        "phase":       cfg.get("portfolio_phase", "Momentum-Max"),
        "version":     "1.6.1",
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


@app.post("/api/reset-cooldown")
def reset_cooldown():
    """Clear the executed flag on all snapshots so the rebalance cooldown resets.
    Use after a failed execution to allow an immediate retry."""
    conn = get_db()
    n = conn.execute("UPDATE snapshots SET executed=0, executed_at=NULL WHERE executed=1").rowcount
    conn.commit()
    conn.close()
    log.info(f"Cooldown reset — cleared executed flag on {n} snapshot(s)")
    return {"reset": True, "snapshots_cleared": n}


@app.post("/api/set-phase")
def set_phase(body: dict = Body(default={})):
    """
    Apply a named portfolio phase preset, writing portfolio_phase to options.json.

    Request body: {"phase": "Momentum-Max"}   (or "Balanced Growth" / "Pre-Retirement")

    The preset immediately sets group allocations, CVaR limit, cost filter,
    rebalance cooldown, and VIX high threshold for the next snapshot.
    The change is also reflected in the HA add-on Configuration tab.

    Returns the full preset so the caller can display what was applied.
    """
    phase = body.get("phase", "").strip()
    if not phase:
        raise HTTPException(400, "Request body must contain {\"phase\": \"<name>\"}")
    if phase not in PHASE_SETTINGS:
        raise HTTPException(
            400,
            f"Unknown phase '{phase}'. Valid values: {list(PHASE_SETTINGS.keys())}"
        )

    opts = _read_options()
    opts["portfolio_phase"] = phase
    try:
        _write_options(opts)
    except Exception as exc:
        raise HTTPException(500, f"Failed to write options.json: {exc}")

    preset = PHASE_SETTINGS[phase]
    log.info(
        f"Phase set to '{phase}': "
        f"CVaR={preset['max_cvar_pct']}%  cost={preset['cost_rate_pct']}%  "
        f"cooldown={preset['min_days_between_rebalance']}d  "
        f"vix_high={preset['vix_high_threshold']}"
    )
    return {"phase": phase, "settings": preset}


@app.post("/api/sync-from-t212")
def sync_from_t212(preview: bool = False):
    """
    Sync the holdings list from the live T212 portfolio.

    Behaviour
    ─────────
    • Existing holdings   — purchase_qty and purchase_price updated from T212.
                            target_weight, group, and yahoo_symbol are preserved.
    • New holdings        — added automatically.  target_weight is set to the
                            holding's actual current weight in the T212 portfolio
                            so the portfolio starts balanced.  group defaults to
                            'global_beta' — edit it in the add-on options UI.
    • Removed holdings    — present in config but zero / absent in T212 (sold).
                            Dropped from the holdings list so they stop affecting
                            rebalance calculations.

    The updated holdings list is written back to /data/options.json, which is
    read by the HA add-on configuration UI — open the add-on options page to
    review and assign groups to any newly discovered holdings.

    Valid group values: momentum_core, global_beta, regional_satellite,
                        defensive, optional_factor

    Pass ?preview=true to see what would change without writing anything.
    """
    cfg = load_config()
    if not cfg["t212_token"]:
        raise HTTPException(400, "t212_token not configured — cannot sync from T212")

    # ── Fetch live positions ───────────────────────────────────────────────────
    try:
        r = requests.get(
            f"{cfg['t212_base']}/api/v0/equity/portfolio",
            headers=_t212_headers(cfg), timeout=20,
        )
        r.raise_for_status()
        t212_positions: list = r.json()
    except Exception as exc:
        raise HTTPException(502, f"T212 portfolio fetch failed: {exc}")

    log.info(f"T212 sync: {len(t212_positions)} position(s) from API")

    # ── Calculate total portfolio value for weight derivation ─────────────────
    # Uses currentPrice if available, falls back to averagePrice.
    total_value = sum(
        float(pos.get("quantity", 0))
        * float(pos.get("currentPrice") or pos.get("averagePrice") or 0)
        for pos in t212_positions
        if float(pos.get("quantity", 0)) > 0
    )

    # ── Build lookup from current config ──────────────────────────────────────
    opts = _read_options()
    existing_holdings      = opts.get("holdings", DEFAULT_HOLDINGS)
    existing_by_t212: dict = {h["t212_ticker"]: h for h in existing_holdings}

    new_holdings: list = []
    updated:      list = []
    added:        list = []
    removed:      list = []
    t212_seen:    set  = set()

    for pos in t212_positions:
        t212_ticker   = pos.get("ticker", "")
        quantity      = float(pos.get("quantity", 0))
        average_price = float(pos.get("averagePrice") or 0)
        current_price = float(pos.get("currentPrice") or average_price)
        fill_date     = pos.get("initialFillDate", "")

        if not t212_ticker or quantity <= 0:
            continue
        t212_seen.add(t212_ticker)

        if t212_ticker in existing_by_t212:
            # ── Known holding — preserve strategy fields, update cost basis ───
            h = dict(existing_by_t212[t212_ticker])
            old_qty   = float(h.get("purchase_qty",   0))
            old_price = float(h.get("purchase_price", 0))
            h["purchase_qty"]   = round(quantity, 6)
            h["purchase_price"] = round(average_price, 4)
            new_holdings.append(h)
            qty_changed   = abs(old_qty   - quantity)      > 0.000001
            price_changed = abs(old_price - average_price) > 0.001
            if qty_changed or price_changed:
                updated.append({
                    "t212_ticker":  t212_ticker,
                    "yahoo_symbol": h["yahoo_symbol"],
                    "old_qty":      round(old_qty,   6),
                    "new_qty":      round(quantity,  6),
                    "old_price":    round(old_price,     4),
                    "new_price":    round(average_price, 4),
                })
                log.info(f"  Sync updated: {t212_ticker}  "
                         f"qty {old_qty:.4f}→{quantity:.4f}  "
                         f"price {old_price:.4f}→{average_price:.4f}")
        else:
            # ── New holding — derive yahoo symbol and calculate actual weight ─
            yahoo_sym    = _t212_ticker_to_yahoo(t212_ticker)
            market_value = quantity * current_price
            actual_wt    = round(market_value / total_value * 100, 2) if total_value else 0.0
            h = {
                "yahoo_symbol":  yahoo_sym,
                "t212_ticker":   t212_ticker,
                "target_weight": actual_wt,   # start balanced at current weight
                "purchase_price": round(average_price, 4),
                "purchase_qty":   round(quantity, 6),
                "group":          "global_beta",  # edit in add-on options UI
            }
            new_holdings.append(h)
            added.append({
                "t212_ticker":   t212_ticker,
                "yahoo_symbol":  yahoo_sym,
                "qty":           round(quantity, 6),
                "avg_price":     round(average_price, 4),
                "target_weight": actual_wt,
                "group":         "global_beta",
                "fill_date":     fill_date,
                "note": (
                    "Added with group='global_beta' and target_weight set to current "
                    "portfolio weight. Edit group in the add-on options UI. "
                    f"Valid groups: {', '.join(VALID_GROUPS)}"
                ),
            })
            log.info(f"  Sync new: {t212_ticker} → {yahoo_sym}  "
                     f"qty={quantity:.4f}  wt={actual_wt:.1f}%  group=global_beta")

    # ── Holdings in config no longer in T212 (sold / closed) ──────────────────
    for h in existing_holdings:
        if h["t212_ticker"] not in t212_seen:
            removed.append({
                "t212_ticker":  h["t212_ticker"],
                "yahoo_symbol": h["yahoo_symbol"],
                "note": "Zero/absent in T212 portfolio — removed from holdings config",
            })
            log.info(f"  Sync removed: {h['t212_ticker']} (not in T212)")

    result = {
        "preview":          preview,
        "as_of":            datetime.now(timezone.utc).isoformat(),
        "t212_positions":   len(t212_seen),
        "holdings_before":  len(existing_holdings),
        "holdings_after":   len(new_holdings),
        "updated":          updated,
        "added":            added,
        "removed":          removed,
    }

    if not preview:
        try:
            opts["holdings"] = new_holdings
            _write_options(opts)
            log.info(
                f"Sync written to options.json — "
                f"{len(new_holdings)} holdings  "
                f"({len(updated)} updated, {len(added)} added, {len(removed)} removed)"
            )
            result["written"] = True
        except Exception as exc:
            raise HTTPException(500, f"Failed to write options.json: {exc}")

    return result


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
    log.info(f"Portfolio Collector v1.6.1 — {len(cfg['target_weights'])} holdings — "
             f"DB: {DB_PATH} — T212: {cfg['t212_base']}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
