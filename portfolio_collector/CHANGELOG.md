# Changelog

All notable changes to the Portfolio Collector add-on are documented here.

---

## [2.0.0] — 2026-05-01

### Changed — breaking
- **T212 is now the source of truth.** The `holdings:` array has been removed
  from `config.yaml`. Positions, quantities and prices are fetched live from
  T212 on every snapshot. No manual holdings list is needed.
- `POST /api/sync-from-t212` endpoint removed — auto-sync happens on every
  `POST /api/collect`.
- Snapshot now raises HTTP 503 (rather than using stale fallback data) when
  the T212 API is unreachable.

### Added
- **Instrument catalog** — `GET /api/v0/equity/metadata/instruments` is fetched
  from T212 and cached in SQLite (`instrument_catalog` table). Used to derive
  Yahoo Finance symbols from exchange codes and to detect GBX (pence) instruments.
- **Reliable pence handling** — uses `currencyCode == "GBX"` from the catalog
  rather than heuristic price comparisons. Affects both T212 and Yahoo prices.
- **ISA compact ticker resolution** — handles both `VWRLl_EQ` and `VWRL_EQ`
  formats returned by T212 ISA accounts, resolving them to canonical tickers.
- **Group labels in SQLite** — new `instrument_groups` table stores group
  assignments. New instruments default to `unassigned` and trigger an HA
  notification prompting assignment.
- **Group manager UI** — FastAPI serves a HTML page at `/groups` (linked from
  the HA sidebar ingress panel). Dropdowns auto-save on change.
- **Snapshot sanity check** — new `max_snapshot_change_pct` option (default 20).
  Snapshots whose portfolio value deviates by more than this percentage from the
  previous snapshot are rejected and not written to the database.
- **Phase change** — `POST /api/set-phase` now auto-enables `use_group_weights`
  and resets the rebalance cooldown.
- `GET /api/groups` — list all instrument group assignments.
- `POST /api/groups/{ticker}` — update group label for an instrument.
- `GET /api/catalog/status` — catalog cache info (age, TTL, instrument count).
- `POST /api/catalog/refresh` — force catalog re-fetch bypassing TTL.
- `catalog_cache_ttl_sec` config option (default 3600).
- `unassigned_count` in snapshot response and `sensor.portfolio_value` attributes.

### Migration from v1.6.x
On first boot, `instrument_groups` is automatically seeded from any `holdings`
array still in options.json (preserving group labels). Historical snapshots are
untouched. The `holdings:` key in options.json is ignored by v2.0.0 and can be
removed from the add-on Configuration UI.

---

## [1.6.2] — 2026-04-28

### Added
- **`GET /api/snapshots?summary=true`** — lightweight list returning only
  `as_of` + `portfolio_value` for each snapshot; no JSON parsing overhead.
  Use this to quickly spot corrupt/rogue values in history.
- **`DELETE /api/snapshots?date=YYYY-MM-DD`** — delete all snapshots for a
  given date. Useful for removing corrupt records caused by bad syncs or
  data errors without needing direct database access.

---

## [1.4.0] — 2026-04-22

### Added
- **ETF group definitions** — holdings are tagged with one of five groups:
  `momentum_core`, `global_beta`, `regional_satellite`, `defensive`,
  `optional_factor`. Group allocations and labels are defined as module-level
  constants (`GROUP_ALLOCATIONS`, `GROUP_LABELS`).
- **Group-based weight derivation** — new `use_group_weights` option (default
  `false`). When enabled, individual target weights are derived from group
  allocations (equal split within each group) instead of per-holding
  `target_weight` values.
- **`group_summary` in snapshot** — every snapshot now includes a per-group
  summary of actual vs target allocation. Derived fresh from positions in
  `_row_to_dict` (no DB schema change required).
- **WMA trend score** (`trend_score`) — linearly-weighted moving-average
  momentum signal (dimensionless, 126-bar lookback) added to every holding's
  momentum dict.
- **9-month momentum** (`momentum_9m`) added alongside 12m, 6m, 3m.
- **Blended momentum score** — `mom_scores` now uses 50% WMA trend signal +
  30% 6m momentum + 20% 12m momentum instead of a simple average.
- **CVaR constraint** (`max_cvar_pct`, default 5%) — if portfolio historical
  tail risk exceeds the limit, non-defensive holdings are scaled back and
  weight redistributed to defensive ETFs before trade sizing.
- **Transaction cost filter** (`cost_rate_pct`, default 0.1%) — primary trades
  are skipped when the expected drift-correction benefit ≤ the estimated
  round-trip cost.
- **`numpy` import** added; required by `_wma_trend_score` and
  `_portfolio_cvar`.
