#!/usr/bin/env python3
"""Slot Manager — autonomous health monitor for the DriftPilot operator.

Runs as a background daemon alongside the operator. Every POLL_INTERVAL
seconds it:
  1. Collects system state (slots, positions, logs, process health)
  2. Sends a structured health report to Qwen for analysis
  3. Executes Qwen's recommended corrective actions

Safe actions the manager can take:
  - Recycle stuck RESERVED slots → EMPTY
  - Log warnings about anomalies
  - Restart the operator process if it's dead

Actions it will NEVER take:
  - Modify OPEN slots or positions
  - Submit or cancel orders
  - Change config or signal settings

Usage:
    python scripts/slot_manager.py                # foreground
    python scripts/slot_manager.py --daemon       # background (writes to logs/)
    python scripts/slot_manager.py --once         # single check, then exit
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "driftpilot" / "operator_state.sqlite3"
CATALYST_DB = PROJECT_ROOT / "data" / "driftpilot" / "catalyst_events.sqlite3"
LOG_DIR = PROJECT_ROOT / "logs"
PID_FILE = LOG_DIR / "operator.pid"
MANAGER_LOG = LOG_DIR / "slot_manager.log"

QWEN_URL = os.environ.get("QWEN_URL", "http://192.168.1.166:8000/v1")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "Qwen/Qwen3-8B")
QWEN_TIMEOUT = 10  # seconds — manager isn't latency-critical
POLL_INTERVAL = 60  # seconds between health checks
RESERVED_STALE_MINUTES = 5  # recycle RESERVED slots older than this

logger = logging.getLogger("slot_manager")


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
            "SELECT slot_id, status, symbol, updated_at FROM slots ORDER BY slot_id"
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
            "SELECT id, symbol, status, opened_at, closed_at "
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
                newest_age_min = round((datetime.now(timezone.utc) - newest_dt).total_seconds() / 60, 1)
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
    now = datetime.now(timezone.utc)
    slots = get_slot_states()
    positions = get_open_positions()
    pos_stats = get_today_position_stats()
    operator_pid = get_operator_pid()
    log_freshness = get_log_freshness()
    catalyst = get_catalyst_stats()
    trade_summary = get_trade_rejection_summary()

    # Classify slots
    slot_summary = {"OPEN": 0, "EMPTY": 0, "RESERVED": 0, "other": 0}
    stale_reserved = []
    for s in slots:
        status = s.get("status", "unknown")
        slot_summary[status] = slot_summary.get(status, 0) + 1
        if status == "RESERVED":
            try:
                updated = datetime.fromisoformat(s["updated_at"])
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age_min = (now - updated).total_seconds() / 60
                if age_min > RESERVED_STALE_MINUTES:
                    stale_reserved.append({
                        "slot_id": s["slot_id"],
                        "symbol": s.get("symbol"),
                        "age_minutes": round(age_min, 1),
                    })
            except (ValueError, TypeError):
                stale_reserved.append({
                    "slot_id": s["slot_id"],
                    "symbol": s.get("symbol"),
                    "age_minutes": -1,
                })

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
                {"id": s["slot_id"], "status": s["status"], "symbol": s.get("symbol")}
                for s in slots
            ],
            "stale_reserved": stale_reserved,
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
# Qwen analysis
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the Slot Manager for DriftPilot, an automated trading system.
You monitor system health and recommend corrective actions.

You receive a JSON health report every 60 seconds. Analyze it and respond
with a JSON action plan.

THINGS YOU SHOULD CHECK:
1. Stuck RESERVED slots: slots stuck in RESERVED for >5 minutes need recycling
2. Operator process health: is it alive? Is the log fresh?
3. Slot utilization: are EMPTY slots not being filled when candidates exist?
4. Error patterns: recurring errors that indicate a systemic problem
5. Position balance: are positions opening and closing normally?
6. Catalyst pool health: are active_4h_tradeable events dropping? pool_warning=STALE means
   the newest positive event is >3 hours old — the engine will starve soon
7. Symbol exhaustion: symbols_at_daily_cap shows symbols that hit the max_trades_per_symbol
   cap. If many symbols are exhausted AND EMPTY slots exist, log a warning about shrinking pool
8. P&L: if realized_pnl_today is approaching -$500 (5% daily loss limit on $10k), warn early
9. Trade velocity: if closed_trades_today > 40, warn — approaching the 50/day cap

ACTIONS YOU CAN RECOMMEND:
- recycle_slot: {slot_id: N} — reset a stuck RESERVED slot to EMPTY
- restart_operator: {} — kill and restart the operator (use sparingly!)
- log_warning: {message: "..."} — log a warning for the human operator
- no_action: {} — everything looks healthy

RULES:
- NEVER recommend modifying OPEN slots or active positions
- NEVER recommend recycling a RESERVED slot younger than 5 minutes
- Only recommend restart_operator if the process is dead OR log is stale >5 min
- Be conservative: when in doubt, recommend log_warning over active intervention
- If everything looks healthy, respond with no_action

Respond JSON only. Format:
{
  "assessment": "one-line health summary",
  "issues": ["list of issues found"],
  "actions": [{"type": "action_type", "params": {...}, "reason": "why"}]
}
"""


