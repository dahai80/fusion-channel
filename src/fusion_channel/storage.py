from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fc_inbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL,
    duplicates INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fc_inbound_dedup ON fc_inbound(channel_id, message_id);
CREATE TABLE IF NOT EXISTS fc_rejected (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fc_outbound (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT NOT NULL,
    sent_at TEXT,
    provider_message_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fc_outbound_dedup ON fc_outbound(dedup_key);
CREATE TABLE IF NOT EXISTS fc_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


class OutboxRecord:
    def __init__(self, row: dict):
        self.id: int = row["id"]
        self.channel_id: str = row["channel_id"]
        self.dedup_key: str = row["dedup_key"]
        payload = row["payload"]
        self.payload: dict = json.loads(payload) if isinstance(payload, str) else payload
        self.status: str = row["status"]
        self.attempts: int = row["attempts"]
        self.next_retry_at: str = row["next_retry_at"]
        self.sent_at: str | None = row.get("sent_at")
        self.provider_message_id: str | None = row.get("provider_message_id")
        self.last_error: str | None = row.get("last_error")
        self.created_at: str = row.get("created_at", "")


class ChannelStore:
    # Storage interface for archive-first inbound, rejected envelopes,
    # outbox and receipts. Swap for the consumer app's own DB by
    # subclassing — the library never assumes a specific engine.

    async def init(self) -> None: ...

    async def close(self) -> None: ...

    async def save_inbound(self, channel_id: str, message_id: str, payload: dict) -> bool:
        # Returns False when (channel_id, message_id) already exists (duplicate).
        raise NotImplementedError

    async def bump_duplicate(self, channel_id: str, message_id: str) -> int:
        raise NotImplementedError

    async def save_rejected(self, channel_id: str, reason: str, payload: dict) -> None:
        raise NotImplementedError

    async def save_outbound(self, channel_id: str, dedup_key: str, payload: dict, next_retry_at: str) -> int | None:
        # Returns None on dedup hit.
        raise NotImplementedError

    async def due_outbound(self, now: str, limit: int = 20) -> list[OutboxRecord]:
        raise NotImplementedError

    async def update_outbound(self, record: OutboxRecord) -> None:
        raise NotImplementedError

    async def claim_outbound(self, record_id: int, lease_until: str) -> bool:
        # CAS: pending -> sending only if still pending; False if another
        # worker claimed it first (double-send guard).
        raise NotImplementedError

    async def requeue_stale_sending(self, now: str) -> int:
        # Crash recovery: 'sending' records whose lease (next_retry_at) has
        # expired go back to 'pending'.
        raise NotImplementedError

    async def save_receipt(self, channel_id: str, message_id: str, status: str, payload: dict) -> None:
        raise NotImplementedError


class MemoryStore(ChannelStore):
    def __init__(self):
        self.inbound: dict[tuple[str, str], dict] = {}
        self.duplicates: dict[tuple[str, str], int] = {}
        self.rejected: list[dict] = []
        self.outbox: dict[int, dict] = {}
        self.receipts: list[dict] = []
        self._next_id = 1

    async def init(self) -> None: ...

    async def close(self) -> None: ...

    async def save_inbound(self, channel_id: str, message_id: str, payload: dict) -> bool:
        key = (channel_id, message_id)
        if key in self.inbound:
            return False
        self.inbound[key] = payload
        return True

    async def bump_duplicate(self, channel_id: str, message_id: str) -> int:
        key = (channel_id, message_id)
        self.duplicates[key] = self.duplicates.get(key, 0) + 1
        return self.duplicates[key]

    async def save_rejected(self, channel_id: str, reason: str, payload: dict) -> None:
        self.rejected.append(
            {"channel_id": channel_id, "reason": reason, "payload": payload, "received_at": _now_iso()}
        )

    async def save_outbound(self, channel_id: str, dedup_key: str, payload: dict, next_retry_at: str) -> int | None:
        for record in self.outbox.values():
            if record["dedup_key"] == dedup_key:
                return None
        record_id = self._next_id
        self._next_id += 1
        self.outbox[record_id] = {
            "id": record_id,
            "channel_id": channel_id,
            "dedup_key": dedup_key,
            "payload": payload,
            "status": "pending",
            "attempts": 0,
            "next_retry_at": next_retry_at,
            "sent_at": None,
            "provider_message_id": None,
            "last_error": None,
            "created_at": _now_iso(),
        }
        return record_id

    async def due_outbound(self, now: str, limit: int = 20) -> list[OutboxRecord]:
        due = [
            record
            for record in self.outbox.values()
            if record["status"] == "pending" and record["next_retry_at"] <= now
        ]
        due.sort(key=lambda r: r["next_retry_at"])
        return [OutboxRecord(record) for record in due[:limit]]

    async def update_outbound(self, record: OutboxRecord) -> None:
        row = self.outbox[record.id]
        row.update(
            status=record.status,
            attempts=record.attempts,
            next_retry_at=record.next_retry_at,
            sent_at=record.sent_at,
            provider_message_id=record.provider_message_id,
            last_error=record.last_error,
        )

    async def claim_outbound(self, record_id: int, lease_until: str) -> bool:
        row = self.outbox.get(record_id)
        if row is None or row["status"] != "pending":
            return False
        row["status"] = "sending"
        row["next_retry_at"] = lease_until
        return True

    async def requeue_stale_sending(self, now: str) -> int:
        count = 0
        for row in self.outbox.values():
            if row["status"] == "sending" and row["next_retry_at"] <= now:
                row["status"] = "pending"
                row["next_retry_at"] = now
                count += 1
        if count:
            logger.warning("requeued stale sending records count=%d", count)
        return count

    async def save_receipt(self, channel_id: str, message_id: str, status: str, payload: dict) -> None:
        self.receipts.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "status": status,
                "payload": payload,
                "received_at": _now_iso(),
            }
        )


