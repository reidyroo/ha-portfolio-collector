#!/usr/bin/env python3
"""
Portfolio Collector — Home Assistant Add-on v2.7.2
===================================================
Monitors a Trading 212 portfolio, computing drift from target weights,
scoring momentum, benchmarking against major indices, and suggesting
rebalance trades for manual approval in Home Assistant.

T212 is the source of truth — no holdings list in config.  Positions are
fetched live on every snapshot; instruments are matched via the T212
catalog (cached in SQLite) and group labels are stored in the database.

API endpoints
─────────────
GET  /api/health                         Liveness probe
GET  /api/latest-snapshot                Latest data (consumed by HA REST sensors)
GET  /api/snapshots?limit=N              History (default 90 records)
GET  /api/snapshots?summary=true         Lightweight list: as_of + portfolio_value only
DELETE /api/snapshots?date=YYYY-MM-DD    Delete all snapshots for a given date
GET  /api/benchmarks?days=N              Benchmark history
POST /api/collect                        Run a full snapshot now
POST /api/approve/{as_of}               Approve rebalance; ?execute=true places orders
POST /api/reset-cooldown                 Clear rebalance cooldown after failed execution
POST /api/set-phase                      Apply a named portfolio phase preset
GET  /api/groups                         List all instrument group assignments
POST /api/groups/{ticker}               Set group for an instrument {"group": "..."}
GET  /api/catalog/status                 Catalog cache info
POST /api/catalog/refresh                Force catalog re-fetch (rate-limited)
GET  /groups                             HTML group-management UI (ingress panel)
"""

import json
import logging
import os
import sqlite3
import time
import math
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests
import uvicorn
import yfinance as yf
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Runtime constants ─────────────────────────────────────────────────────────
DB_PATH      = os.getenv("PORT_DB", "/data/portfolio.db")
PORT         = int(os.getenv("PORT", "8000"))
OPTIONS_PATH = "/data/options.json"

# ── ETF group definitions ─────────────────────────────────────────────────────
GROUP_ALLOCATIONS: dict[str, float] = {
    "momentum_core":       25.0,
    "global_beta":         40.0,
    "regional_satellite":  20.0,
    "defensive":           10.0,
    "optional_factor":      5.0,
}

GROUP_LABELS: dict[str, str] = {
    "momentum_core":       "Momentum Core",
    "global_beta":         "Global Beta",
    "regional_satellite":  "Regional Satellite",
    "defensive":           "Defensive",
    "optional_factor":     "Optional Factor",
    "unassigned":          "Unassigned",
}

GROUP_ORDER: dict[str, int] = {
    "momentum_core":      0,
    "global_beta":        1,
    "regional_satellite": 2,
    "defensive":          3,
    "optional_factor":    4,
    "unassigned":         9,
}

VALID_GROUPS = [
    "momentum_core", "global_beta", "regional_satellite",
    "defensive", "optional_factor", "unassigned",
]

# ── Phase presets ─────────────────────────────────────────────────────────────
PHASE_SETTINGS: dict[str, dict] = {
    "Momentum-Max": {
        "group_allocations": {
            "momentum_core":       35.0,
            "global_beta":         40.0,
            "regional_satellite":  15.0,
            "defensive":            5.0,
            "optional_factor":      5.0,
        },
        "max_cvar_pct":               6.5,
        "cost_rate_pct":              0.10,
        "min_days_between_rebalance": 21,
        "vix_high_threshold":         25.0,
    },
    "Momentum-Chill": {
        "group_allocations": {
            "momentum_core":       30.0,
            "global_beta":         28.0,
            "regional_satellite":  22.0,
            "defensive":           16.0,
            "optional_factor":      4.0,
        },
        "max_cvar_pct":               5.5,
        "cost_rate_pct":              0.10,
        "min_days_between_rebalance": 21,
        "vix_high_threshold":         25.0,
    },
    "Balanced Growth": {
        "group_allocations": {
            "momentum_core":       25.0,
            "global_beta":         38.0,
            "regional_satellite":  17.0,
            "defensive":           15.0,
            "optional_factor":      5.0,
        },
        "max_cvar_pct":               4.5,
        "cost_rate_pct":              0.10,
        "min_days_between_rebalance": 21,
        "vix_high_threshold":         25.0,
    },
    "Pre-Retirement": {
        "group_allocations": {
            "momentum_core":       10.0,
            "global_beta":         33.0,
            "regional_satellite":  12.0,
            "defensive":           35.0,
            "optional_factor":     10.0,
        },
        "max_cvar_pct":               3.0,
        "cost_rate_pct":              0.20,
        "min_days_between_rebalance": 28,
        "vix_high_threshold":         20.0,
    },
}

BENCHMARKS = {
    "msci_world": "URTH",
    "ftse_100":   "^FTSE",
    "sp500":      "^GSPC",
    "dow":        "^DJI",
    "vix":        "^VIX",
}

# Phase positions on a 0–100 risk axis used by weight_mode="dynamic".
# 0 = ultra-defensive, 100 = maximum aggression.
# These anchor the four phase presets along the risk continuum; intermediate
# risk_score values produce linearly-interpolated group allocations.
PHASE_RISK_ANCHORS: dict[str, float] = {
    "Pre-Retirement":   15.0,
    "Balanced Growth":  45.0,
    "Momentum-Chill":   65.0,
    "Momentum-Max":     90.0,
}

# ── Exchange → Yahoo Finance suffix ──────────────────────────────────────────
# Keyed by T212 exchange ID (from instrument catalog or ticker suffix).
_EXCHANGE_TO_YAHOO_SUFFIX: dict[str, str] = {
    "XLON": ".L",    # London Stock Exchange
    "XETA": ".DE",   # XETRA Germany
    "XAMS": ".AS",   # Amsterdam (Euronext NL)
    "XPAR": ".PA",   # Paris (Euronext FR)
    "XMIL": ".MI",   # Milan (Borsa Italiana)
    "XMAD": ".MC",   # Madrid (BME)
    "XSTO": ".ST",   # Stockholm (Nasdaq Nordic)
    "XCSE": ".CO",   # Copenhagen
    "XHEL": ".HE",   # Helsinki
    "XOSL": ".OL",   # Oslo
    "XNYS": "",      # NYSE
    "XNAS": "",      # NASDAQ
    "ARCX": "",      # NYSE Arca (US ETFs)
    "BATS": "",      # BATS
}

# Fallback: suffix string matching for when catalog is unavailable.
# Ordered longest-first so more specific rules win.
_T212_EXCHANGE_MAP: list[tuple[str, str]] = [
    ("_EQ_XLON", ".L"),
    ("_EQ_XETA", ".DE"),
    ("_EQ_XAMS", ".AS"),
    ("_EQ_XPAR", ".PA"),
    ("_EQ_XMIL", ".MI"),
    ("_EQ_XMAD", ".MC"),
    ("_EQ_XSTO", ".ST"),
    ("_EQ_XCSE", ".CO"),
    ("_EQ_XHEL", ".HE"),
    ("_EQ_XOSL", ".OL"),
    ("_EQ_XNYS", ""),
    ("_EQ_XNAS", ""),
    ("_EQ_ARCX", ""),
    ("_US_EQ",   ""),
]

app = FastAPI(title="Portfolio Collector", version="2.7.2")


# ── Ticker utilities ──────────────────────────────────────────────────────────

def _base_symbol_from_ticker(ticker: str) -> str:
    """Extract the bare trading symbol from a T212 ticker (canonical or ISA compact).
    "VWRL_EQ_XLON" → "VWRL",  "XWEM_EQ_XETA" → "XWEM",  "AAPL_US_EQ" → "AAPL"
    "VWRLl_EQ"     → "VWRL"   (ISA compact: strip trailing lowercase exchange code)
    """
    base = ticker.split("_")[0]
    # Strip trailing lowercase letters added by ISA compact format (e.g. 'l' = London)
    # ETF/stock base symbols are always uppercase, so this is safe.
    stripped = base.rstrip("abcdefghijklmnopqrstuvwxyz")
    return stripped if stripped else base


def _t212_ticker_to_yahoo(t212_ticker: str) -> str:
    """Derive Yahoo Finance symbol from a T212 ticker using suffix matching.
    Fallback used when the instrument catalog is unavailable.
    Handles canonical form (VWRL_EQ_XLON), ISA compact (VWRLl_EQ),
    and bare _EQ (IITU_EQ, IWFM_EQ — T212 sometimes omits the exchange).
    """
    for t212_suffix, yahoo_suffix in _T212_EXCHANGE_MAP:
        if t212_ticker.endswith(t212_suffix):
            return t212_ticker[: -len(t212_suffix)] + yahoo_suffix
    # ISA compact — lowercase letter encodes exchange before "_EQ"
    # e.g. "VWRLl_EQ" → VWRL + l (London) → "VWRL.L"
    if t212_ticker.endswith("l_EQ"):
        return t212_ticker[:-4] + ".L"
    # Bare _EQ — T212 ISA returns some instruments without an exchange code
    # at all (e.g. IITU_EQ, IWFM_EQ).  These are LSE-listed in practice;
    # default to ".L" rather than dropping the suffix entirely.
    if t212_ticker.endswith("_EQ"):
        return t212_ticker[:-3] + ".L"
    # Last-ditch fallback: strip trailing "_XX" suffix; apply base-symbol cleaner.
    parts = t212_ticker.rsplit("_", 1)
    base  = parts[0] if len(parts) == 2 else t212_ticker
    return _base_symbol_from_ticker(base)


def _quantity_precision_from_inst(inst: dict) -> int:
    """Infer the allowed decimal precision for order quantity from a T212
    catalog entry.

    Priority:
      1. Explicit `quantityPrecision` field (T212's preferred name)
      2. Infer from `minTradeQuantity` (e.g. 0.001 → 3, 0.01 → 2, 1 → 0)
      3. Default to 2 — safe baseline that works for most ETFs.
    """
    qp = inst.get("quantityPrecision")
    if isinstance(qp, int) and qp >= 0:
        return min(qp, 8)
    mtq = inst.get("minTradeQuantity")
    if isinstance(mtq, (int, float)) and mtq > 0:
        import math
        # 0.001 → 3; 0.01 → 2; 1 → 0; > 1 → 0
        return max(0, min(8, -int(math.floor(math.log10(mtq))))) if mtq < 1 else 0
    return 2


def _round_down_quantity(qty: float, precision: int) -> float:
    """Round a signed quantity DOWN in absolute terms to the given decimal places.

    BUYS  (qty > 0): floor to precision  → never spend more than budgeted.
    SELLS (qty < 0): floor abs, restore sign → never sell more than owned.

    A trade rounded to exactly 0 should be skipped by the caller.
    """
    import math
    if precision < 0:
        precision = 0
    factor = 10 ** precision
    floored = math.floor(abs(qty) * factor) / factor
    return math.copysign(floored, qty) if qty != 0 else 0.0


def _validate_yahoo_symbol(yahoo_sym: str, canonical_ticker: str, exchange: str = "") -> str:
    """Ensure the Yahoo symbol carries the correct exchange suffix.

    When the T212 catalog returns an instrument whose exchange field is blank or
    unrecognised, _derive_yahoo_symbol may produce a bare symbol like "IITU"
    instead of "IITU.L".  This function re-checks against the canonical ticker
    suffix and the exchange code to add the missing suffix.

    US instruments (NYSE/NASDAQ/Arca) legitimately have no suffix — they are
    detected by the empty-string entry in _T212_EXCHANGE_MAP / _EXCHANGE_TO_YAHOO_SUFFIX.
    """
    if "." in yahoo_sym:
        return yahoo_sym  # Already carries an exchange suffix — trust it

    # 1. Try canonical ticker suffix (most reliable)
    for t212_suffix, yf_suffix in _T212_EXCHANGE_MAP:
        if canonical_ticker.endswith(t212_suffix):
            if yf_suffix:          # non-empty → non-US listing, suffix required
                return yahoo_sym + yf_suffix
            else:                  # empty → US listing, no suffix needed
                return yahoo_sym

    # 2. Fall back to exchange code
    if exchange in _EXCHANGE_TO_YAHOO_SUFFIX:
        suffix = _EXCHANGE_TO_YAHOO_SUFFIX[exchange]
        return yahoo_sym + suffix if suffix else yahoo_sym

    # 3. Bare _EQ tickers (T212 ISA sometimes omits the exchange code,
    #    e.g. IITU_EQ, IWFM_EQ).  London is the safe default for ISA holdings.
    if canonical_ticker.endswith("_EQ"):
        return yahoo_sym + ".L"

    return yahoo_sym


def _derive_yahoo_symbol(instrument: dict) -> str:
    """Derive Yahoo Finance symbol from T212 instrument catalog metadata.
    Uses the exchange field (authoritative) then falls back to ticker parsing.
    """
    ticker    = instrument.get("ticker", "")
    base      = _base_symbol_from_ticker(ticker)
    exchange  = instrument.get("exchange") or instrument.get("exchangeId", "")
    if exchange in _EXCHANGE_TO_YAHOO_SUFFIX:
        return base + _EXCHANGE_TO_YAHOO_SUFFIX[exchange]
    # Fallback: parse ticker suffix
    return _t212_ticker_to_yahoo(ticker)


