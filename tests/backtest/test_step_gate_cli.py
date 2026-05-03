"""Tests for the Step-Gate CLI flag (Locked Integration Refactor v1.1, Phase 5.1)."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

from driftpilot.backtest.__main__ import main
from driftpilot.backtest.baseline_lookup import (
    find_latest_baseline_report,
    read_edge_ratio,
)


SIGNAL_NAME = "intraday_momentum_v1"


def _write_report(path: Path, *, edge_ratio: float | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"signal": {"name": SIGNAL_NAME}}
    if edge_ratio is not None:
        payload["headline_metrics"] = {"edge_ratio": edge_ratio}
    path.write_text(json.dumps(payload))


def _argv(reports_root: Path, *extra: str) -> list[str]:
    return [
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-31",
        "--reports-root",
        str(reports_root),
        *extra,
    ]


def test_sweep_aborts_when_no_baseline_exists(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports_root = tmp_path / "reports"
    reports_root.mkdir()

    exit_code = main(_argv(reports_root, "--sweep"))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "No baseline found" in captured.err


def test_sweep_aborts_when_edge_ratio_below_0_8(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports_root = tmp_path / "reports"
    _write_report(reports_root / SIGNAL_NAME / "20240101T000000Z_pass.json", edge_ratio=0.5)

    exit_code = main(_argv(reports_root, "--sweep"))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "edge_ratio=0.500 below 0.8 threshold" in captured.err
    assert "Sweep aborted" in captured.err
    assert "--force-sweep" in captured.err


def test_sweep_passes_gate_when_edge_ratio_above_0_8(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports_root = tmp_path / "reports"
    _write_report(reports_root / SIGNAL_NAME / "20240101T000000Z_pass.json", edge_ratio=0.9)

    exit_code = main(_argv(reports_root, "--sweep"))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Sweep gate passed (edge_ratio=0.900)" in captured.out
    assert "Sweep execution is not yet implemented." in captured.out


def test_force_sweep_bypasses_gate_with_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports_root = tmp_path / "reports"
    _write_report(reports_root / SIGNAL_NAME / "20240101T000000Z_pass.json", edge_ratio=0.5)

    exit_code = main(_argv(reports_root, "--sweep", "--force-sweep"))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "FORCED: edge_ratio=0.500 is below 0.8 threshold" in captured.out
    assert "overriding gate per --force-sweep" in captured.out
    assert "Sweep gate passed" in captured.out


def test_force_sweep_alone_is_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports_root = tmp_path / "reports"
    reports_root.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        main(_argv(reports_root, "--force-sweep"))

    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert "--force-sweep is only valid when combined with --sweep" in captured.err


def test_find_latest_baseline_picks_newest(tmp_path: Path) -> None:
    signal_dir = tmp_path / SIGNAL_NAME
    signal_dir.mkdir(parents=True)
    older = signal_dir / "20240101T000000Z_pass.json"
    newer = signal_dir / "20240601T000000Z_pass.json"
    older.write_text("{}")
    newer.write_text("{}")
    # Force older mtime to be earlier so the newest-wins assertion is robust
    # regardless of filesystem creation-order timing.
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_710_000_000, 1_710_000_000))

    latest = find_latest_baseline_report(SIGNAL_NAME, reports_root=tmp_path)

    assert latest == newer


def test_find_latest_baseline_returns_none_when_dir_missing(tmp_path: Path) -> None:
    assert find_latest_baseline_report(SIGNAL_NAME, reports_root=tmp_path) is None


def test_read_edge_ratio_handles_missing_field(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"signal": {"name": SIGNAL_NAME}}))

    assert read_edge_ratio(report) is None


def test_read_edge_ratio_finds_top_level_field(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"edge_ratio": 1.42}))

    assert read_edge_ratio(report) == pytest.approx(1.42)
