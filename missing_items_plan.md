# Missing Items Execution Plan

## Immediate Execution Order
1. Wire ATR sizing into PEAD watchlist rows so signals become actionable candidates.
2. Replace fixed ticker scans with an earnings-calendar source or importable candidate universe.
3. Build real momentum universe scanning and write `momentum_scores`.
4. Add position management for targets, stops, and time exits.
5. Add backtest mode with fixture tests, transaction costs, and metrics.
6. Upgrade dashboard data from signal/admin status to portfolio, P&L, open positions, and daily summaries.

## Progress
- Done: item 1 now writes actionable PEAD rows with entry price, target, stop, ATR, shares, risk dollars, and position value.
- Done: item 2 has a first scheduler-safe universe path through `config/pead_universe.csv`, with `PEAD_SCAN_TICKERS` kept only as a quick-test override.
- Done: item 3 now runs the momentum rules across the configured universe and persists ranked rows to `momentum_scores`.
- Done: item 4 now checks entered watchlist rows for target, stop, and max-hold exits and records deterministic exit trades.
- Done: item 5 has a first deterministic backtest engine with transaction costs, win rate, Sharpe, max drawdown, profit factor, trade count, and SPY comparison support.
- Done: item 6 now exposes recent trades and daily summaries in the dashboard/admin APIs and UI.
- Done: local earnings-event import path now lets PEAD and earnings momentum prefer `config/earnings_events.csv` before falling back to Yahoo.
- Done: Admin can now import available yfinance earnings events into `config/earnings_events.csv`.
- Done: pending-entry execution now enforces max total and per-strategy position caps before sending order intents.
- Done: pending-entry execution now applies VIX, daily P&L, SPY premarket, and kill-switch pause checks before order intents.
- Done: trade-backed backtest API and Admin control now expose the backtest engine in the app.
- Done: backtests now support train, validation, and out-of-sample splits with the required survivorship-bias label.
- Done: PEAD exits now match the full scenario diagram: +8% target, -4% stop, and 20-day max hold.
- Done: scheduled PEAD sentiment defaults to FinBERT, with keyword mode still available for manual lightweight scans.
- Done: Qwen is now the default LLM provider preference for local reviews.

## Current Focus
Missing-items plan is complete for v1. Next work should be quality hardening: replace yfinance imports with a paid/official earnings-calendar source, add historical universe snapshots, and move the dashboard to Next.js if deployment becomes the priority.
