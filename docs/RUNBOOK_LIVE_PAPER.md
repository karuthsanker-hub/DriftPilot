# Runbook — Live Paper Trading at 9:30 ET

**Status:** v3.0 catalyst layer is GATED (edge_ratio=1.105, N=185, Jul-Dec 2024). Real Alpaca paper-account order submission is **NOW WIRED** via the `--paper-live` flag. Tomorrow we trade.

---

## What ships in `--paper-live`

| Layer | Live? |
|---|---|
| Alpaca News API + dedupe + bus | ✅ |
| Catalyst classifier (regex) | ✅ |
| Qwen3-8B sentiment enrichment (DGX) | ✅ |
| earnings_report_v1 + positive sentiment gate | ✅ |
| analyst_target_raise_v1 (no edge — known) | ✅ subscribed for observation |
| Slot allocator (10 slots × $1k = $10k notional) | ✅ |
| **Real Alpaca paper order submission** | ✅ marketable limit, marketable-limit-on-exit |
| **Real Alpaca paper account state** | ✅ `get_account`, `get_open_positions` |
| Position monitor (REST quote polling, evaluate_exit) | ✅ |
| target_cut → EMERGENCY_FLUSH | ✅ wired (state machine subscription) |
| Live SIP bar streaming for technical signals | ❌ not needed for catalyst signals |

---

## Smoke test (run it now to confirm broker connection)

```
cd "/Users/karuthsanker/Documents/Trading BOT"
./.venv/bin/python -c "
import asyncio
from driftpilot.settings import load_settings
from driftpilot.services_live import build_live_components
from driftpilot.clock import DriftPilotClock
from driftpilot.storage.repositories import DriftPilotRepository

s = load_settings('.env')
clock = DriftPilotClock(s.timezone)
repo = DriftPilotRepository.open(s.sqlite_path_obj, clock)
broker, _, _ = build_live_components(repo, s, clock=clock, catalyst_db_path=s.catalyst_db_path)

async def check():
    acct = await broker.get_account()
    print(f'account_id: {acct.account_id}')
    print(f'status: {acct.status}')
    print(f'equity: \${acct.equity:,.2f}')
    print(f'buying_power: \${acct.buying_power:,.2f}')
    pos = await broker.get_open_positions()
    print(f'open positions: {len(pos)}')

asyncio.run(check())
"
```

Expected output:
```
account_id: 7cace921-eaa1-434e-9421-25e8bacce3a2
status: AccountStatus.ACTIVE
equity: $100,036.32
buying_power: $400,145.28
open positions: 0
```

If you see this, broker is wired correctly. Verified once tonight on this machine.

---

## Pre-warm (already done tonight, re-run anytime)

The catalyst DB needs recent news + Qwen sentiment for the signals to admit candidates at startup.

```
# Pull news for the last 2 weeks (run nightly)
./.venv/bin/python scripts/load_2024_catalyst_events.py \
    --start 2026-04-20 --end 2026-05-03 \
    --output data/driftpilot/catalyst_events_2024.sqlite3

# Enrich with Qwen sentiment (concurrent on DGX)
./.venv/bin/python scripts/enrich_catalyst_events.py \
    --priority-only --concurrency 32
```

Tonight's pull added 7,118 fresh events on top of the 15,016 from earlier. Enrichment runs in ~10 min for ~5,500 priority events.

---

## At 9:25 ET (5 min before open) — TWO-PHASE LAUNCH

### Phase 1 (9:25–9:30): observer-only sanity check

```
cd "/Users/karuthsanker/Documents/Trading BOT"
mkdir -p logs
CATALYST_ENABLED=true ./.venv/bin/python -u -m driftpilot.observer \
    --print-every-s 30 \
    > logs/observer_$(date +%Y%m%d).log 2>&1 &
echo $! > logs/observer.pid
```

Verify in another terminal:
```
tail -F logs/observer_$(date +%Y%m%d).log
```

You should see within 30 sec:
- `LIVE OBSERVER — read-only, NO orders will be submitted`
- `universe: 1507 symbols`
- `alpaca feed published N events`
- Status snapshot every 30s

