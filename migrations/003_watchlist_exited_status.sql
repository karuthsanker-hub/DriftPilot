-- Allows deterministic position management to mark completed watchlist rows.

ALTER TABLE watchlist
    DROP CONSTRAINT IF EXISTS watchlist_status_check;

ALTER TABLE watchlist
    ADD CONSTRAINT watchlist_status_check
    CHECK (status IN ('candidate', 'pending', 'entered', 'skipped', 'queued', 'exited'));
