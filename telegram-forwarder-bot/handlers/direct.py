"""Direct-forward flow.

When the admin sends any message to the bot:
  * For a single message: store as pending, immediately show inline topic picker
  * For a media group (album): accumulate with a short debounce, then show
    picker once for the whole album

When the user taps a topic button:
  * For single: bot.copy_message -> destination topic
  * For album: bot.send_media_group with extracted InputMedia -> destination topic
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import (
    Update, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

ALBUM_DEBOUNCE_SECONDS = 3.0  # kept for backward compat (used by legacy code)

BATCH_WINDOW_SECONDS = 5.0   # NEW: time-based batch window. All messages
                              # received from a user within this window are
                              # combined into a SINGLE picker (no flooding).
                              # If the user keeps sending items, the window
                              # keeps resetting. The picker only fires after
                              # the user stops sending for BATCH_WINDOW_SECONDS.


# ---------- message classification helpers ----------

def _describe(msg) -> str:
    if msg.text:
        return f"text: {msg.text[:60]!r}"
    if msg.photo:
        return f"photo ({len(msg.photo)} sizes)"
    if msg.video:
        return f"video {msg.video.file_id[:8]}..."
    if msg.document:
        return f"document {msg.document.file_name or msg.document.file_id[:8]}"
    if msg.audio:
        return "audio"
    if msg.voice:
        return "voice"
    if msg.video_note:
        return "video_note"
    if msg.sticker:
        return "sticker"
    if msg.animation:
        return "animation"
    if msg.location:
        return "location"
    if msg.contact:
        return "contact"
    return "other"


def _to_input_media(msg) -> Any | None:
    """Convert a Telegram message to an InputMedia* object usable by
    send_media_group. Returns None for non-media messages."""
    caption = msg.caption
    if msg.photo:
        # Use largest size
        return InputMediaPhoto(media=msg.photo[-1].file_id, caption=caption)
    if msg.video:
        return InputMediaVideo(media=msg.video.file_id, caption=caption)
    if msg.document:
        # Skip if document is not a media file (e.g. a sticker)
        return InputMediaDocument(media=msg.document.file_id, caption=caption)
    if msg.audio:
        return InputMediaAudio(media=msg.audio.file_id, caption=caption)
    if msg.animation:
        return InputMediaVideo(media=msg.animation.file_id, caption=caption)
    return None


def _extract_media_info(msg) -> dict | None:
    """Extract media info from a message for use with InputMedia* and
    send_media_group. Returns None for non-media messages (e.g. text-only).

    The returned dict has keys:
      - type: 'photo' | 'video' | 'animation' | 'document' | 'audio'
      - file_id: str (Telegram file ID — usable by the bot to re-send)
      - caption: str | None (the original caption, if any)
    """
    caption = msg.caption
    if msg.photo:
        # photo[-1] is the largest size
        return {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": caption}
    if msg.video:
        return {"type": "video", "file_id": msg.video.file_id, "caption": caption}
    if msg.animation:
        return {"type": "animation", "file_id": msg.animation.file_id, "caption": caption}
    if msg.document:
        return {"type": "document", "file_id": msg.document.file_id, "caption": caption}
    if msg.audio:
        return {"type": "audio", "file_id": msg.audio.file_id, "caption": caption}
    return None


# ---------- main entry ----------

async def handle_direct_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any non-command message the admin sends to the bot.

    Uses a TIME-BASED BATCH WINDOW: when the user sends multiple messages
    within BATCH_WINDOW_SECONDS (default 5s), all of them are accumulated
    into a SINGLE pending forward and the picker is only shown ONCE after
    they stop sending. This prevents the "flooding" behavior of showing a
    separate picker for each individual message.

    Previously this only batched messages with the same media_group_id (i.e.
    true Telegram albums created via multi-select). Now it batches ANY
    sequence of messages from the same user in the same chat — so if you
    send 20 separate images, you get ONE picker for all 20, not 20 pickers.
    """
    if not update.effective_message or not update.effective_user:
        return
    cfg = context.bot_data["config"]
    user_id = update.effective_user.id

    if not cfg.is_admin(user_id):
        await update.effective_message.reply_text(
            "You are not authorized to use this bot."
        )
        return

    msg = update.effective_message
    chat_id = update.effective_chat.id
    logger.info("Direct message from %s: %s (media_group=%s)",
                user_id, _describe(msg), msg.media_group_id)

    # All messages (single, album, or rapid-sequence) go through the same
    # time-based batch path. The batch window is short enough that a single
    # message just waits ~5 sec before the picker shows up — which is fine
    # because the user gets a much smoother experience when sending many.
    await _handle_batched_message(update, context, chat_id, user_id, msg)


