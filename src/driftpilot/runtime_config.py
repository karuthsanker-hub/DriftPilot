"""Runtime-tunable settings that hot-reload without restarting the operator.

A small JSON file at `data/driftpilot/runtime_config.json` carries the
subset of settings safe to change while the operator is running. The
operator reads this file each cycle in `_run_once`; the dashboard's
admin endpoint writes to it.

Only fields that have NO downstream side-effects beyond the next cycle
belong here. Things that DO have side effects (ALPACA_API_KEY, schema
paths, etc.) stay in .env and require a restart.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PATH = "data/driftpilot/runtime_config.json"

# The set of fields the admin UI can edit. Validation rules below.
EDITABLE_FIELDS: dict[str, dict[str, Any]] = {
    "active_signal": {
        "type": str,
        "choices": [
            "earnings_report_v1",
            "filing_8a_v1",
            "analyst_target_raise_v1",
            "earnings_report_v1,filing_8a_v1",
        ],
        "label": "Active signal(s) (RESTART)",
        "help": (
            "Which catalyst signal(s) drive entries. Single name OR comma-separated list "
            "for MultiSignal mode. earnings_report_v1 (validated, ratio=5.09 n=33), "
            "filing_8a_v1 (validated, ratio=2.05 n=256, biggest sample), "
            "analyst_target_raise_v1 (FAIL, ratio=0.85). Combo recommended for broader coverage. "
            "Read at operator boot — pause, change, restart."
        ),
    },
    "scanning_paused": {
        "type": str,
        "choices": ["false", "true"],
        "label": "Pause scanning (HOT)",
        "help": "true = scanner emits 0 candidates (no new entries). Existing positions still managed. Hot-reloads in ~30s.",
    },
    "slot_value": {
        "type": float,
        "min": 100.0,
        "max": 100_000.0,
        "label": "Slot value ($) (RESTART)",
        "help": "Notional dollars per slot. 10 slots × this = max deployed.",
    },
    "max_trades_per_symbol_per_day": {
        "type": int,
        "min": 1,
        "max": 20,
        "label": "Max trades / symbol / day (RESTART)",
        "help": "1 = no re-trading the same symbol once it closes today.",
    },
    "max_slots_per_sector": {
        "type": int,
        "min": 1,
        "max": 10,
        "label": "Max slots / sector (RESTART)",
        "help": "Concentration cap. 3 = at most 3 of 10 slots in any one sector.",
    },
    "catalyst_universe_lookback_minutes": {
        "type": int,
        "min": 30,
        "max": 1440,
        "label": "Catalyst universe lookback (min) (RESTART)",
        "help": "How far back the universe filter looks for catalysts on each symbol.",
    },
    "earnings_max_event_age_minutes": {
        "type": int,
        "min": 30,
        "max": 1440,
        "label": "Earnings event max age (min)",
        "help": "Reject candidates whose news is older than this. Validated cell was 60m.",
    },
    "earnings_profit_take_pct": {
        "type": float,
        "min": 0.1,
        "max": 5.0,
        "label": "Profit take %",
        "help": "Exit at this gain. Validated config was 1.0%.",
    },
    "earnings_stop_loss_pct": {
        "type": float,
        "min": 0.1,
        "max": 5.0,
        "label": "Stop loss %",
        "help": "Exit at this drawdown. Validated config was 1.5%.",
    },
    "earnings_max_hold_minutes": {
        "type": int,
        "min": 5,
        "max": 480,
        "label": "Max hold (min)",
        "help": "Hard time stop after entry. Validated config was 60m.",
    },
    "earnings_require_sentiment": {
        "type": str,
        "choices": ["positive", "negative", "neutral", "any"],
        "label": "Require Qwen sentiment",
        "help": "'positive' (validated GATED config), 'any' = no gate.",
    },
    "earnings_trailing_enabled": {
        "type": str,
        "choices": ["true", "false"],
        "label": "Trailing stop enabled",
        "help": "True = use trailing stop instead of fixed profit_take. Recommended.",
    },
    "earnings_trailing_activation_pct": {
        "type": float,
        "min": 0.1,
        "max": 5.0,
        "label": "Trailing activation %",
        "help": "Peak must reach this before trailing kicks in. e.g. 1.0 = stop trails after +1%.",
    },
    "earnings_trailing_distance_pct": {
        "type": float,
        "min": 0.1,
        "max": 5.0,
        "label": "Trailing distance %",
        "help": "Trailing stop sits this far below peak. Smaller = locks in gains faster, exits more often.",
    },
}


@dataclass
class RuntimeConfig:
    active_signal: str = "earnings_report_v1"
    scanning_paused: str = "false"
    slot_value: float = 1000.0
    max_trades_per_symbol_per_day: int = 1
    max_slots_per_sector: int = 3
    catalyst_universe_lookback_minutes: int = 240
    earnings_max_event_age_minutes: int = 240
    earnings_profit_take_pct: float = 1.0
    earnings_stop_loss_pct: float = 1.5
    earnings_max_hold_minutes: int = 60
    earnings_require_sentiment: str = "positive"
    earnings_trailing_enabled: str = "true"
    earnings_trailing_activation_pct: float = 1.0
    earnings_trailing_distance_pct: float = 2.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_signal": self.active_signal,
            "scanning_paused": self.scanning_paused,
            "slot_value": self.slot_value,
            "max_trades_per_symbol_per_day": self.max_trades_per_symbol_per_day,
            "max_slots_per_sector": self.max_slots_per_sector,
            "catalyst_universe_lookback_minutes": self.catalyst_universe_lookback_minutes,
            "earnings_max_event_age_minutes": self.earnings_max_event_age_minutes,
            "earnings_profit_take_pct": self.earnings_profit_take_pct,
            "earnings_stop_loss_pct": self.earnings_stop_loss_pct,
            "earnings_max_hold_minutes": self.earnings_max_hold_minutes,
            "earnings_require_sentiment": self.earnings_require_sentiment,
            "earnings_trailing_enabled": self.earnings_trailing_enabled,
            "earnings_trailing_activation_pct": self.earnings_trailing_activation_pct,
            "earnings_trailing_distance_pct": self.earnings_trailing_distance_pct,
        }


def load_runtime_config(path: str | Path = DEFAULT_PATH) -> RuntimeConfig:
    """Load (or initialize) the runtime config. Missing file → defaults.
    Bad fields are silently dropped; defaults remain.
    """
    p = Path(path)
    if not p.exists():
        return RuntimeConfig()
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("runtime config %s unreadable, using defaults: %s", p, exc)
        return RuntimeConfig()
    cfg = RuntimeConfig()
    for k, v in raw.items():
        if k in EDITABLE_FIELDS and hasattr(cfg, k):
            try:
                setattr(cfg, k, EDITABLE_FIELDS[k]["type"](v))
            except (TypeError, ValueError):
                pass
    return cfg


def save_runtime_config(
    cfg: RuntimeConfig | dict[str, Any],
    path: str | Path = DEFAULT_PATH,
) -> RuntimeConfig:
    """Validate + persist. Returns the canonical RuntimeConfig that landed on disk."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(cfg, dict):
        # Validate each field against EDITABLE_FIELDS rules
        merged = load_runtime_config(p).to_dict()
        for k, v in cfg.items():
            if k not in EDITABLE_FIELDS:
                raise ValueError(f"unknown field: {k}")
            spec = EDITABLE_FIELDS[k]
            try:
                coerced = spec["type"](v)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{k}: bad type ({exc})") from exc
            if "choices" in spec and coerced not in spec["choices"]:
                raise ValueError(f"{k}: must be one of {spec['choices']}")
            if "min" in spec and coerced < spec["min"]:
                raise ValueError(f"{k}: must be ≥ {spec['min']}")
            if "max" in spec and coerced > spec["max"]:
                raise ValueError(f"{k}: must be ≤ {spec['max']}")
            merged[k] = coerced
        result = RuntimeConfig(**merged)
    else:
        result = cfg

    # Atomic write: temp + rename
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(result.to_dict(), indent=2))
    os.replace(tmp, p)
    logger.info("runtime config saved: %s", result.to_dict())
    return result


def field_specs() -> dict[str, dict[str, Any]]:
    """Public read of EDITABLE_FIELDS for the admin UI to render the form."""
    out: dict[str, dict[str, Any]] = {}
    for k, v in EDITABLE_FIELDS.items():
        out[k] = {
            "type": v["type"].__name__,
            "label": v["label"],
            "help": v["help"],
        }
        for opt in ("min", "max", "choices"):
            if opt in v:
                out[k][opt] = v[opt]
    return out