- **Dashboard: Group allocation card** — new markdown card in the Holdings view
  shows group-level target vs actual weights with colour-coded delta.
- **Dashboard: Holdings table grouped** — rows are now grouped by
  `group_order` and show the group label in the first column.
- **Dashboard: Momentum table** — new `9m` and `Signal` (WMA trend) columns.

### Changed
- `_compute_rebalance` signature extended with `momentum` and `hist`
  parameters (both backwards-compatible with defaults).
- `load_config` returns four new keys: `use_group_weights`, `max_cvar_pct`,
  `cost_rate_pct`, `group_allocations`, `symbol_groups`.
- `sensor.portfolio_snapshot` `json_attributes` now includes `group_summary`.
- `config.yaml` schema updated with `use_group_weights`, `max_cvar_pct`,
  `cost_rate_pct`, and optional `group` field on each holding.

---

## [1.3.0] — 2026-04-21

### Changed
- **Integer target weights** — after normalising to 100%, target weights are rounded
  to whole numbers using the largest-remainder (Hamilton) method so totals always sum
  to exactly 100%. Eliminates sub-1% positional noise.
- **Integer-boundary rebalance rule** — a holding is queued for trading only when
  its rounded actual weight differs from its integer target by ≥ 2 points (normal)
  or ≥ 3 points (VIX elevated). Single-tick rounding noise never triggers a trade.
- **Self-funding trade list** — after building the primary (drifted) trades, the net
  cash surplus or deficit is absorbed by a single balancing trade on the most
  over/under-weighted untouched holding, so total buys ≈ total sells and no cash
  is left stranded. Balancing trades are flagged `balancing_trade: true` in the
  trade plan.
- **Trade plan shows base and adjusted targets** — table now shows the integer base
  target and the momentum-adjusted trade target separately so the two are not confused.
- **Rebalance reason now lists affected holdings** — e.g.
  `"2 holding(s) crossed integer target: MVOL (1.6%→1%), XDEM (11.2%→10%)"`

### Fixed
- Drift bar chart "Configuration error" — added `ignore_history: true`,
  `extend_to: false`, and `apex_config.xaxis.type: category` so apexcharts-card
  treats the series as categorical rather than time-series data.
- Drift chart now shows diverging vertical bars centred on zero with dotted
  threshold annotation lines at ±15% and per-bar colour coding (green/blue/red/orange).
- Holdings table Target % column now displays as a whole number (no decimal).

---

## [1.1.0] — 2026-04-20

### Added
- **Configurable holdings** — up to 20 ticker/weight pairs editable in the HA
  add-on UI; no file editing required
- **Configurable guard-rails** — drift threshold, VIX thresholds, and
  rebalance cooldown now exposed as add-on options
- **Auto-normalisation** — target weights no longer need to sum to exactly 100%;
  the add-on normalises automatically
- **Live config reload** — weight changes take effect on the next snapshot
  without restarting the add-on
- `GET /api/health` now reports holdings count and add-on version

### Changed
- `host_network: true` added to config — fixes `localhost:8000` connectivity
  from HA core
- Yahoo Finance fetcher now uses `threads=False` (batch) with per-ticker
  fallback and exponential back-off to avoid rate-limiting
- TzCache directed to `/data/yf_cache` — eliminates noisy cache warnings
- `yfinance` version pinned to `>=0.2.54`
- Dockerfile simplified to `python:3.12-slim` — removes dependency on HA
  base images which caused build failures on some hardware

### Fixed
- Add-on build failure caused by `ARG BUILD_FROM` + unavailable HA base image
- `JSONDecodeError` from Yahoo Finance on batch download with parallel threads
- `TzCache` race-condition log spam

---

## [1.0.0] — 2026-04-07

### Initial release
- Daily T212 portfolio snapshot via demo API
- Benchmark tracking: MSCI World (URTH), FTSE 100, S&P 500, DOW, VIX
- 12-1m / 6-1m / 3m price momentum scoring per holding
- EMA 20/50 trend classification (bullish / neutral / bearish)
- Relative strength vs MSCI World proxy
- Drift detection with relative threshold
- VIX regime filter (elevated / high / extreme)
- 21-day rebalance cooldown
- Momentum-adjusted target weights (±10% / ±20% tilt)
- Manual approval gate before any order submission
- SQLite persistence at `/data/portfolio.db`
- FastAPI service on port 8000
- HA REST sensors, input_booleans, automations via package file
- 4-view Lovelace dashboard (Overview, Holdings, Rebalance, Benchmarks)
- 13-ETF default portfolio: VWRL, IWFM, VAGP, XDEM, SSAC, VUSA,
  XWEM, IMEU, IJPN, VFEM, IGLS, IWFQ, MVOL
