# Dashboard Updates — May 6, 2026

## Summary of Changes

This update focuses on dashboard clarity, data hygiene, and visibility into dynamic market-driven allocations.

---

## 1. Overview Tab Cleanup

**Removed:** The version/phase/weight-mode summary card that was cluttering the top of the Overview tab.

**Result:** The Overview tab now opens cleanly with just:
- Portfolio Summary (Value, Return %, Cash, VIX)
- Market Regime indicator
- Portfolio Value 90-day chart
- Benchmark returns
- Alpha vs benchmarks
- VIX history
- Snapshot freshness

This keeps the focus on portfolio health and performance.

---

## 2. Data Spike Removal & Cleanup Guidance

**Added:** A helpful reference card on the Overview tab documenting how to remove erroneous snapshots that skew 90-day charts.

**How to clean up spikes:**

```bash
# Remove April 28 spike from portfolio value chart
curl -X DELETE "http://localhost:8000/api/snapshots?date=2026-04-28"

# Remove April 20 spike from alpha history chart
curl -X DELETE "http://localhost:8000/api/snapshots?date=2026-04-20"
```

**Alternative filters:**
```bash
# Delete before a specific date
curl -X DELETE "http://localhost:8000/api/snapshots?before=2026-04-15"

# Delete after a specific date
curl -X DELETE "http://localhost:8000/api/snapshots?after=2026-05-01"

# Delete by portfolio value range (e.g., rogue spikes)
curl -X DELETE "http://localhost:8000/api/snapshots?max_value=50000"
curl -X DELETE "http://localhost:8000/api/snapshots?min_value=10000"

# Combine filters with &
curl -X DELETE "http://localhost:8000/api/snapshots?after=2026-04-15&max_value=50000"
```

After cleanup, refresh your browser to see the corrected 90-day history.

---

## 3. Dynamic Group Allocations View

**Added:** A new "Calculated Group Allocations" card on the Rebalance tab showing real-time market-driven group weights.

**Features:**

### When Dynamic Mode is Active

The card displays:

1. **Current Effective Risk Level** (0–100)
   - Shows the user's base risk_score plus any auto-adjustments from market conditions
   - Includes the reason for any shifts (e.g., "VIX=32.1(+5.0); DD=-5.2%(-2.3)")

2. **Live Group Allocations** with signal adjustments:
   - **Momentum Core** — Growth exposure, adjusted by VIX spikes, drawdowns, and 21-day rally strength
   - **Global Beta** — Broad diversification baseline
   - **Regional Satellite** — Tactical positioning
   - **Defensive** — Drawdown protection
   - **Optional Factor** — Alternative factors or tactical tilts

3. **Total allocation** always sums to 100% (verification)

### How It Works

The allocations shown are interpolated along your risk axis:
- **Low risk (0–30):** Heavy defensive weighting, minimal momentum
- **Moderate risk (40–60):** Balanced growth and protection
- **High risk (70–100):** Growth-tilted, minimal defensive

If `auto_adjust_enabled: true`, additional signal-driven shifts apply:
- **Defensive:** VIX spikes or large drawdowns pull allocations toward defensive
- **Aggressive (bidirectional mode):** Strong 21-day rallies can push toward growth

### Example Interpretation

```
Effective Risk: 68.5/100 (Chill; VIX=24.2(-1.5))

Momentum Core     32.1%   Growth exposure adjusted by VIX, drawdown, rally signals
Global Beta       27.6%   Broad diversification baseline
Regional Satellite 22.4%  Tactical positioning
Defensive         14.2%   Drawdown protection
Optional Factor    3.7%   Alt factors or tactical
Total             100.0%
```

This tells you:
- Your base risk (65, "Chill") has shifted up slightly to 68.5 because VIX is relatively quiet
- The `momentum_core` allocation expanded by 2–3 points relative to the base preset
- Within `momentum_core` itself, signal-driven tilting happens at the individual ticker level (inside the group)

---

## 4. Configuration Workflow Example

### Scenario: High-Risk Growth Environment (SMGB + Momentum ETFs Surging)

**Current Settings:**
- Phase: Momentum-Chill (base risk ≈ 65)
- Weight mode: dynamic
- VIX: 18 (low)
- Drawdown: 0% (near recent peak)
- 21-day rally: +6.5%

