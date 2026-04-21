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
- **Benchmark comparison** — MSCI World, S&P 500, FTSE 100, DOW, VIX
- **Drift detection** — integer-boundary rule: only flags holdings that have moved ≥ 2
  whole percentage points from their target (single-tick noise never fires)
- **Momentum scoring** — 3m / 6m / 12m price momentum + EMA 20/50 trend per holding
- **Rebalance suggestions** — momentum-adjusted trade plan, self-funding (buys ≈ sells),
  generated only when drift + VIX conditions are met; always requires manual approval
- **VIX regime filter** — elevated (>25): requires ≥ 3pt gap; extreme (>35): frozen
- **Configurable via the HA UI** — tickers, weights, and guard-rails without editing files
- **Demo-first** — defaults to T212 demo; live trading requires explicit opt-in each session
- **4-view Lovelace dashboard** — Overview, Holdings & Drift, Rebalance, Benchmarks

---

## Requirements

| Requirement | Notes |
|---|---|
| Home Assistant OS | Tested on HA Green (aarch64). Should work on any HAOS hardware. |
| Trading 212 account | Free account; demo API key from T212 Settings → API |
| Internet access from HA | Yahoo Finance prices fetched daily |
| HACS (optional) | For `apexcharts-card` and `mini-graph-card` dashboard cards |

---

## Installation

### 1. Add this repository to Home Assistant

In Home Assistant:

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
> The token only needs to be stored in the HA add-on configuration (which is local
> to your HA instance and not exposed by this project).

### 3. Install and configure

