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


def _today_logfile() -> Path:
    return LOGDIR / f"operator_{datetime.now(ET).strftime('%Y%m%d')}.log"


def log(msg: str) -> None:
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(_today_logfile(), "a") as f:
        f.write(line + "\n")


def _subprocess_env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": "src"}


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


def _wait_for_exit(pid: int, label: str, timeout: int = 15) -> None:
    """Wait for a process to exit after SIGTERM, with timeout."""
    for _ in range(timeout):
        try:
            os.kill(pid, 0)  # check if still alive
            time.sleep(1)
        except ProcessLookupError:
            log(f"{label} PID={pid} exited cleanly")
            return
    log(f"WARNING: {label} PID={pid} still alive after {timeout}s — sending SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def load_env() -> None:
    env_file = PROJECT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ[key.strip()] = val.strip().strip("'\"")


def kill_processes() -> None:
    """Kill all DriftPilot processes (with process-name safety check)."""
    killed_pids = []

    for pidfile in ["catalyst_refresh.pid", "slot_manager.pid", "operator.pid"]:
        pf = LOGDIR / pidfile
        if pf.exists():
            try:
                pid = int(pf.read_text().strip())
                label = pidfile.replace(".pid", "")
                if _is_driftpilot_process(pid):
                    os.kill(pid, signal.SIGTERM)
                    log(f"sent SIGTERM to {label} PID={pid}")
                    killed_pids.append((pid, label))
                else:
                    log(f"skipped {pidfile} PID={pid} — not a driftpilot process")
            except (ProcessLookupError, ValueError):
                pass
            pf.unlink(missing_ok=True)

    # Kill dashboard on port 8501
    try:
        result = subprocess.run(
            ["lsof", "-ti", ":8501"], capture_output=True, text=True
        )
        for pid_str in result.stdout.strip().split():
            if pid_str:
                pid = int(pid_str)
                if _is_driftpilot_process(pid):
                    os.kill(pid, signal.SIGTERM)
                    log(f"sent SIGTERM to dashboard PID={pid}")
                    killed_pids.append((pid, "dashboard"))
                else:
                    log(f"skipped port-8501 PID={pid} — not a driftpilot process")
    except Exception:
        pass

    # Wait for all processes to exit (up to 15s each, in parallel)
    for pid, label in killed_pids:
        _wait_for_exit(pid, label, timeout=15)


def run_eod_analysis() -> None:
    """Run EOD analysis (best effort)."""
    log("running EOD analysis")
    logfile = _today_logfile()
    env = _subprocess_env()
    try:
        with open(logfile, "a") as lf:
            subprocess.run(
                [PYTHON, "scripts/analyze_paper_trading_day.py", "--include-alpaca-snapshot"],
                cwd=str(PROJECT), env=env, timeout=120,
                stdout=lf, stderr=subprocess.STDOUT,
            )
        log("EOD analysis done")
    except Exception as e:
        log(f"WARNING: EOD analysis failed: {e}")


def run_eod_summary() -> None:
    """Generate Qwen EOD summary (best effort)."""
    log("generating Qwen EOD summary")
    logfile = _today_logfile()
    env = _subprocess_env()
    try:
        with open(logfile, "a") as lf:
            subprocess.run(
                [PYTHON, "scripts/eod_qwen_summary.py"],
                cwd=str(PROJECT), env=env, timeout=120,
                stdout=lf, stderr=subprocess.STDOUT,
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
    kill_processes()  # waits up to 15s for each process to exit
    run_eod_analysis()
    run_eod_summary()
    archive_log()
    log("day complete")


if __name__ == "__main__":
    main()