async def _handle_batched_message(update, context, chat_id, user_id, msg):
    """Time-based batch handler. Accumulates messages from the same chat
    within BATCH_WINDOW_SECONDS and shows ONE picker for everything once the
    user stops sending.

    Key behavior:
      - Every message (single, album item, or rapid-sequence) goes into the
        same per-chat batch.
      - When a new message arrives, the batch timer is RESET — so the picker
        only fires after the user stops sending for BATCH_WINDOW_SECONDS.
      - All accumulated messages are combined into ONE pending forward and
        ONE picker (no flooding).
      - Multi-item batches use the 'album' payload format which uses
        send_media_group / send_file album to send as a real grouped album.

    This replaces the old _handle_album_message which only batched messages
    with the same media_group_id (true Telegram albums via multi-select).
    """
    db = context.bot_data["db"]
    batches = context.chat_data.setdefault("msg_batches", {})
    key = chat_id  # per-chat, NOT per-media_group_id
    batch = batches.get(key, {
        "user_id": user_id,
        "chat_id": chat_id,
        "media_items": [],   # list of {type, file_id, caption}
        "text_items": [],    # list of {text, message_id}
        "message_ids": [],   # source message IDs (for copy_message fallback)
        "first_update": None,  # first update in batch (for sending picker)
    })

    # Always track the message id
    batch["message_ids"].append(msg.message_id)

    # Save the first update so we can send the picker to the right chat
    if batch["first_update"] is None:
        batch["first_update"] = update

    # Extract media info from this item
    media_info = _extract_media_info(msg)
    if media_info:
        batch["media_items"].append(media_info)
    elif msg.text:
        # Text-only message — add to text_items
        batch["text_items"].append({"text": msg.text, "message_id": msg.message_id})

    batches[key] = batch

    # Cancel previously scheduled picker (debounce) — extends the window
    prev_task = batch.get("task")
    if prev_task and not prev_task.done():
        prev_task.cancel()

    async def _later():
        try:
            # Wait BATCH_WINDOW_SECONDS. If another message arrives during
            # this wait, prev_task.cancel() will fire above and this
            # coroutine will be cancelled — so a new _later() starts and
            # extends the window. The picker only fires when the user has
            # stopped sending for BATCH_WINDOW_SECONDS.
            await asyncio.sleep(BATCH_WINDOW_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            media_items = batch["media_items"]
            text_items = batch["text_items"]
            n_media = len(media_items)
            n_text = len(text_items)

            # Always use 'album' payload format (even for 1 item) — this
            # goes through _forward_album which uses send_media_group for
            # proper album grouping. For 1 item, send_media_group sends
            # it as a single message.
            if n_media >= 1 or n_text >= 1:
                kind_payload = {
                    "kind": "album",
                    "media_items": media_items,
                    "text_items": text_items,
                    "message_ids": batch["message_ids"],
                }
            else:
                logger.warning("Empty batch — skipping")
                batches.pop(key, None)
                return

            # Build a label that reflects what was actually captured
            if n_media and not n_text:
                type_counts: dict[str, int] = {}
                for m in media_items:
                    type_counts[m["type"]] = type_counts.get(m["type"], 0) + 1
                summary = ", ".join(f"{c} {t}" for t, c in type_counts.items())
                if n_media == 1:
                    label = f"Got 1 {list(type_counts.keys())[0]} — pick a destination:"
                else:
                    label = f"Got {n_media} items ({summary}) — pick a destination:"
            elif n_media and n_text:
                label = (f"Got {n_media} media + {n_text} text item(s) "
                         f"— pick a destination:")
            else:
                preview = text_items[0]["text"][:50] if text_items else ""
                label = f"Got text: \"{preview}...\" — pick a destination:"

            pending_id = await db.create_pending(
                user_id=user_id,
                source_chat_id=chat_id,
                source_message_id=batch["message_ids"][0] if batch["message_ids"] else 0,
                payload=kind_payload,
                kind="direct",
            )
            # Send a NEW message to the user with the picker
            first_update = batch["first_update"]
            await _send_picker_new(context, first_update.effective_chat.id, pending_id, label)
            batches.pop(key, None)
        except Exception:
            logger.exception("batch picker task failed")
            batches.pop(key, None)

    batch["task"] = asyncio.create_task(_later())


# ---------- picker UI ----------

async def _check_destination_forum(context, group_id: int) -> bool | None:
    """Check if the destination chat is a forum (has topics enabled).

    Returns:
      True: chat is a forum (use topic picker)
      False: chat is a regular group/channel (use single Forward button)
      None: couldn't determine (network error, bot not a member, etc.)

    Uses bot.get_chat() which is the Bot API method. We cache the result
    in context.bot_data['destination_is_forum'] so we don't call get_chat
    on every message — only on first call or after /setgroup changes the
    destination.
    """
    # Check cache
    cache = context.bot_data.get("destination_is_forum")
    cached_group_id = context.bot_data.get("destination_is_forum_group_id")
    if cache is not None and cached_group_id == group_id:
        return cache

    # Cache miss — fetch
    try:
        chat = await context.bot.get_chat(chat_id=group_id)
        is_forum = bool(getattr(chat, "is_forum", False))
        context.bot_data["destination_is_forum"] = is_forum
        context.bot_data["destination_is_forum_group_id"] = group_id
        # Also cache the chat title for nicer UI
        title = getattr(chat, "title", None) or f"Chat {group_id}"
        context.bot_data["destination_chat_title"] = title
        logger.info("Destination chat %s is_forum=%s title=%r",
                    group_id, is_forum, title)
        return is_forum
    except Exception as e:
        logger.warning("get_chat failed for %s: %s — assuming forum=True (legacy behavior)", group_id, e)
        # If we can't determine, assume forum (legacy behavior) so the bot
        # doesn't break for users who had forums set up before this change.
        context.bot_data["destination_is_forum"] = True
        context.bot_data["destination_is_forum_group_id"] = group_id
        context.bot_data["destination_chat_title"] = f"Chat {group_id}"
        return True


async def _show_picker(update: Update, context, pending_id: str, label: str) -> None:
    """Show the topic picker (forum) or a single Forward button (non-forum).

    For non-forum destinations, the picker is just one button labeled
    "Forward to <chat_title>" with callback data fwd:<pending_id>:0
    (topic_id=0 means "no topic")."""
    topics_mgr = context.bot_data["topics"]
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    group_id = cfg.destination_group_id
    if group_id is None:
        group_id_raw = await db.get_runtime("destination_group_id")
        if not group_id_raw:
            await update.effective_message.reply_text(
                "No destination group set. Use /setgroup <group_id> first."
            )
            return
        group_id = int(group_id_raw)

    # Check if destination is a forum
    is_forum = await _check_destination_forum(context, group_id)
    chat_title = context.bot_data.get("destination_chat_title") or f"Chat {group_id}"

    if is_forum:
        # Forum — show topic picker
        topics = await topics_mgr.get_topics(group_id)
        if not topics:
            await update.effective_message.reply_text(
                "No topics found. Try /refresh (requires Telethon user session) "
                "or add manually with /addtopic <title> <topic_id>."
            )
            return
        keyboard = topics_mgr.build_keyboard(pending_id, topics)
        await update.effective_message.reply_text(label, reply_markup=keyboard)
    else:
        # Non-forum — show single "Forward" button
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                text=f"➡️ Forward to {chat_title}",
                callback_data=f"fwd:{pending_id}:0",
            )],
            [InlineKeyboardButton(text="Cancel", callback_data=f"cancel:{pending_id}")],
        ])
        await update.effective_message.reply_text(label, reply_markup=keyboard)


