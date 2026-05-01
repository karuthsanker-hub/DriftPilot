from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel


class SupabaseLike(Protocol):
    def table(self, table_name: str): ...


class TradeRecord(BaseModel):
    ticker: str
    strategy: str
    side: str
    entry_price: float | None = None
    exit_price: float | None = None
    shares: int | None = None
    pnl: float | None = None
    pnl_pct: float | None = None
    hold_days: int | None = None
    exit_reason: str | None = None
    earnings_surprise_pct: float | None = None
    finbert_score: float | None = None
    analyst_count: int | None = None


class WatchlistRecord(BaseModel):
    ticker: str
    strategy: str
    earnings_date: date | None = None
    surprise_pct: float | None = None
    finbert_score: float | None = None
    analyst_count: int | None = None
    market_cap_m: float | None = None
    entry_price: float | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    atr_14: float | None = None
    shares: int | None = None
    risk_dollars: float | None = None
    position_value: float | None = None
    status: str = "pending"
    skip_reason: str | None = None


class MomentumScoreRecord(BaseModel):
    ticker: str
    scan_date: date
    total_score: int
    price_momentum: int
    earnings_momentum: int
    quality_score: int
    entered_position: bool = False


class StrategyConfigRepository:
    def __init__(self, client: SupabaseLike) -> None:
        self.client = client

    def is_trading_active(self) -> bool:
        result = (
            self.client.table("strategy_config")
            .select("value")
            .eq("key", "trading_active")
            .single()
            .execute()
        )
        return result.data["value"] == "true"

    def set_trading_active(self, active: bool) -> Any:
        value = "true" if active else "false"
        return (
            self.client.table("strategy_config")
            .upsert({"key": "trading_active", "value": value})
            .execute()
        )

    def list_config(self) -> dict[str, str]:
        result = self.client.table("strategy_config").select("key,value").execute()
        return {row["key"]: row.get("value", "") for row in result.data or []}


class TradingRepository:
    def __init__(self, client: SupabaseLike) -> None:
        self.client = client

    def insert_trade(self, trade: TradeRecord) -> Any:
        return self.client.table("trades").insert(_dump(trade)).execute()

    def upsert_daily_summary(self, payload: dict[str, Any]) -> Any:
        return self.client.table("daily_summary").upsert(payload).execute()

    def insert_watchlist_candidate(self, record: WatchlistRecord) -> Any:
        return self.client.table("watchlist").insert(_dump(record)).execute()

    def insert_momentum_score(self, record: MomentumScoreRecord) -> Any:
        return self.client.table("momentum_scores").upsert(_dump(record)).execute()

    def list_pending_watchlist(self) -> list[dict[str, Any]]:
        result = (
            self.client.table("watchlist")
            .select("*")
            .in_("status", ["pending", "candidate"])
            .execute()
        )
        return list(result.data or [])

    def list_entered_watchlist(self) -> list[dict[str, Any]]:
        result = (
            self.client.table("watchlist")
            .select("*")
            .eq("status", "entered")
            .execute()
        )
        return list(result.data or [])

    def list_candidate_watchlist(self) -> list[dict[str, Any]]:
        result = (
            self.client.table("watchlist")
            .select("*")
            .in_("status", ["candidate", "pending"])
            .execute()
        )
        return list(result.data or [])

    def list_watchlist_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        result = (
            self.client.table("watchlist")
            .select("*")
            .in_("id", ids)
            .execute()
        )
        return list(result.data or [])

    def list_recent_watchlist(self, *, limit: int = 50) -> list[dict[str, Any]]:
        result = (
            self.client.table("watchlist")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(result.data or [])

    def list_recent_momentum_scores(self, *, limit: int = 50) -> list[dict[str, Any]]:
        result = (
            self.client.table("momentum_scores")
            .select("*")
            .order("scan_date", desc=True)
            .order("total_score", desc=True)
            .limit(limit)
            .execute()
        )
        return list(result.data or [])

    def list_recent_trades(self, *, limit: int = 50) -> list[dict[str, Any]]:
        result = (
            self.client.table("trades")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(result.data or [])

    def list_daily_summaries(self, *, limit: int = 20) -> list[dict[str, Any]]:
        result = (
            self.client.table("daily_summary")
            .select("*")
            .order("date", desc=True)
            .limit(limit)
            .execute()
        )
        return list(result.data or [])

    def mark_watchlist_status(self, watchlist_id: str, status: str) -> Any:
        return (
            self.client.table("watchlist")
            .update({"status": status})
            .eq("id", watchlist_id)
            .execute()
        )

    def update_watchlist_trade_plan(
        self,
        watchlist_id: str,
        *,
        entry_price: float,
        target_price: float,
        stop_loss: float,
        shares: int,
        risk_dollars: float,
        position_value: float,
    ) -> Any:
        return (
            self.client.table("watchlist")
            .update(
                {
                    "entry_price": entry_price,
                    "target_price": target_price,
                    "stop_loss": stop_loss,
                    "shares": shares,
                    "risk_dollars": risk_dollars,
                    "position_value": position_value,
                }
            )
            .eq("id", watchlist_id)
            .execute()
        )

    def reset_operator_paper_state(self) -> dict[str, Any]:
        result = self.client.rpc("reset_operator_paper_state").execute()
        data = result.data or []
        return dict(data[0]) if isinstance(data, list) and data else {}


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)