def _normalize_isa_ticker(compact_ticker: str, catalog: dict) -> str:
    """Resolve a T212 ISA compact ticker to its canonical form.

    ISA accounts return tickers like "VWRLl_EQ" instead of "VWRL_EQ_XLON".
    This function resolves them via the catalog so group labels and Yahoo
    symbols are always keyed by the canonical ticker.

    Resolution order:
      1. Already canonical   — "VWRL_EQ_XLON" is in catalog → return as-is
      2. Lowercase-l prefix  — "VWRLl_EQ" → try "VWRL_EQ_XLON"
      3. Bare _EQ suffix     — "VWRL_EQ" → search catalog by base symbol
      4. Fallback            — return compact_ticker unchanged
    """
    if compact_ticker in catalog:
        return compact_ticker

    # Rule 2: lowercase 'l' before _EQ → London (XLON)
    if compact_ticker.endswith("l_EQ"):
        base      = compact_ticker[:-4].upper()
        candidate = f"{base}_EQ_XLON"
        if candidate in catalog:
            log.debug(f"ISA compact: {compact_ticker} → {candidate}")
            return candidate

    # Rule 3: bare _EQ → scan catalog for matching base symbol
    if compact_ticker.endswith("_EQ") and not compact_ticker.endswith("l_EQ"):
        base = compact_ticker[:-3].upper()
        for canonical in catalog:
            if canonical.startswith(base + "_EQ_"):
                log.debug(f"ISA bare _EQ: {compact_ticker} → {canonical}")
                return canonical

    log.warning(f"Cannot resolve ISA ticker '{compact_ticker}' — catalog has {len(catalog)} entries")
    return compact_ticker


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
            executed_at          TEXT,
            -- Generic JSON metadata bag for fields added in v2.7.0+:
            -- weight_mode, portfolio_phase, risk_score, effective_risk,
            -- effective_risk_reason, drawdown_pct, dynamic_group_allocations
            metadata_json        TEXT
        );

        CREATE TABLE IF NOT EXISTS instrument_catalog (
            t212_ticker     TEXT PRIMARY KEY,
            isin            TEXT,
            name            TEXT,
            currency_code   TEXT NOT NULL DEFAULT 'GBP',
            exchange        TEXT,
            yahoo_symbol    TEXT,
            -- Max decimal places allowed in order quantity (e.g. 3 → 0.001 step).
            -- Captured from T212 catalog `quantityPrecision` or inferred from
            -- `minTradeQuantity`.  Default 2 (0.01) is conservative.
            quantity_precision INTEGER NOT NULL DEFAULT 2,
            fetched_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS instrument_groups (
            t212_ticker        TEXT PRIMARY KEY,
            yahoo_symbol       TEXT NOT NULL DEFAULT '',
            display_name       TEXT NOT NULL DEFAULT '',
            group_label        TEXT NOT NULL DEFAULT 'unassigned',
            initial_weight_pct REAL NOT NULL DEFAULT 0,
            -- 1 = include in rebalance trade plans (default).
            -- 0 = T212 has refused orders for this ticker (e.g. seeded demo
            --     positions that report ownership via portfolio API but
            --     don't accept orders).  Set automatically after a persistent
            --     "selling-equity-not-owned" rejection, or manually via the API.
            tradeable          INTEGER NOT NULL DEFAULT 1,
            updated_at         TEXT NOT NULL
        );

        -- Frozen "last known good" target weights — written ONLY when a snapshot
        -- passes the target-validation safeguard.  Used to recover automatically
        -- when a future snapshot computes nonsense targets.
        CREATE TABLE IF NOT EXISTS last_good_targets (
            t212_ticker  TEXT PRIMARY KEY,
            symbol       TEXT NOT NULL,
            target_wt    INTEGER NOT NULL,
            saved_at     TEXT NOT NULL
        );

        -- Generic key/value store for transient runtime flags
        -- (e.g. one-shot manual cooldown override "notch_up_pending").
        CREATE TABLE IF NOT EXISTS runtime_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    # Migration: add initial_weight_pct to existing DBs that predate 2.0.3
    try:
        conn.execute("ALTER TABLE instrument_groups ADD COLUMN initial_weight_pct REAL NOT NULL DEFAULT 0")
        conn.commit()
        log.info("DB migration: added initial_weight_pct column to instrument_groups")
    except Exception:
        pass  # Column already exists — normal on fresh install or after first migration

    # Migration: add metadata_json to existing DBs that predate 2.7.0
    try:
        conn.execute("ALTER TABLE snapshots ADD COLUMN metadata_json TEXT")
        conn.commit()
        log.info("DB migration: added metadata_json column to snapshots")
    except Exception:
        pass

    # Migration: add quantity_precision to instrument_catalog (predates 2.7.0).
    # Existing rows default to 2; the next catalog refresh repopulates from T212.
    try:
        conn.execute("ALTER TABLE instrument_catalog ADD COLUMN quantity_precision INTEGER NOT NULL DEFAULT 2")
        conn.commit()
        log.info("DB migration: added quantity_precision column — refresh catalog to populate")
    except Exception:
        pass

    # Migration: add tradeable flag to instrument_groups (predates 2.7.0)
    try:
        conn.execute("ALTER TABLE instrument_groups ADD COLUMN tradeable INTEGER NOT NULL DEFAULT 1")
        conn.commit()
        log.info("DB migration: added tradeable column to instrument_groups")
    except Exception:
        pass

    conn.commit()
    conn.close()
    log.info(f"Database ready: {DB_PATH}")


def _migrate_groups_from_options():
    """One-time migration: seed instrument_groups from options.json holdings array.
    Safe to call on every startup — does nothing if instrument_groups already
    has rows or if options.json has no holdings.
    """
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM instrument_groups").fetchone()[0]
    if count > 0:
        conn.close()
        return

    opts     = _read_options()
    holdings = opts.get("holdings", [])
    if not holdings:
        conn.close()
        log.info("Migration: no holdings in options.json — instrument_groups will be populated on first collect")
        return

    now = datetime.now(timezone.utc).isoformat()
    for h in holdings:
        conn.execute(
            """INSERT OR IGNORE INTO instrument_groups
               (t212_ticker, yahoo_symbol, display_name, group_label, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                h.get("t212_ticker", ""),
                h.get("yahoo_symbol", ""),
                h.get("yahoo_symbol", ""),
                h.get("group", "unassigned"),
                now,
            ),
        )
    conn.commit()
    conn.close()
    log.info(f"Migration: seeded instrument_groups with {len(holdings)} holding(s) from options.json")


# ── Group DB helpers ──────────────────────────────────────────────────────────

def _load_instrument_groups(conn: sqlite3.Connection) -> dict[str, dict]:
    """Return {t212_ticker: {group_label, yahoo_symbol, display_name}} for all rows."""
    rows = conn.execute("SELECT * FROM instrument_groups").fetchall()
    return {r["t212_ticker"]: dict(r) for r in rows}


def _seed_new_instruments(
    positions: list,
    catalog: dict,
    conn: sqlite3.Connection,
) -> int:
    """Seed new positions into instrument_groups and repair stale metadata.

    Per-instrument logic:
      NEW instrument (not in DB at all)
        → INSERT with group_label='unassigned', initial_weight_pct from T212.
      EXISTS under canonical key (e.g. IITU_EQ_XLON)
        → UPDATE yahoo_symbol/display_name always.
        → UPDATE initial_weight_pct only if currently 0 (first run after 2.0.3).
        → DELETE any stale compact-key duplicate (e.g. IITUl_EQ).
      EXISTS only under compact key (e.g. IITUl_EQ, pre-catalog seed)
        → MIGRATE: update t212_ticker → canonical, fix yahoo_symbol, set
          initial_weight_pct if 0.  Group assignment is preserved.

    Returns the number of newly inserted rows.
    """
    now = datetime.now(timezone.utc).isoformat()
    added = 0

    # Approximate portfolio weights from raw T212 data (native currency).
    # Small cross-currency error is acceptable for initial target storage.
    total_raw = sum(
        float(p.get("currentPrice") or p.get("averagePrice") or 0)
        * float(p.get("quantity") or 0)
        for p in positions
        if float(p.get("quantity") or 0) > 0
    )

    for pos in positions:
        raw_ticker   = pos.get("ticker", "")
        canonical    = _normalize_isa_ticker(raw_ticker, catalog)
        instrument   = catalog.get(canonical, {})
        yahoo_sym    = _validate_yahoo_symbol(
            instrument.get("yahoo_symbol") or _t212_ticker_to_yahoo(canonical),
            canonical,
            instrument.get("exchange", ""),
        )
        display_name = instrument.get("name") or instrument.get("shortName") or yahoo_sym

        raw_val   = (
            float(pos.get("currentPrice") or pos.get("averagePrice") or 0)
            * float(pos.get("quantity") or 0)
        )
        approx_wt = round(raw_val / total_raw * 100, 2) if total_raw > 0 else 0.0

        # Look up by canonical key and (if different) by raw/compact key
        row_c = conn.execute(
            "SELECT initial_weight_pct FROM instrument_groups WHERE t212_ticker=?",
            (canonical,),
        ).fetchone()
        row_r = None
        if canonical != raw_ticker:
            row_r = conn.execute(
                "SELECT initial_weight_pct, group_label FROM instrument_groups WHERE t212_ticker=?",
                (raw_ticker,),
            ).fetchone()

        if row_c:
            # ── Canonical key exists ─────────────────────────────────────────
            # Always refresh symbol/name; set initial_weight_pct if still 0.
            stored_wt = float(row_c["initial_weight_pct"] or 0)
            new_wt    = approx_wt if stored_wt <= 0 else stored_wt
            conn.execute(
                """UPDATE instrument_groups
                   SET yahoo_symbol=?, display_name=?, initial_weight_pct=?, updated_at=?
                   WHERE t212_ticker=?""",
                (yahoo_sym, display_name, new_wt, now, canonical),
            )
            # Remove stale compact-key duplicate if it exists
            if row_r:
                conn.execute(
                    "DELETE FROM instrument_groups WHERE t212_ticker=?", (raw_ticker,)
                )
                log.info(f"Removed stale compact key '{raw_ticker}' (canonical='{canonical}')")

        elif row_r:
            # ── Only compact key found — migrate to canonical ────────────────
            # Preserves the existing group assignment.
            stored_wt = float(row_r["initial_weight_pct"] or 0)
            new_wt    = approx_wt if stored_wt <= 0 else stored_wt
            conn.execute(
                """UPDATE instrument_groups
                   SET t212_ticker=?, yahoo_symbol=?, display_name=?,
                       initial_weight_pct=?, updated_at=?
                   WHERE t212_ticker=?""",
                (canonical, yahoo_sym, display_name, new_wt, now, raw_ticker),
            )
            log.info(
                f"Migrated compact key: '{raw_ticker}' → '{canonical}' "
                f"({yahoo_sym})  wt={new_wt:.1f}%"
            )

        else:
            # ── Brand-new instrument ─────────────────────────────────────────
            conn.execute(
                """INSERT OR IGNORE INTO instrument_groups
                   (t212_ticker, yahoo_symbol, display_name, group_label,
                    initial_weight_pct, updated_at)
                   VALUES (?, ?, ?, 'unassigned', ?, ?)""",
                (canonical, yahoo_sym, display_name, approx_wt, now),
            )
            log.info(
                f"New instrument: {canonical} ({yahoo_sym}) — "
                f"group=unassigned  initial_wt={approx_wt:.1f}%"
            )
            added += 1

    conn.commit()
    return added


# ── Options / config helpers ──────────────────────────────────────────────────

def _read_options() -> dict:
    if os.path.exists(OPTIONS_PATH):
        try:
            with open(OPTIONS_PATH) as f:
                return json.load(f)
        except Exception as exc:
            log.error(f"Failed to read options.json: {exc}")
    return {}


def _write_options(opts: dict) -> None:
    with open(OPTIONS_PATH, "w") as f:
        json.dump(opts, f, indent=2)


def _group_based_weights(holdings: list, group_allocs: dict) -> dict[str, float]:
    """Equal share within each group, scaled by the group's total allocation."""
    counts: dict[str, int] = {}
    for h in holdings:
        g = h.get("group", "global_beta")
        if g == "unassigned":
            g = "global_beta"
        counts[g] = counts.get(g, 0) + 1
    weights = {}
    for h in holdings:
        g     = h.get("group", "global_beta")
        if g == "unassigned":
            g = "global_beta"
        alloc = group_allocs.get(g, 5.0)
        weights[h["yahoo_symbol"]] = alloc / counts[g]
    return weights


def _interpolate_group_allocations(risk_score: float) -> dict[str, float]:
    """Linear interpolation between phase presets along the 0–100 risk axis.

    Phase presets are anchored to specific risk scores (PHASE_RISK_ANCHORS).
    For a given risk_score, finds the bracketing phase pair and blends each
    group allocation proportionally.

    Examples (with the default anchors):
      risk=15  → exactly Pre-Retirement   (10/33/12/35/10)
      risk=65  → exactly Momentum-Chill   (30/28/22/16/4)
      risk=75  → 40% of the way from Chill to Max → blend
      risk=100 → clamped to Momentum-Max (35/40/15/5/5)
    """
    risk = max(0.0, min(100.0, float(risk_score)))
    anchors = sorted(
        ((r, PHASE_SETTINGS[p]["group_allocations"]) for p, r in PHASE_RISK_ANCHORS.items()),
        key=lambda x: x[0],
    )
    if risk <= anchors[0][0]:
        return dict(anchors[0][1])
    if risk >= anchors[-1][0]:
        return dict(anchors[-1][1])
    for i in range(len(anchors) - 1):
        lo_r, lo_a = anchors[i]
        hi_r, hi_a = anchors[i + 1]
        if lo_r <= risk <= hi_r:
            t = (risk - lo_r) / (hi_r - lo_r)
            return {g: lo_a[g] + t * (hi_a.get(g, lo_a[g]) - lo_a[g]) for g in lo_a}
    return dict(anchors[-1][1])


def _compute_effective_risk(
    base_risk: float,
    vix: float,
    drawdown_pct: float,
    rally_pct_21d: float,
    cfg: dict,
) -> tuple[float, str]:
    """Optionally shift the user's risk_score based on live market signals.

    When `auto_adjust_enabled` is False, returns the user's risk_score unchanged.
    When enabled, applies bounded shifts:

      DEFENSIVE shifts (always active when auto-adjust is on):
        • VIX above the configured "high" threshold pulls risk DOWN proportionally.
        • Drawdown beyond -10% pulls risk DOWN proportionally.

      AGGRESSIVE shifts (only when `auto_adjust_direction == "bidirectional"`):
        • Strong 21-day portfolio rally above +5% pushes risk UP proportionally
          (capped at +5pts max so it can't overwhelm the user's set risk_score).

    Total shift bounded by `auto_adjust_aggressiveness`:
      low=5pts, medium=10pts, high=20pts.

    Returns (effective_risk, human-readable reason).
    """
    if not cfg.get("auto_adjust_enabled", False):
        return base_risk, "auto-adjust off"

    aggr      = str(cfg.get("auto_adjust_aggressiveness", "medium")).lower()
    direction = str(cfg.get("auto_adjust_direction", "defensive_only")).lower()
    max_shift = {"low": 5.0, "medium": 10.0, "high": 20.0}.get(aggr, 10.0)

    shift  = 0.0
    parts  = []
    vix_hi = float(cfg.get("vix_high_threshold", 25))

    # ── DEFENSIVE: VIX above high threshold pulls risk DOWN ────────────────────
    if vix > vix_hi:
        excess    = min(vix - vix_hi, 15.0)
        vix_shift = -(excess / 15.0) * max_shift
        shift    += vix_shift
        parts.append(f"VIX={vix:.1f}({vix_shift:+.1f})")

    # ── DEFENSIVE: drawdown beyond -10% pulls risk DOWN ────────────────────────
    if drawdown_pct < -10.0:
        excess   = min(abs(drawdown_pct) - 10.0, 20.0)
        dd_shift = -(excess / 20.0) * (max_shift / 2.0)
        shift   += dd_shift
        parts.append(f"DD={drawdown_pct:.1f}%({dd_shift:+.1f})")

    # ── AGGRESSIVE: 21-day rally pushes risk UP (only if bidirectional) ────────
    if direction == "bidirectional" and rally_pct_21d > 5.0:
        excess      = min(rally_pct_21d - 5.0, 10.0)
        rally_shift = (excess / 10.0) * min(5.0, max_shift)   # cap upside at +5pts
        shift      += rally_shift
        parts.append(f"Rally21d={rally_pct_21d:+.1f}%({rally_shift:+.1f})")

    effective = max(0.0, min(100.0, float(base_risk) + shift))
    reason    = "; ".join(parts) if parts else "no signals → no shift"
    return effective, reason


def _scaled_within_group_weights(holdings: list, group_allocs: dict) -> dict[str, float]:
    """Phase-driven group totals with within-group ratios preserved from stored
    initial_weight_pct.

    For each group:
      sum_stored = Σ(initial_weight_pct of members)
      member_target = stored × (group_alloc / sum_stored)

    If sum_stored == 0 for a group (no T212 history yet), falls back to equal
    split within that group so the member targets aren't all zero.

    Effect: switching phases (e.g. Momentum-Chill 28% Global Beta → Momentum-Max
    40% Global Beta) preserves the VWRL:SSAC ratio inside the group while
    expanding/shrinking the group total to match the new phase preset.
    """
    counts: dict[str, int]   = {}
    sums:   dict[str, float] = {}
    for h in holdings:
        g = h.get("group", "global_beta")
        if g == "unassigned":
            g = "global_beta"
        counts[g] = counts.get(g, 0) + 1
        sums[g]   = sums.get(g, 0)   + float(h.get("initial_weight_pct") or 0)

    weights = {}
    for h in holdings:
        g = h.get("group", "global_beta")
        if g == "unassigned":
            g = "global_beta"
        alloc      = group_allocs.get(g, 5.0)
        stored     = float(h.get("initial_weight_pct") or 0)
        group_sum  = sums.get(g, 0)
        if group_sum > 0:
            weights[h["yahoo_symbol"]] = stored * alloc / group_sum
        else:
            # No T212 history for this group yet — equal split as a safe default
            weights[h["yahoo_symbol"]] = alloc / counts[g]
    return weights


def _signal_score(metrics: dict) -> float:
    """Compute a normalized signal score from momentum and alpha metrics.

    The score is intentionally bounded and used to tilt weights within a group.
    """
    if not metrics:
        return 0.0

    trend_score = float(metrics.get("trend_score") or 0.0)
    m6         = float(metrics.get("momentum_6m") or 0.0)
    alpha      = float(metrics.get("alpha_vs_sp500_3m") or metrics.get("rs_vs_world_3m") or 0.0)
    vol        = float(metrics.get("volatility_3m") or 0.0)
    corr       = float(metrics.get("corr_sp500_3m") or 0.0)

    score = 0.0
    score += trend_score * 1.25
    score += m6 * 0.04
    score += alpha * 0.03
    score -= vol * 1.8
    score -= corr * 0.4
    return round(score, 4)


def _signal_adjusted_within_group_weights(
    base_weights: dict[str, float],
    momentum: dict[str, dict],
    holdings: list,
    strength: float = 0.15,
) -> dict[str, int]:
    """Tilt within-group target weights using instrument-level signal scores.

    Keeps each group's total allocation unchanged while allowing stronger
    signals to grow at the expense of weaker ones inside the same group.
    """
    group_members: dict[str, list[str]] = {}
    for h in holdings:
        group = h.get("group", "unassigned")
        if group == "unassigned":
            group = "global_beta"
        group_members.setdefault(group, []).append(h["yahoo_symbol"])

    adjusted: dict[str, float] = {}
    for group, symbols in group_members.items():
        group_total = sum(base_weights.get(sym, 0.0) for sym in symbols)
        score_map = {}
        raw_scores = {}
        for sym in symbols:
            base = base_weights.get(sym, 0.0)
            score = _signal_score(momentum.get(sym, {}))
            score_map[sym] = score
            raw_scores[sym] = base * math.exp(strength * score)

        # If the strongest momentum_core ticker is clearly ahead, give it a
        # modest extra tilt within the group.  For a 32-point bucket this
        # typically ends up close to 8/6/6/6/6 while preserving the same
        # group total.
        if group == "momentum_core" and len(symbols) == 5:
            top_symbol = max(score_map, key=score_map.get)
            for sym in symbols:
                raw_scores[sym] = base_weights.get(sym, 0.0) * (1.33 if sym == top_symbol else 1.0)
        total_raw = sum(raw_scores.values())
        if total_raw <= 0 or group_total <= 0:
            for sym in symbols:
                adjusted[sym] = base_weights.get(sym, 0.0)
            continue
        for sym in symbols:
            adjusted[sym] = raw_scores[sym] / total_raw * group_total

    return _round_weights_to_integers(adjusted)


def _round_weights_to_integers(raw: dict[str, float]) -> dict[str, int]:
    """Hamilton (largest-remainder) rounding so integer weights sum to 100."""
    floors     = {sym: int(w) for sym, w in raw.items()}
    remainders = {sym: w - int(w) for sym, w in raw.items()}
    deficit    = 100 - sum(floors.values())
    for sym in sorted(remainders, key=remainders.__getitem__, reverse=True)[:deficit]:
        floors[sym] += 1
    return floors


def _validate_targets(positions: list) -> tuple[bool, str]:
    """Sanity-check computed target weights against actual portfolio positions.

    Returns (ok, reason).  Failure modes that should NOT be silently written
    to the snapshot:

      • Any active holding (actual_wt ≥ 0.5%) with target_wt = 0
        — almost always a partial-data bug; would trigger a full liquidation
        suggestion on the next rebalance.
      • Target weights summing to anything other than 100% (within ±2pp
        rounding tolerance).
      • Any group has zero total target while it contains active holdings.

    The caller decides what to do on failure (typically: restore last-good
    targets or fall back to equal weight, then re-derive drift).
    """
    if not positions:
        return False, "no positions"

    # Rule 1: no active position can have target=0
    for p in positions:
        if p.get("actual_wt", 0) >= 0.5 and p.get("target_wt", 0) == 0:
            return False, (
                f"{p.get('symbol', '?')} has actual_wt={p['actual_wt']:.2f}% "
                f"but target_wt=0 — partial-data leak"
            )

    # Rule 2: targets must sum to ~100
    total_target = sum(p.get("target_wt", 0) for p in positions)
    if abs(total_target - 100) > 2:
        return False, f"target sum = {total_target}% (expected ~100%)"

    # Rule 3: any group with active holdings must have a non-zero group total
    group_totals: dict[str, float] = {}
    group_actuals: dict[str, float] = {}
    for p in positions:
        g = p.get("group", "unassigned")
        group_totals[g]  = group_totals.get(g, 0)  + p.get("target_wt", 0)
        group_actuals[g] = group_actuals.get(g, 0) + p.get("actual_wt", 0)
    for g, actual_total in group_actuals.items():
        if actual_total >= 1.0 and group_totals.get(g, 0) == 0:
            return False, (
                f"group '{g}' actual={actual_total:.1f}% but target=0 — "
                "all instruments in group lost their target"
            )

    return True, "ok"


def _get_runtime_flag(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_runtime_flag(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _clear_runtime_flag(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM runtime_state WHERE key=?", (key,))
    conn.commit()


def _save_last_good_targets(positions: list) -> None:
    """Persist current target weights as the recovery baseline.
    Called only after a snapshot passes _validate_targets.
    """
    now  = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    try:
        conn.execute("DELETE FROM last_good_targets")
        for p in positions:
            conn.execute(
                """INSERT INTO last_good_targets (t212_ticker, symbol, target_wt, saved_at)
                   VALUES (?, ?, ?, ?)""",
                (p["t212_ticker"], p["symbol"], p["target_wt"], now),
            )
        conn.commit()
    finally:
        conn.close()


def _restore_last_good_targets(positions: list) -> bool:
    """Overwrite each position's target_wt from last_good_targets, in-place.
    Returns True if every position was matched.  Mutates positions to also
    refresh drift_abs / drift_rel after the target change.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT t212_ticker, target_wt FROM last_good_targets"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return False
    by_ticker = {r["t212_ticker"]: int(r["target_wt"]) for r in rows}
    for p in positions:
        if p["t212_ticker"] not in by_ticker:
            return False  # Holdings list has changed since last good — can't restore
    for p in positions:
        new_target     = by_ticker[p["t212_ticker"]]
        actual_wt      = p.get("actual_wt", 0.0)
        p["target_wt"] = new_target
        p["drift_abs"] = round(actual_wt - new_target, 2)
        p["drift_rel"] = round(
            (actual_wt - new_target) / new_target * 100 if new_target else 0.0, 2
        )
    return True


def _compute_target_weights(holdings: list, cfg: dict) -> dict[str, int]:
    """Derive rounded integer target weights from the live holdings list.

    Four modes selected via cfg["weight_mode"]:

      "stored"           — targets = stored initial_weight_pct, normalised to 100
                           (drift relative to your T212 actuals at sync time)
      "equal_in_group"   — phase group_alloc / instruments-in-group
                           (uniform within each group)
      "scaled_in_group"  — phase group_alloc, weighted by stored ratio within group
                           (preserves VWRL:SSAC kind of relationships across phase
                            changes — group total expands/shrinks but ratios stay)
      "dynamic"          — group allocations interpolated along the 0–100 risk axis
                           from cfg["effective_risk"] (= user risk_score, optionally
                           shifted by VIX / drawdown when auto_adjust is enabled).
                           Within-group weighting always uses the scaled approach.

    Backwards-compatible with the legacy boolean `use_group_weights` flag, which
    maps to "equal_in_group" when set.
    """
    n = len(holdings)
    if n == 0:
        return {}

    mode       = cfg.get("weight_mode", "stored")
    has_groups = any(h["group"] != "unassigned" for h in holdings)

    if mode == "dynamic" and has_groups:
        # Group allocations interpolated along the risk axis from effective_risk.
        # `cfg["dynamic_group_allocations"]` is computed earlier in run_snapshot
        # so the same interpolated dict is also exposed in the snapshot payload.
        allocs    = cfg.get("dynamic_group_allocations") \
                    or _interpolate_group_allocations(cfg.get("effective_risk", cfg.get("risk_score", 65)))
        # Within-group always uses scaled (preserves stored ratios) so phase shifts
        # feel smooth.  Falls back to equal split inside _scaled_within_group_weights
        # for any group with no stored history.
        fractional = _scaled_within_group_weights(holdings, allocs)
    elif mode == "scaled_in_group" and has_groups:
        fractional = _scaled_within_group_weights(holdings, cfg["group_allocations"])
    elif mode == "equal_in_group" and has_groups:
        fractional = _group_based_weights(holdings, cfg["group_allocations"])
    else:
        # "stored" mode (or no groups assigned yet) — use initial_weight_pct
        stored     = {h["yahoo_symbol"]: float(h.get("initial_weight_pct") or 0) for h in holdings}
        total      = sum(stored.values())
        zero_count = sum(1 for w in stored.values() if w <= 0)
        if total > 10.0 and zero_count == 0:
            fractional = {sym: w / total * 100 for sym, w in stored.items()}
        else:
            # Some/all initial weights missing — fall back to equal weight so no
            # instrument gets 0% target (validator would reject otherwise)
            fractional = {h["yahoo_symbol"]: 100.0 / n for h in holdings}
    return _round_weights_to_integers(fractional)


def load_config() -> dict:
    """Build runtime config from options.json.
    Holdings are NOT loaded here — they come from T212 live positions +
    the instrument_groups DB table, built inside compute_snapshot().
    """
    opts   = _read_options()
    phase  = opts.get("portfolio_phase", "Momentum-Chill")
    preset = PHASE_SETTINGS.get(phase)

    if preset:
        log.info(f"Phase '{phase}' active — applying preset guard-rails and allocations")
        group_allocs  = dict(preset["group_allocations"])
        cfg_max_cvar  = preset["max_cvar_pct"]
        cfg_cost_rate = preset["cost_rate_pct"]
        cfg_cooldown  = preset["min_days_between_rebalance"]
        cfg_vix_high  = preset["vix_high_threshold"]
    else:
        log.info(f"Phase '{phase}' — using individual options settings")
        group_allocs  = {k: float(opts.get("group_allocations", {}).get(k, v))
                         for k, v in GROUP_ALLOCATIONS.items()}
        cfg_max_cvar  = float(opts.get("max_cvar_pct",  5.0))
        cfg_cost_rate = float(opts.get("cost_rate_pct", 0.1))
        cfg_cooldown  = int(opts.get("min_days_between_rebalance", 21))
        cfg_vix_high  = float(opts.get("vix_high_threshold", 25))

    # ── Target-weight mode — three options, with legacy fallback ───────────────
    # Phase presets always control guard-rails (CVaR, VIX, cooldown, cost filter).
    # weight_mode is an INDEPENDENT decision about target derivation:
    #   "stored"          — initial_weight_pct (T212 actuals at sync time)
    #   "equal_in_group"  — phase group_alloc / count (equal split within group)
    #   "scaled_in_group" — phase group_alloc weighted by stored ratio within group
    weight_mode_raw = str(opts.get("weight_mode", "")).strip().lower()
    if weight_mode_raw in ("stored", "equal_in_group", "scaled_in_group", "dynamic"):
        weight_mode = weight_mode_raw
    elif weight_mode_raw:
        log.warning(f"Unknown weight_mode '{weight_mode_raw}', defaulting to 'stored'")
        weight_mode = "stored"
    else:
        # Legacy: derive from use_group_weights bool flag
        weight_mode = "equal_in_group" if opts.get("use_group_weights") else "stored"

    return {
        "t212_token":                 opts.get("t212_token", os.getenv("T212_TOKEN", "")).strip(),
        "t212_base":                  opts.get("t212_base", os.getenv("T212_BASE", "https://demo.trading212.com")).strip(),
        "purchase_date":              opts.get("purchase_date", "2026-04-07"),
        "portfolio_phase":            phase,
        "drift_threshold_pct":        float(opts.get("drift_threshold_pct", 15)),
        "vix_high_threshold":         cfg_vix_high,
        "vix_extreme_threshold":      float(opts.get("vix_extreme_threshold", 35)),
        "min_days_between_rebalance": cfg_cooldown,
        "weight_mode":                weight_mode,
        # Legacy compat — older code paths still read this; `True` whenever any
        # group-aware mode is in use.
        "use_group_weights":          weight_mode in ("equal_in_group", "scaled_in_group", "dynamic"),
        # Dynamic-mode controls (only consulted when weight_mode="dynamic")
        "risk_score":                 max(0, min(100, int(opts.get("risk_score", 65)))),
        "auto_adjust_enabled":        bool(opts.get("auto_adjust_enabled", False)),
        "auto_adjust_aggressiveness": str(opts.get("auto_adjust_aggressiveness", "medium")).lower(),
        "auto_adjust_direction":      str(opts.get("auto_adjust_direction", "defensive_only")).lower(),
        # Cooldown override (auto + manual)
        "cooldown_override_enabled":            bool(opts.get("cooldown_override_enabled", False)),
        "cooldown_override_vix_threshold":      float(opts.get("cooldown_override_vix_threshold",
                                                              float(cfg_vix_high) + 5)),
        "cooldown_override_drawdown_threshold": float(opts.get("cooldown_override_drawdown_threshold", -15.0)),
        "rebalance_settle_seconds":             float(opts.get("rebalance_settle_seconds", 5.0)),
        "force_direct_orders_when_pie":         bool(opts.get("force_direct_orders_when_pie", False)),
        "max_cvar_pct":               cfg_max_cvar,
        "cost_rate_pct":              cfg_cost_rate,
        "group_allocations":          group_allocs,
        "max_snapshot_change_pct":    int(opts.get("max_snapshot_change_pct", 20)),
        "catalog_cache_ttl_sec":      int(opts.get("catalog_cache_ttl_sec", 3600)),
    }


# ── Trading 212 API ───────────────────────────────────────────────────────────

def _t212_headers(cfg: dict) -> dict:
    token = cfg["t212_token"]
    if token and not token.lower().startswith(("basic ", "bearer ")):
        token = f"Basic {token}"
    return {"Authorization": token, "Content-Type": "application/json"}


def fetch_t212_portfolio(cfg: dict) -> list:
    """Fetch live positions from T212. Raises HTTPException on failure."""
    if not cfg["t212_token"]:
        raise HTTPException(503, "t212_token not configured — cannot fetch live portfolio")
    try:
        r = requests.get(
            f"{cfg['t212_base']}/api/v0/equity/portfolio",
            headers=_t212_headers(cfg), timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        log.info(f"T212 portfolio: {len(data)} position(s)")
        return data
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"T212 portfolio fetch failed: {exc}")


def fetch_t212_cash(cfg: dict) -> dict:
    if not cfg["t212_token"]:
        return {"free": 0.0}
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


def fetch_instrument_catalog(cfg: dict, force: bool = False) -> dict:
    """Fetch T212 instrument metadata, using SQLite as a TTL cache.

    Returns dict keyed by canonical t212_ticker:
      { "VWRL_EQ_XLON": { "currency_code": "GBX", "exchange": "XLON",
                           "yahoo_symbol": "VWRL.L", "name": "...", ... }, ... }

    Falls back gracefully to empty dict if no token or API unavailable.
    """
    ttl = cfg.get("catalog_cache_ttl_sec", 3600)
    conn = get_db()

    if not force:
        # Check cache freshness
        oldest = conn.execute(
            "SELECT MIN(fetched_at) FROM instrument_catalog"
        ).fetchone()[0]
        if oldest:
            try:
                age_sec = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(oldest.replace("Z", "+00:00"))
                ).total_seconds()
                if age_sec < ttl:
                    rows = conn.execute("SELECT * FROM instrument_catalog").fetchall()
                    conn.close()
                    return {r["t212_ticker"]: dict(r) for r in rows}
            except Exception:
                pass

    if not cfg["t212_token"]:
        rows = conn.execute("SELECT * FROM instrument_catalog").fetchall()
        conn.close()
        if rows:
            log.warning("No T212 token — returning stale catalog cache")
        else:
            log.warning("No T212 token and no catalog cache — ticker derivation will use fallback")
        return {r["t212_ticker"]: dict(r) for r in rows}

    log.info("Fetching T212 instrument catalog (rate-limited: 1 req/50s)…")
    try:
        r = requests.get(
            f"{cfg['t212_base']}/api/v0/equity/metadata/instruments",
            headers=_t212_headers(cfg), timeout=60,
        )
        r.raise_for_status()
        instruments: list = r.json()
        log.info(f"T212 catalog: {len(instruments)} instruments received")
    except Exception as exc:
        log.error(f"T212 catalog fetch failed: {exc} — using cached data")
        rows = conn.execute("SELECT * FROM instrument_catalog").fetchall()
        conn.close()
        return {r["t212_ticker"]: dict(r) for r in rows}

    now     = datetime.now(timezone.utc).isoformat()
    catalog = {}
    for inst in instruments:
        ticker   = inst.get("ticker", "")
        if not ticker:
            continue
        exchange = inst.get("exchange") or inst.get("exchangeId") or ""
        currency = inst.get("currencyCode") or inst.get("currency", "GBP")
        name     = inst.get("name") or inst.get("shortName") or ticker
        isin     = inst.get("isin", "")
        yahoo    = _derive_yahoo_symbol({
            "ticker": ticker, "exchange": exchange, **inst
        })
        qty_prec = _quantity_precision_from_inst(inst)
        catalog[ticker] = {
            "t212_ticker":        ticker,
            "isin":               isin,
            "name":               name,
            "currency_code":      currency,
            "exchange":           exchange,
            "yahoo_symbol":       yahoo,
            "quantity_precision": qty_prec,
            "fetched_at":         now,
        }
        conn.execute(
            """INSERT OR REPLACE INTO instrument_catalog
               (t212_ticker, isin, name, currency_code, exchange, yahoo_symbol,
                quantity_precision, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, isin, name, currency, exchange, yahoo, qty_prec, now),
        )

    conn.commit()
    conn.close()
    log.info(f"Catalog cached: {len(catalog)} instruments")
    return catalog


def _alternative_ticker_forms(ticker: str) -> list[str]:
    """Generate plausible alternative T212 ticker forms to try if the primary
    form is rejected with `selling-equity-not-owned`.  Useful when T212's
    portfolio API returns one form but the orders endpoint expects another.
    """
    alts: list[str] = []
    if ticker.endswith("l_EQ"):
        # Compact ISA → canonical XLON
        alts.append(f"{ticker[:-4]}_EQ_XLON")
    elif ticker.endswith("_EQ_XLON"):
        # Canonical XLON → compact ISA
        alts.append(f"{ticker[:-8]}l_EQ")
    elif ticker.endswith("_EQ"):
        # Bare _EQ → both London variants
        base = ticker[:-3]
        alts.append(f"{base}l_EQ")
        alts.append(f"{base}_EQ_XLON")
    return alts


def _mark_untradeable(t212_ticker: str, reason: str) -> None:
    """Flag an instrument so future trade plans skip it.  Use for tickers
    whose orders T212 persistently rejects (e.g. seeded demo positions
    that report ownership via portfolio API but reject orders)."""
    conn = get_db()
    try:
        # Try matching both canonical and any alternative form we know about
        candidates = {t212_ticker, *_alternative_ticker_forms(t212_ticker)}
        for tk in candidates:
            cur = conn.execute(
                "UPDATE instrument_groups SET tradeable=0, updated_at=? WHERE t212_ticker=?",
                (datetime.now(timezone.utc).isoformat(), tk),
            )
            if cur.rowcount:
                log.warning(f"Marked {tk} as untradeable: {reason}")
        conn.commit()
    finally:
        conn.close()


def place_market_order(
    cfg: dict,
    t212_ticker: str,
    quantity: float,
    precision: int = 2,
) -> dict:
    """Place a market order. Positive = BUY, negative = SELL.

    `precision` is the max number of decimal places T212 allows for this
    instrument's order quantity (from the catalog).  The quantity is
    floored in absolute terms to that precision before submission, and the
    order is skipped if the result rounds to zero.
    """
    if not cfg["t212_token"]:
        return {"error": "No t212_token configured"}

    qty = _round_down_quantity(float(quantity), precision)
    if qty == 0.0:
        log.info(f"Skip {t212_ticker}: quantity {quantity} rounds to 0 at precision={precision}")
        return {"skipped": "quantity rounds to 0", "requested": quantity, "precision": precision}

    # Try the primary ticker first, then alternative forms on "not-owned" only.
    tickers_tried: list[str] = []
    last_error: dict = {}
    for attempt_ticker in [t212_ticker, *_alternative_ticker_forms(t212_ticker)]:
        tickers_tried.append(attempt_ticker)
        payload = {"ticker": attempt_ticker, "quantity": qty}
        log.info(f"T212 order: {payload}  base={cfg['t212_base']}  (precision={precision})")
        try:
            r = requests.post(
                f"{cfg['t212_base']}/api/v0/equity/orders/market",
                headers=_t212_headers(cfg), json=payload, timeout=20,
            )
        except Exception as exc:
            last_error = {"error": str(exc), "ticker": attempt_ticker, "quantity": qty}
            continue

        if r.ok:
            if attempt_ticker != t212_ticker:
                log.info(f"Order succeeded with alternative ticker: {attempt_ticker} (primary {t212_ticker} failed)")
            return {**r.json(), "ticker_used": attempt_ticker, "tickers_tried": tickers_tried}

        body = r.text[:500]
        log.warning(f"T212 order rejected {r.status_code} for {attempt_ticker}: {body}")
        last_error = {"error": f"{r.status_code}", "detail": body, "ticker": attempt_ticker, "quantity": qty}

        # Only retry alternatives when the failure mode is "not-owned" — for
        # any other class of error (precision, insufficient funds, etc.) the
        # alternative ticker won't help.
        if r.status_code != 400 or "selling-equity-not-owned" not in body:
            break

    last_error["tickers_tried"] = tickers_tried
    return last_error


# ── Yahoo Finance ─────────────────────────────────────────────────────────────

def fetch_yahoo_history(symbols: list, period: str = "13mo") -> pd.DataFrame:
    try:
        yf.set_tz_cache_location("/data/yf_cache")
    except Exception:
        pass

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

    log.info(f"Fetching {len(symbols)} tickers individually…")
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

    return pd.DataFrame(frames).dropna(how="all")


def _to_gbp_v2(price: float, currency_code: str, eurgbp: float, eurusd: float = 1.0) -> float:
    """Convert a price to GBP using the instrument's authoritative currency code.

    GBX (pence)  → divide by 100  (Yahoo .L and T212 LSE prices are both in pence)
    GBP          → as-is
    EUR          → multiply by EUR/GBP rate
    USD          → multiply by EUR/GBP ÷ EUR/USD  (USD→EUR→GBP via cross rate)
    other        → returned unchanged with a warning log
    """
    cc = (currency_code or "GBP").upper()
    if cc == "GBX":
        return price / 100.0
    if cc == "GBP":
        return price
    if cc == "EUR":
        return price * eurgbp if eurgbp > 0 else price
    if cc == "USD":
        return price * (eurgbp / eurusd) if eurgbp > 0 and eurusd > 0 else price
    log.warning(f"Unknown currency_code '{currency_code}' — price returned unconverted")
    return price


# ── Analytics helpers (unchanged) ─────────────────────────────────────────────

def _period_return(series: pd.Series, bars: int) -> Optional[float]:
    s = series.dropna()
    return float((s.iloc[-1] / s.iloc[-bars - 1] - 1) * 100) if len(s) >= bars + 1 else None


def _return_since_date(series: pd.Series, purchase_date_str: str) -> Optional[float]:
    s = series.dropna()
    if s.empty:
        return None
    try:
        ts = pd.Timestamp(purchase_date_str)
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
    f  = float(s.ewm(span=fast, adjust=False).mean().iloc[-1])
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
    return round(
        float(h.iloc[-1] / h.iloc[-bars] - 1) * 100
        - float(b.iloc[-1] / b.iloc[-bars] - 1) * 100,
        2,
    )


def _correlation_vs_benchmark(holding: pd.Series, benchmark: pd.Series, bars: int = 63) -> Optional[float]:
    common = holding.dropna().index.intersection(benchmark.dropna().index)
    if len(common) < bars + 1:
        return None
    rets = holding.loc[common].pct_change().dropna().iloc[-bars:]
    bench_rets = benchmark.loc[common].pct_change().dropna().iloc[-bars:]
    if len(rets) < 10 or len(bench_rets) < 10:
        return None
    return float(rets.corr(bench_rets) or 0.0)


def _volatility(series: pd.Series, bars: int = 63) -> Optional[float]:
    s = series.dropna()
    if len(s) < bars + 1:
        return None
    rets = s.pct_change().dropna().iloc[-bars:]
    return float(rets.std()) if len(rets) > 1 else None


def _wma_trend_score(price_series: pd.Series, lookback: int = 126) -> float:
    s = price_series.dropna()
    if len(s) < lookback + 2:
        return 0.0
    rets = s.pct_change(fill_method=None).dropna().iloc[-lookback:]
    if len(rets) < 10:
        return 0.0
    vol = float(rets.std())
    if vol <= 0:
        return 0.0
    weights = np.linspace(1.0, 2.0, len(rets))
    weights /= weights.sum()
    return round(float(np.dot(rets.values, weights)) / vol, 4)


def _portfolio_cvar(weights_pct: dict, hist_df: pd.DataFrame, alpha: float = 0.95) -> float:
    syms = [s for s in weights_pct if s in hist_df.columns and weights_pct.get(s, 0) > 0]
    if not syms:
        return 0.0
    rets = hist_df[syms].pct_change(fill_method=None).dropna()
    if len(rets) < 30:
        return 0.0
    w = np.array([weights_pct[s] for s in syms], dtype=float)
    w /= w.sum()
    port_rets = rets[syms].values @ w
    cutoff    = max(int((1.0 - alpha) * len(port_rets)), 1)
    tail      = np.sort(port_rets)[:cutoff]
    return round(float(-tail.mean()), 6)


# ── Core snapshot ─────────────────────────────────────────────────────────────

def _sanity_check_snapshot(new_value: float, cfg: dict) -> tuple[bool, str]:
    """Compare new_value against the last saved snapshot.
    Returns (True, "") if safe to save, or (False, reason) if rejected.
    Set max_snapshot_change_pct=0 to disable.
    """
    threshold = cfg.get("max_snapshot_change_pct", 20)
    if threshold <= 0:
        return True, ""
    conn = get_db()
    row  = conn.execute(
        "SELECT portfolio_value FROM snapshots ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return True, ""   # First snapshot ever — always allow
    last_value = float(row["portfolio_value"])
    if last_value <= 0:
        return True, ""
    change_pct = abs(new_value - last_value) / last_value * 100
    if change_pct > threshold:
        reason = (
            f"Snapshot rejected: portfolio value £{new_value:.2f} differs from "
            f"last snapshot £{last_value:.2f} by {change_pct:.1f}% "
            f"(threshold {threshold}%). Likely a data error — not saved."
        )
        log.warning(reason)
        return False, reason
    return True, ""


def compute_snapshot() -> dict:
    cfg = load_config()
    log.info(f"=== Snapshot started — phase={cfg['portfolio_phase']}  T212={cfg['t212_base']} ===")

    # ── Fetch live T212 data ────────────────────────────────────────────────────
    raw_positions = fetch_t212_portfolio(cfg)   # raises 503 on failure
    cash_data     = fetch_t212_cash(cfg)

    # ── Instrument catalog (cached) ────────────────────────────────────────────
    catalog = fetch_instrument_catalog(cfg)

    # ── Seed new instruments into the groups DB ────────────────────────────────
    conn = get_db()
    new_count = _seed_new_instruments(raw_positions, catalog, conn)
    if new_count:
        log.info(f"{new_count} new instrument(s) added to instrument_groups as 'unassigned'")

    groups_db = _load_instrument_groups(conn)
    conn.close()

    # ── EUR rates ───────────────────────────────────────────────────────────────
    # Fetch FX first with a lightweight call so we can convert prices.
    # We'll merge into the main Yahoo history fetch below.
    fx_symbols = ["EURGBP=X", "EURUSD=X"]

    # ── Build holdings list from live positions + catalog + groups ─────────────
    holdings = []
    for pos in raw_positions:
        raw_ticker = pos.get("ticker", "")
        quantity   = float(pos.get("quantity", 0))
        if not raw_ticker or quantity <= 0:
            continue

        canonical   = _normalize_isa_ticker(raw_ticker, catalog)
        instrument  = catalog.get(canonical, {})
        yahoo_sym   = _validate_yahoo_symbol(
            instrument.get("yahoo_symbol") or _t212_ticker_to_yahoo(canonical),
            canonical,
            instrument.get("exchange", ""),
        )
        currency_cc = instrument.get("currency_code", "GBP")
        display_nm  = instrument.get("name") or instrument.get("shortName") or yahoo_sym

        group_row          = groups_db.get(canonical, {})
        group_label        = group_row.get("group_label", "unassigned")
        initial_weight_pct = float(group_row.get("initial_weight_pct") or 0)
        # Default to tradeable=True when row missing (pre-2.7.0 DBs)
        tradeable          = bool(group_row.get("tradeable", 1))

        avg_price_raw = float(pos.get("averagePrice") or 0)
        cur_price_raw = float(pos.get("currentPrice") or avg_price_raw)

        # Quantity precision for orders.  Prefer the catalog value at canonical
        # key; fall back to the raw ticker (which is what's actually returned by
        # the portfolio API) so that ISA-compact-only catalog entries still work.
        qty_prec = (
            instrument.get("quantity_precision")
            if instrument else None
        )
        if qty_prec is None:
            raw_inst = catalog.get(raw_ticker, {})
            qty_prec = raw_inst.get("quantity_precision")
        if qty_prec is None:
            qty_prec = 2  # safe default

        holdings.append({
            "yahoo_symbol":      yahoo_sym,
            "t212_ticker":       canonical,
            "raw_t212_ticker":   raw_ticker,
            "currency_code":     currency_cc,
            "display_name":      display_nm,
            "quantity":          quantity,
            "avg_price_raw":     avg_price_raw,   # in instrument currency (may be pence)
            "cur_price_raw":     cur_price_raw,   # in instrument currency (may be pence)
            "group":             group_label,
            "initial_weight_pct": initial_weight_pct,
            "quantity_precision": int(qty_prec),
            "tradeable":         tradeable,
        })

    if not holdings:
        raise HTTPException(503, "T212 returned no holdings — cannot build snapshot")

    log.info(f"Holdings: {len(holdings)}  unassigned: {sum(1 for h in holdings if h['group'] == 'unassigned')}")

    # ── Dynamic risk axis (weight_mode="dynamic" only) ────────────────────────
    # Compute effective risk from user's base risk_score plus optional VIX/drawdown
    # auto-adjustment.  Uses the most recent stored snapshot for VIX + peak — this
    # snapshot's own VIX is fetched later and can drift the next run, but the lag
    # is acceptable given snapshots are daily.
    risk_score    = float(cfg.get("risk_score", 65))
    effective_risk = risk_score
    risk_reason    = "weight_mode != dynamic"
    drawdown_pct   = 0.0
    interp_allocs  = None

    if cfg.get("weight_mode") == "dynamic":
        conn = get_db()
        prev = conn.execute(
            "SELECT portfolio_value, benchmarks_json FROM snapshots ORDER BY as_of DESC LIMIT 1"
        ).fetchone()
        peak_row = conn.execute("SELECT MAX(portfolio_value) AS peak FROM snapshots").fetchone()
        # 21-day-ago value for rally signal (bidirectional auto-adjust)
        rally_row = conn.execute(
            "SELECT portfolio_value FROM snapshots WHERE as_of <= datetime('now', '-21 days') "
            "ORDER BY as_of DESC LIMIT 1"
        ).fetchone()
        conn.close()

        # Best-effort VIX, peak, drawdown (lagged one snapshot), and 21d rally
        prev_vix = 0.0
        if prev and prev["benchmarks_json"]:
            try:
                bm = json.loads(prev["benchmarks_json"])
                prev_vix = float(bm.get("vix", {}).get("latest") or 0.0)
            except Exception:
                pass
        peak = float(peak_row["peak"]) if peak_row and peak_row["peak"] else 0.0
        prev_value   = float(prev["portfolio_value"]) if prev and prev["portfolio_value"] else 0.0
        drawdown_lag = (prev_value - peak) / peak * 100 if peak > 0 and prev_value > 0 else 0.0
        rally_21d    = (
            (prev_value - float(rally_row["portfolio_value"])) / float(rally_row["portfolio_value"]) * 100
            if rally_row and rally_row["portfolio_value"] and prev_value > 0 else 0.0
        )

        effective_risk, risk_reason = _compute_effective_risk(
            risk_score, prev_vix, drawdown_lag, rally_21d, cfg
        )
        interp_allocs = _interpolate_group_allocations(effective_risk)
        cfg["effective_risk"]            = effective_risk
        cfg["effective_risk_reason"]     = risk_reason
        cfg["dynamic_group_allocations"] = interp_allocs
        log.info(
            f"Dynamic mode: risk={risk_score:.0f} → effective={effective_risk:.1f} "
            f"({risk_reason}); group totals={ {g: round(v,1) for g,v in interp_allocs.items()} }"
        )

    # ── Compute target weights ─────────────────────────────────────────────────
    target_weights = _compute_target_weights(holdings, cfg)

    # Augment cfg with holdings-derived lookups for _compute_rebalance
    cfg["target_weights"] = target_weights
    cfg["symbol_groups"]  = {h["yahoo_symbol"]: h["group"] for h in holdings}
    cfg["yahoo_to_t212"]  = {h["yahoo_symbol"]: h["t212_ticker"] for h in holdings}

    # ── Fetch Yahoo Finance history ────────────────────────────────────────────
    all_symbols = (
        [h["yahoo_symbol"] for h in holdings]
        + list(BENCHMARKS.values())
        + fx_symbols
    )
    hist = fetch_yahoo_history(list(dict.fromkeys(all_symbols)))  # deduplicated

    # Extract FX rates
    eurgbp = 1.0
    eurusd = 1.0
    if "EURGBP=X" in hist.columns:
        s = hist["EURGBP=X"].dropna()
        if not s.empty:
            eurgbp = float(s.iloc[-1])
            log.info(f"EUR/GBP: {eurgbp:.4f}")
    if "EURUSD=X" in hist.columns:
        s = hist["EURUSD=X"].dropna()
        if not s.empty:
            eurusd = float(s.iloc[-1])

    # ── Build positions ────────────────────────────────────────────────────────
    positions   = []
    total_value = 0.0

    for h in holdings:
        sym    = h["yahoo_symbol"]
        cc     = h["currency_code"]
        qty    = h["quantity"]

        # Convert prices from instrument currency → GBP
        avg_price = _to_gbp_v2(h["avg_price_raw"], cc, eurgbp, eurusd)
        cur_t212  = _to_gbp_v2(h["cur_price_raw"], cc, eurgbp, eurusd) if h["cur_price_raw"] else 0.0

        # T212 price preferred (already in account currency after conversion);
        # fall back to Yahoo if T212 price is zero or missing.
        if cur_t212 > 0:
            current_price = cur_t212
        elif sym in hist.columns and not hist[sym].dropna().empty:
            yahoo_raw     = float(hist[sym].dropna().iloc[-1])
            current_price = _to_gbp_v2(yahoo_raw, cc, eurgbp, eurusd)
        else:
            current_price = avg_price

        market_value = qty * current_price
        cost_basis   = qty * avg_price
        pnl_pct      = (current_price / avg_price - 1) * 100 if avg_price > 0 else 0.0
        target_wt    = target_weights.get(sym, 0)

        log.debug(f"{sym} ({cc}): qty={qty:.4f}  avg=£{avg_price:.4f}  "
                  f"cur=£{current_price:.4f}  val=£{market_value:.2f}")

        total_value += market_value
        positions.append({
            "symbol":             sym,
            "t212_ticker":        h["t212_ticker"],
            "raw_t212_ticker":    h.get("raw_t212_ticker", h["t212_ticker"]),
            "display_name":       h["display_name"],
            "quantity":           round(qty, 6),
            "avg_price":          round(avg_price, 4),
            "current_price":      round(current_price, 4),
            "market_value":       round(market_value, 2),
            "cost_basis":         round(cost_basis, 2),
            "pnl_pct":            round(pnl_pct, 2),
            "group":              h["group"],
            "group_order":        GROUP_ORDER.get(h["group"], 9),
            "target_wt":          target_wt,
            "quantity_precision": h.get("quantity_precision", 2),
            "tradeable":          h.get("tradeable", True),
        })

    cash = float(cash_data.get("free", 0.0))
    total_value += cash

    # Drift
    for p in positions:
        actual_wt      = p["market_value"] / total_value * 100 if total_value else 0.0
        drift_abs      = actual_wt - p["target_wt"]
        drift_rel      = drift_abs / p["target_wt"] * 100 if p["target_wt"] else 0.0
        p["actual_wt"] = round(actual_wt, 2)
        p["drift_abs"] = round(drift_abs, 2)
        p["drift_rel"] = round(drift_rel, 2)

    # ── Target-weight safeguard ────────────────────────────────────────────────
    # Validate the computed targets against the live actual weights.  If the
    # validator rejects them (e.g. an active holding got target=0 due to a
    # partial-data bug), recover from the last known good targets, or fall
    # back to equal weight if no recovery snapshot exists.  This stops bad
    # rebalance suggestions from ever leaving the snapshot pipeline.
    ok, reason = _validate_targets(positions)
    if not ok:
        log.error(f"Target validation FAILED: {reason}")
        if _restore_last_good_targets(positions):
            log.warning("Recovered targets from last_good_targets table")
        else:
            n = len(positions)
            equal = _round_weights_to_integers({p["symbol"]: 100.0 / n for p in positions})
            for p in positions:
                new_target     = equal[p["symbol"]]
                p["target_wt"] = new_target
                p["drift_abs"] = round(p["actual_wt"] - new_target, 2)
                p["drift_rel"] = round(
                    (p["actual_wt"] - new_target) / new_target * 100 if new_target else 0.0, 2
                )
            log.warning(f"No last-good targets available — fell back to equal weight ({100/n:.1f}% each)")
    else:
        # Targets are sane — persist as the new recovery baseline
        try:
            _save_last_good_targets(positions)
        except Exception as exc:
            log.warning(f"Could not persist last_good_targets: {exc}")

    max_drift_rel = max((abs(p["drift_rel"]) for p in positions), default=0.0)
    total_cost    = sum(p["cost_basis"] for p in positions)
    portfolio_return_pct = (total_value - total_cost) / total_cost * 100 if total_cost else 0.0

    # Group summary
    group_summary: dict[str, dict] = {}
    for p in positions:
        g = p.get("group", "global_beta")
        if g not in group_summary:
            group_summary[g] = {"label": GROUP_LABELS.get(g, g), "actual_wt": 0.0, "target_wt": 0.0}
        group_summary[g]["actual_wt"] = round(group_summary[g]["actual_wt"] + p["actual_wt"], 2)
        group_summary[g]["target_wt"] = round(group_summary[g]["target_wt"] + p["target_wt"], 2)

    # Benchmarks
    benchmarks   = {}
    world_series = hist.get("URTH")
    purchase_date = cfg.get("purchase_date", "2026-04-07")
    for name, ticker in BENCHMARKS.items():
        if ticker not in hist.columns:
            continue
        s = hist[ticker].dropna()
        if s.empty:
            continue
        since_purchase = _return_since_date(s, purchase_date)
        benchmarks[name] = {
            "ticker":                ticker,
            "latest":                round(float(s.iloc[-1]), 2),
            "return_1d":             round(_period_return(s, 1)   or 0.0, 2),
            "return_1w":             round(_period_return(s, 5)   or 0.0, 2),
            "return_1m":             round(_period_return(s, 21)  or 0.0, 2),
            "return_3m":             round(_period_return(s, 63)  or 0.0, 2),
            "return_6m":             round(_period_return(s, 126) or 0.0, 2),
            "return_since_purchase": since_purchase if since_purchase is not None else 0.0,
        }
        log.info(f"Benchmark {name}: since_purchase={since_purchase}%  1m={benchmarks[name]['return_1m']}%")

    vix = benchmarks.get("vix", {}).get("latest", 0.0)

    # Momentum
    momentum: dict[str, dict] = {}
    for h in holdings:
        sym = h["yahoo_symbol"]
        if sym not in hist.columns:
            momentum[sym] = {}
            continue
        s  = hist[sym].dropna()
        rs = _rs_vs_world(s, world_series, 63) if world_series is not None else None
        sp500 = hist.get(BENCHMARKS["sp500"])
        corr_sp500 = None
        if sp500 is not None:
            corr_sp500 = _correlation_vs_benchmark(s, sp500, 63)
        return_3m = round(_period_return(s, 63) or 0.0, 2)
        alpha_vs_sp500_3m = None
        if sp500 is not None:
            sp500_return_3m = _period_return(sp500, 63)
            if sp500_return_3m is not None:
                alpha_vs_sp500_3m = round(return_3m - sp500_return_3m, 2)

        momentum[sym] = {
            "momentum_12m":   round(v, 2) if (v := _momentum(s, 252))    is not None else None,
            "momentum_9m":    round(v, 2) if (v := _momentum(s, 189))    is not None else None,
            "momentum_6m":    round(v, 2) if (v := _momentum(s, 126))    is not None else None,
            "momentum_3m":    round(v, 2) if (v := _momentum(s, 63, 0))  is not None else None,
            "trend":          _ema_trend(s),
            "trend_score":    _wma_trend_score(s, 126),
            "return_1m":      round(_period_return(s, 21) or 0.0, 2),
            "return_3m":      return_3m,
            "rs_vs_world_3m": rs,
            "alpha_vs_sp500_3m": alpha_vs_sp500_3m,
            "corr_sp500_3m":  round(corr_sp500, 4) if corr_sp500 is not None else None,
            "volatility_3m":  _volatility(s, 63),
            "volatility_6m":  _volatility(s, 126),
            "group":          h["group"],
            "group_order":    GROUP_ORDER.get(h["group"], 9),
        }

        momentum[sym]["signal_score"] = _signal_score(momentum[sym])

    mom_scores: dict[str, float] = {}
    for sym, m in momentum.items():
        if not m:
            mom_scores[sym] = 0.0
            continue
        trend = (m.get("trend_score") or 0.0) * 100
        m6    =  m.get("momentum_6m")  or 0.0
        m12   =  m.get("momentum_12m") or 0.0
        mom_scores[sym] = round(trend * 0.5 + m6 * 0.3 + m12 * 0.2, 2)

    if cfg.get("weight_mode") == "dynamic" and any(h["group"] != "unassigned" for h in holdings):
        signal_weights = _signal_adjusted_within_group_weights(target_weights, momentum, holdings)
        if signal_weights != target_weights:
            log.info("Dynamic mode: applied signal-driven within-group tilting to target weights")
        target_weights = signal_weights
    cfg["target_weights"] = target_weights

    # Live drawdown vs all-time peak (used by cooldown auto-override below)
    conn_dd = get_db()
    pk_dd = conn_dd.execute("SELECT MAX(portfolio_value) AS peak FROM snapshots").fetchone()
    conn_dd.close()
    pk_dd_val = float(pk_dd["peak"]) if pk_dd and pk_dd["peak"] else total_value
    cfg["drawdown_pct"] = round((total_value - pk_dd_val) / pk_dd_val * 100 if pk_dd_val > 0 else 0.0, 2)

    rebalance_needed, rebalance_reason, suggested_actions = _compute_rebalance(
        cfg, positions, mom_scores, momentum, vix, total_value, max_drift_rel, hist
    )
    log.info(f"Rebalance: {rebalance_needed} — {rebalance_reason}")
    if suggested_actions:
        for a in suggested_actions:
            flag = " [BALANCING]" if a.get("balancing_trade") else ""
            log.info(f"  Trade: {a['action']:4s} {a['symbol']:12s}  "
                     f"cur={a['current_wt']:.1f}%  tgt={a['target_wt']:.1f}%  "
                     f"£{a['delta_value']:+.0f}  {a['delta_units']:+.6f} units{flag}")

    # ── Sanity check ───────────────────────────────────────────────────────────
    unassigned_count = sum(1 for h in holdings if h["group"] == "unassigned")
    ok, reject_reason = _sanity_check_snapshot(total_value, cfg)
    if not ok:
        return {
            "rejected":       True,
            "reason":         reject_reason,
            "portfolio_value": round(total_value, 2),
        }

    # ── Persist ────────────────────────────────────────────────────────────────
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn  = get_db()
    # Drawdown vs all-time peak (this snapshot included)
    pk = conn.execute("SELECT MAX(portfolio_value) AS peak FROM snapshots").fetchone()
    pk_val = float(pk["peak"]) if pk and pk["peak"] else total_value
    drawdown_now = round((total_value - pk_val) / pk_val * 100 if pk_val > 0 else 0.0, 2)

    # Pie detection — used by approval/execution to switch to push-to-pie mode.
    # Best-effort: if T212 is unreachable, leaves pie_detected=False and the
    # rebalancer falls back to direct-order behaviour as before.
    active_pie    = _detect_active_pie(cfg)
    pie_detected  = active_pie is not None
    pie_id        = int(active_pie["id"]) if active_pie else None

    metadata = {
        "weight_mode":           cfg.get("weight_mode", "stored"),
        "portfolio_phase":       cfg.get("portfolio_phase", "Momentum-Chill"),
        "risk_score":            int(risk_score),
        "effective_risk":        round(effective_risk, 1),
        "effective_risk_reason": risk_reason,
        "drawdown_pct":          drawdown_now,
        "cooldown_override_used":   bool(cfg.get("_cooldown_override_used", False)),
        "cooldown_override_reason": cfg.get("_cooldown_override_reason", ""),
        "dynamic_group_allocations": (
            {g: round(v, 1) for g, v in interp_allocs.items()} if interp_allocs else None
        ),
        "pie_detected":          pie_detected,
        "pie_id":                pie_id,
        "execution_mode":        "pie" if pie_detected else "direct",
    }

    conn.execute("""
        INSERT OR REPLACE INTO snapshots
          (as_of, portfolio_value, invested_value, cash, portfolio_return_pct,
           positions_json, benchmarks_json, drift_json, momentum_json,
           rebalance_needed, rebalance_reason, suggested_actions, approved, executed,
           metadata_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)
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
        json.dumps(metadata),
    ))
    conn.commit()
    conn.close()

    log.info(f"Snapshot saved: £{total_value:.2f}  return={portfolio_return_pct:.2f}%  "
             f"max_drift={max_drift_rel:.1f}%  rebalance={rebalance_needed}  vix={vix}  "
             f"unassigned={unassigned_count}")

    return {
        "as_of":                as_of,
    "collector_version":    "2.7.2",
        "weight_mode":          cfg.get("weight_mode", "stored"),
        "portfolio_phase":      cfg.get("portfolio_phase", "Momentum-Chill"),
        "risk_score":           int(risk_score),
        "effective_risk":       round(effective_risk, 1),
        "effective_risk_reason": risk_reason,
        "drawdown_pct":         drawdown_now,
        "dynamic_group_allocations": (
            {g: round(v, 1) for g, v in interp_allocs.items()} if interp_allocs else None
        ),
        "portfolio_value":      round(total_value, 2),
        "invested_value":       round(total_cost, 2),
        "cash":                 round(cash, 2),
        "portfolio_return_pct": round(portfolio_return_pct, 2),
        "positions":            positions,
        "benchmarks":           benchmarks,
        "momentum":             momentum,
        "rebalance_needed":     rebalance_needed,
        "rebalance_reason":     rebalance_reason,
        "suggested_actions":    suggested_actions,
        "vix":                  vix,
        "approved":             False,
        "group_summary":        group_summary,
        "unassigned_count":     unassigned_count,
    }


def _compute_rebalance(cfg, positions, mom_scores, momentum, vix, total_value, max_drift_rel, hist=None):
    vix_high      = cfg["vix_high_threshold"]
    vix_extreme   = cfg["vix_extreme_threshold"]
    cooldown_days = cfg["min_days_between_rebalance"]

    # ── Cooldown gate — with override paths ────────────────────────────────────
    # Cooldown can be bypassed by:
    #   a) Manual notch-up flag (one-shot, always honoured)
    #   b) Auto override on extreme conditions when cooldown_override_enabled:
    #        VIX > cooldown_override_vix_threshold, OR
    #        drawdown < cooldown_override_drawdown_threshold
    conn = get_db()
    row  = conn.execute(
        "SELECT approved_at FROM snapshots WHERE executed=1 ORDER BY executed_at DESC LIMIT 1"
    ).fetchone()
    days_since = None
    cooldown_blocked = False
    if row and row["approved_at"]:
        try:
            days_since = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(row["approved_at"].replace("Z", "+00:00"))).days
            if days_since < cooldown_days:
                cooldown_blocked = True
        except Exception:
            pass

    override_used   = False
    override_reason = ""
    if cooldown_blocked:
        # 1. Manual notch-up always wins
        if _get_runtime_flag(conn, "notch_up_pending") == "1":
            override_used   = True
            override_reason = "manual notch-up"
            _clear_runtime_flag(conn, "notch_up_pending")
            log.warning(f"COOLDOWN OVERRIDE: {override_reason} — bypassing {days_since}d cooldown")
        # 2. Auto override on extreme conditions (defensive bias)
        elif cfg.get("cooldown_override_enabled", False):
            vix_thresh = float(cfg.get("cooldown_override_vix_threshold", vix_high + 5))
            dd_thresh  = float(cfg.get("cooldown_override_drawdown_threshold", -15.0))
            drawdown   = float(cfg.get("drawdown_pct", 0.0))
            if vix > vix_thresh:
                override_used   = True
                override_reason = f"VIX={vix:.1f} > {vix_thresh:.0f} auto-override"
            elif drawdown < dd_thresh:
                override_used   = True
                override_reason = f"drawdown={drawdown:.1f}% < {dd_thresh:.0f}% auto-override"
            if override_used:
                log.warning(f"COOLDOWN OVERRIDE: {override_reason} — bypassing {days_since}d cooldown")

        if not override_used:
            conn.close()
            return False, f"Cooldown: {days_since}d since last rebalance (min {cooldown_days}d)", []

    conn.close()
    cfg["_cooldown_override_used"]   = override_used
    cfg["_cooldown_override_reason"] = override_reason

    if vix > vix_extreme:
        return False, f"VIX={vix:.1f} — extreme volatility, rebalancing frozen", []

    def _int_gap(p):
        return abs(round(p["actual_wt"]) - int(p["target_wt"]))

    if vix > vix_high:
        min_gap = 2
        drifted = [p for p in positions if _int_gap(p) >= min_gap and p.get("tradeable", True)]
        if not drifted:
            return False, f"VIX={vix:.1f} elevated — no tradeable holding ≥2 pts off target, holding", []
    else:
        min_gap = 1
        drifted = [p for p in positions if _int_gap(p) >= min_gap and p.get("tradeable", True)]

    if not drifted:
        return False, "No holding is ≥1 integer point from target — no trade needed", []

    adj_weights = _momentum_adjusted_weights(cfg["target_weights"], mom_scores, vix, vix_high)

    max_cvar = cfg.get("max_cvar_pct", 5.0) / 100.0
    if hist is not None and not hist.empty and max_cvar > 0:
        cvar = _portfolio_cvar(adj_weights, hist)
        if cvar > max_cvar:
            defensive_syms = {sym for sym, g in cfg["symbol_groups"].items() if g == "defensive"}
            scale  = max_cvar / cvar
            scaled = {sym: (w if sym in defensive_syms else w * scale)
                      for sym, w in adj_weights.items()}
            total_s    = sum(scaled.values())
            adj_weights = {sym: v / total_s * 100 for sym, v in scaled.items()}
            log.info(f"CVaR={cvar:.4f} > limit={max_cvar:.4f} — defensive tilt applied")

    traded_syms = set()

    def _make_action(p, delta_val, balancing=False):
        sym         = p["symbol"]
        precision   = int(p.get("quantity_precision", 2))
        raw_units   = delta_val / p["current_price"] if p["current_price"] else 0.0
        # Round DOWN in absolute terms so we never spend more than budgeted (BUY)
        # or sell more than owned (SELL).
        delta_units = _round_down_quantity(raw_units, precision)
        return {
            "symbol":             sym,
            # Use the ticker T212's portfolio API actually returned for this
            # position.  That's the form the orders endpoint will recognise;
            # the canonical/normalised form may not match for ISA accounts.
            "t212_ticker":        p.get("raw_t212_ticker", p["t212_ticker"]),
            "action":             "BUY" if delta_val > 0 else "SELL",
            "current_wt":         p["actual_wt"],
            "target_wt":          round(adj_weights[sym], 2),
            "original_target_wt": int(cfg["target_weights"][sym]),
            "delta_value":        round(delta_val, 2),
            "delta_units":        delta_units,
            "quantity_precision": precision,
            "current_value":      round(p["market_value"], 2),
            "target_value":       round(total_value * adj_weights[sym] / 100, 2),
            "drift_rel":          p["drift_rel"],
            "momentum_score":     mom_scores.get(sym, 0.0),
            "balancing_trade":    balancing,
        }

    actions   = []
    cost_rate = cfg.get("cost_rate_pct", 0.1) / 100.0
    for p in positions:
        if _int_gap(p) < min_gap:
            continue
        sym       = p["symbol"]
        if sym not in adj_weights:
            continue
        delta_val = total_value * adj_weights[sym] / 100 - p["market_value"]
        if abs(delta_val) < 10.0:
            continue
        trade_cost       = abs(delta_val) * cost_rate
        expected_benefit = abs(delta_val) * abs(p["drift_rel"]) / 100.0
        if expected_benefit <= trade_cost:
            log.info(f"  Skipped {sym}: cost filter — benefit £{expected_benefit:.2f} ≤ cost £{trade_cost:.2f}")
            continue
        actions.append(_make_action(p, delta_val))
        traded_syms.add(sym)

    net = sum(a["delta_value"] for a in actions)
    if abs(net) >= 10.0:
        untouched = [p for p in positions if p["symbol"] not in traded_syms]
        if net < 0:
            best = max(untouched, key=lambda p: p["target_wt"] - p["actual_wt"], default=None)
        else:
            best = max(untouched, key=lambda p: p["actual_wt"] - p["target_wt"], default=None)
        if best:
            actions.append(_make_action(best, -net, balancing=True))

    actions.sort(key=lambda x: (x["action"] != "SELL", -abs(x["delta_value"])))
    drifted_summary = ", ".join(
        f"{p['symbol']} ({p['actual_wt']:.1f}%→{int(p['target_wt'])}%)"
        for p in drifted
    )
    reason = (
        f"{len(drifted)} holding(s) crossed integer target: {drifted_summary}"
        + (f"; VIX elevated ({vix:.1f}) — ≥2pt gap required" if vix > vix_high else "")
    )
    return True, reason, actions


def _momentum_adjusted_weights(target_weights, mom_scores, vix, vix_high):
    dampen = 0.5 if vix > vix_high else 1.0
    raw    = {}
    for sym, base_wt in target_weights.items():
        score = mom_scores.get(sym, 0.0)
        if   score > 10:  mult = 1.0 + 0.20 * dampen
        elif score >  5:  mult = 1.0 + 0.10 * dampen
        elif score < -10: mult = 1.0 - 0.20 * dampen
        elif score <  -5: mult = 1.0 - 0.10 * dampen
        else:             mult = 1.0
        raw[sym] = base_wt * mult
    total = sum(raw.values())
    return {sym: v / total * 100 for sym, v in raw.items()}


# ── Groups HTML page ──────────────────────────────────────────────────────────

_GROUPS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Portfolio Groups</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body { background: #111827; color: #e5e7eb; font-family: system-ui, -apple-system, sans-serif;
           margin: 0; padding: 1.25rem; }
    h1   { font-size: 1.2rem; color: #f9fafb; margin: 0 0 0.25rem; }
    .sub { color: #9ca3af; font-size: 0.8rem; margin: 0 0 1.25rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th   { text-align: left; padding: 0.45rem 0.75rem; border-bottom: 2px solid #374151;
           color: #9ca3af; font-weight: 600; text-transform: uppercase;
           font-size: 0.7rem; letter-spacing: 0.05em; white-space: nowrap; }
    td   { padding: 0.45rem 0.75rem; border-bottom: 1px solid #1f2937; vertical-align: middle; }
    tr:hover td { background: #1a2233; }
    select { background: #1f2937; color: #e5e7eb; border: 1px solid #374151;
             border-radius: 0.35rem; padding: 0.2rem 0.5rem; font-size: 0.82rem;
             cursor: pointer; min-width: 170px; }
    select:focus { outline: none; border-color: #6366f1; }
    select.changed { border-color: #f59e0b; }
    .mono { font-family: monospace; font-size: 0.75rem; color: #6366f1; }
    .dim  { color: #6b7280; font-size: 0.75rem; }
    .badge-new { background: #78350f; color: #fcd34d; font-size: 0.65rem;
                 padding: 0.1rem 0.35rem; border-radius: 0.2rem; margin-right: 0.35rem;
                 font-weight: 600; vertical-align: middle; }
    .st   { font-size: 0.72rem; margin-left: 0.4rem; }
    .ok   { color: #10b981; }
    .err  { color: #ef4444; }
    .saving { color: #f59e0b; }
    .unassigned-row td:first-child { color: #f59e0b; }
  </style>
</head>
<body>
  <h1>Portfolio Instrument Groups</h1>
  <p class="sub">Assign each holding to a group. Groups drive phase allocations.
     New instruments are tagged <strong style="color:#fcd34d">NEW</strong> until assigned.
     Changes save immediately.</p>
  <table>
    <thead>
      <tr><th>Symbol</th><th>T212 Ticker</th><th>Group</th><th></th></tr>
    </thead>
    <tbody id="tbody">
      <tr><td colspan="5" style="text-align:center;color:#9ca3af;padding:2rem">Loading…</td></tr>
    </tbody>
  </table>

  <script>
    const GROUPS = [
      ['momentum_core',      'Momentum Core'],
      ['global_beta',        'Global Beta'],
      ['regional_satellite', 'Regional Satellite'],
      ['defensive',          'Defensive'],
      ['optional_factor',    'Optional Factor'],
      ['unassigned',         'Unassigned'],
    ];

    function base() {
      const p = window.location.pathname;
      const i = p.lastIndexOf('/groups');
      return window.location.origin + (i >= 0 ? p.slice(0, i) : p);
    }

    function rowId(ticker) { return 'r-' + btoa(ticker).replace(/=/g, ''); }
    function stId(ticker)  { return 's-' + btoa(ticker).replace(/=/g, ''); }

    async function load() {
      const tbody = document.getElementById('tbody');
      try {
        const res  = await fetch(base() + '/api/groups');
        const data = await res.json();
        if (!data.length) {
          tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#9ca3af;padding:2rem">' +
            'No instruments yet. Run a snapshot first.</td></tr>';
          return;
        }
        // Sort: unassigned first, then alphabetical by yahoo_symbol
        data.sort((a, b) => {
          if (a.group_label === 'unassigned' && b.group_label !== 'unassigned') return -1;
          if (b.group_label === 'unassigned' && a.group_label !== 'unassigned') return  1;
          return (a.yahoo_symbol || '').localeCompare(b.yahoo_symbol || '');
        });
        tbody.innerHTML = data.map(row => {
          const isNew = row.group_label === 'unassigned';
          const opts  = GROUPS.map(([v, l]) =>
            `<option value="${v}"${v === row.group_label ? ' selected' : ''}>${l}</option>`
          ).join('');
          const rid = rowId(row.t212_ticker);
          const sid = stId(row.t212_ticker);
          const enc = encodeURIComponent(row.t212_ticker);
          return `<tr id="${rid}" class="${isNew ? 'unassigned-row' : ''}">
            <td class="mono">${isNew ? '<span class="badge-new">NEW</span>' : ''}${row.yahoo_symbol}</td>
            <td class="dim">${row.t212_ticker}</td>
            <td><select id="sel-${rid}" onchange="save('${enc}','${rid}','${sid}',this)">${opts}</select></td>
            <td><span class="st" id="${sid}"></span></td>
          </tr>`;
        }).join('');
      } catch(e) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:#ef4444;padding:2rem">Error: ${e.message}</td></tr>`;
      }
    }

    async function save(encTicker, rid, sid, sel) {
      const st = document.getElementById(sid);
      const tr = document.getElementById(rid);
      st.className = 'st saving'; st.textContent = 'Saving…';
      try {
        const res = await fetch(base() + '/api/groups/' + encTicker, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({group: sel.value}),
        });
        if (res.ok) {
          st.className = 'st ok'; st.textContent = '✓ Saved';
          if (sel.value !== 'unassigned') tr.classList.remove('unassigned-row');
          setTimeout(() => { st.textContent = ''; }, 2500);
        } else {
          const err = await res.json().catch(() => ({}));
          st.className = 'st err'; st.textContent = '✗ ' + (err.detail || 'Error');
        }
      } catch(e) {
        st.className = 'st err'; st.textContent = '✗ Network error';
      }
    }

    load();
  </script>
</body>
</html>"""


# ── FastAPI routes ────────────────────────────────────────────────────────────

@app.get("/")
def root(request: Request):
    """Redirect sidebar / Open Web UI to the groups management page.
    Must include root_path so the redirect stays within the HA ingress
    URL space (e.g. /api/hassio_ingress/TOKEN/groups) rather than
    bouncing to /groups on the HA host (404).
    """
    root_path = request.scope.get("root_path", "").rstrip("/")
    return RedirectResponse(url=f"{root_path}/groups")


@app.get("/api/health")
def health():
    opts = _read_options()
    conn = get_db()
    catalog_count    = conn.execute("SELECT COUNT(*) FROM instrument_catalog").fetchone()[0]
    unassigned_count = conn.execute(
        "SELECT COUNT(*) FROM instrument_groups WHERE group_label='unassigned'"
    ).fetchone()[0]
    oldest_fetched = conn.execute("SELECT MIN(fetched_at) FROM instrument_catalog").fetchone()[0]
    conn.close()
    catalog_age_sec = None
    if oldest_fetched:
        try:
            catalog_age_sec = int((
                datetime.now(timezone.utc)
                - datetime.fromisoformat(oldest_fetched.replace("Z", "+00:00"))
            ).total_seconds())
        except Exception:
            pass
    return {
        "status":           "ok",
        "utc":              datetime.now(timezone.utc).isoformat(),
    "version":          "2.7.2",
        "t212_base":        opts.get("t212_base", "https://demo.trading212.com"),
        "demo_mode":        "demo" in opts.get("t212_base", "demo"),
        "phase":            opts.get("portfolio_phase", "Momentum-Max"),
        "catalog_instruments": catalog_count,
        "catalog_age_sec":  catalog_age_sec,
        "unassigned_count": unassigned_count,
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
def list_snapshots(limit: int = 90, summary: bool = False):
    """List snapshots. ?summary=true returns only as_of + portfolio_value."""
    conn = get_db()
    if summary:
        rows = conn.execute(
            "SELECT as_of, portfolio_value FROM snapshots ORDER BY as_of DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [{"as_of": r["as_of"], "portfolio_value": round(r["portfolio_value"], 2)} for r in rows]
    rows = conn.execute("SELECT * FROM snapshots ORDER BY as_of DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [_row_to_dict(r) for r in rows]


@app.delete("/api/snapshots")
def delete_snapshots(
    date: Optional[str]   = None,
    before: Optional[str] = None,
    after:  Optional[str] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
):
    """Delete snapshots matching one or more filter criteria.

    Query params (any combination):
      date=YYYY-MM-DD     — exact date (matches as_of LIKE 'YYYY-MM-DD%')
      before=YYYY-MM-DD   — all snapshots strictly before this date
      after=YYYY-MM-DD    — all snapshots strictly after this date
      min_value=N         — delete rows where portfolio_value < N (rogue lows)
      max_value=N         — delete rows where portfolio_value > N (rogue spikes)

    Examples:
      DELETE /api/snapshots?date=2026-04-28
      DELETE /api/snapshots?before=2026-05-01
      DELETE /api/snapshots?max_value=50000   ← clean rogue spikes only
      DELETE /api/snapshots?after=2026-04-15&max_value=20000

    At least one filter must be supplied (refuses to delete everything).
    """
    clauses: list = []
    params:  list = []
    if date is not None:
        clauses.append("as_of LIKE ?")
        params.append(f"{date}%")
    if before is not None:
        clauses.append("as_of < ?")
        params.append(before)
    if after is not None:
        clauses.append("as_of > ?")
        params.append(after + "T99")  # past end-of-day
    if min_value is not None:
        clauses.append("portfolio_value < ?")
        params.append(float(min_value))
    if max_value is not None:
        clauses.append("portfolio_value > ?")
        params.append(float(max_value))

    if not clauses:
        raise HTTPException(400, "At least one filter required: date | before | after | min_value | max_value")

    where = " AND ".join(clauses)
    conn  = get_db()
    # Preview first so the response shows what was removed
    preview = conn.execute(
        f"SELECT as_of, portfolio_value FROM snapshots WHERE {where} ORDER BY as_of",
        params,
    ).fetchall()
    result = conn.execute(f"DELETE FROM snapshots WHERE {where}", params)
    deleted = result.rowcount
    conn.commit()
    conn.close()
    log.info(
        f"Deleted {deleted} snapshot(s) — filters: "
        f"date={date} before={before} after={after} min_value={min_value} max_value={max_value}"
    )
    return {
        "deleted":      deleted,
        "filters":      {"date": date, "before": before, "after": after,
                         "min_value": min_value, "max_value": max_value},
        "removed_rows": [{"as_of": r["as_of"], "portfolio_value": r["portfolio_value"]} for r in preview],
    }


@app.post("/api/collect")
def trigger_collect():
    """Trigger a full snapshot. Called daily by HA automation at market close."""
    return compute_snapshot()


@app.post("/api/approve/{as_of}")
def approve_rebalance(as_of: str, execute: bool = False):
    """Approve the rebalance plan. Pass ?execute=true to submit orders to T212.

    `as_of` accepts the special value "latest" to always target the most recent
    snapshot in the DB.  Use that from automations / dashboard buttons so a
    cached HA REST sensor `as_of` attribute can't cause the wrong (older)
    snapshot to be acted upon.
    """
    conn = get_db()
    if as_of == "latest":
        row = conn.execute(
            "SELECT * FROM snapshots ORDER BY as_of DESC LIMIT 1"
        ).fetchone()
        if row:
            as_of = row["as_of"]
            log.info(f"approve_rebalance: 'latest' resolved to snapshot {as_of}")
    else:
        row = conn.execute("SELECT * FROM snapshots WHERE as_of=?", (as_of,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"Snapshot {as_of} not found")

    approved_at = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE snapshots SET approved=1, approved_at=? WHERE as_of=?", (approved_at, as_of))
    conn.commit()

    execution_results = []
    if execute:
        cfg     = load_config()

        # ── Pie detection short-circuit ──────────────────────────────────────
        # If the entire portfolio is inside a T212 auto-invest pie, direct
        # market orders will be rejected (T212 keeps pie holdings non-tradeable
        # at the instrument level).  Refuse to attempt them and tell the user
        # to use POST /api/push-to-pie instead, which updates the pie's
        # instrumentShares to match the snapshot's targets.
        if not cfg.get("force_direct_orders_when_pie", False):
            active_pie = _detect_active_pie(cfg)
            if active_pie:
                conn.close()
                msg = (
                    f"Pie detected (id={active_pie['id']}, value=£{active_pie.get('result', {}).get('priceAvgValue', 0):.2f}). "
                    "Direct orders rejected because all positions are inside a pie. "
                    "Use POST /api/push-to-pie to update pie instrumentShares to match these targets, "
                    "or set force_direct_orders_when_pie: true if you really want to attempt direct orders."
                )
                log.warning(f"approve(execute=true) refused: {msg}")
                return {
                    "approved":         True,
                    "approved_at":      approved_at,
                    "executed":         False,
                    "execution_mode":   "pie-blocked",
                    "pie_id":           active_pie["id"],
                    "execution_results": [],
                    "note":             msg,
                }

        actions = json.loads(row["suggested_actions"] or "[]")

        # ── Two-phase execution ──────────────────────────────────────────────
        # 1. SELLs first — track cash actually freed (not the requested amount,
        #    only what T212 confirms succeeded).
        # 2. Wait briefly for T212 to settle.
        # 3. BUYs afterwards — skip any whose required cash exceeds the running
        #    cash balance, instead of letting them fail with insufficient funds.
        sells = [a for a in actions if a["action"] == "SELL"]
        buys  = [a for a in actions if a["action"] == "BUY"]

        ok_count = 0
        fail_count = 0
        skipped_count = 0
        cash_freed = 0.0

        def _record(action, result):
            nonlocal ok_count, fail_count, skipped_count
            execution_results.append({"action": action, "result": result})
            if "skipped" in result:
                skipped_count += 1
            elif "error" in result:
                fail_count += 1
            else:
                ok_count += 1
            log.info(f"Order: {action['action']} {action['delta_units']} {action['t212_ticker']} → {result}")

        for action in sells:
            try:
                result = place_market_order(
                    cfg,
                    action["t212_ticker"],
                    action["delta_units"],
                    precision=int(action.get("quantity_precision", 2)),
                )
            except Exception as exc:
                result = {"error": "exception", "detail": str(exc)}
            # Track cash freed only on confirmed-successful sells
            if "error" not in result and "skipped" not in result:
                cash_freed += abs(float(action.get("delta_value", 0)))
            elif "selling-equity-not-owned" in str(result.get("detail", "")):
                # T212 persistently rejects orders for this position even after
                # trying alternative ticker forms.  Auto-flag it so future trade
                # plans exclude it; clears either when manually re-enabled or
                # if the position is sold/closed elsewhere.
                _mark_untradeable(action["t212_ticker"], "T212: selling-equity-not-owned")
            _record(action, result)

        # Wait briefly so cash actually appears in T212's free balance before
        # we try to spend it on the buy leg.
        if sells and buys:
            settle_sec = float(cfg.get("rebalance_settle_seconds", 5))
            log.info(f"Sells phase complete: cash_freed=£{cash_freed:.2f}.  "
                     f"Settling for {settle_sec}s before buy phase…")
            time.sleep(settle_sec)

        # Cash budget for buys = freed sells cash, plus a small slack for any
        # pre-existing cash position (we don't have a precise live cash figure
        # mid-rebalance, so use the snapshot's stored cash).
        snapshot_cash = float(_row_to_dict(row).get("cash") or 0.0)
        running_budget = cash_freed + snapshot_cash

        for action in sorted(buys, key=lambda a: -abs(a.get("delta_value", 0))):
            need = abs(float(action.get("delta_value", 0)))
            if need > running_budget + 0.50:   # 50p slack for FX rounding
                skip = {
                    "skipped": "insufficient-cash-budget",
                    "needed":  round(need, 2),
                    "available": round(running_budget, 2),
                }
                _record(action, skip)
                continue
            try:
                result = place_market_order(
                    cfg,
                    action["t212_ticker"],
                    action["delta_units"],
                    precision=int(action.get("quantity_precision", 2)),
                )
            except Exception as exc:
                result = {"error": "exception", "detail": str(exc)}
            if "error" not in result and "skipped" not in result:
                running_budget -= need
            _record(action, result)

        log.info(
            f"Batch complete: {ok_count} ok, {fail_count} failed, {skipped_count} skipped "
            f"of {len(actions)} total  (cash_freed=£{cash_freed:.2f})"
        )
        conn.execute("UPDATE snapshots SET executed=1, executed_at=? WHERE as_of=?",
                     (datetime.now(timezone.utc).isoformat(), as_of))
        conn.commit()

    conn.close()
    return {"approved": True, "approved_at": approved_at,
            "executed": execute, "execution_results": execution_results}


@app.get("/api/benchmarks")
def benchmark_history(days: int = 90):
    conn = get_db()
    rows = conn.execute(
        "SELECT as_of, benchmarks_json FROM snapshots ORDER BY as_of DESC LIMIT ?", (days,)
    ).fetchall()
    conn.close()
    return [{"as_of": r["as_of"], "benchmarks": json.loads(r["benchmarks_json"] or "{}")} for r in rows]


@app.post("/api/reset-cooldown")
def reset_cooldown():
    """Clear the executed flag on all snapshots to reset the rebalance cooldown."""
    conn = get_db()
    n    = conn.execute("UPDATE snapshots SET executed=0, executed_at=NULL WHERE executed=1").rowcount
    conn.commit()
    conn.close()
    log.info(f"Cooldown reset — cleared executed flag on {n} snapshot(s)")
    return {"reset": True, "snapshots_cleared": n}


@app.post("/api/set-phase")
def set_phase(body: dict = Body(default={})):
    """Apply a named portfolio phase preset (guard-rails only).

    Writes portfolio_phase to options.json and resets the rebalance cooldown.
    Does NOT change use_group_weights — group weight derivation is an
    independent decision controlled separately in the add-on configuration.

    Body: {"phase": "Momentum-Max"}
    """
    phase = body.get("phase", "").strip()
    if not phase:
        raise HTTPException(400, 'Request body must contain {"phase": "<name>"}')
    if phase not in PHASE_SETTINGS:
        raise HTTPException(400, f"Unknown phase '{phase}'. Valid: {list(PHASE_SETTINGS.keys())}")

    opts = _read_options()
    opts["portfolio_phase"] = phase
    try:
        _write_options(opts)
    except Exception as exc:
        raise HTTPException(500, f"Failed to write options.json: {exc}")

    # Reset cooldown so a rebalance can fire immediately after a phase change
    conn = get_db()
    n    = conn.execute("UPDATE snapshots SET executed=0, executed_at=NULL WHERE executed=1").rowcount
    conn.commit()
    conn.close()

    preset = PHASE_SETTINGS[phase]
    ugw    = bool(opts.get("use_group_weights", False))
    log.info(f"Phase set to '{phase}': CVaR={preset['max_cvar_pct']}%  "
             f"cost={preset['cost_rate_pct']}%  cooldown={preset['min_days_between_rebalance']}d  "
             f"vix_high={preset['vix_high_threshold']}  use_group_weights={ugw}  cooldown_reset={n} snapshots")
    return {"phase": phase, "settings": preset, "use_group_weights": ugw, "cooldown_reset": n}


@app.post("/api/set-risk-score")
def set_risk_score(body: dict = Body(default={})):
    """Update the user's risk score (0–100) without restarting the add-on.
    Used by the dashboard slider for live tuning.
    Body: {"risk_score": 75}
    """
    try:
        score = int(body.get("risk_score"))
    except (TypeError, ValueError):
        raise HTTPException(400, 'Body must be {"risk_score": <int 0-100>}')
    if not 0 <= score <= 100:
        raise HTTPException(400, "risk_score must be between 0 and 100")

    opts = _read_options()
    opts["risk_score"] = score
    try:
        _write_options(opts)
    except Exception as exc:
        raise HTTPException(500, f"Failed to write options.json: {exc}")
    log.info(f"Risk score set to {score} (next snapshot will use this)")
    return {"risk_score": score, "note": "Run a snapshot to see updated targets."}


@app.post("/api/notch-up")
def notch_up_cooldown():
    """Set a one-shot flag that bypasses the rebalance cooldown for the NEXT snapshot.
    Use this when you want to force a rebalance during a strong market move
    (bull run-up or bear sell-off) without waiting for the 21-day cooldown to expire.
    Flag is consumed (cleared) the moment the next snapshot uses it.
    """
    conn = get_db()
    _set_runtime_flag(conn, "notch_up_pending", "1")
    conn.close()
    log.info("Manual notch-up requested — next snapshot will bypass cooldown")
    return {
        "notch_up_pending": True,
        "note": "Next snapshot will bypass cooldown. Run /api/collect to consume.",
    }


@app.post("/api/cancel-notch-up")
def cancel_notch_up():
    """Clear a previously-set notch-up flag without consuming it."""
    conn = get_db()
    _clear_runtime_flag(conn, "notch_up_pending")
    conn.close()
    return {"notch_up_pending": False}


@app.get("/api/risk-state")
def risk_state():
    """Inspect the current dynamic-mode runtime state at a glance."""
    cfg = load_config()
    conn = get_db()
    notch = _get_runtime_flag(conn, "notch_up_pending") == "1"
    last  = conn.execute(
        "SELECT as_of, metadata_json FROM snapshots ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    conn.close()
    last_meta = {}
    if last and last["metadata_json"]:
        try:
            last_meta = json.loads(last["metadata_json"])
        except Exception:
            pass
    return {
        "weight_mode":                          cfg["weight_mode"],
        "risk_score":                           cfg["risk_score"],
        "auto_adjust_enabled":                  cfg["auto_adjust_enabled"],
        "auto_adjust_aggressiveness":           cfg["auto_adjust_aggressiveness"],
        "auto_adjust_direction":                cfg["auto_adjust_direction"],
        "cooldown_override_enabled":            cfg["cooldown_override_enabled"],
        "cooldown_override_vix_threshold":      cfg["cooldown_override_vix_threshold"],
        "cooldown_override_drawdown_threshold": cfg["cooldown_override_drawdown_threshold"],
        "notch_up_pending":                     notch,
        "last_snapshot": {
            "as_of":                 last["as_of"] if last else None,
            "effective_risk":        last_meta.get("effective_risk"),
            "effective_risk_reason": last_meta.get("effective_risk_reason"),
            "drawdown_pct":          last_meta.get("drawdown_pct"),
        },
    }


@app.get("/api/groups")
def get_groups():
    """List all instrument group assignments, sorted by group then yahoo_symbol."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM instrument_groups ORDER BY group_label='unassigned' DESC, yahoo_symbol ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/groups/{ticker}")
def set_group(ticker: str, body: dict = Body(default={})):
    """Set the group label for an instrument.
    Body: {"group": "momentum_core"}
    """
    from urllib.parse import unquote
    ticker = unquote(ticker)
    group  = body.get("group", "").strip()
    if group not in VALID_GROUPS:
        raise HTTPException(400, f"Invalid group '{group}'. Valid: {VALID_GROUPS}")

    conn = get_db()
    row  = conn.execute("SELECT 1 FROM instrument_groups WHERE t212_ticker=?", (ticker,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"Instrument '{ticker}' not found in instrument_groups")

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE instrument_groups SET group_label=?, updated_at=? WHERE t212_ticker=?",
        (group, now, ticker),
    )
    conn.commit()
    updated = dict(conn.execute(
        "SELECT * FROM instrument_groups WHERE t212_ticker=?", (ticker,)
    ).fetchone())
    conn.close()
    log.info(f"Group set: {ticker} → {group}")
    return updated


def _detect_active_pie(cfg: dict) -> Optional[dict]:
    """Return the first non-empty pie on the account, or None.
    Used by snapshot metadata to flag pie-execution mode.
    """
    if not cfg.get("t212_token"):
        return None
    try:
        r = requests.get(
            f"{cfg['t212_base']}/api/v0/equity/pies",
            headers=_t212_headers(cfg), timeout=15,
        )
        if not r.ok:
            return None
        pies = r.json()
        return pies[0] if pies else None
    except Exception:
        return None


def _fetch_pie_detail(cfg: dict, pie_id: int) -> dict:
    """Get full pie state including instrumentShares, settings, instruments."""
    r = requests.get(
        f"{cfg['t212_base']}/api/v0/equity/pies/{pie_id}",
        headers=_t212_headers(cfg), timeout=20,
    )
    r.raise_for_status()
    return r.json()


def _push_target_shares_to_pie(cfg: dict, pie_id: int, target_shares: dict[str, float]) -> dict:
    """Update a T212 pie's `instrumentShares` to match `target_shares`.

    `target_shares` keys are T212 tickers (matching the pie's existing keys);
    values are decimal fractions that MUST sum to 1.0 (T212 rejects otherwise).

    Pie metadata (name, icon, goal, dividendCashAction, endDate) is preserved
    from the current pie state so the user's settings survive untouched.
    """
    detail   = _fetch_pie_detail(cfg, pie_id)
    settings = detail.get("settings", {})

    body = {
        "name":               settings.get("name"),
        "icon":               settings.get("icon"),
        "goal":               settings.get("goal", 0),
        "dividendCashAction": settings.get("dividendCashAction", "REINVEST"),
        "endDate":            settings.get("endDate"),
        "instrumentShares":   target_shares,
    }
    log.info(f"Updating pie {pie_id} with {len(target_shares)} instrument shares  "
             f"(sum={sum(target_shares.values()):.4f})")
    r = requests.post(
        f"{cfg['t212_base']}/api/v0/equity/pies/{pie_id}",
        headers=_t212_headers(cfg), json=body, timeout=30,
    )
    if not r.ok:
        log.warning(f"Pie update rejected {r.status_code}: {r.text[:500]}")
        return {"error": f"{r.status_code}", "detail": r.text[:500], "request_body": body}
    return {"ok": True, "response": r.json(), "shares_pushed": target_shares}


@app.get("/api/t212/positions")
def t212_raw_positions():
    """Pass-through of T212's raw `/api/v0/equity/portfolio` response.
    Use to compare against what's stored in `instrument_groups` if the
    rebalancer is suggesting trades T212 can't actually execute.
    """
    cfg = load_config()
    if not cfg["t212_token"]:
        raise HTTPException(400, "No t212_token configured")
    try:
        r = requests.get(
            f"{cfg['t212_base']}/api/v0/equity/portfolio",
            headers=_t212_headers(cfg), timeout=20,
        )
        r.raise_for_status()
    except Exception as exc:
        raise HTTPException(502, f"T212 portfolio fetch failed: {exc}")
    return {
        "count":     len(r.json()),
        "positions": r.json(),
    }


@app.get("/api/t212/pies")
def t212_pies():
    """List all auto-invest pies on the T212 account.

    If a position appears in `/api/t212/positions` but the orders endpoint
    rejects it with `selling-equity-not-owned`, the position is likely held
    inside a pie.  T212 doesn't allow direct orders on pie-held instruments
    — you need to manage them through the pie itself (reduce the pie's value,
    duplicate / withdraw it, or sell via the T212 mobile app).
    """
    cfg = load_config()
    if not cfg["t212_token"]:
        raise HTTPException(400, "No t212_token configured")
    try:
        r = requests.get(
            f"{cfg['t212_base']}/api/v0/equity/pies",
            headers=_t212_headers(cfg), timeout=20,
        )
        r.raise_for_status()
    except Exception as exc:
        raise HTTPException(502, f"T212 pies fetch failed: {exc}")
    pies = r.json()
    return {"count": len(pies), "pies": pies}


@app.post("/api/push-to-pie")
def push_to_pie(body: dict = Body(default={})):
    """Update the active T212 pie's `instrumentShares` to match the latest
    snapshot's target weights.

    Use this when ALL your positions are inside an auto-invest pie (which
    blocks direct orders).  T212 will progressively rebalance the pie toward
    the new shares on its own auto-invest schedule.

    Optional body params:
      "pie_id":   int    — override pie auto-detection (default: first active pie)
      "preview":  bool   — return the computed shares without submitting (default false)

    Returns either {"preview": {...}} or T212's pie-update response.
    """
    cfg = load_config()

    # Resolve pie id
    pie_id = body.get("pie_id")
    if pie_id is None:
        pie = _detect_active_pie(cfg)
        if not pie:
            raise HTTPException(404, "No active pie detected on this account")
        pie_id = int(pie["id"])

    # Read latest snapshot's target weights and the t212_ticker mapping
    conn = get_db()
    row  = conn.execute(
        "SELECT positions_json FROM snapshots ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(503, "No snapshot exists yet — call POST /api/collect first")
    positions = json.loads(row["positions_json"] or "[]")
    if not positions:
        raise HTTPException(503, "Latest snapshot has no positions")

    # Build {pie_ticker: fractional_share} from positions.
    # Pie keys MUST match what T212 currently has in instrumentShares.  We use
    # raw_t212_ticker (what the portfolio API returned) which is what the pie
    # also uses internally.
    raw_shares = {}
    for p in positions:
        if not p.get("tradeable", True):
            continue
        target_int = int(p.get("target_wt", 0))
        if target_int <= 0:
            continue
        ticker = p.get("raw_t212_ticker") or p.get("t212_ticker")
        raw_shares[ticker] = raw_shares.get(ticker, 0) + target_int

    if not raw_shares:
        raise HTTPException(400, "Latest snapshot has no positive targets — nothing to push")

    # Convert integer percent → fractional share, normalise to sum exactly 1.0
    total = sum(raw_shares.values())
    shares = {t: round(v / total, 4) for t, v in raw_shares.items()}
    # Adjust the largest by the rounding residual so the sum is exactly 1.0
    residual = round(1.0 - sum(shares.values()), 4)
    if residual != 0:
        biggest = max(shares, key=shares.get)
        shares[biggest] = round(shares[biggest] + residual, 4)

    if body.get("preview"):
        return {
            "preview":  True,
            "pie_id":   pie_id,
            "shares":   shares,
            "share_sum": round(sum(shares.values()), 4),
            "instruments": len(shares),
        }

    try:
        result = _push_target_shares_to_pie(cfg, pie_id, shares)
    except Exception as exc:
        raise HTTPException(502, f"Pie update failed: {exc}")
    return {"pie_id": pie_id, **result}


@app.get("/api/t212/pie/{pie_id}")
def t212_pie_detail(pie_id: int):
    """Pass-through of T212's pie-detail endpoint for inspection."""
    cfg = load_config()
    if not cfg["t212_token"]:
        raise HTTPException(400, "No t212_token configured")
    try:
        return _fetch_pie_detail(cfg, pie_id)
    except Exception as exc:
        raise HTTPException(502, f"Pie fetch failed: {exc}")


@app.post("/api/test-order")
def test_order(body: dict = Body(default={})):
    """Manual single-order endpoint for diagnostics.  Bypasses the rebalance
    pipeline entirely — useful for verifying T212 connectivity and isolating
    "can this specific instrument be traded at all?" questions.

    Body:
      {
        "ticker":    "IGLSl_EQ",          // T212 ticker (compact or canonical)
        "value_gbp": 50.0,                // £ value of trade (price × qty)
        "action":    "SELL"               // "BUY" or "SELL"
      }

    The endpoint:
      1. Looks up current price from the latest snapshot's positions.
      2. Computes quantity = value_gbp / price, signed by action.
      3. Rounds to the catalog's quantity_precision.
      4. Submits via place_market_order (which does its own alt-ticker retry).
      5. Returns full T212 response and the actual quantity submitted.

    DOES NOT update any DB state, DOES NOT consume the cooldown override flag,
    DOES NOT mark instruments untradeable.  Pure diagnostic.
    """
    ticker    = str(body.get("ticker", "")).strip()
    value_gbp = float(body.get("value_gbp", 0))
    action    = str(body.get("action", "")).strip().upper()
    if not ticker or value_gbp <= 0 or action not in ("BUY", "SELL"):
        raise HTTPException(400, 'Body must be {"ticker":"...", "value_gbp": <num>, "action":"BUY"|"SELL"}')

    # Find current price from the latest snapshot's positions
    conn = get_db()
    row  = conn.execute(
        "SELECT positions_json FROM snapshots ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(503, "No snapshot available — run /api/collect first to build position price data")
    positions = json.loads(row["positions_json"] or "[]")
    pos = next(
        (p for p in positions
         if p["t212_ticker"] == ticker
         or p.get("raw_t212_ticker") == ticker
         or p["symbol"] == ticker),
        None,
    )
    if not pos:
        raise HTTPException(
            404,
            f"Ticker '{ticker}' not in latest snapshot positions. "
            f"Use compact ticker (e.g. IGLSl_EQ) or yahoo symbol (e.g. IGLS.L)."
        )
    price = float(pos["current_price"])
    if price <= 0:
        raise HTTPException(503, f"Latest snapshot has price=0 for {ticker} — try running another snapshot")

    raw_qty = value_gbp / price
    if action == "SELL":
        raw_qty = -raw_qty
    precision = int(pos.get("quantity_precision", 2))

    cfg = load_config()
    log.info(
        f"TEST ORDER: {action} £{value_gbp} of {ticker}  "
        f"price=£{price:.4f}  raw_qty={raw_qty:.6f}  precision={precision}"
    )
    result = place_market_order(cfg, pos.get("raw_t212_ticker", ticker), raw_qty, precision)
    return {
        "request": {
            "ticker":          ticker,
            "value_gbp":       value_gbp,
            "action":          action,
            "current_price":   price,
            "raw_quantity":    round(raw_qty, 6),
            "precision":       precision,
            "submitted_ticker": pos.get("raw_t212_ticker", ticker),
        },
        "result": result,
    }


@app.post("/api/groups/{ticker}/tradeable")
def set_tradeable(ticker: str, body: dict = Body(default={})):
    """Manually mark an instrument as tradeable / untradeable.
    Body: {"tradeable": true}  or  {"tradeable": false}

    The collector auto-marks instruments untradeable after persistent
    "selling-equity-not-owned" rejections.  Use this endpoint to:
      • Re-enable an instrument once you've manually closed/replaced the
        problem position in T212, or
      • Pre-emptively exclude one you don't want the rebalancer to touch.
    """
    from urllib.parse import unquote
    ticker = unquote(ticker)
    raw = body.get("tradeable")
    if raw is None or not isinstance(raw, bool):
        raise HTTPException(400, 'Body must be {"tradeable": true|false}')

    conn = get_db()
    row  = conn.execute("SELECT 1 FROM instrument_groups WHERE t212_ticker=?", (ticker,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"Instrument '{ticker}' not found")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE instrument_groups SET tradeable=?, updated_at=? WHERE t212_ticker=?",
        (1 if raw else 0, now, ticker),
    )
    conn.commit()
    updated = dict(conn.execute(
        "SELECT * FROM instrument_groups WHERE t212_ticker=?", (ticker,)
    ).fetchone())
    conn.close()
    log.info(f"Tradeable set: {ticker} → {raw}")
    return updated


@app.get("/api/catalog/status")
def catalog_status():
    """Catalog cache info."""
    conn = get_db()
    count      = conn.execute("SELECT COUNT(*) FROM instrument_catalog").fetchone()[0]
    oldest     = conn.execute("SELECT MIN(fetched_at) FROM instrument_catalog").fetchone()[0]
    conn.close()
    cfg        = load_config()
    ttl        = cfg["catalog_cache_ttl_sec"]
    age_sec    = None
    next_in    = None
    if oldest:
        try:
            age_sec = int((
                datetime.now(timezone.utc)
                - datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            ).total_seconds())
            next_in = max(0, ttl - age_sec)
        except Exception:
            pass
    return {
        "instrument_count": count,
        "fetched_at":       oldest,
        "age_sec":          age_sec,
        "ttl_sec":          ttl,
        "next_fetch_in_sec": next_in,
    }


@app.post("/api/catalog/refresh")
def catalog_refresh():
    """Force an immediate catalog re-fetch, bypassing the TTL cache.
    Use sparingly — T212 rate-limits this endpoint to 1 request per 50 seconds.
    """
    cfg = load_config()
    catalog = fetch_instrument_catalog(cfg, force=True)
    return {
        "refreshed":        True,
        "instrument_count": len(catalog),
        "as_of":            datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/sync-t212-weights")
def sync_t212_weights():
    """Reset initial_weight_pct for every instrument to its CURRENT actual
    weight, sourced from the latest snapshot's GBP-converted `actual_wt`.

    Using snapshot data (rather than raw T212 prices) ensures correctness
    for mixed-currency portfolios: LSE ETFs price in GBX (pence) and would
    otherwise be 100× overweighted vs GBP-priced holdings.

    A snapshot must exist first — call POST /api/collect if you have none.

    Only affects use_group_weights=False mode.  When use_group_weights=True,
    targets are derived from phase group allocations and stored weights are
    not used.
    """
    cfg  = load_config()
    conn = get_db()
    row  = conn.execute(
        "SELECT as_of, positions_json FROM snapshots ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(503, "No snapshot exists yet — call POST /api/collect first")

    positions = json.loads(row["positions_json"] or "[]")
    if not positions:
        conn.close()
        raise HTTPException(503, "Latest snapshot has no positions")

    now    = datetime.now(timezone.utc).isoformat()
    synced = 0
    skipped = []
    for p in positions:
        canonical = p.get("t212_ticker", "")
        actual_wt = float(p.get("actual_wt") or 0)
        if not canonical:
            skipped.append(p.get("yahoo_symbol", "?"))
            continue
        # Round to 2dp for stable storage; matches _seed_new_instruments
        wt = round(actual_wt, 2)
        cur = conn.execute(
            "UPDATE instrument_groups SET initial_weight_pct=?, updated_at=? WHERE t212_ticker=?",
            (wt, now, canonical),
        )
        if cur.rowcount > 0:
            synced += 1
        else:
            skipped.append(canonical)
    conn.commit()
    conn.close()

    log.info(
        f"sync-t212-weights: refreshed initial_weight_pct on {synced} instrument(s) "
        f"using snapshot {row['as_of']}"
        + (f" — skipped: {skipped}" if skipped else "")
    )
    return {
        "synced":             synced,
        "positions":          len(positions),
        "skipped":            skipped,
        "snapshot_as_of":     row["as_of"],
        "use_group_weights":  cfg["use_group_weights"],
        "note": (
            "Run a snapshot to see updated targets."
            if not cfg["use_group_weights"]
            else "use_group_weights=True — stored weights are not used; turn it off to apply."
        ),
        "as_of": now,
    }


@app.get("/api/last-good-targets")
def get_last_good_targets():
    """Inspect the frozen last-known-good target weights used as recovery
    baseline by the snapshot validator.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT t212_ticker, symbol, target_wt, saved_at FROM last_good_targets ORDER BY target_wt DESC"
    ).fetchall()
    conn.close()
    return {
        "count": len(rows),
        "rows":  [dict(r) for r in rows],
    }


@app.get("/groups", response_class=HTMLResponse)
def groups_page():
    """HTML group-management UI. Linked from the HA sidebar ingress panel."""
    return HTMLResponse(content=_GROUPS_HTML)


# ── DB row helper ─────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Always advertise the running collector version so HA sensors / dashboards
    # can confirm the add-on actually upgraded after a Supervisor update.
    d["collector_version"] = "2.7.2"
    for f in ["positions_json", "benchmarks_json", "drift_json", "momentum_json", "suggested_actions"]:
        key = f.replace("_json", "")
        d[key] = json.loads(d.pop(f) or ("[]" if f == "suggested_actions" else "{}"))
    # Expand v2.3+ metadata bag (weight_mode, risk_score, effective_risk, etc.)
    # into top-level keys so HA sensors and the dashboard can read them.
    meta_raw = d.pop("metadata_json", None)
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
            for k, v in meta.items():
                d.setdefault(k, v)
        except Exception:
            pass
    if "positions" in d and isinstance(d["positions"], list):
        gs: dict = {}
        for p in d["positions"]:
            g = p.get("group", "global_beta")
            if g not in gs:
                gs[g] = {"label": GROUP_LABELS.get(g, g), "actual_wt": 0.0, "target_wt": 0.0}
            gs[g]["actual_wt"] = round(gs[g]["actual_wt"] + p.get("actual_wt", 0.0), 2)
            gs[g]["target_wt"] = round(gs[g]["target_wt"] + p.get("target_wt", 0.0), 2)
        d["group_summary"] = gs
    # Count unassigned in live snapshot for HA sensor attribute
    if "positions" in d and isinstance(d["positions"], list):
        d["unassigned_count"] = sum(
            1 for p in d["positions"] if p.get("group") == "unassigned"
        )
    return d


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    _migrate_groups_from_options()
    cfg = load_config()
    # HA Supervisor sets INGRESS_PATH env var when ingress is enabled.
    # Passing it as root_path lets FastAPI generate correct absolute URLs
    # and ensures the /groups page works behind the ingress proxy.
    ingress_path = os.getenv("INGRESS_PATH", "")
    log.info(
    f"Portfolio Collector v2.7.2 — phase={cfg['portfolio_phase']} — "
        f"weight_mode={cfg['weight_mode']} — "
        f"DB: {DB_PATH} — T212: {cfg['t212_base']} — ingress={ingress_path or 'none'}"
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT, root_path=ingress_path)
