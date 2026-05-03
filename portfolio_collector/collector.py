#!/usr/bin/env python3
"""
Portfolio Collector — Home Assistant Add-on v2.2.0
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

app = FastAPI(title="Portfolio Collector", version="2.2.0")


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
            executed_at          TEXT
        );

        CREATE TABLE IF NOT EXISTS instrument_catalog (
            t212_ticker     TEXT PRIMARY KEY,
            isin            TEXT,
            name            TEXT,
            currency_code   TEXT NOT NULL DEFAULT 'GBP',
            exchange        TEXT,
            yahoo_symbol    TEXT,
            fetched_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS instrument_groups (
            t212_ticker        TEXT PRIMARY KEY,
            yahoo_symbol       TEXT NOT NULL DEFAULT '',
            display_name       TEXT NOT NULL DEFAULT '',
            group_label        TEXT NOT NULL DEFAULT 'unassigned',
            initial_weight_pct REAL NOT NULL DEFAULT 0,
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
    """)
    # Migration: add initial_weight_pct to existing DBs that predate 2.0.3
    try:
        conn.execute("ALTER TABLE instrument_groups ADD COLUMN initial_weight_pct REAL NOT NULL DEFAULT 0")
        conn.commit()
        log.info("DB migration: added initial_weight_pct column to instrument_groups")
    except Exception:
        pass  # Column already exists — normal on fresh install or after first migration
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

    Three modes selected via cfg["weight_mode"]:

      "stored"           — targets = stored initial_weight_pct, normalised to 100
                           (drift relative to your T212 actuals at sync time)
      "equal_in_group"   — phase group_alloc / instruments-in-group
                           (uniform within each group)
      "scaled_in_group"  — phase group_alloc, weighted by stored ratio within group
                           (preserves VWRL:SSAC kind of relationships across phase
                            changes — group total expands/shrinks but ratios stay)

    Backwards-compatible with the legacy boolean `use_group_weights` flag, which
    maps to "equal_in_group" when set.
    """
    n = len(holdings)
    if n == 0:
        return {}

    mode       = cfg.get("weight_mode", "stored")
    has_groups = any(h["group"] != "unassigned" for h in holdings)

    if mode == "scaled_in_group" and has_groups:
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
    if weight_mode_raw in ("stored", "equal_in_group", "scaled_in_group"):
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
        "use_group_weights":          weight_mode in ("equal_in_group", "scaled_in_group"),
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
        catalog[ticker] = {
            "t212_ticker":  ticker,
            "isin":         isin,
            "name":         name,
            "currency_code": currency,
            "exchange":     exchange,
            "yahoo_symbol": yahoo,
            "fetched_at":   now,
        }
        conn.execute(
            """INSERT OR REPLACE INTO instrument_catalog
               (t212_ticker, isin, name, currency_code, exchange, yahoo_symbol, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ticker, isin, name, currency, exchange, yahoo, now),
        )

    conn.commit()
    conn.close()
    log.info(f"Catalog cached: {len(catalog)} instruments")
    return catalog


