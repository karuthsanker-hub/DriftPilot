from __future__ import annotations

from driftpilot.states import BlockedReason


def test_catalyst_negative_value_present() -> None:
    assert BlockedReason.CATALYST_NEGATIVE.value == "catalyst_negative"


def test_catalyst_age_exceeded_value_present() -> None:
    assert BlockedReason.CATALYST_AGE_EXCEEDED.value == "catalyst_age_exceeded"


def test_catalyst_values_in_enum_members() -> None:
    values = {member.value for member in BlockedReason}
    assert "catalyst_negative" in values
    assert "catalyst_age_exceeded" in values
