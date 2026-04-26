from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str


def run_env_diagnostics(env_path: str | Path = ".env", *, network: bool = True, timeout: float = 10.0) -> list[CheckResult]:
    load_dotenv(env_path, override=True)
    results = [
        _required("PAPER_MODE"),
        _bool_value("PAPER_MODE"),
        _url_value("SUPABASE_URL", required=True, allowed_schemes={"https"}),
        _required("SUPABASE_KEY"),
        _required("ALPACA_API_KEY"),
        _required("ALPACA_SECRET_KEY"),
        _url_value("ALPACA_BASE_URL", required=True, allowed_schemes={"https"}),
        _url_value("QWEN_BASE_URL", required=False, allowed_schemes={"http", "https"}),
        _provider_choice("ACTIVE_LLM_PROVIDER"),
        _number("RISK_PER_TRADE_PCT", minimum=0, maximum=0.05),
        _number("MAX_POSITION_PCT", minimum=0, maximum=1),
        _number("VIX_PAUSE_THRESHOLD", minimum=0, maximum=100),
        _number("DAILY_LOSS_LIMIT_PCT", maximum=0),
    ]
    if network:
        results.extend(
            [
                check_supabase(timeout=timeout),
                check_alpaca(timeout=timeout),
                check_fred(timeout=timeout),
                check_qwen(timeout=timeout),
            ]
        )
    return results


def check_supabase(*, timeout: float = 10.0) -> CheckResult:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        return CheckResult("supabase_connection", False, "missing SUPABASE_URL or SUPABASE_KEY")
    try:
        response = httpx.get(
            f"{url}/rest/v1/strategy_config",
            params={"select": "key,value", "limit": "1"},
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        if response.status_code == 404:
            return CheckResult("supabase_connection", False, "connected, but strategy_config table was not found; run migration SQL")
        response.raise_for_status()
        return CheckResult("supabase_connection", True, "connected and strategy_config is readable")
    except Exception as exc:
        return CheckResult("supabase_connection", False, _clean_error(exc))


def check_alpaca(*, timeout: float = 10.0) -> CheckResult:
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
    account_url = f"{base_url}/account" if base_url.endswith("/v2") else f"{base_url}/v2/account"
    if not key or not secret:
        return CheckResult("alpaca_connection", False, "missing ALPACA_API_KEY or ALPACA_SECRET_KEY")
    try:
        response = httpx.get(
            account_url,
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        status = data.get("status", "unknown")
        return CheckResult("alpaca_connection", True, f"connected to Alpaca account; status={status}")
    except Exception as exc:
        return CheckResult("alpaca_connection", False, _clean_error(exc))


def check_fred(*, timeout: float = 10.0) -> CheckResult:
    key = os.getenv("FRED_API_KEY", "")
    if not key:
        return CheckResult("fred_connection", False, "missing optional FRED_API_KEY")
    try:
        response = httpx.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": "VIXCLS", "api_key": key, "file_type": "json", "limit": 1, "sort_order": "desc"},
            timeout=timeout,
        )
        response.raise_for_status()
        return CheckResult("fred_connection", True, "connected and VIXCLS is readable")
    except Exception as exc:
        return CheckResult("fred_connection", False, _clean_error(exc))


def check_qwen(*, timeout: float = 10.0) -> CheckResult:
    base_url = os.getenv("QWEN_BASE_URL", "").rstrip("/")
    if not base_url:
        return CheckResult("qwen_connection", False, "missing optional QWEN_BASE_URL")
    headers = {}
    api_key = os.getenv("QWEN_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = httpx.get(f"{base_url}/models", headers=headers, timeout=timeout)
        response.raise_for_status()
        return CheckResult("qwen_connection", True, "OpenAI-compatible local endpoint is reachable")
    except Exception as exc:
        return CheckResult("qwen_connection", False, _clean_error(exc))


def _required(key: str) -> CheckResult:
    return CheckResult(key, bool(os.getenv(key, "")), "set" if os.getenv(key, "") else "missing")


def _bool_value(key: str) -> CheckResult:
    value = os.getenv(key, "").lower()
    ok = value in {"true", "false", "1", "0", "yes", "no", "on", "off"}
    return CheckResult(f"{key}_format", ok, "valid boolean" if ok else "must be boolean-like")


def _url_value(key: str, *, required: bool, allowed_schemes: set[str]) -> CheckResult:
    value = os.getenv(key, "")
    if not value:
        return CheckResult(f"{key}_format", not required, "missing" if required else "not configured")
    parsed = urlparse(value)
    ok = parsed.scheme in allowed_schemes and bool(parsed.netloc)
    return CheckResult(f"{key}_format", ok, "valid URL" if ok else f"must use one of {sorted(allowed_schemes)} and include a host")


def _provider_choice(key: str) -> CheckResult:
    value = os.getenv(key, "")
    ok = value in {"openai", "claude", "gemini", "qwen"}
    return CheckResult(key, ok, "valid provider" if ok else "must be one of openai, claude, gemini, qwen")


def _number(key: str, *, minimum: float | None = None, maximum: float | None = None) -> CheckResult:
    value = os.getenv(key, "")
    try:
        parsed = float(value)
    except ValueError:
        return CheckResult(f"{key}_format", False, "must be numeric")
    if minimum is not None and parsed < minimum:
        return CheckResult(f"{key}_format", False, f"must be >= {minimum}")
    if maximum is not None and parsed > maximum:
        return CheckResult(f"{key}_format", False, f"must be <= {maximum}")
    return CheckResult(f"{key}_format", True, "valid number")


def _clean_error(exc: Exception) -> str:
    text = str(exc)
    for key in ("SUPABASE_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FRED_API_KEY", "QWEN_API_KEY"):
        value = os.getenv(key, "")
        if value:
            text = text.replace(value, "<redacted>")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Trading Bot .env configuration and safe connections.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--no-network", action="store_true", help="Only validate formats and required keys")
    args = parser.parse_args()

    results = run_env_diagnostics(args.env, network=not args.no_network)
    for result in results:
        mark = "PASS" if result.ok else "FAIL"
        print(f"{mark} {result.name}: {result.message}")
    return 0 if all(result.ok for result in results if result.name != "fred_connection") else 1


if __name__ == "__main__":
    raise SystemExit(main())
