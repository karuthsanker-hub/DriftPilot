#!/usr/bin/env bash
# Recurring deploy: push code changes to DGX after a `git push`.
#
# Use this AFTER `migrate_to_dgx.sh` has done the one-time bootstrap.
# This script:
#   1. git fetch + checkout the requested branch on DGX (default:
#      refactor/driftpilot-operator).
#   2. pip install -e ".[test]" to pick up any new deps.
#   3. Smoke test: import registry + run pytest.
#
# It does NOT re-rsync the Databento cache or .env (those rarely change).
# Pass --with-env to push .env, --with-cache to push the cache, --with-all
# for both. Pass --branch <name> to deploy a different branch.
#
# Typical usage on Mac:
#   git push origin refactor/driftpilot-operator
#   bash scripts/deploy_to_dgx.sh
#
# Roughly 30-60 seconds for code-only deploys.

set -euo pipefail

DGX_USER="sankerkr"
DGX_HOST="192.168.1.166"
DGX_REPO_REL="driftpilot"
DEFAULT_BRANCH="refactor/driftpilot-operator"
MAC_REPO_DEFAULT="$(cd "$(dirname "$0")/.." && pwd)"

WITH_ENV=0
WITH_CACHE=0
BRANCH="${DEFAULT_BRANCH}"
MAC_REPO="${MAC_REPO_DEFAULT}"

usage() {
    cat <<EOF
Usage: $0 [--branch <name>] [--with-env] [--with-cache] [--with-all]

  --branch <name>   git branch to deploy on DGX (default: ${DEFAULT_BRANCH})
  --with-env        also scp the .env file (gitignored secrets)
  --with-cache      also rsync the Databento cache (1.7 GB)
  --with-all        shorthand for --with-env --with-cache
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        --with-env) WITH_ENV=1; shift ;;
        --with-cache) WITH_CACHE=1; shift ;;
        --with-all) WITH_ENV=1; WITH_CACHE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

echo "===================================================="
echo "DriftPilot deploy → DGX"
echo "  Target:    ${DGX_USER}@${DGX_HOST}:~/${DGX_REPO_REL}"
echo "  Branch:    ${BRANCH}"
echo "  .env push: $([ ${WITH_ENV} -eq 1 ] && echo yes || echo skip)"
echo "  cache:     $([ ${WITH_CACHE} -eq 1 ] && echo yes \(1.7 GB\) || echo skip)"
echo "===================================================="
echo

# ---------- 1. git pull + pip install on DGX ----------
echo "[1/${WITH_ENV}+${WITH_CACHE}+1] git pull + pip install on DGX..."
ssh "${DGX_USER}@${DGX_HOST}" bash -s <<REMOTE_DEPLOY
set -euo pipefail
REPO_DIR="\${HOME}/${DGX_REPO_REL}"
BRANCH="${BRANCH}"

if [ ! -d "\${REPO_DIR}/.git" ]; then
    echo "FATAL: \${REPO_DIR} is not a git repo. Run scripts/migrate_to_dgx.sh first."
    exit 1
fi

cd "\${REPO_DIR}"
echo "  fetching..."
git fetch origin --quiet
echo "  checking out \${BRANCH}..."
git checkout "\${BRANCH}"
echo "  pulling..."
git pull --ff-only origin "\${BRANCH}"
echo "  HEAD: \$(git log -1 --oneline)"

if [ ! -d ".venv" ]; then
    echo "  no venv; creating..."
    python3 -m venv .venv
fi
echo "  pip install -e .[test] (picks up new deps if pyproject.toml changed)..."
.venv/bin/pip install --quiet --upgrade pip wheel
.venv/bin/pip install --quiet -e ".[test]"
echo "  ok"
REMOTE_DEPLOY

# ---------- 2. (optional) push .env ----------
if [ "${WITH_ENV}" -eq 1 ]; then
    echo
    echo "[+] scp .env (chmod 600 on DGX)..."
    if [ ! -f "${MAC_REPO}/.env" ]; then
        echo "  WARN: ${MAC_REPO}/.env missing on Mac — skipping."
    else
        scp -q "${MAC_REPO}/.env" "${DGX_USER}@${DGX_HOST}:${DGX_REPO_REL}/.env"
        ssh "${DGX_USER}@${DGX_HOST}" "chmod 600 ${DGX_REPO_REL}/.env && \
            echo '  .env updated. keys: '\$(grep -c -E '^[A-Z_]+=' ${DGX_REPO_REL}/.env)"
    fi
fi

# ---------- 3. (optional) rsync cache ----------
if [ "${WITH_CACHE}" -eq 1 ]; then
    echo
    echo "[+] rsync Databento cache (incremental)..."
    rsync -avz --partial --progress \
        "${MAC_REPO}/data/bars/databento/" \
        "${DGX_USER}@${DGX_HOST}:${DGX_REPO_REL}/data/bars/databento/"
fi

# ---------- 4. Smoke test ----------
echo
echo "[*] Smoke test on DGX..."
ssh "${DGX_USER}@${DGX_HOST}" bash <<'REMOTE_SMOKE'
set -euo pipefail
cd ~/driftpilot
export PYTHONPATH=src

echo "  --- registry ---"
.venv/bin/python3 -c "
from driftpilot.signals import list_signals
sigs = list_signals()
print(f'  registered signals: {sigs}')
"

echo "  --- pytest ---"
.venv/bin/python3 -m pytest -q 2>&1 | tail -3
REMOTE_SMOKE

echo
echo "===================================================="
echo "DEPLOY COMPLETE"
echo "===================================================="
