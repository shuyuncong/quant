"""SQLite signal deduplication, outbox delivery and candidate-pool storage."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable
import uuid

from models import SignalEvent
from utils.time_utils import now_shanghai


class SignalStore:
    def __init__(self, path: str = "./data/signal_monitor.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS signal_event (
                    event_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    side TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox_delivery (
                    event_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT,
                    delivered_at TEXT,
                    claimed_at TEXT,
                    claim_token TEXT,
                    PRIMARY KEY (event_id, channel),
                    FOREIGN KEY (event_id) REFERENCES signal_event(event_id)
                );

                CREATE TABLE IF NOT EXISTS candidate (
                    symbol TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    expires_on TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(outbox_delivery)").fetchall()
            }
            if "claimed_at" not in columns:
                connection.execute("ALTER TABLE outbox_delivery ADD COLUMN claimed_at TEXT")
            if "claim_token" not in columns:
                connection.execute("ALTER TABLE outbox_delivery ADD COLUMN claim_token TEXT")

    def enqueue_event(self, event: SignalEvent, channels: Iterable[str]) -> bool:
        payload = json.dumps(event.to_payload(), ensure_ascii=False)
        now = now_shanghai().isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO signal_event
                (event_id, symbol, timeframe, signal_type, side, confirmed_at, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.symbol,
                    event.timeframe,
                    event.signal_type,
                    event.side,
                    event.confirmed_at,
                    payload,
                    now,
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                for channel in channels:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO outbox_delivery
                        (event_id, channel, status, attempts, next_attempt_at)
                        VALUES (?, ?, 'pending', 0, ?)
                        """,
                        (event.event_id, channel, now),
                    )
        return inserted

    def pending_deliveries(self, limit: int = 100, max_attempts: int = 5) -> list[dict[str, Any]]:
        now = now_shanghai().isoformat(timespec="seconds")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.event_id, d.channel, d.attempts, e.payload
                FROM outbox_delivery d
                JOIN signal_event e ON e.event_id = d.event_id
                WHERE d.status = 'pending'
                  AND d.attempts < ?
                  AND d.next_attempt_at <= ?
                ORDER BY e.created_at, d.channel
                LIMIT ?
                """,
                (max_attempts, now, limit),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "channel": row["channel"],
                "attempts": row["attempts"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def claim_deliveries(self, limit: int = 100, max_attempts: int = 5) -> list[dict[str, Any]]:
        now = now_shanghai()
        stale = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        claimed: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE outbox_delivery
                SET status='pending', claimed_at=NULL, claim_token=NULL
                WHERE status='sending' AND claimed_at < ?
                """,
                (stale,),
            )
            rows = connection.execute(
                """
                SELECT d.event_id, d.channel, d.attempts, e.payload
                FROM outbox_delivery d
                JOIN signal_event e ON e.event_id = d.event_id
                WHERE d.status='pending' AND d.attempts < ? AND d.next_attempt_at <= ?
                ORDER BY e.created_at, d.channel
                LIMIT ?
                """,
                (max_attempts, now.isoformat(timespec="seconds"), limit),
            ).fetchall()
            for row in rows:
                claim_token = uuid.uuid4().hex
                cursor = connection.execute(
                    """
                    UPDATE outbox_delivery
                    SET status='sending', claimed_at=?, claim_token=?
                    WHERE event_id=? AND channel=? AND status='pending'
                    """,
                    (
                        now.isoformat(timespec="seconds"),
                        claim_token,
                        row["event_id"],
                        row["channel"],
                    ),
                )
                if cursor.rowcount == 1:
                    claimed.append(
                        {
                            "event_id": row["event_id"],
                            "channel": row["channel"],
                            "attempts": row["attempts"],
                            "claim_token": claim_token,
                            "payload": json.loads(row["payload"]),
                        }
                    )
        return claimed

    def mark_delivered(self, event_id: str, channel: str, claim_token: str) -> bool:
        now = now_shanghai().isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_delivery
                SET status='delivered', delivered_at=?, last_error=NULL,
                    claimed_at=NULL, claim_token=NULL
                WHERE event_id=? AND channel=? AND status='sending' AND claim_token=?
                """,
                (now, event_id, channel, claim_token),
            )
        return cursor.rowcount == 1

    def mark_failed(
        self,
        event_id: str,
        channel: str,
        attempts: int,
        error: str,
        claim_token: str,
    ) -> bool:
        next_attempt = now_shanghai() + timedelta(seconds=min(3600, 2 ** min(attempts + 1, 10)))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox_delivery
                SET status='pending', attempts=?, next_attempt_at=?, last_error=?,
                    claimed_at=NULL, claim_token=NULL
                WHERE event_id=? AND channel=? AND status='sending' AND claim_token=?
                """,
                (
                    attempts + 1,
                    next_attempt.isoformat(timespec="seconds"),
                    error[:1000],
                    event_id,
                    channel,
                    claim_token,
                ),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _business_expiry(start: date, business_days: int) -> date:
        current = start
        remaining = max(0, business_days)
        while remaining:
            current += timedelta(days=1)
            if current.weekday() < 5:
                remaining -= 1
        return current

    def replace_candidates(
        self,
        candidates: list[dict[str, Any]],
        ttl_business_days: int = 5,
        capacity: int = 100,
    ) -> None:
        now = now_shanghai()
        expires_on = self._business_expiry(now.date(), ttl_business_days).isoformat()
        ranked = sorted(candidates, key=lambda item: item.get("score", 0), reverse=True)[:capacity]
        with self._connect() as connection:
            connection.execute("DELETE FROM candidate")
            for candidate in ranked:
                connection.execute(
                    """
                    INSERT INTO candidate(symbol, name, score, expires_on, payload, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate["symbol"],
                        candidate.get("name", ""),
                        int(candidate.get("score", 0)),
                        expires_on,
                        json.dumps(candidate, ensure_ascii=False),
                        now.isoformat(timespec="seconds"),
                    ),
                )

    def upsert_candidates(
        self,
        candidates: list[dict[str, Any]],
        ttl_business_days: int = 5,
        capacity: int = 100,
    ) -> None:
        now = now_shanghai()
        expires_on = self._business_expiry(now.date(), ttl_business_days).isoformat()
        with self._connect() as connection:
            for candidate in candidates:
                connection.execute(
                    """
                    INSERT INTO candidate(symbol, name, score, expires_on, payload, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        name=excluded.name,
                        score=excluded.score,
                        expires_on=excluded.expires_on,
                        payload=excluded.payload,
                        updated_at=excluded.updated_at
                    """,
                    (
                        candidate["symbol"],
                        candidate.get("name", ""),
                        int(candidate.get("score", 0)),
                        expires_on,
                        json.dumps(candidate, ensure_ascii=False),
                        now.isoformat(timespec="seconds"),
                    ),
                )
            connection.execute(
                """
                DELETE FROM candidate
                WHERE symbol NOT IN (
                    SELECT symbol FROM candidate ORDER BY score DESC, updated_at DESC LIMIT ?
                )
                """,
                (capacity,),
            )

    def active_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        today = now_shanghai().date().isoformat()
        with self._connect() as connection:
            connection.execute("DELETE FROM candidate WHERE expires_on < ?", (today,))
            rows = connection.execute(
                "SELECT payload FROM candidate ORDER BY score DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_value FROM run_state WHERE state_key=?", (key,)
            ).fetchone()
        return row["state_value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        now = now_shanghai().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO run_state(state_key, state_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_value=excluded.state_value,
                    updated_at=excluded.updated_at
                """,
                (key, value, now),
            )

    def event_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM signal_event").fetchone()
        return int(row["count"])
