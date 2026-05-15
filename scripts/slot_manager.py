#!/usr/bin/env python3
"""Slot Manager — watchdog/supervisor for DriftPilot slot inventory.

Runs as a background daemon alongside the operator. Every POLL_INTERVAL
seconds it:
  1. Collects system state (slots, positions, logs, process health)
  2. Runs deterministic slot inventory health checks
  3. Executes only narrow, deterministic corrective actions

Safe actions the manager can take:
  - Recycle stuck RESERVED slots → EMPTY
  - Log warnings about anomalies
  - Premarket slot cleanup only with explicit broker-flat confirmation

Actions it will NEVER take:
  - Modify OPEN/ENTERING/EXITING slots during normal watchdog checks
  - Submit or cancel orders
  - Change config or signal settings

Usage:
    python scripts/slot_manager.py                # foreground
    python scripts/slot_manager.py --daemon       # background (writes to logs/)
    python scripts/slot_manager.py --once         # single check, then exit
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from driftpilot.clock import DriftPilotClock, datetime_from_storage, datetime_to_storage

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = PROJECT_ROOT / "data" / "driftpilot" / "operator_state.sqlite3"
CATALYST_DB = PROJECT_ROOT / "data" / "driftpilot" / "catalyst_events.sqlite3"
LOG_DIR = PROJECT_ROOT / "logs"
PID_FILE = LOG_DIR / "operator.pid"
MANAGER_LOG = LOG_DIR / "slot_manager.log"

POLL_INTERVAL = 60  # seconds between health checks
RESERVED_STALE_MINUTES = 5  # recycle RESERVED slots older than this
ACTIVE_SLOT_STATUSES = frozenset({"OPEN", "ENTERING", "EXITING"})

logger = logging.getLogger("slot_manager")


@dataclass(frozen=True, slots=True)
class HealthIssue:
    """Deterministic health issue emitted by watchdog checks."""

    code: str
    severity: str
    message: str
    slot_id: int | None = None
    position_id: int | None = None


@dataclass(frozen=True, slots=True)
class PremarketCleanDecision:
    """Pure decision result for the guarded premarket cleanup action."""

    allowed: bool
    reason: str


# ---------------------------------------------------------------------------
# Pure health helpers
# ---------------------------------------------------------------------------

def _coerce_positive_int(value: object) -> int | None:
    """Return a positive int, or None for null/invalid identifiers."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def find_slot_inventory_issues(
    slots: list[dict],
    open_positions: list[dict],
) -> list[HealthIssue]:
    """Detect impossible slot/position inventory without mutating anything."""
    issues: list[HealthIssue] = []
    open_positions_by_id = {
        int(position["id"]): position
        for position in open_positions
        if _coerce_positive_int(position.get("id")) is not None
    }
    active_slots_by_position_id: dict[int, dict] = {}

    for slot in slots:
        status = str(slot.get("status") or "").upper()
        if status not in ACTIVE_SLOT_STATUSES:
            continue

        slot_id = _coerce_positive_int(slot.get("slot_id"))
        position_id = _coerce_positive_int(slot.get("position_id"))
        if position_id is None:
            issues.append(
                HealthIssue(
                    code="active_slot_invalid_position_id",
                    severity="warning",
                    message=(
                        f"{status} slot {slot_id or '?'} has null/invalid position_id"
                    ),
                    slot_id=slot_id,
                )
            )
            continue

        position = open_positions_by_id.get(position_id)
        if position is None:
            issues.append(
                HealthIssue(
                    code="active_slot_missing_open_position",
                    severity="warning",
                    message=(
                        f"{status} slot {slot_id or '?'} references position "
                        f"{position_id}, but that position is not locally open"
                    ),
                    slot_id=slot_id,
                    position_id=position_id,
                )
            )
        elif slot.get("symbol") and position.get("symbol") and slot["symbol"] != position["symbol"]:
            issues.append(
                HealthIssue(
                    code="active_slot_symbol_mismatch",
                    severity="warning",
                    message=(
                        f"{status} slot {slot_id or '?'} symbol {slot['symbol']} "
                        f"does not match open position {position_id} symbol "
                        f"{position['symbol']}"
                    ),
                    slot_id=slot_id,
                    position_id=position_id,
                )
            )
        active_slots_by_position_id[position_id] = slot

    for position_id, position in open_positions_by_id.items():
        if position_id not in active_slots_by_position_id:
            issues.append(
                HealthIssue(
                    code="open_position_without_active_slot",
                    severity="warning",
                    message=(
                        f"open position {position_id} ({position.get('symbol')}) "
                        "has no OPEN/ENTERING/EXITING slot"
                    ),
                    position_id=position_id,
                )
            )

    return issues