If the observer crashes or shows `Traceback` — **DO NOT proceed to Phase 2**. Fix first.

### Phase 2 (9:30): kill observer, launch live operator

```
kill $(cat logs/observer.pid) 2>/dev/null
rm logs/observer.pid

CATALYST_ENABLED=true ACTIVE_SIGNAL=earnings_report_v1 \
    ./.venv/bin/python -u -m driftpilot.operator \
    --paper-live \
    > logs/operator_$(date +%Y%m%d).log 2>&1 &
echo $! > logs/operator.pid

tail -F logs/operator_$(date +%Y%m%d).log
```

You should see:
- `🚨 PAPER-LIVE MODE: submitting real orders to Alpaca paper account at https://paper-api.alpaca.markets`
- `catalyst layer ENABLED: db=...catalyst_events_2024.sqlite3 qwen=http://192.168.1.166:8000/v1`
- State transitions: `BOOT → SCANNING → ALLOCATING → IN_POSITION → ...`
- Per-allocation: `LIVE: submitting paper buy AAPL qty=5 slot=1 ref_price=200.00`
- Per-fill: `LIVE: position opened symbol=AAPL qty=5 entry=200.10 broker_order_id=...`

---

## What "healthy" looks like during the day

Check Alpaca dashboard at https://app.alpaca.markets/paper/dashboard/overview to see real positions appearing as the operator trades.

In the log:
- ✅ One `LIVE: submitting paper buy` per candidate the allocator picks
- ✅ One `LIVE: position opened` per successful fill
- ✅ One `LIVE: signal requests exit` then `LIVE: position closed` per exit
- ⚠️ `LIVE: entry not submitted for X — reason=quote_unavailable` is OK (illiquid name)
- ❌ `Traceback` or `LIVE: entry submission failed` → kill, investigate

---

## Kill switches (in priority order)

1. **Stop the operator** (no new entries, no new exits):
   ```
   kill $(cat logs/operator.pid) && rm logs/operator.pid
   ```

2. **Cancel all open orders + flatten all positions** (via Alpaca dashboard or CLI):
   ```
   ./.venv/bin/python -c "
   from alpaca.trading.client import TradingClient
   from driftpilot.settings import load_settings
   s = load_settings('.env')
   c = TradingClient(s.alpaca_key_id, s.alpaca_secret_key, paper=True)
   c.cancel_orders()
   c.close_all_positions(cancel_orders=True)
   print('all orders canceled, all positions closed (market orders)')
   "
   ```

3. **Disable trading at the Alpaca dashboard** (most aggressive — turn the account off entirely)

---

## Risk envelope (paper account, but still)

- Account: paper at `https://paper-api.alpaca.markets`
- Equity: $100,036.32 (verified)
- Slots: **10 slots × $1,000 = $10k max notional exposure**
- Per-trade: catalyst event drives entry; profit_take=1.0%, stop_loss=1.5%, max_hold=60min
- Per-day cap: `MAX_TRADES_PER_DAY=50`
- Per-symbol cap: `MAX_TRADES_PER_SYMBOL_PER_DAY=3`
- Daily loss limit: `DAILY_LOSS_LIMIT_PCT=0.03` (3% of equity → flatten if breached)
- target_cut on a held name → `EMERGENCY_FLUSH` state → market-exit

---

## What to watch for honestly

**Tomorrow is between earnings seasons.** Q1 reporters mostly done by Apr 30; Q2 doesn't start until mid-July. Realistic expectation:

- 5-15 earnings/report events tomorrow (vs ~50/day during peak season)
- After positive-sentiment gate: maybe 3-7 candidates entered all day
- Some days zero — that's correct behavior, not a bug
- target_raise: 50-100/day, but the signal already FAILed this category so it's just noise on the dashboard

**Backtest verdict reminder:** edge_ratio=1.105 over 6 months. **One day's results mean nothing.** A losing day is within expectation. The validation says we have an edge over hundreds of trades, not over five.

---

## End of day (4:00 ET) — full audit

