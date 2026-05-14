# DriftPilot Daily Operations Playbook

> Lessons from May 12–13 paper trading. Every checklist item maps to a real
> incident that cost money or wasted compute.

Updated: 2026-05-14

---

## Pre-Market Checklist (before 9:25 ET)

### 1. Verify Qwen/DGX is reachable

**What went wrong:** On May 13 the Qwen server was intermittently unreachable.
79 enrichment failures and 54 agent timeouts. Every timeout fell through to
`fallback_action=approve`, meaning the PM agent rubber-stamped entries it
should have evaluated. On May 14 morning, all catalyst events landed as
`neutral` due to connection failure — the sentiment gate would have blocked
every signal all day.

**Check:**
```bash
curl -s --connect-timeout 5 http://192.168.1.166:8000/v1/models | python3 -m json.tool
```

**If it fails:**
- SSH to DGX: `ssh 192.168.1.166` (uses NVIDIA Sync key via ~/.ssh/config)
- Check vLLM process: `ps aux | grep vllm`
- Check GPU: `nvidia-smi`
- Restart if needed: `cd ~/vllm-env && nohup vllm serve Qwen/Qwen3-8B --host 0.0.0.0 --port 8000 --gpu-memory-utilization 0.90 &`
- Re-run enrichment after fixing (see step 3)

**Why it matters:** Without Qwen, all events get `sentiment=neutral`. The
`require_sentiment=positive` gate blocks every signal. Zero trades all day.
Worse: agent decisions fall back to `approve`, bypassing the PM risk check.

---

### 2. Kill stale processes from yesterday

**What went wrong:** On May 13, the operator restarted 7 times. Each restart
left RESERVED slots that blocked new allocations until the stale-slot recycler
(added in `edd61c4`) cleaned them up after 10 minutes. That's 10 minutes of
dead time per restart = ~70 minutes of missed opportunities.

**Check:**
```bash
# Kill any leftover PIDs
for f in logs/operator.pid logs/slot_manager.pid logs/catalyst_refresh.pid; do
    [ -f "$f" ] && kill $(cat "$f") 2>/dev/null; rm -f "$f"
done
# Verify port 8000 (dashboard) is free or restart it
lsof -ti :8000 | xargs kill 2>/dev/null
```

**Why it matters:** Stale operator processes hold DB locks (WAL mode mitigates
reads but not writes). Stale RESERVED slots block capital allocation.

---

### 3. Pre-warm catalyst DB and verify enrichment

**What went wrong:** On May 13, the enricher default pointed at the wrong
database (`catalyst_events_2024.sqlite3`). All enrichment went to the
archived DB. Live events stayed unenriched. Fixed in `edd61c4` but the
fallback pattern remains dangerous: if enrichment fails silently, events
get stamped `neutral` and the enricher considers them "done."

**Steps:**
```bash
# Load 2 weeks of news
START=$(TZ=America/New_York date -v-14d +%Y-%m-%d)
END=$(TZ=America/New_York date +%Y-%m-%d)
.venv/bin/python scripts/load_2024_catalyst_events.py \
    --start "$START" --end "$END" \
    --output data/driftpilot/catalyst_events.sqlite3

# Enrich with Qwen sentiment
.venv/bin/python scripts/enrich_catalyst_events.py \
    --db data/driftpilot/catalyst_events.sqlite3 \
    --priority-only --concurrency 32
```

