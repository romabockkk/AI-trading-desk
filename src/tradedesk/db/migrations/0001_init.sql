-- 0001_init: core persistent memory of the trading desk.
-- All timestamps are ISO-8601 UTC strings ending with 'Z'.
-- JSON payloads are stored as TEXT validated by json_valid().

CREATE TABLE market_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_id     TEXT NOT NULL UNIQUE,
    created_utc     TEXT NOT NULL,
    source          TEXT NOT NULL,            -- e.g. MOCK, MT5_DEMO, CSV:<file>
    is_mock         INTEGER NOT NULL CHECK (is_mock IN (0,1)),
    symbol          TEXT NOT NULL,
    timeframe       TEXT,
    data_from_utc   TEXT,
    data_to_utc     TEXT,
    bid             REAL,
    ask             REAL,
    spread_points   REAL,
    validation_status TEXT NOT NULL CHECK (validation_status IN ('VALID','INVALID','UNVALIDATED')),
    validation_errors TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(validation_errors)),
    payload         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload)),
    content_sha256  TEXT NOT NULL
);
CREATE INDEX ix_snap_symbol_time ON market_snapshots(symbol, created_utc);

CREATE TABLE agent_reports (
    id              INTEGER PRIMARY KEY,
    report_id       TEXT NOT NULL UNIQUE,
    cycle_id        TEXT NOT NULL,
    agent           TEXT NOT NULL,
    created_utc     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('OK','FAILED','TIMEOUT','INVALID_OUTPUT','SKIPPED')),
    is_critical     INTEGER NOT NULL CHECK (is_critical IN (0,1)),
    snapshot_ids    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(snapshot_ids)),
    payload         TEXT NOT NULL CHECK (json_valid(payload)),
    llm_provider    TEXT,
    llm_model       TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    latency_ms      INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 1,
    error           TEXT
);
CREATE INDEX ix_reports_cycle ON agent_reports(cycle_id);

CREATE TABLE debates (
    id              INTEGER PRIMARY KEY,
    cycle_id        TEXT NOT NULL,
    round           INTEGER NOT NULL CHECK (round BETWEEN 1 AND 2),
    seq             INTEGER NOT NULL,
    from_agent      TEXT NOT NULL,
    to_agent        TEXT,
    message_type    TEXT NOT NULL CHECK (message_type IN ('HYPOTHESIS','ALTERNATIVE','OBJECTION','RESPONSE','DEMAND_NO_TRADE','CONCESSION')),
    created_utc     TEXT NOT NULL,
    payload         TEXT NOT NULL CHECK (json_valid(payload)),
    UNIQUE (cycle_id, round, seq)
);

CREATE TABLE strategy_versions (
    id              INTEGER PRIMARY KEY,
    strategy_id     TEXT NOT NULL,
    version         TEXT NOT NULL,
    created_utc     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('DRAFT','RESEARCH','APPROVED_PAPER','ACTIVE_PAPER','RETIRED','ROLLED_BACK')),
    parent_version  TEXT,
    params          TEXT NOT NULL CHECK (json_valid(params)),
    rules_text      TEXT NOT NULL,
    code_sha256     TEXT,
    approved_by     TEXT,                    -- owner only; agents cannot approve
    approved_utc    TEXT,
    notes           TEXT,
    UNIQUE (strategy_id, version)
);

CREATE TABLE decisions (
    id              INTEGER PRIMARY KEY,
    decision_id     TEXT NOT NULL UNIQUE,
    cycle_id        TEXT NOT NULL,
    created_utc     TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('RESEARCH','BACKTEST','PAPER','BROKER_DEMO')),
    symbol          TEXT NOT NULL,
    strategy_id     TEXT,
    strategy_version TEXT,
    action          TEXT NOT NULL CHECK (action IN ('LONG','SHORT','NO_TRADE')),
    entry           REAL,
    stop_loss       REAL,
    take_profit     REAL,
    volume_lots     REAL,
    planned_risk_pct REAL,
    planned_rr      REAL,
    risk_gate_result TEXT NOT NULL CHECK (risk_gate_result IN ('ALLOW','DENY','NOT_EVALUATED')),
    risk_gate_reasons TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(risk_gate_reasons)),
    risk_limits_sha256 TEXT,
    reasons         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(reasons)),
    objections      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(objections)),
    snapshot_ids    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(snapshot_ids)),
    report_ids      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(report_ids)),
    no_trade_reason TEXT,
    outcome         TEXT,                    -- filled post-hoc by Performance agent
    outcome_metrics TEXT CHECK (outcome_metrics IS NULL OR json_valid(outcome_metrics)),
    CHECK (action = 'NO_TRADE' OR (stop_loss IS NOT NULL AND entry IS NOT NULL)),
    CHECK (action = 'NO_TRADE' OR risk_gate_result = 'ALLOW' OR no_trade_reason IS NOT NULL)
);
CREATE INDEX ix_decisions_time ON decisions(created_utc);

