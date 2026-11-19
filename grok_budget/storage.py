"""SQLite usage log + response cache."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DAY = 86400.0
WEEK = 7 * DAY

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    bot TEXT NOT NULL,
    team TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,          -- ok | error | empty | cache_hit | blocked
    prompt_hash TEXT,
    outcome TEXT,                  -- optional: useful | useless (set by bots / head trader)
    latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_bot_ts ON usage(bot, ts);
CREATE TABLE IF NOT EXISTS cache (
    prompt_hash TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS weekly_summary (
    week_start REAL NOT NULL,
    bot TEXT NOT NULL,
    calls INTEGER NOT NULL,
    tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    PRIMARY KEY (week_start, bot)
);
"""


@dataclass
class UsageRow:
    id: int
    ts: float
    bot: str
    team: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    status: str
    prompt_hash: str | None
    outcome: str | None
    latency_ms: int | None

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Storage:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "usage.db"
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- usage -------------------------------------------------------------
    def log(self, *, bot: str, team: str, model: str, prompt_tokens: int = 0,
            completion_tokens: int = 0, cost_usd: float = 0.0, status: str = "ok",
            prompt_hash: str | None = None, outcome: str | None = None,
            latency_ms: int | None = None, ts: float | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO usage (ts, bot, team, model, prompt_tokens, completion_tokens, cost_usd,"
            " status, prompt_hash, outcome, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts or time.time(), bot, team, model, prompt_tokens, completion_tokens, cost_usd,
             status, prompt_hash, outcome, latency_ms),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_outcome(self, call_id: int, outcome: str) -> None:
        self.conn.execute("UPDATE usage SET outcome=? WHERE id=?", (outcome, call_id))
        self.conn.commit()

    def rows(self, since: float, until: float | None = None) -> list[UsageRow]:
        until = until or time.time() + 1
        cur = self.conn.execute(
            "SELECT id, ts, bot, team, model, prompt_tokens, completion_tokens, cost_usd, status,"
            " prompt_hash, outcome, latency_ms FROM usage WHERE ts >= ? AND ts < ? ORDER BY ts",
            (since, until),
        )
        return [UsageRow(*r) for r in cur.fetchall()]

    def spent(self, since: float, bot: str | None = None) -> float:
        q = "SELECT COALESCE(SUM(cost_usd),0) FROM usage WHERE ts >= ?"
        args: tuple = (since,)
        if bot:
            q += " AND bot = ?"
            args += (bot,)
        return float(self.conn.execute(q, args).fetchone()[0])

    # --- cache -------------------------------------------------------------
    def cache_get(self, prompt_hash: str, max_age_s: float) -> str | None:
        row = self.conn.execute("SELECT ts, response FROM cache WHERE prompt_hash=?", (prompt_hash,)).fetchone()
        if row and time.time() - row[0] <= max_age_s:
            return row[1]
        return None

    def cache_put(self, prompt_hash: str, response: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO cache (prompt_hash, ts, response) VALUES (?,?,?)",
                          (prompt_hash, time.time(), response))
        self.conn.commit()

    # --- cleanup helpers ---------------------------------------------------
    def count_older(self, cutoff: float) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM usage WHERE ts < ?", (cutoff,)).fetchone()[0])

    def count_cache_older(self, cutoff: float) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM cache WHERE ts < ?", (cutoff,)).fetchone()[0])

    def archive_and_delete_older(self, cutoff: float) -> int:
        """Aggregate old rows into weekly_summary (so history totals survive), then delete them."""
        agg = self.conn.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ?, bot, COUNT(*),"
            " SUM(prompt_tokens + completion_tokens), SUM(cost_usd)"
            " FROM usage WHERE ts < ? GROUP BY 1, 2",
            (WEEK, WEEK, cutoff),
        ).fetchall()
        for week_start, bot, calls, tokens, cost in agg:
            self.conn.execute(
                "INSERT INTO weekly_summary (week_start, bot, calls, tokens, cost_usd) VALUES (?,?,?,?,?)"
                " ON CONFLICT(week_start, bot) DO UPDATE SET calls = calls + excluded.calls,"
                " tokens = tokens + excluded.tokens, cost_usd = cost_usd + excluded.cost_usd",
                (week_start, bot, calls, tokens, cost),
            )
        n = self.conn.execute("DELETE FROM usage WHERE ts < ?", (cutoff,)).rowcount
        self.conn.commit()
        return n

    def delete_cache_older(self, cutoff: float) -> int:
        n = self.conn.execute("DELETE FROM cache WHERE ts < ?", (cutoff,)).rowcount
        self.conn.commit()
        return n

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")
