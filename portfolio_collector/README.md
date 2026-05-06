# Portfolio Collector

Monitor a Trading 212 portfolio from Home Assistant. Live T212 holdings, drift detection,
momentum scoring, benchmark comparison, and rebalance suggestions — all running locally,
all under manual approval, demo-first.

> **Not financial advice.** Hobby project. Defaults to T212 demo (paper trading). See the
> [full README](https://github.com/reidyroo/ha-portfolio-collector) for installation,
> configuration, diagnostics, recovery procedures, and the disclaimer.

## Quick start

1. Generate a T212 API token (Key ID + Secret → Base64-encoded `keyId:secret`)
2. Configuration tab: paste into `t212_token`, leave `t212_base` as the demo URL
3. **Save → Start**
4. Deploy the HA package + dashboard (curl commands in the full README)
5. **Restart Home Assistant Core**
6. Run a snapshot, then **Sync T212 Weights → Targets**, then run another snapshot

After that you have a steady-state dashboard that shows your portfolio with ~0% drift
relative to your T212 actuals, with phase guard-rails (CVaR, cooldown, VIX threshold)
governing when rebalances are suggested.

## What it does

- Fetches live T212 positions, prices, quantities every snapshot (no manual holdings list)
- Caches the T212 instrument catalog (~16 000 entries) in SQLite for symbol resolution
- Resolves Yahoo Finance symbols correctly for ISA compact tickers (`VWRLl_EQ` → `VWRL.L`)
  and bare `_EQ` tickers (`IITU_EQ` → `IITU.L`)
- Stores group assignments in SQLite, edited via a built-in web UI at `/groups`
- Two target-weight modes:
  - **T212 actuals** (`use_group_weights: false`) — drift relative to your real portfolio
  - **Phase group allocations** (`use_group_weights: true`) — drift relative to a target shape using
    risk/CVaR-driven dynamic group-weight optimisation
- Four phase presets bundling CVaR / cost / cooldown / VIX guard-rails
- Validator + auto-recovery prevents bad targets from ever being written to a snapshot
- One-click "Sync T212 Weights → Targets" to reset the drift baseline to current actuals
- Trade plan: momentum-tilted, CVaR-constrained, cost-filtered, self-funding, manual approval

## Health check

```
curl http://<HA-IP>:8000/api/health
```

Returns the running version, phase, catalog age, and unassigned instrument count.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/groups` | Web UI for group assignment |
| `GET` | `/api/health` | Version + status |
| `GET` | `/api/latest-snapshot` | Latest stored snapshot |
| `POST` | `/api/collect` | Run snapshot now |
| `POST` | `/api/sync-t212-weights` | Reset target weights to current T212 actuals |
| `POST` | `/api/set-phase` | Body `{"phase":"Momentum-Chill"}` |
| `GET` | `/api/last-good-targets` | Inspect validator recovery baseline |

Full API reference in the [GitHub README](https://github.com/reidyroo/ha-portfolio-collector).

## Safety

- Live trading mode is **off by default** and resets after each session
- Every trade requires explicit approval via the dashboard
- Demo mode (`https://demo.trading212.com`) submits to the T212 paper account — no real money
- All data stored locally in `/data/portfolio.db` — survives uninstall/reinstall
