from __future__ import annotations

from trading_bot.settings import AppSettings, load_settings


def create_supabase_client(settings: AppSettings | None = None):
    settings = settings or load_settings()
    if not settings.supabase_url or settings.supabase_key is None:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be configured")
    from supabase import create_client

    return create_client(settings.supabase_url, settings.supabase_key.get_secret_value())
