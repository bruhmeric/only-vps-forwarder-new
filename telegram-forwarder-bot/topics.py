"""Forum topic discovery + inline keyboard builder.

Uses Telethon (when available) to enumerate forum topics of the destination
group, falling back to user-provided overrides (added via /addtopic).
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from db import Database
from user_session import UserSession

logger = logging.getLogger(__name__)


class TopicManager:
    def __init__(self, db: Database, user_session: Optional[UserSession]) -> None:
        self.db = db
        self.user_session = user_session

    async def refresh(self, group_id: int) -> list[dict]:
        """Force-refresh the topics cache from Telethon. Returns the new list."""
        if not self.user_session or not self.user_session.available:
            logger.warning("Cannot refresh topics: Telethon user session not available")
            return await self.db.get_all_topics(group_id)

        topics = await self.user_session.list_forum_topics(group_id)
        if topics:
            await self.db.set_cached_topics(group_id, topics)
        return await self.db.get_all_topics(group_id)

    async def get_topics(self, group_id: int) -> list[dict]:
        """Get cached topics (refreshing if the cache is empty)."""
        cached = await self.db.get_all_topics(group_id)
        if cached:
            return cached
        # cache empty -> try to refresh via Telethon
        if self.user_session and self.user_session.available:
            return await self.refresh(group_id)
        return []

    def build_keyboard(self, pending_id: str, topics: list[dict],
                        columns: int = 2) -> InlineKeyboardMarkup:
        """Build an inline keyboard with one button per topic. Buttons carry
        callback data 'fwd:<pending_id>:<topic_id>'.
        """
        rows: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []
        for t in topics:
            title = t["title"]
            if len(title) > 32:
                title = title[:29] + "..."
            row.append(InlineKeyboardButton(
                text=title,
                callback_data=f"fwd:{pending_id}:{t['id']}",
            ))
            if len(row) == columns:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        # Cancel button
        rows.append([InlineKeyboardButton(text="Cancel",
                                          callback_data=f"cancel:{pending_id}")])
        return InlineKeyboardMarkup(rows)
