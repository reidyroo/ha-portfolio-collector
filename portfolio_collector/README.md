# Portfolio Collector

Monitor a Trading 212 investment portfolio from Home Assistant. Tracks up to 20 holdings,
benchmarks against major indices, scores momentum, and suggests rebalance trades for
manual approval — all running locally on your HA hardware.

> **Not financial advice.** This is a hobby project. Demo mode is on by default.
> See the [full README](https://github.com/reidyroo/ha-portfolio-collector)
> for installation instructions, configuration reference, and the disclaimer.

## Quick start

1. Set `t212_token` to your Trading 212 API key (T212 → Settings → API)
2. Leave `t212_base` as the demo URL until you are ready for live trading
3. **Save → Start**
4. Open the **Investment Monitor → Rebalance** dashboard tab
5. Press **Sync Holdings from T212** to pull your real portfolio automatically
6. Review the synced holdings in the **Configuration** tab — assign the correct `group`
   to any newly discovered holdings (see valid values below)
7. Press **Run Snapshot Now** to collect your first full snapshot

## ETF group values

Assign one of these to the `group` field for each holding:

| Value | Label | Typical holdings |
|---|---|---|
| `momentum_core` | Momentum Core | High-momentum factor ETFs |
| `global_beta` | Global Beta | Broad global equity trackers |
| `regional_satellite` | Regional Satellite | Country / regional ETFs |
| `defensive` | Defensive | Bonds, short-duration gilts |
| `optional_factor` | Optional Factor | Quality, minimum-volatility ETFs |

Full documentation: <https://github.com/reidyroo/ha-portfolio-collector>
