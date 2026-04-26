-- Trading Bot v4 Supabase schema.
-- Run this in the Supabase SQL editor for the project used by SUPABASE_URL.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS trades (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK (strategy IN ('PEAD_LONG', 'PEAD_SHORT', 'MOMENTUM')),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell', 'short', 'cover')),
    entry_price DECIMAL(12,4),
    exit_price DECIMAL(12,4),
    shares INTEGER CHECK (shares >= 0),
    pnl DECIMAL(12,2),
    pnl_pct DECIMAL(8,4),
    hold_days INTEGER,
    exit_reason TEXT,
    earnings_surprise_pct DECIMAL(8,3),
    finbert_score DECIMAL(5,4),
    analyst_count INTEGER
);

CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);

CREATE TABLE IF NOT EXISTS daily_summary (
    date DATE PRIMARY KEY,
    starting_value DECIMAL(12,2),
    ending_value DECIMAL(12,2),
    pnl DECIMAL(12,2),
    pnl_pct DECIMAL(8,4),
    open_positions INTEGER DEFAULT 0,
    new_signals INTEGER DEFAULT 0,
    vix DECIMAL(8,3),
    trading_active BOOLEAN DEFAULT TRUE,
    pause_reason TEXT,
    notes TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS watchlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    ticker TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK (strategy IN ('PEAD_LONG', 'PEAD_SHORT', 'MOMENTUM')),
    earnings_date DATE,
    surprise_pct DECIMAL(8,3),
    finbert_score DECIMAL(5,4),
    analyst_count INTEGER,
    market_cap_m DECIMAL(12,2),
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'entered', 'skipped', 'queued')),
    skip_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_watchlist_status ON watchlist(status);
CREATE INDEX IF NOT EXISTS idx_watchlist_created_at ON watchlist(created_at DESC);

CREATE TABLE IF NOT EXISTS strategy_config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO strategy_config (key, value, updated_at) VALUES
    ('trading_active', 'true', NOW()),
    ('max_positions', '6', NOW()),
    ('risk_per_trade_pct', '0.01', NOW()),
    ('pead_min_surprise_pct', '5.0', NOW()),
    ('finbert_min_score', '0.70', NOW()),
    ('momentum_min_score', '4', NOW())
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS momentum_scores (
    ticker TEXT,
    scan_date DATE,
    total_score INTEGER CHECK (total_score BETWEEN 0 AND 6),
    price_momentum INTEGER CHECK (price_momentum BETWEEN 0 AND 2),
    earnings_momentum INTEGER CHECK (earnings_momentum BETWEEN 0 AND 2),
    quality_score INTEGER CHECK (quality_score BETWEEN 0 AND 2),
    entered_position BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (ticker, scan_date)
);

ALTER PUBLICATION supabase_realtime ADD TABLE trades;
ALTER PUBLICATION supabase_realtime ADD TABLE daily_summary;
ALTER PUBLICATION supabase_realtime ADD TABLE strategy_config;

