# Trading Bot v4 Implementation Plan

## Summary
Build a daily-batch trading research system around PEAD, FinBERT sentiment, and multi-dimensional momentum. The execution path must stay pure Python and rule-based; LLMs are only used for scheduled analysis and portfolio explanation, never for placing trades.

## Phase 1: Research Harness
- Add project modules for market data, indicators, strategy rules, backtesting, persistence, and execution.
- Implement a yfinance-backed data layer for daily OHLCV, earnings history, market cap, analyst count, and basic fundamentals.
- Implement PEAD long/short signal rules:
  - Market cap $200M-$2B, price > $5, average volume > 50,000, analyst count <= 5.
  - Positive EPS surprise >= 5% plus positive FinBERT score >= 0.70 for longs.
  - Negative EPS surprise <= -5% plus negative FinBERT score >= 0.70 for shorts.
  - Trend filter: price above/below 50-day EMA.
  - Conviction filter: earnings-day volume >= 2x 20-day average.
- Implement momentum scoring:
  - Price momentum from 3-month and 6-month returns.
  - Earnings momentum from at least 3 of last 4 quarters beating estimates.
  - Quality score from ROE, debt/equity, and profit margin.
- Add backtest mode with transaction cost, train/validate/out-of-sample split, and metrics: win rate, Sharpe, max drawdown, profit factor, trade count, and SPY comparison.

## Phase 2: Supabase Persistence
- Replace SQLite assumptions with Supabase via `supabase-py`.
- Add schema/migration SQL for:
  - `trades`
  - `daily_summary`
  - `watchlist`
  - `strategy_config`
  - `momentum_scores`
- Add repository classes for table reads/writes.
- Ensure every trading action checks `strategy_config.trading_active`.
- Log pauses to `daily_summary` with clear reasons.

## Phase 3: Paper Trading Engine
- Keep Alpaca paper mode as the default and only execution target for v1.
- Add scheduled jobs:
  - Daily PEAD scan after market close.
  - Next-morning entry for pending PEAD candidates.
  - Weekly Monday momentum scan.
  - Daily position management for stops, targets, and time exits.
- Implement ATR-based position sizing:
  - Risk 1% of portfolio per trade.
  - Stop distance = 2x ATR_14.
  - Maximum 20% of portfolio in one position.
  - Maximum 6 total positions: 3 PEAD long, 2 PEAD short, 1 momentum.
- Add automatic pause checks:
  - Kill switch inactive.
  - VIX <= 25.
  - Daily P&L above -2%.
  - SPY pre-market change above -1.5%.

## Phase 4: Dashboard And LLM Hooks
- Build or migrate to a Vercel Next.js dashboard backed by Supabase realtime.
- Dashboard v1 must show open positions, P&L, PEAD queue, momentum scores, daily summaries, and kill switch.
- Keep the existing LLM adapter, but narrow its role to:
  - Nightly Qwen analysis of trades.
  - Weekly Claude/OpenAI/Gemini/Qwen watchlist explanation.
  - Monthly strategy review.
  - On-demand portfolio Q&A.
- Add prompt functions only for strategy analysis:
  - `nightly_analysis_prompt(trades_today)`
  - `weekly_watchlist_prompt(earnings_calendar)`
  - `monthly_review_prompt(month_trades, month_summary)`

## Test Plan
- Unit tests for PEAD filters, momentum scoring, ATR sizing, pause checks, and Supabase repository mapping.
- Backtest tests with fixture data proving entries, exits, transaction costs, and metrics are calculated correctly.
- Paper trading tests with mocked Alpaca proving orders are blocked by kill switch, VIX, P&L, max positions, and paper-mode defaults.
- Dashboard/API tests for strategy config reads/writes and kill switch behavior.
- LLM tests should verify prompts and adapter routing only; LLM output must never be required for trade execution.

## Defaults And Assumptions
- `strategy_master_v4.md` is the product source of truth.
- Backtesting comes before paper trading.
- Current-universe yfinance backtests are acceptable for v1, but must be labeled survivorship-biased.
- Supabase is the primary database; SQLite should not be added.
- Live trading remains blocked until explicitly enabled later.
