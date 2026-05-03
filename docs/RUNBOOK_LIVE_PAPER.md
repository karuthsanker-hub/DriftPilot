# Runbook — Live Paper Trading at 9:30 ET

**Status:** v3.0 catalyst layer is GATED (edge_ratio=1.105, N=185, Jul-Dec 2024). Live Alpaca broker execution is **NOT yet wired**. This runbook covers the **live observer** that exercises the news → signal pipeline against real market events without placing orders. Use this to validate the system before wiring real broker execution.

---

## Prerequisites

1. `.env` has Alpaca paper credentials (already does):
   ```
   ALPACA_API_KEY=...
   ALPACA_SECRET_KEY=...
   ```

2. DGX Qwen3-8B running:
   ```
   sudo systemctl start vllm-qwen   # already running per yesterday
   ```
   Health check from Mac:
   ```
   curl -s http://192.168.1.166:8000/v1/models | head
   ```

3. (Optional) Historical sentiment backfill — improves first-hour signal quality:
   ```
   python scripts/load_2024_catalyst_events.py --start 2024-12-01 --end 2026-05-03
   python scripts/enrich_catalyst_events.py --priority-only --concurrency 32
   ```

---

## At 9:25 ET (5 min before open)

```
cd "/Users/karuthsanker/Documents/Trading BOT"
CATALYST_ENABLED=true ./.venv/bin/python -u -m driftpilot.observer \
    --print-every-s 60 \
    > logs/observer_$(date +%Y%m%d).log 2>&1 &
echo "observer PID $!" > logs/observer.pid
```

Verify connect:
```
sleep 10; tail -20 logs/observer_$(date +%Y%m%d).log
```
Expect within 30s:
- `LIVE OBSERVER — read-only, NO orders will be submitted`
- `universe: 1507 symbols`
- `alpaca feed published N events` (N=0 OK pre-open, then > 0 once news flows)
- A status block every 60s with active_subscribed_events count + candidates_admitted

---

## What "healthy" looks like at 9:30

After 9:30 the Alpaca News stream should be ticking. Each 60s status block prints:

```
=== 2026-05-04T13:30:00+00:00 ===
  signal: earnings_report_v1
    active_subscribed_events: 7  sentiments: {'positive': 4, 'neutral': 2, 'negative': 1}
    candidates_admitted: 4
    symbols: AAPL, MSFT, NVDA, AMD
      [AAPL] age=12.3min sentiment=positive score=+0.12  Apple beats Q1 earnings, raises guidance...
  signal: analyst_target_raise_v1
    active_subscribed_events: 12  sentiments: {'positive': 11, 'neutral': 1}
    candidates_admitted: 12
```

Healthy indicators:
- ✅ `active_subscribed_events` is non-zero (news is flowing)
- ✅ `sentiments` shows mix of positive/neutral/negative (Qwen is enriching)
- ✅ `candidates_admitted ≤ active_subscribed_events` for earnings_report_v1 (sentiment gate filtering)
- ✅ candidates have age < 60 min and recognizable headlines

Not-yet-broken-but-watch indicators:
- ⚠️ `candidates_admitted = 0` for earnings_report_v1 means no positive earnings reports in the last hour. **This is expected** outside earnings season (May 4 is between seasons). Not a bug.
- ⚠️ `sentiments: {'unenriched': N}` means Qwen failed for those events. Check DGX. Not fatal — events still flow, just no directional gate.

Hard-broken indicators (kill and investigate):
- ❌ `Traceback` in the log
- ❌ `alpaca feed published 0 events` for > 10 minutes after 9:30 (news API is dead)
- ❌ `qwen enrichment failed (ConnectError)` repeatedly (DGX is down)

---

## Kill switch

```
kill $(cat logs/observer.pid) 2>/dev/null && rm logs/observer.pid
# or
pkill -f "driftpilot.observer"
```

The observer does NOT submit orders. Killing it has zero financial effect — only stops logging.

---

## Why no live broker today

The validated v3 stack is:
```
Alpaca News → DiscoveryService → Bus → Qwen Enrichment → Signal.scan() → ???
                                                                          ↑
                                                               THIS IS THE GAP
```

The "???" is wiring `AlpacaSIPStream` (live bars), `AlpacaBrokerClient` (real orders), and the state machine's IN_POSITION/EXITING transitions. Those modules exist (`src/driftpilot/market_data/alpaca_stream.py` and `src/driftpilot/broker/alpaca_client.py`) but the operator entrypoint never composed them — it's been mock all along.

Wiring them is a real integration job (probably a half-day with the existing test suite). What the observer DOES validate today:

| Component | Validated by observer? |
|---|---|
| Alpaca News auth + polling | ✅ |
| Deterministic classifier on live headlines | ✅ |
| Bus delivery + dedupe | ✅ |
| Qwen enrichment latency + accuracy | ✅ |
| Signal.scan() candidate generation | ✅ |
| Sentiment gate behavior | ✅ |
| Live bar streaming | ❌ not exercised |
| Order submission | ❌ not exercised |
| Slot allocator + fill engine | ❌ not exercised |

Tomorrow's run answers: **"Is the news pipeline healthy?"** It does NOT answer **"Does the strategy make money in real time?"** — that requires the next sprint.

---

## What to do tomorrow

1. **9:25 ET**: start the observer per above
2. **Watch for first 30 min**. Capture screenshot or paste a few status blocks.
3. **At 10:00 ET**: count `candidates_admitted` totals. If > 0 and the headlines look right → the pipeline is healthy and ready for live broker wiring.
4. **At 4:00 ET**: stop the observer. Save `logs/observer_<date>.log` for analysis.

If the observer ran clean for 6+ hours with non-trivial event flow, the GREEN-light to start the live broker integration is earned.

If it threw exceptions or the news flow was suspiciously empty, fix those first.

---

## When live broker wiring lands (next sprint)

The flag will be `--live` on the operator entrypoint. The default will remain mock. Pre-prod checklist:
- [ ] Account is paper, not live (`ALPACA_BASE_URL=https://paper-api.alpaca.markets`)
- [ ] `paper_trading_gate_passed=true` in env
- [ ] `ACTIVE_SIGNAL=earnings_report_v1`
- [ ] `OPERATOR_TRADE_SLOTS=10`, `OPERATOR_SLOT_VALUE=1000` (paper $10k notional)
- [ ] Catalyst SQLite has > 1k enriched events (so signals have history to filter on)
- [ ] Observer has been clean for at least one full session

Until then: observer only.
