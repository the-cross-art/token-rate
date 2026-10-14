"""SQLite ledger: one immutable row per served request.

This is the source of truth you reconcile Stripe against. Grafana shows you
rates and trends; the ledger lets you answer "why was agent X charged $Y"
after the fact, which is the question metered billing always eventually
produces.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  REAL    NOT NULL,
    request_id          TEXT    NOT NULL,
    run_id              TEXT,
    agent               TEXT,
    customer            TEXT,
    backend             TEXT    NOT NULL,
    model_alias         TEXT    NOT NULL,
    upstream_model      TEXT,
    speculative         INTEGER NOT NULL,
    prompt_tokens       INTEGER NOT NULL,
    completion_tokens   INTEGER NOT NULL,
    reasoning_tokens    INTEGER,
    ttft_seconds        REAL,
    wall_seconds        REAL    NOT NULL,
    decode_tps          REAL,
    draft_n             INTEGER,
    draft_accepted      INTEGER,
    market_usd          REAL    NOT NULL,
    local_usd           REAL    NOT NULL,
    margin_usd          REAL    NOT NULL,
    stripe_event_id     TEXT,
    status              TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_run   ON requests(run_id);
CREATE INDEX IF NOT EXISTS idx_requests_agent ON requests(agent);
CREATE INDEX IF NOT EXISTS idx_requests_ts    ON requests(ts);
"""


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + an explicit lock: FastAPI runs handlers on a
        # threadpool, and writes here are short enough that one lock is cheaper
        # than a connection pool.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def record(self, **row) -> int:
        row.setdefault("ts", time.time())
        cols = ", ".join(row)
        placeholders = ", ".join(f":{k}" for k in row)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO requests ({cols}) VALUES ({placeholders})", row
            )
            self._conn.commit()
            return cur.lastrowid

    def attach_stripe_event(self, row_id: int, event_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE requests SET stripe_event_id = ? WHERE id = ?",
                (event_id, row_id),
            )
            self._conn.commit()

    def summary(self, run_id: str | None = None) -> list[dict]:
        q = """
        SELECT agent, model_alias, backend,
               COUNT(*)                AS requests,
               SUM(prompt_tokens)      AS prompt_tokens,
               SUM(completion_tokens)  AS completion_tokens,
               SUM(COALESCE(reasoning_tokens, 0)) AS reasoning_tokens,
               ROUND(AVG(decode_tps), 2)   AS avg_tps,
               ROUND(AVG(ttft_seconds), 3) AS avg_ttft,
               ROUND(SUM(market_usd), 6)   AS market_usd,
               ROUND(SUM(local_usd), 6)    AS local_usd
        FROM requests
        """
        params: tuple = ()
        if run_id:
            q += " WHERE run_id = ?"
            params = (run_id,)
        q += " GROUP BY agent, model_alias, backend ORDER BY market_usd DESC"
        with self._lock:
            cur = self._conn.execute(q, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