def find_stale_reserved_slots(
    slots: list[dict],
    *,
    now: datetime,
    stale_minutes: int = RESERVED_STALE_MINUTES,
) -> list[dict]:
    """Return RESERVED slots older than stale_minutes, with deterministic ages."""
    stale_reserved: list[dict] = []
    now = DriftPilotClock().to_et(now).astimezone(timezone.utc)
    for slot in slots:
        if slot.get("status") != "RESERVED":
            continue
        try:
            updated = datetime_from_storage(slot["updated_at"])
            age_min = (now - updated.astimezone(timezone.utc)).total_seconds() / 60
        except (KeyError, TypeError, ValueError):
            age_min = float("inf")
        if age_min > stale_minutes:
            stale_reserved.append(
                {
                    "slot_id": slot["slot_id"],
                    "symbol": slot.get("symbol"),
                    "age_minutes": -1 if age_min == float("inf") else round(age_min, 1),
                }
            )
    return stale_reserved


def decide_premarket_clean(
    *,
    now: datetime,
    operator_alive: bool,
    local_open_position_count: int,
    broker_flat_confirmed: bool | None,
    clock: DriftPilotClock | None = None,
) -> PremarketCleanDecision:
    """Allow premarket cleanup only when every safety predicate is explicit."""
    clock = clock or DriftPilotClock()
    now_et = clock.to_et(now)
    market_open = datetime_time(9, 30)
    if now_et.time() >= market_open:
        return PremarketCleanDecision(False, "refused: not before 09:30 ET")
    if operator_alive:
        return PremarketCleanDecision(False, "refused: operator is alive")
    if local_open_position_count != 0:
        return PremarketCleanDecision(False, "refused: local open positions exist")
    if broker_flat_confirmed is not True:
        return PremarketCleanDecision(
            False,
            "refused: broker-flat confirmation was not explicitly true",
        )
    return PremarketCleanDecision(
        True,
        "allowed: premarket, operator dead, local flat, broker flat",
    )


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def get_slot_states() -> list[dict]:
    """Read all slot states from SQLite."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT slot_id, status, symbol, position_id, reserved_order_id, "
            "slot_value, updated_at, metadata_json FROM slots ORDER BY slot_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_open_positions() -> list[dict]:
    """Read today's open positions."""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, symbol, slot_id, status, quantity, opened_at, closed_at "
            "FROM positions WHERE status = 'open' ORDER BY opened_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_today_position_stats() -> dict:
    """Summary stats for today's trading."""
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(
            "SELECT count(*) as total, "
            "sum(case when status='open' then 1 else 0 end) as open_count, "
            "sum(case when status='closed' then 1 else 0 end) as closed_count "
            "FROM positions WHERE date(opened_at) = date('now')"
        ).fetchone()
        return {
            "total_today": row[0],
            "open": row[1],
            "closed": row[2],
        }
    finally:
        conn.close()


def get_operator_pid() -> int | None:
    """Read the operator PID from the PID file."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        # Check if process is alive
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def get_recent_log_lines(n: int = 30) -> str:
    """Read the last N lines from today's operator log."""
    today = datetime.now().strftime("%Y%m%d")
    log_path = LOG_DIR / f"operator_{today}.log"
    if not log_path.exists():
        return "(no log file for today)"
    try:
        lines = log_path.read_text().splitlines()
        return "\n".join(lines[-n:])
    except Exception as exc:
        return f"(error reading log: {exc})"


def get_log_freshness() -> float:
    """Seconds since the operator log was last modified."""
    today = datetime.now().strftime("%Y%m%d")
    log_path = LOG_DIR / f"operator_{today}.log"
    if not log_path.exists():
        return 9999.0
    try:
        mtime = log_path.stat().st_mtime
        return time.time() - mtime
    except Exception:
        return 9999.0


