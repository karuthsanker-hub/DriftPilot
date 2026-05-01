from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_bot.data.repositories import TradingRepository, WatchlistRecord
from trading_bot.execution.alpaca_broker import Broker, OrderIntent
from trading_bot.settings import AppSettings


@dataclass(frozen=True)
class OperatorProjection:
    paper_capital: float
    per_trade_allocation: float
    target_pct: float
    stop_pct: float
    trade_slots: int
    candidate_count: int
    planned_capital: float
    target_profit: float
    max_loss: float
    reward_risk: float | None


def build_top_bets(rows: list[dict[str, Any]], settings: AppSettings, *, open_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    candidates = [_candidate(row, settings) for row in rows if _eligible(row)]
    candidates = sorted(candidates, key=lambda row: row["score"], reverse=True)[: settings.operator_max_candidates]
    projection = _projection(candidates, settings)
    open_rows = open_rows or []
    deployed_capital = round(sum(_float(row.get("position_value")) or 0.0 for row in open_rows), 2)
    return {
        "paper_capital": settings.operator_paper_capital,
        "available_capital": round(max(0.0, settings.operator_paper_capital - deployed_capital), 2),
        "deployed_capital": deployed_capital,
        "open_positions": len(open_rows),
        "target_pct": settings.operator_target_pct,
        "stop_pct": settings.operator_stop_pct,
        "max_candidates": settings.operator_max_candidates,
        "trade_slots": settings.operator_trade_slots,
        "industry_groups": _industry_groups(candidates),
        "candidates": candidates,
        "projection": projection.__dict__,
    }


def momentum_rows_to_operator_rows(momentum_rows: list[dict[str, Any]], prices: dict[str, float]) -> list[dict[str, Any]]:
    rows = []
    for item in momentum_rows:
        ticker = str(item.get("ticker", "")).upper()
        price = prices.get(ticker)
        if not ticker or price is None or price <= 0:
            continue
        rows.append(
            {
                "id": f"momentum:{ticker}",
                "ticker": ticker,
                "strategy": "MOMENTUM",
                "status": "pending",
                "entry_price": price,
                "surprise_pct": item.get("earnings_momentum") or item.get("total_score") or 0,
                "operator_only": True,
                "momentum_score": item.get("total_score"),
                "sector": item.get("sector"),
                "industry": item.get("industry"),
            }
        )
    return rows


def approve_paper_trades(
    *,
    rows: list[dict[str, Any]],
    selected_ids: list[str],
    settings: AppSettings,
    repository: TradingRepository,
    broker: Broker,
    submit: bool,
    open_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected = {item for item in selected_ids if item}
    open_tickers = {str(row.get("ticker", "")).upper() for row in open_rows or [] if row.get("ticker")}
    candidates = [_candidate(row, settings) for row in rows if str(row.get("id")) in selected and _eligible(row)]
    submitted = []
    skipped = []
    for candidate in candidates:
        if candidate["ticker"].upper() in open_tickers:
            skipped.append({"id": candidate["id"], "ticker": candidate["ticker"], "reason": "ticker already has an open paper position"})
            continue
        if candidate["shares"] <= 0:
            skipped.append({"id": candidate["id"], "ticker": candidate["ticker"], "reason": "allocation too small for share price"})
            continue
        side = "short" if candidate["direction"] == "short" else "buy"
        result = broker.submit_market_order(
            OrderIntent(ticker=candidate["ticker"], side=side, shares=candidate["shares"], strategy=candidate["strategy"]),
            dry_run=not submit,
        )
        if candidate["operator_only"]:
            if result.submitted:
                repository.insert_watchlist_candidate(_watchlist_record(candidate, status="entered"))
        else:
            repository.update_watchlist_trade_plan(
                candidate["id"],
                entry_price=candidate["entry_price"],
                target_price=candidate["target_price"],
                stop_loss=candidate["stop_loss"],
                shares=candidate["shares"],
                risk_dollars=candidate["max_loss"],
                position_value=candidate["capital_used"],
            )
            if result.submitted:
                repository.mark_watchlist_status(candidate["id"], "entered")
        submitted.append(
            {
                "id": candidate["id"],
                "ticker": candidate["ticker"],
                "shares": candidate["shares"],
                "side": side,
                "submitted": result.submitted,
                "message": result.message,
                "order_id": result.order_id,
            }
        )
    return {
        "submit": submit,
        "requested": len(selected_ids),
        "attempted": len(candidates),
        "submitted": submitted,
        "skipped": skipped,
        "projection": _projection(candidates, settings).__dict__,
    }


def _eligible(row: dict[str, Any]) -> bool:
    return row.get("status") in {"candidate", "pending"} and _float(row.get("entry_price")) is not None


def _candidate(row: dict[str, Any], settings: AppSettings) -> dict[str, Any]:
    entry = _float(row.get("entry_price")) or 0.0
    direction = "short" if row.get("strategy") == "PEAD_SHORT" else "long"
    per_trade = settings.operator_paper_capital / settings.operator_trade_slots
    shares = int(per_trade / entry) if entry > 0 else 0
    capital_used = shares * entry
    if direction == "short":
        target_price = entry * (1 - settings.operator_target_pct)
        stop_loss = entry * (1 + settings.operator_stop_pct)
    else:
        target_price = entry * (1 + settings.operator_target_pct)
        stop_loss = entry * (1 - settings.operator_stop_pct)
    target_profit = abs(target_price - entry) * shares
    max_loss = abs(entry - stop_loss) * shares
    surprise = abs(_float(row.get("surprise_pct")) or 0.0)
    risk_dollars = _float(row.get("risk_dollars")) or max_loss
    score = surprise + max(0.0, min(25.0, risk_dollars)) / 10
    return {
        "id": str(row.get("id", "")),
        "ticker": row.get("ticker", ""),
        "strategy": row.get("strategy", ""),
        "status": row.get("status", ""),
        "direction": direction,
        "score": round(score, 3),
        "entry_price": round(entry, 4),
        "target_price": round(target_price, 4),
        "stop_loss": round(stop_loss, 4),
        "target_pct": settings.operator_target_pct,
        "stop_pct": settings.operator_stop_pct,
        "allocation": round(per_trade, 2),
        "shares": shares,
        "capital_used": round(capital_used, 2),
        "target_profit": round(target_profit, 2),
        "max_loss": round(max_loss, 2),
        "reward_risk": round(target_profit / max_loss, 2) if max_loss else None,
        "surprise_pct": row.get("surprise_pct"),
        "operator_only": bool(row.get("operator_only")),
        "momentum_score": row.get("momentum_score"),
        "sector": _sector(row),
        "industry": _industry(row),
        "reason": _reason(row, direction),
        "risk_flags": _risk_flags(row, shares),
    }


def _projection(candidates: list[dict[str, Any]], settings: AppSettings) -> OperatorProjection:
    planned_capital = sum(float(row["capital_used"]) for row in candidates)
    target_profit = sum(float(row["target_profit"]) for row in candidates)
    max_loss = sum(float(row["max_loss"]) for row in candidates)
    return OperatorProjection(
        paper_capital=settings.operator_paper_capital,
        per_trade_allocation=settings.operator_paper_capital / settings.operator_trade_slots,
        target_pct=settings.operator_target_pct,
        stop_pct=settings.operator_stop_pct,
        trade_slots=settings.operator_trade_slots,
        candidate_count=len(candidates),
        planned_capital=round(planned_capital, 2),
        target_profit=round(target_profit, 2),
        max_loss=round(max_loss, 2),
        reward_risk=round(target_profit / max_loss, 2) if max_loss else None,
    )


def _watchlist_record(candidate: dict[str, Any], *, status: str) -> WatchlistRecord:
    return WatchlistRecord(
        ticker=candidate["ticker"],
        strategy=candidate["strategy"],
        entry_price=candidate["entry_price"],
        target_price=candidate["target_price"],
        stop_loss=candidate["stop_loss"],
        shares=candidate["shares"],
        risk_dollars=candidate["max_loss"],
        position_value=candidate["capital_used"],
        status=status,
        skip_reason="operator-approved momentum fallback",
    )


def _reason(row: dict[str, Any], direction: str) -> str:
    if row.get("strategy") == "MOMENTUM":
        score = row.get("momentum_score")
        suffix = f" with score {score}" if score is not None else ""
        return f"MOMENTUM {direction} setup{suffix}. Operator projection uses configured target and stop exits."
    surprise = row.get("surprise_pct")
    strategy = row.get("strategy", "candidate")
    if surprise is None:
        return f"{strategy} {direction} setup from the pending watchlist."
    return f"{strategy} {direction} setup with {float(surprise):.2f}% earnings surprise."


def _risk_flags(row: dict[str, Any], shares: int) -> list[str]:
    flags = []
    if shares <= 0:
        flags.append("share price exceeds per-trade allocation")
    if row.get("skip_reason"):
        flags.append(str(row["skip_reason"]))
    return flags


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _sector(row: dict[str, Any]) -> str:
    return str(row.get("sector") or _classification(str(row.get("ticker", "")))[0])


def _industry(row: dict[str, Any]) -> str:
    return str(row.get("industry") or _classification(str(row.get("ticker", "")))[1])


def _industry_groups(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = candidate["industry"]
        group = groups.setdefault(key, {"industry": key, "sector": candidate["sector"], "count": 0, "tickers": [], "planned_capital": 0.0})
        group["count"] += 1
        group["tickers"].append(candidate["ticker"])
        group["planned_capital"] = round(float(group["planned_capital"]) + float(candidate["capital_used"]), 2)
    return sorted(groups.values(), key=lambda row: (-row["count"], row["industry"]))[:20]


def _classification(ticker: str) -> tuple[str, str]:
    ticker = ticker.upper()
    known = {
        "AAPL": ("Technology", "Consumer Electronics"),
        "MSFT": ("Technology", "Software Infrastructure"),
        "NVDA": ("Technology", "Semiconductors"),
        "AMD": ("Technology", "Semiconductors"),
        "AVGO": ("Technology", "Semiconductors"),
        "CSCO": ("Technology", "Communication Equipment"),
        "AMZN": ("Consumer Cyclical", "Internet Retail"),
        "META": ("Communication Services", "Internet Content"),
        "GOOGL": ("Communication Services", "Internet Content"),
        "TSLA": ("Consumer Cyclical", "Auto Manufacturers"),
        "PLTR": ("Technology", "Software Application"),
        "JPM": ("Financial Services", "Banks"),
        "BAC": ("Financial Services", "Banks"),
        "XOM": ("Energy", "Oil and Gas Integrated"),
        "CVX": ("Energy", "Oil and Gas Integrated"),
        "UNH": ("Healthcare", "Healthcare Plans"),
        "LLY": ("Healthcare", "Drug Manufacturers"),
        "WMT": ("Consumer Defensive", "Discount Stores"),
        "COST": ("Consumer Defensive", "Discount Stores"),
    }
    return known.get(ticker, ("Unclassified", "Unclassified"))
