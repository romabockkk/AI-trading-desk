-- 0002: internal paper accounts with a money ledger, instrument specs with provenance,
-- quote/bar storage for real data providers, daily reports.

CREATE TABLE paper_accounts (
    account_id      TEXT PRIMARY KEY,
    created_utc     TEXT NOT NULL,
    initial_balance REAL NOT NULL CHECK (initial_balance > 0),
    currency        TEXT NOT NULL DEFAULT 'USD',
    status          TEXT NOT NULL CHECK (status IN ('ACTIVE','ARCHIVED')),
    archived_utc    TEXT,
    note            TEXT
);
-- at most one ACTIVE account
CREATE UNIQUE INDEX ux_one_active_account ON paper_accounts(status) WHERE status = 'ACTIVE';

-- Every change of balance is a ledger row; balance = SUM(amount). Append-only.
CREATE TABLE account_ledger (
    id              INTEGER PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES paper_accounts(account_id),
    created_utc     TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('DEPOSIT','COMMISSION','REALIZED_PNL','SWAP')),
    amount          REAL NOT NULL,
    ref             TEXT,
    note            TEXT
);
CREATE INDEX ix_ledger_account ON account_ledger(account_id, id);
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON account_ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON account_ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;

ALTER TABLE orders ADD COLUMN account_id TEXT;
ALTER TABLE positions ADD COLUMN account_id TEXT;
ALTER TABLE positions ADD COLUMN commission_paid REAL NOT NULL DEFAULT 0;
ALTER TABLE positions ADD COLUMN swap_accrued REAL NOT NULL DEFAULT 0;
ALTER TABLE positions ADD COLUMN last_swap_utc TEXT;
ALTER TABLE positions ADD COLUMN planned_risk_money REAL;
ALTER TABLE trades ADD COLUMN account_id TEXT;
CREATE INDEX ix_positions_account ON positions(account_id, status);
CREATE INDEX ix_trades_account ON trades(account_id, closed_utc);

-- Instrument specifications with provenance. Missing required fields => NO TRADE.
CREATE TABLE instrument_specs (
    symbol          TEXT PRIMARY KEY,
    data            TEXT NOT NULL CHECK (json_valid(data)),
    field_sources   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(field_sources)),
    updated_utc     TEXT NOT NULL,
    updated_by      TEXT NOT NULL
);

CREATE TABLE latest_quotes (
    symbol          TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    quote_time_utc  TEXT NOT NULL,
    received_utc    TEXT NOT NULL,
    bid             REAL NOT NULL,
    ask             REAL NOT NULL,
    tradeable       INTEGER
);

CREATE TABLE bars (
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    open_time_utc   TEXT NOT NULL,
    provider        TEXT NOT NULL,
    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
    ask_c           REAL,
    spread_points   REAL,
    volume          REAL,
    PRIMARY KEY (symbol, timeframe, provider, open_time_utc)
);

CREATE TABLE provider_status (
    provider        TEXT PRIMARY KEY,
    last_ok_utc     TEXT,
    last_error_utc  TEXT,
    last_error      TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE daily_reports (
    day_utc         TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    created_utc     TEXT NOT NULL,
    metrics         TEXT NOT NULL CHECK (json_valid(metrics)),
    report          TEXT NOT NULL,
    PRIMARY KEY (day_utc, account_id)
);

ALTER TABLE weekly_reviews ADD COLUMN account_id TEXT;
