"""Forwarder Bot Dashboard — single-bot monitoring & control.

Runs on port 8080. Proxies requests to:
  - Forwarder Bot: http://forwarder-bot:8081/stats
"""
import asyncio
import json
import logging
import os
import time
from aiohttp import web, ClientSession, ClientTimeout
from urllib.parse import quote

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("dashboard")

# Bot endpoint (Docker container name)
FORWARDER_STATS_URL = os.environ.get("FORWARDER_STATS_URL",
                                      "http://forwarder-bot:8081/stats")
FORWARDER_STOP_SCRAPE_URL = os.environ.get("FORWARDER_STOP_SCRAPE_URL",
                                           "http://forwarder-bot:8081/stop_scrape")
FORWARDER_CANCEL_CAPTION_URL = os.environ.get("FORWARDER_CANCEL_CAPTION_URL",
                                              "http://forwarder-bot:8081/cancel_caption")
FORWARDER_RESET_STATS_URL = os.environ.get("FORWARDER_RESET_STATS_URL",
                                           "http://forwarder-bot:8081/reset_stats")

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))

# Every response we serve is live telemetry: nothing may be cached by
# the browser, a proxy (Cloudflare) or the back/forward cache — a cached
# /api/stats reply is indistinguishable from a frozen dashboard.
NO_STORE = {"Cache-Control": "no-store, must-revalidate"}

# Build of the forwarder-bot code this dashboard expects (kept in sync
# with telegram_forwarder-bot/config.py CODE_VERSION — bump both
# together). The bot reports its own `build` in /stats; a mismatch means
# one container is running stale code (classic cause of "dashboard
# doesn't update": docker-compose up -d was run WITHOUT --build) and the
# dashboard shows a rebuild banner.
EXPECTED_BOT_BUILD = "v9"

# Shared outbound HTTP session (reused across polls instead of a new
# connection every 3s) and a generous timeout: a busy bot can take a
# few seconds to answer /stats while a scrape is running, and the old
# 3-second timeout flipped the whole page to "Bot Offline".
#
# DIAGNOSTIC: the last fetch attempt's outcome is kept in _last_diag so
# /api/diag and /api/stats can surface it to the browser — this is the
# single most useful addition for debugging "dashboard stuck at
# Checking..." because it tells the user EXACTLY why the dashboard
# can't reach the bot (connection refused, DNS failure, timeout,
# non-200, etc.) without having to open the browser devtools or docker
# logs.
_client_session: ClientSession | None = None
_last_diag: dict = {
    "bot_url": FORWARDER_STATS_URL,
    "last_attempt": 0.0,          # epoch seconds of the last fetch attempt
    "last_status": "never",        # "never" | "ok" | "timeout" | "error:<type>" | "http:NNN"
    "last_error": "",              # human-readable error string
    "last_latency_ms": 0,          # ms from fetch start to response body parsed
    "last_ok": 0.0,                # epoch seconds of the last successful fetch
    "consecutive_failures": 0,     # how many fetches in a row have failed
}


def _record_diag(status: str, error: str = "", latency_ms: int = 0,
                 ok: bool = False) -> None:
    """Update the last-fetch diagnostic record. Called by fetch_json
    on every attempt (success or failure) so /api/diag always reflects
    the latest state of the dashboard → bot connection."""
    now = time.time()
    _last_diag["last_attempt"] = now
    _last_diag["last_status"] = status
    _last_diag["last_error"] = error
    _last_diag["last_latency_ms"] = latency_ms
    if ok:
        _last_diag["last_ok"] = now
        _last_diag["consecutive_failures"] = 0
    else:
        _last_diag["consecutive_failures"] += 1
    # Also log it so `docker logs dashboard` shows the same info.
    if ok:
        logger.info("fetch_json OK: %s (%dms)", status, latency_ms)
    else:
        logger.warning("fetch_json FAIL: %s — %s (%dms, %d in a row)",
                       status, error, latency_ms,
                       _last_diag["consecutive_failures"])


async def fetch_json(url: str, timeout: float = 8.0) -> dict | None:
    """Fetch JSON from a URL with a short timeout. Returns None on error.

    DIAGNOSTIC: records the outcome of every attempt into _last_diag so
    /api/diag can tell the user EXACTLY why the dashboard can't reach
    the bot (connection refused, DNS failure, timeout, non-200, etc.).
    On error, the shared session is forced closed so the next attempt
    builds a fresh one with a clean connection pool — this prevents a
    stale pool (after a bot container restart) from hanging forever
    even though the timeout has fired.

    Uses aiohttp.ClientTimeout(total=...) instead of the deprecated
    `timeout=float` kwarg for correctness on all aiohttp versions.
    """
    global _client_session
    t0 = time.time()
    try:
        if _client_session is None or _client_session.closed:
            _client_session = ClientSession(
                timeout=ClientTimeout(total=timeout),
            )
        async with _client_session.get(url) as resp:
            latency = int((time.time() - t0) * 1000)
            if resp.status == 200:
                data = await resp.json()
                _record_diag("ok", latency_ms=latency, ok=True)
                return data
            # Non-200 — record the status code so the user can see it
            body = await resp.text()
            _record_diag(f"http:{resp.status}",
                         error=body[:200],
                         latency_ms=latency)
            return None
    except asyncio.TimeoutError:
        latency = int((time.time() - t0) * 1000)
        _record_diag("timeout",
                     error=f"Bot did not respond within {timeout}s",
                     latency_ms=latency)
        # Force a fresh session next time — a timed-out connection may
        # still be half-open in the pool and cause the next request to
        # hang on the same dead socket.
        await _close_session()
        return None
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        _record_diag(f"error:{type(e).__name__}",
                     error=str(e)[:200],
                     latency_ms=latency)
        # Connection errors (DNS failure, connection refused, etc.) —
        # close the session so the next attempt rebuilds the pool.
        await _close_session()
        return None


async def _close_session() -> None:
    """Close and null the shared client session (forces a fresh one
    on the next fetch). Safe to call multiple times."""
    global _client_session
    if _client_session is not None and not _client_session.closed:
        try:
            await _client_session.close()
        except Exception:
            pass
    _client_session = None


