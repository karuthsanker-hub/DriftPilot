#!/usr/bin/env python3
"""LaunchD-friendly shutdown script for DriftPilot.

Kills all DriftPilot processes and runs EOD analysis.
"""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path("/Users/karuthsanker/Documents/Trading BOT")
PYTHON = str(PROJECT / ".venv/bin/python")
LOGDIR = PROJECT / "logs"
ET = ZoneInfo("America/New_York")


def log(msg: str) -> None:
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    logfile = LOGDIR / f"operator_{datetime.now(ET).strftime('%Y%m%d')}.log"
    with open(logfile, "a") as f:
        f.write(line + "\n")


def load_env() -> None:
    env_file = PROJECT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ[key.strip()] = val.strip().strip("'\"")


def kill_processes() -> None:
    """Kill all DriftPilot processes."""
    for pidfile in ["catalyst_refresh.pid", "slot_manager.pid", "operator.pid"]:
        pf = LOGDIR / pidfile
        if pf.exists():
            try:
                pid = int(pf.read_text().strip())
                label = pidfile.replace(".pid", "")
                os.kill(pid, signal.SIGTERM)
                log(f"stopped {label} PID={pid}")
            except (ProcessLookupError, ValueError):
                pass
            pf.unlink(missing_ok=True)

    # Kill dashboard on port 8501
    try:
        result = subprocess.run(
            ["lsof", "-ti", ":8501"], capture_output=True, text=True
        )
        for pid in result.stdout.strip().split():
            if pid:
                os.kill(int(pid), signal.SIGTERM)
                log(f"stopped dashboard PID={pid}")
    except Exception:
        pass

    # Fallback: pkill any uvicorn on 8501
    try:
        subprocess.run(
            ["pkill", "-f", "uvicorn.*8501"],
            capture_output=True, timeout=5,
        )
        log("pkill dashboard fallback executed")
    except Exception:
        pass


def run_eod_analysis() -> None:
    """Run EOD analysis (best effort)."""
    log("running EOD analysis")
    try:
        subprocess.run(
            [PYTHON, "scripts/analyze_paper_trading_day.py", "--include-alpaca-snapshot"],
            cwd=str(PROJECT), timeout=120,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log("EOD analysis done")
    except Exception as e:
        log(f"WARNING: EOD analysis failed: {e}")


def run_eod_summary() -> None:
    """Generate Qwen EOD summary (best effort)."""
    log("generating Qwen EOD summary")
    try:
        subprocess.run(
            [PYTHON, "scripts/eod_qwen_summary.py"],
            cwd=str(PROJECT), timeout=120,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log("Qwen EOD summary done")
    except Exception as e:
        log(f"WARNING: Qwen EOD summary failed: {e}")


def archive_log() -> None:
    logdate = datetime.now(ET).strftime("%Y%m%d")
    logfile = LOGDIR / f"operator_{logdate}.log"
    archive = LOGDIR / "archive"
    archive.mkdir(exist_ok=True)
    if logfile.exists():
        import shutil
        shutil.copy2(logfile, archive / logfile.name)
        log("log archived")


def main() -> None:
    os.chdir(str(PROJECT))
    log("=== launchd_stop.py ===")
    load_env()
    kill_processes()
    time.sleep(3)  # let operator finish final cycle
    run_eod_analysis()
    run_eod_summary()
    archive_log()
    log("day complete")


if __name__ == "__main__":
    main()
