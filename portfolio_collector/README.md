# Portfolio Collector

Monitor a Trading 212 investment portfolio from Home Assistant. Tracks up to 20 holdings,
benchmarks against major indices, scores momentum, and suggests rebalance trades for
manual approval — all running locally on your HA hardware.

> **Not financial advice.** This is a hobby project. Demo mode is on by default.
> See the [full README](https://github.com/reidyroo/ha-portfolio-collector)
> for installation instructions, configuration reference, and the disclaimer.

## Quick start

1. Set `t212_token` to your Trading 212 API key (T212 → Settings → API)
2. Leave `t212_base` as the demo URL until you are ready
3. Edit the `holdings` list to match your portfolio
4. Save → Start → trigger your first snapshot from the HA dashboard

Full documentation: <https://github.com/reidyroo/ha-portfolio-collector>