def place_market_order(cfg: dict, t212_ticker: str, quantity: float) -> dict:
    """Place a market order. Positive = BUY, negative = SELL."""
    if not cfg["t212_token"]:
        return {"error": "No t212_token configured"}
    payload = {"ticker": t212_ticker, "quantity": round(quantity, 6)}
    log.info(f"T212 order: {payload}  base={cfg['t212_base']}")
    try:
        r = requests.post(
            f"{cfg['t212_base']}/api/v0/equity/orders/market",
            headers=_t212_headers(cfg), json=payload, timeout=20,
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

        avg_price_raw = float(pos.get("averagePrice") or 0)
        cur_price_raw = float(pos.get("currentPrice") or avg_price_raw)

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
        })

    if not holdings:
        raise HTTPException(503, "T212 returned no holdings — cannot build snapshot")

    log.info(f"Holdings: {len(holdings)}  unassigned: {sum(1 for h in holdings if h['group'] == 'unassigned')}")

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
            "symbol":        sym,
            "t212_ticker":   h["t212_ticker"],
            "display_name":  h["display_name"],
            "quantity":      round(qty, 6),
            "avg_price":     round(avg_price, 4),
            "current_price": round(current_price, 4),
            "market_value":  round(market_value, 2),
            "cost_basis":    round(cost_basis, 2),
            "pnl_pct":       round(pnl_pct, 2),
            "group":         h["group"],
            "group_order":   GROUP_ORDER.get(h["group"], 9),
            "target_wt":     target_wt,
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
        momentum[sym] = {
            "momentum_12m":   round(v, 2) if (v := _momentum(s, 252))    is not None else None,
            "momentum_9m":    round(v, 2) if (v := _momentum(s, 189))    is not None else None,
            "momentum_6m":    round(v, 2) if (v := _momentum(s, 126))    is not None else None,
            "momentum_3m":    round(v, 2) if (v := _momentum(s, 63, 0))  is not None else None,
            "trend":          _ema_trend(s),
            "trend_score":    _wma_trend_score(s, 126),
            "return_1m":      round(_period_return(s, 21) or 0.0, 2),
            "return_3m":      round(_period_return(s, 63) or 0.0, 2),
            "rs_vs_world_3m": rs,
            "group":          h["group"],
            "group_order":    GROUP_ORDER.get(h["group"], 9),
        }

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
             f"max_drift={max_drift_rel:.1f}%  rebalance={rebalance_needed}  vix={vix}  "
             f"unassigned={unassigned_count}")

    return {
        "as_of":                as_of,
        "collector_version":    "2.2.0",
        "weight_mode":          cfg.get("weight_mode", "stored"),
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

    conn = get_db()
    row  = conn.execute(
        "SELECT approved_at FROM snapshots WHERE executed=1 ORDER BY executed_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row and row["approved_at"]:
        try:
            days = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(row["approved_at"].replace("Z", "+00:00"))).days
            if days < cooldown_days:
                return False, f"Cooldown: {days}d since last rebalance (min {cooldown_days}d)", []
        except Exception:
            pass

    if vix > vix_extreme:
        return False, f"VIX={vix:.1f} — extreme volatility, rebalancing frozen", []

    def _int_gap(p):
        return abs(round(p["actual_wt"]) - int(p["target_wt"]))

    if vix > vix_high:
        min_gap = 2
        drifted = [p for p in positions if _int_gap(p) >= min_gap]
        if not drifted:
            return False, f"VIX={vix:.1f} elevated — no holding ≥2 pts off target, holding", []
    else:
        min_gap = 1
        drifted = [p for p in positions if _int_gap(p) >= min_gap]

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
        delta_units = delta_val / p["current_price"] if p["current_price"] else 0.0
        return {
            "symbol":             sym,
            "t212_ticker":        p["t212_ticker"],
            "action":             "BUY" if delta_val > 0 else "SELL",
            "current_wt":         p["actual_wt"],
            "target_wt":          round(adj_weights[sym], 2),
            "original_target_wt": int(cfg["target_weights"][sym]),
            "delta_value":        round(delta_val, 2),
            "delta_units":        round(delta_units, 6),
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
        "version":          "2.2.0",
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
def delete_snapshots(date: str):
    """Delete all snapshots for a given date. Example: DELETE /api/snapshots?date=2026-04-28"""
    conn    = get_db()
    result  = conn.execute("DELETE FROM snapshots WHERE as_of LIKE ?", (f"{date}%",))
    deleted = result.rowcount
    conn.commit()
    conn.close()
    log.info(f"Deleted {deleted} snapshot(s) for date {date}")
    return {"deleted": deleted, "date": date}


@app.post("/api/collect")
def trigger_collect():
    """Trigger a full snapshot. Called daily by HA automation at market close."""
    return compute_snapshot()


@app.post("/api/approve/{as_of}")
def approve_rebalance(as_of: str, execute: bool = False):
    """Approve the rebalance plan. Pass ?execute=true to submit orders to T212."""
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
    d["collector_version"] = "2.2.0"
    for f in ["positions_json", "benchmarks_json", "drift_json", "momentum_json", "suggested_actions"]:
        key = f.replace("_json", "")
        d[key] = json.loads(d.pop(f) or ("[]" if f == "suggested_actions" else "{}"))
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
        f"Portfolio Collector v2.2.0 — phase={cfg['portfolio_phase']} — "
        f"weight_mode={cfg['weight_mode']} — "
        f"DB: {DB_PATH} — T212: {cfg['t212_base']} — ingress={ingress_path or 'none'}"
    )
    uvicorn.run(app, host="0.0.0.0", port=PORT, root_path=ingress_path)