1. Click **Portfolio Collector → Install** (first build: 3–5 minutes)
2. Go to the **Configuration** tab
3. Paste the Base64 token generated above into `t212_token`
4. Leave `t212_base` as `https://demo.trading212.com` until you are confident
5. Edit the `holdings` list to match your portfolio (see [Holdings configuration](#holdings-configuration))
6. **Save → Start**

### 3. Copy the HA package file

Copy `packages/portfolio.yaml` into `/config/packages/portfolio.yaml` on your HA instance.

Create the `packages` folder if it does not already exist, then ensure your
`configuration.yaml` contains:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

### 4. Copy the dashboard

Copy `lovelace/dashboard.yaml` into `/config/lovelace/dashboard.yaml`.

Add the following to your `configuration.yaml` under the `lovelace:` key
(create the key if it does not exist):

```yaml
lovelace:
  mode: storage
  resources:
    - url: /hacsfiles/apexcharts-card/apexcharts-card.js
      type: module
    - url: /hacsfiles/mini-graph-card/mini-graph-card-bundle.js
      type: module
  dashboards:
    lovelace-portfolio:
      mode: yaml
      title: Investment Monitor
      icon: mdi:chart-line
      show_in_sidebar: true
      filename: lovelace/dashboard.yaml
```

Install these HACS frontend cards if you have not already:
- **apexcharts-card** — portfolio value and benchmark charts
- **mini-graph-card** — VIX sparkline

### 5. Restart Home Assistant

**Settings → System → Restart** — required to load the package sensors.

### 6. Trigger your first snapshot

**Developer Tools → Actions** → search `rest_command.trigger_portfolio_collect` → **Perform action**

Watch the add-on **Log** tab. After ~30 seconds you should see:

```
Snapshot saved: £XXXXX  return=X.XX%  max_drift=X.X%  rebalance=False  vix=XX
```

From then on, snapshots run automatically at **20:00 every weekday** (after London market close).

---

## Holdings configuration

Each entry in the `holdings` list in the add-on Configuration tab:

| Field | Example | Description |
|---|---|---|
| `yahoo_symbol` | `VWRL.L` | Yahoo Finance ticker. Append `.L` for LSE, `.DE` for Xetra, no suffix for US |
| `t212_ticker` | `VWRL_EQ_XLON` | Trading 212 instrument ID. See [Finding T212 tickers](#finding-t212-tickers) |
| `target_weight` | `18.0` | Target allocation %. Does **not** need to sum to 100 — normalised automatically, then rounded to whole numbers |
| `purchase_price` | `123.67` | Average cost basis per unit in GBP. Used for P&L display |
| `purchase_qty` | `7.28` | Units held. Used as fallback if T212 API is unreachable |

**Maximum 20 holdings.** The add-on normalises weights and rounds them to whole integers using
the largest-remainder method, so totals always sum to exactly 100%.

### Finding T212 tickers

The T212 ticker is not always obvious. Three ways to find it:

1. **T212 API** — call `GET /api/v0/equity/portfolio` with your token; each position has a `ticker` field.
2. **T212 app** — search the instrument; the ticker shown in instrument details is the base symbol.
   Append `_EQ_{MIC}` where MIC is the exchange code.
3. **Common MIC codes:**

| MIC | Exchange |
|---|---|
| `XLON` | London Stock Exchange |
| `XETA` | Xetra (Germany) |
| `XNAS` | NASDAQ |
| `XNYS` | NYSE |
| `XAMS` | Euronext Amsterdam |

---

## Guard-rails configuration

| Option | Default | Description |
|---|---|---|
| `drift_threshold_pct` | `15` | Kept for reference display; integer-boundary rule is used for trade decisions |
| `vix_high_threshold` | `25` | Above this VIX, requires ≥ 3 integer points of drift |
| `vix_extreme_threshold` | `35` | Above this VIX, all rebalancing is frozen |
| `min_days_between_rebalance` | `21` | Cooldown in days between executed rebalances |

### Rebalance trigger rules

| Market condition | Trigger |
|---|---|
| Normal (VIX ≤ 25) | Any holding ≥ 2 whole percentage points from integer target |
| Elevated (VIX 25–35) | Any holding ≥ 3 whole percentage points from target |
| Extreme (VIX > 35) | Frozen — no trades suggested |

---

## Rebalance approval flow

1. Snapshot runs — if drift + VIX conditions are met, `sensor.rebalance_signal` becomes `1`
2. A persistent HA notification appears with the reason and trade plan
3. Open the **Rebalance** dashboard view to review the suggested trades
4. Toggle **Approve — Dry Run** to log the approval without placing any orders
5. When confident: enable **⚠ Live Trading Mode** → toggle **Approve Trades**
6. Orders are submitted to T212; the approval toggle resets automatically

> **Live mode is off by default and must be explicitly enabled each time.**
> Demo mode submits to the T212 paper-trading environment — trades appear in your
> demo account but involve no real money.

---

## Dashboard views

| View | Contents |
|---|---|
| **Overview** | Portfolio value (90d chart), total return, VIX regime, benchmark 1M returns, alpha vs S&P 500 and MSCI World |
| **Holdings** | Full holdings table (actual %, integer target %, drift, P&L, value), drift bar chart, momentum scores |
| **Rebalance** | Signal status, rebalance reason, trade plan (base target vs momentum-adjusted target), approval controls |
| **Benchmarks** | 90d return history chart, alpha history, 3M benchmark snapshot |

---

## Currency handling

- **LSE holdings** (`.L` suffix) — Yahoo Finance returns prices in pence (GBp).
  The add-on divides by 100 to convert to pounds.
- **Xetra holdings** (`.DE` suffix) — Yahoo Finance returns prices in EUR.
  The add-on fetches the live EUR/GBP rate and converts.
- **T212 API prices** — always in your account currency (GBP). The add-on prefers
  T212 prices over Yahoo for current valuation; Yahoo is used for historical momentum.

---

## API reference

The add-on exposes a local REST API on port 8000.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Liveness check; returns version, T212 base, holdings count |
| `GET` | `/api/latest-snapshot` | Latest snapshot (consumed by HA REST sensors) |
| `GET` | `/api/snapshots?limit=90` | Snapshot history |
| `GET` | `/api/benchmarks?days=90` | Benchmark history |
| `POST` | `/api/collect` | Run a snapshot now |
| `POST` | `/api/approve/{as_of}` | Approve rebalance; `?execute=true` places orders |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `404 No snapshots yet` | First run — nothing collected yet | POST `/api/collect` or use the HA dashboard button |
| Sensors show `Unavailable` | Package not loaded or HA not restarted | Check `packages/portfolio.yaml` is in place; restart HA |
| `JSONDecodeError` from Yahoo Finance | Rate limiting | Add-on auto-retries per ticker; try again in a few minutes |
| `T212 portfolio fetch failed` | Wrong token or wrong base URL | Check your T212 API key in the Configuration tab |
| Dashboard `Configuration error` | HACS cards not installed | Install `apexcharts-card` and `mini-graph-card` via HACS |
| Portfolio value wildly wrong | Old inflated data in recorder | Run `recorder.purge_entities` on `sensor.portfolio_value` |
| Add-on fails to build | Docker Hub unreachable | Check HA internet connectivity; try rebuild after a few minutes |

---

## Phased weight adjustment

As your investment goals change, update `target_weight` values in the Configuration tab.
Example progression:

| Phase | Momentum ETFs | Bonds | Quality/MinVol |
|---|---|---|---|
| Momentum-Max | 32% | 16% | 4% |
| Balanced Growth | 26% | 22% | 6% |
| Pre-Retirement | 18% | 32% | 10% |

Weights do not need to sum to 100% — the add-on normalises them automatically.

---

## Data & privacy

- All portfolio data is stored **locally** in `/data/portfolio.db` (SQLite)
- No data is sent to any third party except:
  - **Trading 212** — your own account API
  - **Yahoo Finance** — public price data, no account required
- The database is preserved across add-on updates and restarts
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

See [portfolio_collector/CHANGELOG.md](portfolio_collector/CHANGELOG.md).

---

## Licence

MIT — do whatever you like with it. If you find it useful or improve it, sharing back is
always appreciated but never required. Enjoy! 🎉
