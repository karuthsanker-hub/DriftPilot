"""Tests for per-signal TypedDict declarations of `position.metadata`.

Per Phase 0.5 of the DriftPilot Refactor Plan v2: each new signal
declares a TypedDict listing every key its `exits.py` reads/writes.
The TypedDict is type-only (runtime is still `dict[str, Any]`); these
tests verify the contract holds:

  1. Each TypedDict exists and is a TypedDict subclass.
  2. The TypedDict's optional keys are a superset of the literal
     `metadata["<key>"]` accesses found in the signal's exits.py.
     This catches drift where a new key gets added to exits.py but
     not the TypedDict.
  3. `typed_signal_state(position, Cls)` is a runtime no-op - it
     returns the same dict object as `position.metadata`.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from driftpilot.signals.apex_hunter_v2.signal_state import ApexHunterState
from driftpilot.signals.base import typed_signal_state
from driftpilot.signals.rs_drift_v1.signal_state import RsDriftState
from driftpilot.signals.stationary_ghost_v1.signal_state import StationaryGhostState
from driftpilot.signals.whale_tail_v1.signal_state import WhaleTailState


_SIGNALS_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "driftpilot" / "signals"
)


SIGNAL_TYPEDDICT_CASES = [
    ("apex_hunter_v2", ApexHunterState),
    ("rs_drift_v1", RsDriftState),
    ("stationary_ghost_v1", StationaryGhostState),
    ("whale_tail_v1", WhaleTailState),
]


def _is_typeddict(cls: type) -> bool:
    """A TypedDict subclass at runtime is a `dict` subclass and exposes
    `__optional_keys__` / `__required_keys__` attributes."""
    return (
        issubclass(cls, dict)
        and hasattr(cls, "__optional_keys__")
        and hasattr(cls, "__required_keys__")
    )


@pytest.mark.parametrize("_signal_name,state_cls", SIGNAL_TYPEDDICT_CASES)
def test_each_signal_declares_a_state_typeddict(
    _signal_name: str, state_cls: type
) -> None:
    """Every new signal must declare a TypedDict for its metadata keys."""
    assert _is_typeddict(state_cls), (
        f"{state_cls!r} is not a TypedDict subclass"
    )
    # total=False means every key lives in __optional_keys__
    assert state_cls.__total__ is False, (
        f"{state_cls!r} must be declared with total=False"
    )


def _metadata_string_keys_in(source: str) -> set[str]:
    """Parse `metadata["<key>"]` and `md["<key>"]` literal subscripts.

    We scan the AST for any Subscript whose value is a Name and whose
    slice is a string Constant. The signal source uses a few aliases
    (`metadata`, `md`); accept either - we want the union.
    """
    tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        value = node.value
        if not isinstance(value, ast.Name):
            continue
        if value.id not in {"metadata", "md"}:
            continue
        slc = node.slice
        if isinstance(slc, ast.Constant) and isinstance(slc.value, str):
            keys.add(slc.value)
    return keys


@pytest.mark.parametrize("signal_name,state_cls", SIGNAL_TYPEDDICT_CASES)
def test_typeddict_keys_match_exits_writes(
    signal_name: str, state_cls: type
) -> None:
    """Every literal `metadata["<key>"]` in exits.py must be in the
    TypedDict's optional keys. Catches drift where exits.py gains a
    new key but the TypedDict is not updated."""
    exits_path = _SIGNALS_ROOT / signal_name / "exits.py"
    if not exits_path.exists():
        # Stationary Ghost has no custom exits.py with metadata access
        # (it just defines exit constants). Skip with empty expectation.
        return
    source = exits_path.read_text()
    accessed = _metadata_string_keys_in(source)
    optional_keys = set(state_cls.__optional_keys__)
    missing = accessed - optional_keys
    assert not missing, (
        f"{signal_name}/exits.py reads/writes metadata keys "
        f"{sorted(missing)} that are not declared in {state_cls.__name__}. "
        f"Add them to signal_state.py or remove from exits.py."
    )


@dataclass
class _FakePosition:
    metadata: dict[str, Any] = field(default_factory=dict)


def test_typed_signal_state_is_runtime_noop() -> None:
    """`typed_signal_state` must return the exact same dict object - the
    cast is type-only, never copies."""
    md: dict[str, Any] = {"ratchet_stage": 1, "peak_price": 100.0}
    pos = _FakePosition(metadata=md)
    state = typed_signal_state(pos, ApexHunterState)
    assert state is md
    # Mutations propagate (i.e. it really is the same object)
    state["ratchet_stage"] = 2  # type: ignore[typeddict-item]
    assert md["ratchet_stage"] == 2
