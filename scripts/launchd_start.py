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


def _today_logfile() -> Path:
    return LOGDIR / f"operator_{datetime.now(ET).strftime('%Y%m%d')}.log"


def log(msg: str) -> None:
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(_today_logfile(), "a") as f:
        f.write(line + "\n")


def _is_driftpilot_process(pid: int) -> bool:
    """Check that a PID belongs to a DriftPilot-related Python process."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        cmd = result.stdout.strip().lower()
        return "python" in cmd and ("driftpilot" in cmd or "uvicorn" in cmd or "catalyst" in cmd)
    except Exception:
        return False


def kill_stale() -> None:
    """Kill stale operator/dashboard processes (with process-name safety check)."""
    for pidfile in ["operator.pid", "slot_manager.pid", "catalyst_refresh.pid"]:
        pf = LOGDIR / pidfile
        if pf.exists():
            try:
                pid = int(pf.read_text().strip())
                if _is_driftpilot_process(pid):
                    os.kill(pid, signal.SIGTERM)
                    log(f"killed stale {pidfile} PID={pid}")
                else:
                    log(f"skipped {pidfile} PID={pid} — not a driftpilot process")
            except (ProcessLookupError, ValueError):
                pass
            pf.unlink(missing_ok=True)

    # Kill stale dashboard
    try:
        result = subprocess.run(
            ["lsof", "-ti", ":8501"], capture_output=True, text=True
        )
        for pid_str in result.stdout.strip().split():
            if pid_str:
                pid = int(pid_str)
                if _is_driftpilot_process(pid):
                    os.kill(pid, signal.SIGTERM)
                    log(f"killed stale dashboard PID={pid}")
                else:
                    log(f"skipped port-8501 PID={pid} — not a driftpilot process")
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


def _is_market_holiday() -> bool:
    """Check if today is a US market holiday via Alpaca calendar API."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    try:
        result = subprocess.run(
            [PYTHON, "-c", f"""
import sys; sys.path.insert(0, 'src')
from driftpilot.settings import load_settings
from alpaca.trading.client import TradingClient
s = load_settings()
c = TradingClient(s.alpaca_key_id, s.alpaca_secret_key, url_override=s.alpaca_base_url)
cal = c.get_calendar(filters=None)
dates = {{str(d.date) for d in cal}}
print('OPEN' if '{today}' in dates else 'CLOSED')
"""],
            cwd=str(PROJECT), capture_output=True, text=True, timeout=30,
        )
        return "CLOSED" in result.stdout
    except Exception as e:
        log(f"holiday check failed ({e}) — assuming market open")
        return False


def _subprocess_env() -> dict[str, str]:
    """Standard environment for all child processes."""
    return {**os.environ, "PYTHONPATH": "src"}


def prewarm_catalysts() -> None:
    """Pre-warm catalyst DB with 2 weeks of news."""
    from datetime import timedelta
    now_et = datetime.now(ET)
    start = (now_et - timedelta(days=14)).strftime("%Y-%m-%d")
    end = now_et.strftime("%Y-%m-%d")
    log(f"pre-warming catalyst DB: {start} to {end}")
    logfile = _today_logfile()
    try:
        with open(logfile, "a") as lf:
            subprocess.run(
                [PYTHON, "scripts/load_2024_catalyst_events.py",
                 "--start", start, "--end", end,
                 "--output", "data/driftpilot/catalyst_events.sqlite3"],
                cwd=str(PROJECT), timeout=300,
                stdout=lf, stderr=subprocess.STDOUT,
            )
        log("catalyst pre-warm done")
    except Exception as e:
        log(f"WARNING: catalyst pre-warm failed: {e}")


def enrich_catalysts() -> None:
    """Enrich with Qwen sentiment."""
    log("enriching with Qwen sentiment")
    logfile = _today_logfile()
    try:
        with open(logfile, "a") as lf:
            subprocess.run(
                [PYTHON, "scripts/enrich_catalyst_events.py",
                 "--db", "data/driftpilot/catalyst_events.sqlite3",
                 "--priority-only", "--concurrency", "32"],
                cwd=str(PROJECT), timeout=300,
                stdout=lf, stderr=subprocess.STDOUT,
            )
        log("Qwen enrichment done")
    except Exception as e:
        log(f"WARNING: Qwen enrichment failed: {e}")


def start_dashboard() -> int:
    """Start dashboard on port 8501 (detached from parent)."""
    log("starting dashboard on :8501")
    env = _subprocess_env()
    dash_log = open(LOGDIR / "dashboard.log", "a")
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "trading_bot.dashboard.app:app",
         "--host", "0.0.0.0", "--port", "8501"],
        cwd=str(PROJECT), env=env,
        stdout=dash_log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    dash_log.close()  # parent doesn't need the handle; child inherited it
    log(f"dashboard started PID={proc.pid}")
    return proc.pid


def start_operator() -> int:
    """Start the operator (services_live), detached from parent."""
    logfile = _today_logfile()
    log("launching operator (services_live)")
    env = _subprocess_env()
    op_log = open(logfile, "a")
    proc = subprocess.Popen(
        [PYTHON, "-u", "-m", "driftpilot.operator", "--paper-live"],
        cwd=str(PROJECT), env=env,
        stdout=op_log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    op_log.close()
    (LOGDIR / "operator.pid").write_text(str(proc.pid))
    log(f"operator launched PID={proc.pid}")
    return proc.pid


def start_catalyst_refresh() -> int:
    """Start mid-day catalyst refresh loop, detached from parent."""
    log("launching catalyst refresh loop")
    logfile = _today_logfile()
    env = _subprocess_env()
    cat_log = open(logfile, "a")
    proc = subprocess.Popen(
        ["/bin/bash", "scripts/midday_catalyst_refresh.sh", "--loop", "5400"],
        cwd=str(PROJECT), env=env,
        stdout=cat_log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    cat_log.close()
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

    # Check for market holidays
    if _is_market_holiday():
        log("today is a market holiday — skipping startup")
        return

    # Pre-warm and enrich (best effort)
    prewarm_catalysts()
    enrich_catalysts()

    # Launch processes
    start_dashboard()
    start_operator()
    start_catalyst_refresh()

    log("=== all processes launched ===")


if __name__ == "__main__":
    main()