async def _send_picker_new(context, chat_id: int, pending_id: str, label: str) -> None:
    """Same as _show_picker but sends a NEW message (used for albums)."""
    topics_mgr = context.bot_data["topics"]
    cfg = context.bot_data["config"]
    db = context.bot_data["db"]
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        group_id = int(v) if v else None
    if group_id is None:
        await context.bot.send_message(chat_id=chat_id,
                                       text="No destination group set. Use /setgroup <group_id>.")
        return

    # Check if destination is a forum
    is_forum = await _check_destination_forum(context, group_id)
    chat_title = context.bot_data.get("destination_chat_title") or f"Chat {group_id}"

    if is_forum:
        topics = await topics_mgr.get_topics(group_id)
        if not topics:
            await context.bot.send_message(chat_id=chat_id,
                                           text="No topics found. /refresh or /addtopic first.")
            return
        keyboard = topics_mgr.build_keyboard(pending_id, topics)
        await context.bot.send_message(chat_id=chat_id, text=label, reply_markup=keyboard)
    else:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                text=f"➡️ Forward to {chat_title}",
                callback_data=f"fwd:{pending_id}:0",
            )],
            [InlineKeyboardButton(text="Cancel", callback_data=f"cancel:{pending_id}")],
        ])
        await context.bot.send_message(chat_id=chat_id, text=label, reply_markup=keyboard)