CREATE TABLE orders (
    id              INTEGER PRIMARY KEY,
    client_order_id TEXT NOT NULL UNIQUE,    -- idempotency key: prevents duplicate submission
    decision_id     TEXT NOT NULL REFERENCES decisions(decision_id),
    venue           TEXT NOT NULL CHECK (venue IN ('PAPER','BROKER_DEMO')),
    created_utc     TEXT NOT NULL,
    updated_utc     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    order_type      TEXT NOT NULL CHECK (order_type IN ('MARKET','LIMIT','STOP')),
    volume_lots     REAL NOT NULL CHECK (volume_lots > 0),
    price           REAL,
    stop_loss       REAL NOT NULL,
    take_profit     REAL,
    status          TEXT NOT NULL CHECK (status IN ('PENDING_SUBMIT','SUBMITTED','ACCEPTED','REJECTED','FILLED','PARTIALLY_FILLED','CANCELLED','UNKNOWN')),
    broker_order_id TEXT,
    broker_server   TEXT,
    broker_account_is_demo INTEGER CHECK (broker_account_is_demo IS NULL OR broker_account_is_demo = 1),
    error           TEXT
);

CREATE TABLE fills (
    id              INTEGER PRIMARY KEY,
    fill_id         TEXT NOT NULL UNIQUE,
    client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
    filled_utc      TEXT NOT NULL,
    price           REAL NOT NULL,
    volume_lots     REAL NOT NULL CHECK (volume_lots > 0),
    commission      REAL NOT NULL DEFAULT 0,
    slippage_points REAL,
    broker_deal_id  TEXT
);

CREATE TABLE positions (
    id              INTEGER PRIMARY KEY,
    position_id     TEXT NOT NULL UNIQUE,
    venue           TEXT NOT NULL CHECK (venue IN ('PAPER','BROKER_DEMO')),
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    volume_lots     REAL NOT NULL,
    open_price      REAL NOT NULL,
    stop_loss       REAL NOT NULL,
    take_profit     REAL,
    opened_utc      TEXT NOT NULL,
    updated_utc     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED')),
    client_order_id TEXT REFERENCES orders(client_order_id),
    broker_ticket   TEXT,
    last_reconciled_utc TEXT
);

CREATE TABLE trades (
    id              INTEGER PRIMARY KEY,
    trade_id        TEXT NOT NULL UNIQUE,
    position_id     TEXT NOT NULL REFERENCES positions(position_id),
    decision_id     TEXT NOT NULL REFERENCES decisions(decision_id),
    venue           TEXT NOT NULL CHECK (venue IN ('BACKTEST','PAPER','BROKER_DEMO')),
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    strategy_id     TEXT,
    strategy_version TEXT,
    opened_utc      TEXT NOT NULL,
    closed_utc      TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    exit_price      REAL NOT NULL,
    volume_lots     REAL NOT NULL,
    gross_pnl       REAL NOT NULL,
    commission      REAL NOT NULL DEFAULT 0,
    swap            REAL NOT NULL DEFAULT 0,
    net_pnl         REAL NOT NULL,
    planned_risk_money REAL NOT NULL CHECK (planned_risk_money > 0),
    r_multiple      REAL NOT NULL,
    exit_reason     TEXT NOT NULL CHECK (exit_reason IN ('STOP_LOSS','TAKE_PROFIT','MANUAL','TIME_EXIT','INVALIDATION','EMERGENCY_STOP','OTHER')),
    error_tags      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(error_tags))
);