class SqliteStore(ChannelStore):
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        logger.info("channel store ready path=%s", self.db_path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SqliteStore not initialized; call init() first")
        return self._db

    async def save_inbound(self, channel_id: str, message_id: str, payload: dict) -> bool:
        try:
            await self.db.execute(
                "INSERT INTO fc_inbound (channel_id, message_id, payload, received_at) VALUES (?, ?, ?, ?)",
                (channel_id, message_id, _dump(payload), _now_iso()),
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            logger.info("inbound duplicate channel=%s message_id=%s", channel_id, message_id[:24])
            return False

    async def bump_duplicate(self, channel_id: str, message_id: str) -> int:
        cursor = await self.db.execute(
            "UPDATE fc_inbound SET duplicates = duplicates + 1 WHERE channel_id = ? AND message_id = ? RETURNING duplicates",
            (channel_id, message_id),
        )
        row = await cursor.fetchone()
        await self.db.commit()
        return int(row[0]) if row else 0

    async def save_rejected(self, channel_id: str, reason: str, payload: dict) -> None:
        await self.db.execute(
            "INSERT INTO fc_rejected (channel_id, reason, payload, received_at) VALUES (?, ?, ?, ?)",
            (channel_id, reason, _dump(payload), _now_iso()),
        )
        await self.db.commit()

    async def save_outbound(self, channel_id: str, dedup_key: str, payload: dict, next_retry_at: str) -> int | None:
        try:
            cursor = await self.db.execute(
                "INSERT INTO fc_outbound (channel_id, dedup_key, payload, next_retry_at, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (channel_id, dedup_key, _dump(payload), next_retry_at, _now_iso()),
            )
            await self.db.commit()
            return int(cursor.lastrowid)
        except aiosqlite.IntegrityError:
            logger.info("outbox dedup hit key=%s channel=%s", dedup_key[:32], channel_id)
            return None

    async def due_outbound(self, now: str, limit: int = 20) -> list[OutboxRecord]:
        cursor = await self.db.execute(
            "SELECT * FROM fc_outbound WHERE status = 'pending' AND next_retry_at <= ? ORDER BY next_retry_at LIMIT ?",
            (now, limit),
        )
        rows = await cursor.fetchall()
        return [OutboxRecord(dict(row)) for row in rows]

    async def update_outbound(self, record: OutboxRecord) -> None:
        await self.db.execute(
            "UPDATE fc_outbound SET status = ?, attempts = ?, next_retry_at = ?, sent_at = ?,"
            " provider_message_id = ?, last_error = ? WHERE id = ?",
            (
                record.status,
                record.attempts,
                record.next_retry_at,
                record.sent_at,
                record.provider_message_id,
                record.last_error,
                record.id,
            ),
        )
        await self.db.commit()

    async def claim_outbound(self, record_id: int, lease_until: str) -> bool:
        cursor = await self.db.execute(
            "UPDATE fc_outbound SET status = 'sending', next_retry_at = ? WHERE id = ? AND status = 'pending'",
            (lease_until, record_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def requeue_stale_sending(self, now: str) -> int:
        cursor = await self.db.execute(
            "UPDATE fc_outbound SET status = 'pending', next_retry_at = ? WHERE status = 'sending' AND next_retry_at <= ?",
            (now, now),
        )
        await self.db.commit()
        if cursor.rowcount:
            logger.warning("requeued stale sending records count=%d", cursor.rowcount)
        return cursor.rowcount

    async def save_receipt(self, channel_id: str, message_id: str, status: str, payload: dict) -> None:
        await self.db.execute(
            "INSERT INTO fc_receipts (channel_id, message_id, status, payload, received_at) VALUES (?, ?, ?, ?, ?)",
            (channel_id, message_id, status, _dump(payload), _now_iso()),
        )
        await self.db.commit()
