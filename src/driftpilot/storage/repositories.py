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


def primary_key_columns(connection: sqlite3.Connection, table_name: str) -> list[tuple[str, int]]:
    if not table_name.replace("_", "").isalnum():
        raise ValueError("table_name must be a simple SQLite identifier")
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [(row["name"], row["pk"]) for row in rows if row["pk"]]


def _json_dumps(value: dict[str, Any] | list[Any] | None) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, separators=(",", ":"))


def _json_loads_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("stored JSON value must be an object")
    return parsed


def _date_to_storage(value: date) -> str:
    return value.isoformat()


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
class DailyCounterRecord:
    date_et: date
    counter_name: str
    counter_value: int
    updated_at: datetime


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


class StateRepository:
    def __init__(self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None) -> None:
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
    def __init__(self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None) -> None:
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
            (from_state, to_state, reason, datetime_to_storage(happened_at), _json_dumps(metadata)),
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


class SlotRepository:
    def __init__(self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None) -> None:
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


class DailyCounterRepository:
    def __init__(self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()

    def get(self, counter_name: str, *, date_et: date | None = None) -> DailyCounterRecord:
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
            (_date_to_storage(counter_date), counter_name, amount, datetime_to_storage(timestamp)),
        )
        self.connection.commit()
        return self.get(counter_name, date_et=counter_date)


class AllocatorStateRepository:
    def __init__(self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None) -> None:
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
        return AllocatorStateRecord(status=status, locked_at=locked_at, updated_at=timestamp, metadata=metadata or {})

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
            locked_at=datetime_from_storage(row["locked_at"]) if row["locked_at"] else None,
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
    def __init__(self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None) -> None:
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
                (normalized, reason, _json_dumps(features), datetime_to_storage(timestamp), datetime_to_storage(timestamp)),
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
                (reason, _json_dumps(features), datetime_to_storage(timestamp), existing["id"]),
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


class DriftPilotRepository:
    def __init__(self, connection: sqlite3.Connection, clock: DriftPilotClock | None = None) -> None:
        self.connection = connection
        self.clock = clock or DriftPilotClock()
        self.state = StateRepository(connection, self.clock)
        self.transitions = TransitionRepository(connection, self.clock)
        self.slots = SlotRepository(connection, self.clock)
        self.daily_counters = DailyCounterRepository(connection, self.clock)
        self.allocator_state = AllocatorStateRepository(connection, self.clock)
        self.fills = FillRepository(connection)
        self.candidate_queue = CandidateQueueRepository(connection, self.clock)

    @classmethod
    def open(cls, path: str | Path, clock: DriftPilotClock | None = None) -> DriftPilotRepository:
        connection = connect(path)
        initialize_schema(connection)
        return cls(connection, clock)
