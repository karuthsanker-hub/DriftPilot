#!/usr/bin/env python3
"""LaunchD-friendly startup script for DriftPilot.

Called directly by launchd (no shell wrapper needed).
This avoids the macOS 'Operation not permitted' issue with /bin/bash.
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


def kill_stale() -> None:
    """Kill stale operator/dashboard processes."""
    for pidfile in ["operator.pid", "slot_manager.pid", "catalyst_refresh.pid"]:
        pf = LOGDIR / pidfile
        if pf.exists():
            try:
                pid = int(pf.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                log(f"killed stale {pidfile} PID={pid}")
            except (ProcessLookupError, ValueError):
                pass
            pf.unlink(missing_ok=True)

    # Kill stale dashboard
    try:
        result = subprocess.run(
            ["lsof", "-ti", ":8501"], capture_output=True, text=True
        )
        for pid in result.stdout.strip().split():
            if pid:
                os.kill(int(pid), signal.SIGTERM)
                log(f"killed stale dashboard PID={pid}")
    except Exception:
        pass


def load_env() -> None:
    """Load .env file into environment."""
    env_file = PROJECT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ[key.strip()] = val.strip().strip("'\"")
        log("loaded .env")


def prewarm_catalysts() -> None:
    """Pre-warm catalyst DB with 2 weeks of news."""
    from datetime import timedelta
    now_et = datetime.now(ET)
    start = (now_et - timedelta(days=14)).strftime("%Y-%m-%d")
    end = now_et.strftime("%Y-%m-%d")
    log(f"pre-warming catalyst DB: {start} to {end}")
    try:
        subprocess.run(
            [PYTHON, "scripts/load_2024_catalyst_events.py",
             "--start", start, "--end", end,
             "--output", "data/driftpilot/catalyst_events.sqlite3"],
            cwd=str(PROJECT), timeout=300,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log("catalyst pre-warm done")
    except Exception as e:
        log(f"WARNING: catalyst pre-warm failed: {e}")


def enrich_catalysts() -> None:
    """Enrich with Qwen sentiment."""
    log("enriching with Qwen sentiment")
    try:
        subprocess.run(
            [PYTHON, "scripts/enrich_catalyst_events.py",
             "--db", "data/driftpilot/catalyst_events.sqlite3",
             "--priority-only", "--concurrency", "32"],
            cwd=str(PROJECT), timeout=300,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log("Qwen enrichment done")
    except Exception as e:
        log(f"WARNING: Qwen enrichment failed: {e}")


def start_dashboard() -> int:
    """Start dashboard on port 8501 (detached from parent)."""
    log("starting dashboard on :8501")
    env = {**os.environ, "PYTHONPATH": "src"}
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "trading_bot.dashboard.app:app",
         "--host", "0.0.0.0", "--port", "8501"],
        cwd=str(PROJECT), env=env,
        stdout=open(LOGDIR / "dashboard.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach from parent process group
    )
    log(f"dashboard started PID={proc.pid}")
    return proc.pid


def start_operator() -> int:
    """Start the operator (services_live), detached from parent."""
    logdate = datetime.now(ET).strftime("%Y%m%d")
    logfile = LOGDIR / f"operator_{logdate}.log"
    log("launching operator (services_live)")
    proc = subprocess.Popen(
        [PYTHON, "-u", "-m", "driftpilot.operator", "--paper-live"],
        cwd=str(PROJECT),
        stdout=open(logfile, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach from parent process group
    )
    (LOGDIR / "operator.pid").write_text(str(proc.pid))
    log(f"operator launched PID={proc.pid}")
    return proc.pid


def start_catalyst_refresh() -> int:
    """Start mid-day catalyst refresh loop, detached from parent."""
    log("launching catalyst refresh loop")
    proc = subprocess.Popen(
        ["/bin/bash", "scripts/midday_catalyst_refresh.sh", "--loop", "5400"],
        cwd=str(PROJECT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach from parent process group
    )
    (LOGDIR / "catalyst_refresh.pid").write_text(str(proc.pid))
    log(f"catalyst refresh launched PID={proc.pid}")
    return proc.pid


def main() -> None:
    os.chdir(str(PROJECT))
    LOGDIR.mkdir(exist_ok=True)
    (LOGDIR / "archive").mkdir(exist_ok=True)

    log("=== launchd_start.py starting ===")

    load_env()
    kill_stale()

    # Pre-warm and enrich (best effort)
    prewarm_catalysts()
    enrich_catalysts()

    # Launch processes
    start_dashboard()
    op_pid = start_operator()
    start_catalyst_refresh()

    log("=== all processes launched ===")


if __name__ == "__main__":
    main()
