"""SQLite-backed persistence layer.

Stores:
  * pending forwards — content awaiting topic selection
  * cached forum topics — refreshed via /refresh or lazily
  * runtime config — destination_group_id override (set via /setgroup)
  * manual topic overrides — added via /addtopic <name> <id>
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import aiosqlite


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_forwards (
                id            TEXT PRIMARY KEY,
                user_id       INTEGER NOT NULL,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                payload       TEXT NOT NULL,
                kind          TEXT NOT NULL,   -- 'direct' | 'link'
                created_at    INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS topics_cache (
                group_id      INTEGER NOT NULL,
                topic_id      INTEGER NOT NULL,
                title         TEXT NOT NULL,
                PRIMARY KEY (group_id, topic_id)
            );

            CREATE TABLE IF NOT EXISTS topic_overrides (
                group_id      INTEGER NOT NULL,
                topic_id      INTEGER NOT NULL,
                title         TEXT NOT NULL,
                PRIMARY KEY (group_id, topic_id)
            );

            CREATE TABLE IF NOT EXISTS runtime_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_forwards(user_id);
            CREATE INDEX IF NOT EXISTS idx_pending_created ON pending_forwards(created_at);
            """
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # -------- pending forwards --------

    async def create_pending(
        self,
        user_id: int,
        source_chat_id: int,
        source_message_id: int,
        payload: dict[str, Any],
        kind: str,
    ) -> str:
        pid = uuid.uuid4().hex[:12]
        await self._conn.execute(
            "INSERT INTO pending_forwards VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, user_id, source_chat_id, source_message_id,
             json.dumps(payload, default=str), kind, int(time.time())),
        )
        await self._conn.commit()
        return pid

    async def get_pending(self, pending_id: str) -> Optional[dict[str, Any]]:
        async with self._conn.execute(
            "SELECT * FROM pending_forwards WHERE id = ?", (pending_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d["payload"] = json.loads(d["payload"])
            return d

    async def delete_pending(self, pending_id: str) -> None:
        await self._conn.execute("DELETE FROM pending_forwards WHERE id = ?", (pending_id,))
        await self._conn.commit()

    async def purge_old_pending(self, max_age_seconds: int = 3600) -> int:
        cutoff = int(time.time()) - max_age_seconds
        async with self._conn.execute(
            "DELETE FROM pending_forwards WHERE created_at < ?", (cutoff,)
        ) as cur:
            await self._conn.commit()
            return cur.rowcount or 0

    # -------- topics cache --------

    async def set_cached_topics(self, group_id: int, topics: list[dict[str, Any]]) -> None:
        await self._conn.execute("DELETE FROM topics_cache WHERE group_id = ?", (group_id,))
        await self._conn.executemany(
            "INSERT INTO topics_cache VALUES (?, ?, ?)",
            [(group_id, t["id"], t["title"]) for t in topics],
        )
        await self._conn.commit()

    async def get_cached_topics(self, group_id: int) -> list[dict[str, Any]]:
        async with self._conn.execute(
            "SELECT topic_id, title FROM topics_cache WHERE group_id = ? ORDER BY topic_id",
            (group_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"id": r["topic_id"], "title": r["title"]} for r in rows]

    async def add_topic_override(self, group_id: int, topic_id: int, title: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO topic_overrides VALUES (?, ?, ?)",
            (group_id, topic_id, title),
        )
        await self._conn.commit()

    async def get_topic_overrides(self, group_id: int) -> list[dict[str, Any]]:
        async with self._conn.execute(
            "SELECT topic_id, title FROM topic_overrides WHERE group_id = ? ORDER BY topic_id",
            (group_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"id": r["topic_id"], "title": r["title"]} for r in rows]

    async def get_all_topics(self, group_id: int) -> list[dict[str, Any]]:
        """Merge cache + overrides (overrides win on conflict)."""
        cached = await self.get_cached_topics(group_id)
        overrides = await self.get_topic_overrides(group_id)
        merged = {t["id"]: t["title"] for t in cached}
        for ov in overrides:
            merged[ov["id"]] = ov["title"]
        return [{"id": tid, "title": title} for tid, title in sorted(merged.items())]

    # -------- runtime config --------

    async def set_runtime(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO runtime_config VALUES (?, ?)", (key, value)
        )
        await self._conn.commit()

    async def get_runtime(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with self._conn.execute(
            "SELECT value FROM runtime_config WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row["value"] if row else default

    # -------- cumulative stats (persisted across restarts) --------

    async def increment_stat(self, key: str, amount: int = 1) -> int:
        """Atomically increment a cumulative stat by `amount`.
        Returns the new value. Used for: total_scrapes, total_sent,
        total_failed, total_skipped, total_flood_waits, total_bytes_uploaded,
        total_saved_forwards.

        ATOMIC: a single SQL UPSERT — concurrent callers cannot lose
        increments via a read-modify-write race. This matters now that
        stats_callback (fired by parallel _send_one tasks during a
        /scrape, or by /scrapeid batch ticks) pushes per-delta increments
        live into the cumulative counters; without atomicity two
        concurrent +1 calls would both read 10 and both write 11,
        losing one increment. SQLite's INSERT ... ON CONFLICT DO UPDATE
        is a single statement, so the read and write happen atomically
        inside SQLite (>= 3.24, 2018)."""
        # No-op fast path (also avoids storing "0" on a non-existent row).
        if not amount:
            return await self.get_stat(key)
        await self._conn.execute(
            "INSERT INTO runtime_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = CAST(CAST(value AS INTEGER) + ? AS TEXT)",
            (key, str(amount), amount)
        )
        await self._conn.commit()
        return await self.get_stat(key)

    async def get_stat(self, key: str, default: int = 0) -> int:
        """Get a cumulative stat as int."""
        val = await self.get_runtime(key, str(default))
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    async def get_all_stats(self) -> dict:
        """Get all cumulative stats as a dict. Used by /stats endpoint
        and the dashboard."""
        return {
            "total_scrapes": await self.get_stat("total_scrapes"),
            "total_sent": await self.get_stat("total_sent"),
            "total_failed": await self.get_stat("total_failed"),
            "total_skipped": await self.get_stat("total_skipped"),
            "total_flood_waits": await self.get_stat("total_flood_waits"),
            "total_bytes_uploaded": await self.get_stat("total_bytes_uploaded"),
            "total_saved_forwards": await self.get_stat("total_saved_forwards"),
        }

    async def reset_stats(self) -> None:
        """Reset all cumulative stats to 0."""
        for key in ("total_scrapes", "total_sent", "total_failed",
                     "total_skipped", "total_flood_waits",
                     "total_bytes_uploaded", "total_saved_forwards"):
            await self.set_runtime(key, "0")