async def post_json(url: str, timeout: float = 5.0) -> dict | None:
    """POST to a URL with a short timeout. Returns None on error."""
    try:
        async with ClientSession(timeout=ClientTimeout(total=timeout)) as session:
            async with session.post(url) as resp:
                try:
                    return await resp.json()
                except Exception:
                    return {"ok": False, "status": resp.status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ----- API handlers -----

async def api_stats(request: web.Request) -> web.Response:
    """GET /api/stats — fetch stats from the forwarder bot.

    The response includes the current diagnostic record so the browser
    can show WHY it can't reach the bot (e.g. 'connection refused',
    'timeout after 8s', 'DNS resolution failed') directly in the UI
    instead of a useless 'Checking...' state."""
    forwarder_stats = await fetch_json(FORWARDER_STATS_URL)
    return web.json_response({
        "forwarder": forwarder_stats,
        "forwarder_online": forwarder_stats is not None,
        # Diagnostic snapshot — lets the dashboard UI render a helpful
        # "Bot unreachable: <reason>" message instead of "Checking...".
        "diag": _diag_snapshot(),
    }, headers=NO_STORE)


async def api_stop_scrape(request: web.Request) -> web.Response:
    """POST /api/stop_scrape?job=J1|all — stop scrape job(s) on the bot.
    The optional `job` query param is forwarded verbatim (J1 / 1 / all);
    without it the bot stops every running scrape."""
    job = (request.query.get("job") or "").strip()
    url = FORWARDER_STOP_SCRAPE_URL
    if job:
        url += f"?job={quote(job)}"
    result = await post_json(url)
    return web.json_response(result or {"ok": False, "error": "Failed to reach forwarder bot"},
                             headers=NO_STORE)


async def api_cancel_caption(request: web.Request) -> web.Response:
    """POST /api/cancel_caption — clear the custom caption."""
    result = await post_json(FORWARDER_CANCEL_CAPTION_URL)
    return web.json_response(result or {"ok": False, "error": "Failed to reach forwarder bot"},
                             headers=NO_STORE)


async def api_reset_stats(request: web.Request) -> web.Response:
    """POST /api/reset_stats — reset all cumulative stats to 0."""
    result = await post_json(FORWARDER_RESET_STATS_URL)
    return web.json_response(result or {"ok": False, "error": "Failed to reach forwarder bot"},
                             headers=NO_STORE)


async def api_health(request: web.Request) -> web.Response:
    """GET /api/health — simple health check."""
    return web.json_response({"ok": True, "service": "dashboard"}, headers=NO_STORE)


def _diag_snapshot() -> dict:
    """Return a JSON-serializable copy of _last_diag with human-readable
    timing info. Used by /api/stats and /api/diag so the browser can
    surface WHY the bot is unreachable."""
    now = time.time()
    last_attempt_age = (now - _last_diag["last_attempt"]) if _last_diag["last_attempt"] else None
    last_ok_age = (now - _last_diag["last_ok"]) if _last_diag["last_ok"] else None
    return {
        "bot_url": _last_diag["bot_url"],
        "last_status": _last_diag["last_status"],
        "last_error": _last_diag["last_error"],
        "last_latency_ms": _last_diag["last_latency_ms"],
        "last_attempt_ago_sec": (round(last_attempt_age, 1)
                                  if last_attempt_age is not None else None),
        "last_ok_ago_sec": (round(last_ok_age, 1)
                             if last_ok_age is not None else None),
        "consecutive_failures": _last_diag["consecutive_failures"],
    }


async def api_diag(request: web.Request) -> web.Response:
    """GET /api/diag — diagnostic snapshot of the dashboard → bot
    connection. Returns the bot's /stats URL, the last fetch outcome
    (ok / timeout / error:<type> / http:NNN), the last error string,
    the last latency, and how many fetches have failed in a row.

    This endpoint is the fastest way to find out WHY the dashboard
    can't reach the bot — curl it directly:

        curl http://YOUR_VPS_IP:8080/api/diag | python3 -m json.tool

    Common outcomes:
      - last_status="never"              — dashboard just started, no poll yet
      - last_status="ok"                 — dashboard CAN reach the bot
      - last_status="timeout"            — bot is up but /stats hangs (busy?)
      - last_status="error:ClientConnectorError" — DNS / network / port issue
      - last_status="http:503"           — bot returned a non-200 status
    """
    # Force a fresh fetch so the diag reflects the CURRENT state, not
    # a stale poll from up to 3s ago.
    await fetch_json(FORWARDER_STATS_URL, timeout=5.0)
    return web.json_response({
        "dashboard_service": "ok",
        "bot_url": FORWARDER_STATS_URL,
        "expected_bot_build": EXPECTED_BOT_BUILD,
        "last_fetch": _diag_snapshot(),
        "hint": (
            "If last_status is not 'ok', the dashboard container cannot "
            "reach the bot container. Common causes: (1) bot container "
            "not running — check `docker ps`; (2) bot's stats server "
            "not listening on port 8081 — check `docker logs "
            "forwarder-bot`; (3) containers on different Docker "
            "networks — check `docker network inspect bots-network`; "
            "(4) FORWARDER_STATS_URL env var set to the wrong address "
            "— check docker-compose.yml."
        ),
    }, headers=NO_STORE)


# ----- HTML dashboard -----

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Forwarder Bot Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #0a0e1a; color: #e0e0e0; padding: 20px; min-height: 100vh; }
        .header { text-align: center; margin-bottom: 25px; }
        .header h1 { font-size: 26px; color: #fff; font-weight: 700; }
        .header .subtitle { color: #6b7280; font-size: 13px; margin-top: 5px; }

        .status-bar { display: flex; justify-content: center; gap: 20px; margin-bottom: 25px;
                      flex-wrap: wrap; }
        .status-pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px;
                       background: #1a1f2e; border-radius: 20px; border: 1px solid #2a3142;
                       font-size: 13px; font-weight: 500; }
        .status-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
        .status-online { background: #10b981; box-shadow: 0 0 8px #10b981; }
        .status-offline { background: #ef4444; box-shadow: 0 0 8px #ef4444; }
        .status-unknown { background: #6b7280; }

        .container { max-width: 1200px; margin: 0 auto; }

        .section-title { font-size: 16px; color: #8b95a7; margin: 25px 0 12px;
                         text-transform: uppercase; letter-spacing: 1px; font-weight: 600; }

        /* All-time stats grid */
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                      gap: 15px; margin-bottom: 25px; }
        .stat-card { background: linear-gradient(135deg, #1a1f2e 0%, #161a26 100%);
                     border-radius: 12px; padding: 20px; border: 1px solid #2a3142;
                     transition: transform 0.2s, border-color 0.2s; }
        .stat-card:hover { transform: translateY(-2px); border-color: #3a4252; }
        .stat-card .label { font-size: 11px; color: #6b7280; text-transform: uppercase;
                            letter-spacing: 0.8px; font-weight: 600; }
        .stat-card .value { font-size: 28px; font-weight: 800; margin-top: 8px; color: #fff; }
        .stat-card .value.green { color: #10b981; }
        .stat-card .value.red { color: #ef4444; }
        .stat-card .value.orange { color: #f59e0b; }
        .stat-card .value.blue { color: #3b82f6; }
        .stat-card .value.purple { color: #a78bfa; }
        .stat-card .icon { font-size: 20px; margin-bottom: 8px; }

        /* Live scrape panel */
        .scrape-panel { background: #1a1f2e; border-radius: 12px; padding: 20px;
                        border: 1px solid #2a3142; margin-bottom: 20px; }
        .scrape-panel.active { border-color: #10b981; box-shadow: 0 0 20px rgba(16,185,129,0.15); }
        .scrape-panel h2 { font-size: 18px; margin-bottom: 15px; display: flex;
                           align-items: center; gap: 10px; }
        .scrape-badge { font-size: 11px; padding: 3px 10px; border-radius: 12px;
                        font-weight: 600; text-transform: uppercase; }
        .badge-running { background: #10b981; color: #fff; }
        .badge-idle { background: #2a3142; color: #6b7280; }

        /* One card per concurrent scrape job (e.g. /scrape + /scrapeid) */
        .job-card { background: #141928; border: 1px solid #2a3142;
                    border-radius: 10px; padding: 14px; margin-bottom: 12px; }
        .job-card-header { display: flex; align-items: center; gap: 8px;
                           flex-wrap: wrap; margin-bottom: 8px; font-size: 13px; }
        .job-chip { font-size: 11px; font-weight: 700; padding: 2px 8px;
                    border-radius: 6px; letter-spacing: 0.5px; }
        .job-chip-id { background: #3b82f6; color: #fff; }
        .job-chip-kind { background: #2a3142; color: #cbd5e1; }
        .job-chip-scrapeid { background: #8b5cf6; color: #fff; }
        .job-meta { color: #8b95a7; font-size: 12.5px; }

        .progress-bar-container { background: #0a0e1a; border-radius: 6px; height: 24px;
                                  overflow: hidden; margin: 12px 0; position: relative; }
        .progress-bar-fill { height: 100%; background: linear-gradient(90deg, #10b981, #34d399);
                             transition: width 0.5s; border-radius: 6px; }
        .progress-text { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
                         font-size: 12px; font-weight: 700; color: #fff; text-shadow: 0 1px 2px rgba(0,0,0,0.5); }

        .live-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
                      gap: 12px; margin: 15px 0; }
        .live-stat { background: #0a0e1a; padding: 12px; border-radius: 8px; text-align: center; }
        .live-stat .num { font-size: 22px; font-weight: 700; color: #fff; }
        .live-stat .lbl { font-size: 10px; color: #6b7280; text-transform: uppercase;
                          letter-spacing: 0.5px; margin-top: 4px; }

        .btn { padding: 10px 18px; border: none; border-radius: 8px; cursor: pointer;
               font-size: 13px; font-weight: 600; transition: all 0.2s; }
        .btn-stop { background: #ef4444; color: #fff; }
        .btn-stop:hover { background: #dc2626; }
        .btn-warn { background: #f59e0b; color: #000; }
        .btn-warn:hover { background: #d97706; }
        .btn-secondary { background: #2a3142; color: #e0e0e0; }
        .btn-secondary:hover { background: #3a4252; }
        .btn:disabled { opacity: 0.4; cursor: not-allowed; }
        .btn-row { display: flex; gap: 10px; margin-top: 15px; flex-wrap: wrap; }

        /* Info cards */
        .info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                     gap: 12px; margin-bottom: 20px; }
        .info-card { background: #1a1f2e; border-radius: 8px; padding: 14px;
                     border: 1px solid #2a3142; }
        .info-card .label { font-size: 11px; color: #6b7280; text-transform: uppercase;
                            letter-spacing: 0.5px; font-weight: 600; }
        .info-card .value { font-size: 14px; color: #e0e0e0; margin-top: 4px; font-weight: 500; }

        .footer { text-align: center; margin-top: 30px; color: #4b5563; font-size: 12px;
                  padding-top: 20px; border-top: 1px solid #1a1f2e; }
        .refresh-indicator { display: inline-block; width: 8px; height: 8px;
                            border-radius: 50%; background: #10b981; margin-left: 8px;
                            animation: pulse 1.5s infinite; }
        @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }

        .empty-state { text-align: center; padding: 40px; color: #6b7280; }
        .empty-state .icon { font-size: 48px; margin-bottom: 12px; }

        /* Version-mismatch banner (stale docker image warning) */
        #version-banner { max-width: 1200px; margin: 0 auto 20px; padding: 14px 18px;
                         background: #3b2a10; border: 1px solid #f59e0b; border-radius: 10px;
                         color: #fcd34d; font-size: 13.5px; line-height: 1.6; }
        #version-banner code { background: #1a1f2e; padding: 2px 7px; border-radius: 5px;
                              font-size: 12.5px; color: #fde68a; }

        /* Connection diagnostic banner — shown when the dashboard can't
           reach the bot. Red for errors (connection refused, DNS failure,
           timeout), with the EXACT error and the bot URL it's trying to
           reach, plus a curl command the user can run to test directly. */
        #conn-banner { max-width: 1200px; margin: 0 auto 20px; padding: 14px 18px;
                       background: #2a1015; border: 1px solid #ef4444; border-radius: 10px;
                       color: #fca5a5; font-size: 13.5px; line-height: 1.6; }
        #conn-banner.ok { background: #102a18; border-color: #10b981; color: #86efac; }
        #conn-banner code { background: #1a1f2e; padding: 2px 7px; border-radius: 5px;
                            font-size: 12.5px; color: #fde68a; }
        #conn-banner .diag-detail { margin-top: 10px; padding-top: 10px;
                                    border-top: 1px solid #3a1420; font-size: 12.5px;
                                    color: #f87171; }
        #conn-banner.ok .diag-detail { color: #4ade80; border-top-color: #1a3a2a; }
        #conn-banner .diag-url { color: #fde68a; font-weight: 600; }

        .status-dot.status-stale { background: #f59e0b; box-shadow: 0 0 8px #f59e0b; }
        .status-pill.warn { border-color: #f59e0b; }
        .status-pill.warn #freshness-text { color: #f59e0b; font-weight: 600; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Forwarder Bot Dashboard</h1>
        <div class="subtitle">Real-time monitoring &amp; control <span class="refresh-indicator"></span></div>
    </div>

    <div class="status-bar">
        <div class="status-pill">
            <span class="status-dot status-unknown" id="bot-dot"></span>
            <span id="bot-status-text">Checking...</span>
        </div>
        <div class="status-pill">
            <span class="status-dot status-unknown" id="telethon-dot"></span>
            <span>Telethon: <span id="telethon-status">?</span></span>
        </div>
        <div class="status-pill" id="build-pill" style="display:none">
            <span id="build-pill-text">build ?</span>
        </div>
        <div class="status-pill warn" id="freshness-pill" style="display:none">
            <span id="freshness-text">?</span>
        </div>
    </div>

    <!-- Version mismatch / stale-image warning: shown when the bot's
         reported `build` differs from EXPECTED_BOT_BUILD above. -->
    <div id="version-banner" style="display:none"></div>

    <!-- Connection diagnostic banner: shown when the dashboard cannot
         reach the bot, with the EXACT error (timeout, connection refused,
         DNS failure, etc.) and the bot URL it's trying to reach. Replaces
         the useless "Checking..." state with actionable info. -->
    <div id="conn-banner" style="display:none"></div>

    <div class="container">
        <!-- All-Time Stats -->
        <div class="section-title">All-Time Statistics</div>
        <div class="stats-grid" id="cumulative-stats">
            <div class="stat-card">
                <div class="icon">🔍</div>
                <div class="label">Total Scrapes</div>
                <div class="value blue" id="cum-scrapes">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">📤</div>
                <div class="label">Total Sent</div>
                <div class="value green" id="cum-sent">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">❌</div>
                <div class="label">Total Failed</div>
                <div class="value red" id="cum-failed">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">⏭</div>
                <div class="label">Total Skipped</div>
                <div class="value orange" id="cum-skipped">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">⏳</div>
                <div class="label">Flood Waits</div>
                <div class="value orange" id="cum-flood">0</div>
            </div>
            <div class="stat-card">
                <div class="icon">💾</div>
                <div class="label">Saved Forwards</div>
                <div class="value purple" id="cum-saved">0</div>
            </div>
        </div>

        <!-- Live Scrape -->
        <div class="section-title">Live Scrape</div>
        <div class="scrape-panel" id="scrape-panel">
            <h2>
                Scrape Status
                <span class="scrape-badge badge-idle" id="scrape-badge">IDLE</span>
            </h2>
            <div id="scrape-content">
                <div class="empty-state">
                    <div class="icon">💤</div>
                    <div>No scrape running</div>
                    <div style="font-size:12px; margin-top:8px; color:#4b5563;">
                        Send /scrape &lt;url&gt; to the bot to start
                    </div>
                </div>
            </div>
        </div>

        <!-- Bot Info -->
        <div class="section-title">Bot Configuration</div>
        <div class="info-grid" id="bot-info"></div>

        <!-- Controls -->
        <div class="section-title">Controls</div>
        <div class="btn-row">
            <button class="btn btn-warn" onclick="cancelCaption()">Clear Caption</button>
            <button class="btn btn-secondary" onclick="resetStats()">Reset All Stats</button>
        </div>
    </div>

    <div class="footer">
        Auto-refreshing every 3 seconds · Last update: <span id="last-update">--</span>
        · Bot data age: <span id="bot-data-age">--</span>
    </div>

    <script>
        const EXPECTED_BOT_BUILD = 'v9';
        // Last good /stats payload + when it arrived. On transient fetch
        // failures we keep rendering it (with a "reconnecting" pill) for
        // up to 15s instead of instantly blanking everything to "Bot
        // Offline" — a busy bot can be slow, and the page flipping fully
        // offline on one slow poll LOOKS like "dashboard doesn't update".
        let lastGood = null, lastGoodAt = 0;
        const STALE_GRACE_MS = 15000;

        // ---- poll scheduling ----
        // STRICTLY SERIAL, self-scheduling chain — NEVER setInterval.
        // setInterval + async fetch overlaps in-flight requests when the
        // bot is slow (exactly during scrapes): an older response can
        // resolve AFTER a newer one and re-render stale numbers, making
        // the dashboard look frozen or jump backwards. `let` (not const)
        // so the Node test-harness can shorten both.
        let POLL_MS = 3000;
        let FETCH_TIMEOUT_MS = 9000;
        let pollBusy = false;      // overlap guard
        let pollSeq = 0;           // superseded-response guard
        let pollTimer = null;

        // Run one update section; a failure in one section must never
        // block the others (a JS error in updateScrape used to abort the
        // whole tick, freezing the footer timestamp too).
        function safe(fn, ...args) {
            try { fn(...args); } catch (e) { console.error(fn.name, e); }
        }

        async function fetchStats() {
            if (pollBusy) return;              // one poll at a time
            pollBusy = true;
            const mySeq = ++pollSeq;
            let killTimer = null;
            try {
                // Cache-buster + no-store: proxies (Cloudflare "cache
                // everything" rules) and the browser must never serve a
                // cached /api/stats reply — that looks EXACTLY like a
                // frozen dashboard.
                const url = '/api/stats?_=' + Date.now();
                const opts = { cache: 'no-store' };
                const ctl = (typeof AbortController !== 'undefined')
                    ? new AbortController() : null;
                if (ctl) opts.signal = ctl.signal;
                // One work promise covers fetch AND body-parse; raced
                // against a hard timeout so a hung connection (laptop
                // sleep/wake, network switch, proxy idle timeout) can
                // never wedge the poll loop forever.
                const work = (async () => {
                    const resp = await fetch(url, opts);
                    return await resp.json();
                })();
                const timeoutP = new Promise((_, reject) => {
                    killTimer = setTimeout(
                        () => reject(new Error('poll timeout')),
                        FETCH_TIMEOUT_MS);
                });
                let data;
                try {
                    data = await Promise.race([work, timeoutP]);
                } catch (e) {
                    if (ctl) ctl.abort();   // cancel the hung request
                    throw e;
                } finally {
                    if (killTimer) clearTimeout(killTimer);
                }
                if (mySeq !== pollSeq) return;          // superseded — drop
                if (data.forwarder_online) {
                    lastGood = data;
                    lastGoodAt = Date.now();
                    render(data, false);
                } else if (lastGood && Date.now() - lastGoodAt < STALE_GRACE_MS) {
                    render(lastGood, true);
                } else {
                    render(data, false);
                }
            } catch (e) {
                if (mySeq === pollSeq) {
                    // /api/stats itself unreachable or timed out — reuse
                    // recent good data. The empty-data render now carries
                    // a diag stub so the connection banner can show
                    // "Bot unreachable (browser fetch failed)" instead of
                    // leaving the page stuck at "Checking...".
                    if (lastGood && Date.now() - lastGoodAt < STALE_GRACE_MS) {
                        render(lastGood, true);
                    } else {
                        render({
                            forwarder_online: false,
                            forwarder: null,
                            diag: {
                                bot_url: '/api/stats',
                                last_status: 'error:BrowserFetchFailed',
                                last_error: String(e),
                                last_latency_ms: 0,
                                last_attempt_ago_sec: 0,
                                last_ok_ago_sec: null,
                                consecutive_failures: 1,
                            },
                        }, false);
                    }
                    console.error('Fetch failed:', e);
                }
            } finally {
                pollBusy = false;
            }
        }

        function render(data, stale) {
            safe(updateVersionBanner, data);
            safe(updateConnBanner, data);
            safe(updateBot, data, stale);
            safe(updateCumulative, data);
            safe(updateScrape, data);
            safe(updateFreshness, data, stale);
            if (!stale) {
                document.getElementById('last-update').textContent =
                    new Date().toLocaleTimeString();
            }
        }

        // ---- connection diagnostic banner ----
        // Renders the EXACT reason the dashboard can't reach the bot
        // (timeout, connection refused, DNS failure, non-200) into a
        // prominent red banner at the top of the page. This replaces
        // the useless "Checking..." state with actionable info: the
        // user sees the bot URL, the error, and a curl command to test.
        function updateConnBanner(data) {
            const banner = document.getElementById('conn-banner');
            if (!banner) return;
            const diag = data.diag || {};
            const botUrl = diag.bot_url || '?';
            const status = diag.last_status || 'never';

            // If the bot is online, hide the banner (no error to show).
            if (data.forwarder_online && status === 'ok') {
                banner.style.display = 'none';
                return;
            }

            // If we've never even tried to fetch yet, hide — the initial
            // "Checking..." state is handled by the watchdog below.
            if (status === 'never') {
                banner.style.display = 'none';
                return;
            }

            // Build a human-readable error message based on the status.
            let title, detail;
            if (status === 'ok') {
                // Bot recovered — show a brief green confirmation then
                // let the next poll hide the banner entirely.
                banner.className = 'ok';
                title = '✅ Bot connection restored.';
                detail = '';
            } else if (status === 'timeout') {
                banner.className = '';
                title = '❌ Bot unreachable: TIMEOUT.';
                detail = 'The dashboard reached the bot container but it ' +
                         'did not respond to /stats within the timeout. ' +
                         'This usually means the bot is busy (running a ' +
                         'large scrape) or its stats server crashed. ' +
                         'Check: docker logs forwarder-bot';
            } else if (status.startsWith('error:')) {
                banner.className = '';
                const errType = status.substring(6);
                title = '❌ Bot unreachable: ' + esc(errType) + '.';
                if (errType.includes('ClientConnectorError') ||
                    errType.includes('ConnectorDNSError')) {
                    detail = 'The dashboard container cannot connect to ' +
                             'the bot container. Most likely cause: the ' +
                             'bot container is not running, or the Docker ' +
                             'network is misconfigured, or the bot is not ' +
                             'listening on port 8081.\n' +
                             'Check:\n' +
                             '  docker ps  (is forwarder-bot running?)\n' +
                             '  docker logs forwarder-bot\n' +
                             '  docker network inspect bots-network';
                } else {
                    detail = 'Error detail: ' + esc(diag.last_error || '');
                }
            } else if (status.startsWith('http:')) {
                banner.className = '';
                title = '❌ Bot returned HTTP ' + esc(status.substring(5)) + '.';
                detail = 'The bot is reachable but returned a non-200 ' +
                         'response. Error body: ' + esc(diag.last_error || '');
            } else {
                banner.className = '';
                title = '❌ Bot unreachable: ' + esc(status) + '.';
                detail = 'Error: ' + esc(diag.last_error || '');
            }

            const failures = diag.consecutive_failures || 0;
            const latency = diag.last_latency_ms || 0;
            const attemptAgo = diag.last_attempt_ago_sec;
            const okAgo = diag.last_ok_ago_sec;

            let timing = '';
            if (attemptAgo != null) {
                timing += 'Last attempt: ' + attemptAgo + 's ago';
                if (latency) timing += ' (' + latency + 'ms)';
            }
            if (okAgo != null) {
                timing += ' · Last OK: ' + okAgo + 's ago';
            }
            if (failures > 0) {
                timing += ' · ' + failures + ' consecutive failure' +
                           (failures === 1 ? '' : 's');
            }

            banner.style.display = 'block';
            banner.innerHTML =
                '<strong>' + title + '</strong><br>' +
                'Bot endpoint: <span class="diag-url">' + esc(botUrl) + '</span><br>' +
                (detail ? '<div class="diag-detail">' + detail.replace(/\n/g, '<br>') + '</div>' : '') +
                (timing ? '<div class="diag-detail">' + esc(timing) + '</div>' : '') +
                '<div class="diag-detail">Test directly: <code>curl ' +
                esc(window.location.origin) + '/api/diag</code></div>';
        }

        // ---- data-freshness watchdog ----
        // /stats embeds the BOT's own clock (`timestamp`, unix seconds).
        // A healthy pipeline advances it on EVERY poll even while
        // counters sit still (long flood waits / breaks — the countdown
        // is recomputed server-side per request). If it does NOT advance
        // across 2+ online polls, the reply is cached or the bot process
        // is stalled — an amber pill then makes "waiting" vs "dead"
        // obvious at a glance instead of looking like a frozen dashboard.
        let lastBotTs = 0, frozenPolls = 0;

        function updateFreshness(data, stale) {
            const pill = document.getElementById('freshness-pill');
            const text = document.getElementById('freshness-text');
            if (stale) return;              // amber "Bot slow" pill covers it
            const f = data.forwarder || {};
            const botTs = Number(f.timestamp) || 0;
            if (botTs) {
                if (botTs <= lastBotTs + 0.001) frozenPolls += 1;
                else frozenPolls = 0;
                lastBotTs = botTs;
                const age = Math.max(0, Math.floor(Date.now() / 1000 - botTs));
                document.getElementById('bot-data-age').textContent = age + 's';
            }
            if (!data.forwarder_online) {
                pill.style.display = 'none';
                frozenPolls = 0;
                return;
            }
            if (frozenPolls >= 2) {
                pill.style.display = '';
                text.textContent = 'Bot data frozen ' + frozenPolls +
                    ' polls — cached reply or stalled bot. Check: docker logs forwarder-bot';
            } else {
                pill.style.display = 'none';
            }
        }

        function updateVersionBanner(data) {
            const banner = document.getElementById('version-banner');
            const pill = document.getElementById('build-pill');
            const pillText = document.getElementById('build-pill-text');
            const f = data.forwarder || {};
            const build = f.build;
            if (build) {
                pill.style.display = '';
                pillText.textContent = 'bot build: ' + build;
            }
            if (!data.forwarder_online) {
                banner.style.display = 'none';
                return;
            }
            if (!build) {
                // Bot predates the `build` field — definitely an old image.
                banner.style.display = 'block';
                banner.innerHTML =
                    '⚠️ The bot does not report a code build — it is running an <b>old image</b> ' +
                    'and will not show live countdowns, concurrent jobs or the 10-minute ' +
                    'flood-wait cap. On the VPS run:<br>' +
                    '<code>docker-compose up -d --build</code> ' +
                    '(a plain <code>up -d</code> does NOT rebuild changed code).';
            } else if (build !== EXPECTED_BOT_BUILD) {
                banner.style.display = 'block';
                banner.innerHTML =
                    '⚠️ Version mismatch: bot reports build <b>' + esc(build) + '</b> but this ' +
                    'dashboard expects <b>' + EXPECTED_BOT_BUILD + '</b>. One container is ' +
                    'running stale code — rebuild BOTH on the VPS:<br>' +
                    '<code>docker-compose up -d --build</code>';
            } else {
                banner.style.display = 'none';
            }
        }

        function updateBot(data, stale) {
            const dot = document.getElementById('bot-dot');
            const text = document.getElementById('bot-status-text');
            const tDot = document.getElementById('telethon-dot');
            const tStatus = document.getElementById('telethon-status');

            if (stale) {
                // Recent good data being reused while a poll failed —
                // amber pill instead of a full offline flip.
                dot.className = 'status-dot status-stale';
                const age = Math.floor((Date.now() - lastGoodAt) / 1000);
                text.textContent = 'Bot slow — showing ' + age + 's-old data';
                return;
            }

            if (!data.forwarder_online) {
                dot.className = 'status-dot status-offline';
                text.textContent = 'Bot Offline';
                tDot.className = 'status-dot status-offline';
                tStatus.textContent = 'offline';
                document.getElementById('bot-info').innerHTML = '';
                return;
            }

            dot.className = 'status-dot status-online';
            text.textContent = 'Bot Online';
            const f = data.forwarder || {};

            // Telethon status
            if (f.telethon === 'connected') {
                tDot.className = 'status-dot status-online';
            } else {
                tDot.className = 'status-dot status-offline';
            }
            tStatus.textContent = f.telethon || 'unknown';

            // Bot info
            let capMode = 'Original captions';
            if (f.custom_caption === '') capMode = 'STRIP (no captions)';
            else if (f.custom_caption) capMode = '"' + f.custom_caption.substring(0,40) + (f.custom_caption.length > 40 ? '...' : '') + '"';

            const isForum = f.destination_is_forum ? 'Yes' : 'No';
            document.getElementById('bot-info').innerHTML = `
                <div class="info-card"><div class="label">Bot Username</div><div class="value">@${f.bot_name || '?'}</div></div>
                <div class="info-card"><div class="label">Mode</div><div class="value">${f.mode || '?'}</div></div>
                <div class="info-card"><div class="label">Destination</div><div class="value">${f.destination_chat_title || f.destination_group || 'Not set'}</div></div>
                <div class="info-card"><div class="label">Is Forum</div><div class="value">${isForum}</div></div>
                <div class="info-card"><div class="label">Caption Mode</div><div class="value">${capMode}</div></div>
            `;
        }

        function updateCumulative(data) {
            if (!data.forwarder_online) return;
            const c = (data.forwarder || {}).cumulative || {};
            // The bot now pushes per-message / per-batch cumulative
            // deltas LIVE into SQLite (handlers/admin.py stats_callback),
            // so these persisted values already tick up DURING a scrape
            // instead of jumping at the end — the dashboard just displays
            // them. (We deliberately do NOT overlay active jobs' live
            // sent_count here — that would double-count, because the
            // bot has already flushed those increments to SQLite.)
            document.getElementById('cum-scrapes').textContent = c.total_scrapes || 0;
            document.getElementById('cum-sent').textContent    = c.total_sent || 0;
            document.getElementById('cum-failed').textContent  = c.total_failed || 0;
            document.getElementById('cum-skipped').textContent  = c.total_skipped || 0;
            document.getElementById('cum-flood').textContent   = c.total_flood_waits || 0;
            document.getElementById('cum-saved').textContent   = c.total_saved_forwards || 0;
        }

        function updateScrape(data) {
            if (!data.forwarder_online) return;
            const f = data.forwarder || {};
            const panel = document.getElementById('scrape-panel');
            const badge = document.getElementById('scrape-badge');
            const content = document.getElementById('scrape-content');

            // Multi-job payload: f.scrapes is an array of job objects.
            // (Older bots only send the single f.scrape object — wrap it.)
            let jobs = Array.isArray(f.scrapes) ? f.scrapes.slice() : [];
            if (!jobs.length && f.scrape && (f.scrape_running || f.scrape.total_seen > 0)) {
                jobs = [Object.assign({}, f.scrape, {running: !!f.scrape_running})];
            }
            const active = jobs.filter(j => j.running);

            if (active.length > 0) {
                panel.classList.add('active');
                badge.className = 'scrape-badge badge-running';
                badge.textContent = active.length > 1
                    ? 'RUNNING (' + active.length + ')' : 'RUNNING';
                let html = active.map(jobCard).join('');
                if (active.length > 1) {
                    html += '<div class="btn-row"><button class="btn btn-stop" onclick="stopScrape(\'all\')">Stop All Jobs</button></div>';
                }
                content.innerHTML = html;
            } else {
                panel.classList.remove('active');
                badge.className = 'scrape-badge badge-idle';
                badge.textContent = 'IDLE';
                // Show last scrape results if available
                const s = f.scrape || {};
                if (s && s.total_seen > 0) {
                    content.innerHTML = `
                        <div style="color:#8b95a7; font-size:13px; margin-bottom:10px;">Last scrape results${s.job_id ? ' — ' + esc(s.job_id) + (s.kind ? ' (/' + esc(s.kind) + ')' : '') : ''}:</div>
                        <div class="live-stats">
                            <div class="live-stat"><div class="num" style="color:#10b981">${s.sent_count || 0}</div><div class="lbl">Sent</div></div>
                            <div class="live-stat"><div class="num" style="color:#ef4444">${s.failed_count || 0}</div><div class="lbl">Failed</div></div>
                            <div class="live-stat"><div class="num" style="color:#f59e0b">${s.skipped_count || 0}</div><div class="lbl">Skipped</div></div>
                            <div class="live-stat"><div class="num">${s.total_seen || 0}</div><div class="lbl">Seen</div></div>
                            <div class="live-stat"><div class="num">${Math.floor(s.elapsed_sec || 0)}s</div><div class="lbl">Elapsed</div></div>
                        </div>
                        <div style="color:#6b7280; font-size:12px; margin-top:8px;">Source: ${esc(s.source_ref || '?')} → ${esc(s.dest_label || '?')}</div>
                    `;
                } else {
                    content.innerHTML = `
                        <div class="empty-state">
                            <div class="icon">💤</div>
                            <div>No scrape has been run yet</div>
                            <div style="font-size:12px; margin-top:8px; color:#4b5563;">
                                Send /scrape &lt;url&gt; to the bot to start
                            </div>
                        </div>
                    `;
                }
            }
        }

        function esc(s) {
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function fmtLeft(left) {
            left = Math.max(0, Math.floor(left || 0));
            if (left >= 3600) return Math.floor(left / 3600) + 'h' + String(Math.floor((left % 3600) / 60)).padStart(2, '0') + 'm';
            if (left >= 60) return Math.floor(left / 60) + 'm' + String(left % 60).padStart(2, '0') + 's';
            return left + 's';
        }

        // One card per scrape job — each job (a /scrape and/or a
        // /scrapeid running concurrently) gets its own activity line,
        // live wait-phase countdown, progress bar, counters, and Stop button.
        function jobCard(s) {
            const jid = esc(s.job_id || '');
            const kind = s.kind === 'scrapeid' ? 'scrapeid' : (s.kind || 'scrape');
            const pct = s.total_seen > 0 ? (s.sent_count / s.total_seen * 100) : 0;
            const elapsed = s.elapsed_sec || 0;
            const speed = elapsed > 0 ? (s.sent_count / (elapsed / 60)).toFixed(1) : 0;
            const inFlight = s.in_flight || 0;

            // Live wait phase (flood wait / recovery break) with a
            // ticking countdown — makes long waits clearly visible
            // instead of looking like a frozen dashboard.
            let phaseHtml = '';
            if (s.phase) {
                const label = s.phase === 'break' ? '☕ Recovery break'
                            : s.phase === 'flood' ? '⏳ Flood wait' : '⏳ ' + esc(s.phase);
                const color = s.phase === 'break' ? '#3b82f6' : '#f59e0b';
                phaseHtml = `<div style="margin-bottom:8px; color:${color}; font-weight:600; font-size:14px;">${label}: ${fmtLeft(s.phase_seconds_left)} remaining</div>`;
            }

            // Liveness: how long since the last real progress update
            let livenessHtml = '';
            if (s.seconds_since_progress != null && s.seconds_since_progress > 30) {
                livenessHtml = `<div style="margin-bottom:8px; color:#6b7280; font-size:12px;">Last progress ${Math.floor(s.seconds_since_progress)}s ago</div>`;
            }

            let activity = 'Scanning...';
            if (s.phase === 'flood') activity = 'Flood wait — auto-resumes';
            else if (s.phase === 'break') activity = 'Recovery break — auto-resumes';
            else if (inFlight > 0) activity = 'Sending ' + inFlight + ' item(s)...';
            else if (kind === 'scrapeid') activity = 'Forwarding batches...';

            return `
                <div class="job-card">
                    <div class="job-card-header">
                        <span class="job-chip job-chip-id">${jid}</span>
                        <span class="job-chip job-chip-${kind}">/${kind}</span>
                        <span class="job-meta">${esc(s.source_ref || '?')} → ${esc(s.dest_label || '?')}</span>
                    </div>
                    <div style="color:#8b95a7; font-size:12.5px; margin-bottom:6px;">
                        <strong>Order:</strong> ${esc(s.order || '?')} &nbsp;|&nbsp;
                        <strong>Filter:</strong> ${esc(s.filter || 'ALL')} &nbsp;|&nbsp;
                        <strong>Parallel:</strong> ${s.parallel || 5}
                    </div>
                    <div style="margin-bottom:8px; color:#10b981; font-weight:600; font-size:14px;">${activity}</div>
                    ${phaseHtml}
                    ${livenessHtml}
                    <div class="progress-bar-container">
                        <div class="progress-bar-fill" style="width:${pct}%"></div>
                        <div class="progress-text">${pct.toFixed(0)}%</div>
                    </div>
                    <div class="live-stats">
                        <div class="live-stat"><div class="num" style="color:#10b981">${s.sent_count || 0}</div><div class="lbl">Sent</div></div>
                        <div class="live-stat"><div class="num" style="color:#3b82f6">${inFlight}</div><div class="lbl">In-flight</div></div>
                        <div class="live-stat"><div class="num" style="color:#ef4444">${s.failed_count || 0}</div><div class="lbl">Failed</div></div>
                        <div class="live-stat"><div class="num" style="color:#f59e0b">${s.skipped_count || 0}</div><div class="lbl">Skipped</div></div>
                        <div class="live-stat"><div class="num">${s.total_seen || 0}</div><div class="lbl">Seen</div></div>
                        <div class="live-stat"><div class="num">${Math.floor(elapsed)}s</div><div class="lbl">Elapsed</div></div>
                        <div class="live-stat"><div class="num">${speed}</div><div class="lbl">Items/min</div></div>
                        <div class="live-stat"><div class="num">${s.last_message_id || 0}</div><div class="lbl">Last ID</div></div>
                    </div>
                    <div class="btn-row">
                        <button class="btn btn-stop" onclick="stopScrape('${jid}')">Stop ${jid}</button>
                    </div>
                </div>
            `;
        }

        async function stopScrape(jobId) {
            try {
                const url = jobId
                    ? '/api/stop_scrape?job=' + encodeURIComponent(jobId)
                    : '/api/stop_scrape';
                await fetch(url, { method: 'POST' });
                setTimeout(fetchStats, 500);
            } catch (e) { console.error(e); }
        }

        async function cancelCaption() {
            if (!confirm('Clear the custom caption?')) return;
            try {
                await fetch('/api/cancel_caption', { method: 'POST' });
                setTimeout(fetchStats, 500);
            } catch (e) { console.error(e); }
        }

        async function resetStats() {
            if (!confirm('Reset ALL cumulative stats to 0? This cannot be undone.')) return;
            try {
                await fetch('/api/reset_stats', { method: 'POST' });
                setTimeout(fetchStats, 500);
            } catch (e) { console.error(e); }
        }

        // ---- poll loop: strictly serial, self-scheduling ----
        // pollCycle always re-arms itself AFTER the current poll settles,
        // so requests can never overlap and responses can never arrive
        // out of order (the classic "frozen / backwards-jumping numbers"
        // bug of setInterval + slow backend).
        async function pollCycle() {
            try {
                await fetchStats();
            } catch (e) {
                console.error(e);
            } finally {
                pollTimer = setTimeout(pollCycle, POLL_MS);
            }
        }

        // Force an immediate poll. Used when the tab becomes visible
        // again (browsers throttle timers in hidden tabs to ~1/min —
        // Chrome's "intensive throttling" can freeze a backgrounded
        // dashboard for minutes) and when the network comes back after
        // sleep / Wi-Fi switch.
        function kickPoll() {
            if (pollBusy) return;               // a poll is in flight
            if (pollTimer) clearTimeout(pollTimer);
            pollTimer = setTimeout(pollCycle, 0);
        }
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) kickPoll();
        });
        if (typeof window !== 'undefined' && window.addEventListener) {
            window.addEventListener('online', kickPoll);
        }

        // ---- "Checking..." watchdog ----
        // If the FIRST poll hasn't completed within 15 seconds (the
        // FETCH_TIMEOUT_MS is 9s, plus the bot's /api/stats handler has
        // an 8s timeout, so 15s covers the worst case), force the UI out
        // of the initial "Checking..." state into a diagnostic message.
        // This catches the case where a reverse proxy (nginx/Cloudflare)
        // buffers the /api/stats response and never delivers it to the
        // browser, leaving the dashboard stuck at "Checking..." forever.
        // Also fires if render() was somehow never called at all.
        let _watchdogFired = false;
        function _checkingWatchdog() {
            if (_watchdogFired) return;
            // If the bot status text has advanced past "Checking...",
            // render() was called — cancel the watchdog.
            const txt = document.getElementById('bot-status-text');
            if (!txt) return;
            if (txt.textContent !== 'Checking...') {
                _watchdogFired = true;
                return;
            }
            _watchdogFired = true;
            // Still "Checking..." after 15s — the first poll never
            // completed. Flip to a diagnostic state explaining that
            // the /api/stats request is hanging (most likely a proxy
            // buffering the response), and show how to test directly.
            txt.textContent = 'Bot unreachable (first poll hung)';
            const dot = document.getElementById('bot-dot');
            if (dot) dot.className = 'status-dot status-offline';
            const banner = document.getElementById('conn-banner');
            if (banner) {
                banner.style.display = 'block';
                banner.className = '';
                banner.innerHTML =
                    '<strong>❌ Dashboard is stuck: the first /api/stats ' +
                    'request never completed.</strong><br>' +
                    'The dashboard HTML loaded, but the browser\'s fetch ' +
                    'to <code>/api/stats</code> is hanging (no response ' +
                    'after 15 seconds). This is NOT a bot problem — it ' +
                    'usually means a reverse proxy (nginx, Cloudflare, ' +
                    'Traefik) in front of the dashboard is buffering the ' +
                    'response and not streaming it to the browser.<br><br>' +
                    '<strong>To fix:</strong><br>' +
                    '1. <strong>Open the browser DevTools → Network tab</strong> ' +
                    'and look at the /api/stats request — is it stuck ' +
                    '"Pending"? That confirms a proxy buffering issue.<br>' +
                    '2. <strong>Test the dashboard directly</strong> ' +
                    '(bypass any proxy): <code>curl http://YOUR_VPS_IP:8080/api/diag</code><br>' +
                    '3. <strong>Test the bot directly from the VPS</strong>: ' +
                    '<code>docker exec dashboard curl -s ' +
                    'http://forwarder-bot:8081/stats | head -c 200</code><br>' +
                    '4. If the proxy is Cloudflare, disable "Always Online" ' +
                    'and "Rocket Loader" for this domain, and set a ' +
                    'Page Rule to bypass caching for <code>*/api/*</code>.<br>' +
                    '5. If the proxy is nginx, ensure it is NOT buffering ' +
                    'responses: add <code>proxy_buffering off;</code> to ' +
                    'the dashboard location block.';
            }
            // Also try a direct fetch to /api/diag and render the result
            // into the banner — this works even if /api/stats is buffered
            // because /api/diag is a different endpoint.
            fetch('/api/diag?_=' + Date.now(), { cache: 'no-store' })
                .then(r => r.json())
                .then(d => {
                    if (d && d.last_fetch) {
                        const lf = d.last_fetch;
                        const detail = document.createElement('div');
                        detail.className = 'diag-detail';
                        detail.innerHTML = '<br><strong>Direct /api/diag ' +
                            'result:</strong><br>Bot URL: ' +
                            esc(lf.bot_url || '?') + '<br>Status: ' +
                            esc(lf.last_status || 'never') + '<br>Error: ' +
                            esc(lf.last_error || '(none)') + '<br>' +
                            esc(lf.consecutive_failures || 0) +
                            ' consecutive failure(s)';
                        banner.appendChild(detail);
                    }
                })
                .catch(e => {
                    const detail = document.createElement('div');
                    detail.className = 'diag-detail';
                    detail.innerHTML = '<br><strong>/api/diag also failed: ' +
                        esc(String(e)) + '</strong> — the dashboard ' +
                        'container itself may be down or unreachable.';
                    banner.appendChild(detail);
                });
        }
        setTimeout(_checkingWatchdog, 15000);

        pollCycle();
    </script>
</body>
</html>"""


async def dashboard_handler(request: web.Request) -> web.Response:
    """Serve the HTML dashboard. no-store: the browser must always run
    the CURRENT page JS (an older cached copy can lack every fix below
    and look like a dead dashboard; a hard refresh fixes that)."""
    return web.Response(text=DASHBOARD_HTML, content_type="text/html",
                        headers=NO_STORE)


async def main():
    app = web.Application()
    app.router.add_get("/", dashboard_handler)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/diag", api_diag)
    app.router.add_post("/api/stop_scrape", api_stop_scrape)
    app.router.add_post("/api/cancel_caption", api_cancel_caption)
    app.router.add_post("/api/reset_stats", api_reset_stats)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", DASHBOARD_PORT)
    await site.start()
    logger.info("Dashboard listening on 0.0.0.0:%d", DASHBOARD_PORT)
    logger.info("Bot endpoint: %s", FORWARDER_STATS_URL)
    logger.info("Expected bot build: %s", EXPECTED_BOT_BUILD)
    logger.info("Diagnostic endpoint: http://0.0.0.0:%d/api/diag",
                DASHBOARD_PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
