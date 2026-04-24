# Changelog

All notable changes to the Portfolio Collector add-on are documented here.

---

## [1.5.0] — 2026-04-24

### Added
- **`POST /api/sync-from-t212`** — syncs the holdings list directly from the
  live T212 portfolio API.
  - **Existing holdings**: `purchase_qty` and `purchase_price` updated from
    T212's `quantity` and `averagePrice`. `target_weight`, `group`, and
    `yahoo_symbol` are preserved.
  - **New holdings** (in T212 but absent from config): added automatically.
    `target_weight` is set to the holding's actual current weight in the T212
    portfolio so the config starts balanced. `group` defaults to `global_beta`.
  - **Removed holdings** (in config but zero/absent in T212, i.e. sold):
    dropped from the holdings list so they no longer affect rebalancing.
  - The updated holdings list is written back to `/data/options.json`. Open
    the add-on options page in HA to review and assign correct groups to any
    newly discovered holdings.
  - Add `?preview=true` for a dry-run that logs the diff without writing.
- **`POST /api/sync-from-t212?preview=true`** — dry-run variant; returns the
  same diff payload without touching `options.json`.
- **`_t212_ticker_to_yahoo()`** helper — derives a Yahoo Finance symbol from
  a T212 instrument ticker using a priority-ordered exchange suffix map
  (covers LSE, XETRA, Euronext, Nasdaq Nordic, NYSE, NASDAQ, and NYSE Arca).
- **Dashboard: Sync buttons** — two new buttons on the Rebalance tab:
  *Sync Holdings from T212* (writes) and *Preview T212 Sync* (dry-run).
  Both have confirmation dialogs with instructions on assigning groups.
- **`rest_command.sync_portfolio_from_t212`** and
  **`rest_command.preview_t212_sync`** wired in `packages/portfolio.yaml`.
- **Holdings view responsive layout** — Holdings tab changed from
  `type: panel` + fixed `type: grid` to `type: masonry` with `max_columns: 2`.
  Renders as two columns on desktop and a single column on mobile.

### Changed
- `_read_options()` now uses a module-level `OPTIONS_PATH` constant.
- `_write_options()` helper added for safe write-back to `options.json`.
- Version bumped to `1.5.0` throughout.

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
