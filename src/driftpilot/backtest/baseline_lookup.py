"""Helpers for locating the most recent baseline expectancy report.

Used by the Step-Gate CLI flag (Locked Integration Refactor v1.1, Phase 5.1).
The gate reads `edge_ratio` from the latest baseline report under
`reports/<signal_name>/` and refuses to launch a sweep when the baseline is
weak. Kept in its own module so the parser can be unit-tested in isolation
and reused by future sweep-execution code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def find_latest_baseline_report(
    signal_name: str,
    reports_root: Path = Path("reports"),
) -> Path | None:
    """Return the most recently modified baseline report for ``signal_name``.

    Looks under ``reports_root/<signal_name>/`` for ``*.json`` files. Returns
    ``None`` when the directory is missing or contains no JSON reports.
    """
    signal_dir = reports_root / signal_name
    if not signal_dir.is_dir():
        return None
    candidates = [path for path in signal_dir.glob("*.json") if path.is_file()]
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def read_edge_ratio(report_path: Path) -> float | None:
    """Return the ``edge_ratio`` recorded in ``report_path``.

    Searches a small set of conventional locations (top-level, ``metrics``,
    ``headline_metrics``) so that schema evolution doesn't break the gate.
    Returns ``None`` when the field is absent or the file cannot be parsed.
    """
    try:
        payload: Any = json.loads(report_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for container in (payload, payload.get("metrics"), payload.get("headline_metrics")):
        if isinstance(container, dict) and "edge_ratio" in container:
            value = container["edge_ratio"]
            if isinstance(value, (int, float)):
                return float(value)
    return None