# ---------- callback handler (topic button tapped) ----------

async def topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Invoked when user taps a topic button under a pending forward."""
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = (q.data or "").split(":")
    # Expected formats:
    #   fwd:<pending_id>:<topic_id>
    #   cancel:<pending_id>
    if not data:
        return
    action = data[0]

    if action == "cancel":
        pending_id = data[1] if len(data) > 1 else ""
        if pending_id:
            await context.bot_data["db"].delete_pending(pending_id)
        await q.edit_message_text("Cancelled.")
        return

    if action != "fwd":
        return
    if len(data) < 3:
        return
    pending_id = data[1]
    # topic_id: integer for forum topics, or 0 for non-forum destinations
    # (0 means "no specific topic — send to the chat without a thread")
    topic_id_raw = int(data[2])
    topic_id = topic_id_raw if topic_id_raw > 0 else None

    db = context.bot_data["db"]
    cfg = context.bot_data["config"]
    pending = await db.get_pending(pending_id)
    if not pending:
        await q.edit_message_text("This forward expired. Send the content again.")
        return

    user_id = pending["user_id"]
    if not cfg.is_admin(user_id):
        await q.edit_message_text("Not authorized.")
        await db.delete_pending(pending_id)
        return

    # Resolve destination group id
    group_id = cfg.destination_group_id
    if group_id is None:
        v = await db.get_runtime("destination_group_id")
        if not v:
            await q.edit_message_text("No destination group set. /setgroup <group_id>.")
            return
        group_id = int(v)

    payload = pending["payload"]
    kind = pending["kind"]
    src_chat_id = pending["source_chat_id"]
    src_msg_id = pending["source_message_id"]

    # Show a "Forwarding..." message immediately so the user knows the tap
    # was registered. For albums, include the count so they have a sense of
    # how long it'll take.
    is_album = (kind == "direct" and payload.get("kind") == "album")
    is_link_direct = (kind == "link" and payload.get("direct_send") is True)
    target_label = f"topic {topic_id}" if topic_id else "chat"
    if is_album:
        n_media = len(payload.get("media_items") or [])
        n_text = len(payload.get("text_items") or [])
        if n_media:
            await q.edit_message_text(
                f"Forwarding {n_media} media item(s) to {target_label}..."
            )
        else:
            await q.edit_message_text(
                f"Forwarding {n_text} text item(s) to {target_label}..."
            )
    elif is_link_direct:
        n_msgs = len(payload.get("source_message_ids") or [])
        if n_msgs > 1:
            await q.edit_message_text(
                f"📡 Forwarding {n_msgs} message(s) to {target_label} via Telethon..."
            )
        else:
            media_type = (payload.get("media_types") or ["message"])[0]
            await q.edit_message_text(
                f"📡 Forwarding 1 {media_type} to {target_label} via Telethon..."
            )
    else:
        await q.edit_message_text(f"Forwarding to {target_label}...")

    try:
        if kind == "direct":
            if is_album:
                await _forward_album(context, payload, group_id, topic_id)
            else:
                await _forward_single(context, src_chat_id, src_msg_id,
                                       group_id, topic_id)
        elif kind == "link":
            # Build a progress callback that updates the inline keyboard
            # message (the "Forwarding..." one the user tapped). We throttle
            # updates to ~2/sec to avoid Telegram API rate limits, but ALWAYS
            # show the first update so the user sees immediate feedback.
            import time as _time
            last_update = {"time": 0.0, "text": "", "first": True}

            async def progress_cb(sent_bytes: int, total_bytes: int, label: str):
                now = _time.time()
                # Build the text first
                if total_bytes > 0:
                    pct = (sent_bytes / total_bytes) * 100
                    sent_mb = sent_bytes / (1024 * 1024)
                    total_mb = total_bytes / (1024 * 1024)
                    text = (f"📡 {label}\n\n"
                            f"Progress: {pct:.1f}%\n"
                            f"{sent_mb:.2f} / {total_mb:.2f} MB")
                else:
                    text = f"📡 {label}..."

                # Skip if the text is the same as the last update (no progress)
                if text == last_update["text"]:
                    return

                # Throttle: only update at most every 0.5 sec, EXCEPT for the
                # very first update which we always send so the user sees
                # immediate feedback when they tap the button.
                if not last_update["first"]:
                    if now - last_update["time"] < 0.5:
                        return
                last_update["first"] = False
                last_update["time"] = now
                last_update["text"] = text
                try:
                    await q.edit_message_text(text)
                except Exception:
                    pass  # don't crash on edit failures (rate limit, etc.)

            await _forward_link(context, payload, group_id, topic_id,
                                progress_callback=progress_cb)
    except Exception as e:
        logger.exception("forward failed")
        await q.edit_message_text(f"Forward failed: {e}")
        return

    await db.delete_pending(pending_id)
    if is_album:
        n_media = len(payload.get("media_items") or [])
        n_text = len(payload.get("text_items") or [])
        parts = []
        if n_media:
            parts.append(f"{n_media} media item(s)")
        if n_text:
            parts.append(f"{n_text} text item(s)")
        summary = " + ".join(parts) if parts else "items"
        if topic_id:
            await q.edit_message_text(f"Done — {summary} sent to topic {topic_id}.")
        else:
            await q.edit_message_text(f"Done — {summary} sent to chat.")
    else:
        if topic_id:
            await q.edit_message_text(f"Done — sent to topic {topic_id}.")
        else:
            await q.edit_message_text(f"Done — sent to chat.")


# ---------- actual forward primitives ----------

def _thread_kwargs(topic_id: int | None) -> dict:
    """Return kwargs for the Bot API call with message_thread_id only if
    topic_id is set. For non-forum destinations, topic_id is None and we
    don't pass message_thread_id at all (Telegram would reject it)."""
    if topic_id:
        return {"message_thread_id": topic_id}
    return {}


