"""Link-forward flow.

When the admin sends a t.me/c/<id>/<msg_id> or t.me/<channel>/<msg_id> URL:
  * Parse it
  * Use the Telethon user session to fetch the message(s)
  * Download media to temp files (since locked channels often disable
    forwarding — we have to re-upload rather than forward)
  * Create a pending forward with the downloaded file paths + caption
  * Show the inline topic picker
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters

from user_session import parse_telegram_link, UserSession
from db import Database

logger = logging.getLogger(__name__)

LINK_FILTER = filters.Regex(r"(https?://)?t(?:elegram)?\.me/")

# No file size limit — Telethon handles files up to 2GB (Telegram's hard limit)
# via InputFileBig. The old 50MB limit was for the Bot API's sendDocument
# cap, but we use Telethon's send_file which supports up to 2GB.
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB (Telegram's hard limit)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect a t.me link in the user's message and pull the content via
    the Telethon user session."""
    if not update.effective_message or not update.effective_user:
        return
    cfg = context.bot_data["config"]
    user_id = update.effective_user.id
    if not cfg.is_admin(user_id):
        return

    text = update.effective_message.text or update.effective_message.caption or ""
    user_session: UserSession = context.bot_data.get("user_session")
    if not user_session:
        await update.effective_message.reply_text(
            "❌ Telethon session failed to start at boot.\n\n"
            "Send /reconnect to retry.\n"
            "If that fails, check logs: docker-compose logs forwarder-bot\n"
            "Common fix: re-run `python login.py --string` locally and update SESSION_STRING in .env"
        )
        return
    # Auto-reconnect if the Telethon connection has dropped (Telegram idle
    # disconnect, network blip, etc.).
    if not await user_session._ensure_connected():
        await update.effective_message.reply_text(
            "❌ Telethon user session is disconnected and could not be reconnected.\n"
            "Try restarting the container: docker-compose restart forwarder-bot"
        )
        return

    parsed = parse_telegram_link(text)
    if not parsed:
        await update.effective_message.reply_text(
            "Couldn't parse that link. Supported formats:\n"
            "• https://t.me/c/1234567890/42  (private channel post)\n"
            "• https://t.me/channelname/42   (public channel post)\n"
            "\nNote: invite-only links like t.me/+abc... can't be auto-fetched "
            "because we can't join chats for you."
        )
        return

    status = await update.effective_message.reply_text(
        f"🔗 Fetching message from `{parsed.chat_ref}` / msg `{parsed.message_id}`...",
        parse_mode="Markdown",
    )
    try:
        fetched, diag = await user_session.fetch_message(parsed)
    except Exception as e:
        logger.exception("fetch_message failed")
        await status.edit_text(
            f"❌ Failed to fetch: `{type(e).__name__}: {e}`\n\n"
            f"This is unexpected — the fetch_message method should have caught this. "
            f"Check the bot logs.",
            parse_mode="Markdown",
        )
        return

    if not fetched:
        # Show the full step-by-step diagnostic log to the user so they can
        # pinpoint exactly where the fetch failed. This is the key change —
        # previously the user just got "Couldn't fetch that message" with
        # no detail.
        diag_text = "\n".join(diag)
        # Telegram messages have a 4096-char limit. Truncate if needed.
        if len(diag_text) > 3800:
            diag_text = diag_text[:3800] + "\n... (truncated)"
        await status.edit_text(
            f"❌ Couldn't fetch that message.\n\n"
            f"📋 Step-by-step diagnostic:\n```\n{diag_text}\n```",
            parse_mode="Markdown",
        )
        return

    msg = fetched["message"]
    album = fetched.get("album") or []

    # NEW HYBRID APPROACH (per user's research on GetRestrictedMessages etc.):
    # Instead of downloading media to disk now and re-uploading via Bot API
    # when the user taps a topic, we just store the source chat_id and
    # message IDs. When the user taps a topic, we use Telethon's
    # `send_to_destination` which:
    #   1. Tries `forward_messages` (true Telegram forward — fastest)
    #   2. Falls back to `send_message(file=msg.media)` which re-uploads
    #      from Telegram's servers WITHOUT downloading to disk first.
    #
    # This is dramatically faster (no download step) and works for protected
    # content (the noforwards restriction is at Telegram's forward level, not
    # at MTProto).
    #
    # The trade-off: the message appears as sent by the user account (since
    # it's the user account sending), not by the bot. For most users this is
    # fine — they're sending to their own group with their own account.

    source_chat_id = fetched["chat_id"]
    source_message_ids = [m.id for m in (album if album else [msg])]

    # Determine media type for display (without downloading)
    from telethon.tl import types as tl
    has_media = bool(getattr(msg, "media", None))
    media_types: list[str] = []
    if album:
        for m in album:
            if isinstance(getattr(m, "media", None), tl.MessageMediaPhoto):
                media_types.append("photo")
            elif isinstance(getattr(m, "media", None), tl.MessageMediaDocument):
                doc = m.media.document
                if doc and doc.mime_type and doc.mime_type.startswith("video/"):
                    media_types.append("video")
                else:
                    media_types.append("document")
            else:
                media_types.append("text")
    else:
        if isinstance(getattr(msg, "media", None), tl.MessageMediaPhoto):
            media_types.append("photo")
        elif isinstance(getattr(msg, "media", None), tl.MessageMediaDocument):
            doc = msg.media.document
            if doc and doc.mime_type and doc.mime_type.startswith("video/"):
                media_types.append("video")
            else:
                media_types.append("document")
        elif msg.message:
            media_types.append("text")
        else:
            media_types.append("unknown")

    caption_preview = (msg.message or "")[:50]

    payload = {
        "direct_send": True,  # NEW — signals to use the fast path
        "source_chat_id": source_chat_id,
        "source_message_ids": source_message_ids,
        "has_media": has_media,
        "media_types": media_types,
        "caption_preview": caption_preview,
    }

    db: Database = context.bot_data["db"]
    pending_id = await db.create_pending(
        user_id=user_id,
        source_chat_id=source_chat_id,
        source_message_id=parsed.message_id,
        payload=payload,
        kind="link",
    )

    # Show topic picker (or single Forward button for non-forum)
    topics_mgr = context.bot_data["topics"]
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await status.edit_text("No destination group set. Use /setgroup <group_id>.")
        return

    # Build the label showing what was fetched
    n = len(source_message_ids)
    if n == 1:
        label = f"Fetched 1 {media_types[0]}"
        if caption_preview:
            label += f" (\"{caption_preview}...\")"
        label += " — pick a destination:"
    elif n > 1:
        type_counts: dict[str, int] = {}
        for t in media_types:
            type_counts[t] = type_counts.get(t, 0) + 1
        summary = ", ".join(f"{c} {t}" for t, c in type_counts.items())
        label = f"Fetched {n} items ({summary}) — pick a destination:"
    else:
        label = "Fetched message — pick a destination:"

    # Use the same picker logic as direct forward — handles both forum and non-forum
    # Note: For private chats, the chat_id equals the user's id.
    from handlers.direct import _send_picker_new
    # The user sent the link in a private chat with the bot, so chat_id == user_id
    # _send_picker_new sends a NEW message (doesn't reply to the original link)
    await _send_picker_new(context, update.effective_chat.id, pending_id, label)


