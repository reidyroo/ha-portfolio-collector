# 📈 Portfolio Collector — Home Assistant Add-on

Monitor a [Trading 212](https://www.trading212.com) investment portfolio directly from Home Assistant.
Tracks up to 20 holdings, benchmarks against major indices, scores momentum, and suggests
rebalance trades — all running locally on your HA hardware.

---

> ⚠️ **Important: Please read before using**
>
> **This project is not financial advice.** I am not a financial advisor. Nothing in this
> project should be taken as a recommendation to buy, sell, or hold any investment.
> What you do with your portfolio is entirely your own decision and your own risk.
>
> **This project starts in demo mode deliberately.** It connects to the Trading 212 demo
> (paper trading) environment by default. No real money is involved until you explicitly
> switch to live mode — and even then, every trade requires manual approval.
>
> **This is a hobby project**, created for fun and personal interest. It is provided as-is,
> with no warranty of any kind. Use it, adapt it, break it, learn from it — enjoy! 😊

---

## Features

- **Daily snapshot** of your Trading 212 portfolio via the T212 API
- **Benchmark comparison** — MSCI World (URTH), S&P 500, FTSE 100, DOW, VIX
- **ETF group system** — holdings tagged across five strategy groups, each with a target allocation
- **Drift detection** — integer-boundary rule: flags holdings ≥ 1 whole point from target in normal markets, ≥ 2 points when VIX is elevated; single-tick noise never fires
- **Momentum scoring** — 3m / 6m / 9m / 12m price momentum + WMA trend signal + EMA 20/50 trend per holding
- **Rebalance suggestions** — momentum-adjusted, CVaR-constrained trade plan; self-funding (buys ≈ sells); always requires manual approval
- **VIX regime filter** — elevated (>25): ≥ 2pt gap required; extreme (>35): frozen
- **CVaR tail-risk constraint** — scales back non-defensive holdings if portfolio tail risk exceeds a configurable limit
- **Transaction cost filter** — skips trades where the drift-correction benefit is smaller than the estimated round-trip cost
- **T212 portfolio sync** — one-button sync of actual quantities and cost basis from your T212 account; new holdings auto-added with current weights as defaults
- **Configurable via the HA UI** — tickers, weights, groups, and guard-rails without editing files
- **Demo-first** — defaults to T212 demo; live trading requires explicit opt-in each session
- **4-view Lovelace dashboard** — responsive on desktop and mobile; Overview, Holdings & Drift, Rebalance, Benchmarks

---

## Requirements

| Requirement | Notes |
|---|---|
| Home Assistant OS | Tested on HA Green (aarch64). Works on any HAOS hardware. |
| Trading 212 account | Free account; demo or live API key from T212 Settings → API |
| Internet access from HA | Yahoo Finance prices fetched daily |
| HACS (optional) | For `apexcharts-card` dashboard charts |

---

## Installation

### 1. Add this repository to Home Assistant

1. **Settings → Add-ons → Add-on Store**
2. Top-right menu (⋮) → **Repositories**
3. Paste: `https://github.com/reidyroo/ha-portfolio-collector`
4. Close — **Portfolio Collector** now appears in the store

> **Local install (no GitHub):**
> Copy the `portfolio_collector/` folder to `/config/addons/portfolio_collector/`
> via Samba share (`\\homeassistant\config\addons\`), then
> Add-on Store → ⋮ → **Check for updates**.

### 2. Generate your T212 API token

Trading 212 issues an **API Key ID** and an **API Secret** separately. You must combine
them into a single Base64-encoded string before pasting it into the add-on.

**Steps:**

1. In the T212 app go to **Settings → API → Generate key**
2. Copy both the **Key ID** and the **Secret** (the secret is only shown once)
3. Run the following in PowerShell on your PC to generate the token:

```powershell
$keyId  = "paste-your-key-id-here"
$secret = "paste-your-secret-here"

$token = [Convert]::ToBase64String(
    [System.Text.Encoding]::UTF8.GetBytes("${keyId}:${secret}")
)

Write-Output "Your t212_token value:"
Write-Output $token
```

4. Copy the output string — that is the value to paste into `t212_token` in the add-on
   Configuration tab. Do **not** add any `Basic` or `Bearer` prefix; the add-on adds the
   correct `Authorization: Basic <token>` header automatically.

> **Keep your Key ID and Secret safe.** Do not commit them to Git or share them.
> If you suspect they have been exposed, regenerate the key in T212 immediately.

### 3. Install and configure the add-on

1. Click **Portfolio Collector → Install** (first build: 3–5 minutes)
2. Go to the **Configuration** tab
3. Paste the Base64 token generated above into `t212_token`
4. Leave `t212_base` as `https://demo.trading212.com` until you are confident
5. **New users:** leave the default holdings in place for now — you will sync your real
   portfolio in step 7 below
6. **Save → Start**

### 4. Copy the HA package file

Copy `packages/portfolio.yaml` into `/config/packages/portfolio.yaml` on your HA instance.

Create the `packages` folder if it does not already exist, then ensure your
`configuration.yaml` contains:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

### 5. Copy the dashboard

Copy `lovelace/dashboard.yaml` into `/config/lovelace/dashboard.yaml`.

Add the following to your `configuration.yaml` under the `lovelace:` key
(create the key if it does not exist):

```yaml
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

Install the **apexcharts-card** HACS frontend card if you have not already — it powers
the portfolio value, VIX, alpha, and benchmark history charts.

### 6. Restart Home Assistant

**Settings → System → Restart** — required to load the package sensors.

### 7. Sync your portfolio from T212

Once the add-on is running, open the **Investment Monitor → Rebalance** dashboard tab
and press **Sync Holdings from T212**. This will:

- Pull your actual positions, quantities, and average prices from T212
- Replace the default holdings with your real portfolio
- Set each holding's `target_weight` to its current portfolio weight as a starting point
- Assign `group: global_beta` to any holdings not previously configured

After the sync:

1. Open the add-on **Configuration** tab in HA
2. Review the holdings list — find any with `group: global_beta` that belong elsewhere
3. Update the group to the appropriate value (see [ETF groups](#etf-groups) below)
4. Adjust `target_weight` values to reflect your intended allocation
5. **Save** the configuration

### 8. Trigger your first full snapshot

**Rebalance tab → Run Snapshot Now**, or:

**Developer Tools → Actions** → `rest_command.trigger_portfolio_collect` → **Perform action**

Watch the add-on **Log** tab. After ~30 seconds you should see:

```
Snapshot saved: £XXXXX  return=X.XX%  max_drift=X.X%  rebalance=False  vix=XX
```

Snapshots run automatically at **20:00 every weekday** from then on (after London market close).

---

## ETF groups

Holdings are assigned to one of five strategy groups. Each group has a target allocation;
the group system is used for the Holdings tab summary and can optionally drive individual
target weights (see `use_group_weights` below).

| Group key | Label | Default allocation | Purpose |
|---|---|:---:|---|
| `momentum_core` | Momentum Core | 25% | Alpha engine: high-momentum factor ETFs |
| `global_beta` | Global Beta | 40% | Broad market: global equity trackers |
| `regional_satellite` | Regional Satellite | 20% | Regional/country-specific ETFs |
| `defensive` | Defensive | 10% | Bonds, short-duration gilts |
| `optional_factor` | Optional Factor | 5% | Quality, minimum-volatility factor ETFs |

Assign each holding's group in the add-on **Configuration** tab using the `group` field.

---

## Holdings configuration

Each entry in the `holdings` list in the add-on Configuration tab:

| Field | Example | Description |
|---|---|---|
| `yahoo_symbol` | `VWRL.L` | Yahoo Finance ticker. Append `.L` for LSE, `.DE` for Xetra, no suffix for US |
| `t212_ticker` | `VWRL_EQ_XLON` | Trading 212 instrument ID. See [Finding T212 tickers](#finding-t212-tickers) |
| `target_weight` | `18.0` | Target allocation %. Does **not** need to sum to 100 — normalised automatically and rounded to whole numbers using largest-remainder method |
| `purchase_price` | `123.67` | Average cost basis per unit in GBP. Used for P&L display |
| `purchase_qty` | `7.28` | Units held. Used as fallback if T212 API is unreachable |
| `group` | `global_beta` | Strategy group. See [ETF groups](#etf-groups) above |

**Maximum 20 holdings.**

### Finding T212 tickers

The T212 ticker is not always obvious. Three ways to find it:

1. **T212 Sync** — use the **Sync Holdings from T212** button; the add-on fetches all tickers directly
2. **T212 API** — call `GET /api/v0/equity/portfolio` with your token; each position has a `ticker` field
3. **Common MIC codes** — the format is `SYMBOL_EQ_{MIC}` for equities:

| MIC | Exchange | Yahoo suffix |
|---|---|---|
| `XLON` | London Stock Exchange | `.L` |
| `XETA` | Xetra (Germany) | `.DE` |
| `XNAS` | NASDAQ | *(none)* |
| `XNYS` | NYSE | *(none)* |
| `XAMS` | Euronext Amsterdam | `.AS` |
| `XPAR` | Euronext Paris | `.PA` |

---

## Syncing from T212

The **Sync Holdings from T212** button (Rebalance tab) pulls your live portfolio from T212
and updates `options.json` without requiring any manual file editing.

| Scenario | What happens |
|---|---|
| **Existing holding** | `purchase_qty` and `purchase_price` updated from T212. `target_weight` and `group` preserved. |
| **New holding** (in T212, not in config) | Added automatically. `target_weight` set to actual current portfolio weight. `group` defaults to `global_beta` — edit in add-on options. |
| **Sold holding** (in config, absent from T212) | Removed from config so it no longer affects rebalancing. DB history is preserved. |

After a sync, open the add-on options to assign the correct group to any new holdings.
Valid group values: `momentum_core`, `global_beta`, `regional_satellite`, `defensive`, `optional_factor`.

Use **Preview T212 Sync** for a dry run that logs the diff without writing anything.

---

## Guard-rails configuration

| Option | Default | Description |
|---|---|---|
| `drift_threshold_pct` | `15` | Reference threshold shown in dashboard; integer-boundary rule governs actual trade decisions |
| `vix_high_threshold` | `25` | Above this VIX, requires ≥ 2 integer points of drift before trading |
| `vix_extreme_threshold` | `35` | Above this VIX, all rebalancing is frozen |
| `min_days_between_rebalance` | `21` | Cooldown in days between executed rebalances |
| `use_group_weights` | `false` | If `true`, individual target weights are derived from group allocations (equal split within each group) instead of per-holding `target_weight` values |
| `max_cvar_pct` | `5.0` | Historical CVaR (tail-risk) limit in %. Non-defensive holdings are scaled back if portfolio tail risk exceeds this. Set to `0` to disable |
| `cost_rate_pct` | `0.1` | Round-trip transaction cost rate (%). Trades where expected drift-correction benefit ≤ estimated cost are skipped |

### Rebalance trigger rules

| Market condition | Required drift to trade |
|---|---|
| Normal (VIX ≤ 25) | Any holding ≥ 1 whole percentage point from integer target |
| Elevated (VIX 25–35) | Any holding ≥ 2 whole percentage points from target |
| Extreme (VIX > 35) | Frozen — no trades suggested |

---

## Rebalance approval flow

1. Snapshot runs — if drift + VIX conditions are met, `sensor.rebalance_signal` becomes `1`
2. A persistent HA notification appears with the reason and trade plan
3. Open the **Rebalance** dashboard tab to review the suggested trades
4. Use **Approve — Dry Run** to log the approval without placing any orders
5. When confident: enable **⚠ Live Trading Mode** → toggle **Approve Trades**
6. Orders are submitted to T212; the approval toggle resets automatically

> **Live mode is off by default and must be explicitly enabled each time.**
> Demo mode submits to the T212 paper-trading environment — trades appear in your
> demo account but involve no real money.

### If a rebalance fails

If T212 rejects orders (check the add-on logs for error details), the 21-day cooldown
timer will have started even though no trades executed. Use the **Reset Rebalance Cooldown**
button on the Rebalance tab to clear it so you can retry immediately after fixing the issue.

---

## Dashboard views

The dashboard is responsive — two columns on desktop, single column on mobile.

| View | Contents |
|---|---|
| **Overview** | Portfolio value (90d chart), total return, free cash, VIX regime, benchmark returns since purchase, alpha vs S&P 500 and MSCI World, snapshot freshness |
| **Holdings** | Group allocation summary (target vs actual with colour-coded delta), full holdings table (group, symbol, actual %, target %, drift %, P&L %, value), relative drift bar chart, momentum scores (3m / 6m / 9m / 12m + WMA trend signal) |
| **Rebalance** | Signal status, rebalance reason, suggested trade plan (base target vs momentum-adjusted target, delta £ and units), approval controls, sync and snapshot buttons |
| **Benchmarks** | 90d return history chart, alpha history (vs S&P 500 and MSCI World), returns since purchase, 3M snapshot |

---

## Currency handling

- **LSE holdings** (`.L` suffix) — Yahoo Finance returns prices in pence (GBp).
  The add-on divides by 100 to convert to pounds. T212 API prices (in GBP) are preferred for current valuation.
- **Xetra holdings** (`.DE` suffix) — Yahoo Finance returns prices in EUR.
  The add-on fetches the live EUR/GBP rate (`EURGBP=X`) and converts automatically.
- **US holdings** — assumed to be in USD. T212 converts to GBP when reporting `currentPrice`.

---

## API reference

The add-on exposes a local REST API on port 8000.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness check; returns version, T212 base URL, holdings count |
| `GET` | `/api/latest-snapshot` | Latest snapshot (consumed by HA REST sensors) |
| `GET` | `/api/snapshots?limit=90` | Snapshot history |
| `GET` | `/api/benchmarks?days=90` | Benchmark history |
| `POST` | `/api/collect` | Run a full snapshot now |
| `POST` | `/api/approve/{as_of}` | Approve rebalance; `?execute=true` places live orders |
| `POST` | `/api/reset-cooldown` | Clear the rebalance cooldown (use after a failed execution) |
| `POST` | `/api/sync-from-t212` | Sync holdings from live T212 portfolio; writes `options.json` |
| `POST` | `/api/sync-from-t212?preview=true` | Dry-run sync — returns diff without writing |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `404 No snapshots yet` | First run — nothing collected yet | Use **Run Snapshot Now** button or POST `/api/collect` |
| Sensors show `Unavailable` | Package not loaded or HA not restarted | Check `packages/portfolio.yaml` is in place; restart HA |
| `JSONDecodeError` from Yahoo Finance | Rate limiting | Add-on auto-retries per ticker; wait a few minutes and try again |
| `T212 portfolio fetch failed` | Wrong token or base URL | Check the t212_token and t212_base in the Configuration tab |
| T212 order rejected 400 | Insufficient funds or invalid quantity | Check add-on logs for the T212 error body; use Reset Cooldown after fixing |
| All holdings show as `global_beta` | Holdings saved before v1.4.0 have no `group` field | Run **Sync Holdings from T212** or manually assign groups in the Configuration tab |
| Dashboard `Configuration error` on charts | HACS `apexcharts-card` not installed | Install via HACS Frontend |
| Portfolio value wildly wrong | Stale pence-denominated data in HA recorder | Run `recorder.purge_entities` on `sensor.portfolio_value` then trigger a fresh snapshot |
| Rebalance cooldown stuck after failed trades | `executed=1` flag set even on T212 rejection | Press **Reset Rebalance Cooldown** on the Rebalance tab |
| New holdings after T212 sync show wrong group | Auto-assigned `global_beta` as placeholder | Open add-on options, update the `group` field, save |
| Add-on fails to build | Docker Hub unreachable | Check HA internet connectivity; try rebuild after a few minutes |

---

## Swapping an ETF

To replace one ETF with another (e.g. `XWEM.DE` → `IEFM.L`):

1. **Sell the old ETF** in your T212 account
2. **Buy the new ETF** in T212
3. Press **Sync Holdings from T212** on the Rebalance dashboard tab
   - The old ETF (now zero balance) will be removed from config
   - The new ETF will be added with its current portfolio weight as the default `target_weight`
4. Open the add-on **Configuration** tab and assign the correct `group` to the new holding
5. Adjust `target_weight` if needed, then **Save**
6. Press **Run Snapshot Now** to refresh

---

## Phased weight adjustment

As your investment goals change, update `target_weight` values in the Configuration tab.
Example progression:

| Phase | Momentum ETFs | Global Beta | Bonds | Optional Factor |
|---|:---:|:---:|:---:|:---:|
| Momentum-Max | 30% | 40% | 20% | 10% |
| Balanced Growth | 25% | 40% | 25% | 10% |
| Pre-Retirement | 15% | 35% | 40% | 10% |

Weights do not need to sum to 100% — the add-on normalises and rounds them automatically.

---

## Data & privacy

- All portfolio data is stored **locally** in `/data/portfolio.db` (SQLite)
- No data is sent to any third party except:
  - **Trading 212** — your own account API (read positions, place orders)
  - **Yahoo Finance** — public price data, no account required
- `options.json` is updated in-place by the T212 sync feature; no external service is involved
- To reset history: stop the add-on, delete `/data/portfolio.db` via SSH or the HA terminal

---

## Disclaimer

This project is provided for **educational and personal interest purposes only**.

- I am not a financial advisor
- Nothing here is investment advice
- Past performance of any strategy shown is not indicative of future results
- You are solely responsible for any investment decisions you make
- Always do your own research before investing

The project defaults to **demo mode** so you can explore it safely before connecting
any real account. Take your time to understand what it does before enabling live trading.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## Licence

MIT — do whatever you like with it. If you find it useful or improve it, sharing back is
always appreciated but never required. Enjoy! 🎉
