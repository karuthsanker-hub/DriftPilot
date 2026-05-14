"""PM Analyst — periodic Qwen-powered trade analysis.

Every 15 minutes, builds a snapshot of today's trading activity,
sends it to Qwen for structured analysis, and stores the result
in SQLite for the dashboard to display.

The analysis covers:
- Session P&L and win rate
- Worst-performing symbols and why
- Stuck/zombie positions
- Signal effectiveness
- Actionable recommendations

The dashboard shows the latest analysis as a PM briefing, not raw logs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ANALYSIS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS pm_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'qwen',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT NOT NULL DEFAULT '{}'
)
"""

# Keep last 50 analyses (one per 15 min ≈ 12 hours of trading)
MAX_ROWS = 50


@dataclass
class TradingSnapshot:
    """Raw data snapshot for Qwen to analyze."""

    timestamp: str
    total_trades: int
    open_positions: int
    closed_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    total_pnl: float
    avg_win: float
    avg_loss: float
    # Per-symbol breakdown (top 10 by abs P&L)
    symbol_pnl: list[dict[str, Any]]
    # Stuck positions (held > max_hold)
    stuck_positions: list[dict[str, Any]]
    # Exit reason breakdown
    exit_reasons: dict[str, dict[str, Any]]
    # Signal breakdown
    signal_pnl: dict[str, dict[str, Any]]
    # Recent machine-gun re-entries
    rapid_reentries: list[dict[str, Any]]
    # Slot utilization
    slots_empty: int
    slots_active: int
    total_slots: int
    # Active signals
    active_signals: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "total_trades": self.total_trades,
            "open_positions": self.open_positions,
            "closed_trades": self.closed_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate_pct": self.win_rate_pct,
            "total_pnl": self.total_pnl,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "symbol_pnl": self.symbol_pnl,
            "stuck_positions": self.stuck_positions,
            "exit_reasons": self.exit_reasons,
            "signal_pnl": self.signal_pnl,
            "rapid_reentries": self.rapid_reentries,
            "slots_empty": self.slots_empty,
            "slots_active": self.slots_active,
            "total_slots": self.total_slots,
            "active_signals": self.active_signals,
        }


ANALYSIS_PROMPT_SYSTEM = """You are the Portfolio Manager for DriftPilot, an automated paper-trading system.
You receive a snapshot of today's trading data every 15 minutes.
Your job is to identify problems, flag issues, and recommend fixes.

Respond with ONLY a JSON object (no markdown, no explanation outside the JSON):
{
  "summary": "1-2 sentence overall assessment",
  "pnl_status": "winning|losing|flat",
  "issues": [
    {
      "severity": "critical|warning|info",
      "title": "Short title (under 60 chars)",
      "detail": "What's wrong and why it matters (1-2 sentences)",
      "recommendation": "Specific action to take"
    }
  ],
  "top_performers": ["SYM1", "SYM2"],
  "worst_performers": ["SYM3", "SYM4"],
  "signal_verdict": {
    "signal_name": "effective|marginal|harmful"
  },
  "stuck_position_action": "none|investigate|force_close",
  "risk_level": "low|moderate|high|critical"
}

Rules:
- Be specific. "ORCL lost $86 on 5 trades in 8 minutes" not "some symbols are losing".
- Flag zombie positions (held > 60 min) as critical.
- Flag asymmetric risk (avg loss > avg win) as warning.
- Flag signals with negative total P&L as harmful.
- Flag rapid re-entries (>3 trades on same symbol in <30 min) as warning.
- Keep issues to max 5 most important ones.
- If everything looks fine, say so. Don't invent problems."""


def _build_analysis_prompt(snapshot: TradingSnapshot) -> str:
    """Build the user prompt with today's data."""
    data = json.dumps(snapshot.to_dict(), indent=2, default=str)
    return f"""Analyze this trading session snapshot and identify issues:

{data}

Respond with the JSON analysis object only."""


