from __future__ import annotations

import argparse
from datetime import date

from trading_bot.data.provider_factory import create_market_data_provider
from trading_bot.data.repositories import StrategyConfigRepository, TradingRepository
from trading_bot.data.supabase_client import create_supabase_client
from trading_bot.diagnostics import run_env_diagnostics
from trading_bot.execution.alpaca_broker import AlpacaBroker
from trading_bot.execution.paper_engine import PaperExecutionEngine
from trading_bot.scanners.pead_scanner import PEADScanner
from trading_bot.sentiment import FinBERTSentimentScorer, KeywordSentimentScorer
from trading_bot.settings import load_settings


def main() -> int:
    parser = argparse.ArgumentParser(prog="trading-bot")
    sub = parser.add_subparsers(dest="command", required=True)

    diag = sub.add_parser("diagnostics")
    diag.add_argument("--env", default=".env")
    diag.add_argument("--no-network", action="store_true")

    scan = sub.add_parser("scan-pead")
    scan.add_argument("--env", default=".env")
    scan.add_argument("--date", default=date.today().isoformat())
    scan.add_argument("--tickers", required=True, help="Comma-separated ticker list")
    scan.add_argument("--persist", action="store_true")
    scan.add_argument("--persist-skips", action="store_true")
    scan.add_argument("--sentiment", choices=["keyword", "finbert"], default="keyword")

    execute = sub.add_parser("execute-pending")
    execute.add_argument("--env", default=".env")
    execute.add_argument("--submit", action="store_true", help="Submit paper orders. Defaults to dry-run.")

    args = parser.parse_args()
    if args.command == "diagnostics":
        results = run_env_diagnostics(args.env, network=not args.no_network)
        for result in results:
            mark = "PASS" if result.ok else "FAIL"
            print(f"{mark} {result.name}: {result.message}")
        return 0 if all(result.ok for result in results if result.name not in {"fred_connection", "qwen_connection"}) else 1
    if args.command == "scan-pead":
        return _scan_pead(args)
    if args.command == "execute-pending":
        return _execute_pending(args)
    return 2


def _scan_pead(args) -> int:
    settings = load_settings(args.env)
    repository = None
    if args.persist:
        repository = TradingRepository(create_supabase_client(settings))
    scorer = FinBERTSentimentScorer() if args.sentiment == "finbert" else KeywordSentimentScorer()
    scanner = PEADScanner(create_market_data_provider(settings), scorer, repository)
    tickers = [ticker.strip() for ticker in args.tickers.split(",")]
    results = scanner.scan(tickers, date.fromisoformat(args.date), persist_skips=args.persist_skips)
    for result in results:
        print(
            f"{result.ticker}: {result.signal.action.value} "
            f"surprise={result.signal.surprise_pct:.2f}% "
            f"persisted={result.persisted} "
            f"reason={result.signal.skip_reason}"
        )
    return 0


def _execute_pending(args) -> int:
    settings = load_settings(args.env)
    client = create_supabase_client(settings)
    repo = TradingRepository(client)
    config_repo = StrategyConfigRepository(client)
    engine = PaperExecutionEngine(repo, config_repo, AlpacaBroker(settings))
    summary = engine.execute_pending_watchlist(dry_run=not args.submit)
    print(f"attempted={summary.attempted} submitted={summary.submitted} blocked_reason={summary.blocked_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
