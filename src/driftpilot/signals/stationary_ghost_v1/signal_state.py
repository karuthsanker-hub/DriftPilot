"""Stationary Ghost v1 - TypedDict for signal_state keys.

This signal uses no custom state; it relies on the harness's default
TARGET/STOP/TIME exit rules and does not write any keys into
`position.metadata`. The empty TypedDict is included for symmetry with
the other signals so callers can uniformly do
`typed_signal_state(position, StationaryGhostState)` without special-
casing the no-state case. Per refactor plan v1.1 section 3.1.
"""
from __future__ import annotations

from typing import TypedDict


class StationaryGhostState(TypedDict, total=False):
    pass


__all__ = ["StationaryGhostState"]