def ask_qwen(health_report: dict) -> dict | None:
    """Send the health report to Qwen and get action recommendations."""
    url = f"{QWEN_URL.rstrip('/')}/chat/completions"
    user_content = json.dumps(health_report, indent=2, default=str)

    body = {
        "model": QWEN_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content + "\n/no_think"},
        ],
        "max_tokens": 512,
        "temperature": 0.0,
    }

    try:
        with httpx.Client(timeout=QWEN_TIMEOUT) as client:
            resp = client.post(url, json=body)
            resp.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("Qwen timeout — skipping AI analysis this cycle")
        return None
    except httpx.HTTPError as exc:
        logger.warning("Qwen HTTP error: %s — skipping AI analysis", exc)
        return None

    raw = resp.json()["choices"][0]["message"]["content"]

    # Strip markdown fences and think blocks
    text = raw.strip()
    if text.startswith("```"):
        text = text[text.index("\n") + 1 :]
    if text.endswith("```"):
        text = text[:-3].rstrip()
    if "<think>" in text:
        end = text.find("</think>")
        if end != -1:
            text = text[end + 8 :].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Qwen response not valid JSON: %s", raw[:200])
        return None


# ---------------------------------------------------------------------------
# Action executor
# ---------------------------------------------------------------------------

def execute_actions(actions: list[dict], health: dict) -> int:
    """Execute Qwen's recommended actions. Returns count of actions taken."""
    executed = 0
    for action in actions:
        action_type = action.get("type", "unknown")
        params = action.get("params", {})
        reason = action.get("reason", "")

        if action_type == "recycle_slot":
            slot_id = params.get("slot_id")
            if slot_id is None:
                logger.warning("recycle_slot missing slot_id, skipping")
                continue
            # Safety: verify it's actually stuck RESERVED
            stale_ids = {s["slot_id"] for s in health["slots"]["stale_reserved"]}
            if slot_id not in stale_ids:
                logger.warning(
                    "recycle_slot(%d) rejected — not in stale_reserved list", slot_id
                )
                continue
            _recycle_slot(slot_id, reason)
            executed += 1

        elif action_type == "restart_operator":
            if health["operator"]["alive"] and not health["operator"]["log_stale"]:
                logger.warning(
                    "restart_operator rejected — operator is alive and logging"
                )
                continue
            _restart_operator(reason)
            executed += 1

        elif action_type == "log_warning":
            msg = params.get("message", reason)
            logger.warning("[QWEN-ALERT] %s", msg)
            executed += 1

        elif action_type == "no_action":
            pass  # healthy, nothing to do

        else:
            logger.warning("Unknown action type: %s", action_type)

    return executed


def _recycle_slot(slot_id: int, reason: str) -> None:
    """Reset a stuck RESERVED slot to EMPTY."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.execute(
            "UPDATE slots SET status='EMPTY', symbol=NULL, metadata_json='{}', "
            "updated_at=? WHERE slot_id=? AND status='RESERVED'",
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


def _restart_operator(reason: str) -> None:
    """Kill stale operator and relaunch via daily_operator.sh."""
    logger.warning("🔄 restarting operator: %s", reason)

    # Kill old process
    old_pid = get_operator_pid()
    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(3)
        except ProcessLookupError:
            pass

    # Relaunch
    script = PROJECT_ROOT / "scripts" / "daily_operator.sh"
    if not script.exists():
        logger.error("Cannot restart — daily_operator.sh not found")
        return

    subprocess.Popen(
        ["bash", str(script)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("operator restart initiated via daily_operator.sh")


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

    # 2. Alert if operator is dead
    if not health["operator"]["alive"]:
        logger.error(
            "🚨 operator process is DEAD (PID file: %s)",
            PID_FILE,
        )
        actions_taken += 1

    # 3. Alert if log is stale (>3 min without output)
    if health["operator"]["alive"] and health["operator"]["log_stale"]:
        logger.warning(
            "⚠️  operator log stale (%.0fs since last write) — may be hung",
            health["operator"]["log_freshness_seconds"],
        )
        actions_taken += 1

    return actions_taken


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

    # Then ask Qwen for deeper analysis
    qwen_response = ask_qwen(health)
    qwen_actions = 0
    if qwen_response:
        assessment = qwen_response.get("assessment", "")
        issues = qwen_response.get("issues", [])
        actions = qwen_response.get("actions", [])

        if assessment:
            logger.info("🤖 Qwen assessment: %s", assessment)
        for issue in issues:
            logger.info("🤖 Qwen issue: %s", issue)
        if actions:
            qwen_actions = execute_actions(actions, health)

    if safety_actions == 0 and qwen_actions == 0:
        logger.debug("✅ all clear — no corrective actions needed")

    return health


def main() -> None:
    parser = argparse.ArgumentParser(description="DriftPilot Slot Manager")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon")
    parser.add_argument("--once", action="store_true", help="Single check then exit")
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
        "Slot Manager starting (interval=%ds, qwen=%s, db=%s)",
        args.interval, QWEN_URL, DB_PATH,
    )

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
