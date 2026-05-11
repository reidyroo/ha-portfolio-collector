# 📈 Portfolio Collector — Home Assistant Add-on

Monitor a [Trading 212](https://www.trading212.com) investment portfolio directly from
Home Assistant. Tracks live holdings, benchmarks against major indices, scores momentum,
and suggests rebalance trades — all running locally on your HA hardware.

T212 is the source of truth: positions, quantities, and prices are fetched live from your
T212 account on every snapshot. There is no manual holdings list to maintain.

---

> ⚠️ **Important: please read before using**
>
> **This is not financial advice.** I am not a financial advisor. Nothing in this project
> should be taken as a recommendation to buy, sell, or hold any investment. What you do
> with your portfolio is entirely your own decision and your own risk.
>
> **Demo mode is the default.** The add-on connects to the Trading 212 *demo* (paper
> trading) environment until you change `t212_base`. No real money is at stake until
> you explicitly switch to live mode — and even then every trade requires manual approval.
>
> **Hobby project.** Provided as-is, with no warranty. Use it, adapt it, break it, learn
> from it.

---

## Features

- **Live T212 snapshot** — positions, quantities, average price, current price pulled
  directly on every `/api/collect`. No manual holdings file.
- **Instrument catalog** — full T212 instrument list cached in SQLite (~16 000 entries),
  used to resolve Yahoo Finance symbols and detect GBX (pence) instruments.
- **Group system** — five strategy groups (Momentum Core / Global Beta / Regional
  Satellite / Defensive / Optional Factor). Assigned via a web UI; new instruments
  auto-detected and flagged as `unassigned` until you classify them.
- **Phase presets** — four named bundles of guard-rails and group allocations
  (Momentum-Max, Momentum-Chill, Balanced Growth, Pre-Retirement). Switching phases
  reconfigures CVaR limit, cost filter, cooldown, VIX threshold, and group splits in one move.
- **Two target-weight modes**, toggle independently of phase:
  - `use_group_weights: false` — targets = your T212 actual weights at sync time
    (zero drift on day one; only fires on real movement)
  - `use_group_weights: true` — targets = phase group allocations using the
    market/risk/CVaR-optimised dynamic weighting flow
    (actively pulls portfolio toward the configured shape)
- **Drift detection** — integer-boundary rule with VIX-aware threshold (1pt normal, 2pt
  elevated, frozen at extreme). Single-tick rounding noise never fires a trade.
- **Momentum scoring** — 3m / 6m / 9m / 12m price momentum + WMA trend signal +
  EMA 20/50 trend per holding. Tilts trade targets ±10–20%.
- **CVaR tail-risk constraint** — scales back non-defensive holdings if portfolio
  historical CVaR exceeds the phase limit.
- **Transaction cost filter** — skips trades where the drift-correction benefit ≤
  estimated round-trip cost.
- **Self-funding trade plan** — buys ≈ sells; one balancing trade absorbs residual cash.
- **Manual approval gate** — every trade requires a button press. Live mode is a separate
  toggle that defaults off after each session.
- **Benchmarks** — MSCI World (URTH), S&P 500, FTSE 100, Dow Jones, VIX. Daily history
  + alpha vs S&P 500 / MSCI World since purchase.
- **Snapshot validator** — refuses to write nonsense targets (zero target on an active
  holding, group totals not summing). Auto-recovers from `last_good_targets` baseline.
- **Sync T212 weights** — one-click reset of stored target weights to current T212 actuals.
  Drift returns to ~0 with no rebalance fire.
- **Health endpoint + version banner** — every snapshot carries the collector version
  (`collector_version`) so the dashboard can confirm what produced it.
- **HA dashboard** — five views (Overview, Holdings & Drift, Rebalance, Benchmarks, Groups)
  responsive on desktop and mobile.

---

## Requirements

| Requirement | Notes |
|---|---|
| Home Assistant OS / Supervised | Tested on HA Green (aarch64). Works on amd64 / armv7 too. |
| Trading 212 account | Free; demo or live API key from T212 → Settings → API |
| Internet from HA | Yahoo Finance prices fetched daily |
| HACS *(optional)* | For `apexcharts-card` charts on Overview / Benchmarks views |

---

## Installation

### 1. Add the repository to Home Assistant

1. **Settings → Add-ons → Add-on Store**
2. Top-right ⋮ → **Repositories**
3. Paste: `https://github.com/reidyroo/ha-portfolio-collector`
4. Close. **Portfolio Collector** appears in the store.

> **Local install (no GitHub):** copy the `portfolio_collector/` folder to
> `/addons/portfolio_collector/` via Samba, then Add-on Store → ⋮ → Check for updates.

### 2. Generate your T212 API token

T212 issues a Key ID + Secret separately. Combine them into Base64 before pasting.

```powershell
$keyId  = "paste-your-key-id-here"
$secret = "paste-your-secret-here"
$token  = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${keyId}:${secret}"))
Write-Output $token
```

On macOS / Linux:

```bash
echo -n "PASTE_KEY_ID:PASTE_SECRET" | base64
```

The output is the value for `t212_token`. **Do not** add `Basic` / `Bearer` prefixes — the
add-on prepends `Basic` automatically.

### 3. Install and start the add-on

1. **Portfolio Collector → Install** (first build: 3–5 minutes on aarch64)
2. **Configuration** tab:
   - paste `t212_token`
   - leave `t212_base` as `https://demo.trading212.com` (demo) until you are confident
   - leave `portfolio_phase` as `Momentum-Chill`
   - leave `use_group_weights: false` for now
3. **Save → Start**
4. **Log** tab — confirm the startup banner:
   ```
   Portfolio Collector v2.0.13 — phase=Momentum-Chill — DB: /data/portfolio.db ...
   ```

### 4. Deploy the HA package + dashboard

```bash
# From a machine that can reach HA's SSH/Terminal (or use the Terminal & SSH add-on):
curl -s -L https://raw.githubusercontent.com/reidyroo/ha-portfolio-collector/main/packages/portfolio.yaml \
  -o /config/packages/portfolio.yaml

curl -s -L https://raw.githubusercontent.com/reidyroo/ha-portfolio-collector/main/lovelace/dashboard.yaml \
  -o /config/lovelace/dashboard.yaml
```

Make sure `configuration.yaml` includes:

```yaml
homeassistant:
  packages: !include_dir_named packages

lovelace:
  mode: storage
  resources:
    - url: /hacsfiles/apexcharts-card/apexcharts-card.js
      type: module
  dashboards:
    lovelace-portfolio:
      mode: yaml
      title: Investment Monitor
      icon: mdi:chart-line
      show_in_sidebar: true
      filename: lovelace/dashboard.yaml
```

As of v2.5.0 the dashboard does not embed any external URLs — all controls
talk to the add-on through HA Core's loopback (`http://localhost:8000`),
which only HA itself can reach. There's nothing to rewrite on your LAN.

### 5. Restart Home Assistant Core

Settings → System → ⋮ → Restart → **Restart Home Assistant Core**.

This loads the package sensors, REST commands (`sync_t212_weights`,
`assign_instrument_group`, `set_risk_score`, `notch_up_cooldown`), and the
**Investment Monitor** dashboard with its five views.

### 6. First snapshot + steady state

All curl commands below run from inside HA's Terminal & SSH add-on (or via
SSH to your HA host). The add-on listens on `http://localhost:8000` from
that vantage point. From any other machine on your LAN, substitute your
HA's IP address (Settings → System → Network).