```bash
kill $(cat logs/operator.pid) && rm logs/operator.pid

# Full audit: per-position chain (catalyst event → order → fill → exit → PnL)
# plus aggregate metrics + Alpaca ground-truth + catalyst event volume
./.venv/bin/python scripts/analyze_paper_trading_day.py --include-alpaca-snapshot
```

This prints:

1. **Per-position chain** for every position opened today:
   - The catalyst event that triggered it (sentiment, headline, age at entry)
   - The order submission (limit price, broker_order_id)
   - The exit (reason, realized PnL, hold time)

2. **Aggregate summary** in the same shape as the backtest reports:
   - trades, win rate, breakeven, edge_ratio, total realized PnL
   - exit_reasons breakdown (profit_take / time_stop / stop_loss)
   - by_catalyst_sentiment breakdown

3. **Alpaca ground-truth snapshot** (with `--include-alpaca-snapshot`):
   - account equity, buying power, open positions

4. **Catalyst event volume** for the day (top 20 by count) — to compare
   the news flow against trade activity.

### What every log line tells you (grep cheatsheet)

```bash
# Every event published to the bus, with sentiment + headline
grep "EVENT " logs/operator_*.log

# Every candidate the scanner emitted (admitted by signal + sentiment gate)
grep "CANDIDATE " logs/operator_*.log

# Every paper buy submitted
grep "LIVE: submitting paper buy" logs/operator_*.log

# Every position opened (with broker_order_id)
grep "LIVE: position opened" logs/operator_*.log

# Every signal-driven exit
grep "LIVE: signal requests exit" logs/operator_*.log

# Every error
grep -E "ERROR|Traceback" logs/operator_*.log
```

If a candidate did NOT become a position, look back to:
1. `LIVE: entry not submitted — reason=quote_unavailable` (broker rejected)
2. `LIVE: entry submission failed` (Alpaca API error)
3. SlotAllocator `BlockedReason` (sector cap, dup symbol, daily cap, catalyst_negative)

Then:
```
./.venv/bin/python -c "
import asyncio
from driftpilot.settings import load_settings
from driftpilot.services_live import build_live_components
from driftpilot.storage.repositories import DriftPilotRepository
from driftpilot.clock import DriftPilotClock
s = load_settings('.env')
clock = DriftPilotClock(s.timezone)
repo = DriftPilotRepository.open(s.sqlite_path_obj, clock)
broker, _, _ = build_live_components(repo, s, clock=clock, catalyst_db_path=s.catalyst_db_path)
async def check():
    acct = await broker.get_account()
    print(f'EOD equity: \${acct.equity:,.2f}')
    print(f'cash: \${acct.cash:,.2f}')
    pos = await broker.get_open_positions()
    print(f'open positions left: {len(pos)}')
    for p in pos: print(f'  {p.symbol} qty={p.quantity} unrealized=\${p.unrealized_pl:.2f}')
asyncio.run(check())
"
```

If positions are still open at 4:00, leave them and the system will manage them tomorrow (max_hold is 60min so they should auto-exit but cron-belt-and-suspenders: watch the dashboard).

Save the day's log:
```
cp logs/operator_$(date +%Y%m%d).log logs/archive/
```

---

## Common issues + fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| `qwen enrichment failed (ConnectError)` repeatedly | DGX vllm down | `ssh sankerkr@192.168.1.166 'sudo systemctl status vllm-qwen'` |
| `alpaca feed published 0 events` for 30+ min after open | News API throttled or auth issue | Check `.env` ALPACA keys; `curl -H "APCA-API-KEY-ID: $ALPACA_API_KEY" -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" https://data.alpaca.markets/v1beta1/news` |
| `entry not submitted — quote_unavailable` for everything | Quote provider failing | Check Alpaca status page; the REST quote endpoint may be degraded |
| Operator hangs in BOOT | DB path issue or repo schema mismatch | `rm data/driftpilot/operator_state.sqlite3` (paper state, safe to wipe) and restart |
| One position never exits | evaluate_exit not firing — check signal config | Hard kill operator; flatten via dashboard; investigate logs/ |

---

## Test counts at handoff

511 / 511 passing including 7 new live-services tests. Last updated commit on the integration branch.