**Verify enrichment actually worked (don't trust "fully enriched"):**
```bash
.venv/bin/python3 -c "
import sqlite3
db = sqlite3.connect('data/driftpilot/catalyst_events.sqlite3')
for row in db.execute('SELECT sentiment, COUNT(*) FROM catalyst_events WHERE event_ts > datetime(\"now\", \"-4 hours\") GROUP BY sentiment'):
    print(f'  {str(row[0]):12} {row[1]}')
"
```

**Red flag:** If you see 100% neutral for recent events, enrichment failed
silently. Reset and re-enrich:
```bash
.venv/bin/python3 -c "
import sqlite3
db = sqlite3.connect('data/driftpilot/catalyst_events.sqlite3')
db.execute(\"\"\"UPDATE catalyst_events SET sentiment=NULL, qwen_response_json=NULL
    WHERE sentiment='neutral' AND qwen_response_json IS NULL
    AND event_ts > datetime('now','-6 hours')\"\"\")
db.commit()
"
# Then re-run enrich_catalyst_events.py
```

**Why it matters:** May 14 morning: 65 events enriched as neutral (all
fallback). After fixing Qwen connectivity and re-enriching, 3 earnings
beats (VSNT, WWW, YETI) correctly classified as positive. Without this
step, the operator would have had zero tradable signals at open.

---

### 4. Verify runtime config is sane

**What went wrong:** On May 12–13, several config values were incorrect:
- `stop_loss_pct=1.5%` vs `profit_take_pct=1.0%` (asymmetric risk, defect #2)
- `trailing_distance_pct=2.0%` > activation 1.0% (trailing stop could never trigger, defect #3)
- `max_trades_per_symbol_per_day=5` (ORCL lost money on 5/5 trades)
- `analyst_target_raise_v1` was active (negative EV signal, defect #10)

**Check:**
```bash
cat data/driftpilot/runtime_config.json
```

**Expected values (validated May 14):**
| Key | Value | Why |
|-----|-------|-----|
| `active_signal` | `earnings_report_v1,filing_8a_v1` | Only positive-EV signals |
| `earnings_stop_loss_pct` | 1.0 | Symmetric with profit_take |
| `earnings_profit_take_pct` | 1.0 | |
| `earnings_trailing_distance_pct` | 0.4 | Below activation (1.0%) so it can trigger |
| `max_trades_per_symbol_per_day` | 3 | Prevents machine-gunning |
| `min_reentry_minutes` | 15 | Cooldown after exit |
| `earnings_max_hold_minutes` | 45 | Prevents zombies |
| `slot_value` | 1000 | Paper position size |
| `max_price_drift_pct` | 3.0 | Reject stale-priced entries |

---

### 5. Verify dashboard is serving clean code

**What went wrong:** On May 14 morning, the dashboard was still running
the old pre-cleanup code from a process started May 13. After we deleted
74 legacy files and cleaned app.py, the running dashboard was stale.

**Check:**
```bash
curl -s http://127.0.0.1:8000/api/health
# Should return: {"ok":true,"service":"driftpilot-dashboard"}
```

**If stale, restart:**
```bash
lsof -ti :8000 | xargs kill 2>/dev/null
PYTHONPATH=src .venv/bin/python -m uvicorn trading_bot.dashboard.app:app \
    --host 127.0.0.1 --port 8000 >> logs/dashboard.log 2>&1 &
```

---

## Launch Sequence

Run `bash scripts/daily_operator.sh` or manually:

```bash
# 1. Dashboard (if not already running)
PYTHONPATH=src .venv/bin/python -m uvicorn trading_bot.dashboard.app:app \
    --host 127.0.0.1 --port 8000 >> logs/dashboard.log 2>&1 &

# 2. Operator (with defect guardrails)
export ACTIVE_SIGNAL="earnings_report_v1,filing_8a_v1"
export MAX_TRADES_PER_SYMBOL_PER_DAY=3
export MAX_HOLD_MINUTES=45
export DAILY_LOSS_LIMIT_PCT=0.03
export SCAN_INTERVAL_SECONDS=30
.venv/bin/python -u -m driftpilot.operator --paper-live >> logs/operator_$(date +%Y%m%d).log 2>&1 &

# 3. Slot manager
.venv/bin/python -u scripts/slot_manager.py --daemon --interval 60 >> logs/slot_manager.log 2>&1 &

# 4. Catalyst refresh loop (every 90 min)
bash scripts/midday_catalyst_refresh.sh --loop 5400 &
```

**Post-launch verify (within 60s):**
```bash
curl -s http://127.0.0.1:8000/api/operator/state | python3 -m json.tool | head -10
# Confirm: state, mode=PAPER, equity > 0
```

---

## Mid-Day Monitoring

### Watch for Qwen timeouts
```bash
grep -c "Qwen timeout" logs/operator_$(date +%Y%m%d).log
```
**Threshold:** >5 timeouts in an hour = Qwen is struggling. Check DGX load.

**What went wrong:** May 13 had 54 fallback approvals. Each one was a trade
the PM agent would have evaluated but instead auto-approved. The `/no_think`
fix (Codex `edd61c4`) reduces this by suppressing Qwen3's thinking blocks,
but network issues can still cause timeouts.

### Watch for event starvation
```bash
grep "LOW EVENT COUNT" logs/operator_$(date +%Y%m%d).log | tail -3
```
**What went wrong:** May 13: 612 empty feed cycles vs 8 productive ones.
The catalyst pool ran dry mid-day because pre-market events expired (240-min
max_age) and no fresh positive events arrived. The midday refresh loop
(added in `4857aa3`) mitigates this, but low event count still means low
trading opportunity.

### Watch for operator restarts
```bash
grep -c "bootstrapped" logs/operator_$(date +%Y%m%d).log
```
**Threshold:** >1 = operator restarted. Check why:
```bash
grep -B5 "bootstrapped" logs/operator_$(date +%Y%m%d).log | grep -E "ERROR|EXCEPTION|signal"
```
**What went wrong:** May 13: 7 restarts. Each restart reset the in-memory
`_first_seen_prices` cache (defect #12), allowing already-drifted symbols
to bypass the max_price_drift check. Also, RESERVED slots blocked capital
for up to 10 minutes per restart.

### Check PM Analyst output
```bash
curl -s http://127.0.0.1:8000/api/operator/pm-analysis | python3 -m json.tool | head -20
```
First analysis runs ~15 minutes after first trade. Manual trigger:
```bash
curl -s -X POST http://127.0.0.1:8000/api/operator/pm-analysis/run | python3 -m json.tool
```

---

## Known Open Defects (Active Risk)

### Defect #9 — Stop-loss slippage (P0)
**Risk:** Software stop evaluated every ~30s. Fast movers can drop 6–8%
between polls. TALO lost 8.12% on a 1% stop.
**Mitigation today:** `max_entry_atr_pct=6.0` rejects ultra-volatile names.
`high_volatility_slot_multiplier=0.5` halves position size on volatile ones.
Phase-4 commit added Alpaca bracket orders (stop submitted at entry time).
**Monitor:** Watch for any `STOP_LOSS` exit with `realized_pnl < -$30`
(that's >3% on a $1000 slot — slippage is happening).

### Defect #11 — Sentiment misclassification (P0)
**Risk:** Qwen sometimes classifies earnings beats as neutral, or negative
headlines as positive.
**What happened:** May 14 morning, 3 clear earnings beats (VSNT $1.99 vs
$1.63 est, WWW beat + raised guidance, YETI beat + raised guidance) were
all classified neutral due to Qwen connection failure during enrichment.
**Mitigation today:** Pre-market re-enrichment step (checklist item 3).
Sentiment refresh in signals (Codex fix) re-reads DB every 120s.
**Monitor:** On dashboard, check if active positions match positive-sentiment events.

### Defect #12 — Price drift cache resets on restart (P1)
**Risk:** After restart, `_first_seen_prices` is empty. A symbol that
already drifted 8% from catalyst price gets a fresh baseline.
**Mitigation today:** Fewer restarts (operator stability improved in
phase-4). `max_price_drift_pct=3.0` still catches new drift.
**Long-term fix needed:** Persist first-seen prices to SQLite.

---

## End of Day

### EOD analysis
```bash
.venv/bin/python scripts/analyze_paper_trading_day.py
```

### Stop all processes
```bash
bash scripts/daily_stop.sh
```

### Archive logs
```bash
mv logs/operator_$(date +%Y%m%d).log logs/archive/
mv logs/slot_manager.log logs/archive/slot_manager_$(date +%Y%m%d).log
```

### Update DEFECTS.md
After each trading day, review the log for new patterns:
```bash
# New error patterns
grep -E "ERROR|EXCEPTION|CRITICAL" logs/operator_$(date +%Y%m%d).log | sort -u

# Exit reason distribution
grep "exit_reasons" logs/operator_$(date +%Y%m%d).log | tail -1

# Total P&L
grep "total_realized_pnl" logs/operator_$(date +%Y%m%d).log | tail -1
```

---

## Incident Pattern Reference

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| All events arrive as `neutral` | Qwen unreachable during enrichment | Check DGX, re-enrich with NULL reset |
| Zero trades all day | No positive-sentiment events in pool | Verify enrichment worked (step 3) |
| Agent always approves | Qwen timeout → fallback approve | Check DGX latency, `/no_think` flag |
| Slots stuck RESERVED | Operator died mid-fill | Auto-recycled after 10min (boot) |
| Same symbol bought 5x | No reentry cooldown | `min_reentry_minutes=15` (defect #5) |
| Position held 3+ hours | Zombie from reconciliation | `FAILSAFE_TIME_STOP` (defect #6) |
| Stop loss exit at -6% | Software stop polling delay | Bracket orders (phase-4), ATR filter |
| Operator restarts repeatedly | DB lock, signal error, feed timeout | Check logs for root cause |
| Enricher says "fully enriched" but all neutral | Failed events stamped neutral as fallback | Reset `qwen_response_json IS NULL` rows |
| Trailing stop never triggers | distance > activation | Config: distance=0.4% < activation=1.0% |

---

## Config Change Audit Trail

| Date | Change | Reason | Impact |
|------|--------|--------|--------|
| 2026-05-13 | `stop_loss_pct` 1.5→1.0 | Asymmetric risk (defect #2) | Symmetric with profit_take |
| 2026-05-13 | `trailing_distance_pct` 2.0→0.4 | Could never trigger (defect #3) | Locks in 0.6%+ on winners |
| 2026-05-13 | `max_trades_per_symbol_per_day` 5→3 | Machine-gunning losses | Limits repeat damage |
| 2026-05-13 | Disabled `analyst_target_raise_v1` | Negative EV (defect #10) | Only profitable signals active |
| 2026-05-13 | Added `min_reentry_minutes=15` | Re-entry within seconds (defect #5) | 15-min cooldown |
| 2026-05-14 | Added `max_entry_atr_pct=6.0` | Volatile name slippage (defect #9) | Reject high-ATR entries |
| 2026-05-14 | Added `high_volatility_slot_multiplier=0.5` | Limit damage on volatile names | Half position size |
