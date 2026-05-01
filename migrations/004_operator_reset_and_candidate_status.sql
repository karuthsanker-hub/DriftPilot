-- Keeps operator-generated rows first-class and provides a clean paper reset helper.

ALTER TABLE watchlist
    DROP CONSTRAINT IF EXISTS watchlist_status_check;

ALTER TABLE watchlist
    ADD CONSTRAINT watchlist_status_check
    CHECK (status IN ('candidate', 'pending', 'entered', 'skipped', 'queued', 'exited'));

CREATE OR REPLACE FUNCTION reset_operator_paper_state()
RETURNS TABLE (
    deleted_trades BIGINT,
    deleted_watchlist BIGINT,
    deleted_daily_summary BIGINT,
    deleted_momentum_scores BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    trade_count BIGINT;
    watchlist_count BIGINT;
    summary_count BIGINT;
    momentum_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO trade_count FROM trades;
    SELECT COUNT(*) INTO watchlist_count FROM watchlist;
    SELECT COUNT(*) INTO summary_count FROM daily_summary;
    SELECT COUNT(*) INTO momentum_count FROM momentum_scores;

    TRUNCATE TABLE trades, watchlist, daily_summary, momentum_scores;

    RETURN QUERY SELECT trade_count, watchlist_count, summary_count, momentum_count;
END;
$$;