def get_recent_errors(n: int = 10) -> str:
    """Extract recent ERROR/Traceback lines from the operator log."""
    today = datetime.now().strftime("%Y%m%d")
    log_path = LOG_DIR / f"operator_{today}.log"
    if not log_path.exists():
        return "(no log)"
    try:
        lines = log_path.read_text().splitlines()
        error_lines = []
        for i, line in enumerate(lines):
            if "ERROR" in line or "Traceback" in line or "Exception" in line:
                # Include the error line plus up to 2 context lines
                context = lines[i : i + 3]
                error_lines.append("\n".join(context))
        return "\n---\n".join(error_lines[-n:]) if error_lines else "(no errors)"
    except Exception as exc:
        return f"(error reading log: {exc})"


def get_catalyst_stats() -> dict:
    """Quick stats on today's catalyst events + 4-hour window health."""
    if not CATALYST_DB.exists():
        return {}
    conn = sqlite3.connect(str(CATALYST_DB))
    try:
        # Today's totals
        row = conn.execute(
            "SELECT count(*) as total, "
            "sum(case when sentiment='positive' then 1 else 0 end) as positive, "
            "sum(case when sentiment='negative' then 1 else 0 end) as negative, "
            "sum(case when sentiment='neutral' then 1 else 0 end) as neutral, "
            "sum(case when sentiment IS NULL then 1 else 0 end) as unenriched "
            "FROM catalyst_events WHERE date(event_ts) = date('now')"
        ).fetchone()
        # 4-hour window (what the signals actually use)
        cutoff_4h = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        row_4h = conn.execute(
            "SELECT count(*) as total, "
            "count(DISTINCT symbol) as symbols, "
            "sum(case when sentiment IN ('positive','bullish') then 1 else 0 end) as tradeable "
            "FROM catalyst_events WHERE event_ts >= ?",
            (cutoff_4h,),
        ).fetchone()
        # Newest event age — shows if the pool is going stale
        newest = conn.execute(
            "SELECT max(event_ts) FROM catalyst_events WHERE sentiment IN ('positive','bullish')"
        ).fetchone()
        newest_age_min = None
        if newest and newest[0]:
            try:
                newest_dt = datetime.fromisoformat(newest[0].replace("Z", "+00:00"))
                newest_age_min = round(
                    (datetime.now(timezone.utc) - newest_dt).total_seconds() / 60,
                    1,
                )
            except Exception:
                pass
        return {
            "today_total": row[0],
            "today_positive": row[1],
            "today_negative": row[2],
            "today_neutral": row[3],
            "today_unenriched": row[4],
            "active_4h_events": row_4h[0],
            "active_4h_symbols": row_4h[1],
            "active_4h_tradeable": row_4h[2],
            "newest_positive_age_minutes": newest_age_min,
            "pool_warning": "STALE" if (newest_age_min and newest_age_min > 180) else "OK",
        }
    finally:
        conn.close()


