from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from driftpilot.clock import DriftPilotClock, datetime_from_storage, datetime_to_storage


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_PATH.read_text())
    connection.commit()


def list_user_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {row["name"] for row in rows}


def primary_key_columns(
    connection: sqlite3.Connection, table_name: str
) -> list[tuple[str, int]]:
    if not table_name.replace("_", "").isalnum():
        raise ValueError("table_name must be a simple SQLite identifier")
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [(row["name"], row["pk"]) for row in rows if row["pk"]]


def _json_dumps(value: dict[str, Any] | list[Any] | None) -> str:
    return json.dumps(
        value if value is not None else {}, sort_keys=True, separators=(",", ":")
    )


def _json_loads_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("stored JSON value must be an object")
    return parsed


def _date_to_storage(value: date) -> str:
    return value.isoformat()


def _optional_dict_str(value: dict[str, Any], key: str) -> str | None:
    raw = value.get(key)
    return None if raw is None else str(raw)


def _last_insert_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite insert did not produce a row id")
    return cursor.lastrowid


@dataclass(frozen=True, slots=True)
class OperatorStateRecord:
    current_state: str
    updated_at: datetime
    active_gate: str | None = None
    last_transition_id: int | None = None
    last_error_id: int | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StateTransitionRecord:
    id: int
    from_state: str | None
    to_state: str
    reason: str
    timestamp: datetime
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SlotRecord:
    slot_id: int
    status: str
    slot_value: float
    updated_at: datetime
    symbol: str | None = None
    position_id: int | None = None
    reserved_order_id: int | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PositionRecord:
    id: int
    symbol: str
    status: str
    quantity: float
    entry_price: float
    target_price: float
    stop_price: float
    opened_at: datetime
    broker_position_id: str | None = None
    slot_id: int | None = None
    closed_at: datetime | None = None
    exit_reason: str | None = None
    realized_pnl: float | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DailyCounterRecord:
    date_et: date
    counter_name: str
    counter_value: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OrderRecord:
    id: int
    symbol: str
    side: str
    order_type: str
    status: str
    quantity: float
    submitted_at: datetime
    updated_at: datetime
    broker_order_id: str | None = None
    position_id: int | None = None
    slot_id: int | None = None
    limit_price: float | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StreamStateRecord:
    name: str
    shard_cursor: int
    updated_at: datetime
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AllocatorStateRecord:
    status: str
    updated_at: datetime
    locked_at: datetime | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class FillRecord:
    id: int
    symbol: str
    side: str
    quantity: float
    price: float
    filled_at: datetime
    order_id: int | None = None
    broker_fill_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CandidateRow:
    symbol: str
    score: float
    rvol: float
    vwap_distance_pct: float
    return_15m_pct: float
    sector: str
    blocked_reason: str | None
    queue_status: str
    cycle_at: datetime


@dataclass(frozen=True, slots=True)
class PriceDriftBaselineRecord:
    symbol: str
    event_key: str
    first_seen_price: float
    first_seen_at: datetime
    last_seen_price: float
    last_seen_at: datetime
    drift_pct: float
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RecycleEvent:
    id: int
    slot_id: int
    freed_symbol: str
    exit_reason: str
    exit_pnl_pct: float
    replacement_symbol: str | None
    at: datetime


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    id: int
    severity: str
    message: str
    raised_at: datetime
    resolved_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class StateRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def get(self) -> OperatorStateRecord | None:
        row = self.connection.execute(
            """
            SELECT current_state, active_gate, last_transition_id, last_error_id, updated_at, metadata_json
            FROM operator_state
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return None
        return OperatorStateRecord(
            current_state=row["current_state"],
            active_gate=row["active_gate"],
            last_transition_id=row["last_transition_id"],
            last_error_id=row["last_error_id"],
            updated_at=datetime_from_storage(row["updated_at"]),
            metadata=_json_loads_object(row["metadata_json"]),
        )

    def set(
        self,
        current_state: str,
        *,
        active_gate: str | None = None,
        last_transition_id: int | None = None,
        last_error_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> OperatorStateRecord:
        timestamp = updated_at or self.clock.now_utc()
        self.connection.execute(
            """
            INSERT INTO operator_state (
                id, current_state, active_gate, last_transition_id, last_error_id, updated_at, metadata_json
            )
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                current_state = excluded.current_state,
                active_gate = excluded.active_gate,
                last_transition_id = excluded.last_transition_id,
                last_error_id = excluded.last_error_id,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                current_state,
                active_gate,
                last_transition_id,
                last_error_id,
                datetime_to_storage(timestamp),
                _json_dumps(metadata),
            ),
        )
        self.connection.commit()
        return OperatorStateRecord(
            current_state=current_state,
            active_gate=active_gate,
            last_transition_id=last_transition_id,
            last_error_id=last_error_id,
            updated_at=timestamp,
            metadata=metadata or {},
        )


class TransitionRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def append(
        self,
        *,
        from_state: str | None,
        to_state: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> StateTransitionRecord:
        happened_at = timestamp or self.clock.now_utc()
        cursor = self.connection.execute(
            """
            INSERT INTO state_transitions (from_state, to_state, reason, timestamp, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                from_state,
                to_state,
                reason,
                datetime_to_storage(happened_at),
                _json_dumps(metadata),
            ),
        )
        transition_id = _last_insert_id(cursor)
        self.connection.commit()
        return StateTransitionRecord(
            id=transition_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            timestamp=happened_at,
            metadata=metadata or {},
        )

    def latest(self) -> StateTransitionRecord | None:
        row = self.connection.execute(
            """
            SELECT id, from_state, to_state, reason, timestamp, metadata_json
            FROM state_transitions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return StateTransitionRecord(
            id=row["id"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            reason=row["reason"],
            timestamp=datetime_from_storage(row["timestamp"]),
            metadata=_json_loads_object(row["metadata_json"]),
        )

    def list_latest(self, *, limit: int = 50) -> list[StateTransitionRecord]:
        rows = self.connection.execute(
            """
            SELECT id, from_state, to_state, reason, timestamp, metadata_json
            FROM state_transitions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            StateTransitionRecord(
                id=row["id"],
                from_state=row["from_state"],
                to_state=row["to_state"],
                reason=row["reason"],
                timestamp=datetime_from_storage(row["timestamp"]),
                metadata=_json_loads_object(row["metadata_json"]),
            )
            for row in rows
        ]


class SlotRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def upsert(
        self,
        slot_id: int,
        *,
        status: str,
        slot_value: float,
        symbol: str | None = None,
        position_id: int | None = None,
        reserved_order_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> SlotRecord:
        timestamp = updated_at or self.clock.now_utc()
        self.connection.execute(
            """
            INSERT INTO slots (
                slot_id, status, symbol, position_id, reserved_order_id, slot_value, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slot_id) DO UPDATE SET
                status = excluded.status,
                symbol = excluded.symbol,
                position_id = excluded.position_id,
                reserved_order_id = excluded.reserved_order_id,
                slot_value = excluded.slot_value,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                slot_id,
                status,
                symbol,
                position_id,
                reserved_order_id,
                slot_value,
                datetime_to_storage(timestamp),
                _json_dumps(metadata),
            ),
        )
        self.connection.commit()
        return SlotRecord(
            slot_id=slot_id,
            status=status,
            symbol=symbol,
            position_id=position_id,
            reserved_order_id=reserved_order_id,
            slot_value=slot_value,
            updated_at=timestamp,
            metadata=metadata or {},
        )

    def get(self, slot_id: int) -> SlotRecord | None:
        row = self.connection.execute(
            """
            SELECT slot_id, status, symbol, position_id, reserved_order_id, slot_value, updated_at, metadata_json
            FROM slots
            WHERE slot_id = ?
            """,
            (slot_id,),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def list_all(self) -> list[SlotRecord]:
        rows = self.connection.execute(
            """
            SELECT slot_id, status, symbol, position_id, reserved_order_id, slot_value, updated_at, metadata_json
            FROM slots
            ORDER BY slot_id
            """
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def _from_row(self, row: sqlite3.Row) -> SlotRecord:
        return SlotRecord(
            slot_id=row["slot_id"],
            status=row["status"],
            symbol=row["symbol"],
            position_id=row["position_id"],
            reserved_order_id=row["reserved_order_id"],
            slot_value=row["slot_value"],
            updated_at=datetime_from_storage(row["updated_at"]),
            metadata=_json_loads_object(row["metadata_json"]),
        )


class PositionRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def list_open(self) -> list[PositionRecord]:
        rows = self.connection.execute(
            """
            SELECT
                id, broker_position_id, symbol, slot_id, status, quantity, entry_price,
                target_price, stop_price, opened_at, closed_at, exit_reason, realized_pnl, metadata_json
            FROM positions
            WHERE status = 'open'
            ORDER BY id
            """
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def close(
        self,
        position_id: int,
        *,
        exit_reason: str,
        realized_pnl: float,
        closed_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PositionRecord:
        existing = self.get(position_id)
        if existing is None:
            raise ValueError(f"position {position_id} does not exist")
        timestamp = closed_at or self.clock.now_utc()
        merged_metadata = {**(existing.metadata or {}), **(metadata or {})}
        self.connection.execute(
            """
            UPDATE positions
            SET status = 'closed',
                closed_at = ?,
                exit_reason = ?,
                realized_pnl = ?,
                metadata_json = ?
            WHERE id = ?
            """,
            (
                datetime_to_storage(timestamp),
                exit_reason,
                realized_pnl,
                _json_dumps(merged_metadata),
                position_id,
            ),
        )
        self.connection.commit()
        updated = self.get(position_id)
        if updated is None:
            raise RuntimeError("closed position disappeared")
        return updated

    def create_open(
        self,
        *,
        symbol: str,
        quantity: float,
        entry_price: float,
        target_price: float,
        stop_price: float,
        broker_position_id: str | None = None,
        slot_id: int | None = None,
        opened_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PositionRecord:
        timestamp = opened_at or self.clock.now_utc()
        cursor = self.connection.execute(
            """
            INSERT INTO positions (
                broker_position_id, symbol, slot_id, status, quantity, entry_price,
                target_price, stop_price, opened_at, metadata_json
            )
            VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
            """,
            (
                broker_position_id,
                symbol.upper(),
                slot_id,
                quantity,
                entry_price,
                target_price,
                stop_price,
                datetime_to_storage(timestamp),
                _json_dumps(metadata),
            ),
        )
        self.connection.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("position insert did not return an id")
        position_id = cursor.lastrowid
        row = self.connection.execute(
            """
            SELECT
                id, broker_position_id, symbol, slot_id, status, quantity, entry_price,
                target_price, stop_price, opened_at, closed_at, exit_reason, realized_pnl, metadata_json
            FROM positions
            WHERE id = ?
            """,
            (position_id,),
        ).fetchone()
        return self._from_row(row)

    def update_metadata(
        self,
        position_id: int,
        metadata: dict[str, Any],
    ) -> PositionRecord:
        existing = self.get(position_id)
        if existing is None:
            raise ValueError(f"position {position_id} does not exist")
        merged_metadata = {**(existing.metadata or {}), **metadata}
        self.connection.execute(
            """
            UPDATE positions
            SET metadata_json = ?
            WHERE id = ?
            """,
            (_json_dumps(merged_metadata), position_id),
        )
        self.connection.commit()
        updated = self.get(position_id)
        if updated is None:
            raise RuntimeError("updated position disappeared")
        return updated

    def reconcile_broker_open_positions(
        self,
        *,
        broker_positions: list[dict[str, Any]],
        slot_value: float,
        target_pct: float,
        stop_pct: float,
        trade_slots: int,
    ) -> str:
        local_open = self.list_open()
        local_by_symbol = {position.symbol: position for position in local_open}
        broker_by_symbol = {
            str(position["symbol"]).upper(): position for position in broker_positions
        }
        broker_symbols = set(broker_by_symbol)
        local_symbols = set(local_by_symbol)
        used_slots: set[int] = set()
        timestamp = self.clock.now_utc()

        for position in local_open:
            if position.symbol in broker_symbols:
                continue
            self.connection.execute(
                """
                UPDATE positions
                SET status = 'closed',
                    closed_at = ?,
                    exit_reason = 'broker_missing_at_boot',
                    metadata_json = ?
                WHERE id = ?
                """,
                (
                    datetime_to_storage(timestamp),
                    _json_dumps(
                        {
                            **(position.metadata or {}),
                            "reconciled": "broker_missing_at_boot",
                        }
                    ),
                    position.id,
                ),
            )
            if position.slot_id is not None:
                self._upsert_slot(
                    position.slot_id,
                    status="available",
                    slot_value=slot_value,
                    updated_at=timestamp,
                    metadata={
                        "reconciled": "broker_missing_at_boot",
                        "previous_symbol": position.symbol,
                    },
                )

        for symbol, broker_position in broker_by_symbol.items():
            existing = local_by_symbol.get(symbol)
            slot_id = self._slot_for_broker_position(existing, used_slots, trade_slots)
            self._ensure_slot_exists(
                slot_id, slot_value=slot_value, updated_at=timestamp
            )
            used_slots.add(slot_id)
            entry_price = float(broker_position["entry_price"])
            quantity = float(broker_position["quantity"])
            target_price = round(entry_price * (1 + target_pct), 4)
            stop_price = round(entry_price * (1 - stop_pct), 4)
            metadata = broker_position.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            if existing is None:
                position = self.create_open(
                    broker_position_id=_optional_dict_str(
                        broker_position, "broker_position_id"
                    ),
                    symbol=symbol,
                    slot_id=slot_id,
                    quantity=quantity,
                    entry_price=entry_price,
                    target_price=target_price,
                    stop_price=stop_price,
                    opened_at=timestamp,
                    metadata={**metadata, "reconciled": "broker_open_at_boot"},
                )
            else:
                self.connection.execute(
                    """
                    UPDATE positions
                    SET broker_position_id = ?,
                        slot_id = ?,
                        quantity = ?,
                        entry_price = ?,
                        target_price = ?,
                        stop_price = ?,
                        metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        _optional_dict_str(broker_position, "broker_position_id"),
                        slot_id,
                        quantity,
                        entry_price,
                        target_price,
                        stop_price,
                        _json_dumps(
                            {
                                **(existing.metadata or {}),
                                **metadata,
                                "reconciled": "broker_truth_at_boot",
                            }
                        ),
                        existing.id,
                    ),
                )
                if self.get(existing.id) is None:
                    raise RuntimeError("reconciled position disappeared")
                position = existing

            self._upsert_slot(
                slot_id,
                # Use "OPEN" (the canonical active-slot status) instead of
                # the legacy "occupied" string — otherwise SlotAllocator's
                # ACTIVE_SLOT_STATUSES check skips these slots and the same
                # symbol can be re-allocated, breaking duplicate-symbol and
                # day-cap gates.
                status="OPEN",
                slot_value=slot_value,
                symbol=symbol,
                position_id=position.id,
                updated_at=timestamp,
                metadata={"reconciled": "broker_open_at_boot"},
            )

        self.connection.commit()
        return "mismatch_corrected" if broker_symbols != local_symbols else "matched"

    def get(self, position_id: int) -> PositionRecord | None:
        row = self.connection.execute(
            """
            SELECT
                id, broker_position_id, symbol, slot_id, status, quantity, entry_price,
                target_price, stop_price, opened_at, closed_at, exit_reason, realized_pnl, metadata_json
            FROM positions
            WHERE id = ?
            """,
            (position_id,),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def _slot_for_broker_position(
        self,
        existing: PositionRecord | None,
        used_slots: set[int],
        trade_slots: int,
    ) -> int:
        if (
            existing is not None
            and existing.slot_id is not None
            and existing.slot_id not in used_slots
        ):
            return existing.slot_id
        rows = self.connection.execute(
            """
            SELECT slot_id
            FROM slots
            WHERE status = 'available'
            ORDER BY slot_id
            """
        ).fetchall()
        for row in rows:
            slot_id = int(row["slot_id"])
            if slot_id not in used_slots:
                return slot_id
        for slot_id in range(1, trade_slots + 1):
            if slot_id not in used_slots:
                return slot_id
        raise RuntimeError(
            "broker has more open positions than configured DriftPilot slots"
        )

    def _upsert_slot(
        self,
        slot_id: int,
        *,
        status: str,
        slot_value: float,
        symbol: str | None = None,
        position_id: int | None = None,
        reserved_order_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        timestamp = updated_at or self.clock.now_utc()
        self.connection.execute(
            """
            INSERT INTO slots (
                slot_id, status, symbol, position_id, reserved_order_id, slot_value, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slot_id) DO UPDATE SET
                status = excluded.status,
                symbol = excluded.symbol,
                position_id = excluded.position_id,
                reserved_order_id = excluded.reserved_order_id,
                slot_value = excluded.slot_value,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                slot_id,
                status,
                symbol,
                position_id,
                reserved_order_id,
                slot_value,
                datetime_to_storage(timestamp),
                _json_dumps(metadata),
            ),
        )

    def _ensure_slot_exists(
        self, slot_id: int, *, slot_value: float, updated_at: datetime
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO slots (slot_id, status, slot_value, updated_at, metadata_json)
            VALUES (?, 'available', ?, ?, '{}')
            ON CONFLICT(slot_id) DO NOTHING
            """,
            (slot_id, slot_value, datetime_to_storage(updated_at)),
        )

    def _from_row(self, row: sqlite3.Row) -> PositionRecord:
        return PositionRecord(
            id=row["id"],
            broker_position_id=row["broker_position_id"],
            symbol=row["symbol"],
            slot_id=row["slot_id"],
            status=row["status"],
            quantity=row["quantity"],
            entry_price=row["entry_price"],
            target_price=row["target_price"],
            stop_price=row["stop_price"],
            opened_at=datetime_from_storage(row["opened_at"]),
            closed_at=datetime_from_storage(row["closed_at"])
            if row["closed_at"]
            else None,
            exit_reason=row["exit_reason"],
            realized_pnl=row["realized_pnl"],
            metadata=_json_loads_object(row["metadata_json"]),
        )


class DailyCounterRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def get(
        self, counter_name: str, *, date_et: date | None = None
    ) -> DailyCounterRecord:
        counter_date = date_et or self.clock.date_et()
        row = self.connection.execute(
            """
            SELECT date_et, counter_name, counter_value, updated_at
            FROM daily_counters
            WHERE date_et = ? AND counter_name = ?
            """,
            (_date_to_storage(counter_date), counter_name),
        ).fetchone()
        if row is None:
            return DailyCounterRecord(
                date_et=counter_date,
                counter_name=counter_name,
                counter_value=0,
                updated_at=self.clock.now_utc(),
            )
        return DailyCounterRecord(
            date_et=date.fromisoformat(row["date_et"]),
            counter_name=row["counter_name"],
            counter_value=row["counter_value"],
            updated_at=datetime_from_storage(row["updated_at"]),
        )

    def increment(
        self,
        counter_name: str,
        *,
        amount: int = 1,
        date_et: date | None = None,
        updated_at: datetime | None = None,
    ) -> DailyCounterRecord:
        if amount < 1:
            raise ValueError("amount must be positive")
        counter_date = date_et or self.clock.date_et()
        timestamp = updated_at or self.clock.now_utc()
        self.connection.execute(
            """
            INSERT INTO daily_counters (date_et, counter_name, counter_value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date_et, counter_name) DO UPDATE SET
                counter_value = daily_counters.counter_value + excluded.counter_value,
                updated_at = excluded.updated_at
            """,
            (
                _date_to_storage(counter_date),
                counter_name,
                amount,
                datetime_to_storage(timestamp),
            ),
        )
        self.connection.commit()
        return self.get(counter_name, date_et=counter_date)


class OrderRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def create(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        status: str,
        quantity: float,
        broker_order_id: str | None = None,
        position_id: int | None = None,
        slot_id: int | None = None,
        limit_price: float | None = None,
        metadata: dict[str, Any] | None = None,
        submitted_at: datetime | None = None,
    ) -> OrderRecord:
        timestamp = submitted_at or self.clock.now_utc()
        cursor = self.connection.execute(
            """
            INSERT INTO orders (
                broker_order_id, position_id, slot_id, symbol, side, order_type,
                status, quantity, limit_price, submitted_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                broker_order_id,
                position_id,
                slot_id,
                symbol.upper(),
                side,
                order_type,
                status,
                quantity,
                limit_price,
                datetime_to_storage(timestamp),
                datetime_to_storage(timestamp),
                _json_dumps(metadata),
            ),
        )
        order_id = _last_insert_id(cursor)
        self.connection.commit()
        return OrderRecord(
            id=order_id,
            broker_order_id=broker_order_id,
            position_id=position_id,
            slot_id=slot_id,
            symbol=symbol.upper(),
            side=side,
            order_type=order_type,
            status=status,
            quantity=quantity,
            limit_price=limit_price,
            submitted_at=timestamp,
            updated_at=timestamp,
            metadata=metadata or {},
        )

    def update_status(
        self,
        order_id: int,
        *,
        status: str,
        metadata: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        timestamp = updated_at or self.clock.now_utc()
        self.connection.execute(
            """
            UPDATE orders
            SET status = ?, updated_at = ?, metadata_json = ?
            WHERE id = ?
            """,
            (status, datetime_to_storage(timestamp), _json_dumps(metadata), order_id),
        )
        self.connection.commit()

    def list_all(self) -> list[OrderRecord]:
        rows = self.connection.execute(
            """
            SELECT id, broker_order_id, position_id, slot_id, symbol, side, order_type,
                   status, quantity, limit_price, submitted_at, updated_at, metadata_json
            FROM orders
            ORDER BY id
            """
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def _from_row(self, row: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            id=row["id"],
            broker_order_id=row["broker_order_id"],
            position_id=row["position_id"],
            slot_id=row["slot_id"],
            symbol=row["symbol"],
            side=row["side"],
            order_type=row["order_type"],
            status=row["status"],
            quantity=row["quantity"],
            limit_price=row["limit_price"],
            submitted_at=datetime_from_storage(row["submitted_at"]),
            updated_at=datetime_from_storage(row["updated_at"]),
            metadata=_json_loads_object(row["metadata_json"]),
        )


class AllocatorStateRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def set(
        self,
        *,
        status: str,
        updated_at: datetime | None = None,
        locked_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AllocatorStateRecord:
        timestamp = updated_at or self.clock.now_utc()
        self.connection.execute(
            """
            INSERT INTO allocator_state (id, status, locked_at, updated_at, metadata_json)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                locked_at = excluded.locked_at,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                status,
                datetime_to_storage(locked_at) if locked_at is not None else None,
                datetime_to_storage(timestamp),
                _json_dumps(metadata),
            ),
        )
        self.connection.commit()
        return AllocatorStateRecord(
            status=status,
            locked_at=locked_at,
            updated_at=timestamp,
            metadata=metadata or {},
        )

    def get(self) -> AllocatorStateRecord | None:
        row = self.connection.execute(
            """
            SELECT status, locked_at, updated_at, metadata_json
            FROM allocator_state
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return None
        return AllocatorStateRecord(
            status=row["status"],
            locked_at=datetime_from_storage(row["locked_at"])
            if row["locked_at"]
            else None,
            updated_at=datetime_from_storage(row["updated_at"]),
            metadata=_json_loads_object(row["metadata_json"]),
        )


class FillRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def record(self, fill: Any) -> FillRecord:
        metadata = dict(getattr(fill, "metadata", {}) or {})
        cursor = self.connection.execute(
            """
            INSERT INTO fills (
                order_id, broker_fill_id, symbol, side, quantity, price, filled_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                getattr(fill, "order_id", None),
                getattr(fill, "broker_fill_id", None),
                str(getattr(fill, "symbol")).upper(),
                str(getattr(fill, "side")),
                float(getattr(fill, "quantity")),
                float(getattr(fill, "price")),
                datetime_to_storage(getattr(fill, "filled_at")),
                _json_dumps(metadata),
            ),
        )
        fill_id = _last_insert_id(cursor)
        self.connection.commit()
        return FillRecord(
            id=fill_id,
            order_id=getattr(fill, "order_id", None),
            broker_fill_id=getattr(fill, "broker_fill_id", None),
            symbol=str(getattr(fill, "symbol")).upper(),
            side=str(getattr(fill, "side")),
            quantity=float(getattr(fill, "quantity")),
            price=float(getattr(fill, "price")),
            filled_at=getattr(fill, "filled_at"),
            metadata=metadata,
        )

    def list_all(self) -> list[FillRecord]:
        rows = self.connection.execute(
            """
            SELECT id, order_id, broker_fill_id, symbol, side, quantity, price, filled_at, metadata_json
            FROM fills
            ORDER BY id
            """
        ).fetchall()
        return [
            FillRecord(
                id=row["id"],
                order_id=row["order_id"],
                broker_fill_id=row["broker_fill_id"],
                symbol=row["symbol"],
                side=row["side"],
                quantity=row["quantity"],
                price=row["price"],
                filled_at=datetime_from_storage(row["filled_at"]),
                metadata=_json_loads_object(row["metadata_json"]),
            )
            for row in rows
        ]


class CandidateQueueRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def mark_blocked(
        self,
        symbol: str,
        *,
        reason: str,
        features: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        timestamp = updated_at or self.clock.now_utc()
        normalized = symbol.upper()
        existing = self.connection.execute(
            """
            SELECT id
            FROM candidate_queue
            WHERE symbol = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO candidate_queue (
                    symbol, score, rank, status, blocked_reason, features_json, created_at, updated_at
                )
                VALUES (?, 0, 0, 'blocked', ?, ?, ?, ?)
                """,
                (
                    normalized,
                    reason,
                    _json_dumps(features),
                    datetime_to_storage(timestamp),
                    datetime_to_storage(timestamp),
                ),
            )
        else:
            self.connection.execute(
                """
                UPDATE candidate_queue
                SET status = 'blocked',
                    blocked_reason = ?,
                    features_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    reason,
                    _json_dumps(features),
                    datetime_to_storage(timestamp),
                    existing["id"],
                ),
            )
        self.connection.commit()

    def blocked_reason(self, symbol: str) -> str | None:
        row = self.connection.execute(
            """
            SELECT blocked_reason
            FROM candidate_queue
            WHERE symbol = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (symbol.upper(),),
        ).fetchone()
        return None if row is None else row["blocked_reason"]


class PriceDriftBaselineRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def get(self, symbol: str, event_key: str) -> PriceDriftBaselineRecord | None:
        row = self.connection.execute(
            """
            SELECT symbol, event_key, first_seen_price, first_seen_at,
                   last_seen_price, last_seen_at, drift_pct, metadata_json
            FROM price_drift_baselines
            WHERE symbol = ? AND event_key = ?
            """,
            (symbol.upper(), event_key),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def get_or_create(
        self,
        *,
        symbol: str,
        event_key: str,
        price: float,
        seen_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PriceDriftBaselineRecord:
        existing = self.get(symbol, event_key)
        if existing is not None:
            return existing
        timestamp = seen_at or self.clock.now_utc()
        normalized_symbol = symbol.upper()
        self.connection.execute(
            """
            INSERT INTO price_drift_baselines (
                symbol, event_key, first_seen_price, first_seen_at,
                last_seen_price, last_seen_at, drift_pct, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                normalized_symbol,
                event_key,
                price,
                datetime_to_storage(timestamp),
                price,
                datetime_to_storage(timestamp),
                _json_dumps(metadata),
            ),
        )
        self.connection.commit()
        return PriceDriftBaselineRecord(
            symbol=normalized_symbol,
            event_key=event_key,
            first_seen_price=price,
            first_seen_at=timestamp,
            last_seen_price=price,
            last_seen_at=timestamp,
            drift_pct=0.0,
            metadata=metadata or {},
        )

    def update_seen(
        self,
        *,
        symbol: str,
        event_key: str,
        price: float,
        seen_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PriceDriftBaselineRecord:
        baseline = self.get_or_create(
            symbol=symbol,
            event_key=event_key,
            price=price,
            seen_at=seen_at,
            metadata=metadata,
        )
        timestamp = seen_at or self.clock.now_utc()
        drift_pct = (
            abs(price - baseline.first_seen_price) / baseline.first_seen_price * 100.0
            if baseline.first_seen_price > 0
            else 0.0
        )
        merged_metadata = {**(baseline.metadata or {}), **(metadata or {})}
        self.connection.execute(
            """
            UPDATE price_drift_baselines
            SET last_seen_price = ?,
                last_seen_at = ?,
                drift_pct = ?,
                metadata_json = ?
            WHERE symbol = ? AND event_key = ?
            """,
            (
                price,
                datetime_to_storage(timestamp),
                drift_pct,
                _json_dumps(merged_metadata),
                baseline.symbol,
                baseline.event_key,
            ),
        )
        self.connection.commit()
        updated = self.get(baseline.symbol, baseline.event_key)
        if updated is None:
            raise RuntimeError("price drift baseline disappeared after update")
        return updated

    def list_recent(self, *, limit: int = 500) -> list[PriceDriftBaselineRecord]:
        rows = self.connection.execute(
            """
            SELECT symbol, event_key, first_seen_price, first_seen_at,
                   last_seen_price, last_seen_at, drift_pct, metadata_json
            FROM price_drift_baselines
            ORDER BY last_seen_at DESC, symbol, event_key
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def prune_before(self, cutoff: datetime) -> int:
        cursor = self.connection.execute(
            "DELETE FROM price_drift_baselines WHERE last_seen_at < ?",
            (datetime_to_storage(cutoff),),
        )
        self.connection.commit()
        return cursor.rowcount if cursor.rowcount is not None else 0

    def _from_row(self, row: sqlite3.Row) -> PriceDriftBaselineRecord:
        return PriceDriftBaselineRecord(
            symbol=row["symbol"],
            event_key=row["event_key"],
            first_seen_price=row["first_seen_price"],
            first_seen_at=datetime_from_storage(row["first_seen_at"]),
            last_seen_price=row["last_seen_price"],
            last_seen_at=datetime_from_storage(row["last_seen_at"]),
            drift_pct=row["drift_pct"],
            metadata=_json_loads_object(row["metadata_json"]),
        )


class ErrorRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def record(
        self,
        *,
        severity: str,
        message: str,
        metadata: dict[str, Any] | None = None,
        raised_at: datetime | None = None,
    ) -> ErrorRecord:
        timestamp = raised_at or self.clock.now_utc()
        cursor = self.connection.execute(
            """
            INSERT INTO errors (severity, message, raised_at, metadata_json)
            VALUES (?, ?, ?, ?)
            """,
            (severity, message, datetime_to_storage(timestamp), _json_dumps(metadata)),
        )
        error_id = _last_insert_id(cursor)
        self.connection.commit()
        return ErrorRecord(
            id=error_id,
            severity=severity,
            message=message,
            raised_at=timestamp,
            metadata=metadata or {},
        )


class StreamStateRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def get(self, name: str) -> StreamStateRecord:
        row = self.connection.execute(
            """
            SELECT name, shard_cursor, updated_at, metadata_json
            FROM stream_state
            WHERE name = ?
            """,
            (name,),
        ).fetchone()
        if row is None:
            return StreamStateRecord(
                name=name,
                shard_cursor=0,
                updated_at=self.clock.now_utc(),
                metadata={},
            )
        return StreamStateRecord(
            name=row["name"],
            shard_cursor=row["shard_cursor"],
            updated_at=datetime_from_storage(row["updated_at"]),
            metadata=_json_loads_object(row["metadata_json"]),
        )

    def set_cursor(
        self,
        name: str,
        shard_cursor: int,
        *,
        metadata: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> StreamStateRecord:
        timestamp = updated_at or self.clock.now_utc()
        self.connection.execute(
            """
            INSERT INTO stream_state (name, shard_cursor, updated_at, metadata_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                shard_cursor = excluded.shard_cursor,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (name, shard_cursor, datetime_to_storage(timestamp), _json_dumps(metadata)),
        )
        self.connection.commit()
        return StreamStateRecord(name=name, shard_cursor=shard_cursor, updated_at=timestamp, metadata=metadata or {})


class DriftPilotRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None
    ) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()
        self.state = StateRepository(connection, self.clock)
        self.transitions = TransitionRepository(connection, self.clock)
        self.slots = SlotRepository(connection, self.clock)
        self.positions = PositionRepository(connection, self.clock)
        self.daily_counters = DailyCounterRepository(connection, self.clock)
        self.orders = OrderRepository(connection, self.clock)
        self.stream_state = StreamStateRepository(connection, self.clock)
        self.allocator_state = AllocatorStateRepository(connection, self.clock)
        self.fills = FillRepository(connection)
        self.candidate_queue = CandidateQueueRepository(connection, self.clock)
        self.price_drift_baselines = PriceDriftBaselineRepository(
            connection, self.clock
        )
        self.errors = ErrorRepository(connection, self.clock)

    @classmethod
    def open(
        cls, path: str | Path, clock: DriftPilotClock | None = None
    ) -> DriftPilotRepository:
        connection = connect(path)
        initialize_schema(connection)
        return cls(connection, clock)

    def upsert_candidate_queue_row(
        self,
        *,
        symbol: str,
        score: float,
        rvol: float,
        vwap_distance_pct: float,
        return_15m_pct: float,
        sector: str,
        blocked_reason: str | None,
        queue_status: str,
        cycle_at: datetime,
    ) -> None:
        normalized = symbol.upper()
        features = {
            "rvol": rvol,
            "vwap_distance_pct": vwap_distance_pct,
            "return_15m_pct": return_15m_pct,
            "sector": sector,
            "cycle_at": datetime_to_storage(cycle_at),
        }
        existing = self.connection.execute(
            """
            SELECT id
            FROM candidate_queue
            WHERE symbol = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        rank = self._next_candidate_rank(cycle_at) if existing is None else None
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO candidate_queue (
                    symbol, score, rank, status, blocked_reason, features_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized,
                    score,
                    rank,
                    queue_status,
                    blocked_reason,
                    _json_dumps(features),
                    datetime_to_storage(cycle_at),
                    datetime_to_storage(cycle_at),
                ),
            )
        else:
            self.connection.execute(
                """
                UPDATE candidate_queue
                SET score = ?,
                    status = ?,
                    blocked_reason = ?,
                    features_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    score,
                    queue_status,
                    blocked_reason,
                    _json_dumps(features),
                    datetime_to_storage(cycle_at),
                    existing["id"],
                ),
            )
        self.connection.commit()

    def clear_candidate_queue(self, *, before: datetime) -> None:
        self.connection.execute(
            "DELETE FROM candidate_queue WHERE updated_at < ?",
            (datetime_to_storage(before),),
        )
        self.connection.commit()

    def list_candidates(self, *, limit: int = 20) -> list[CandidateRow]:
        rows = self.connection.execute(
            """
            SELECT symbol, score, status, blocked_reason, features_json, updated_at
            FROM candidate_queue
            ORDER BY score DESC, symbol
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        candidates: list[CandidateRow] = []
        for row in rows:
            features = _json_loads_object(row["features_json"])
            candidates.append(
                CandidateRow(
                    symbol=row["symbol"],
                    score=row["score"],
                    rvol=float(features.get("rvol", 0.0)),
                    vwap_distance_pct=float(features.get("vwap_distance_pct", 0.0)),
                    return_15m_pct=float(features.get("return_15m_pct", 0.0)),
                    sector=str(features.get("sector", "")),
                    blocked_reason=row["blocked_reason"],
                    queue_status=row["status"],
                    cycle_at=datetime_from_storage(row["updated_at"]),
                )
            )
        return candidates

    def increment_daily_counter(
        self, *, date_et: date, counter_name: str, delta: int = 1
    ) -> int:
        if delta < 1:
            raise ValueError("delta must be positive")
        return self.daily_counters.increment(
            counter_name,
            amount=delta,
            date_et=date_et,
        ).counter_value

    def get_daily_counter(self, *, date_et: date, counter_name: str) -> int:
        return self.daily_counters.get(counter_name, date_et=date_et).counter_value

    def record_recycle_event(
        self,
        *,
        slot_id: int,
        freed_symbol: str,
        exit_reason: str,
        exit_pnl_pct: float,
        replacement_symbol: str | None,
        at: datetime,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO recycle_events (slot_id, reason, timestamp, metadata_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                slot_id,
                exit_reason,
                datetime_to_storage(at),
                _json_dumps(
                    {
                        "freed_symbol": freed_symbol.upper(),
                        "exit_pnl_pct": exit_pnl_pct,
                        "replacement_symbol": replacement_symbol.upper()
                        if replacement_symbol
                        else None,
                    }
                ),
            ),
        )
        self.connection.commit()

    def list_recycle_events(self, *, limit: int = 20) -> list[RecycleEvent]:
        rows = self.connection.execute(
            """
            SELECT id, slot_id, reason, timestamp, metadata_json
            FROM recycle_events
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        events: list[RecycleEvent] = []
        for row in rows:
            metadata = _json_loads_object(row["metadata_json"])
            replacement = metadata.get("replacement_symbol")
            events.append(
                RecycleEvent(
                    id=row["id"],
                    slot_id=row["slot_id"],
                    freed_symbol=str(metadata.get("freed_symbol", "")),
                    exit_reason=row["reason"],
                    exit_pnl_pct=float(metadata.get("exit_pnl_pct", 0.0)),
                    replacement_symbol=str(replacement) if replacement else None,
                    at=datetime_from_storage(row["timestamp"]),
                )
            )
        return events

    def update_position_state(
        self, *, position_id: int, state: str, metadata: dict | None = None
    ) -> None:
        existing = self.positions.get(position_id)
        if existing is None:
            raise ValueError(f"position {position_id} does not exist")
        merged_metadata = {**(existing.metadata or {}), **(metadata or {})}
        self.connection.execute(
            """
            UPDATE positions
            SET status = ?, metadata_json = ?
            WHERE id = ?
            """,
            (state, _json_dumps(merged_metadata), position_id),
        )
        self.connection.commit()

    def record_fill(
        self,
        *,
        order_id: int,
        symbol: str,
        side: str,
        qty: int,
        reference_price: float,
        slippage_applied: float,
        fill_price: float,
        at: datetime,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO fills (
                order_id, symbol, side, quantity, price, filled_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                symbol.upper(),
                side,
                qty,
                fill_price,
                datetime_to_storage(at),
                _json_dumps(
                    {
                        "reference_price": reference_price,
                        "slippage_applied": slippage_applied,
                    }
                ),
            ),
        )
        self.connection.commit()

    def _next_candidate_rank(self, cycle_at: datetime) -> int:
        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(rank), 0) AS max_rank
            FROM candidate_queue
            WHERE updated_at = ?
            """,
            (datetime_to_storage(cycle_at),),
        ).fetchone()
        return int(row["max_rank"]) + 1