```bash
# 1. Confirm add-on is healthy
curl -s http://localhost:8000/api/health

# 2. Run the first snapshot — populates instrument catalog + initial weights
curl -X POST http://localhost:8000/api/collect

# 3. Sync the stored target weights to your live T212 actuals
#    (gives ~0% drift baseline for the next snapshot)
curl -X POST http://localhost:8000/api/sync-t212-weights

# 4. Run a second snapshot — drift should now be ~0% across the board
curl -X POST http://localhost:8000/api/collect

# 5. Inspect the result
curl -s http://localhost:8000/api/latest-snapshot | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'rebalance_needed: {d[\"rebalance_needed\"]}   '
      f'max_drift: {max(abs(p[\"drift_rel\"]) for p in d[\"positions\"]):.1f}%')
print()
print(f'{\"Symbol\":<10} {\"Group\":<22} {\"Actual\":>7}  {\"Target\":>6}  {\"DriftAbs\":>9}  {\"DriftRel\":>9}')
print('-' * 75)
for p in sorted(d['positions'], key=lambda x: -x['actual_wt']):
    print(f'{p[\"symbol\"]:<10} {p[\"group\"]:<22} {p[\"actual_wt\"]:>6.2f}%  '
          f'{p[\"target_wt\"]:>5}%  {p[\"drift_abs\"]:>+8.2f}pp  {p[\"drift_rel\"]:>+8.1f}%')
"
```

