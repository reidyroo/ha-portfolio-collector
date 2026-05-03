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
  - `use_group_weights: true` — targets = phase group allocations split equally within group
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

The dashboard URLs default to `http://homeassistant.local:8000/groups`. If that doesn't
resolve on your network, replace it with HA's static IP:

```bash
# Replace 192.168.1.6 with your HA's LAN IP from Settings → System → Network
sed -i 's|http://homeassistant.local:8000|http://192.168.1.6:8000|g' /config/packages/portfolio.yaml
sed -i 's|http://homeassistant.local:8000|http://192.168.1.6:8000|g' /config/lovelace/dashboard.yaml
```

### 5. Restart Home Assistant Core

Settings → System → ⋮ → Restart → **Restart Home Assistant Core**.

This loads the package sensors, the REST commands (including `sync_t212_weights`),
the dashboard, and the Portfolio Groups sidebar panel.

### 6. First snapshot + steady state

```bash
HA_IP=192.168.1.6   # ← your HA's LAN IP

# 1. Confirm add-on is healthy
curl -s http://$HA_IP:8000/api/health

# 2. Run the first snapshot — populates instrument catalog + initial weights
curl -X POST http://$HA_IP:8000/api/collect

# 3. Sync the stored target weights to your live T212 actuals
#    (gives ~0% drift baseline for the next snapshot)
curl -X POST http://$HA_IP:8000/api/sync-t212-weights

# 4. Run a second snapshot — drift should now be ~0% across the board
curl -X POST http://$HA_IP:8000/api/collect

# 5. Inspect the result
curl -s http://$HA_IP:8000/api/latest-snapshot | python3 -c "
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

By default, every instrument is `unassigned`. Click **Open Group Manager** on the dashboard
or open `http://<HA-IP>:8000/groups` directly. Each row has a dropdown — assign each
holding to one of the five groups. Changes save automatically.

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
curl -s http://$HA_IP:8000/api/health | python3 -c "import sys,json; print('v'+json.load(sys.stdin)['version'])"
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

**Run it any time** (passing your static IP as an env var so the iframe / dashboard
button URLs get rewritten to the LAN address):

```bash
HA_IP=192.168.1.6 /config/sync_portfolio_files.sh
```

If `homeassistant.local` resolves cleanly on your network, leave `HA_IP` unset — the
upstream URLs will be kept as-is.

The script does:

1. `curl` the latest `packages/portfolio.yaml` to `/config/packages/portfolio.yaml`
2. `curl` the latest `lovelace/dashboard.yaml` to `/config/lovelace/dashboard.yaml`
3. `sed` rewrite `homeassistant.local` → `$HA_IP` in both files (if `HA_IP` is set)
4. `ha core check` to catch YAML errors before restarting
5. `ha core restart` to reload package sensors, REST commands, and the dashboard

### Verify after sync

```bash
# Add-on version
curl -s http://$HA_IP:8000/api/health | python3 -c "import sys,json; d=json.load(sys.stdin); print('v'+d['version'])"

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

### Legacy compatibility

The pre-2.2 boolean flag `use_group_weights` is still respected for older configs:

- `use_group_weights: true` (no `weight_mode` set) → behaves like `weight_mode: equal_in_group`
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
| `use_group_weights` | `false` | `true` = phase group allocations, `false` = stored T212 weights |

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
| **Overview** | Collector version banner, value (90d chart), total return, free cash, VIX regime, benchmark returns since purchase, alpha vs S&P 500 / MSCI World, snapshot age |
| **Holdings & Drift** | Allocation by group (target vs actual + delta), per-holding table (group, symbol, actual %, target %, drift %, P&L %, value), relative drift bar chart, momentum scores |
| **Rebalance** | Status, phase settings, drift slider, live-mode toggle, trade plan table, sync / snapshot / approval / cooldown-reset buttons |
| **Benchmarks** | 90d return chart, alpha history, since-purchase + 3M returns |
| **Groups** | Markdown intro + Open Group Manager button (opens add-on's `/groups` UI directly) |

The collector version banner at the top of Overview shows what version produced the
latest snapshot — use it to verify upgrades took effect.

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
| `DELETE` | `/api/snapshots?date=YYYY-MM-DD` | Delete all snapshots for a given date |
| `GET` | `/api/benchmarks?days=N` | Benchmark history |
| `POST` | `/api/collect` | Run a full snapshot now |
| `POST` | `/api/approve/{as_of}` | Approve rebalance; `?execute=true` places live orders |
| `POST` | `/api/reset-cooldown` | Clear the rebalance cooldown |
| `POST` | `/api/set-phase` | Body `{"phase":"Momentum-Chill"}`; writes to options.json |
| `POST` | `/api/sync-t212-weights` | Reset stored target weights to current T212 actuals |
| `GET` | `/api/groups` | List all instrument group assignments |
| `POST` | `/api/groups/{ticker}` | Set group label; body `{"group":"momentum_core"}` |
| `GET` | `/api/catalog/status` | Catalog cache age + count |
| `POST` | `/api/catalog/refresh` | Force catalog re-fetch |
| `GET` | `/api/last-good-targets` | Inspect the validator's recovery baseline |

---

## Diagnostics

A grab-bag of one-liner curl commands. Set `HA_IP` first:

```bash
HA_IP=192.168.1.6
```

### Verify the add-on version

```bash
curl -s http://$HA_IP:8000/api/health | python3 -c "import sys,json; d=json.load(sys.stdin); print('v'+d['version'], '— phase:', d['phase'])"
```

### Inspect stored group / weight state

```bash
curl -s http://$HA_IP:8000/api/groups | python3 -c "
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
curl -s http://$HA_IP:8000/api/last-good-targets | python3 -m json.tool
```

### Latest snapshot — full position table

```bash
curl -s http://$HA_IP:8000/api/latest-snapshot | python3 -c "
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
curl -s http://$HA_IP:8000/api/last-good-targets | python3 -m json.tool
```

---

## Recovery

### "I see weird targets / wild drift / nonsense allocations"

1. **Force a fresh snapshot** — old behaviour may be cached:
   ```bash
   curl -X POST http://$HA_IP:8000/api/collect
   ```
2. **If targets still look wrong** — sync to live T212 actuals:
   ```bash
   curl -X POST http://$HA_IP:8000/api/sync-t212-weights
   curl -X POST http://$HA_IP:8000/api/collect
   ```
3. **Check `use_group_weights`** — if you didn't intend phase-driven targets, set it
   to `false` in the Configuration tab and run a snapshot.
4. **Inspect the validator** — `curl /api/last-good-targets` shows what the recovery
   baseline contains; the add-on log shows whether the validator fired.

### "I just installed an update but the dashboard still shows old behaviour"

Supervisor sometimes reuses cached Docker images. Force a clean rebuild:

```bash
ha addons rebuild 54e2df00_portfolio_collector
ha addons restart 54e2df00_portfolio_collector
curl -s http://$HA_IP:8000/api/health  # confirm new version
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
curl -X POST http://$HA_IP:8000/api/reset-cooldown
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
| `404 No snapshots yet` | First run | `curl -X POST http://$HA_IP:8000/api/collect` |
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