class PMAnalyst:
    """Periodic trade analyst powered by Qwen."""

    def __init__(
        self,
        operator_db_path: str | Path,
        analysis_db_path: str | Path | None = None,
        qwen_url: str = "http://192.168.1.166:8000/v1",
        qwen_model: str = "Qwen/Qwen3-8B",
        qwen_timeout_ms: int = 10000,
        interval_minutes: int = 15,
    ) -> None:
        self._operator_db = str(operator_db_path)
        self._analysis_db = str(analysis_db_path or operator_db_path)
        self._qwen_url = qwen_url.rstrip("/")
        self._qwen_model = qwen_model
        self._qwen_timeout_ms = qwen_timeout_ms
        self._interval_minutes = interval_minutes
        self._last_run: float = 0
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            conn = sqlite3.connect(self._analysis_db)
            conn.execute(ANALYSIS_TABLE_DDL)
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning("pm_analyst: schema init failed: %s", exc)

    def should_run(self) -> bool:
        """Check if enough time has passed since last run."""
        return (time.time() - self._last_run) >= self._interval_minutes * 60

    def run(self) -> dict[str, Any] | None:
        """Build snapshot, call Qwen, store result. Returns the analysis dict or None."""
        try:
            snapshot = self._build_snapshot()
            if snapshot.total_trades == 0 and snapshot.open_positions == 0:
                logger.debug("pm_analyst: no trades today, skipping analysis")
                self._last_run = time.time()
                return None

            analysis = self._call_qwen(snapshot)
            if analysis is not None:
                self._store_analysis(analysis, snapshot)
                self._last_run = time.time()
                logger.info(
                    "pm_analyst: analysis complete — %s, %d issues, risk=%s",
                    analysis.get("pnl_status", "?"),
                    len(analysis.get("issues", [])),
                    analysis.get("risk_level", "?"),
                )
            return analysis
        except Exception as exc:
            logger.exception("pm_analyst: run failed: %s", exc)
            self._last_run = time.time()
            return None

    def get_latest(self) -> dict[str, Any] | None:
        """Read the most recent analysis from DB."""
        try:
            conn = sqlite3.connect(self._analysis_db)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT analysis_json, created_at, latency_ms FROM pm_analysis "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row is None:
                return None
            result = json.loads(row["analysis_json"])
            result["_meta"] = {
                "analyzed_at": row["created_at"],
                "latency_ms": row["latency_ms"],
            }
            return result
        except Exception as exc:
            logger.warning("pm_analyst: get_latest failed: %s", exc)
            return None

    def get_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Read recent analyses for trend tracking."""
        try:
            conn = sqlite3.connect(self._analysis_db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT analysis_json, created_at, latency_ms FROM pm_analysis "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
            results = []
            for row in rows:
                r = json.loads(row["analysis_json"])
                r["_meta"] = {
                    "analyzed_at": row["created_at"],
                    "latency_ms": row["latency_ms"],
                }
                results.append(r)
            return results
        except Exception:
            return []

    def _build_snapshot(self) -> TradingSnapshot:
        """Query operator DB for today's trading data."""
        conn = sqlite3.connect(self._operator_db)
        conn.row_factory = sqlite3.Row
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Basic stats
        row = conn.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) as closed,
                SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_pos,
                SUM(CASE WHEN status='closed' AND realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN status='closed' AND realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
                COALESCE(SUM(CASE WHEN status='closed' THEN realized_pnl ELSE 0 END), 0) as total_pnl,
                COALESCE(AVG(CASE WHEN status='closed' AND realized_pnl > 0 THEN realized_pnl END), 0) as avg_win,
                COALESCE(AVG(CASE WHEN status='closed' AND realized_pnl <= 0 THEN realized_pnl END), 0) as avg_loss
            FROM positions WHERE opened_at >= ?""",
            (today,),
        ).fetchone()

        total = row["total"] or 0
        closed = row["closed"] or 0
        wins = row["wins"] or 0
        losses = row["losses"] or 0

        # Per-symbol P&L (top 10 by absolute P&L)
        sym_rows = conn.execute(
            """SELECT symbol,
                COUNT(*) as trades,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) as losses,
                ROUND(SUM(realized_pnl), 2) as total_pnl
            FROM positions
            WHERE status='closed' AND opened_at >= ?
            GROUP BY symbol ORDER BY ABS(SUM(realized_pnl)) DESC LIMIT 10""",
            (today,),
        ).fetchall()
        symbol_pnl = [
            {"symbol": r["symbol"], "trades": r["trades"], "wins": r["wins"],
             "losses": r["losses"], "total_pnl": r["total_pnl"]}
            for r in sym_rows
        ]

        # Stuck positions (open > 60 min)
        stuck_rows = conn.execute(
            """SELECT symbol, entry_price, quantity,
                ROUND((julianday('now') - julianday(opened_at)) * 24 * 60, 0) as hold_min,
                json_extract(metadata_json, '$.reconciled') as reconciled
            FROM positions
            WHERE status='open' AND opened_at >= ?
            AND (julianday('now') - julianday(opened_at)) * 24 * 60 > 60""",
            (today,),
        ).fetchall()
        stuck = [
            {"symbol": r["symbol"], "hold_min": r["hold_min"],
             "reconciled": r["reconciled"], "entry_price": r["entry_price"]}
            for r in stuck_rows
        ]

        # Exit reason breakdown
        exit_rows = conn.execute(
            """SELECT exit_reason, COUNT(*) as cnt,
                ROUND(SUM(realized_pnl), 2) as total_pnl,
                ROUND(AVG(realized_pnl), 2) as avg_pnl
            FROM positions
            WHERE status='closed' AND opened_at >= ?
            GROUP BY exit_reason""",
            (today,),
        ).fetchall()
        exit_reasons = {
            r["exit_reason"]: {"count": r["cnt"], "total_pnl": r["total_pnl"], "avg_pnl": r["avg_pnl"]}
            for r in exit_rows
        }

        # Signal P&L
        sig_rows = conn.execute(
            """SELECT json_extract(metadata_json, '$.signal_name') as sig,
                COUNT(*) as trades,
                ROUND(SUM(realized_pnl), 2) as total_pnl,
                ROUND(AVG(realized_pnl), 2) as avg_pnl
            FROM positions
            WHERE status='closed' AND opened_at >= ?
            GROUP BY sig""",
            (today,),
        ).fetchall()
        signal_pnl = {
            (r["sig"] or "unknown"): {"trades": r["trades"], "total_pnl": r["total_pnl"], "avg_pnl": r["avg_pnl"]}
            for r in sig_rows
        }

        # Rapid re-entries (>3 trades same symbol in <30 min)
        rapid_rows = conn.execute(
            """SELECT symbol, COUNT(*) as trades,
                MIN(opened_at) as first_entry,
                MAX(COALESCE(closed_at, opened_at)) as last,
                ROUND((julianday(MAX(COALESCE(closed_at, opened_at))) - julianday(MIN(opened_at))) * 24 * 60, 1) as span_min,
                ROUND(SUM(realized_pnl), 2) as total_pnl
            FROM positions
            WHERE opened_at >= ? AND status='closed'
            GROUP BY symbol
            HAVING COUNT(*) > 3
              AND (julianday(MAX(COALESCE(closed_at, opened_at))) - julianday(MIN(opened_at))) * 24 * 60 < 30""",
            (today,),
        ).fetchall()
        rapid = [
            {"symbol": r["symbol"], "trades": r["trades"], "span_min": r["span_min"],
             "total_pnl": r["total_pnl"]}
            for r in rapid_rows
        ]

        # Slot utilization
        slot_rows = conn.execute("SELECT status FROM slots").fetchall()
        slots_empty = sum(1 for r in slot_rows if (r["status"] or "").upper() == "EMPTY")
        slots_active = len(slot_rows) - slots_empty

        # Active signals from runtime config
        active_signals = ""
        try:
            rc_path = Path("data/driftpilot/runtime_config.json")
            if rc_path.exists():
                rc = json.loads(rc_path.read_text())
                active_signals = rc.get("active_signal", "")
        except Exception:
            pass

        conn.close()

        return TradingSnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            total_trades=total,
            open_positions=row["open_pos"] or 0,
            closed_trades=closed,
            wins=wins,
            losses=losses,
            win_rate_pct=round(wins / closed * 100, 1) if closed > 0 else 0,
            total_pnl=round(row["total_pnl"] or 0, 2),
            avg_win=round(row["avg_win"] or 0, 2),
            avg_loss=round(row["avg_loss"] or 0, 2),
            symbol_pnl=symbol_pnl,
            stuck_positions=stuck,
            exit_reasons=exit_reasons,
            signal_pnl=signal_pnl,
            rapid_reentries=rapid,
            slots_empty=slots_empty,
            slots_active=slots_active,
            total_slots=len(slot_rows),
            active_signals=active_signals,
        )

    def _call_qwen(self, snapshot: TradingSnapshot) -> dict[str, Any] | None:
        """Send snapshot to Qwen for analysis."""
        import httpx

        user_prompt = _build_analysis_prompt(snapshot)
        # Append /no_think to suppress Qwen3's internal thinking blocks
        user_prompt += "\n/no_think"

        body = {
            "model": self._qwen_model,
            "messages": [
                {"role": "system", "content": ANALYSIS_PROMPT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 1024,
            "temperature": 0.3,
        }

        url = f"{self._qwen_url}/chat/completions"
        start = time.perf_counter()
        try:
            with httpx.Client(timeout=self._qwen_timeout_ms / 1000.0) as client:
                resp = client.post(url, json=body)
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("pm_analyst: Qwen call failed: %s", exc)
            return self._fallback_analysis(snapshot)

        latency_ms = int((time.perf_counter() - start) * 1000)
        data = resp.json()
        raw_text = data["choices"][0]["message"]["content"]

        # Strip markdown fences and thinking blocks
        text = raw_text.strip()
        if text.startswith("```"):
            text = text[text.index("\n") + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
        if "<think>" in text:
            think_end = text.find("</think>")
            if think_end != -1:
                text = text[think_end + 8:].strip()

        try:
            result = json.loads(text)
            result["_latency_ms"] = latency_ms
            return result
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("pm_analyst: Qwen JSON parse failed: %s | raw: %s", exc, raw_text[:300])
            return self._fallback_analysis(snapshot)

    def _fallback_analysis(self, snapshot: TradingSnapshot) -> dict[str, Any]:
        """Deterministic fallback when Qwen is unavailable."""
        issues = []

        # Check for stuck positions
        if snapshot.stuck_positions:
            syms = ", ".join(p["symbol"] for p in snapshot.stuck_positions)
            issues.append({
                "severity": "critical",
                "title": f"{len(snapshot.stuck_positions)} zombie position(s): {syms}",
                "detail": f"Held beyond max_hold. Likely reconciled without metadata.",
                "recommendation": "Restart operator with latest code (FAILSAFE_TIME_STOP fix) or manually close.",
            })

        # Check asymmetric risk
        if snapshot.avg_loss != 0 and abs(snapshot.avg_loss) > abs(snapshot.avg_win) * 1.2:
            issues.append({
                "severity": "warning",
                "title": f"Asymmetric risk: avg loss ${abs(snapshot.avg_loss):.0f} > avg win ${snapshot.avg_win:.0f}",
                "detail": "Losses are larger than wins. Stop loss may be too wide or slipping.",
                "recommendation": "Check stop_loss_pct config. Consider broker-side stop orders.",
            })

        # Check rapid re-entries
        for r in snapshot.rapid_reentries:
            issues.append({
                "severity": "warning",
                "title": f"Machine-gun: {r['symbol']} {r['trades']}x in {r['span_min']}min (${r['total_pnl']})",
                "detail": "Rapid re-entry on same catalyst event.",
                "recommendation": "Increase min_reentry_minutes or lower max_trades_per_symbol_per_day.",
            })

        # Check signals
        signal_verdict = {}
        for sig_name, stats in snapshot.signal_pnl.items():
            if stats["total_pnl"] < -50:
                signal_verdict[sig_name] = "harmful"
                issues.append({
                    "severity": "warning",
                    "title": f"Signal {sig_name}: ${stats['total_pnl']} on {stats['trades']} trades",
                    "detail": "Negative P&L signal is destroying value.",
                    "recommendation": f"Consider disabling {sig_name} in runtime config.",
                })
            elif stats["total_pnl"] < 0:
                signal_verdict[sig_name] = "marginal"
            else:
                signal_verdict[sig_name] = "effective"

        pnl_status = "winning" if snapshot.total_pnl > 20 else ("losing" if snapshot.total_pnl < -20 else "flat")
        risk_level = "critical" if snapshot.stuck_positions else ("high" if snapshot.total_pnl < -100 else "moderate" if snapshot.total_pnl < 0 else "low")

        # Top/worst performers
        sorted_syms = sorted(snapshot.symbol_pnl, key=lambda x: x["total_pnl"])
        worst = [s["symbol"] for s in sorted_syms[:3]]
        best = [s["symbol"] for s in sorted_syms[-3:]]

        return {
            "summary": f"Session: {snapshot.closed_trades} closed, {snapshot.wins}W/{snapshot.losses}L, "
                       f"P&L ${snapshot.total_pnl:+.2f}. {'Issues found.' if issues else 'No major issues.'}",
            "pnl_status": pnl_status,
            "issues": issues[:5],
            "top_performers": best,
            "worst_performers": worst,
            "signal_verdict": signal_verdict,
            "stuck_position_action": "force_close" if snapshot.stuck_positions else "none",
            "risk_level": risk_level,
            "_fallback": True,
        }

    def _store_analysis(self, analysis: dict[str, Any], snapshot: TradingSnapshot) -> None:
        """Persist analysis to SQLite."""
        try:
            conn = sqlite3.connect(self._analysis_db)
            conn.execute(
                "INSERT INTO pm_analysis (created_at, analysis_json, model, latency_ms, snapshot_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(analysis, default=str),
                    self._qwen_model if not analysis.get("_fallback") else "fallback",
                    analysis.get("_latency_ms", 0),
                    json.dumps(snapshot.to_dict(), default=str),
                ),
            )
            # Prune old rows
            conn.execute(
                "DELETE FROM pm_analysis WHERE id NOT IN "
                "(SELECT id FROM pm_analysis ORDER BY id DESC LIMIT ?)",
                (MAX_ROWS,),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning("pm_analyst: store failed: %s", exc)