Expected: `rebalance_needed: 0`, `max_drift` under 5%, every target matches actual within
rounding. If anything is off, see [Diagnostics](#diagnostics) and [Recovery](#recovery).

### 7. Assign groups to instruments

By default, every new instrument is `unassigned`. Open the **Groups** view on the dashboard:

1. **Pick an instrument** from the dropdown (populated from the latest snapshot)
2. **Pick a target group** from the dropdown
3. Click **APPLY** — fires `rest_command.assign_instrument_group` which POSTs to the
   add-on, updating the SQLite `instrument_groups` table immediately
4. A persistent notification confirms the change
5. Click **Run Snapshot Now (refresh table)** to recompute targets with the new group

The table at the bottom of the view shows the current state of every holding —
unassigned rows are highlighted 🟡 with a count summary.

Everything stays within HA's auth boundary — no external URL, no DNS or firewall
accommodation needed. The legacy `/groups` HTML endpoint on the add-on still works
(`http://<HA-IP>:8000/groups`) for power users / curl, but is no longer the supported path.

Until at least one instrument has a group, group-based weight derivation falls back to
treating everything as `global_beta`.

Snapshots run automatically at **20:00 UK time on weekdays** (post-market close).

---

## Updating

Three things can move when a new release is published:

1. The **add-on** itself (`portfolio_collector/collector.py`, `config.yaml`, `Dockerfile`)
2. The **HA package YAML** (`packages/portfolio.yaml`) — sensors, REST commands, automations, panel_iframe
3. The **dashboard YAML** (`lovelace/dashboard.yaml`) — Lovelace card definitions

The add-on updates via the Supervisor UI (or `ha addons rebuild ...`). The two YAML files
live in your `/config/` directory and need to be re-pulled from the repo whenever they
change. Watch the [CHANGELOG](CHANGELOG.md) — entries that mention dashboard buttons,
sensors, REST commands, or sidebar panels mean those files have moved and need re-syncing.

### Update the add-on

```bash
# Force a clean rebuild — bypasses any cached image layer
ha addons rebuild 54e2df00_portfolio_collector
ha addons restart 54e2df00_portfolio_collector

# Confirm the new version is actually running
curl -s http://localhost:8000/api/health | python3 -c "import sys,json; print('v'+json.load(sys.stdin)['version'])"
```

If "Update available" appears in the Add-on Store before you do this, the regular Update
button works too — but `ha addons rebuild` is the one that always picks up changes when
Supervisor's image cache is stale.

### Re-sync the package + dashboard YAML

A ready-made script lives in the repo: [`sync_portfolio_files.sh`](sync_portfolio_files.sh).

**One-time install:**

```bash
curl -fsSL https://raw.githubusercontent.com/reidyroo/ha-portfolio-collector/main/sync_portfolio_files.sh \
  -o /config/sync_portfolio_files.sh
chmod +x /config/sync_portfolio_files.sh
```

**Run it any time:**

```bash
/config/sync_portfolio_files.sh
```

The script does:

1. `curl` the latest `packages/portfolio.yaml` to `/config/packages/portfolio.yaml`
2. `curl` the latest `lovelace/dashboard.yaml` to `/config/lovelace/dashboard.yaml`
3. `ha core check` to catch YAML errors before restarting
4. `ha core restart` to reload package sensors, REST commands, and the dashboard

> Since v2.5.0 the dashboard talks to the add-on only via `http://localhost:8000`
> from HA Core, so there's no `homeassistant.local` → `IP` rewrite to do anymore.
> If you've migrated from an older snapshot of the YAML files that still
> references `homeassistant.local:8000`, set `HA_IP=<your-ha-ip>` before
> running the script and it will substitute. Fresh deploys don't need it.

### Verify after sync

```bash
# Add-on version
curl -s http://localhost:8000/api/health | python3 -c "import sys,json; d=json.load(sys.stdin); print('v'+d['version'])"

# Package YAML has the latest REST commands
grep -c "sync_t212_weights" /config/packages/portfolio.yaml   # ≥ 1

# Dashboard YAML has the latest buttons
grep -c "Sync T212 Weights" /config/lovelace/dashboard.yaml   # ≥ 1

# HA service registered
# Developer Tools → Services → search "rest_command.sync_t212_weights" — should appear

# Dashboard banner shows the running version
# Open the Investment Monitor dashboard → Overview → top markdown card shows
# "Collector running: vX.Y.Z · Snapshot: ..."
```

If the dashboard banner is missing or shows an old version after a sync + restart:

- **Hard-refresh the browser** (Ctrl+Shift+R / Cmd+Shift+R) — Lovelace caches aggressively
- **Force a sensor repoll**: Developer Tools → Services → `homeassistant.update_entity`
  → entity_id: `sensor.portfolio_value, sensor.portfolio_snapshot` → Call

### Per-release upgrade ritual

When you see a new version on the repo:

1. Read the [CHANGELOG](CHANGELOG.md) entry for that version
2. Run `ha addons rebuild ...` (always)
3. Run `/config/sync_portfolio_files.sh` (only when the changelog mentions dashboard /
   package / sensor / automation changes — the typical cadence is every few releases)
4. Verify the version banner on the dashboard

---

## ETF groups

| Group key | Label | Purpose |
|---|---|---|
| `momentum_core` | Momentum Core | Alpha engine: high-momentum factor ETFs |
| `global_beta` | Global Beta | Broad-market global trackers |
| `regional_satellite` | Regional Satellite | Country / regional ETFs |
| `defensive` | Defensive | Bonds, short-duration gilts, money market |
| `optional_factor` | Optional Factor | Quality / minimum-volatility factor ETFs |

---

## Phase presets

A phase bundles guard-rails (CVaR, cooldown, VIX threshold, cost filter) **and** group
allocation defaults. Set via the Configuration tab or the dashboard phase selector.

| Phase | Mom Core | Global Beta | Regional | Defensive | Optional | CVaR | Cost | Cooldown | VIX high |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Momentum-Max | 35 | 40 | 15 | 5 | 5 | 6.5% | 0.10% | 21d | 25 |
| **Momentum-Chill** | 30 | 28 | 22 | 16 | 4 | 5.5% | 0.10% | 21d | 25 |
| Balanced Growth | 25 | 38 | 17 | 15 | 5 | 4.5% | 0.10% | 21d | 25 |
| Pre-Retirement | 10 | 33 | 12 | 35 | 10 | 3.0% | 0.20% | 28d | 20 |

Set `portfolio_phase` to anything else (e.g. `"Custom"`) to ignore presets and use the
individual `max_cvar_pct`, `cost_rate_pct`, `min_days_between_rebalance`, `vix_high_threshold`
options directly.

**Phase change does NOT touch `use_group_weights`** — that is an independent decision.

---

## Target-weight modes

Three ways to derive each holding's `target_wt`. Set via `weight_mode` in the add-on
Configuration tab. Phase guard-rails (CVaR, cooldown, VIX, cost filter) are independent —
they apply regardless of which weight mode you pick.

### `weight_mode: stored` (default — "match my T212 portfolio")

Targets come from each instrument's stored `initial_weight_pct`, which equals its actual
T212 weight at the time of the most recent **Sync T212 Weights → Targets** click.

- Day-one drift = 0
- Rebalance only fires if the portfolio actually moves away from the baseline
- Phase changes affect *guard-rails only* — your targets don't move
- Run **Sync T212 Weights → Targets** any time you want to redefine "where I am right
  now" as the new baseline

### `weight_mode: equal_in_group` ("pull toward equal-split phase allocations")

Targets = phase group allocation ÷ number of instruments in that group.

- Momentum-Chill with 5 instruments in Momentum Core → each gets 30% / 5 = 6%
- Switch phases → group totals jump to the new phase's preset (35/40/15/5/5 for Max,
  30/28/22/16/4 for Chill, etc.) and within-group is uniform
- Day-one drift may be large; the add-on suggests trades to converge

### `weight_mode: scaled_in_group` ("preserve my within-group ratios across phase changes")

Targets = phase group allocation, weighted by stored within-group ratio.

For each group: take the stored `initial_weight_pct` of each member, scale them so the
group sum equals the phase preset's allocation for that group.

