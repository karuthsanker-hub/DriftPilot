"""DriftPilot autonomous paper-trading operator entrypoint.

python -m driftpilot.operator               # full run during market hours
python -m driftpilot.operator --once        # single deterministic cycle
python -m driftpilot.operator --mock-stream # synthetic bars, no Alpaca WS
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DriftPilot operator.")
    parser.add_argument("--once", action="store_true", help="Run one deterministic cycle and exit.")
    parser.add_argument(
        "--mock-stream",
        action="store_true",
        help="Use synthetic bars instead of Alpaca WebSocket data.",
    )
    parser.parse_args()
    raise NotImplementedError(
        "Phase 9 runtime wiring is intentionally deferred until Phase 12 returns GATED or better."
    )


if __name__ == "__main__":
    main()
