"""Configuration loader — reads .env and exposes typed settings.

Supports two run modes:
  * MODE=polling  (default, for local dev) — bot uses long-polling
  * MODE=webhook  (recommended for Render) — bot serves a webhook on $PORT

For Render:
  * Render injects PORT automatically — the bot listens on 0.0.0.0:$PORT
  * The Telethon session must be a StringSession stored in SESSION_STRING,
    because Render's free tier filesystem is ephemeral (the .session file
    would be lost on every restart)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# Code version of the forwarder-bot package. Bumped on every behavior
# change. The /stats endpoint reports it as `build` and the dashboard
# compares it against its own expected build — if they don't match, the
# dashboard shows a "rebuild your containers" banner. This exists because
# the most common "dashboard doesn't update" report turned out to be a
# stale docker image (docker-compose up -d WITHOUT --build) still running
# the old code.
#
# v7: cumulative stats now update LIVE per-message/per-batch (admin.py
# stats_callback) instead of only at scrape completion, /scrapeid fills
# in skipped_count + cancelled + total_flood_waits cumulative, /scrapeid
# publishes in_flight during forward batches, db.increment_stat is now
# an atomic UPSERT. Together these fix the "dashboard All-Time cards
# appear frozen for hours during a long scrape" symptom.
#
# v8: /scrapeid gained a `clean` flag (scrape_channel_by_ids_clean in
# user_session.py) that fetches + resends each ID instead of using
# forward_messages, so it can strip all t.me / generic URLs from text
# and captions, strip ALL captions (media AND text-only messages —
# forward_messages' drop_media_captions only handles media), and drop
# the "Forwarded from" header (a fresh send has none). Slower than
# plain /scrapeid but produces clean output.
#
# v9: dashboard diagnostic overhaul — /api/diag endpoint, connection
# diagnostic banner with EXACT error (timeout / connection refused /
# DNS failure), "Checking..." watchdog that fires after 15s, fetch_json
# now logs every outcome and recreates the session on error. The bot
# itself is unchanged in v9 — only the dashboard changed. But the
# version must still be bumped so the dashboard knows the user has
# rebuilt the bot image (the dashboard's EXPECTED_BOT_BUILD compares
# against this).
CODE_VERSION = "v9"

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # dotenv is optional; environment may already be set
    pass


def _int_list(value: str) -> list[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


@dataclass
class Config:
    bot_token: str
    api_id: Optional[int]
    api_hash: Optional[str]
    phone: Optional[str]
    destination_group_id: Optional[int]
    admin_ids: list[int] = field(default_factory=list)
    session_name: str = "user_session"
    db_path: str = "forwarder.db"
    # --- deployment mode ---
    mode: str = "polling"  # "polling" | "webhook"
    webhook_url: Optional[str] = None
    port: int = 8080
    session_string: Optional[str] = None
    # --- scrape performance ---
    default_parallel: int = 5  # default parallel sends during /scrape (was 3)
    # --- flood resilience (Telethon-first strategy) ---
    # Telethon auto-sleeps + auto-retries any FloodWait/SlowModeWait whose
    # requested wait is <= this threshold (in seconds); longer waits raise
    # FloodWaitError to our code, which shows a live countdown in the
    # status message + dashboard and sleeps it off before retrying.
    #
    # Default 60 = best of both worlds: short waits (the common case) are
    # absorbed silently by Telethon, while LONG waits (the ones that make
    # the bot look frozen for 15-60 min) surface to the visible handler.
    #
    # WARNING: raising this to 86400 makes Telethon absorb EVERY wait
    # internally — the scrape will still never crash, but during long
    # flood waits NOTHING updates (no status, no dashboard, /stop_scrape
    # tier-1 is unresponsive) because our code never sees the error.
    # That "silent waiting" is exactly what made earlier builds look
    # "stuck at budget recovery".
    # NOTE: None/0 does NOT mean "wait forever" — Telethon's setter turns
    # None into 0, which RAISES every FloodWaitError instead.
    flood_sleep_threshold: int = 60
    # Max time (seconds) we will sleep on ANY single server-requested
    # FloodWait before retrying the request. Telegram often demands
    # 15-30+ minute waits on big scrapes; instead of silently sleeping the
    # whole thing, we sleep at most this long and then RETRY — the server
    # re-answers with the (now smaller) remaining wait, which we again cap.
    # Counters therefore resume moving (and the countdown keeps ticking)
    # at least every 10 minutes instead of freezing for half an hour.
    # 0 disables the cap (sleep the exact server-requested time).
    flood_wait_cap: int = 600
    # Human-like pacing: take an extended break after this many sent
    # messages (0 disables), lasting flood_break_seconds.
    flood_break_every: int = 500
    flood_break_seconds: int = 300
    # --- concurrent scrapes ---
    # How many scrape jobs (mix of /scrape and /scrapeid) may run at the
    # same time. Default 2 = one /scrape + one /scrapeid in parallel (the
    # classic combo: /scrapeid hammers the SEND bucket while /scrape reads
    # history). All jobs share ONE Telegram account, so the account-level
    # rate budget is shared too — more jobs = more frequent flood waits,
    # each shown as a live countdown per job.
    max_concurrent_scrapes: int = 2

    @classmethod
    def load(cls) -> "Config":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN missing — copy .env.example to .env and fill it in.")

        api_id_raw = os.environ.get("API_ID", "").strip()
        api_hash = os.environ.get("API_HASH", "").strip()
        phone = os.environ.get("PHONE", "").strip() or None

        dest_raw = os.environ.get("DESTINATION_GROUP_ID", "").strip()
        dest = int(dest_raw) if dest_raw else None

        admin_raw = os.environ.get("ADMIN_IDS", "").strip()
        admins = _int_list(admin_raw) if admin_raw else []

        mode_raw = os.environ.get("MODE", "polling").strip().lower()
        mode = mode_raw if mode_raw in ("polling", "webhook") else "polling"

        webhook_url = os.environ.get("WEBHOOK_URL", "").strip() or None

        port_raw = os.environ.get("PORT", "").strip()
        port = int(port_raw) if port_raw else 8080

        session_string = os.environ.get("SESSION_STRING", "").strip() or None

        # PARALLEL env var: default number of concurrent sends during /scrape.
        # Higher = faster but risks FloodWait. Default 5 (was 3). Max 10.
        parallel_raw = os.environ.get("PARALLEL", "").strip()
        default_parallel = max(1, min(int(parallel_raw), 10)) if parallel_raw else 5

        def _env_int(name: str, default: int, lo: int = 0, hi: int = 2**31 - 1) -> int:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return max(lo, min(int(raw), hi))
            except ValueError:
                return default

        # Flood resilience knobs (see dataclass docs above)
        flood_sleep_threshold = _env_int("FLOOD_SLEEP_THRESHOLD", 60, 0, 24 * 60 * 60)
        flood_wait_cap = _env_int("FLOOD_WAIT_MAX_SECONDS", 600, 0, 24 * 60 * 60)
        flood_break_every = _env_int("FLOOD_BREAK_EVERY", 500)
        flood_break_seconds = _env_int("FLOOD_BREAK_SECONDS", 300, 0, 24 * 60 * 60)

        # Concurrent scrape jobs (see dataclass docs above). 1 = old
        # one-at-a-time behavior; 4 = hard ceiling.
        max_concurrent_scrapes = _env_int("MAX_CONCURRENT_SCRAPES", 2, 1, 4)

        return cls(
            bot_token=token,
            api_id=int(api_id_raw) if api_id_raw else None,
            api_hash=api_hash or None,
            phone=phone,
            destination_group_id=dest,
            admin_ids=admins,
            session_name=os.environ.get("SESSION_NAME", "user_session") or "user_session",
            db_path=os.environ.get("DB_PATH", "forwarder.db") or "forwarder.db",
            mode=mode,
            webhook_url=webhook_url,
            port=port,
            session_string=session_string,
            default_parallel=default_parallel,
            flood_sleep_threshold=flood_sleep_threshold,
            flood_wait_cap=flood_wait_cap,
            flood_break_every=flood_break_every,
            flood_break_seconds=flood_break_seconds,
            max_concurrent_scrapes=max_concurrent_scrapes,
        )

    @property
    def has_user_session(self) -> bool:
        # Session works if we have api_id+api_hash AND at least one of
        # (file-based session name, session_string).
        return bool(self.api_id and self.api_hash)

    def is_admin(self, user_id: int) -> bool:
        if not self.admin_ids:
            # No whitelist configured -> allow anyone (single-user self-hosted bot)
            return True
        return user_id in self.admin_ids

    @property
    def webhook_url_path(self) -> str:
        """Path component of the webhook URL — uses the secret part of the
        bot token so the webhook endpoint is not easily guessable."""
        return self.bot_token.split(":")[-1]