**What You'll See:**
```
Effective Risk: 72.1/100 (Chill; Rally21d=+6.5%(+5.0))

Momentum Core     38%     Boosted for growth capture
Global Beta       25%
Regional Satellite 22%
Defensive         12%     Trimmed
Optional Factor   3%
```

**Action:** The system automatically over-exposes growth (Momentum Core from 30% → 38%) while trimming defensive (16% → 12%), channeling freed capital back to underweight momentum tickers.

**Next Snapshot:** Trade plan will reflect these adjusted allocations without you needing to manually change settings.

### Scenario: Defensive Downturn (VIX Spike, DD > -10%)

**Current Settings:**
- Phase: Momentum-Chill (base risk ≈ 65)
- VIX: 35 (elevated)
- Drawdown: -12% (below previous peak)

**What You'll See:**
```
Effective Risk: 48.2/100 (Chill; VIX=35.0(-15.0); DD=-12.0%(-8.5))

Momentum Core     20%     Defensive tilt active
Global Beta       26%
Regional Satellite 18%
Defensive         30%     Boosted for protection
Optional Factor   6%
```

**Action:** Allocations automatically flip defensive. Next rebalance will reduce momentum holdings and reinvest in defensive instruments like bonds, treasuries, and counter-cyclical hedges.

---

## 5. Technical Notes

### API Endpoints for Data Cleanup

All snapshot deletion is done via:
```
DELETE /api/snapshots
```

Query parameters (any combination):
- `date=YYYY-MM-DD` — exact date match
- `before=YYYY-MM-DD` — strictly before
- `after=YYYY-MM-DD` — strictly after
- `min_value=N` — portfolio value < N
- `max_value=N` — portfolio value > N

At least one filter must be supplied.

### Dashboard Sensors

The "Calculated Group Allocations" card reads from:
- `sensor.portfolio_value` attributes:
  - `weight_mode` — must equal `"dynamic"`
  - `effective_risk` — the calculated risk after auto-adjustments
  - `effective_risk_reason` — human-readable cause (VIX, drawdown, rally, etc.)
  - `dynamic_group_allocations` — dict of {group: allocation_pct}

These attributes are populated every snapshot when `weight_mode="dynamic"`.

---

## 6. User Workflow

### For High-Risk Scenarios (SMGB / Momentum Surge)

1. **Note the conditions:** VIX is low, portfolio rallying, momentum ETFs strong
2. **Set risk higher** (or wait for auto-adjust to kick in if enabled):
   - Either drag `input_number.portfolio_risk_score` slider toward 90
   - Or enable `auto_adjust_enabled: true` to let market signals do it
3. **Check the Allocations card** → see Momentum Core expand, Defensive trim
4. **Run a snapshot** → trade plan will over-weight momentum holdings
5. **Approve & execute** → portfolio rebalances toward growth

### For Defensive Scenarios (VIX Spike / Drawdown)

1. **Market warning signs:** VIX > 30, drawdown > -10%
2. **Check the Allocations card** → see Defensive expand, Momentum trim
3. **Run a snapshot** → trade plan will shift to defensive
4. **Use cooldown override if needed:** Click **Notch Up** to bypass cooldown and fire immediately
5. **Approve & execute** → portfolio locks in defensive positioning

### For Data Hygiene

1. **Spot erroneous snapshots:** Chart shows a clear spike/dip that doesn't match your activity
2. **Delete the bad date(s):** Use the curl command from the Overview card's cleanup guidance
3. **Refresh browser** → 90-day history updates, spike is gone

---

## Files Modified

- `lovelace/dashboard.yaml`
  - Removed version/phase summary card from Overview
  - Added "Calculated Group Allocations" card to Rebalance tab
  - Added data cleanup guidance card to Overview

---

## Next Steps

- **Monitor the dynamic allocations** over the next few market cycles
- **Experiment with risk settings** and observe how allocations shift in real-time
- **Use cooldown overrides** during sharp market moves to react faster
- **Keep spiky data cleaned** using the curl commands provided for accurate 90-day analysis

---

**Version:** Portfolio Collector v2.7.3  
**Commit:** 9c588e1  
**Date:** May 6, 2026
