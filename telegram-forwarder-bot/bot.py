"""Main entry point for the Telegram Forwarder Bot.

Wires together:
  * config.py     — env-based settings
  * db.py         — SQLite persistence
  * user_session.py — Telethon user session (for locked channels & topic
                     enumeration)
  * topics.py     — forum topic discovery + inline keyboard
  * handlers/    — direct / link / admin handlers

Run modes:
  * MODE=polling  — local dev (uses PTB's run_polling)
  * MODE=webhook  — production / Render / fps.ms (uses our custom aiohttp
                    server that ALSO serves GET / with 200 OK so platform
                    health checks pass — PTB's built-in tornado server
                    returns 404 for /, which fails Render's deploy)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)

from config import Config, CODE_VERSION
from db import Database
from user_session import UserSession
from topics import TopicManager
from handlers.admin import register_admin_handlers
from handlers.direct import handle_direct_message, topic_callback
from handlers.link import handle_link, LINK_FILTER

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
# Telegram lib is verbose; quiet it down
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
logger = logging.getLogger("forwarder")


class _CtxView:
    """Duck-typed context so admin-module helpers (which expect a PTB
    context with .bot_data) can read the raw bot_data dict from plain
    aiohttp handlers."""
    def __init__(self, data):
        self.bot_data = data


# ---------------------------------------------------------------------------
# Application lifecycle hooks (called by PTB in both polling and webhook mode)
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    """Called after the Application is fully initialised, before polling starts."""
    db: Database = app.bot_data["db"]
    await db.init()

    cfg: Config = app.bot_data["config"]
    user_session: UserSession | None = None

    # Debug: log what credentials we have (without revealing secrets)
    logger.info("Config check: API_ID=%s, API_HASH=%s, SESSION_STRING=%s",
                "SET" if cfg.api_id else "MISSING",
                "SET" if cfg.api_hash else "MISSING",
                f"SET ({len(cfg.session_string)} chars)" if cfg.session_string else "MISSING")

    if not cfg.api_id or not cfg.api_hash:
        logger.error("Telethon NOT configured: API_ID or API_HASH is missing in .env")
        app.bot_data["user_session"] = None
    elif not cfg.session_string:
        logger.error("Telethon NOT configured: SESSION_STRING is missing in .env")
        app.bot_data["user_session"] = None
    else:
        logger.info("Creating Telethon client (api_id=%s, session_string=%d chars)...",
                    cfg.api_id, len(cfg.session_string))
        user_session = UserSession(
            cfg.session_name, cfg.api_id, cfg.api_hash,
            session_string=cfg.session_string,
            flood_sleep_threshold=cfg.flood_sleep_threshold,
            flood_wait_cap=getattr(cfg, "flood_wait_cap", 600),
        )
        ok = await user_session.start()
        if ok:
            app.bot_data["user_session"] = user_session
            logger.info("✓ Telethon session connected successfully")
        else:
            app.bot_data["user_session"] = None
            logger.error(
                "✗ Telethon session FAILED to start. Possible causes:\n"
                "  1. SESSION_STRING is invalid or has been revoked\n"
                "  2. SESSION_STRING was generated with a different Telethon version\n"
                "  3. Network issue connecting to Telegram\n"
                "  4. API_ID/API_HASH mismatch\n"
                "Fix: Re-run `python login.py --string` locally and update SESSION_STRING in .env\n"
                "Then restart: docker-compose restart forwarder-bot"
            )

    app.bot_data["topics"] = TopicManager(db, app.bot_data["user_session"])

    me = await app.bot.get_me()
    logger.info("Bot started: @%s (%d) mode=%s", me.username, me.id, cfg.mode)
    if cfg.destination_group_id:
        logger.info("Destination group: %d", cfg.destination_group_id)
    else:
        logger.info("Destination group not set yet — use /setgroup <group_id>")
    if cfg.mode == "webhook":
        if not cfg.webhook_url:
            logger.error("MODE=webhook but WEBHOOK_URL is not set! The bot will "
                         "start but Telegram cannot reach it.")
        else:
            logger.info("Webhook URL: %s/%s", cfg.webhook_url.rstrip('/'),
                        cfg.webhook_url_path)


async def post_stop(app: Application) -> None:
    db: Database = app.bot_data.get("db")
    if db:
        await db.close()
    us: UserSession | None = app.bot_data.get("user_session")
    if us:
        await us.stop()


# ---------------------------------------------------------------------------
# Polling mode with stats server (VPS / local dev)
# ---------------------------------------------------------------------------

async def _run_polling_with_stats(app: Application, cfg: Config) -> None:
    """Run the bot in polling mode AND start a tiny aiohttp server on port 8081
    that serves /health and /stats for the unified dashboard.

    The stats server lets the dashboard query:
      - GET /health → 200 OK (container health check)
      - GET /stats  → JSON with bot status, scrape status, Telethon status, etc.
      - POST /stop_scrape → triggers the cancel event for the active scrape
      - POST /cancel_caption → clears the custom caption
    """
    from aiohttp import web
    import json

    # Initialize the app (same sequence as run_polling)
    await app.initialize()
    await post_init(app)
    await app.start()

    # Start the updater (polling)
    if app.updater:
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
    logger.info("Bot started in polling mode: @%s (%d)",
                app.bot.username, app.bot.id)

    # ----- aiohttp handlers -----

    async def health_handler(request: web.Request) -> web.Response:
        return web.Response(text="OK", status=200,
                            headers={"Content-Type": "text/plain",
                                     # live telemetry — never cache anywhere
                                     "Cache-Control": "no-store"})

    async def stats_handler(request: web.Request) -> web.Response:
        """Return JSON status of the forwarder bot for the dashboard."""
        import time as _time
        bot_data = app.bot_data
        config = bot_data.get("config")
        user_session = bot_data.get("user_session")
        db = bot_data.get("db")

        # Telethon status
        telethon_status = "not configured"
        if user_session:
            telethon_status = "connected" if user_session.available else "disconnected"

        # Scrape status — the job registry (multiple scrapes can run at
        # once: one /scrape + one /scrapeid is the classic combo). We
        # publish a `scrapes` array (one entry per job) plus the legacy
        # single-job `scrape` object (most recent active job, else the
        # last finished one) so old dashboards keep working.
        from handlers.admin import (_get_custom_caption, _scrape_jobs,
                                    _scrape_job_payload,
                                    _active_scrape_jobs, _job_is_active)

        _ctx = _CtxView(bot_data)
        jobs_registry = _scrape_jobs(_ctx)
        active_jobs = _active_scrape_jobs(_ctx)
        # All jobs: active first (oldest first), then last finished
        job_list = list(active_jobs)
        finished_jobs = [j for j in jobs_registry.values() if not _job_is_active(j)]
        if finished_jobs:
            last_finished = max(finished_jobs,
                                key=lambda j: j.get("finished_at", 0.0))
            job_list.append(last_finished)
        scrape_payloads = [_scrape_job_payload(j) for j in job_list]
        # Primary job for the legacy `scrape` field: newest active, else
        # the last finished one (drives the dashboard's "last results").
        primary = active_jobs[-1] if active_jobs else (
            job_list[0] if job_list else None)
        primary_payload = _scrape_job_payload(primary) if primary else {}
        scrape_running = bool(active_jobs)

        # Custom caption
        custom_caption = await _get_custom_caption(_ctx)

        # Cumulative stats (persisted to SQLite, survive restarts)
        cumulative_stats = {}
        if db:
            try:
                cumulative_stats = await db.get_all_stats()
            except Exception:
                cumulative_stats = {}

        stats = {
            "bot_name": app.bot.username,
            "bot_id": app.bot.id,
            "mode": cfg.mode,
            # Code build of the running bot (config.CODE_VERSION). The
            # dashboard compares this to its expected build — a mismatch
            # shows a "rebuild your containers" banner (the most common
            # "dashboard doesn't update" cause is a stale image).
            "build": CODE_VERSION,
            "telethon": telethon_status,
            "destination_group": cfg.destination_group_id,
            "destination_chat_title": bot_data.get("destination_chat_title"),
            "destination_is_forum": bot_data.get("destination_is_forum"),
            "custom_caption": custom_caption,
            "scrape_running": scrape_running,
            "scrape_jobs_running": len(active_jobs),
            # One entry per job (active first, then the last finished one).
            # Each entry carries job_id/kind/running plus the full counter
            # set, live wait phase + countdown, and the liveness field.
            "scrapes": scrape_payloads,
            # Legacy single-job view (newest active, else last finished) —
            # same schema as each `scrapes` entry, so old dashboards work.
            "scrape": primary_payload,
            # All-time cumulative stats (persisted across restarts)
            "cumulative": {
                "total_scrapes": cumulative_stats.get("total_scrapes", 0),
                "total_sent": cumulative_stats.get("total_sent", 0),
                "total_failed": cumulative_stats.get("total_failed", 0),
                "total_skipped": cumulative_stats.get("total_skipped", 0),
                "total_flood_waits": cumulative_stats.get("total_flood_waits", 0),
                "total_saved_forwards": cumulative_stats.get("total_saved_forwards", 0),
            },
            "timestamp": _time.time(),
        }
        return web.json_response(stats, headers={"Cache-Control": "no-store"})

    async def stop_scrape_handler(request: web.Request) -> web.Response:
        """POST /stop_scrape[?job=J1|all] — stop scrape job(s).

        Query params (optional):
          job=J1  — stop only that job (also accepts "1", "j1")
          job=all — stop every running job
        With no param: stops ALL running jobs (legacy dashboard behavior —
        there used to be only one scrape at a time).
        This endpoint only sends the graceful tier-1 cancel signal; the
        bot command /stop_scrape has the full tiered escalation.
        """
        from handlers.admin import _scrape_jobs, _job_is_active
        arg = request.query.get("job", "").strip()
        jobs = _scrape_jobs(_CtxView(app.bot_data))
        active = [j for j in jobs.values() if _job_is_active(j)]
        if not active:
            return web.json_response({"ok": False, "error": "No active scrape"},
                                     headers={"Cache-Control": "no-store"})
        a = arg.lower()
        if a in ("", "all", "*"):
            targets = active
        else:
            jid = a if a.startswith("j") else f"j{a}"
            job = jobs.get(jid.upper())
            if job is None or not _job_is_active(job):
                return web.json_response(
                    {"ok": False, "error": f"No active job matching '{arg}'"},
                    headers={"Cache-Control": "no-store"})
            targets = [job]
        for j in targets:
            j["cancel_event"].set()
        return web.json_response({
            "ok": True,
            "stopped": [j["job_id"] for j in targets],
            "message": f"Stop signal sent to {len(targets)} job(s)",
        }, headers={"Cache-Control": "no-store"})

    async def cancel_caption_handler(request: web.Request) -> web.Response:
        """POST /cancel_caption — clears the custom caption."""
        db = app.bot_data.get("db")
        if db:
            await db.set_runtime("custom_caption", "__none__")
        app.bot_data.pop("custom_caption", None)
        return web.json_response({"ok": True, "message": "Caption cleared"},
                                headers={"Cache-Control": "no-store"})

    async def reset_stats_handler(request: web.Request) -> web.Response:
        """POST /reset_stats — reset all cumulative stats to 0."""
        db = app.bot_data.get("db")
        if db:
            try:
                await db.reset_stats()
                return web.json_response({"ok": True, "message": "Stats reset to 0"},
                                        headers={"Cache-Control": "no-store"})
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)},
                                        headers={"Cache-Control": "no-store"})
        return web.json_response({"ok": False, "error": "Database not available"},
                                headers={"Cache-Control": "no-store"})

    # ----- build the web app -----
    web_app = web.Application()
    web_app.router.add_get("/", health_handler)
    web_app.router.add_get("/health", health_handler)
    web_app.router.add_get("/stats", stats_handler)
    web_app.router.add_post("/stop_scrape", stop_scrape_handler)
    web_app.router.add_post("/cancel_caption", cancel_caption_handler)
    web_app.router.add_post("/reset_stats", reset_stats_handler)

    runner = web.AppRunner(web_app, access_log=None)
    await runner.setup()

    # Use port 8081 for the forwarder bot's stats server (8080 is the dashboard)
    stats_port = 8081
    site = web.TCPSite(runner, "0.0.0.0", stats_port)
    await site.start()
    logger.info("Stats server listening on 0.0.0.0:%d", stats_port)

    # ----- keep running forever -----
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Shutting down polling + stats server...")
        await site.stop()
        await runner.cleanup()
        if app.updater:
            await app.updater.stop()
        try:
            await post_stop(app)
        except Exception:
            logger.exception("post_stop failed")
        await app.stop()
        await app.shutdown()


# ---------------------------------------------------------------------------
# Application builder
# ---------------------------------------------------------------------------

def build_application(cfg: Config, db: Database) -> Application:
    app = (
        Application.builder()
        .token(cfg.bot_token)
        .post_init(post_init)
        .post_stop(post_stop)
        # Enable concurrent update processing. Default is 1 (sequential) which
        # means while one link is being fetched via Telethon (10-30 sec), the
        # second link waits in the queue and only processes AFTER the first
        # completes — the user sees the second link "ignored".
        #
        # With concurrent_updates=True (256 max), each update runs in its
        # own asyncio task — so two links sent close together both fetch in
        # parallel and the user sees two pickers.
        .concurrent_updates(True)
        .build()
    )
    app.bot_data["config"] = cfg
    app.bot_data["db"] = db

    # Global error handler — any exception thrown by a handler that isn't
    # caught will land here. Without this, exceptions in handlers are
    # silently swallowed by PTB and the user sees no reply (the
    # "/setgroup does nothing" symptom).
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Log the full traceback (logger.exception uses context.error info
        # automatically when called inside a handler — but to be safe, we
        # also log it explicitly)
        error = context.error if hasattr(context, "error") else None
        if error:
            logger.error("🚨 Unhandled exception in handler for update %s: %s",
                         update, type(error).__name__, exc_info=error)
        else:
            logger.error("🚨 Unhandled exception in handler for update %s (no error info)", update)

        # Try to notify the user that something went wrong
        try:
            if isinstance(update, Update) and update.effective_message:
                err_str = str(error)[:500] if error else "unknown error"
                await update.effective_message.reply_text(
                    f"Internal error: {err_str}\n\n"
                    f"Check the bot logs for details."
                )
        except Exception:
            pass  # don't recurse

    app.add_error_handler(on_error)

    register_admin_handlers(app)

    # IMPORTANT: both MessageHandlers MUST be in the same group with mutually
    # exclusive filters. In PTB v21, handlers in DIFFERENT groups all run,
    # which caused this bug:
    #   - User sends a t.me/c/... link
    #   - handle_link (group 1) runs -> fetches media via Telethon, shows picker
    #   - handle_direct_message (group 2) ALSO runs -> treats the link as
    #     plain text, shows a SECOND picker that just copy_message's the link
    #   - User taps the wrong picker -> bot forwards the link as text, not media
    #
    # By putting both handlers in the same group (0, the default), PTB stops
    # after the first match — only one of them runs per update.
    app.add_handler(
        MessageHandler(LINK_FILTER & ~filters.COMMAND, handle_link),
        group=0,
    )
    app.add_handler(
        MessageHandler(~LINK_FILTER & ~filters.COMMAND, handle_direct_message),
        group=0,
    )
    # Topic picker callback
    app.add_handler(CallbackQueryHandler(topic_callback, pattern=r"^(fwd|cancel):"))
    return app


# ---------------------------------------------------------------------------
# Custom aiohttp webhook server (Render / fps.ms / any cloud)
# ---------------------------------------------------------------------------

async def _run_webhook_custom(app: Application, cfg: Config) -> None:
    """Run the bot in webhook mode using a custom aiohttp server.

    Routes:
      * GET  /                      -> 200 OK (for Render / fps.ms health check)
      * GET  /<bot-token-secret>    -> 200 OK (also passes health check if you
                                            point it at the secret path)
      * POST /<bot-token-secret>    -> forwards to PTB's process_update
      * *                           -> 404

    We use aiohttp instead of PTB's built-in tornado server because PTB's
    server returns 404 for /, which causes Render's deploy health check to
    fail with "Timed out waiting for successful response code".
    """
    from aiohttp import web

    if not cfg.webhook_url:
        raise RuntimeError(
            "MODE=webhook requires WEBHOOK_URL to be set. "
            "Set it to your service URL, e.g. "
            "https://your-service-name.onrender.com"
        )

    url_path = cfg.webhook_url_path
    full_webhook_url = f"{cfg.webhook_url.rstrip('/')}/{url_path}"

    # CRITICAL: We must call post_init MANUALLY because we're bypassing PTB's
    # run_webhook() / run_polling(). In PTB v21, post_init is invoked by
    # run_polling/run_webhook but NOT by app.initialize() / app.start()
    # directly. PTB docs explicitly state:
    #   "Does not call post_init - that is only done by run_polling and run_webhook."
    #
    # PTB's official run_webhook order is:
    #   1. initialize
    #   2. post_init
    #   3. updater.start_webhook (calls set_webhook)
    #   4. start
    #   5. run_forever
    #
    # We mirror this order exactly:
    #   1. app.initialize()
    #   2. post_init(app)            (DB tables, Telethon, TopicManager)
    #   3. app.bot.set_webhook(...)
    #   4. app.start()                (starts update fetcher)
    #   5. Start aiohttp server
    #   6. asyncio.Event().wait()     (run forever)
    #
    # Previously I had app.start() BEFORE set_webhook, which is the wrong
    # order. The exact ordering may not matter functionally, but matching
    # PTB's official order is safer and more correct.

    # Step 1: initialize the PTB app (handlers, bot, updater)
    logger.info("[step 1/5] Initializing PTB application...")
    await app.initialize()

    # Step 2: run post_init manually (DB tables, Telethon, TopicManager)
    logger.info("[step 2/5] Running post_init manually (DB init, Telethon, TopicManager)...")
    await post_init(app)

    # Step 3: register webhook URL with Telegram (BEFORE app.start, per PTB order)
    logger.info("[step 3/5] Registering webhook URL with Telegram: %s", full_webhook_url)
    await app.bot.set_webhook(
        url=full_webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,  # keep pending updates — they'll arrive
    )
    logger.info("✓ Webhook registered with Telegram")

    # Step 4: start the app (begins processing updates)
    logger.info("[step 4/5] Starting Application (update fetcher)...")
    await app.start()

    # Step 5: start the aiohttp server (accepts webhook HTTP requests)
    logger.info("[step 5/5] Starting aiohttp webhook server on port %d...", cfg.port)

    # ----- aiohttp handlers -----

    async def health_handler(request: web.Request) -> web.Response:
        """Return 200 OK — used by Render / fps.ms / UptimeRobot health checks."""
        return web.Response(text="OK", status=200, headers={"Content-Type": "text/plain"})

    async def webhook_handler(request: web.Request) -> web.Response:
        """Receive an update from Telegram, dispatch to PTB's process_update."""
        if request.path != f"/{url_path}":
            logger.warning("webhook_handler: 404 — request.path=%r, expected=%r",
                          request.path, f"/{url_path}")
            return web.Response(status=404)
        try:
            data = await request.json()
        except Exception:
            logger.warning("webhook_handler: invalid JSON body")
            return web.Response(status=400, text="Bad request")
        try:
            update = Update.de_json(data, app.bot)
            if update is None:
                logger.warning("webhook_handler: Update.de_json returned None")
                return web.Response(status=400, text="Could not parse update")
            # Log the incoming update for debugging
            update_id = data.get("update_id", "?")
            msg = data.get("message") or data.get("callback_query", {}).get("message")
            if msg and isinstance(msg, dict):
                text = msg.get("text") or msg.get("caption") or ""
                chat_id = msg.get("chat", {}).get("id")
                logger.info("📨 Incoming update_id=%s chat=%s text=%r",
                            update_id, chat_id, text[:80] if text else "(no text)")
            else:
                logger.info("📨 Incoming update_id=%s (non-message)", update_id)
            # Schedule the update in the background — return 200 immediately
            # so Telegram doesn't think the bot is slow.
            asyncio.create_task(app.process_update(update))
        except Exception:
            logger.exception("webhook_handler: failed to dispatch update")
            # Telegram expects 200 even on error, otherwise it retries forever
            return web.Response(status=200, text="OK (handler error)")
        return web.Response(status=200, text="OK")

    # ----- build the web app -----

    web_app = web.Application(client_max_size=10 * 1024 * 1024)  # 10 MB
    web_app.router.add_get("/", health_handler)
    # aiohttp automatically registers HEAD alongside GET, so we don't need
    # an explicit add_head call.
    web_app.router.add_get(f"/{url_path}", health_handler)  # safety: GET passes health check too
    web_app.router.add_post(f"/{url_path}", webhook_handler)

    runner = web.AppRunner(web_app, access_log=None)  # access_log=None to keep logs clean
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.port)
    await site.start()

    logger.info("Custom webhook server listening on 0.0.0.0:%d", cfg.port)
    logger.info("Health check endpoint: GET /  -> 200 OK")
    logger.info("Webhook endpoint:      POST /%s  -> forwards to PTB", url_path)

    # ----- keep the process alive forever -----

    try:
        await asyncio.Event().wait()  # never resolves unless Ctrl+C / signal
    finally:
        logger.info("Shutting down webhook server...")
        await site.stop()
        await runner.cleanup()
        # Unregister webhook with Telegram so messages don't queue forever
        try:
            await app.bot.delete_webhook()
        except Exception:
            pass
        # Call post_stop manually (mirror of post_init — would normally be
        # called by run_webhook/run_polling)
        try:
            await post_stop(app)
        except Exception:
            logger.exception("post_stop failed")
        await app.stop()
        await app.shutdown()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = Config.load()
    db = Database(cfg.db_path)

    app = build_application(cfg, db)
    logger.info("Starting bot in %s mode...", cfg.mode)

    if cfg.mode == "webhook":
        # Use our custom aiohttp server (Render / fps.ms / etc.)
        # We use asyncio.run because we need finer control over the lifecycle
        # than PTB's run_webhook provides.
        try:
            asyncio.run(_run_webhook_custom(app, cfg))
        except KeyboardInterrupt:
            logger.info("Interrupted")
        except Exception:
            logger.exception("Webhook mode crashed")
            sys.exit(1)
    else:
        # Polling mode (VPS / local dev) — use PTB's built-in run_polling
        # BUT also start a tiny aiohttp server for /stats and /health so the
        # unified dashboard can query this bot's status.
        try:
            asyncio.run(_run_polling_with_stats(app, cfg))
        except KeyboardInterrupt:
            logger.info("Interrupted")
        except Exception:
            logger.exception("Polling mode crashed")
            sys.exit(1)


if __name__ == "__main__":
    main()