def get_trade_rejection_summary() -> dict:
    """Count today's trades per symbol to detect exhaustion."""
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT symbol, COUNT(*) as cnt FROM positions "
            "WHERE date(opened_at) = date('now') "
            "GROUP BY symbol ORDER BY cnt DESC"
        ).fetchall()
        symbol_counts = {r[0]: r[1] for r in rows}
        # How many symbols have hit the cap?
        cap = 5  # max_trades_per_symbol_per_day
        exhausted = {s: c for s, c in symbol_counts.items() if c >= cap}
        # P&L summary
        pnl_row = conn.execute(
            "SELECT sum(realized_pnl), count(*) FROM positions "
            "WHERE date(opened_at) = date('now') AND status='closed'"
        ).fetchone()
        return {
            "total_trades_today": sum(symbol_counts.values()),
            "unique_symbols_traded": len(symbol_counts),
            "symbols_at_daily_cap": exhausted,
            "realized_pnl_today": round(pnl_row[0] or 0, 2) if pnl_row else 0,
            "closed_trades_today": pnl_row[1] if pnl_row else 0,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Health report builder
# ---------------------------------------------------------------------------

def build_health_report() -> dict:
    """Collect all system state into a single health report."""
    clock = DriftPilotClock()
    now = clock.now_utc()
    slots = get_slot_states()
    positions = get_open_positions()
    pos_stats = get_today_position_stats()
    operator_pid = get_operator_pid()
    log_freshness = get_log_freshness()
    catalyst = get_catalyst_stats()
    trade_summary = get_trade_rejection_summary()

    # Classify slots
    slot_summary = {"OPEN": 0, "EMPTY": 0, "RESERVED": 0, "other": 0}
    for s in slots:
        status = s.get("status", "unknown")
        slot_summary[status] = slot_summary.get(status, 0) + 1
    stale_reserved = find_stale_reserved_slots(slots, now=now)
    inventory_issues = find_slot_inventory_issues(slots, positions)

    return {
        "timestamp": now.isoformat(),
        "operator": {
            "pid": operator_pid,
            "alive": operator_pid is not None,
            "log_freshness_seconds": round(log_freshness, 1),
            "log_stale": log_freshness > 120,
        },
        "slots": {
            "total": len(slots),
            "summary": slot_summary,
            "details": [
                {
                    "id": s["slot_id"],
                    "status": s["status"],
                    "symbol": s.get("symbol"),
                    "position_id": s.get("position_id"),
                }
                for s in slots
            ],
            "stale_reserved": stale_reserved,
            "inventory_issues": [
                {
                    "code": issue.code,
                    "severity": issue.severity,
                    "message": issue.message,
                    "slot_id": issue.slot_id,
                    "position_id": issue.position_id,
                }
                for issue in inventory_issues
            ],
        },
        "positions": {
            "open_count": len(positions),
            "open_symbols": [p["symbol"] for p in positions],
            "today_stats": pos_stats,
        },
        "catalyst": catalyst,
        "trade_summary": trade_summary,
    }


# ---------------------------------------------------------------------------
# Action helpers
# ---------------------------------------------------------------------------


def _recycle_slot(slot_id: int, reason: str) -> None:
    """Reset a stuck RESERVED slot to EMPTY."""
    now_iso = datetime_to_storage(DriftPilotClock().now_utc())
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute(
            "UPDATE slots SET status='EMPTY', symbol=NULL, position_id=NULL, "
            "reserved_order_id=NULL, metadata_json='{}', updated_at=? "
            "WHERE slot_id=? AND status='RESERVED'",
            (now_iso, slot_id),
        )
        conn.commit()
        if cur.rowcount > 0:
            logger.info(
                "♻️  recycled slot %d (RESERVED → EMPTY): %s", slot_id, reason
            )
        else:
            logger.warning("recycle_slot(%d): no RESERVED slot found", slot_id)
    finally:
        conn.close()


def _premarket_clean_slots(reason: str) -> int:
    """Clear only non-active premarket inventory after external flat confirmation."""
    now_iso = datetime_to_storage(DriftPilotClock().now_utc())
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute(
            "UPDATE slots SET status='EMPTY', symbol=NULL, position_id=NULL, "
            "reserved_order_id=NULL, metadata_json='{}', updated_at=? "
            "WHERE status IN ('RESERVED')",
            (now_iso,),
        )
        conn.commit()
        if cur.rowcount:
            logger.warning(
                "premarket clean reset %d RESERVED slot(s) to EMPTY: %s",
                cur.rowcount,
                reason,
            )
        else:
            logger.info("premarket clean found no RESERVED slots to reset")
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Hardcoded safety checks (run even when Qwen is unreachable)
# ---------------------------------------------------------------------------

def run_safety_checks(health: dict) -> int:
    """Deterministic safety checks that don't need Qwen."""
    actions_taken = 0

    # 1. Recycle stale RESERVED slots (>5 min old)
    for stale in health["slots"]["stale_reserved"]:
        slot_id = stale["slot_id"]
        age = stale["age_minutes"]
        logger.info(
            "safety check: slot %d stuck RESERVED for %.1f min (symbol=%s)",
            slot_id, age, stale.get("symbol"),
        )
        _recycle_slot(slot_id, f"stale {age:.0f}min (safety check)")
        actions_taken += 1

    # 2. Warn on active-slot anomalies. These are never auto-mutated here.
    for issue in health["slots"].get("inventory_issues", []):
        logger.warning("slot inventory anomaly [%s]: %s", issue["code"], issue["message"])
        actions_taken += 1

    # 3. Alert if operator is dead
    if not health["operator"]["alive"]:
        logger.error(
            "🚨 operator process is DEAD (PID file: %s)",
            PID_FILE,
        )
        actions_taken += 1

    # 4. Alert if log is stale (>3 min without output)
    if health["operator"]["alive"] and health["operator"]["log_stale"]:
        logger.warning(
            "⚠️  operator log stale (%.0fs since last write) — may be hung",
            health["operator"]["log_freshness_seconds"],
        )
        actions_taken += 1

    return actions_taken


def run_premarket_clean(*, broker_flat_confirmed: bool | None) -> bool:
    """Run the guarded premarket cleanup action if all predicates allow it."""
    health = build_health_report()
    decision = decide_premarket_clean(
        now=datetime_from_storage(health["timestamp"]),
        operator_alive=bool(health["operator"]["alive"]),
        local_open_position_count=int(health["positions"]["open_count"]),
        broker_flat_confirmed=broker_flat_confirmed,
    )
    if not decision.allowed:
        logger.error("premarket clean %s", decision.reason)
        return False
    cleaned = _premarket_clean_slots(decision.reason)
    logger.info("premarket clean completed (%d slot(s) reset)", cleaned)
    return True


def _parse_explicit_bool(value: str | None) -> bool | None:
    """Parse CLI confirmation while preserving missing as None."""
    if value is None:
        return None
    return value.lower() == "true"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once() -> dict:
    """Single health check cycle. Returns the health report."""
    health = build_health_report()
    slot_summary = health["slots"]["summary"]

    ts = health.get("trade_summary", {})
    cat = health.get("catalyst", {})
    logger.info(
        "health check: slots=%d/%d open, %d reserved, %d empty | "
        "positions=%d open | operator=%s | log_age=%.0fs | "
        "pnl=$%.2f (%d trades) | catalyst_pool=%d symbols (%s) | "
        "exhausted_symbols=%d",
        slot_summary.get("OPEN", 0),
        health["slots"]["total"],
        slot_summary.get("RESERVED", 0),
        slot_summary.get("EMPTY", 0),
        health["positions"]["open_count"],
        "alive" if health["operator"]["alive"] else "DEAD",
        health["operator"]["log_freshness_seconds"],
        ts.get("realized_pnl_today", 0),
        ts.get("closed_trades_today", 0),
        cat.get("active_4h_symbols", 0),
        cat.get("pool_warning", "?"),
        len(ts.get("symbols_at_daily_cap", {})),
    )

    # Always run deterministic safety checks first
    safety_actions = run_safety_checks(health)

    if safety_actions == 0:
        logger.debug("✅ all clear — no corrective actions needed")

    return health


def main() -> None:
    parser = argparse.ArgumentParser(description="DriftPilot Slot Manager")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon")
    parser.add_argument("--once", action="store_true", help="Single check then exit")
    parser.add_argument(
        "--premarket-clean",
        action="store_true",
        help="Reset stale premarket RESERVED slots only when all safety gates pass",
    )
    parser.add_argument(
        "--broker-flat-confirmed",
        choices=["true", "false"],
        help="Explicit broker-flat confirmation required by --premarket-clean",
    )
    parser.add_argument(
        "--interval", type=int, default=POLL_INTERVAL,
        help=f"Seconds between checks (default: {POLL_INTERVAL})",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    # Logging setup
    handlers: list[logging.Handler] = []
    if args.daemon:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(MANAGER_LOG))
        fh.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
        ))
        handlers.append(fh)
    else:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        handlers.append(sh)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        handlers=handlers,
        force=True,
    )

    logger.info(
        "Slot Manager starting (interval=%ds, db=%s)",
        args.interval, DB_PATH,
    )

    if args.premarket_clean:
        run_premarket_clean(
            broker_flat_confirmed=_parse_explicit_bool(args.broker_flat_confirmed)
        )
        return

    if args.once:
        run_once()
        return

    # Graceful shutdown
    running = True

    def _handle_signal(signum, frame):
        nonlocal running
        logger.info("Received signal %d — shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while running:
        try:
            run_once()
        except Exception:
            logger.exception("Unhandled error in health check cycle")
        # Sleep in small increments so we can respond to signals
        deadline = time.monotonic() + args.interval
        while running and time.monotonic() < deadline:
            time.sleep(min(5, deadline - time.monotonic()))

    logger.info("Slot Manager stopped")


if __name__ == "__main__":
    main()
