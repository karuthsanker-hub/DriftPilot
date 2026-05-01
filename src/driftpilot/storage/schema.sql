PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS operator_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_state TEXT NOT NULL,
    active_gate TEXT,
    last_transition_id INTEGER,
    last_error_id INTEGER,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (last_transition_id) REFERENCES state_transitions(id),
    FOREIGN KEY (last_error_id) REFERENCES errors(id)
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS slots (
    slot_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    symbol TEXT,
    position_id INTEGER,
    reserved_order_id INTEGER,
    slot_value REAL NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK (slot_id > 0),
    FOREIGN KEY (position_id) REFERENCES positions(id),
    FOREIGN KEY (reserved_order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_position_id TEXT,
    symbol TEXT NOT NULL,
    slot_id INTEGER,
    status TEXT NOT NULL,
    quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    target_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_reason TEXT,
    realized_pnl REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (slot_id) REFERENCES slots(slot_id)
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_order_id TEXT,
    position_id INTEGER,
    slot_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    quantity REAL NOT NULL,
    limit_price REAL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (position_id) REFERENCES positions(id),
    FOREIGN KEY (slot_id) REFERENCES slots(slot_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    broker_fill_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    filled_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS candidate_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    score REAL NOT NULL,
    rank INTEGER NOT NULL,
    status TEXT NOT NULL,
    blocked_reason TEXT,
    features_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidate_queue_status_rank ON candidate_queue(status, rank);

CREATE TABLE IF NOT EXISTS recycle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL,
    position_id INTEGER,
    reason TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (slot_id) REFERENCES slots(slot_id),
    FOREIGN KEY (position_id) REFERENCES positions(id)
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date_et TEXT PRIMARY KEY,
    realized_pnl REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_counters (
    date_et TEXT NOT NULL,
    counter_name TEXT NOT NULL,
    counter_value INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date_et, counter_name)
);

CREATE TABLE IF NOT EXISTS live_gate_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    passed INTEGER NOT NULL,
    evaluated_at TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    resolved_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS allocator_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    locked_at TEXT,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS universe (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    shard INTEGER,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sector_map (
    symbol TEXT PRIMARY KEY,
    sector TEXT NOT NULL,
    industry TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (symbol) REFERENCES universe(symbol)
);

CREATE TABLE IF NOT EXISTS stream_state (
    name TEXT PRIMARY KEY,
    shard_cursor INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
