-- Adds execution-ready fields to PEAD/momentum watchlist rows.

ALTER TABLE watchlist
    ADD COLUMN IF NOT EXISTS entry_price DECIMAL(12,4),
    ADD COLUMN IF NOT EXISTS target_price DECIMAL(12,4),
    ADD COLUMN IF NOT EXISTS stop_loss DECIMAL(12,4),
    ADD COLUMN IF NOT EXISTS atr_14 DECIMAL(12,4),
    ADD COLUMN IF NOT EXISTS shares INTEGER CHECK (shares IS NULL OR shares >= 0),
    ADD COLUMN IF NOT EXISTS risk_dollars DECIMAL(12,2),
    ADD COLUMN IF NOT EXISTS position_value DECIMAL(12,2);

