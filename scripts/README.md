# scripts/

Operational scripts for the DriftPilot project.

| Script | When to run | What it does |
|---|---|---|
| `pull_databento_2024.sh` | One-time, when populating the bar cache. | Pulls 1500-symbol × 2024 1-min bars from Databento into `data/bars/databento/{SYMBOL}/2024.parquet`. Aborts before spend if estimate exceeds `--max-cost 70` (dry-run was $62). |
| `migrate_to_dgx.sh` | Once, the first time you deploy to DGX. | Clones the repo on DGX, builds venv, installs deps, `rsync`s the 1.7 GB Databento cache, `scp`s the `.env`, runs the smoke test. |
| `deploy_to_dgx.sh` | Every time you push code changes. | `git pull` + `pip install -e .[test]` + smoke test on DGX. Code-only by default. Pass `--with-env`, `--with-cache`, or `--with-all` to also push gitignored data. ~30-60 sec for code-only. |
| `databento_pull.py` | Used by `pull_databento_2024.sh`. | The actual Databento pull CLI. |

## Typical workflow

**First time (one-time bootstrap to DGX):**
```bash
bash scripts/migrate_to_dgx.sh
```

**Recurring deploys after code changes:**
```bash
git push origin refactor/driftpilot-operator
bash scripts/deploy_to_dgx.sh
```

**Deploying a different branch:**
```bash
bash scripts/deploy_to_dgx.sh --branch some/other-branch
```

**Pushing updated secrets or refreshed cache:**
```bash
bash scripts/deploy_to_dgx.sh --with-env       # .env only
bash scripts/deploy_to_dgx.sh --with-cache     # Databento cache only
bash scripts/deploy_to_dgx.sh --with-all       # both
```

## Running backtests on DGX

After a successful deploy:
```bash
ssh sankerkr@192.168.1.166
cd ~/driftpilot && mkdir -p logs && export PYTHONPATH=src
for sig in stationary_ghost_v1 whale_tail_v1 rs_drift_v1 apex_hunter_v2_2; do
  nohup .venv/bin/python3 -m driftpilot.backtest --signal $sig \
    --start 2024-01-01 --end 2024-12-31 \
    > logs/${sig}_backtest.log 2>&1 &
done
jobs
```

## Configuration

Both DGX scripts hardcode the host as `sankerkr@192.168.1.166`. Edit the
`DGX_USER` and `DGX_HOST` variables at the top of each script if your DGX
address changes. Set up SSH key auth (`ssh-copy-id sankerkr@192.168.1.166`)
to avoid being prompted for password on every run.