async def _download_media(user_session: UserSession, msg, tmp_dir: str, idx: int) -> tuple[Optional[dict], Optional[str]]:
    """Download the media attached to a Telethon Message to disk.

    Returns a tuple (result, error):
      - result: dict {'path': str, 'type': str} on success, None on failure
      - error: str | None — human-readable error message if download failed,
                or None if there was simply no media (not an error)

    The error string is what's new — previously the function returned None
    for both "no media" and "download failed", which made it impossible to
    surface the actual cause to the user. Now the caller can distinguish
    and show the user exactly why the download failed.
    """
    from telethon.tl import types as tl

    if not msg or not msg.media:
        return None, None  # no media — not an error
    # Skip web pages / contacts / geos (no downloadable file)
    if isinstance(msg.media, (tl.MessageMediaWebPage, tl.MessageMediaContact,
                               tl.MessageMediaGeo, tl.MessageMediaVenue,
                               tl.MessageMediaGame, tl.MessageMediaPoll,
                               tl.MessageMediaUnsupported)):
        return None, f"unsupported media type: {type(msg.media).__name__}"

    # Determine type
    media_type = "document"
    if isinstance(msg.media, tl.MessageMediaPhoto):
        media_type = "photo"
    elif isinstance(msg.media, tl.MessageMediaDocument):
        doc = msg.media.document
        if doc and doc.mime_type:
            mt = doc.mime_type
            if mt.startswith("video/"):
                # check for video vs animation (gif)
                for attr in doc.attributes:
                    if isinstance(attr, tl.DocumentAttributeAnimated):
                        media_type = "animation"
                        break
                    if isinstance(attr, tl.DocumentAttributeVideo):
                        # round video -> video_note; for now treat as video
                        if getattr(attr, "round", False):
                            media_type = "video_note"  # send as video for simplicity
                        else:
                            media_type = "video"
                        break
            elif mt.startswith("audio/"):
                if "ogg" in mt:
                    media_type = "voice"
                else:
                    media_type = "audio"
    elif isinstance(msg.media, tl.MessageMediaWebPage):
        return None, "media is a web page (no file to download)"

    # Get file size BEFORE downloading if possible — avoids downloading
    # a huge file only to reject it for being too large
    try:
        if isinstance(msg.media, tl.MessageMediaDocument) and msg.media.document:
            file_size = msg.media.document.size or 0
            if file_size > MAX_DOWNLOAD_BYTES:
                mb = file_size / (1024 * 1024)
                return None, f"file too large ({mb:.1f} MB > 2048 MB Telegram limit)"
        elif isinstance(msg.media, tl.MessageMediaPhoto) and msg.media.photo:
            # Photo sizes are in msg.media.photo.sizes; the largest is the
            # last one (or one with type='x')
            sizes = msg.media.photo.sizes
            if sizes:
                largest = sizes[-1]
                file_size = getattr(largest, "size", 0) or 0
                if file_size > MAX_DOWNLOAD_BYTES:
                    mb = file_size / (1024 * 1024)
                    return None, f"photo too large ({mb:.1f} MB > 2048 MB Telegram limit)"
    except Exception:
        pass  # best-effort size check; proceed with download

    # Download
    out_path = os.path.join(tmp_dir, f"media_{idx}_{int(time.time())}")
    try:
        result = await user_session.client.download_media(msg, file=out_path)
    except Exception as e:
        logger.warning("download_media failed: %s", e)
        return None, f"download failed: {type(e).__name__}: {e}"
    if not result:
        return None, "download returned no result"
    if isinstance(result, bytes):
        out_path = out_path  # bytes path, save to disk
        with open(out_path, "wb") as f:
            f.write(result)
    else:
        out_path = str(result)

    # Check size
    try:
        sz = os.path.getsize(out_path)
    except OSError as e:
        return None, f"could not get file size: {e}"
    if sz > MAX_DOWNLOAD_BYTES:
        logger.warning("Skipping media (%d bytes) — exceeds %d limit",
                       sz, MAX_DOWNLOAD_BYTES)
        try:
            os.remove(out_path)
        except OSError:
            pass
        mb = sz / (1024 * 1024)
        return None, f"downloaded file too large ({mb:.1f} MB > 2048 MB Telegram limit)"

    return {"path": out_path, "type": media_type}, None


async def _cleanup_tmp_later(tmp_dir: str, delay_seconds: int) -> None:
    """Wait `delay_seconds`, then delete the temp directory and all files in it.

    Replacement for the old JobQueue-based cleanup. We use asyncio.sleep
    because the PTB JobQueue is None in our custom webhook mode (PTB only
    initializes it for run_polling / run_webhook, and only if the
    'job-queue' extra is installed).

    This is a fire-and-forget task scheduled via asyncio.create_task — the
    caller doesn't await it. If the process exits before the delay elapses,
    the cleanup doesn't run, but that's OK because we use /tmp which the
    OS cleans up, and Render's filesystem is ephemeral anyway.
    """
    try:
        await asyncio.sleep(delay_seconds)
    except asyncio.CancelledError:
        return
    import shutil
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info("Cleaned up %s", tmp_dir)
    except Exception:
        logger.exception("cleanup failed for %s", tmp_dir)
