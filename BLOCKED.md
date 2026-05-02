# Blocked Questions

## Phase 12 Databento Backtest

- Confirm the authoritative Databento dataset for the 2024 1-minute U.S. equities replay. The implementation defaults to `EQUS.MINI` because Databento documents `ohlcv-1m` support and NMS-stock coverage, but this needs owner approval before Phase 12 can be declared complete.
- Confirm the production validation universe. The implementation seeds `config/sector_map.csv` and always includes `SPY`, but no point-in-time constituents source was provided.
- Provide `DATABENTO_API_KEY` or pre-populated Parquet files under `data/bars/databento/` so the 2024-01-01 through 2024-12-31 replay can generate the real `expectancy_report.json`.