**Worked example.** You hold VWRL.L=18%, SSAC.L=10% (both Global Beta). Stored sum = 28%.

| Phase | Global Beta total | VWRL target | SSAC target |
|---|:---:|:---:|:---:|
| Momentum-Chill | 28% | 18% × 28/28 = **18** | 10% × 28/28 = **10** |
| Momentum-Max | 40% | 18% × 40/28 = **26** | 10% × 40/28 = **14** |
| Pre-Retirement | 33% | 18% × 33/28 = **21** | 10% × 33/28 = **12** |

Switching back from Max → Chill restores the original 18/10 split. The within-group
relationship you established (e.g. "I want roughly 1.8× VWRL per SSAC") stays consistent
no matter which phase you toggle to. New scaling **only redistributes between groups** —
within-group ratios are sticky.

Falls back to equal-split for any group whose members have no stored history yet
(e.g. a brand-new install before the first **Sync T212 Weights** click).

### When to use which

| Goal | Mode |
|---|---|
| Day-to-day monitoring; only react to real portfolio moves | `stored` |
| Want each instrument in a group treated equally; happy to rebalance away from current shape | `equal_in_group` |
| Want phase changes to redistribute *between* groups while keeping *within-group* preferences | `scaled_in_group` |

Switch modes any time. Run a snapshot afterwards to see the new targets — config changes
don't recompute on their own.

### `weight_mode: dynamic` (v2.3.0+ — phase interpolation along a risk axis)

Treat the four phase presets as anchors on a 0–100 risk continuum:

| Phase | Risk anchor |
|---|:---:|
| Pre-Retirement | 15 |
| Balanced Growth | 45 |
| Momentum-Chill | 65 |
| Momentum-Max | 90 |

Set `risk_score` (0–100) and the system interpolates between adjacent anchors:

- `risk_score: 65` → exactly Momentum-Chill (30/28/22/16/4)
- `risk_score: 75` → 40% blend toward Momentum-Max (32/32.8/19.2/11.6/4.4)
- `risk_score: 90` → exactly Momentum-Max (35/40/15/5/5)

Within-group weighting always uses the scaled approach (preserves your stored
within-group ratios — VWRL still gets ~1.8× SSAC inside Global Beta no matter
the risk score).

#### Optional auto-adjustment from market signals

When `auto_adjust_enabled: true`, live signals can shift the **effective**
risk score from your set value:

| Direction | Signal | Effect |
|---|---|---|
| ⬇ Defensive | VIX above `vix_high_threshold` (default 25) | Risk shifts down proportionally; capped at aggressiveness max at VIX = high+15 |
| ⬇ Defensive | Portfolio drawdown below -10% from peak | Additional shift down, up to half the max, scaling linearly to -30% |
| ⬆ Aggressive *(`auto_adjust_direction: bidirectional` only)* | 21-day portfolio rally above +5% | Risk shifts up proportionally, capped at +5pts so it can't overwhelm your set value |

`auto_adjust_aggressiveness` caps the defensive shift:

- `low` → ±5 points
- `medium` → ±10 points (default)
- `high` → ±20 points

`auto_adjust_direction`:

- `defensive_only` (default) — only VIX/drawdown shifts apply (always toward safety)
- `bidirectional` — also lean in on strong positive momentum

The user's `risk_score` is never modified — only the **effective_risk** the
snapshot uses for that run. Dashboard banner shows both:

> **Risk:** 75 → effective 70.5 (VIX=27.0(-1.5); DD=-12.3%(-3.0))

When VIX normalises and drawdown closes, effective risk snaps back to your set
value automatically — no manual reset needed.

#### Live tuning via the dashboard slider

A **Risk Score slider** on the Rebalance dashboard tab (HA `input_number.portfolio_risk_score`)
calls `POST /api/set-risk-score` automatically when moved. Quick-set buttons
underneath jump to each phase anchor (15 / 45 / 65 / 90). Slider movement is
captured by the `portfolio_risk_score_change` automation; a notification
confirms the new value, and the next snapshot picks it up.

### Cooldown override

By default, an executed rebalance starts a 21-day cooldown to prevent
trade churn. Two ways to bypass it during sharp market moves:

#### Auto override (set-and-forget)

Enable `cooldown_override_enabled: true`. The cooldown is bypassed automatically
when:

- VIX exceeds `cooldown_override_vix_threshold` (default `vix_high_threshold + 5`, i.e. 30), **or**
- Drawdown exceeds `cooldown_override_drawdown_threshold` (default -15%)

Combined with `auto_adjust_enabled`, this lets the system drift defensively in
a real sell-off without you needing to act.

#### Manual notch-up (one-shot)

Click **Notch Up — Bypass Cooldown** on the Rebalance dashboard tab, or:

```bash
curl -X POST http://localhost:8000/api/notch-up
```

The next snapshot ignores cooldown for that run only; the flag is consumed
automatically. Useful when you spot a strong move and want to force a rebalance
without waiting (or without enabling permanent auto-override). Cancel with
**Cancel Notch-Up** if you change your mind before the next snapshot.

A persistent HA notification fires whenever any override actually consumes —
you'll always know when one took effect.

### Legacy compatibility

The pre-2.2 boolean flag `use_group_weights` is still respected for older configs:

- `use_group_weights: true` (no `weight_mode` set) → behaves like `weight_mode: dynamic`
- `use_group_weights: false` (no `weight_mode` set) → behaves like `weight_mode: stored`

If `weight_mode` is set, `use_group_weights` is ignored.

---

## Daily flow