async def _forward_single(context, src_chat_id, src_msg_id, dest_group_id, topic_id) -> None:
    """Copy a single message to the destination topic (or chat if non-forum)
    using bot.copy_message."""
    await context.bot.copy_message(
        chat_id=dest_group_id,
        from_chat_id=src_chat_id,
        message_id=src_msg_id,
        **_thread_kwargs(topic_id),
    )


async def _forward_album(context, payload, dest_group_id, topic_id) -> None:
    """Forward an album to the destination topic using send_media_group.

    This is the Bot API's actual "send as album" method — items appear in the
    destination as a single grouped album with proper navigation arrows.

    Telegram limits send_media_group to 10 items per call. For albums larger
    than 10, we split into multiple send_media_group calls. Each call appears
    as a separate (but still grouped) album in the destination.

    Photos, videos, and animations can be mixed in an album (Telegram
    supports this). Documents and audio can NOT be in an album — they must
    be sent separately via send_document / send_audio.

    Text-only items (rare in a "media group" — Telegram usually splits them
    out) are sent as separate messages.
    """
    media_items: list[dict] = payload.get("media_items") or []
    text_items: list[dict] = payload.get("text_items") or []

    if not media_items and not text_items:
        # Backward compat with old payload format that only had message_ids
        # (no longer used, but we keep it so any pre-existing pending forwards
        # don't crash — they'll just be skipped here)
        logger.warning("_forward_album: payload has no media_items or text_items")
        return

    from telegram import (
        InputMediaPhoto, InputMediaVideo, InputMediaAnimation,
        InputMediaDocument, InputMediaAudio,
    )

    # Split media into album-eligible (photo/video/animation) and others
    # (document/audio — Telegram doesn't allow them in send_media_group)
    album_items = [m for m in media_items if m["type"] in ("photo", "video", "animation")]
    other_items = [m for m in media_items if m["type"] not in ("photo", "video", "animation")]

    sent_count = 0
    failed = []

    # Send album items in chunks of 10 (Telegram max per send_media_group)
    if album_items:
        # Apply the custom_caption policy:
        # - custom_caption=None: use original caption (first item's)
        # - custom_caption="" (strip): no caption
        # - custom_caption="<text>": use this text
        from handlers.admin import _get_custom_caption
        custom_caption = await _get_custom_caption(context)
        if custom_caption is None:
            first_caption = album_items[0].get("caption") if album_items else None
        elif custom_caption == "":
            first_caption = None  # strip mode
        else:
            first_caption = custom_caption

        for chunk_start in range(0, len(album_items), 10):
            chunk = album_items[chunk_start:chunk_start + 10]
            input_media = []
            for i, item in enumerate(chunk):
                # Only the first item of the FIRST chunk gets the caption
                cap = first_caption if (chunk_start == 0 and i == 0) else None
                if item["type"] == "photo":
                    input_media.append(InputMediaPhoto(media=item["file_id"], caption=cap))
                elif item["type"] == "video":
                    input_media.append(InputMediaVideo(
                        media=item["file_id"], caption=cap,
                        supports_streaming=True,
                    ))
                elif item["type"] == "animation":
                    input_media.append(InputMediaAnimation(
                        media=item["file_id"], caption=cap,
                    ))

            try:
                await context.bot.send_media_group(
                    chat_id=dest_group_id,
                    media=input_media,
                    **_thread_kwargs(topic_id),
                )
                sent_count += len(chunk)
                logger.info("Album chunk sent: %d items to %s",
                            len(chunk), f"topic {topic_id}" if topic_id else "chat")
            except Exception as e:
                logger.warning("send_media_group failed for chunk %d-%d: %s",
                              chunk_start, chunk_start + len(chunk), e)
                failed.append((chunk, e))

    # Send other items (documents, audio) individually
    # NOTE: custom_caption was already loaded above in the album_items branch.
    # If album_items was empty, we need to load it now. We use a simple
    # sentinel: if custom_caption is not in locals(), load it.
    try:
        _ = custom_caption  # check if defined
    except NameError:
        from handlers.admin import _get_custom_caption
        custom_caption = await _get_custom_caption(context)

    for item in other_items:
        try:
            # Determine caption: use custom_caption policy
            if custom_caption is None:
                cap = item.get("caption")
            elif custom_caption == "":
                cap = None  # strip
            else:
                cap = custom_caption
            if item["type"] == "document":
                await context.bot.send_document(
                    chat_id=dest_group_id,
                    document=item["file_id"],
                    caption=cap,
                    **_thread_kwargs(topic_id),
                )
            elif item["type"] == "audio":
                await context.bot.send_audio(
                    chat_id=dest_group_id,
                    audio=item["file_id"],
                    caption=cap,
                    **_thread_kwargs(topic_id),
                )
            sent_count += 1
        except Exception as e:
            logger.warning("send_%s failed: %s", item["type"], e)
            failed.append(([item], e))

    # Send text items as separate messages
    # If custom_caption is "" (strip) or a string, skip sending text items
    # entirely — they were text captions from the source, not user content.
    if custom_caption is None:
        for item in text_items:
            try:
                await context.bot.send_message(
                    chat_id=dest_group_id,
                    text=item["text"],
                    **_thread_kwargs(topic_id),
                )
                sent_count += 1
            except Exception as e:
                logger.warning("send_message (text item) failed: %s", e)
                failed.append(([item], e))
    # If custom_caption is a non-empty string, send it ONCE as a separate text
    # message (only if there are text items — otherwise the caption is on the
    # media already).
    elif custom_caption and text_items:
        try:
            await context.bot.send_message(
                chat_id=dest_group_id,
                text=custom_caption,
                **_thread_kwargs(topic_id),
            )
            sent_count += 1
        except Exception as e:
            logger.warning("send_message (custom caption text) failed: %s", e)
    # If custom_caption == "" (strip), don't send text items at all

    total = len(media_items) + len(text_items)
    target = f"topic {topic_id}" if topic_id else "chat"
    logger.info("Album forward: %d/%d items sent to %s",
                sent_count, total, target)

    if failed and sent_count == 0:
        # All failed — re-raise the first error so the user sees it
        raise failed[0][1]


