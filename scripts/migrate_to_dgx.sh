#!/usr/bin/env bash
# One-shot migration: Mac → DGX Spark.
#
# Steps:
#   1. SSH to DGX, clone the repo from GitHub at the integration branch.
#   2. Set up venv + install all 19 deps from pyproject.toml + [test] extra.
#   3. rsync the Databento 2024 cache from Mac to DGX (1.7 GB).
#   4. scp the .env file (gitignored — 45+ keys including DATABENTO/ALPACA/etc).
#   5. Smoke-test: registry lists 5 signals + full pytest passes.
#
# Run from your Mac terminal. You may be prompted for password ~5 times unless
# you have key-based SSH set up to DGX.

set -euo pipefail

DGX_USER="sankerkr"
DGX_HOST="192.168.1.166"
DGX_REPO_REL="driftpilot"           # under $HOME on DGX
GIT_URL="https://github.com/karuthsanker-hub/DriftPilot.git"
GIT_BRANCH="refactor/driftpilot-operator"
MAC_REPO="/Users/karuthsanker/Documents/Trading BOT"
MAC_CACHE="${MAC_REPO}/data/bars/databento"
MAC_ENV="${MAC_REPO}/.env"

echo "===================================================="
echo "DriftPilot Mac → DGX migration"
echo "  Target:    ${DGX_USER}@${DGX_HOST}:~/${DGX_REPO_REL}"
echo "  Branch:    ${GIT_BRANCH}"
echo "  Cache:     ${MAC_CACHE}  (1.7 GB)"
echo "  Env file:  ${MAC_ENV}  (45 keys, secrets — chmod 600 on remote)"
echo "===================================================="
echo

# ---------- 1. SSH connectivity check ----------
echo "[1/5] SSH connectivity check..."
ssh -o BatchMode=no -o ConnectTimeout=10 "${DGX_USER}@${DGX_HOST}" \
    'echo "  connected as $(whoami) on $(hostname)"; \
     command -v git || { echo "FATAL: git not on DGX"; exit 1; } ; \
     command -v python3 || { echo "FATAL: python3 not on DGX"; exit 1; } ; \
     python3 -c "import sys; print(f\"  python3: {sys.version_info.major}.{sys.version_info.minor}\")"'

# ---------- 2. Clone or update repo + venv setup on DGX ----------
echo
echo "[2/5] Clone repo on DGX (or update if exists) + venv + deps..."
ssh "${DGX_USER}@${DGX_HOST}" bash -s <<REMOTE_SETUP
set -euo pipefail

REPO_DIR="\${HOME}/${DGX_REPO_REL}"
GIT_URL="${GIT_URL}"
GIT_BRANCH="${GIT_BRANCH}"

# Clone or fast-forward.
if [ -d "\${REPO_DIR}/.git" ]; then
    echo "  repo exists; fetching + checking out \${GIT_BRANCH}..."
    cd "\${REPO_DIR}"
    git fetch origin
    git checkout "\${GIT_BRANCH}"
    git pull --ff-only origin "\${GIT_BRANCH}"
else
    echo "  cloning \${GIT_URL}..."
    git clone "\${GIT_URL}" "\${REPO_DIR}"
    cd "\${REPO_DIR}"
    git checkout "\${GIT_BRANCH}"
fi

mkdir -p "\${REPO_DIR}/data/bars/databento"

# venv + deps. pip install -e .[test] pulls all 19 runtime deps from
# pyproject.toml plus pytest (the [test] extra). Skips heavy [ai] extras
# (torch/transformers) — not needed for backtests.
cd "\${REPO_DIR}"
if [ ! -d ".venv" ]; then
    echo "  creating venv..."
    python3 -m venv .venv
fi
echo "  upgrading pip + wheel..."
.venv/bin/pip install --quiet --upgrade pip wheel
echo "  installing deps from pyproject.toml + [test] extra..."
.venv/bin/pip install --quiet -e ".[test]"
echo "  installed packages:"
.venv/bin/pip list 2>&1 | grep -E '^(pandas|pyarrow|numpy|databento|pytest|alpaca-py|anthropic|fastapi|pydantic|python-dotenv) ' | sed 's/^/    /'

echo "  venv ready at \${REPO_DIR}/.venv"
REMOTE_SETUP

# ---------- 3. rsync Databento cache (Mac → DGX) ----------
echo
echo "[3/5] rsync Databento cache (1.7 GB) Mac → DGX..."
rsync -avz --partial --progress \
    "${MAC_CACHE}/" \
    "${DGX_USER}@${DGX_HOST}:${DGX_REPO_REL}/data/bars/databento/"

# ---------- 4. scp .env (secrets) with restricted perms ----------
echo
echo "[4/5] Transfer .env (gitignored, contains secrets)..."
if [ ! -f "${MAC_ENV}" ]; then
    echo "  WARN: ${MAC_ENV} missing on Mac — skipping. Backtests should still"
    echo "        work since data is cached, but live ops will need creds."
else
    scp -q "${MAC_ENV}" "${DGX_USER}@${DGX_HOST}:${DGX_REPO_REL}/.env"
    # Tighten file mode on DGX so other users can't read secrets.
    ssh "${DGX_USER}@${DGX_HOST}" "chmod 600 ${DGX_REPO_REL}/.env && \
        echo '  .env transferred, chmod 600 applied. Keys present:' && \
        grep -E '^[A-Z_]+=' ${DGX_REPO_REL}/.env | wc -l | xargs printf '  %s keys\n'"
fi

# ---------- 5. Smoke test on DGX ----------
echo
echo "[5/5] Smoke test on DGX..."
ssh "${DGX_USER}@${DGX_HOST}" bash <<'REMOTE_SMOKE'
set -euo pipefail
cd ~/driftpilot
export PYTHONPATH=src

echo "  --- registry ---"
.venv/bin/python3 -c "
from driftpilot.signals import list_signals
sigs = list_signals()
print(f'  registered signals: {sigs}')
assert len(sigs) == 5, f'expected 5 signals, got {len(sigs)}'
"

echo "  --- cache health ---"
echo "  symbol dirs: $(ls data/bars/databento 2>/dev/null | wc -l)"
test -f data/bars/databento/SPY/2024.parquet && \
    echo "  SPY/2024.parquet: present" || \
    echo "  SPY/2024.parquet: MISSING (RS-Drift + Apex Hunter need this)"

echo "  --- pytest ---"
.venv/bin/python3 -m pytest -q 2>&1 | tail -3
REMOTE_SMOKE

echo
echo "===================================================="
echo "MIGRATION COMPLETE"
echo
echo "Kick off all four backtests in parallel:"
echo "  ssh ${DGX_USER}@${DGX_HOST}"
echo "  cd ~/${DGX_REPO_REL}"
echo "  mkdir -p logs"
echo "  export PYTHONPATH=src"
echo "  for sig in stationary_ghost_v1 whale_tail_v1 rs_drift_v1 apex_hunter_v2_2; do"
echo "    nohup .venv/bin/python3 -m driftpilot.backtest --signal \\\$sig \\\\"
echo "      --start 2024-01-01 --end 2024-12-31 \\\\"
echo "      > logs/\\\${sig}_backtest.log 2>&1 &"
echo "  done"
echo "  jobs"
echo "===================================================="