1. **20:00 UK weekdays** — `automation.portfolio_daily_collect` triggers `/api/collect`
2. Snapshot pulls T212 positions + Yahoo prices + benchmarks
3. Computes drift, momentum, CVaR, cost-filtered trade plan
4. Validator checks targets are sane (auto-recover if not — see [Safeguards](#safeguards))
5. Stores result in SQLite
6. HA REST sensors update; dashboard refreshes within 5 minutes
7. If `rebalance_needed=1`, persistent notification appears with the reason
8. You open the dashboard, review the trade plan, and either:
   - Click **Approve — Dry Run** to log without ordering, or
   - Toggle **⚠ Live Trading Mode**, then **Approve Trades** to submit to T212

---

## Configuration reference

All editable in the add-on Configuration tab.

### Required

| Option | Description |
|---|---|
| `t212_token` | Base64-encoded `keyId:secret` from T212 |
| `t212_base` | `https://demo.trading212.com` (paper) or `https://live.trading212.com` |
| `purchase_date` | Anchors benchmark "since-purchase" returns. Format: `YYYY-MM-DD` |

### Phase + weight mode

| Option | Default | Description |
|---|---|---|
| `portfolio_phase` | `Momentum-Chill` | One of the four named phases, or any other string for custom mode |
| `use_group_weights` | `false` | `true` = phase group allocations using risk/CVaR-driven dynamic weighting, `false` = stored T212 weights |

### Guard-rails (when `portfolio_phase` is custom)

| Option | Default | Description |
|---|---|---|
| `drift_threshold_pct` | `15` | Reference threshold shown on dashboard |
| `vix_high_threshold` | `25` | Above this, ≥ 2pt drift required to fire |
| `vix_extreme_threshold` | `35` | Above this, all rebalancing frozen |
| `min_days_between_rebalance` | `21` | Cooldown after an executed rebalance |
| `max_cvar_pct` | `5.0` | Tail-risk cap. `0` to disable |
| `cost_rate_pct` | `0.1` | Round-trip cost in %. Trades below this benefit are skipped |
| `max_snapshot_change_pct` | `20` | Reject snapshot if portfolio value moves by more than this since the previous one (sanity check). `0` to disable |
| `catalog_cache_ttl_sec` | `3600` | Instrument catalog refresh frequency (T212 rate-limits to 1/50s) |

---

## Dashboard

Five views, accessible from the Investment Monitor sidebar entry:

| View | Contents |
|---|---|
| **Overview** | Portfolio Health gauge + state, Health Breakdown table (4 sub-scores + drivers + rebalance alert), Portfolio Summary glance (value / return / cash / VIX), Allocation by Group table, Group donut chart, Portfolio Value 90d chart, Return History 90d chart (portfolio vs benchmarks) |
| **Holdings** | Holdings table (group / symbol / actual % / target % / drift % / P&L % / value), Momentum & Signal Scores table |
| **Rebalance** | Status glance (rebalance signal / VIX regime / VIX / snapshot age), Active Phase summary, Rebalance Reason, Trade Plan table, Controls (phase selector / drift threshold / live mode / approve), Dynamic Risk Score slider, Cooldown override buttons, Action buttons (Run Snapshot / Dry Run / Reset Cooldown / Refresh Catalog / Sync T212 Targets) |
| **Benchmarks** | Alpha History 90d chart (vs S&P 500 / MSCI World), VIX 90d chart, Returns Since Purchase glance |
| **Groups** | Assign Group form (instrument + group dropdowns + Apply button), Current assignments table |

### Portfolio Health Score

A composite 0–100 score computed entirely from HA template sensors (no backend changes needed):

| Sub-score | Weight | Formula |
|---|:---:|---|
| Return Edge | 35% | `clamp(alpha_vs_MSCI × 5 + 50, 0, 100)` — alpha 0 = 50, +10% alpha = 100 |
| Resilience | 25% | `clamp(100 + drawdown_pct × 3, 0, 100)` — drawdown −10% = 70 |
| Drift Discipline | 20% | `clamp(100 − total_group_drift × 4, 0, 100)` |
| Momentum Quality | 20% | `clamp(avg_signal_score × 500 + 50, 0, 100)` |

**Health states:** Strong (≥80) / Acceptable (≥65) / Caution (≥50) / Weak (<50).

---

## API reference

The add-on serves a REST API on port 8000.

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Redirects to `/groups` (HA ingress entry point) |
| `GET` | `/groups` | HTML group-management UI |
| `GET` | `/api/health` | Liveness, version, phase, catalog age |
| `GET` | `/api/latest-snapshot` | Most recent snapshot (consumed by HA REST sensors) |
| `GET` | `/api/snapshots?limit=N` | Snapshot history (default 90) |
| `GET` | `/api/snapshots?summary=true` | Lightweight: just `as_of` + `portfolio_value` |
| `DELETE` | `/api/snapshots` | Delete snapshots by filter — see [Database management](#database-management-via-api) |
| `POST` | `/api/snapshots/anchor` | Insert/update a value-only anchor row to pin the return baseline |
| `POST` | `/api/snapshots/backfill-returns` | Recompute all historical `portfolio_return_pct` from the earliest snapshot |
| `GET` | `/api/benchmarks?days=N` | Benchmark history |
| `POST` | `/api/collect` | Run a full snapshot now |
| `POST` | `/api/approve/{as_of}` | Approve rebalance; `?execute=true` places live orders |
| `POST` | `/api/reset-cooldown` | Clear the rebalance cooldown |
| `POST` | `/api/set-phase` | Body `{"phase":"Momentum-Chill"}`; writes to options.json |
| `POST` | `/api/set-risk-score` | Body `{"risk_score": 75}`; live-tune dynamic mode |
| `POST` | `/api/notch-up` | Set one-shot flag: next snapshot bypasses cooldown |
| `POST` | `/api/cancel-notch-up` | Clear a pending notch-up flag without consuming |
| `GET` | `/api/risk-state` | Inspect dynamic-mode + override runtime state |
| `POST` | `/api/sync-t212-weights` | Reset stored target weights to current T212 actuals |
| `GET` | `/api/groups` | List all instrument group assignments |
| `POST` | `/api/groups/{ticker}` | Set group label; body `{"group":"momentum_core"}` |
| `POST` | `/api/groups/{ticker}/tradeable` | Re-enable a ticker auto-flagged as untradeable; body `{"tradeable": true}` |
| `GET` | `/api/t212/positions` | Pass-through of raw T212 portfolio positions |
| `GET` | `/api/t212/pies` | List all T212 auto-invest pies on the account |
| `GET` | `/api/t212/pie/{pie_id}` | Full pie detail including current `instrumentShares` |
| `POST` | `/api/push-to-pie` | Manually push latest snapshot weights to the active pie |
| `GET` | `/api/catalog/status` | Catalog cache age + count |
| `POST` | `/api/catalog/refresh` | Force catalog re-fetch |
| `GET` | `/api/last-good-targets` | Inspect the validator's recovery baseline |

---

## T212 Auto-Invest Pie workflow

If your entire portfolio is held inside a T212 auto-invest pie, direct market orders on
those instruments are rejected by T212. The collector detects this automatically and switches
to **pie execution mode** — updating the pie's `instrumentShares` proportions instead of
placing orders.

### How it works

1. At snapshot time, `pie_detected` is set in the snapshot metadata. The dashboard shows
   **execution_mode: pie** and displays the pie ID.
2. When you approve a rebalance with **execute=true**, the collector:
   - Converts the suggested trade weights into normalised `instrumentShares` (must sum to 1.0)
   - Calls `PUT /api/v0/equity/pies/{pie_id}` with the new shares **and** an incremented
     version suffix in the pie name (e.g. `"My Portfolio v3"`)
   - The name increment is a T212 API workaround — without a payload change beyond
     `instrumentShares`, T212 sometimes silently ignores the update
3. T212 progressively rebalances the pie toward the new target over subsequent sessions

### Step-by-step: rebalancing a pie portfolio

```bash
# 1. Confirm pie is detected
curl -s http://localhost:8000/api/latest-snapshot | python3 -c "
import sys, json; d = json.load(sys.stdin)
print('pie_detected:', d.get('pie_detected'))
print('pie_id:      ', d.get('pie_id'))
print('exec_mode:   ', d.get('execution_mode'))
"

# 2. List pies on the account (confirm the right one is active)
curl -s http://localhost:8000/api/t212/pies | python3 -m json.tool

# 3. Inspect the current pie weights
curl -s http://localhost:8000/api/t212/pie/<pie_id> | python3 -m json.tool

# 4. Run a snapshot to get fresh positions and trade plan
curl -X POST http://localhost:8000/api/collect

# 5. Review trade plan on the dashboard Rebalance tab, then approve + execute:
curl -X POST "http://localhost:8000/api/approve/latest?execute=true"

# 6. Verify the pie was updated (name should have incremented)
curl -s http://localhost:8000/api/t212/pie/<pie_id> | python3 -c "
import sys, json; d = json.load(sys.stdin)
print('Pie name:', d['settings']['name'])
for t, s in d.get('instrumentShares', {}).items():
    print(f'  {t}: {s:.4f}')
"

# 7. Manually push weights without a full rebalance approval (emergency / test)
curl -X POST http://localhost:8000/api/push-to-pie
```

### Configuration options

| Option | Default | Description |
|---|---|---|
| `force_direct_orders_when_pie` | `false` | Set `true` to attempt direct orders even when a pie is detected. T212 will reject them for pie-held instruments — only useful if you have a mixed account. |
| `rebalance_settle_seconds` | `5` | Seconds to wait between SELL leg and BUY leg. Irrelevant in pie mode (no two-phase execution), but applies when `force_direct_orders_when_pie: true`. |

---

## Database management via API

The collector's SQLite database is inside the add-on container and not directly accessible
from outside. All management is done through the REST API. All commands below run from
inside HA's Terminal & SSH add-on, or any machine that can reach port 8000.

### View portfolio value history

```bash
# Lightweight summary — just dates and values
curl -s "http://localhost:8000/api/snapshots?summary=true" | python3 -m json.tool

# Full history with return %
curl -s "http://localhost:8000/api/snapshots?limit=90" | python3 -c "
import sys, json
rows = json.load(sys.stdin)
print(f'{'Date':<25} {'Value (GBP)':>12} {'Return %':>10}')
print('-' * 50)
for r in rows:
    print(f'{r[\"as_of\"]:<25} {r[\"portfolio_value\"]:>12.2f} {r.get(\"portfolio_return_pct\", 0):>10.2f}')
"
```

### View benchmark history

```bash
curl -s "http://localhost:8000/api/benchmarks?days=30" | python3 -c "
import sys, json
rows = json.load(sys.stdin)
for r in rows:
    b = r['benchmarks']
    msci = b.get('msci_world', {}).get('return_since_purchase', 'n/a')
    sp   = b.get('sp500',      {}).get('return_since_purchase', 'n/a')
    vix  = b.get('vix',        {}).get('latest', 'n/a')
    print(f'{r[\"as_of\"][:10]}  MSCI={msci}%  SP500={sp}%  VIX={vix}')
"
```

### Set a return baseline (anchor)

Use this after a rebalance that resets T212's cost basis, or on a fresh install where you
know what the portfolio was worth on day one.

```bash
# Insert a stub snapshot for a historical date with a known value
curl -X POST http://localhost:8000/api/snapshots/anchor \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-04-07", "value": 5000}'

# With starting cash
curl -X POST http://localhost:8000/api/snapshots/anchor \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-04-07", "value": 5000, "cash": 0}'
```

The anchor row becomes the earliest snapshot in the DB. All subsequent `portfolio_return_pct`
calculations use it as the baseline (rather than T212's cost basis, which resets on
pie/rebalance updates).

### Backfill historical return values

After setting a new anchor, retrofit all existing snapshots so their stored
`portfolio_return_pct` reflects the new baseline:

```bash
curl -X POST http://localhost:8000/api/snapshots/backfill-returns
```

Response confirms rows updated and the baseline used:
```json
{"updated": 24, "base_date": "2026-04-07T00:00:00+00:00", "base_value": 5000.0}
```

**Typical sequence after a rebalance resets your cost basis:**

```bash
# 1. Set the anchor to your known starting value
curl -X POST http://localhost:8000/api/snapshots/anchor \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-04-07", "value": 5000}'

# 2. Backfill all historical return % in the DB
curl -X POST http://localhost:8000/api/snapshots/backfill-returns

# 3. Run a fresh snapshot so HA sensors pick up the corrected current return
curl -X POST http://localhost:8000/api/collect
```

### Delete bad snapshots (spikes / corrupt data)

```bash
# Delete a specific date
curl -X DELETE "http://localhost:8000/api/snapshots?date=2026-04-28"

# Delete snapshots before a date
curl -X DELETE "http://localhost:8000/api/snapshots?before=2026-04-01"

# Delete rogue upward spikes above a threshold
curl -X DELETE "http://localhost:8000/api/snapshots?max_value=7000"

# Delete rogue downward spikes below a threshold
curl -X DELETE "http://localhost:8000/api/snapshots?min_value=3000"

# Combine filters — scoped to a date range
curl -X DELETE "http://localhost:8000/api/snapshots?after=2026-04-01&max_value=7000"
```

The response lists every row that was deleted before removal, so you can verify:
```json
{
  "deleted": 2,
  "removed_rows": [
    {"as_of": "2026-04-15T20:00:00+00:00", "portfolio_value": 45000.0},
    {"as_of": "2026-04-22T20:00:00+00:00", "portfolio_value": 48000.0}
  ]
}
```

---

## Diagnostics

A grab-bag of one-liner curl commands. Run them from inside HA's Terminal &
SSH add-on (or via SSH to your HA host) where `localhost:8000` reaches the
collector. From another machine on your LAN, substitute HA's IP for
`localhost`.

### Verify the add-on version

```bash
curl -s http://localhost:8000/api/health | python3 -c "import sys,json; d=json.load(sys.stdin); print('v'+d['version'], '— phase:', d['phase'])"
```

### Inspect stored group / weight state

```bash
curl -s http://localhost:8000/api/groups | python3 -c "
import sys, json
rows = json.load(sys.stdin)
total = sum(r['initial_weight_pct'] for r in rows)
print(f'{len(rows)} instruments, sum of weights = {total:.2f}%')
for r in sorted(rows, key=lambda x: -x['initial_weight_pct']):
    print(f\"  {r['t212_ticker']:<20} {r['yahoo_symbol']:<10} {r['group_label']:<22} initial={r['initial_weight_pct']:>6.2f}%\")
"
```

### Inspect the validator recovery baseline

```bash
curl -s http://localhost:8000/api/last-good-targets | python3 -m json.tool
```

### Latest snapshot — full position table

```bash
curl -s http://localhost:8000/api/latest-snapshot | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'snapshot: {d[\"as_of\"]}  collector_version: {d.get(\"collector_version\")}')
print(f'rebalance_needed: {d[\"rebalance_needed\"]}   '
      f'max_drift: {max(abs(p[\"drift_rel\"]) for p in d[\"positions\"]):.1f}%')
print()
print(f'{\"Symbol\":<10} {\"Group\":<22} {\"Actual\":>7}  {\"Target\":>6}  {\"DriftAbs\":>9}  {\"DriftRel\":>9}')
print('-' * 75)
for p in sorted(d['positions'], key=lambda x: -x['actual_wt']):
    print(f'{p[\"symbol\"]:<10} {p[\"group\"]:<22} {p[\"actual_wt\"]:>6.2f}%  '
          f'{p[\"target_wt\"]:>5}%  {p[\"drift_abs\"]:>+8.2f}pp  {p[\"drift_rel\"]:>+8.1f}%')
"
```

### Force HA REST sensors to repoll

If the dashboard is showing stale data after a curl-driven snapshot:

```bash
# In the HA Terminal & SSH add-on:
ha core check  # should show no errors
# Or via Developer Tools → Services → homeassistant.update_entity:
#   entity_id: sensor.portfolio_value, sensor.portfolio_snapshot
```

Then hard-refresh the browser (Ctrl+Shift+R / Cmd+Shift+R).

### Watch the live add-on log

Settings → Add-ons → Portfolio Collector → **Log**. Look for:

- Startup banner with version + phase
- `Snapshot saved: £... return=... max_drift=... rebalance=False` (healthy steady state)
- `Target validation FAILED: ...` (validator caught a bad snapshot — see [Safeguards](#safeguards))
- `Recovered targets from last_good_targets table` (recovery fired — investigate root cause)

---

## Safeguards

The snapshot pipeline has three layers of protection against bad target weights.

### 1. Output validator

After computing targets and actual weights, `_validate_targets` runs three rules:

- **No active position with target = 0** — any holding with `actual_wt ≥ 0.5%` must have a
  non-zero target. (This catches the partial-data bug that would otherwise suggest
  liquidating a real position.)
- **Target sum = 100% ± 2pp** — total of all target weights must round-trip back to ~100.
- **No active group has zero target** — any group whose holdings sum to ≥ 1% actual must
  have a non-zero target sum.

If any rule fails, the targets are rejected before being stored.

### 2. Auto-recovery from `last_good_targets`

Every passed validation also writes the targets to a `last_good_targets` SQLite table. On
the next failed validation, the bad targets are silently replaced with the last-known-good
ones in-memory; drift is recomputed; the snapshot is stored with sane numbers.

The error is logged loudly:

```
ERROR Target validation FAILED: VWRL.L has actual_wt=18.00% but target_wt=0 — partial-data leak
WARNING Recovered targets from last_good_targets table
```

### 3. Equal-weight last resort

If validation fails *and* no recovery baseline exists (e.g. very first snapshot),
each holding receives `100/n%` as target. Not ideal, but never destructive.

You can inspect the current recovery baseline:

```bash
curl -s http://localhost:8000/api/last-good-targets | python3 -m json.tool
```

---

## Recovery

### "I see weird targets / wild drift / nonsense allocations"

1. **Force a fresh snapshot** — old behaviour may be cached:
   ```bash
   curl -X POST http://localhost:8000/api/collect
   ```
2. **If targets still look wrong** — sync to live T212 actuals:
   ```bash
   curl -X POST http://localhost:8000/api/sync-t212-weights
   curl -X POST http://localhost:8000/api/collect
   ```
3. **Check `use_group_weights`** — if you didn't intend the phase/market/risk-driven
  dynamic target flow, set it to `false` in the Configuration tab and run a snapshot.
4. **Inspect the validator** — `curl /api/last-good-targets` shows what the recovery
   baseline contains; the add-on log shows whether the validator fired.

### "I just installed an update but the dashboard still shows old behaviour"

Supervisor sometimes reuses cached Docker images. Force a clean rebuild:

```bash
ha addons rebuild 54e2df00_portfolio_collector
ha addons restart 54e2df00_portfolio_collector
curl -s http://localhost:8000/api/health  # confirm new version
```

If `Rebuild` itself doesn't help: stop, **uninstall (without ticking "Remove data")**,
reload the add-on store, reinstall. The `/data/portfolio.db` (snapshots, group
assignments, recovery baseline) is on a persistent volume that survives uninstall.

### "I want to wipe and start fresh"

```bash
# Stops the add-on and removes the DB. You will lose all snapshots and group assignments.
ha addons stop 54e2df00_portfolio_collector
docker exec addon_54e2df00_portfolio_collector rm /data/portfolio.db   # via Terminal & SSH
ha addons start 54e2df00_portfolio_collector
```

### "Open Web UI returns 404"

Your initial install pre-dates `ingress: true` in `config.yaml`. Either:

- Use the direct URL `http://<HA-IP>:8000/groups` (the dashboard's *Open Group Manager*
  button does this), **or**
- Uninstall + reinstall the add-on so Supervisor registers an ingress route. Data
  on `/data` survives uninstall as long as you don't tick "Remove add-on data".

### "Rebalance cooldown stuck after a failed live trade"

Press **Reset Rebalance Cooldown** on the Rebalance dashboard tab, or:

```bash
curl -X POST http://localhost:8000/api/reset-cooldown
```

---

## Currency handling

- **LSE holdings** (`.L`): T212 reports `currentPrice` in GBX (pence) for some, GBP for
  others. The catalog's `currency_code` field disambiguates; GBX prices are divided by 100.
- **Xetra** (`.DE`): EUR. Live `EURGBP=X` rate fetched from Yahoo each snapshot.
- **US** (no suffix): T212's `currentPrice` is already GBP-converted.

The `sync-t212-weights` endpoint reads `actual_wt` from the latest snapshot (which already
applies these conversions) rather than re-deriving from raw T212 prices, so mixed-currency
portfolios are handled correctly.

---

## Data & privacy

- All portfolio data stored locally in `/data/portfolio.db` (SQLite, persistent volume)
- Outbound calls only to:
  - **Trading 212** — your own account API (read positions, place orders if approved)
  - **Yahoo Finance** — public price data, no account required
- No telemetry, no third-party analytics, no cloud sync

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `404 No snapshots yet` | First run | `curl -X POST http://localhost:8000/api/collect` |
| Dashboard sensors `Unavailable` | Package not loaded | Check `/config/packages/portfolio.yaml` exists; restart HA Core |
| `Action rest_command.sync_t212_weights not found` | HA hasn't reloaded since package was deployed | Restart HA Core |
| Add-on log: `IITU possibly delisted` | Version older than 2.0.6 | Rebuild add-on; v2.0.6+ resolves bare `_EQ` tickers to `.L` |
| Target sums to 55/19/20/0/6 | Older version with partial-data leak | Update to ≥ v2.0.8; v2.0.13 validator prevents recurrence |
| Live `Open Web UI` button → 404 | Pre-existing install lacks ingress entry | Uninstall + reinstall (data preserved) |
| `homeassistant.local` doesn't resolve | mDNS unreliable on some networks | Replace with HA's static IP in `packages/portfolio.yaml` and `lovelace/dashboard.yaml` |
| Yahoo rate-limit errors | Burst of requests | Add-on auto-retries with backoff; wait a few minutes |
| Snapshot rejected as "value changed by >20%" | Sanity check tripped | Investigate the cause; if legitimate, set `max_snapshot_change_pct: 0` to disable |

---

## Disclaimer

This project is provided for **educational and personal interest purposes only**.

- I am not a financial advisor
- Nothing here is investment advice
- Past performance of any strategy shown is not indicative of future results
- You are solely responsible for any investment decisions you make
- Always do your own research before investing

The project defaults to **demo mode** so you can explore it safely. Take your time to
understand what it does before enabling live trading.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## Licence

MIT. Use it, fork it, improve it. Sharing back is appreciated but never required.