async def _forward_link(context, payload, dest_group_id, topic_id,
                        progress_callback=None) -> None:
    """Forward a previously-fetched locked-channel message to the destination
    topic (or chat). Handles two payload formats:

    1. NEW direct_send format (preferred, fast):
       payload has direct_send=True, source_chat_id, source_message_ids.
       Uses Telethon's user account to send directly from source to destination
       via forward_messages (or send_message with file=... for protected content).
       No disk download needed — re-uploads from Telegram's servers.

    2. OLD format (legacy fallback):
       payload has media_paths (list of downloaded file paths), caption, text.
       Uses Bot API to re-upload from disk.

    The legacy format is kept for backward compatibility with any pending
    forwards that were created before this update.

    Args:
      progress_callback: optional async callable(sent_bytes, total_bytes, label)
        called during downloads and uploads. Used to show progress to the user
        by editing the bot's "Forwarding..." message.
    """
    # ----- NEW: direct send via Telethon user session -----
    if payload.get("direct_send"):
        user_session = context.bot_data.get("user_session")
        if not user_session:
            raise RuntimeError("Direct send requires the Telethon user session, "
                              "but it's not configured. Run /status and check.")
        # Auto-reconnect if the Telethon connection has dropped.
        if not await user_session._ensure_connected():
            raise RuntimeError("Telethon user session is disconnected and could "
                              "not be reconnected. Try: docker-compose restart forwarder-bot")
        source_chat_id = payload["source_chat_id"]
        source_message_ids = payload["source_message_ids"]
        # Load the custom_caption setting (None = use original, "" = strip,
        # "<text>" = use this text)
        from handlers.admin import _get_custom_caption
        custom_caption = await _get_custom_caption(context)
        success, diag = await user_session.send_to_destination(
            source_chat_id=source_chat_id,
            source_message_ids=source_message_ids,
            dest_chat_id=dest_group_id,
            topic_id=topic_id,
            progress_callback=progress_callback,
            custom_caption=custom_caption,
        )
        if not success:
            # Build a helpful error message — show the last few diagnostic lines
            err_lines = "\n".join(diag[-7:])  # last 7 lines (most relevant)
            raise RuntimeError(
                f"Direct send via Telethon failed.\n\n"
                f"Last diagnostic steps:\n{err_lines}\n\n"
                f"Likely causes:\n"
                f"  • Your user account is NOT a member of the destination chat\n"
                f"  • Your user account was kicked from the source chat\n"
                f"  • Telethon session expired — re-run `python login.py --string`\n"
                f"  • For protected content: all three fallback paths failed "
                f"(true forward, send_message(file=), download+send_file)"
            )
        # Log full diagnostic
        for line in diag:
            logger.info("  " + line)
        return

    # ----- LEGACY: download + re-upload via Bot API -----
    from telethon.tl import types as tl

    bot = context.bot
    media_paths: list[dict] = payload.get("media_paths", [])
    caption: str | None = payload.get("caption")
    text: str | None = payload.get("text")

    if media_paths:
        # Use send_media_group for albums, send_* for single
        if len(media_paths) == 1:
            m = media_paths[0]
            await _send_one(bot, dest_group_id, topic_id, m, caption or text)
        else:
            from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument
            input_media = []
            for i, m in enumerate(media_paths):
                cap = caption if i == 0 else None
                if m["type"] == "photo":
                    input_media.append(InputMediaPhoto(media=open(m["path"], "rb"), caption=cap))
                elif m["type"] == "video":
                    input_media.append(InputMediaVideo(media=open(m["path"], "rb"), caption=cap))
                elif m["type"] == "document":
                    input_media.append(InputMediaDocument(media=open(m["path"], "rb"), caption=cap))
            await bot.send_media_group(
                chat_id=dest_group_id,
                media=input_media,
                **_thread_kwargs(topic_id),
            )
    elif text or caption:
        # Fallback: text-only post or media download failed
        # Send the text/caption as a plain message to the destination topic (or chat)
        await bot.send_message(
            chat_id=dest_group_id, text=text or caption,
            **_thread_kwargs(topic_id),
        )
    else:
        raise RuntimeError("Empty link payload")


async def _send_one(bot, dest_group_id, topic_id, media, caption):
    """Send a single media item to the destination topic (or chat)."""
    path = media["path"]
    mtype = media["type"]
    tk = _thread_kwargs(topic_id)
    if mtype == "photo":
        await bot.send_photo(chat_id=dest_group_id, photo=open(path, "rb"),
                              caption=caption, **tk)
    elif mtype == "video":
        await bot.send_video(chat_id=dest_group_id, video=open(path, "rb"),
                              caption=caption, supports_streaming=True, **tk)
    elif mtype == "animation":
        await bot.send_animation(chat_id=dest_group_id, animation=open(path, "rb"),
                                  caption=caption, **tk)
    elif mtype == "audio":
        await bot.send_audio(chat_id=dest_group_id, audio=open(path, "rb"),
                              caption=caption, **tk)
    elif mtype == "voice":
        await bot.send_voice(chat_id=dest_group_id, voice=open(path, "rb"),
                              caption=caption, **tk)
    elif mtype == "document":
        await bot.send_document(chat_id=dest_group_id, document=open(path, "rb"),
                                 caption=caption, **tk)
    else:
        # fallback
        await bot.send_document(chat_id=dest_group_id, document=open(path, "rb"),
                                 caption=caption, **tk)
