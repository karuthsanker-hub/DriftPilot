"""Filing 8-A v1 — catalyst-driven 60m drift signal on filing/8a events.

Same architecture as earnings_report_v1 but subscribes to (category="filing",
subcategory="8a"). Validation cell from reports/catalyst_horizons_midcap_2024.json:
n=256, ratio_mean=2.05 at 60m, p>1%=29%, mean|r|=1.10%. The largest validated
sample we have, ~2x the target_raise edge (1.42).
"""

from __future__ import annotations

from driftpilot.signals.filing_8a_v1.config import Filing8AConfig
from driftpilot.signals.filing_8a_v1.signal import Filing8ASignal

SIGNAL_NAME = "filing_8a_v1"
SIGNAL_VERSION = "1.0.0"

__all__ = [
    "SIGNAL_NAME",
    "SIGNAL_VERSION",
    "Filing8AConfig",
    "Filing8ASignal",
]
