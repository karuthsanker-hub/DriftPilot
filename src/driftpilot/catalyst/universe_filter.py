from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Categories that boost a symbol's rank when present in lookback window.
POSITIVE_MICRO_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("earnings", "report"),
    ("analyst", "target_raise"),
    ("filing", "8a"),
)

# Categories that DROP a symbol from the universe entirely.
NEGATIVE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("analyst", "target_cut"),
)


class CatalystUniverseFilter:
    """Filter + rank a symbol universe based on recent catalyst events.

    Hard rule: this changes WHAT the technical signals see, NOT how they
    decide. Their thresholds are unchanged.

    Behavior:
      * Symbols with `analyst/target_cut` in last `lookback_minutes` -> DROPPED
      * Symbols with positive Micro catalyst -> ranked first (newest first)
      * Symbols with no catalyst -> preserved order, ranked last
    """

    def __init__(self, db_path: str | None, lookback_minutes: int = 240) -> None:
        self._db_path = db_path
        self._lookback_minutes = lookback_minutes

    def filter_and_rank(self, symbols: list[str], now: datetime | None = None) -> list[str]:
        if not symbols:
            return []
        if not self._db_path:
            return symbols  # graceful degradation

        if now is None:
            now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=self._lookback_minutes)).isoformat()

        try:
            return self._query_and_rank(symbols, cutoff)
        except sqlite3.Error as exc:
            logger.warning("catalyst DB unreachable (%s) - returning input universe unchanged", exc)
            return symbols

    def _query_and_rank(self, symbols: list[str], cutoff: str) -> list[str]:
        symbol_set = set(symbols)
        rows: list[tuple[str, str, str, str]] = []
        for chunk in _chunks(symbols, 500):
            placeholders = ",".join("?" * len(chunk))
            query = (
                f"SELECT symbol, category, subcategory, event_ts "
                f"FROM catalyst_events "
                f"WHERE symbol IN ({placeholders}) AND event_ts >= ? "
                f"ORDER BY event_ts DESC"
            )
            conn = sqlite3.connect(self._db_path)
            try:
                cur = conn.execute(query, (*chunk, cutoff))
                rows.extend(cur.fetchall())
            finally:
                conn.close()

        dropped: set[str] = set()
        positive_first_seen: dict[str, str] = {}

        for sym, cat, subcat, ev_ts in rows:
            if (cat, subcat) in NEGATIVE_CATEGORIES:
                dropped.add(sym)
                continue
            if (cat, subcat) in POSITIVE_MICRO_CATEGORIES:
                positive_first_seen.setdefault(sym, ev_ts)

        for sym in dropped:
            positive_first_seen.pop(sym, None)

        positives_sorted = sorted(
            positive_first_seen.keys(),
            key=lambda s: positive_first_seen[s],
            reverse=True,
        )

        positive_set = set(positives_sorted)
        non_catalyst = [s for s in symbols if s in symbol_set and s not in positive_set and s not in dropped]

        return positives_sorted + non_catalyst


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
