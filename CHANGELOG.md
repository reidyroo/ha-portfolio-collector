# Changelog

All notable changes to the Portfolio Collector add-on are documented here.

---

## [2.5.0] — 2026-05-04

### Changed — group assignment now lives entirely in HA dashboard
- **Removed** the `panel_iframe.portfolio_groups` sidebar entry. Group
  assignment no longer requires the external `http://<HA-IP>:8000/groups`
  page, an HA ingress route, or any DNS / firewall accommodation. Everything
  stays inside HA's auth boundary.
- **New** in-dashboard assignment flow on the Groups view:
  - `input_select.portfolio_assign_instrument` — populated dynamically from
    each snapshot's positions list (format: `"VWRL.L (VWRLl_EQ)"`).
  - `input_select.portfolio_assign_group` — static list of the 5 groups +
    unassigned.
  - **APPLY** call-service row in the entities card runs
    `rest_command.assign_instrument_group`, which extracts the t212_ticker
    from the dropdown and POSTs to `/api/groups/{ticker}`.
- **New automation** `portfolio_refresh_instrument_dropdown` — refreshes the
  instrument dropdown whenever a new snapshot lands (also at HA startup).
- **New automation** `portfolio_assign_instrument_done` — persistent
  notification confirms each assignment with a "Run Snapshot Now to refresh"
  reminder.
- **Live assignment table** — markdown card on the Groups view shows current
  group / symbol / actual% / target% from the most recent snapshot, with
  unassigned rows highlighted 🟡.
- **`/groups` HTML endpoint kept** on the add-on for power-users / curl, but
  no longer the supported path.

### Why
Old approach required `http://<HA-IP>:8000` to be reachable from your
browser/phone. Behind Tailscale, split-DNS, or a strict firewall this
created friction or required exposing the add-on port externally. The new
approach uses only HA's already-authenticated dashboard + the loopback
`http://localhost:8000` path that HA Core uses for REST commands — never
exposed beyond HA itself.

---

## [2.4.1] — 2026-05-04

### Added
- **Bulk snapshot delete filters** — `DELETE /api/snapshots` now accepts any
  combination of `date`, `before`, `after`, `min_value`, `max_value`. Useful
  for cleaning up rogue spikes from early data without picking dates one by one.
  Example: `DELETE /api/snapshots?max_value=50000` removes any snapshot whose
  stored portfolio value exceeded £50k (a known data error pattern from
  pre-sanity-check eras). Response includes the list of rows removed.
- Backwards-compatible — old `?date=YYYY-MM-DD` calls work unchanged.

---

## [2.4.0] — 2026-05-04

### Added — cooldown override + live risk tuning + bidirectional auto-adjust

**Cooldown override** — bypass the 21-day rebalance cooldown during sharp
market moves:

- **Auto override** (when `cooldown_override_enabled: true`): VIX above
  `cooldown_override_vix_threshold` (default 30) or drawdown below
  `cooldown_override_drawdown_threshold` (default -15%) lets a defensive
  rebalance fire even within cooldown.
- **Manual notch-up** (one-shot): `POST /api/notch-up` sets a flag; the next
  snapshot bypasses cooldown and consumes the flag automatically.
- New endpoints: `POST /api/notch-up`, `POST /api/cancel-notch-up`.
- New `runtime_state` SQLite table for transient flags.
- Dashboard: **Notch Up — Bypass Cooldown** + **Cancel Notch-Up** buttons on
  the Rebalance tab.
- Snapshot metadata now records `cooldown_override_used` and
  `cooldown_override_reason`.
- HA automation `portfolio_cooldown_override_fired` sends a notification
  whenever an override actually consumes.

**Live risk-score tuning** — adjust dynamic-mode risk without opening the
add-on Configuration tab:

- New `input_number.portfolio_risk_score` (0–100, step 5).
- New `POST /api/set-risk-score` endpoint writes to options.json on demand.
- HA automation `portfolio_risk_score_change` watches the slider and pushes
  changes to the add-on automatically.
- Dashboard: risk slider with quick-set buttons (Pre-Retirement 15 /
  Balanced 45 / Chill 65 / Max 90) on the Rebalance tab.

**Bidirectional auto-adjust** — let strong rallies push effective risk UP:

- New `auto_adjust_direction` option (default `defensive_only`).
- Set to `bidirectional` to allow strong 21-day portfolio rallies above +5%
  to push effective risk up by up to +5 points (capped so it can't overwhelm
  the user's set risk_score).

**Inspection endpoint:**

- `GET /api/risk-state` — single one-stop view of weight_mode, risk_score,
  effective risk from last snapshot, override status, all auto-adjust config.

---

## [2.3.1] — 2026-05-03

### Fixed
- **Dynamic-mode fields persist across snapshots.** v2.3.0 returned the new
  fields (`weight_mode`, `risk_score`, `effective_risk`, `effective_risk_reason`,
  `drawdown_pct`, `dynamic_group_allocations`, `portfolio_phase`) only from the
  in-memory `/api/collect` response, not from the DB-reconstructed
  `/api/latest-snapshot`. Now stored in a new `metadata_json` column and
  expanded in `_row_to_dict`. Migration auto-adds the column on existing DBs.

---

## [2.3.0] — 2026-05-03

### Added — dynamic risk axis (foundation)
- **`weight_mode: dynamic`** — fourth weight derivation mode. Group allocations
  are interpolated linearly between phase-preset anchors along a 0–100 risk
  axis, driven by `risk_score`. Within-group ratios are always preserved
  (uses the `_scaled_within_group_weights` algorithm internally).
- **Phase risk anchors**: Pre-Retirement=15, Balanced Growth=45,
  Momentum-Chill=65, Momentum-Max=90. Intermediate `risk_score` values produce
  blended group allocations (e.g. risk=75 → 40% of the way from Chill to Max).
- **`risk_score` config option** (0–100, default 65 = Momentum-Chill anchor).
- **`auto_adjust_enabled`** + **`auto_adjust_aggressiveness`** (low/medium/high) —
  when enabled, VIX above the high threshold and portfolio drawdown beyond
  -10% pull the *effective* risk down by up to 5/10/20 points.
- **Snapshot exposes**: `risk_score`, `effective_risk`, `effective_risk_reason`,
  `drawdown_pct`, `dynamic_group_allocations`, `portfolio_phase`.
- **Dashboard banner** now shows risk score → effective risk and the reason
  string (only when `weight_mode` is `dynamic`), plus current drawdown%.
- Add-on startup banner logs the active `weight_mode`.

### Coming next (v2.4.0)
- Cooldown override during VIX spikes / large drawdowns
- "Notch up" one-shot button to bypass cooldown manually
- HA `input_number.portfolio_risk_score` slider for live tuning

---

## [2.2.0] — 2026-05-03

### Added
- **`weight_mode` configuration option** — three target-derivation strategies,
  replacing the boolean `use_group_weights`:
  - `stored` (default) — targets = stored T212 weights at sync time. Drift relative
    to your real portfolio. Phase changes don't move targets. Same as the old
    `use_group_weights: false`.
  - `equal_in_group` — targets = phase group allocation / instruments-in-group.
    Equal split within each group. Same as the old `use_group_weights: true`.
  - `scaled_in_group` — **NEW**. Phase-driven group totals, with within-group
    ratios preserved from stored weights. Switching from Momentum-Chill to
    Momentum-Max scales VWRL.L 18→26 and SSAC.L 10→14 (preserving 1.8:1 ratio
    while expanding Global Beta from 28% → 40%). Reversibly restores the
    original split when switching back.
- `weight_mode` exposed as a `sensor.portfolio_value` attribute and shown in
  the dashboard banner (Overview view top markdown card).
- Add-on startup banner logs the active weight mode.

### Backwards compatibility
- `use_group_weights` still respected for older configs:
  - `use_group_weights: true` + no `weight_mode` → maps to `equal_in_group`
  - `use_group_weights: false` + no `weight_mode` → maps to `stored`
- Existing configs work unchanged.

---

## [2.1.0] — 2026-05-02

### Promoted
- v2.1.0 marks the steady-state release after the 2.0.1 → 2.0.13 stabilisation series.
  README + CHANGELOG fully rewritten to document T212-as-source-of-truth, group manager,
  phase presets, two target-weight modes, validator + recovery safeguards, and the
  diagnostics curl cookbook.

### Same code as 2.0.13
- All v2.0.13 features (target validator, last-good-targets recovery, equal-weight
  fallback, `/api/last-good-targets`) carried forward unchanged.

---

## [2.0.13] — 2026-05-02

### Added
- **Snapshot target validator** — three-rule sanity check on every snapshot:
  rejects targets where any active holding (≥0.5% actual) has target=0; where
  target sum is outside 100% ± 2pp; or where any group with active holdings
  has zero total target. Prevents the partial-data leak that caused
  liquidate-everything trade plans on earlier versions.
- **`last_good_targets` recovery table** — every passing snapshot persists its
  target weights as the recovery baseline. On a future failed validation, the
  bad targets are silently replaced with the last-known-good ones in-memory,
  drift recomputed, snapshot saved with sane numbers. Logged loudly.
- **Equal-weight last-resort fallback** — if validation fails AND no recovery
  baseline exists, each holding gets `100/n%` rather than the broken numbers.
- `GET /api/last-good-targets` — inspect the validator recovery baseline.

## [2.0.12] — 2026-05-02

### Fixed
- **`POST /api/sync-t212-weights` now uses GBP-converted weights** from the
  latest snapshot's `actual_wt` instead of raw T212 native-currency prices.
  Mixed-currency portfolios (LSE GBX + GBP holdings) were previously skewed
  ~100× toward whichever currency dominated. Snapshot must now exist first.

## [2.0.11] — 2026-05-02

### Fixed
- **Bare `_EQ` T212 tickers** (e.g. `IITU_EQ`, `IWFM_EQ`) now resolve to `.L`
  Yahoo symbols. T212 ISA accounts return some instruments without an exchange
  code at all; previously these became bare `IITU` / `IWFM` and 404'd Yahoo.
  Both `_t212_ticker_to_yahoo` and `_validate_yahoo_symbol` apply the rule.

## [2.0.10] — 2026-05-02

### Added
- **`POST /api/sync-t212-weights`** — one-button reset of stored target weights
  to current T212 actual weights. Drift returns to ~0 with no rebalance fire.
- **`rest_command.sync_t212_weights`** + **Sync T212 Weights → Targets**
  dashboard button.

## [2.0.9] — 2026-05-02

### Added
- **`collector_version` field** in every snapshot response and as a
  `sensor.portfolio_value` attribute. Dashboard banner displays it so the user
  can verify the add-on actually upgraded after a Supervisor update.
- **Direct-URL Open Group Manager button** (`http://<host>:8000/groups`) —
  works without `panel_iframe` registration or ingress. Survives reinstalls
  and HA cache state.

## [2.0.8] — 2026-05-02

### Fixed
- **Phase preset and `use_group_weights` are now independent.** A named phase
  applies guard-rails only (CVaR, cost filter, VIX threshold, cooldown).
  Group-based weight derivation is a separate decision. Reverts the v2.0.7
  forced-True coupling.
- `POST /api/set-phase` no longer auto-enables `use_group_weights`.

## [2.0.7] — 2026-05-02 *(superseded by 2.0.8)*

### Fixed
- `_compute_target_weights`: fall back to equal weight when *any* instrument
  has `initial_weight_pct = 0`. Stops 0% targets leaking through when DB key
  migration is partial.

## [2.0.6] — 2026-05-02

### Fixed
- **`_validate_yahoo_symbol`** post-processor catches bare Yahoo symbols
  (e.g. `IITU` for an LSE-listed ETF whose catalog entry has a blank/unknown
  exchange) and adds the missing `.L` suffix using the canonical T212 ticker.
  Applied at both snapshot time and instrument seeding.
- `pct_change(fill_method=None)` silences pandas FutureWarning in momentum
  and CVaR calculations.

## [2.0.5] — 2026-05-02

### Changed
- Version bump to force Supervisor update detection after cache issues.

## [2.0.4] — 2026-05-02

### Fixed
- `_seed_new_instruments`: 3-case logic (canonical exists / compact key only /
  brand new) correctly migrates compact DB keys (`IITUl_EQ`) to canonical form
  (`IITU_EQ_XLON`), preserves group assignments, and removes stale duplicates.
- UPDATE path now sets `initial_weight_pct = approx_wt` when stored value is 0,
  fixing v2.0.3 where pre-existing instruments never had their weight populated.

## [2.0.3] — 2026-05-02

### Added
- **`initial_weight_pct` column** in `instrument_groups` — captures each
  instrument's actual T212 weight at first snapshot. When `use_group_weights=false`,
  these are used as targets so day-one drift = 0 on a fresh install.
- **Default `portfolio_phase: "Momentum-Chill"`** for new installs.
- **`panel_iframe` Portfolio Groups sidebar entry** — works without HA ingress.

## [2.0.2] — 2026-05-02

### Fixed
- **Ingress redirect** — `GET /` includes the HA ingress `root_path` prefix in
  the redirect target, so Open Web UI no longer 404s.

## [2.0.1] — 2026-05-02

### Fixed
- **ISA compact ticker `.L` resolution** — `VWRLl_EQ` → `VWRL.L` etc., applied
  at both display and Yahoo-fetch sites.
- `panel_iframe` registration via packages YAML.

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
