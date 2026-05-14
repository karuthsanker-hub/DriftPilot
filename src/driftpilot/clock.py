from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from driftpilot.settings import DEFAULT_TIMEZONE


def require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def datetime_to_storage(value: datetime) -> str:
    return require_aware(value).isoformat()


def datetime_from_storage(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return require_aware(parsed)


@dataclass(frozen=True, slots=True)
class DriftPilotClock:
    timezone_name: str = DEFAULT_TIMEZONE

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def now_et(self) -> datetime:
        return self.now_utc().astimezone(self.timezone)

    def to_et(self, value: datetime) -> datetime:
        return require_aware(value).astimezone(self.timezone)

    def date_et(self, value: datetime | None = None) -> date:
        if value is None:
            value = self.now_et()
        return self.to_et(value).date()


@dataclass(frozen=True, slots=True)
class FixedClock(DriftPilotClock):
    fixed_now: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def now_utc(self) -> datetime:
        return require_aware(self.fixed_now).astimezone(UTC)


def market_time_profile(
    value: datetime | None,
    *,
    clock: DriftPilotClock | None = None,
) -> tuple[str, float, float]:
    """Return (profile_name, target_mult, stop_mult) for regular-hours ET."""
    if value is None:
        return "unknown", 1.0, 1.0

    et = (clock or DriftPilotClock()).to_et(value)
    minutes = et.hour * 60 + et.minute
    open_minutes = 9 * 60 + 30
    close_minutes = 16 * 60
    if minutes < open_minutes or minutes >= close_minutes:
        return "outside_regular_hours", 1.0, 1.0
    if minutes < 10 * 60:
        return "opening_volatility", 1.10, 1.25
    if 11 * 60 + 30 <= minutes < 14 * 60:
        return "midday_quiet", 0.90, 0.90
    if minutes >= 15 * 60 + 30:
        return "closing_volatility", 1.05, 1.15
    return "regular", 1.0, 1.0