CREATE TABLE risk_events (
    id              INTEGER PRIMARY KEY,
    created_utc     TEXT NOT NULL,
    event_type      TEXT NOT NULL,           -- GATE_DENY, DAILY_DD_STOP, LOSS_STREAK_PAUSE, KILL_SWITCH, VETO, ...
    severity        TEXT NOT NULL CHECK (severity IN ('INFO','WARN','CRITICAL')),
    decision_id     TEXT,
    details         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details))
);

CREATE TABLE errors (
    id              INTEGER PRIMARY KEY,
    created_utc     TEXT NOT NULL,
    component       TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('INFO','WARN','ERROR','CRITICAL')),
    message         TEXT NOT NULL,
    cycle_id        TEXT,
    details         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details)),
    resolved_utc    TEXT
);

CREATE TABLE lessons (
    id              INTEGER PRIMARY KEY,
    lesson_id       TEXT NOT NULL UNIQUE,
    created_utc     TEXT NOT NULL,
    trade_id        TEXT,
    decision_id     TEXT,
    error_category  TEXT NOT NULL CHECK (error_category IN (
        'FALSE_CHOCH','FAILED_ORDER_BLOCK','WRONG_REGIME','NEWS','SPREAD','LATENCY',
        'EXECUTION','RISK_ESTIMATE','DATA_QUALITY','NONE','OTHER')),
    description     TEXT NOT NULL,
    evidence        TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence)),
    becomes_hypothesis INTEGER NOT NULL DEFAULT 0 CHECK (becomes_hypothesis IN (0,1))
);

CREATE TABLE experiments (
    id              INTEGER PRIMARY KEY,
    experiment_id   TEXT NOT NULL UNIQUE,
    created_utc     TEXT NOT NULL,
    hypothesis      TEXT NOT NULL,
    stage           TEXT NOT NULL CHECK (stage IN ('PROPOSAL','BACKTEST','OUT_OF_SAMPLE','BASELINE_COMPARISON','AWAITING_APPROVAL','APPROVED','REJECTED','PAPER_ROLLOUT','ROLLED_BACK')),
    strategy_id     TEXT,
    baseline_version TEXT,
    candidate_version TEXT,
    dataset         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(dataset)),
    in_sample_metrics TEXT CHECK (in_sample_metrics IS NULL OR json_valid(in_sample_metrics)),
    oos_metrics     TEXT CHECK (oos_metrics IS NULL OR json_valid(oos_metrics)),
    baseline_metrics TEXT CHECK (baseline_metrics IS NULL OR json_valid(baseline_metrics)),
    conclusion      TEXT,
    approved_by     TEXT,
    approved_utc    TEXT
);

CREATE TABLE weekly_reviews (
    id              INTEGER PRIMARY KEY,
    week_start_utc  TEXT NOT NULL,
    week_end_utc    TEXT NOT NULL,
    created_utc     TEXT NOT NULL,
    venue           TEXT NOT NULL,
    n_trades        INTEGER NOT NULL,
    metrics         TEXT NOT NULL CHECK (json_valid(metrics)),
    statistically_sufficient INTEGER NOT NULL CHECK (statistically_sufficient IN (0,1)),
    report          TEXT NOT NULL,
    UNIQUE (week_start_utc, venue)
);

-- Append-only, hash-chained audit trail.
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY,
    created_utc     TEXT NOT NULL,
    actor           TEXT NOT NULL,           -- OWNER, SYSTEM, agent name
    action          TEXT NOT NULL,
    entity          TEXT,
    entity_id       TEXT,
    details         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details)),
    prev_hash       TEXT NOT NULL,
    row_hash        TEXT NOT NULL UNIQUE
);
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- Operational switches (pause, kill switch). Changed only via backend actions, audited.
CREATE TABLE system_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_utc     TEXT NOT NULL,
    updated_by      TEXT NOT NULL
);
INSERT INTO system_state(key, value, updated_utc, updated_by) VALUES
    ('kill_switch', 'OFF', strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'SYSTEM'),
    ('paused', 'false', strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'SYSTEM');
