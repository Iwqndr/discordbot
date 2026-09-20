"""
Flask dashboard: all routes and API endpoints.
The actual HTML lives in templates/dashboard.html.

Edit this file when you want to add or modify API endpoints.
Edit templates/dashboard.html when you want to change the UI.
"""

import os
import re
import json
import math
import time
import datetime
import platform
import subprocess
import asyncio
import threading
import uuid
import random
import urllib.error
import urllib.parse
import urllib.request
import psutil
import discord
from flask import Flask, render_template, request, jsonify, session, redirect
from antiraid import (
    history_tracker,
    history_flags_from,
    notes_store,
    watchlist,
    log_index,
    thread_index,
    account_risk_score,
    compute_risk,
    gather_moderation_history,
    fetch_user_thread_lines,
    load_verification_config,
    save_verification_config,
    refresh_verification_panel,
    deploy_verification_panel,
    verification_role_ids,
    load_ticket_panel_config,
    save_ticket_panel_config,
    refresh_ticket_panel,
    deploy_ticket_panel,
    ticket_support_url,
    member_is_verified,
    MESSAGE_LOG_DIR,
    DELETED_LOG_DIR,
    AUTOMOD_RULES,
    AUTOMOD_ACTIONS,
    automod_config,
    get_anti_raid,
    send_punishment_dm,
    record_case,
    case_store,
    punishment_label,
    media_only_store,
    selfpromo_store,
    is_media_only,
    is_selfpromo_channel,
)
from supabase_helper import fetch_member
from config import (
    TICKET_CHANNEL_ID,
    TICKET_PING_ROLE_ID,
    DISCORD_CLIENT_ID,
    DISCORD_CLIENT_SECRET,
    DISCORD_REDIRECT_URI,
    FLASK_SECRET_KEY,
    GUILD_ID,
    CATBOX_USERHASH,
    CATBOX_API,
    TICKET_ATTACH_MAX_FILES,
    TICKET_ATTACH_MAX_MB,
)

# The HTML sits in the repository-root templates/ folder, which is one level up
# from referenced/, so the template folder is pointed there explicitly.
_TEMPLATE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates")
)

app = Flask(__name__, template_folder=_TEMPLATE_DIR)
app.secret_key = FLASK_SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
BOT_START = time.time()

# The bot instance is injected at startup by main.py
bot = None

def set_bot(b):
    global bot
    bot = b


# ==========================================================
# PAGE
# ==========================================================

# The tunnel address serves the PANEL, not the member hub.
#
# member.html belongs to the Cloudflare Pages site (`MEMBER_SITE_URL`, i.e.
# jamesheston.pages.dev), which serves it and brings its own Functions for its
# data. The tunnel exists so the staff panel can be reached from anywhere else,
# so a request arriving on the tunnel's own address is panel traffic. Without
# this, opening the tunnel link landed on a second, unlisted copy of the member
# hub — command list and all — on an address nobody is watching, with the panel
# hidden a click behind it.
#
# Only the two hub *pages* are affected; every panel route, API and the local
# address behave exactly as before.

_TUNNEL_HOST_SUFFIXES = (".trycloudflare.com", ".loca.lt")


def _via_tunnel() -> bool:
    """Did this request arrive over the published tunnel address?"""
    host = (request.host or "").split(":")[0].strip().lower()
    if not host:
        return False
    if host.endswith(_TUNNEL_HOST_SUFFIXES):
        return True
    # A named tunnel, or any other host, is trusted only when it is the address
    # that was actually published. Imported here so this module does not depend
    # on the tunnel starting first.
    try:
        import tunnel

        published = urllib.parse.urlsplit(tunnel.public_url() or "").hostname or ""
    except Exception:
        return False
    return bool(published) and host == published.lower()


@app.before_request
def _member_hub_is_not_public():
    """Send the member hub's address on the tunnel to the panel instead."""
    if request.path not in ("/", "/member"):
        return None
    if not _via_tunnel():
        return None
    return redirect("/admin")


@app.route("/")
def index():
    """Public member hub: support tickets, command list, and info.

    The member page is the site root; the staff panel lives at /admin, so the
    two can never be confused for one another. The public tunnel address is the
    exception — there the member page is not served at all, because that
    address is the panel's (see _member_hub_is_not_public above).
    """
    return render_template("member.html")


@app.route("/admin")
def admin_page():
    """Staff panel. The page itself asks for authorization before it opens."""
    return render_template("dashboard.html")


@app.route("/member")
def member_page():
    """The member hub's original address, kept so existing links still work.

    Over the tunnel this lands on /admin as well, for the same reason as `/`.
    """
    return redirect("/")


# ==========================================================
# MEMBER LOGIN (Discord OAuth, identify only)
# ==========================================================

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/users/@me"

# Discord sits behind Cloudflare, which answers 403 to the default
# "Python-urllib/x.y" agent. Always send a real one.
OAUTH_HEADERS = {"User-Agent": "MemberHub/1.0 (+https://discord.com)"}


def _http_body(exc):
    """Read the JSON error body Discord sends back, for the console log."""
    try:
        return exc.read().decode("utf-8", "replace")[:400]
    except Exception:
        return ""


def _oauth_ready():
    return bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET)


def _refresh_session_member(stored):
    """Keep the session's copy of the account in step with Discord.

    The snapshot is taken once at sign-in, but names, nicknames and avatars
    change all the time — and Discord's CDN URL changes with the avatar. The
    Members tab has always read live from the guild, so read live here too and
    nobody has to re-authorize to see their own new name or picture.
    """
    uid = str(stored.get("user_id") or "")
    if not uid.isdigit() or bot is None or getattr(bot, "user", None) is None:
        return stored

    user = None
    guild = _main_guild()
    if guild is not None:
        user = guild.get_member(int(uid))
    if user is None:
        try:
            user = bot.get_user(int(uid))
        except Exception:
            user = None
    if user is None:
        return stored

    try:
        stored["username"] = user.name or stored.get("username") or ""
        stored["display_name"] = (
            getattr(user, "display_name", None) or user.name or stored.get("display_name") or ""
        )
        avatar = getattr(user, "display_avatar", None)
        if avatar:
            stored["avatar_url"] = avatar.url
        session["member"] = stored
    except Exception:
        return stored
    return stored


def _current_member():
    member = session.get("member") or {}
    if not member.get("user_id"):
        return {}
    try:
        return _refresh_session_member(member)
    except Exception:
        return member


def _current_member_id(data=None):
    """Logged-in Discord id when there is one, else an id from the request."""
    member = _current_member()
    if member:
        return str(member["user_id"])
    return str((data or {}).get("user_id") or "").strip()


def _avatar_url(user):
    uid = str(user.get("id") or "")
    hashed = user.get("avatar")
    if uid and hashed:
        ext = "gif" if str(hashed).startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{uid}/{hashed}.{ext}?size=128"
    index = (int(uid) >> 22) % 6 if uid.isdigit() else 0
    return f"https://cdn.discordapp.com/embed/avatars/{index}.png"


def _owner_unlocked():
    """Owner mode is remembered per account, not per browser.

    Unlocking stamps the account that was signed in at the time (empty when the
    key was entered while signed out). If a different account later signs in on
    the same browser the stamp no longer matches, so the unlock is treated as
    expired instead of being handed to them.
    """
    if not session.get("owner_ok"):
        return False
    current = str((session.get("member") or {}).get("user_id") or "")
    return str(session.get("owner_ok_for") or "") == current


def _safe_next(path, fallback="/"):
    """Only ever bounce back to a path on this site, never somewhere else."""
    path = str(path or "").strip()
    if not path.startswith("/") or path.startswith("//"):
        return fallback
    return path[:200]


@app.route("/auth/discord")
def auth_discord():
    """Start a login.

    Preferred route: bounce to the member site's `/dashboard`, which already
    knows this person from its own session and will hand a signed proof back.
    That keeps the panel's callback out of the picture entirely — which matters
    because the tunnel address changes on every restart, and a callback
    registered with Discord cannot keep up with that.

    Falls back to running OAuth here when the member site is not configured.
    """
    if MEMBER_SITE_URL:
        target = _safe_next(request.args.get("next"), "/admin")
        return redirect(f"{MEMBER_SITE_URL}/dashboard?next={urllib.parse.quote(target)}")

    if not _oauth_ready():
        return redirect("/?login=unavailable")
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "prompt": "consent",
    }
    # The redirect_uri is fixed in the Discord app settings, so where to land
    # afterwards rides along in `state` and comes back untouched.
    target = request.args.get("next")
    if target:
        params["state"] = _safe_next(target)
    return redirect(f"{DISCORD_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}")


@app.route("/auth/discord/callback")
def auth_discord_callback():
    code = request.args.get("code")
    back = _safe_next(request.args.get("state"))
    fail = f"{back}{'&' if '?' in back else '?'}login=failed"
    if not code or not _oauth_ready():
        return redirect(fail)

    body = urllib.parse.urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }).encode()

    try:
        token_req = urllib.request.Request(
            DISCORD_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", **OAUTH_HEADERS},
        )
        with urllib.request.urlopen(token_req, timeout=15) as resp:
            access_token = json.loads(resp.read().decode()).get("access_token")
    except urllib.error.HTTPError as exc:
        print(f"[login] token exchange failed: {exc} {_http_body(exc)}")
        return redirect(fail)
    except Exception as exc:
        print(f"[login] token exchange failed: {exc}")
        return redirect(fail)

    if not access_token:
        return redirect(fail)

    try:
        me_req = urllib.request.Request(
            DISCORD_ME_URL, headers={"Authorization": f"Bearer {access_token}", **OAUTH_HEADERS}
        )
        with urllib.request.urlopen(me_req, timeout=15) as resp:
            user = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"[login] user lookup failed: {exc} {_http_body(exc)}")
        return redirect(fail)
    except Exception as exc:
        print(f"[login] user lookup failed: {exc}")
        return redirect(fail)

    user_id = str(user.get("id") or "")
    if not user_id:
        return redirect(fail)

    # Owner mode follows the account, not the browser. A different account
    # signing in here never inherits an unlock; when the key was entered while
    # signed out, the account that signs in next takes it over.
    if session.get("owner_ok"):
        held = str(session.get("owner_ok_for") or "")
        if held and held != user_id:
            session.pop("owner_ok", None)
            session.pop("owner_ok_for", None)
        else:
            session["owner_ok_for"] = user_id

    session["member"] = {
        "user_id": user_id,
        "username": user.get("username") or "",
        "display_name": user.get("global_name") or user.get("username") or "",
        "avatar_url": _avatar_url(user),
    }
    return redirect(f"{back}{'&' if '?' in back else '?'}login=ok")


@app.route("/auth/logout")
def auth_logout():
    """Drop the session, then land wherever the caller asked to go.

    The admin panel signs out to `/admin` and the member page to `/`, so the
    destination rides along — validated as a local path, never an open
    redirect.
    """
    session.clear()
    return redirect(_safe_next(request.args.get("next"), "/"))


# ---------------------------------------------------------------------------
# Handoff from the member site
# ---------------------------------------------------------------------------

PANEL_HANDOFF_SECRET = (os.getenv("PANEL_HANDOFF_SECRET") or "").strip()


def _handoff_signature(uid: str, expires: str) -> str:
    import hashlib
    import hmac

    message = f"{uid}.{expires}".encode("utf-8")
    return hmac.new(PANEL_HANDOFF_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


@app.route("/auth/handoff")
def auth_handoff():
    """Accept an already-signed-in member coming from the member site.

    The member site authenticates people with its own signed cookie, so without
    this they would have to authorise with Discord a second time just to open
    the panel. Instead the Worker signs `uid.expires` with a secret both sides
    hold, and the panel trusts that signature rather than running OAuth again.

    A missing secret, a bad signature or an expired stamp all fall through to
    the normal Discord login — never an error page, and never a session.
    """
    back = _safe_next(request.args.get("next"), "/admin")
    login = f"{back}{'&' if '?' in back else '?'}login=failed"

    if not PANEL_HANDOFF_SECRET:
        return redirect(login)

    uid = str(request.args.get("uid") or "").strip()
    expires = str(request.args.get("expires") or "").strip()
    signature = str(request.args.get("sig") or "").strip()
    if not uid.isdigit() or not expires.isdigit() or not signature:
        return redirect(login)

    import hmac as _hmac

    if not _hmac.compare_digest(signature, _handoff_signature(uid, expires)):
        print(f"[handoff] rejected a bad signature for {uid}")
        return redirect(login)

    if time.time() > int(expires):
        print(f"[handoff] rejected an expired handoff for {uid}")
        return redirect(login)

    row = None
    try:
        row = fetch_member(uid)
    except Exception:
        row = None

    session["member"] = {
        "user_id": uid,
        "username": (row or {}).get("username") or "",
        "display_name": (row or {}).get("display_name") or (row or {}).get("username") or "",
        "avatar_url": (row or {}).get("avatar_url") or "",
    }
    return redirect(f"{back}{'&' if '?' in back else '?'}login=ok")


@app.route("/api/me", methods=["GET"])
def api_me():
    member = _current_member()
    open_count = 0
    if member:
        with TICKET_LOCK:
            rows = _load_tickets()
        open_count = sum(
            1 for t in rows
            if str(t.get("user_id")) == member["user_id"] and t.get("status") != "closed"
        )
    # Cache only: this is the member page's own hot path, so it must never wait
    # on a Discord round-trip just to draw a badge.
    ctx = _member_context(member.get("user_id"), deep=False) if member else {"role_ids": [], "role_names": []}
    return jsonify({
        "status": "ok",
        "oauth_ready": _oauth_ready(),
        "logged_in": bool(member),
        "user": member or None,
        "open_count": open_count,
        "staff_title": _member_title(ctx["role_ids"], ctx["role_names"]) if member else None,
    })


# ==========================================================
# STATS / TELEMETRY
# ==========================================================

@app.route("/api/stats", methods=["GET"])
def api_stats():
    # `bot.latency` is NaN before the first heartbeat and `inf` during the
    # initial handshake — `round(inf)` raises OverflowError, so both have to be
    # filtered out, not just NaN.
    ping_val = 0 if (math.isnan(bot.latency) or math.isinf(bot.latency)) else round(bot.latency * 1000)
    user_str = str(bot.user) if bot.user else "Connecting..."
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    proc = psutil.Process(os.getpid())
    try:
        proc_mem = round(proc.memory_info().rss / 1024 / 1024, 1)
        proc_cpu = round(proc.cpu_percent(interval=None), 1)
    except Exception:
        proc_mem, proc_cpu = 0, 0
    uptime_secs = int(time.time() - BOT_START)
    uptime_str = f"{uptime_secs // 3600}h {(uptime_secs % 3600) // 60}m {uptime_secs % 60}s"
    member_total = sum(g.member_count or 0 for g in bot.guilds)
    return jsonify({
        "cpu": psutil.cpu_percent(),
        "ram": vm.percent,
        "guilds": len(bot.guilds),
        "ping": ping_val,
        "user": user_str,
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_used_gb": round(vm.used / 1024 ** 3, 2),
        "ram_total_gb": round(vm.total / 1024 ** 3, 2),
        "member_total": member_total,
        "uptime": uptime_str,
        "os": f"{platform.system()} {platform.release()}",
        "hostname": platform.node(),
        "pid": os.getpid(),
        "proc_mem_mb": proc_mem,
        "proc_cpu": proc_cpu,
        "net_sent_mb": round(net.bytes_sent / 1024 / 1024, 2),
        "net_recv_mb": round(net.bytes_recv / 1024 / 1024, 2),
    })


# ==========================================================
# GUILDS / CHANNELS / USERS / ROLES
# ==========================================================

def _guild_panel_access(guild, actor_id, perms, owner=False):
    """Whether one account may administer one guild, and what it may do there.

    Both halves have to hold: the account must actually be a member of *that*
    guild (checked against the live member list, never another server's role
    table), and a role it holds *there* must grant panel access. The master key
    lifts it, because the key is the recovery hatch. Returns (ok, why, access).
    """
    if owner:
        return True, "owner", None
    uid = str(actor_id or "")
    if not uid.isdigit() or guild is None:
        return False, "not_signed_in", None
    gm = guild.get_member(int(uid))
    if gm is None:
        return False, "not_in_guild", None
    try:
        roles = sorted((r for r in gm.roles if not r.is_default()),
                       key=lambda r: r.position, reverse=True)
    except Exception:
        roles = []
    if not roles:
        return False, "no_roles", None
    labels = {str(r.id): r.name for r in roles}
    access = _access_for_roles([str(r.id) for r in roles], perms, labels, guild=guild)
    if not access.get("allowed"):
        return False, "no_permissions", access
    return True, "", access


@app.route("/api/channels", methods=["GET"])
def api_channels():
    """Only the servers this account can actually administer.

    A guild is left out of the payload entirely when the account is not in it
    or holds no role there that grants access, so an inaccessible server never
    even shows up in the picker.
    """
    member = _current_member()
    actor_id = str((member or {}).get("user_id") or "")
    perms = _load_dash_perms()
    owner = _owner_unlocked()
    payload = []
    for guild in bot.guilds:
        ok, _why, _access = _guild_panel_access(guild, actor_id, perms, owner)
        if not ok:
            continue
        channels_list = []
        # guild.channels is already in position order, so the dashboard tree can
        # group by category and keep Discord's ordering. Categories themselves
        # are skipped; only their name is reported on each child channel.
        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel):
                continue
            if isinstance(channel, discord.TextChannel):
                ch_type = "announcement" if channel.is_news() else "text"
            elif isinstance(channel, discord.VoiceChannel):
                ch_type = "voice"
            else:
                continue
            channels_list.append({
                "id": str(channel.id),
                "name": channel.name,
                "type": ch_type,
                "category": channel.category.name if channel.category else None,
            })
        payload.append({
            "guild_id": str(guild.id),
            "guild_name": guild.name,
            "icon": str(guild.icon.url) if guild.icon else "",
            "member_count": guild.member_count or 0,
            "role_count": len(guild.roles),
            "channels": channels_list
        })
    return jsonify(payload)


@app.route("/api/guild/<guild_id>/users", methods=["GET"])
def api_guild_users(guild_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify([])
    users = []
    for m in guild.members[:500]:
        users.append({
            "id": str(m.id),
            "name": str(m),
            "avatar": str(m.display_avatar.url) if m.display_avatar else ""
        })
    return jsonify(users)


@app.route("/api/guild/<guild_id>/roles", methods=["GET"])
def api_guild_roles(guild_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return _fail("Guild not found.", 404)
    bot_top = guild.me.top_role.position if guild.me else 0
    limits = _actor_limits(guild)
    roles = [
        _serialize_role(r, bot_top, limits)
        for r in sorted(guild.roles, key=lambda x: x.position, reverse=True)
    ]
    return jsonify({
        "roles": roles,
        "bot_top_position": bot_top,
        "can_manage": bool(guild.me and guild.me.guild_permissions.manage_roles),
        "member_count": guild.member_count or 0,
        # Who is asking and what they may touch — the role manager greys out
        # everything above this and locks the switches it cannot grant.
        "actor": limits,
        "role_perms": DASH_ROLE_PERMS,
    })


# ==========================================================
# BROADCAST / DISPATCH
# ==========================================================

@app.route("/api/dispatch", methods=["POST"])
def api_dispatch():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    content = data.get("content", "")
    is_embed = data.get("is_embed", False)
    tts = data.get("tts", False)
    delete_after = data.get("delete_after")
    channel_delay = data.get("channel_delay", 0) / 1000.0

    embed = None
    if is_embed:
        title = data.get("embed_title", "")
        color_hex = (data.get("embed_color", "#5865F2") or "#5865F2").replace("#", "")
        try:
            color_int = int(color_hex, 16)
        except ValueError:
            color_int = 0x5865F2
        embed = discord.Embed(title=title or None, description=content, color=color_int)
        author = data.get("embed_author")
        if author:
            embed.set_author(name=author)
        if data.get("embed_thumbnail"):
            embed.set_thumbnail(url=data["embed_thumbnail"])
        if data.get("embed_image"):
            embed.set_image(url=data["embed_image"])
        if data.get("embed_footer"):
            embed.set_footer(text=data["embed_footer"])
        if data.get("embed_timestamp"):
            embed.timestamp = discord.utils.utcnow()
        for field in data.get("embed_fields") or []:
            name = (field.get("name") or "").strip()
            value = (field.get("value") or "").strip()
            if name and value:
                embed.add_field(name=name[:256], value=value[:1024], inline=bool(field.get("inline")))

    async def do_send():
        sent = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if not ch:
                continue
            try:
                kwargs = {"tts": tts}
                if delete_after:
                    kwargs["delete_after"] = delete_after
                if is_embed and embed:
                    await ch.send(embed=embed, **kwargs)
                else:
                    await ch.send(content, **kwargs)
                sent += 1
                if channel_delay > 0:
                    await asyncio.sleep(channel_delay)
            except Exception:
                pass
        return sent

    try:
        sent = asyncio.run_coroutine_threadsafe(do_send(), bot.loop).result(timeout=30)
    except Exception:
        sent = 0
    if sent > 0:
        return jsonify({"status": "success", "message": f"Transmitted to {sent} channel(s)."})
    return jsonify({"status": "error", "message": "Failed to send to any channel."})


# ==========================================================
# MODERATION (channels)
# ==========================================================

@app.route("/api/purge", methods=["POST"])
def api_purge():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    amount = data.get("amount", 10)
    bots_only = data.get("bots_only", False)

    async def do_purge():
        total = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if not ch:
                continue
            try:
                def check(m):
                    return m.author.bot if bots_only else True
                deleted = await ch.purge(limit=amount, check=check)
                total += len(deleted)
            except Exception:
                pass
        return total

    try:
        total = asyncio.run_coroutine_threadsafe(do_purge(), bot.loop).result(timeout=30)
    except Exception:
        total = 0
    return jsonify({"status": "success", "message": f"Purged {total} message(s)."})


@app.route("/api/slowmode", methods=["POST"])
def api_slowmode():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    seconds = data.get("seconds", 0)

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    await ch.edit(slowmode_delay=seconds)
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Slowmode updated in {n} channel(s)."})


@app.route("/api/lockdown", methods=["POST"])
def api_lockdown():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])

    # Flipping every channel at once is a mass action, so it waits its turn.
    guard = _mass_cooldown_guard("lock_all")
    if guard is not None:
        return guard

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if not ch:
                continue
            try:
                ow = ch.overwrites_for(ch.guild.default_role)
                ow.send_messages = False if ow.send_messages is not False else None
                await ch.set_permissions(ch.guild.default_role, overwrite=ow)
                n += 1
            except Exception:
                pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    _mass_cooldown_record("lock_all")
    return jsonify({"status": "success", "message": f"Toggled lockdown in {n} channel(s)."})


@app.route("/api/rename", methods=["POST"])
def api_rename():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    name = data.get("name", "")

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    await ch.edit(name=name)
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Renamed {n} channel(s)."})


@app.route("/api/topic", methods=["POST"])
def api_topic():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    topic = data.get("topic", "")

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch and isinstance(ch, discord.TextChannel):
                try:
                    await ch.edit(topic=topic)
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Topic updated in {n} channel(s)."})


@app.route("/api/nsfw", methods=["POST"])
def api_nsfw():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    state = data.get("state", False)

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch and isinstance(ch, discord.TextChannel):
                try:
                    await ch.edit(nsfw=state)
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"NSFW set in {n} channel(s)."})


@app.route("/api/archive", methods=["POST"])
def api_archive():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    archive = data.get("archive", True)

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.edit(archived=archive)
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Archive state updated in {n} channel(s)."})


@app.route("/api/clone", methods=["POST"])
def api_clone():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    await ch.clone()
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Cloned {n} channel(s)."})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    await ch.delete()
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Deleted {n} channel(s)."})


@app.route("/api/permissions", methods=["POST"])
def api_permissions():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    role_id = int(data.get("role_id", 0))
    perm = data.get("permission", "send_messages")
    state = data.get("state", "allow")

    state_val = {"allow": True, "deny": False, "neutral": None}[state]

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if not ch:
                continue
            target = ch.guild.get_role(role_id) or ch.guild.get_member(role_id)
            if not target:
                continue
            try:
                ow = ch.overwrites_for(target)
                setattr(ow, perm, state_val)
                await ch.set_permissions(target, overwrite=ow)
                n += 1
            except Exception:
                pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Permissions applied to {n} channel(s)."})


# ==========================================================
# MEMBERS
# ==========================================================

@app.route("/api/member/ban", methods=["POST"])
def api_member_ban():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    try:
        user = discord.Object(id=int(data.get("user_id", 0)))
    except Exception:
        return jsonify({"status": "error", "message": "Invalid user ID."})

    ok, why = _touch_guard(guild, int(data.get("user_id", 0)))
    if not ok:
        return jsonify({"status": "error", "message": why})

    async def do():
        await guild.ban(user, reason=data.get("reason") or None, delete_message_days=data.get("delete_days", 0))
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": f"Banned {data.get('user_id')}."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Ban failed: {e}"})


@app.route("/api/member/kick", methods=["POST"])
def api_member_kick():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    member = guild.get_member(int(data.get("user_id", 0)))
    if not member:
        return jsonify({"status": "error", "message": "Member not found."})

    ok, why = _touch_guard(guild, member.id)
    if not ok:
        return jsonify({"status": "error", "message": why})

    async def do():
        await member.kick(reason=data.get("reason") or None)
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": f"Kicked {member}."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Kick failed: {e}"})


@app.route("/api/member/timeout", methods=["POST"])
def api_member_timeout():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    member = guild.get_member(int(data.get("user_id", 0)))
    if not member:
        return jsonify({"status": "error", "message": "Member not found."})
    ok, why = _touch_guard(guild, member.id)
    if not ok:
        return jsonify({"status": "error", "message": why})
    seconds = int(data.get("seconds", 0))
    until = None if seconds == 0 else discord.utils.utcnow() + datetime.timedelta(seconds=seconds)

    async def do():
        await member.timeout(until)
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        msg = "Timeout removed." if seconds == 0 else f"Timed out for {seconds}s."
        return jsonify({"status": "success", "message": msg})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Timeout failed: {e}"})


@app.route("/api/member/dm", methods=["POST"])
def api_member_dm():
    data = request.json or {}
    user_id = int(data.get("user_id", 0))
    content = data.get("content", "")
    if not content:
        return jsonify({"status": "error", "message": "Empty message."})

    async def do():
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(content)
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": "DM sent."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"DM failed: {e}"})


# ==========================================================
# MOD VIEW — one member, one action, authorised against the actor.
# ==========================================================

# Flags that make an account dangerous to touch from below. A plain member and
# a moderator both carry @everyone basics, so comparing *all* permissions would
# block perfectly ordinary work; comparing only these keeps Discord's rule and
# adds the part it leaves to you: you do not get to act on someone who holds
# more power than you do, even if their role happens to sit lower.
ELEVATED_PERMS = (
    "administrator", "manage_guild", "view_audit_log", "manage_roles",
    "manage_channels", "manage_webhooks", "manage_messages", "mention_everyone",
    "kick_members", "ban_members", "moderate_members", "manage_nicknames",
)


def _permission_extras(actor, target):
    """Elevated flags the target holds that the actor does not."""
    try:
        actor_perms = {name for name, value in actor.guild_permissions if value}
        target_perms = {name for name, value in target.guild_permissions if value}
    except Exception:
        return []
    return sorted(p for p in (target_perms - actor_perms) if p in ELEVATED_PERMS)


def can_act_on(guild, actor_id, uid, owner=False):
    """May this account act on that member at all? Returns (ok, why).

    The same wall Discord draws (nobody at or above your top role, nobody at or
    above the bot's) plus an explicit permission comparison, so a lower-ranked
    role cannot punish someone who outranks them by role *or* by what their
    permissions actually let them do. The master key lifts the role wall, but
    the bot's own ceiling is always real.
    """
    if guild is None:
        return False, "Guild not found."
    target = guild.get_member(int(uid))
    if target is None:
        # Not in the guild: there is no standing to compare, so this is not the
        # rule that should block a ban-by-id. The caller decides from here.
        return True, ""
    if target.id == guild.owner_id:
        return False, "That account owns the server."
    actor = guild.get_member(int(actor_id)) if str(actor_id or "").isdigit() else None
    if actor is None:
        return (True, "") if owner else (False, "Log in with Discord first.")
    if actor.id == target.id:
        return True, ""
    if not owner:
        if target.top_role >= actor.top_role:
            return False, f"'{target.top_role.name}' is at or above your highest role."
        extras = _permission_extras(actor, target)
        if extras:
            return False, f"They hold permissions you do not ({', '.join(extras[:3])})."
    if guild.me is not None and target.top_role >= guild.me.top_role:
        return False, f"'{target.top_role.name}' is at or above the bot's own role."
    return True, ""


def _touch_guard(guild, uid):
    """`can_act_on` for the endpoints that never needed to know the actor."""
    actor = _current_member()
    actor_id = str((actor or {}).get("user_id") or "")
    return can_act_on(guild, actor_id, uid, _owner_unlocked())

# What each action calls itself, and the Discord permission flag it needs on
# top of panel access. Banning without `ban_members` is refused here, not just
# hidden in the page.
MOD_ACTIONS = {
    "warn": {"label": "warn", "perm": None, "cap": "member_mod", "tracked": "warn"},
    "timeout": {"label": "timeout", "perm": "moderate_members", "cap": "member_mod", "tracked": "timeout"},
    "untimeout": {"label": "clear timeout", "perm": "moderate_members", "cap": "member_mod", "tracked": None},
    "purge": {"label": "purge messages", "perm": "manage_messages", "cap": "purge", "tracked": None},
    "role_lock": {"label": "role lock", "perm": "manage_roles", "cap": "member_mod", "tracked": None},
    "role_unlock": {"label": "role unlock", "perm": "manage_roles", "cap": "member_mod", "tracked": None},
    "kick": {"label": "kick", "perm": "kick_members", "cap": "member_mod", "tracked": "kick"},
    "softban": {"label": "softban", "perm": "ban_members", "cap": "member_mod", "tracked": "softban"},
    "ban": {"label": "ban", "perm": "ban_members", "cap": "member_mod", "tracked": "ban"},
    "hardban": {"label": "hardban", "perm": "ban_members", "cap": "member_mod", "tracked": "ban"},
    "unban": {"label": "unban", "perm": "ban_members", "cap": "member_mod", "tracked": None},
}


_HERE = os.path.dirname(os.path.abspath(__file__))
ROLE_LOCK_FILE = os.path.join(_HERE, "..", "logging", "role_locks.json")
ROLE_LOCK_LOCK = threading.Lock()


def _load_role_locks():
    try:
        with open(ROLE_LOCK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_role_locks(data):
    try:
        with ROLE_LOCK_LOCK, open(ROLE_LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _mod_action_state(guild, uid):
    """What this account may actually run on this member right now.

    The panel renders its punishment list from this, so an option that would be
    refused is never offered in the first place.
    """
    actor = _current_member()
    actor_id = str((actor or {}).get("user_id") or "")
    owner = _owner_unlocked()
    access = None
    if not owner:
        ok, _why, access = _guild_panel_access(guild, actor_id, _load_dash_perms())
        if not ok:
            return None
    caps = None if owner else ((access or {}).get("caps") or [])
    actor_member = guild.get_member(int(actor_id)) if actor_id.isdigit() else None
    target = guild.get_member(uid)
    treatable, treat_why = can_act_on(guild, actor_id, uid, owner)
    out = {}
    for name, spec in MOD_ACTIONS.items():
        allowed, why = True, ""
        if not owner:
            if not treatable:
                allowed, why = False, treat_why
            elif spec["cap"] not in caps:
                allowed, why = False, "Your dashboard roles do not include this."
            elif spec["perm"] and not getattr(actor_member.guild_permissions, spec["perm"], False):
                allowed, why = False, f"Needs the {spec['perm'].replace('_', ' ')} permission."
        else:
            allowed, why = can_act_on(guild, actor_id, uid, True)
        out[name] = {"allowed": allowed, "reason": why}
    return out


@app.route("/api/guild/<guild_id>/mod/<user_id>/actions", methods=["GET"])
def api_guild_mod_actions(guild_id, user_id):
    """Which punishments this account may hand to this member, and why not."""
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    uid_raw = str(user_id or "")
    if not uid_raw.isdigit():
        return _fail("Invalid user ID.")
    states = _mod_action_state(guild, int(uid_raw))
    if states is None:
        return _fail("Your roles do not grant access to this server.", 403)
    member = guild.get_member(int(uid_raw))
    touch_ok, touch_why = can_act_on(guild, str((_current_member() or {}).get("user_id") or ""),
                                     int(uid_raw), _owner_unlocked())
    tracked = history_tracker.get(guild.id, int(uid_raw)) or {}
    return jsonify({
        "status": "ok",
        "actions": states,
        "in_guild": member is not None,
        "can_touch": touch_ok,
        "touch_reason": touch_why,
        "banned": False if not member else False,
        "roles": [
            {"id": str(r.id), "name": r.name, "color": _color_hex(r.color), "position": r.position}
            for r in sorted(getattr(member, "roles", []) or [], key=lambda r: r.position, reverse=True)
            if not r.is_default()
        ] if member else [],
        "tracked": {
            "warns": tracked.get("warns", 0), "kicks": tracked.get("kicks", 0),
            "bans": tracked.get("bans", 0), "softbans": tracked.get("softbans", 0),
            "timeouts": tracked.get("timeouts", 0),
        },
        "locked_roles": len(_load_role_locks().get(f"{guild.id}:{int(uid_raw)}") or []),
        "channels": [
            {"id": str(c.id), "name": c.name}
            for c in guild.text_channels
        ],
    })


@app.route("/api/guild/<guild_id>/mod", methods=["POST"])
def api_guild_mod(guild_id):
    """Run one moderation action, authorised against the actor who asked.

    Four walls, checked here rather than in the page: panel access for *this*
    guild, the capability the action needs, Discord's own permission flag, and
    the role hierarchy. The master key is the one deliberate bypass, and even
    then Discord still gets the final vote.
    """
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)

    data = request.json or {}
    action = str(data.get("action") or "").strip().lower()
    if action not in MOD_ACTIONS:
        return _fail("Unknown moderation action.")
    spec = MOD_ACTIONS[action]
    reason = (str(data.get("reason") or "").strip() or "No reason provided")[:400]
    note = str(data.get("note") or "").strip()[:400]
    uid_raw = str(data.get("user_id") or "")
    if not uid_raw.isdigit():
        return _fail("Invalid user ID.")
    uid = int(uid_raw)

    actor = _current_member()
    actor_id = str((actor or {}).get("user_id") or "")
    owner = _owner_unlocked()
    if not owner:
        if not actor_id.isdigit():
            return _fail("Log in with Discord first.", 403)
        ok, why, access = _guild_panel_access(guild, actor_id, _load_dash_perms())
        if not ok:
            return _fail({
                "not_in_guild": "You are not a member of that server.",
                "no_roles": "You hold no roles in that server.",
                "no_permissions": "Your roles there do not grant dashboard access.",
            }.get(why, "Your roles do not grant access to this server."), 403)
        if spec["cap"] not in ((access or {}).get("caps") or []):
            return _fail("Your dashboard roles do not include member actions.", 403)

    actor_member = guild.get_member(int(actor_id)) if actor_id.isdigit() else None
    if not owner and spec["perm"]:
        if actor_member is None or not getattr(actor_member.guild_permissions, spec["perm"], False):
            return _fail(f"Your Discord roles do not include the {spec['perm'].replace('_', ' ')} permission.", 403)

    member = guild.get_member(uid)
    if action not in ("unban",) and member is None:
        return _fail("That member is not in this server.", 404)
    # One shared wall for every endpoint that acts on a member.
    touch_ok, touch_why = can_act_on(guild, actor_id, uid, owner)
    if not touch_ok:
        return _fail(touch_why, 403)

    moderator_id = int(actor_id) if actor_id.isdigit() else (bot.user.id if bot and bot.user else 0)
    notify = bool(data.get("notify"))
    who = str(member) if member is not None else f"ID {uid}"
    state = {"summary": "", "tracked": None, "case_id": None}
    punishment = action in ("kick", "ban", "softban", "hardban")

    async def do():
        # The case comes first: its id goes into the DM, the history entry and
        # the reply, so all three agree on one handle the member can appeal.
        case = None
        if punishment and member is not None:
            case = record_case(guild, member, action, reason, moderator_id, {"notes": note})
            state["case_id"] = case["id"]

        # The explanation goes out first, while the bot and the member still
        # share a guild — a ban would otherwise cut off the DM.
        if notify and member is not None and punishment:
            await send_punishment_dm(member, guild, action, reason, state["case_id"], moderator_id,
                                     {"moderator_tag": (case or {}).get("moderator_tag")})

        if action == "warn":
            state["tracked"] = "warn"
            state["summary"] = f"Warned {who}."
        elif action in ("timeout", "untimeout"):
            seconds = _clamp_int(data.get("duration") or data.get("seconds"), 0, 2419200, 600)
            until = None if action == "untimeout" or seconds == 0 else discord.utils.utcnow() + datetime.timedelta(seconds=seconds)
            await member.timeout(until, reason=reason)
            if action == "untimeout":
                state["summary"] = f"Cleared the timeout on {who}."
            else:
                state["tracked"] = "timeout"
                state["summary"] = f"Timed out {who} for {seconds // 60} minute(s)."
        elif action == "purge":
            channel = guild.get_channel(int(data.get("channel_id") or 0))
            if channel is None:
                raise ValueError("Pick a channel to purge from.")
            limit = _clamp_int(data.get("purge_count"), 1, 500, 50)
            deleted = await channel.purge(limit=limit, check=lambda m: m.author.id == uid, reason=reason)
            state["summary"] = f"Deleted {len(deleted)} message(s) by {who} in #{channel.name}."
        elif action == "role_lock":
            bot_top = guild.me.top_role.position if guild.me else 0
            removable = [r for r in member.roles
                         if (not r.is_default()) and (not r.managed) and r.position < bot_top]
            if not removable:
                raise ValueError("Nothing to lock — no removable roles on that member.")
            await member.remove_roles(*removable, reason=reason)
            locks = _load_role_locks()
            locks[f"{guild.id}:{uid}"] = [str(r.id) for r in removable]
            _save_role_locks(locks)
            state["summary"] = f"Locked {who} down — removed {len(removable)} role(s)."
        elif action == "role_unlock":
            locks = _load_role_locks()
            ids = locks.get(f"{guild.id}:{uid}") or []
            roles = [r for r in (guild.get_role(int(i)) for i in ids) if r is not None]
            if not roles:
                raise ValueError("Nothing stored to restore for that member.")
            await member.add_roles(*roles, reason=reason)
            locks.pop(f"{guild.id}:{uid}", None)
            _save_role_locks(locks)
            state["summary"] = f"Restored {len(roles)} role(s) to {who}."
        elif action == "kick":
            await member.kick(reason=reason)
            state["tracked"] = "kick"
            state["summary"] = f"Kicked {who}."
        elif action in ("softban", "ban", "hardban"):
            days = _clamp_int(data.get("delete_message_days"), 0, 7, 0)
            if action == "hardban":
                days = 7
            await guild.ban(discord.Object(id=uid), reason=reason, delete_message_seconds=days * 86400)
            if action == "softban":
                await guild.unban(discord.Object(id=uid), reason="Softban — messages purged")
                state["tracked"] = "softban"
                state["summary"] = f"Softbanned {who} and purged their messages."
            elif action == "hardban":
                state["tracked"] = "ban"
                state["summary"] = f"Hardbanned {who} — messages from the last 7 days purged."
            else:
                state["tracked"] = "ban"
                state["summary"] = f"Banned {who}."
        elif action == "unban":
            await guild.unban(discord.Object(id=uid), reason=reason)
            state["summary"] = f"Lifted the ban on {who}."

        # The generic one-liner is for everything that is not a removal; kick
        # and ban sent the formatted embed (with the appeal link) up top.
        if notify and member is not None and not punishment:
            try:
                await member.send(f"**{spec['label'].capitalize()} — {guild.name}**\n{reason}")
            except Exception:
                pass

        # The summary names the case so a moderator can quote it right away.
        if state["case_id"]:
            state["summary"] = f"{state['summary']} Case {state['case_id']}."

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=25)
    except Exception as e:
        low = str(e).lower()
        if isinstance(e, ValueError):
            return _fail(str(e))
        if "50013" in low or "forbidden" in low or "permission" in low:
            return _fail("Discord refused that action — the bot's own role is not high enough for it.")
        return _fail(f"{spec['label'].capitalize()} failed: {e}")

    if state["tracked"]:
        entry_reason = f"{reason} — {note}" if note else reason
        try:
            history_tracker.record(guild.id, uid, state["tracked"], moderator_id, entry_reason,
                                   case_id=state["case_id"])
        except Exception:
            pass

    return jsonify({"status": "success", "message": state["summary"], "action": action,
                    "case_id": state["case_id"]})


# ==========================================================
# CASES
# ==========================================================

def _case_party(guild, user_id, fallback_tag=""):
  """Profile snapshot for one side of a case: member, moderator or offender.

  A banned member has left the guild, so this falls back to Discord's cached
  user object and finally to the name the case recorded at punishment time —
  an old case still resolves to a human name instead of a bare id.
  """
  uid = str(user_id or "").strip()
  card = {
      "id": uid,
      "name": str(fallback_tag or (f"ID {uid}" if uid else "unknown")),
      "avatar": "",
      "in_guild": False,
      "roles": [],
  }
  if not uid.isdigit():
      return card
  member = guild.get_member(int(uid)) if guild is not None else None
  user = None
  if member is None and bot is not None:
      try:
          user = bot.get_user(int(uid))
      except Exception:
          user = None
  target = member or user
  if member is not None:
      card["in_guild"] = True
      card["name"] = str(member)
      # getattr, not member.roles: the cache can hand back a plain User when
      # only the account is known, and that carries no roles at all.
      owned = getattr(member, "roles", None) or []
      card["roles"] = [
          r.name for r in sorted((r for r in owned if not r.is_default()),
                                 key=lambda r: r.position, reverse=True)
      ][:6]
  if target is not None:
      avatar = getattr(target, "display_avatar", None)
      card["avatar"] = str(avatar.url) if avatar else ""
      if card["name"].startswith("ID "):
          card["name"] = (getattr(target, "display_name", None)
                          or getattr(target, "name", None) or card["name"])
  return card


def _case_payload(guild, case):
  """One case with both people resolved, ready for the dashboard modal."""
  item = dict(case)
  item["user"] = _case_party(guild, case.get("user_id"), case.get("user_tag"))
  item["moderator"] = _case_party(guild, case.get("moderator_id"), case.get("moderator_tag"))
  item["action_label"] = punishment_label(case.get("action"))
  return item


@app.route("/api/cases/<guild_id>/<case_id>", methods=["GET"])
def api_case_get(guild_id, case_id):
    """One case by id, with whoever is still around resolved to a name."""
    gate = _require_access()
    if gate is not None:
        return gate
    case = case_store.get(case_id)
    if not case:
        return _fail(f"No case {case_id} on record.", 404)
    if str(case.get("guild_id") or "") != str(guild_id):
        return _fail("That case belongs to a different server.", 404)
    guild = bot.get_guild(int(guild_id)) if bot else None
    if guild is None:
        return _fail("Guild not found.", 404)
    return jsonify({"status": "ok", "case": _case_payload(guild, case)})


@app.route("/api/my-cases", methods=["GET"])
def api_my_cases():
    """The signed-in member's own cases, newest first.

    Deliberately narrow: it answers for the account that is signed in and
    nobody else, which is why it needs no staff gate — a punished member has
    to be able to read their own case id in order to appeal it. Only the
    fields an appeal needs come back; the moderator is left out.
    """
    member = _current_member()
    uid = str((member or {}).get("user_id") or "").strip()
    if not uid.isdigit():
        return _fail("Sign in with Discord to see your cases.", 403)
    rows = [c for c in (case_store.data.get("cases") or {}).values()
            if isinstance(c, dict) and str(c.get("user_id") or "") == uid]
    rows.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)
    return jsonify({
        "status": "ok",
        "cases": [{
            "id": c.get("id"),
            "action": c.get("action"),
            "action_label": punishment_label(c.get("action")),
            "reason": c.get("reason") or "No reason recorded",
            "created_at": c.get("created_at"),
            "expires_at": c.get("expires_at"),
        } for c in rows[:12]],
    })


@app.route("/api/cases/<guild_id>/user/<user_id>", methods=["GET"])
def api_case_for_user(guild_id, user_id):
    """Every case against one member, newest first."""
    gate = _require_access()
    if gate is not None:
        return gate
    guild = bot.get_guild(int(guild_id)) if bot else None
    if guild is None:
        return _fail("Guild not found.", 404)
    rows = case_store.for_user(guild_id, user_id)
    return jsonify({
        "status": "ok",
        "cases": [_case_payload(guild, c) for c in rows],
        "user": _case_party(guild, user_id),
    })


# ==========================================================
# ROLES
# ==========================================================

@app.route("/api/role/create", methods=["POST"])
def api_role_create():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    color_hex = (data.get("color", "#5865F2") or "#5865F2").replace("#", "")
    try:
        color = discord.Color(int(color_hex, 16))
    except ValueError:
        color = discord.Color.default()

    async def do():
        return await guild.create_role(
            name=data.get("name") or "new-role",
            color=color,
            hoist=data.get("hoist", False),
            mentionable=data.get("mentionable", True),
        )

    try:
        role = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": f"Created role '{role.name}'."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Create failed: {e}"})


@app.route("/api/role/assign", methods=["POST"])
def api_role_assign():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    role = guild.get_role(int(data.get("role_id", 0)))
    member = guild.get_member(int(data.get("user_id", 0)))
    if not role or not member:
        return jsonify({"status": "error", "message": "Role or member not found."})
    action = data.get("action", "add")

    # The capability has to be granted at all before hierarchy even matters.
    if not _owner_unlocked():
        member = _current_member()
        actor_id = str((member or {}).get("user_id") or "")
        ok, why, access = _guild_panel_access(guild, actor_id, _load_dash_perms())
        if not ok:
            reason = {
                "not_signed_in": "Log in with Discord first.",
                "not_in_guild": "You are not a member of that server.",
                "no_roles": "You hold no roles in that server.",
                "no_permissions": "Your roles there do not grant dashboard access.",
            }.get(why, "Your dashboard access does not include assigning roles.")
            return jsonify({"status": "error", "message": reason})
        if "role_assign" not in ((access or {}).get("caps") or []):
            return jsonify({"status": "error",
                            "message": "Your dashboard access does not include assigning roles."})

    # Two walls: you cannot touch a member who outranks you (by role or by the
    # permissions they hold), and you cannot hand away a role that outranks you.
    member_ctx = _current_member()
    actor_id = str((member_ctx or {}).get("user_id") or "")
    touch_ok, touch_why = can_act_on(guild, actor_id, member.id, _owner_unlocked())
    if not touch_ok:
        return jsonify({"status": "error", "message": touch_why})

    limits = _actor_limits(guild)
    allowed, why = _actor_may_edit_role(limits, role)
    if not allowed:
        return jsonify({"status": "error", "message": why})

    async def do():
        if action == "add":
            await member.add_roles(role)
        else:
            await member.remove_roles(role)
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": f"{action.capitalize()}ed role '{role.name}' for {member}."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Role update failed: {e}"})


@app.route("/api/role/delete", methods=["POST"])
def api_role_delete():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    role = guild.get_role(int(data.get("role_id", 0)))
    if not role:
        return jsonify({"status": "error", "message": "Role not found."})

    limits = _actor_limits(guild)
    allowed, why = _actor_may_edit_role(limits, role)
    if not allowed:
        return jsonify({"status": "error", "message": why})
    if not limits["actions"]["delete"]:
        return jsonify({"status": "error", "message": "Your dashboard access does not include deleting roles."})

    async def do():
        await role.delete()
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": f"Deleted role '{role.name}'."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Delete failed: {e}"})


# ==========================================================
# PRESENCE
# ==========================================================

@app.route("/api/presence", methods=["POST"])
def api_presence():
    data = request.json or {}
    activity_type = data.get("type", "playing")
    activity_text = data.get("activity", "")
    status = data.get("status", "online")
    url = data.get("url") or None
    reset = data.get("reset", False)

    type_map = {
        "playing": discord.ActivityType.playing,
        "listening": discord.ActivityType.listening,
        "watching": discord.ActivityType.watching,
        "competing": discord.ActivityType.competing,
        "streaming": discord.ActivityType.streaming,
        "custom": discord.ActivityType.custom,
    }

    status_map = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "invisible": discord.Status.invisible,
    }

    if reset:
        activity = discord.Activity(type=discord.ActivityType.playing, name="")
        status_obj = discord.Status.online
    elif activity_type == "streaming":
        activity = discord.Streaming(name=activity_text or "Streaming", url=url or "https://twitch.tv/")
        status_obj = status_map.get(status, discord.Status.online)
    elif activity_type == "custom":
        activity = discord.CustomActivity(name=activity_text or None)
        status_obj = status_map.get(status, discord.Status.online)
    else:
        activity = discord.Activity(type=type_map.get(activity_type, discord.ActivityType.playing), name=activity_text or "")
        status_obj = status_map.get(status, discord.Status.online)

    asyncio.run_coroutine_threadsafe(bot.change_presence(activity=activity, status=status_obj), bot.loop)
    return jsonify({"status": "success", "message": "Presence updated."})


# ==========================================================
# SYSTEM
# ==========================================================

@app.route("/api/exec", methods=["POST"])
def api_exec():
    data = request.json or {}
    command = data.get("command")
    if not command:
        return jsonify({"status": "error", "output": "No command provided."})
    try:
        output = subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT, timeout=15).decode('utf-8', errors='replace')
        return jsonify({"status": "success", "output": output})
    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "output": f"Command failed (exit {e.returncode}):\n{e.output.decode('utf-8', errors='replace')}"})
    except Exception as e:
        return jsonify({"status": "error", "output": str(e)})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    from bot import GUILD_ID
    async def do():
        try:
            target_guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=target_guild)
            synced = await bot.tree.sync(guild=target_guild)
            return f"Synced {len(synced)} command(s) to guild {GUILD_ID}."
        except Exception as e:
            return f"Sync error: {e}"

    try:
        output = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
        return jsonify({"status": "success", "message": "Commands synced.", "output": output})
    except Exception as e:
        return jsonify({"status": "error", "message": "Sync failed.", "output": str(e)})


@app.route("/api/commands", methods=["GET"])
def api_commands():
    cmds = []
    for cmd in bot.tree.get_commands():
        cmds.append({
            "name": cmd.name,
            "description": getattr(cmd, "description", ""),
            "type": str(cmd.type) if hasattr(cmd, "type") else "unknown",
        })
    return jsonify(cmds)


# ==========================================================
# PANEL HELPERS
# ==========================================================

MEMBER_LIST_CAP = 2500
DEFAULT_ROLE_COLOR = "#99aab5"
DEFAULT_LOG_LIMIT = 400
SEARCH_RESULT_LIMIT = 300
LOG_SCAN_LIMIT = 40000


class PanelError(Exception):
    """Raised for expected failures so routes can reply without a traceback."""


def _run(coro, timeout=30):
    """Run a coroutine on the bot's event loop from a Flask worker thread."""
    if bot is None or bot.loop is None:
        raise PanelError("The bot is not connected yet.")
    return asyncio.run_coroutine_threadsafe(coro, bot.loop).result(timeout=timeout)


def _fail(message, code=400):
    return jsonify({"status": "error", "message": message}), code


def _hex_to_color(value):
    raw = str(value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", raw or ""):
        return discord.Color.default()
    return discord.Color(int(raw, 16))


def _color_hex(color):
    value = int(getattr(color, "value", color) or 0)
    if value == 0:
        return DEFAULT_ROLE_COLOR
    return f"#{value:06x}"


PERMISSION_NAMES = [name for name, _ in discord.Permissions()]
_PERMISSION_SET = set(PERMISSION_NAMES)


def _permissions_from(names):
    perms = discord.Permissions.none()
    for name in names or []:
        if name in _PERMISSION_SET:
            setattr(perms, name, True)
    return perms


# What a role's own Discord permissions imply for the dashboard. Used as the
# default for a role nobody has configured yet, so access starts from what the
# role can already do in the server instead of from "everything". Sections are
# deliberately absent: which pages a role may open stays the owner's call, only
# the abilities it can wield are derived.
DISCORD_CAP_DERIVATION = {
    "manage_roles": ["role_manager", "role_perms", "role_assign"],
    "manage_guild": ["channel_edit", "broadcast", "logs_read"],
    "manage_channels": ["channel_edit"],
    "manage_webhooks": ["webhooks"],
    "manage_messages": ["purge", "broadcast", "logs_read"],
    "manage_threads": ["purge"],
    "view_audit_log": ["logs_read"],
    "kick_members": ["member_mod"],
    "ban_members": ["member_mod"],
    "moderate_members": ["member_mod"],
    "mute_members": ["member_mod"],
    "deafen_members": ["member_mod"],
    "move_members": ["member_mod"],
    "manage_nicknames": ["notes"],
    "manage_events": ["verification"],
    "mention_everyone": ["broadcast"],
}

# Nothing a Discord permission says can hand these out: one runs shell commands
# on the host, the other is the master-key suite.
DERIVED_CAPS_NEVER = {"console", "owner_panel"}


def _derived_caps_for_role(role):
    """The capabilities a role's own Discord permissions imply, nothing more."""
    if role is None:
        return []
    try:
        held = {name for name, value in role.permissions if value}
    except Exception:
        return []
    allowed = set(_CAP_IDS) - DERIVED_CAPS_NEVER
    if "administrator" in held:
        return sorted(allowed)
    caps = set()
    for name in held:
        for cap in DISCORD_CAP_DERIVATION.get(name, []):
            if cap in allowed:
                caps.add(cap)
    return sorted(caps)


def _serialize_role(role, bot_top=0, limits=None):
    perms = {name: bool(value) for name, value in role.permissions}
    icon = getattr(role, "display_icon", None)
    actor_ok, actor_why = (True, "") if limits is None else _actor_may_edit_role(limits, role)
    return {
        # What this role would get if nobody configures it: derived from Discord,
        # capabilities only, never dashboard sections.
        "derived_caps": _derived_caps_for_role(role),
        "actor_editable": actor_ok,
        "actor_reason": actor_why,
        "id": str(role.id),
        "name": role.name,
        "color": _color_hex(role.color),
        "color_int": int(getattr(role.color, "value", 0) or 0),
        "position": role.position,
        "member_count": len(role.members),
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "managed": role.managed,
        "is_default": role.is_default(),
        "permission_count": sum(1 for value in perms.values() if value),
        "permissions": perms,
        "icon": str(icon) if icon else None,
        "editable": (not role.is_default()) and (not role.managed) and (role.position < bot_top),
    }


LOG_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+\[#(?P<channel>[^\]]+)\]\s+\(id=(?P<mid>\d+)\)\s?(?P<rest>.*)$"
)


def _parse_log_line(line):
    """Turn one logged line into a message entry, or None if unrecognised."""
    match = LOG_LINE_RE.match(line.strip())
    if not match:
        return None
    rest = match.group("rest") or ""
    kind = "message"
    if rest.startswith("[DELETED]"):
        kind = "deleted"
        rest = rest[len("[DELETED]"):].strip()
    elif rest.startswith("[EDITED]"):
        kind = "edited"
        rest = rest[len("[EDITED]"):].strip()
    original = None
    edited = None
    if kind == "edited" and "\u21d2" in rest:
        original, edited = (part.strip() for part in rest.split("\u21d2", 1))
    return {
        "timestamp": match.group("ts"),
        "channel": match.group("channel"),
        "message_id": match.group("mid"),
        "kind": kind,
        "content": rest,
        "original": original,
        "edited": edited,
    }


def _log_paths(user_id=None, guild_id=None, include_deleted=True):
    """Log files matching a user and/or guild, as (path, from_archive) pairs."""
    directories = [(MESSAGE_LOG_DIR, False)]
    if include_deleted:
        directories.append((DELETED_LOG_DIR, True))
    suffix = f"_{user_id}.txt" if user_id else ".txt"
    prefix = f"{guild_id}_" if guild_id else ""
    found = []
    for directory, archived in directories:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.endswith(suffix) and name.startswith(prefix):
                found.append((os.path.join(directory, name), archived))
    return found


def _iter_log_entries(paths, per_file_limit=LOG_SCAN_LIMIT):
    """Yield parsed entries, newest data last. Archive files mark plain lines deleted."""
    for path, archived in paths:
        guild_part, _, user_part = os.path.basename(path)[:-4].partition("_")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-per_file_limit:]
        except Exception:
            continue
        for line in lines:
            parsed = _parse_log_line(line)
            if not parsed:
                continue
            if archived and parsed["kind"] == "message":
                parsed["kind"] = "deleted"
            parsed["guild_id"] = guild_part
            parsed["user_id"] = user_part
            parsed["archive"] = archived
            yield parsed


# ==========================================================
# ROLE MANAGER
# ==========================================================

@app.route("/api/permissions", methods=["GET"])
def api_permission_names():
    return jsonify({"permissions": PERMISSION_NAMES})


@app.route("/api/guild/<guild_id>/roles/create", methods=["POST"])
def api_panel_role_create(guild_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return _fail("Guild not found.", 404)
    data = request.json or {}
    name = (data.get("name") or "").strip()[:100] or "new role"
    limits = _actor_limits(guild)
    if not limits["actions"]["create"]:
        return _fail("Your dashboard access does not include creating roles.", 403)
    # A brand new role may only carry permissions the account could hand out.
    perms, ignored = _apply_role_perm_request(limits, _PseudoRole(), data.get("permissions") or {})

    async def do():
        return await guild.create_role(
            name=name,
            color=_hex_to_color(data.get("color")),
            hoist=bool(data.get("hoist")),
            mentionable=bool(data.get("mentionable")),
            permissions=perms,
            reason="Dashboard",
        )

    try:
        role = _run(do(), 20)
    except Exception as e:
        return _fail(f"Create failed: {e}")
    bot_top = guild.me.top_role.position if guild.me else 0
    message = f"Created role '{role.name}'."
    if ignored:
        message += f" Skipped {len(ignored)} permission(s) you cannot grant: {', '.join(ignored[:4])}."
    return jsonify({"status": "success", "message": message, "role": _serialize_role(role, bot_top, limits),
                    "ignored": ignored})


@app.route("/api/guild/<guild_id>/roles/<role_id>/update", methods=["POST"])
def api_panel_role_update(guild_id, role_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return _fail("Guild not found.", 404)
    role = guild.get_role(int(role_id))
    if not role:
        return _fail("Role not found.", 404)

    limits = _actor_limits(guild)
    allowed, why = _actor_may_edit_role(limits, role)
    if not allowed:
        return _fail(why, 403)

    data = request.json or {}
    acts = limits["actions"]
    kwargs = {"reason": "Dashboard"}
    ignored = []
    if data.get("name") and not acts["rename"]:
        return _fail("Your dashboard access does not include renaming roles.", 403)
    if data.get("color") and not acts["color"]:
        return _fail("Your dashboard access does not include recolouring roles.", 403)
    if ("hoist" in data or "mentionable" in data) and not acts["rename"]:
        return _fail("Your dashboard access does not include changing role display options.", 403)
    if data.get("name"):
        kwargs["name"] = str(data["name"])[:100]
    if data.get("color"):
        kwargs["color"] = _hex_to_color(data["color"])
    if "hoist" in data:
        kwargs["hoist"] = bool(data["hoist"])
    if "mentionable" in data:
        kwargs["mentionable"] = bool(data["mentionable"])
    if "permissions" in data:
        if not acts["permissions"]:
            return _fail("Your dashboard access does not include editing permissions.", 403)
        kwargs["permissions"], ignored = _apply_role_perm_request(limits, role, data["permissions"])
    if len(kwargs) == 1:
        return _fail("Nothing to change.")

    async def do():
        return await role.edit(**kwargs)

    try:
        role = _run(do(), 20) or role
    except Exception as e:
        return _fail(f"Update failed: {e}")
    bot_top = guild.me.top_role.position if guild.me else 0
    message = f"Saved '{role.name}'."
    if ignored:
        message += f" Skipped {len(ignored)} permission(s) you cannot grant: {', '.join(ignored[:4])}."
    return jsonify({"status": "success", "message": message, "role": _serialize_role(role, bot_top),
                    "ignored": ignored})


@app.route("/api/guild/<guild_id>/roles/<role_id>/delete", methods=["POST"])
def api_panel_role_delete(guild_id, role_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return _fail("Guild not found.", 404)
    role = guild.get_role(int(role_id))
    if not role:
        return _fail("Role not found.", 404)
    if role.is_default():
        return _fail("The @everyone role cannot be deleted.")
    limits = _actor_limits(guild)
    allowed, why = _actor_may_edit_role(limits, role)
    if not allowed:
        return _fail(why, 403)
    if not limits["actions"]["delete"]:
        return _fail("Your dashboard access does not include deleting roles.", 403)
    name = role.name

    async def do():
        await role.delete(reason="Dashboard")

    try:
        _run(do(), 20)
    except Exception as e:
        return _fail(f"Delete failed: {e}")
    return jsonify({"status": "success", "message": f"Deleted role '{name}'."})


@app.route("/api/guild/<guild_id>/roles/reorder", methods=["POST"])
def api_panel_role_reorder(guild_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return _fail("Guild not found.", 404)
    order = [int(rid) for rid in (request.json or {}).get("order", [])]
    if not order:
        return _fail("No order supplied.")

    limits = _actor_limits(guild)
    if not limits["actions"]["reorder"]:
        return _fail("Your dashboard access does not include reordering roles.", 403)

    # The client sends every role from top to bottom; a role's new position is
    # its distance from the bottom. @everyone is discarded first so its slot
    # never skews the numbering. Discord rejects the whole request if it
    # includes a managed role or any role at/above the bot's own top role, so
    # those are left where they are and reported instead of being sent (which
    # used to fail the entire reorder). The signed-in account has the same
    # ceiling as the bot, so their own top role is a wall too.
    bot_top = guild.me.top_role.position if guild.me else 0
    wall = min(bot_top, int(limits["ceiling"]))
    ordered_roles = []
    for role_id in order:
        role = guild.get_role(role_id)
        if role is None or role.is_default():
            continue
        ordered_roles.append(role)

    total = len(ordered_roles)
    positions = {}
    skipped = []
    for index, role in enumerate(ordered_roles):
        desired = total - index
        if role.managed or role.position >= wall or desired >= wall:
            if role.position != desired:
                skipped.append(role.name)
            continue
        if role.position != desired:
            positions[role] = desired

    if not positions:
        message = "Roles are already in that order."
        if skipped:
            message = f"Nothing to move. Left in place: {', '.join(skipped[:5])}."
        return jsonify({"status": "success", "message": message, "roles": []})

    async def do():
        return await guild.edit_role_positions(positions=positions, reason="Dashboard reorder")

    try:
        _run(do(), 20)
    except Exception as e:
        return _fail(f"Reorder failed: {e}")
    roles = [
        _serialize_role(r, bot_top, limits)
        for r in sorted(guild.roles, key=lambda x: x.position, reverse=True)
    ]
    message = f"Moved {len(positions)} role(s)."
    if skipped:
        message += f" Left in place: {', '.join(skipped[:5])}."
    return jsonify({"status": "success", "message": message, "roles": roles})


# ==========================================================
# LOOKUP (member browser, profiles, background checks)
# ==========================================================

@app.route("/api/guild/<guild_id>/members", methods=["GET"])
def api_guild_members(guild_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return _fail("Guild not found.", 404)
    members = []
    # The staff badges the owner designed, so the member list wears the same
    # tags the member page does.
    titles = _load_titles()
    for member in guild.members[:MEMBER_LIST_CAP]:
        risk_score, risk_flags = account_risk_score(member)
        top = member.top_role
        member_roles = list(getattr(member, "roles", []) or [])
        members.append({
            "id": str(member.id),
            "name": member.name,
            "display_name": member.display_name,
            "mention": member.mention,
            "staff_title": _member_title([str(r.id) for r in member_roles],
                                         [r.name for r in member_roles], titles=titles),
            "avatar": member.display_avatar.url if member.display_avatar else "",
            "bot": member.bot,
            "joined_at": member.joined_at.isoformat() if member.joined_at else None,
            "created_at": member.created_at.isoformat() if member.created_at else None,
            "account_age_days": (discord.utils.utcnow() - member.created_at).days,
            "top_role": {"id": str(top.id), "name": top.name, "color": _color_hex(top.color)},
            "role_count": max(len(member.roles) - 1, 0),
            "timed_out": member.is_timed_out(),
            "pending": member.pending,
            "risk_score": risk_score,
            "risk_flags": risk_flags,
        })
    members.sort(key=lambda m: m["display_name"].lower())
    total = guild.member_count or len(guild.members)
    return jsonify({"members": members, "total": total, "capped": len(guild.members) > MEMBER_LIST_CAP})


def _resolve_actor(guild, user_id):
    if not user_id:
        return {"id": None, "name": "Dashboard", "avatar": ""}
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return {"id": None, "name": "Unknown", "avatar": ""}
    user = (guild.get_member(uid) if guild else None) or bot.get_user(uid)
    if not user:
        return {"id": str(uid), "name": f"ID {uid}", "avatar": ""}
    return {
        "id": str(uid),
        "name": getattr(user, "display_name", None) or user.name,
        "avatar": user.display_avatar.url if user.display_avatar else "",
    }


@app.route("/api/member/<guild_id>/<user_id>", methods=["GET"])
def api_member_profile(guild_id, user_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return _fail("Guild not found.", 404)
    uid = int(user_id)
    member = guild.get_member(uid)
    user = member or bot.get_user(uid)

    tracked = history_tracker.get(guild.id, uid)
    score, reasons = account_risk_score(member) if member else (0, [])

    profile = {
        "id": str(uid),
        "in_guild": member is not None,
        "name": user.name if user else f"ID {uid}",
        "display_name": (member.display_name if member else (user.name if user else f"ID {uid}")),
        "mention": member.mention if member else f"<@{uid}>",
        "avatar": user.display_avatar.url if user and user.display_avatar else "",
        "banner": user.banner.url if user and user.banner else "",
        "bot": bool(user.bot) if user else False,
        "created_at": user.created_at.isoformat() if user else None,
        "account_age_days": (discord.utils.utcnow() - user.created_at).days if user else None,
        "joined_at": member.joined_at.isoformat() if member and member.joined_at else None,
        "verified": bool(member and member_is_verified(member)),
        "boosting_since": member.premium_since.isoformat() if member and member.premium_since else None,
        "timed_out_until": member.timed_out_until.isoformat() if member and member.timed_out_until else None,
        "pending": bool(member.pending) if member else False,
        "status": str(member.status) if member and hasattr(member, "status") else "offline",
        "activity": None,
        "nickname": member.nick if member else None,
    }
    if member and member.activity:
        profile["activity"] = f"{member.activity.type.name.title()} {member.activity.name}"

    roles = []
    if member:
        for role in sorted(member.roles, key=lambda r: r.position, reverse=True):
            if role.is_default():
                continue
            roles.append({
                "id": str(role.id),
                "name": role.name,
                "color": _color_hex(role.color),
                "position": role.position,
                "managed": role.managed,
            })

    actions = []
    for entry in tracked.get("actions", [])[::-1]:
        actor = _resolve_actor(guild, entry.get("moderator_id"))
        actions.append({
            "action": entry.get("action", "unknown"),
            "reason": entry.get("reason") or "No reason recorded",
            "timestamp": entry.get("timestamp", ""),
            "moderator": actor,
            # Present only for punishments that opened a case, so the log can
            # offer the case behind the entry.
            "case_id": entry.get("case_id") or "",
        })

    notes = []
    for note in notes_store.list(guild.id, uid):
        notes.append({
            "text": note.get("text", ""),
            "timestamp": note.get("timestamp", ""),
            "author": _resolve_actor(guild, note.get("author_id")),
        })

    watch = watchlist.get(guild.id, uid)
    if watch:
        watch = {
            "reason": watch.get("reason", ""),
            "timestamp": watch.get("timestamp", ""),
            "author": _resolve_actor(guild, watch.get("author_id")),
        }

    log_entry = log_index.get(guild.id, uid)
    log_link = None
    if log_entry:
        log_link = f"https://discord.com/channels/{guild.id}/{log_entry['channel_id']}/{log_entry['message_id']}"

    supabase_row = None
    try:
        supabase_row = fetch_member(str(uid))
    except Exception:
        supabase_row = None

    # Whether the account asking may act on this member at all, decided by the
    # same wall the moderation endpoints use, so the profile card can say so
    # before anyone clicks a punishment and watches it fail.
    actor = _current_member()
    touch_ok, touch_why = can_act_on(guild, str((actor or {}).get("user_id") or ""),
                                     uid, _owner_unlocked())

    return jsonify({
        "profile": profile,
        "roles": roles,
        "can_touch": touch_ok,
        "touch_reason": touch_why,
        "history": {
            "kicks": tracked.get("kicks", 0),
            "bans": tracked.get("bans", 0),
            "softbans": tracked.get("softbans", 0),
            "timeouts": tracked.get("timeouts", 0),
            "warns": tracked.get("warns", 0),
            "total": len(tracked.get("actions", [])),
        },
        "actions": actions,
        "notes": notes,
        "watch": watch,
        "risk": {"score": score, "reasons": reasons},
        "log_link": log_link,
        "supabase": supabase_row,
        "thread_ids": thread_index.all_thread_ids(guild.id, uid),
    })


@app.route("/api/member/<guild_id>/<user_id>/note", methods=["POST"])
def api_member_note(guild_id, user_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return _fail("Guild not found.", 404)
    text = ((request.json or {}).get("text") or "").strip()
    if not text:
        return _fail("Note text is required.")
    total = notes_store.add(guild.id, int(user_id), 0, text)
    return jsonify({"status": "success", "message": f"Note saved ({total} on file)."})


@app.route("/api/member/<guild_id>/<user_id>/watch", methods=["POST"])
def api_member_watch(guild_id, user_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return _fail("Guild not found.", 404)
    uid = int(user_id)
    data = request.json or {}
    if data.get("remove") or data.get("action") == "remove":
        removed = watchlist.remove(guild.id, uid)
        return jsonify({"status": "success", "message": "Removed from the watchlist." if removed else "That user was not on the watchlist."})
    reason = (data.get("reason") or "").strip()
    if not reason:
        return _fail("A reason is required.")
    watchlist.add(guild.id, uid, 0, reason)
    return jsonify({"status": "success", "message": "Added to the watchlist."})


async def _verification_log_snapshot(guild, uid):
    """Rebuild the verification embed the member got when they clicked Verify.

    The bot mirrors every verification into a log channel, so reading that
    message back is the only honest source for the invite information that was
    captured at join time.
    """
    entry = log_index.get(guild.id, uid)
    if not entry:
        return None
    channel = guild.get_channel(int(entry.get("channel_id") or 0))
    if channel is None:
        return None
    try:
        message = await channel.fetch_message(int(entry.get("message_id") or 0))
    except Exception:
        return None
    if not message.embeds:
        return None
    embed = message.embeds[0]
    return {
        "title": embed.title or "Verification Log",
        "url": f"https://discord.com/channels/{guild.id}/{channel.id}/{message.id}",
        "fields": [{"name": f.name, "value": f.value} for f in embed.fields],
        "channel": getattr(channel, "name", ""),
        # When the button was pressed — the closest thing to a verification date.
        "verified_at": message.created_at.strftime("%b %d, %Y at %H:%M UTC") if message.created_at else None,
        "author": str(message.author) if message.author else "",
    }


@app.route("/api/member/<guild_id>/<user_id>/checkbg", methods=["GET", "POST"])
def api_member_checkbg(guild_id, user_id):
    """Everything the panel knows about one account, and never a dead end.

    The live half (Discord member, moderation history) and the stored half (the
    verification embed the member got) are gathered independently, and whatever
    cannot be read is reported as a note instead of failing the whole check —
    the old build answered a 500 whenever the audit-log half was unavailable,
    which the panel showed as "the background check could not be completed".
    """
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    if not str(user_id or "").isdigit():
        return _fail("That user id does not look like a Discord id.")
    uid = int(user_id)
    notes = []

    # A cold member cache is not evidence of absence: ask Discord (and then the
    # synced table) before declaring the live half missing.
    member = guild.get_member(uid)
    if member is None:
        try:
            member, asked = _fetch_guild_member(guild, str(uid))
        except Exception:
            member, asked = None, False
        if member is None and asked:
            notes.append("Discord does not list this account as a member of the server, "
                         "so the live half of this report is unavailable.")
        elif member is None:
            notes.append("The member lookup could not be verified right now; showing the stored record only.")

    live = {"tracked_kicks": 0, "tracked_bans": 0, "tracked_softbans": 0,
            "tracked_timeouts": 0, "tracked_warns": 0, "actual_ban": False, "ban_reason": None}
    if member is not None:
        try:
            live = _run(gather_moderation_history(guild, member), timeout=25)
        except PanelError as e:
            notes.append(f"Moderation history unavailable: {e}")
        except Exception as e:
            notes.append(f"Moderation history could not be read ({e}). "
                         "The bot needs View Audit Log and Ban Members for that part.")

    # The stored verification message captures what the live data no longer
    # holds: the invite that was used and when the button was pressed.
    snapshot = None
    try:
        snapshot = _run(_verification_log_snapshot(guild, uid), timeout=10)
    except Exception:
        snapshot = None
    verify_index = None
    try:
        verify_index = log_index.get(guild.id, uid)
    except Exception:
        verify_index = None
    if snapshot is None:
        if verify_index:
            notes.append("The stored verification log could not be fetched from Discord right now.")
        else:
            notes.append("No stored verification record for this account — they may have joined before the "
                         "bot mirrored verifications, or never went through the panel.")

    if member is None and not verify_index:
        return _fail("That user is not in this server and no verification record is stored for them, "
                     "so there is nothing to check.")

    if member is not None:
        score, label, color, reasons = compute_risk(member, live)
        risk_color = _color_hex(color)
        now = discord.utils.utcnow()
        account_age = (now - member.created_at).days if member.created_at else None
        join_age = (now - member.joined_at).days if member.joined_at else None
        member_bits = {
            "verified": bool(member_is_verified(member)),
            "timed_out": member.is_timed_out(),
            "pending": bool(member.pending),
            "bot": member.bot,
            "username": member.name,
            "display_name": member.display_name,
            "nickname": member.nick or "",
            "avatar": member.display_avatar.url if member.display_avatar else "",
            "top_role": member.top_role.name if member.top_role else None,
            "role_count": max(len(member.roles) - 1, 0),
            "roles": [r.name for r in member.roles if not r.is_default()][:25],
            "premium_since": member.premium_since.strftime("%b %d, %Y") if member.premium_since else None,
            "created_at": member.created_at.strftime("%b %d, %Y at %H:%M UTC") if member.created_at else "Unknown",
            "joined_at": member.joined_at.strftime("%b %d, %Y at %H:%M UTC") if member.joined_at else "Unknown",
        }
    else:
        # No live member: fall back to what the verification record remembers.
        score, label, color, reasons = 0, "Unknown", None, []
        risk_color = None
        account_age = join_age = None
        member_bits = {
            "verified": bool(verify_index), "timed_out": False, "pending": False,
            "bot": False, "username": "", "display_name": "", "nickname": "", "avatar": "",
            "top_role": None, "role_count": 0, "roles": [],
            "premium_since": None, "created_at": "Unknown", "joined_at": "Unknown",
        }

    flags = history_flags_from(live) if member is not None else []
    tracked = history_tracker.get(guild.id, uid)

    invite_text = None
    if snapshot:
        for field in snapshot["fields"]:
            if "invite" in (field.get("name") or "").lower():
                invite_text = field.get("value")
                break

    name = member_bits["display_name"] or member_bits["username"] or (snapshot or {}).get("author") or f"ID {uid}"
    return jsonify({
        "status": "success",
        "message": f"Background check complete for {name}.",
        "partial": bool(notes) and member is None,
        "notes": notes,
        "check": {
            "user_id": str(uid),
            "live": member is not None,
            "risk_score": score,
            "risk_label": label,
            "risk_color": risk_color,
            "risk_reasons": reasons,
            "risk_factors": reasons,
            "flags": flags,
            "counts": {
                "bans": live["tracked_bans"],
                "kicks": live["tracked_kicks"],
                "softbans": live["tracked_softbans"],
                "timeouts": live["tracked_timeouts"],
                "warns": live["tracked_warns"],
            },
            "currently_banned": bool(live["actual_ban"]),
            "ban_reason": live["ban_reason"],
            "verified": member_bits["verified"],
            "timed_out": member_bits["timed_out"],
            "pending": member_bits["pending"],
            "account_age_days": account_age,
            "join_age_days": join_age,
            "top_role": member_bits["top_role"],
            "role_count": member_bits["role_count"],
            "username": member_bits["username"],
            "display_name": member_bits["display_name"],
            "nickname": member_bits["nickname"],
            "avatar": member_bits["avatar"],
            "bot": member_bits["bot"],
            "roles": member_bits["roles"],
            "tracked_total": len(tracked.get("actions", [])),
            "timeline": {
                "account_created": member_bits["created_at"],
                "joined_server": member_bits["joined_at"],
                "boosting_since": member_bits["premium_since"] or "Not Boosting",
            },
            "invite_text": invite_text,
            "verify_log": snapshot,
            # The stored record survives even when the embed cannot be fetched.
            "verify_record": {
                "has_entry": bool(verify_index),
                "channel_id": str((verify_index or {}).get("channel_id") or "") or None,
                "message_id": str((verify_index or {}).get("message_id") or "") or None,
                "verified_at": (snapshot or {}).get("verified_at"),
                "author": (snapshot or {}).get("author") or "",
                "invite": invite_text,
            },
        },
    })


# ==========================================================
# MESSAGE HISTORY (rendered as a chat log)
# ==========================================================

@app.route("/api/member/<guild_id>/<user_id>/messages", methods=["GET"])
def api_member_messages(guild_id, user_id):
    try:
        uid = int(user_id)
        gid = int(guild_id)
    except (TypeError, ValueError):
        return _fail("Invalid guild or user ID.")

    kind_filter = (request.args.get("filter") or "all").lower()
    channel_filter = (request.args.get("channel") or "").strip().lower()
    text_filter = (request.args.get("q") or "").strip().lower()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    include_threads = (request.args.get("include_threads", "1") or "1").lower() not in ("0", "false", "no")
    try:
        limit = max(1, min(int(request.args.get("limit", DEFAULT_LOG_LIMIT) or DEFAULT_LOG_LIMIT), 2000))
    except ValueError:
        limit = DEFAULT_LOG_LIMIT

    # Merge local archives with the Discord threads, keyed by message ID so a
    # message logged in both places only shows once (deleted/edited wins).
    merged = {}
    local_count = 0
    for entry in _iter_log_entries(_log_paths(user_id=uid, guild_id=gid)):
        local_count += 1
        current = merged.get(entry["message_id"])
        if current is None or (current["kind"] == "message" and entry["kind"] != "message"):
            merged[entry["message_id"]] = entry

    thread_count = 0
    thread_error = None
    if include_threads:
        try:
            lines = _run(fetch_user_thread_lines(bot, gid, uid, max_lines=1200), timeout=30)
        except PanelError as e:
            lines, thread_error = [], str(e)
        except Exception as e:
            lines, thread_error = [], f"Thread fetch failed: {e}"
        for line in lines:
            entry = _parse_log_line(line)
            if not entry:
                continue
            thread_count += 1
            entry["guild_id"], entry["user_id"], entry["archive"] = str(gid), str(uid), False
            current = merged.get(entry["message_id"])
            if current is None or (current["kind"] == "message" and entry["kind"] != "message"):
                merged[entry["message_id"]] = entry

    entries = sorted(merged.values(), key=lambda e: (e["timestamp"], int(e["message_id"])))
    stats = {
        "total": len(entries),
        "deleted": sum(1 for e in entries if e["kind"] == "deleted"),
        "edited": sum(1 for e in entries if e["kind"] == "edited"),
        "first": entries[0]["timestamp"] if entries else None,
        "last": entries[-1]["timestamp"] if entries else None,
    }
    channels = sorted({e["channel"] for e in entries}, key=str.lower)

    filtered = []
    for entry in entries:
        if kind_filter == "deleted" and entry["kind"] != "deleted":
            continue
        if kind_filter == "edited" and entry["kind"] != "edited":
            continue
        if channel_filter and entry["channel"].lower() != channel_filter:
            continue
        if text_filter and text_filter not in entry["content"].lower():
            continue
        if date_from and entry["timestamp"][:10] < date_from:
            continue
        if date_to and entry["timestamp"][:10] > date_to:
            continue
        filtered.append(entry)

    return jsonify({
        "entries": filtered[-limit:],
        "stats": stats,
        "shown": len(filtered[-limit:]),
        "matched": len(filtered),
        "channels": channels,
        "sources": {"local": local_count, "threads": thread_count, "unique": len(entries)},
        "thread_error": thread_error,
    })


# ==========================================================
# GLOBAL SEARCH (log files + every JSON store)
# ==========================================================

def _search_json_stores(needle, guild_filter, limit, results):
    def room():
        return len(results) < limit

    for key, record in history_tracker.data.items():
        if not room():
            return
        gid, _, uid = key.partition(":")
        if guild_filter and gid != guild_filter:
            continue
        for entry in record.get("actions", []):
            haystack = f"{entry.get('action','')} {entry.get('reason','')} {entry.get('moderator_id','')}".lower()
            if needle in haystack:
                results.append({
                    "kind": "moderation",
                    "title": f"{entry.get('action','action').capitalize()} - {entry.get('reason') or 'no reason'}",
                    "detail": "Moderation history",
                    "guild_id": gid,
                    "user_id": uid,
                    "timestamp": (entry.get("timestamp") or "")[:19].replace("T", " "),
                })

    for key, notes in notes_store.store.data.items():
        if not room():
            return
        gid, _, uid = key.partition(":")
        if guild_filter and gid != guild_filter:
            continue
        for note in notes:
            if needle in (note.get("text") or "").lower():
                results.append({
                    "kind": "note",
                    "title": note.get("text", ""),
                    "detail": "Private staff note",
                    "guild_id": gid,
                    "user_id": uid,
                    "timestamp": (note.get("timestamp") or "")[:19].replace("T", " "),
                })

    for gid, users in watchlist.store.data.items():
        if not room():
            return
        if guild_filter and str(gid) != guild_filter:
            continue
        for uid, watch in users.items():
            reason = watch.get("reason", "")
            if needle in reason.lower() or needle in str(uid):
                results.append({
                    "kind": "watch",
                    "title": reason or "Watchlisted",
                    "detail": "Watchlist entry",
                    "guild_id": str(gid),
                    "user_id": str(uid),
                    "timestamp": (watch.get("timestamp") or "")[:19].replace("T", " "),
                })

    for key, record in thread_index.store.data.items():
        if not room():
            return
        gid, _, uid = key.partition(":")
        if guild_filter and gid != guild_filter:
            continue
        for thread in record.get("threads", []):
            name = str(thread.get("name", ""))
            if needle in name.lower() or needle == str(thread.get("id")):
                results.append({
                    "kind": "thread",
                    "title": name or f"Thread {thread.get('id')}",
                    "detail": f"Log thread - {thread.get('count', 0)} message(s)",
                    "guild_id": gid,
                    "user_id": uid,
                    "thread_id": str(thread.get("id")),
                    "url": f"https://discord.com/channels/{gid}/{thread.get('id')}",
                })

    for key, record in log_index.data.items():
        if not room():
            return
        gid, _, uid = key.partition(":")
        if guild_filter and gid != guild_filter:
            continue
        if needle in str(uid):
            results.append({
                "kind": "verification",
                "title": f"Verification log for {uid}",
                "detail": "Logged join record",
                "guild_id": gid,
                "user_id": uid,
                "url": f"https://discord.com/channels/{gid}/{record.get('channel_id')}/{record.get('message_id')}",
            })


@app.route("/api/search", methods=["GET"])
def api_panel_search():
    query = (request.args.get("q") or "").strip()
    guild_filter = (request.args.get("guild_id") or "").strip()
    if len(query) < 2:
        return jsonify({"results": [], "message": "Type at least 2 characters."})
    needle = query.lower()
    try:
        limit = max(1, min(int(request.args.get("limit", SEARCH_RESULT_LIMIT) or SEARCH_RESULT_LIMIT), 1000))
    except ValueError:
        limit = SEARCH_RESULT_LIMIT

    results = []
    scanned = 0
    seen_messages = {}
    kind_rank = {"deleted": 3, "edited": 2, "message": 1}
    for entry in _iter_log_entries(_log_paths(guild_id=guild_filter or None)):
        scanned += 1
        if scanned > 200000 or len(results) >= limit:
            break
        if needle in entry["content"].lower() or needle in entry["channel"].lower() or needle == entry["message_id"]:
            row = {
                "kind": entry["kind"],
                "title": entry["content"][:300] or "(no text)",
                "detail": f"#{entry['channel']} - {'deleted' if entry['kind'] == 'deleted' else 'logged message'}",
                "guild_id": entry["guild_id"],
                "user_id": entry["user_id"],
                "channel": entry["channel"],
                "message_id": entry["message_id"],
                "timestamp": entry["timestamp"],
            }
            # The same message normally sits in both the live log and the
            # deleted archive; keep one row, the more telling copy wins.
            key = (entry["guild_id"], entry["user_id"], entry["message_id"])
            rank = kind_rank.get(entry["kind"], 0)
            previous = seen_messages.get(key)
            if previous is None:
                seen_messages[key] = (rank, len(results))
                results.append(row)
            elif rank > previous[0]:
                results[previous[1]] = row
                seen_messages[key] = (rank, previous[1])

    if len(results) < limit and bot is not None:
        for guild in bot.guilds:
            if len(results) >= limit:
                break
            if guild_filter and str(guild.id) != guild_filter:
                continue
            for member in guild.members:
                if len(results) >= limit:
                    break
                if needle in member.name.lower() or needle in member.display_name.lower() or needle == str(member.id):
                    results.append({
                        "kind": "member",
                        "title": f"{member.display_name} (@{member.name})",
                        "detail": f"Member of {guild.name}",
                        "guild_id": str(guild.id),
                        "user_id": str(member.id),
                        "avatar": member.display_avatar.url if member.display_avatar else "",
                    })

    if len(results) < limit:
        _search_json_stores(needle, guild_filter, limit, results)

    for row in results:
        if row.get("user_id"):
            guild = bot.get_guild(int(row["guild_id"])) if row.get("guild_id") else None
            row["actor"] = _resolve_actor(guild, row["user_id"])

    counts = {}
    for row in results:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    return jsonify({"results": results, "counts": counts, "scanned_lines": scanned})


# ==========================================================
# OWNER AUTH & ROLE-BASED ACCESS CONTROL (RBAC)
# ==========================================================

OWNER_KEY = os.getenv("OWNER_KEY", "k8Fp9X2mQv5N7bL1wR4zY6tJ0sK3uM8P")
PERMISSIONS_FILE = os.path.join(_HERE, "..", "logging", "dash_permissions.json")

# ----------------------------------------------------------
# Grantable sections and capabilities.
# The dashboard builds its whole role editor from these lists, so adding an
# entry here is enough to make it appear in the UI.
# ----------------------------------------------------------

DASH_SECTIONS = [
    {"id": "overview", "label": "Overview", "icon": "layout-dashboard",
     "desc": "Live telemetry, server snapshot and the audit trail."},
    {"id": "lookup", "label": "Members", "icon": "users",
     "desc": "Member browser, profiles, notes and watchlist."},
    {"id": "roles", "label": "Roles", "icon": "shield",
     "desc": "Role list, role manager and the permission editor."},
    {"id": "channels", "label": "Channels & Broadcast", "icon": "hash",
     "desc": "Channel tree, embeds and message dispatch."},
    {"id": "automod", "label": "AutoMod Suite", "icon": "shield-alert",
     "desc": "Automod rules and the mass moderation toolkit."},
    {"id": "tickets", "label": "Support Tickets", "icon": "ticket",
     "desc": "Read and answer member support tickets."},
    {"id": "owner", "label": "Owner Panel", "icon": "crown",
     "desc": "Master key only — a role grant never reveals the owner suite."},
]

DASH_CAPS = [
    {"id": "role_manager", "group": "Roles", "icon": "shield-plus",
     "label": "Role manager", "desc": "Create, edit, reorder and delete roles."},
    {"id": "role_perms", "group": "Roles", "icon": "list-checks",
     "label": "Permission editor", "desc": "Toggle Discord permission flags on a role."},
    {"id": "role_assign", "group": "Roles", "icon": "user-check",
     "label": "Assign roles", "desc": "Hand roles to, or take them from, a member."},
    {"id": "channel_edit", "group": "Channels", "icon": "settings-2",
     "label": "Edit channels", "desc": "Rename, retopic, slowmode, NSFW, lock, clone or delete."},
    {"id": "broadcast", "group": "Channels", "icon": "megaphone",
     "label": "Broadcast & embeds", "desc": "Dispatch messages and rich embeds to channels."},
    {"id": "purge", "group": "Moderation", "icon": "eraser",
     "label": "Message purge", "desc": "Bulk-delete messages out of a channel."},
    {"id": "member_mod", "group": "Moderation", "icon": "gavel",
     "label": "Member actions", "desc": "Ban, kick, timeout or DM one member at a time."},
    {"id": "mass_mod", "group": "Moderation", "icon": "layers",
     "label": "Mass moderation", "desc": "Run those actions across many members at once."},
    {"id": "notes", "group": "Members", "icon": "notebook-pen",
     "label": "Staff notes", "desc": "Read and write staff notes on a member."},
    {"id": "watchlist", "group": "Members", "icon": "eye",
     "label": "Watchlist", "desc": "Put members on the watchlist."},
    {"id": "logs_read", "group": "Members", "icon": "scroll-text",
     "label": "Message logs", "desc": "Read a member's logged and deleted messages."},
    {"id": "tickets_manage", "group": "Tickets", "icon": "ticket-check",
     "label": "Answer tickets", "desc": "Accept, invite, reply to and close tickets."},
    {"id": "tickets_delete", "group": "Tickets", "icon": "trash-2",
     "label": "Delete tickets", "desc": "Permanently remove a ticket and its thread."},
    {"id": "webhooks", "group": "Integrations", "icon": "webhook",
     "label": "Webhooks", "desc": "Create and edit channel webhooks."},
    {"id": "verification", "group": "Integrations", "icon": "badge-check",
     "label": "Verification panel", "desc": "Configure and post the verification panel."},
    {"id": "ticket_panel", "group": "Integrations", "icon": "ticket",
     "label": "Ticket panel", "desc": "Configure and post the Create Ticket panel."},
    {"id": "presence", "group": "System", "icon": "radio",
     "label": "Bot presence", "desc": "Change the bot's status and activity."},
    {"id": "console", "group": "System", "icon": "terminal",
     "label": "Terminal console", "desc": "Run shell commands straight from the dashboard."},
    {"id": "owner_panel", "group": "System", "icon": "crown",
     "label": "Owner panel", "desc": "Open the owner control suite at all."},
    {"id": "member_management_bypass", "group": "System", "icon": "shield-off",
     "label": "Bypass member management limits",
     "desc": "Skips the mass-action cooling-off period and can clear active cooldowns from the Member Management panel."},
]

DASH_PRESETS = [
    {"id": "full", "label": "Full access", "icon": "crown",
     "desc": "Every section and every capability, owner panel included.",
     "tabs": [s["id"] for s in DASH_SECTIONS], "caps": [c["id"] for c in DASH_CAPS]},
    # "Every capability except the shell and the owner panel" already includes
    # member_management_bypass, so this preset needs no entry of its own.
    {"id": "administrator", "label": "Administrator", "icon": "shield-check",
     "desc": "All day-to-day power, but no shell console.",
     "tabs": [s["id"] for s in DASH_SECTIONS if s["id"] != "owner"],
     "caps": [c["id"] for c in DASH_CAPS if c["id"] not in ("console", "owner_panel")]},
    {"id": "moderator", "label": "Moderator", "icon": "gavel",
     "desc": "Look up members and act on them, without touching roles.",
     "tabs": ["overview", "lookup", "tickets"],
     "caps": ["member_mod", "mass_mod", "purge", "notes", "watchlist", "logs_read", "tickets_manage"]},
    {"id": "role_manager", "label": "Role manager only", "icon": "shield-plus",
     "desc": "Just the role manager and its permission editor.",
     "tabs": ["overview", "roles"], "caps": ["role_manager", "role_perms", "role_assign"]},
    {"id": "ticket_support", "label": "Ticket support", "icon": "ticket-check",
     "desc": "Only the support ticket console.",
     "tabs": ["tickets"], "caps": ["tickets_manage"]},
    {"id": "readonly", "label": "Read only", "icon": "eye",
     "desc": "Can look at the overview and nothing else.",
     "tabs": ["overview"], "caps": []},
    {"id": "none", "label": "No access", "icon": "lock",
     "desc": "Blocked from every section and capability.",
     "tabs": [], "caps": []},
]

_SECTION_IDS = {s["id"] for s in DASH_SECTIONS}
_CAP_IDS = {c["id"] for c in DASH_CAPS}

# ----------------------------------------------------------
# Discord role permissions the dashboard's role editor exposes.
# One registry, served to the panel, so the switches there and the checks
# here can never disagree about what a permission is called.
# ----------------------------------------------------------

DASH_ROLE_PERMS = [
    {"group": "Server management", "desc": "Core administrative controls", "perms": [
        {"id": "manage_guild", "label": "Manage Server", "desc": "Server name, icon and settings.", "danger": True},
        {"id": "view_audit_log", "label": "View Audit Log", "desc": "Read the staff action history."},
        {"id": "manage_roles", "label": "Manage Roles", "desc": "Create, edit and reorder roles below their own.", "danger": True},
        {"id": "manage_channels", "label": "Manage Channels", "desc": "Create, edit and delete channels.", "danger": True},
        {"id": "manage_webhooks", "label": "Manage Webhooks", "desc": "Create and edit channel webhooks."},
        {"id": "manage_expressions", "label": "Manage Expressions", "desc": "Emoji, stickers and soundboard."},
        {"id": "manage_nicknames", "label": "Manage Nicknames", "desc": "Change other members' nicknames."},
    ]},
    {"group": "Membership & moderation", "desc": "Acting on members", "perms": [
        {"id": "kick_members", "label": "Kick Members", "desc": "Remove members from the server.", "danger": True},
        {"id": "ban_members", "label": "Ban Members", "desc": "Permanently ban accounts.", "danger": True},
        {"id": "moderate_members", "label": "Timeout Members", "desc": "Time members out."},
        {"id": "create_instant_invite", "label": "Create Invite", "desc": "Create server invite links."},
    ]},
    {"group": "Text channels", "desc": "Chat participation and content", "perms": [
        {"id": "view_channel", "label": "View Channels", "desc": "See channels at all."},
        {"id": "send_messages", "label": "Send Messages", "desc": "Post messages in channels."},
        {"id": "manage_messages", "label": "Manage Messages", "desc": "Delete other people's messages."},
        {"id": "embed_links", "label": "Embed Links", "desc": "Let links unfurl into embeds."},
        {"id": "attach_files", "label": "Attach Files", "desc": "Upload images and files."},
        {"id": "add_reactions", "label": "Add Reactions", "desc": "React with emoji."},
        {"id": "mention_everyone", "label": "Mention @everyone", "desc": "Ping @everyone and @here.", "danger": True},
        {"id": "read_message_history", "label": "Read Message History", "desc": "Read older messages."},
    ]},
    {"group": "Voice channels", "desc": "Voice participation", "perms": [
        {"id": "connect", "label": "Connect", "desc": "Join voice channels."},
        {"id": "speak", "label": "Speak", "desc": "Talk in voice channels."},
        {"id": "stream", "label": "Video", "desc": "Share video in voice."},
        {"id": "priority_speaker", "label": "Priority Speaker", "desc": "Duck other people's audio."},
        {"id": "mute_members", "label": "Mute Members", "desc": "Server-mute others."},
        {"id": "deafen_members", "label": "Deafen Members", "desc": "Server-deafen others."},
        {"id": "move_members", "label": "Move Members", "desc": "Move people between voice channels."},
    ]},
    {"group": "Elevated", "desc": "Unrestricted access", "perms": [
        {"id": "administrator", "label": "Administrator", "desc": "Bypasses every channel and role restriction.", "danger": True},
    ]},
]

DASH_ROLE_PERM_IDS = {p["id"] for g in DASH_ROLE_PERMS for p in g["perms"]}

# What a granted role may do inside the role editor, and the default when the
# owner has not said anything: day-to-day edits yes, creating and deleting no.
EDITOR_ACTIONS = [
    {"id": "rename", "label": "Rename & nicknames", "desc": "Change a role's name, hoist and mentionable flags."},
    {"id": "color", "label": "Role colour", "desc": "Repaint a role."},
    {"id": "permissions", "label": "Permission switches", "desc": "Toggle the permissions they hold themselves."},
    {"id": "reorder", "label": "Reorder roles", "desc": "Drag roles up and down — never above their own."},
    {"id": "create", "label": "Create roles", "desc": "Add brand new roles."},
    {"id": "delete", "label": "Delete roles", "desc": "Permanently remove roles they outrank."},
]
_EDITOR_ACTION_DEFAULTS = {
    "rename": True, "color": True, "permissions": True, "reorder": True,
    "create": False, "delete": False,
}
_EDITOR_ACTION_IDS = {a["id"] for a in EDITOR_ACTIONS}


def _normalize_editor(raw):
    """The role-editor grant that rides along with a dashboard access entry.

    `ceiling` is the highest role they may touch: "own" stops at their own
    highest role (Discord's rule), "all" lets them work up to the bot's, and
    "custom" names an explicit position. `grant` hands them permission flags
    they do not hold themselves — how an owner lets a moderator hand out
    Administrator without making them an administrator; `deny` is the veto
    that always wins.
    """
    raw = raw if isinstance(raw, dict) else {}
    ceiling = str(raw.get("ceiling") or "own").strip().lower()
    if ceiling not in ("own", "all", "custom"):
        ceiling = "own"
    position = raw.get("position")
    if position in ("", None):
        position = None
    else:
        position = _clamp_int(position, 0, 999, 0)
    grant = [str(x) for x in _as_list(raw.get("grant")) if str(x) in DASH_ROLE_PERM_IDS]
    deny = [str(x) for x in _as_list(raw.get("deny")) if str(x) in DASH_ROLE_PERM_IDS]
    actions_in = raw.get("actions") if isinstance(raw.get("actions"), dict) else {}
    return {
        "ceiling": ceiling,
        "position": position,
        "grant": sorted(set(grant) - set(deny)),
        "deny": sorted(set(deny)),
        "actions": {a: bool(actions_in.get(a, _EDITOR_ACTION_DEFAULTS[a])) for a in _EDITOR_ACTION_IDS},
    }


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []

# ----------------------------------------------------------
# Staff titles.
# A title is a little badge that rides next to a staff member's name on the
# public member page ("[ADMIN]", "[MOD]", anything else you invent). A title
# claims roles by role ID, and optionally by keyword so a role that has not
# been synced yet can still be recognised by its name.
# ----------------------------------------------------------

# `colors` is how many hex colours that effect actually paints with, and the
# editor only shows that many pickers: rainbow supplies its own seven, so it
# asks for none, and a solid fill only needs the one. The two-colour effects
# read `--tg-b` for their second end.
DASH_TITLE_EFFECTS = [
    {"id": "solid", "label": "Solid colour", "colors": 1, "desc": "One flat colour."},
    {"id": "gradient", "label": "Gradient", "colors": 2, "desc": "Two colours flowing across the letters."},
    {"id": "duotone", "label": "Duotone", "colors": 2, "desc": "Two colours split down the middle of the text."},
    {"id": "rainbow", "label": "Rainbow", "colors": 0, "desc": "Animated seven-colour sweep. Picks its own colours."},
    {"id": "shimmer", "label": "Shimmer", "colors": 2, "desc": "A moving highlight that sweeps along the letters."},
    {"id": "chrome", "label": "Chrome", "colors": 2, "desc": "Polished metal gradient with a lit top edge."},
    {"id": "glow", "label": "Glow", "colors": 1, "desc": "A coloured halo behind the text."},
    {"id": "neon", "label": "Neon", "colors": 2, "desc": "White core with a two-colour tube glow around it."},
    {"id": "outline", "label": "Outline", "colors": 1, "desc": "Hollow letters drawn in the accent colour."},
    {"id": "shadow", "label": "Hard shadow", "colors": 2, "desc": "Flat letters with a solid offset shadow behind them."},
    {"id": "emboss", "label": "Emboss", "colors": 1, "desc": "Chiselled letters with a lit and shaded edge."},
    {"id": "underline", "label": "Underline", "colors": 2, "desc": "Plain letters with an accent rule underneath."},
]

DASH_TITLE_FILLS = [
    {"id": "soft", "label": "Soft pill", "desc": "Tinted background in the accent colour."},
    {"id": "solid", "label": "Solid pill", "desc": "Filled background with contrast text."},
    {"id": "outline", "label": "Outline pill", "desc": "Transparent background, accent border."},
    {"id": "plain", "label": "Plain text", "desc": "No pill at all, just the letters."},
]

DASH_TITLE_FONTS = [
    "Fredoka", "Nunito", "Poppins", "Montserrat", "Inter", "Roboto", "Oswald",
    "Bebas Neue", "Playfair Display", "Lora", "Merriweather", "Caveat", "Pacifico",
    "Lobster", "Bungee", "Righteous", "Press Start 2P", "JetBrains Mono",
]

_DEFAULT_TITLE_STYLE = {
    "effect": "solid", "colors": ["#6ea8c9", "#c98f26"], "font": "",
    "weight": 800, "size": 11, "spacing": 0.5, "italic": False,
    "uppercase": True, "fill": "soft", "radius": 999, "pad_x": 8, "pad_y": 3,
    "border": 0, "glow": 0,
}

def _title_preset(label, color, effect="solid", fill="soft", keywords=None, priority=10, **style):
    entry = {"label": label, "priority": priority, "keywords": keywords if keywords else [label.lower()]}
    entry.update(style)
    entry["style"] = {"colors": [color, color], "effect": effect, "fill": fill}
    return entry


# Ready-made badges the owner can drop in with one click.
DASH_TITLE_PRESETS = [
    _title_preset("OWNER", "#c98f26", "gradient", "soft", ["owner", "founder"], 200,
                 preset_icon="crown", preset_desc="Server owner — gold gradient pill."),
    _title_preset("ADMIN", "#e06c6c", "solid", "soft", ["admin", "administrator"], 150,
                 preset_icon="shield", preset_desc="Full administrator badge in red."),
    _title_preset("MOD", "#5a9a68", "solid", "soft", ["mod", "moderator"], 120,
                 preset_icon="gavel", preset_desc="Classic green moderator tag."),
    _title_preset("MODERATOR", "#6ec9c9", "glow", "outline", ["moderator"], 110,
                 preset_icon="shield-check", preset_desc="Full word, glowing outline pill."),
    _title_preset("HELPER", "#8b5cf6", "solid", "soft", ["helper", "trial", "trainee"], 80,
                 preset_icon="hand-heart", preset_desc="Support and trial staff."),
    _title_preset("STAFF", "#7a8ce0", "solid", "plain", ["staff", "team"], 60,
                 preset_icon="users", preset_desc="Generic staff tag with no pill."),
    _title_preset("SUPPORT", "#c97ae0", "gradient", "soft", ["support", "ticket"], 70,
                 preset_icon="life-buoy", preset_desc="Ticket desk — purple gradient."),
    _title_preset("LEAD", "#c98f26", "outline", "outline", ["lead", "head"], 130,
                 preset_icon="star", preset_desc="Team lead, outlined gold."),
]

# Out of the box: the three badges the member page has always promised.
DEFAULT_TITLES = [
    {"id": "title_owner", "label": "OWNER", "priority": 200, "keywords": ["owner", "founder"],
     "roles": [], "prefix": "", "suffix": "",
     "style": {"effect": "gradient", "colors": ["#e0a86c", "#c98f26"], "fill": "soft"}},
    {"id": "title_admin", "label": "ADMIN", "priority": 150, "keywords": ["admin", "administrator"],
     "roles": [], "prefix": "", "suffix": "",
     "style": {"effect": "solid", "colors": ["#e06c6c", "#e06c6c"], "fill": "soft"}},
    {"id": "title_mod", "label": "MOD", "priority": 120, "keywords": ["mod", "moderator"],
     "roles": [], "prefix": "", "suffix": "",
     "style": {"effect": "solid", "colors": ["#5a9a68", "#5a9a68"], "fill": "soft"}},
]


def _clean_title_color(raw, fallback):
    value = str(raw or "").strip().lower()
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        value = "#" + "".join(ch * 2 for ch in value[1:])
    return value if re.fullmatch(r"#[0-9a-f]{6}", value) else fallback


def _clamp_int(raw, low, high, fallback):
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, value))


def _clamp_float(raw, low, high, fallback):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, round(value, 2)))


def _clean_title_style(raw):
    raw = raw if isinstance(raw, dict) else {}
    base = dict(_DEFAULT_TITLE_STYLE)

    effect = str(raw.get("effect") or "").strip().lower()
    base["effect"] = effect if effect in {e["id"] for e in DASH_TITLE_EFFECTS} else "solid"
    fill = str(raw.get("fill") or "").strip().lower()
    base["fill"] = fill if fill in {f["id"] for f in DASH_TITLE_FILLS} else "soft"

    colors = raw.get("colors")
    if isinstance(colors, str):
        colors = [c.strip() for c in colors.split(",") if c.strip()]
    if not isinstance(colors, list):
        colors = []
    first = _clean_title_color(colors[0] if colors else "", "#6ea8c9")
    second = _clean_title_color(colors[1] if len(colors) > 1 else "", first)
    base["colors"] = [first, second]

    font = str(raw.get("font") or "").strip()[:40]
    base["font"] = font if font in DASH_TITLE_FONTS else ""

    base["weight"] = _clamp_int(raw.get("weight"), 400, 900, base["weight"])
    base["size"] = _clamp_int(raw.get("size"), 8, 22, base["size"])
    base["spacing"] = _clamp_float(raw.get("spacing"), 0, 3, base["spacing"])
    base["radius"] = _clamp_int(raw.get("radius"), 0, 999, base["radius"])
    base["pad_x"] = _clamp_int(raw.get("pad_x"), 0, 24, base["pad_x"])
    base["pad_y"] = _clamp_int(raw.get("pad_y"), 0, 12, base["pad_y"])
    base["border"] = _clamp_int(raw.get("border"), 0, 4, base["border"])
    base["glow"] = _clamp_int(raw.get("glow"), 0, 24, base["glow"])
    base["italic"] = bool(raw.get("italic"))
    base["uppercase"] = raw.get("uppercase") is not False
    return base


def _clean_staff_title(raw, index=0):
    raw = raw if isinstance(raw, dict) else {}
    label = str(raw.get("label") or "").strip()[:24]
    if not label:
        label = "STAFF"
    roles = raw.get("roles")
    if not isinstance(roles, list):
        roles = []
    keywords = raw.get("keywords")
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    if not isinstance(keywords, list):
        keywords = []
    title_id = re.sub(r"[^A-Za-z0-9_-]", "", str(raw.get("id") or ""))[:40] or f"title_{index}"
    return {
        "id": title_id,
        "label": label,
        "prefix": str(raw.get("prefix") or "")[:8],
        "suffix": str(raw.get("suffix") or "")[:8],
        "priority": _clamp_int(raw.get("priority"), 0, 999, 50),
        "roles": sorted({str(r) for r in roles if str(r).strip()})[:200],
        "keywords": [str(k).strip().lower()[:24] for k in keywords if str(k).strip()][:20],
        "style": _clean_title_style(raw.get("style")),
    }


def _default_titles():
    """The seeded badges, run through the cleaner so every style key exists."""
    return [_clean_staff_title(json.loads(json.dumps(t)), i) for i, t in enumerate(DEFAULT_TITLES)]


def _normalize_titles(raw, allow_empty=False):
    if not isinstance(raw, list):
        return _default_titles()
    out = []
    seen = set()
    for i, item in enumerate(raw[:60]):
        cleaned = _clean_staff_title(item, i)
        if cleaned["id"] in seen:
            cleaned["id"] = f"{cleaned['id']}_{i}"
        seen.add(cleaned["id"])
        out.append(cleaned)
    if out:
        return out
    # An explicitly empty list means "no badges anywhere", which is a real
    # choice; only a missing key falls back to the seeded defaults.
    return [] if allow_empty else _default_titles()


def _load_titles(perms=None):
    perms = perms if isinstance(perms, dict) else _load_dash_perms()
    if "titles" not in perms:
        return _default_titles()
    return _normalize_titles(perms.get("titles"), allow_empty=True)


def _title_public(title):
    """Only what a browser needs to draw the badge."""
    return {
        "id": title.get("id"),
        "label": title.get("label"),
        "prefix": title.get("prefix") or "",
        "suffix": title.get("suffix") or "",
        "style": title.get("style") or {},
    }


def _title_for(role_ids, role_names, titles):
    """The staff badge a member wears, by role ID first then name keyword.

    Highest priority wins, so OWNER beats ADMIN beats MOD. Returns None when
    nothing matches, which is the normal case for ordinary members.
    """
    if not titles:
        return None
    ids = {str(r) for r in (role_ids or [])}
    names = [str(n).lower() for n in (role_names or [])]
    best = None
    for title in titles:
        claimed = ids & set(title.get("roles") or [])
        hit = bool(claimed)
        if not hit and title.get("keywords"):
            for keyword in title["keywords"]:
                if any(keyword == name or keyword in name for name in names):
                    hit = True
                    break
        if not hit:
            continue
        if best is None or (title.get("priority") or 0) > (best.get("priority") or 0):
            best = title
    return _title_public(best) if best else None


def _member_title(role_ids, role_names, titles=None):
    try:
        return _title_for(role_ids, role_names, titles if titles is not None else _load_titles())
    except Exception:
        return None



def _normalize_role_entry(entry):
    """Accept both the legacy list-of-tabs shape and the new dict shape."""
    if isinstance(entry, list):
        return {"tabs": sorted({t for t in entry if t in _SECTION_IDS}), "caps": [],
                "editor": _normalize_editor({})}
    if isinstance(entry, dict):
        tabs = entry.get("tabs") if isinstance(entry.get("tabs"), list) else []
        caps = entry.get("caps") if isinstance(entry.get("caps"), list) else []
        return {
            "tabs": sorted({str(t) for t in tabs if str(t) in _SECTION_IDS}),
            "caps": sorted({str(c) for c in caps if str(c) in _CAP_IDS}),
            "editor": _normalize_editor(entry.get("editor")),
        }
    return {"tabs": [], "caps": [], "editor": _normalize_editor({})}


def _dash_perms_payload():
    """The whole editor: saved roles plus the registry the UI renders from."""
    perms = _load_dash_perms()
    roles = {}
    for rid, entry in (perms.get("roles") or {}).items():
        roles[str(rid)] = _normalize_role_entry(entry)
    return {
        "roles": roles,
        "sections": DASH_SECTIONS,
        "capabilities": DASH_CAPS,
        "presets": DASH_PRESETS,
        "titles": _load_titles(perms),
        "title_effects": DASH_TITLE_EFFECTS,
        "title_fills": DASH_TITLE_FILLS,
        "title_fonts": DASH_TITLE_FONTS,
        "title_presets": DASH_TITLE_PRESETS,
        "role_perms": DASH_ROLE_PERMS,
        "editor_actions": EDITOR_ACTIONS,
    }


def _access_for_roles(role_ids, perms, role_labels=None, guild=None):
    """What a member gets, decided by their highest configured role.

    `role_ids` must arrive in descending Discord position order. The highest
    role that has a saved entry is the one that counts, so a Member role set
    to "no access" next to an Admin role can never veto the admin. When their
    highest configured entry grants nothing at all, the door stays shut.

    When *no* role of theirs has an entry the default is not "everything": a
    role is read against its own Discord permissions and gets exactly those
    capabilities, with no dashboard sections. A role that can do nothing in
    the server therefore gets nothing here either, until an owner configures
    it by hand.
    """
    roles_map = perms.get("roles") or {}
    labels = role_labels or {}
    entry = None
    granted_by = None
    for rid in role_ids:
        key = str(rid)
        if key in roles_map:
            entry = _normalize_role_entry(roles_map[key])
            granted_by = {"id": key, "name": str(labels.get(key) or "")}
            break

    if entry is None:
        top = None
        caps = []
        g = guild if guild is not None else _main_guild()
        if g is not None:
            for rid in role_ids:
                key = str(rid)
                role = g.get_role(int(key)) if key.isdigit() else None
                if role is not None:
                    top = role
                    caps = _derived_caps_for_role(role)
                    break
        return {
            "configured": True,
            "derived": True,
            "allowed": bool(caps),
            "tabs": [],
            "caps": caps,
            "granted_by": ({"id": str(top.id), "name": top.name} if top is not None else None),
            "editor": _normalize_editor({}),
        }
    return {
        "configured": True,
        "derived": False,
        "allowed": bool(entry["tabs"] or entry["caps"]),
        "tabs": entry["tabs"],
        "caps": entry["caps"],
        "granted_by": granted_by,
        "editor": entry["editor"],
    }


def _actor_limits(guild=None, ctx=None, access=None):
    """What the signed-in account may change inside the role manager.

    Three things decide it, and the tightest one wins: Discord's own rule that
    you can only touch roles below your highest role, the same rule for the
    bot (it cannot edit roles at or above its own top role), and the editor
    grant the owner saved for the role that let them in. That grant can raise
    the ceiling and hand out permission flags the account does not hold.
    """
    if guild is None:
        guild = _main_guild()
    perms = _load_dash_perms()
    if ctx is None:
        member = _current_member()
        try:
            ctx = _member_context(member["user_id"]) if member else {}
        except Exception:
            ctx = {}
    ctx = ctx or {}
    if access is None:
        access = _access_for_roles(ctx.get("role_ids") or [], perms, ctx.get("labels") or {})
    editor = (access or {}).get("editor") or _normalize_editor({})
    owner = _owner_unlocked()
    bot_top = guild.me.top_role.position if (guild is not None and guild.me) else 0

    gm = ctx.get("member")
    own_top = 0
    own_perms = set()
    top_role = None
    if gm is not None:
        try:
            roles = sorted((r for r in getattr(gm, "roles", []) if not r.is_default()),
                           key=lambda r: r.position, reverse=True)
        except Exception:
            roles = []
        if roles:
            own_top = int(roles[0].position)
            top_role = {"id": str(roles[0].id), "name": roles[0].name, "position": own_top}
        try:
            own_perms = {name for name, value in gm.guild_permissions if value}
        except Exception:
            own_perms = set()

    if owner:
        ceiling = bot_top
        actions = {a: True for a in _EDITOR_ACTION_IDS}
        grant, deny = [], []
        allowed = set(DASH_ROLE_PERM_IDS)
    else:
        mode = editor["ceiling"]
        if mode == "all":
            ceiling = bot_top
        elif mode == "custom":
            ceiling = min(int(editor["position"] or 0), bot_top)
        else:
            ceiling = min(own_top, bot_top)
        actions = dict(editor["actions"])
        grant, deny = editor["grant"], editor["deny"]
        allowed = (own_perms | set(grant)) - set(deny)

    if top_role:
        label = f"below {top_role['name']}"
    elif owner:
        label = "every role the bot can reach"
    else:
        label = "no role you outrank"
    return {
        "owner": owner,
        "ceiling": ceiling,
        "ceiling_label": label,
        "ceiling_mode": "all" if owner else editor["ceiling"],
        "own_top": own_top,
        "bot_top": bot_top,
        "top_role": top_role,
        "actions": actions,
        "grant": list(grant),
        "deny": list(deny),
        # The flags they may hand out, and the ones they hold already, so the
        # panel can lock exactly the switches that would be refused.
        "perms": sorted(p for p in allowed if p in DASH_ROLE_PERM_IDS),
        "held": sorted(p for p in own_perms if p in DASH_ROLE_PERM_IDS),
    }


class _PseudoRole:
    """Stands in for a role that does not exist yet (a new role being created).

    `_apply_role_perm_request` only reads the current permission value, so an
    empty baseline is the honest answer: nothing is being turned *off*.
    """

    class _Empty:
        value = 0

        def __iter__(self):
            return iter(())

    permissions = _Empty()


def _apply_role_perm_request(limits, role, requested):
    """Which of the requested permission flips this account may actually make.

    Flags they cannot grant are left exactly as they were rather than failing
    the whole save, so a partial edit still lands — the ignored list is what
    the panel reports back. A flag that is not changing is never policed: you
    are not granting anything by leaving an existing permission alone.
    """
    allowed = set(limits["perms"])
    deny = set(limits["deny"])
    current = {name: bool(value) for name, value in role.permissions}
    perms = discord.Permissions(role.permissions.value)
    ignored = []
    for name, state in (requested or {}).items():
        if not hasattr(perms, name):
            continue
        want = bool(state)
        if current.get(name) == want:
            continue
        if name in deny or name not in allowed:
            ignored.append(name)
            continue
        setattr(perms, name, want)
    return perms, sorted(set(ignored))


def _actor_may_edit_role(limits, role):
    """Discord's hierarchy rule, applied to the dashboard account."""
    if role is None:
        return False, "Role not found."
    if role.is_default():
        return False, "The @everyone role cannot be changed here."
    if role.managed:
        return False, "That role is managed by an integration."
    if int(role.position) >= int(limits["ceiling"]):
        top = limits.get("top_role") or {}
        if top and str(top.get("id")) == str(getattr(role, "id", "")):
            return False, f"'{role.name}' is your own highest role, so you cannot change it here."
        whose = top.get("name") or "your highest role"
        return False, f"'{role.name}' sits above your highest role ({whose}), so you cannot change it."
    return True, ""

def _load_dash_perms():
    if os.path.exists(PERMISSIONS_FILE):
        try:
            with open(PERMISSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"roles": {}, "default_tabs": ["overview", "lookup", "roles", "channels", "dispatch", "search"]}

def _save_dash_perms(data):
    try:
        with open(PERMISSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False

@app.route("/api/owner/auth", methods=["POST"])
def api_owner_auth():
    data = request.json or {}
    key = data.get("key", "").strip()
    if key == OWNER_KEY:
        # Remember it against this browser session so the owner panel does not
        # ask for the key again on the next page load. The account that was
        # signed in at the time is stamped alongside it, so a different account
        # signing in on the same browser never inherits the unlock. Signing out
        # drops it too.
        session["owner_ok"] = True
        session["owner_ok_for"] = str((session.get("member") or {}).get("user_id") or "")
        session.permanent = True
        return jsonify({"status": "success", "message": "Owner Mode Unlocked!", "authenticated": True})
    return jsonify({"status": "error", "message": "Invalid Owner Key.", "authenticated": False}), 403


@app.route("/api/owner/lock", methods=["POST"])
def api_owner_lock():
    """Forget the remembered key — for shared machines."""
    session.pop("owner_ok", None)
    session.pop("owner_ok_for", None)
    return jsonify({"status": "success", "message": "Owner mode locked.", "owner_unlocked": False})

@app.route("/api/owner/access", methods=["GET", "POST"])
def api_owner_access():
    if request.method == "POST":
        data = request.json or {}
        # Changing who can reach what is owner-key territory, never something
        # a signed-in role can hand to itself. A session that already unlocked
        # with the key counts too, so a page refresh does not demand the key a
        # second time just to save.
        if not (_owner_unlocked() or str(data.get("owner_key") or "") == OWNER_KEY):
            return _fail("Owner key required to change dashboard access.", 403)
        perms = _load_dash_perms()
        roles_out = perms.setdefault("roles", {})

        # Either one role at a time (the editor saves per role) or the whole map.
        single = data.get("role_id")
        roles_config = data.get("roles")
        titles_in = data.get("titles")
        if not single and not isinstance(roles_config, dict) and not isinstance(titles_in, list):
            return _fail("Send a role_id, a roles map or a titles list.")

        if single:
            roles_out[str(single)] = _normalize_role_entry(data.get("entry"))
        elif isinstance(roles_config, dict):
            roles_out.clear()
            for rid, entry in roles_config.items():
                roles_out[str(rid)] = _normalize_role_entry(entry)

        if isinstance(titles_in, list):
            perms["titles"] = _normalize_titles(titles_in, allow_empty=True)

        if not _save_dash_perms(perms):
            return _fail("Failed to save permissions.", 500)
        return jsonify({"status": "success", "message": "Dashboard access saved.", **_dash_perms_payload()})
    return jsonify(_dash_perms_payload())


def _access_catalog():
    """Section and capability names, for explaining a grant to its owner.

    Only ids, labels and grouping — the same information the tabs already show,
    so it is safe to hand to a signed-in staffer who cannot open the owner
    panel and therefore cannot fetch the full registry.
    """
    return {
        "sections": [{"id": s["id"], "label": s["label"], "icon": s.get("icon", ""),
                      "desc": s.get("desc", "")} for s in DASH_SECTIONS],
        "caps": [{"id": c["id"], "label": c["label"], "group": c.get("group", "Other"),
                  "desc": c.get("desc", "")} for c in DASH_CAPS],
    }


@app.route("/api/admin/me", methods=["GET"])
def api_admin_me():
    """Who the dashboard is signed in as, and what that buys them."""
    member = _current_member()
    ctx = {
        "in_guild": False, "role_ids": [], "role_names": [], "labels": {},
        "member": None, "checked": False, "source": "none",
    }
    if member:
        try:
            ctx = _member_context(member["user_id"])
        except Exception:
            pass

    perms = _load_dash_perms()
    access = _access_for_roles(ctx["role_ids"], perms, ctx["labels"])
    # Being signed in is not the same as being allowed in. The account has to
    # belong to this server, hold at least one role, and that highest role has
    # to actually grant something.
    allowed = bool(member) and ctx["in_guild"] and access["allowed"]
    reason = "not_signed_in"
    if member:
        if not ctx["in_guild"]:
            # Only accuse the account of not being in the server when something
            # actually told us that. An unreachable bot is not evidence.
            reason = "not_in_guild" if ctx["checked"] else "unverifiable"
        elif not ctx["live"]:
            # Known only from the synced member table: that carries role names
            # but not the role ids the role access is keyed on, so the honest
            # answer is "cannot tell yet", never a silent full grant.
            allowed = False
            reason = "unverified"
        elif not ctx["role_ids"]:
            allowed = False
            reason = "no_roles"
        elif not access["allowed"]:
            reason = "no_permissions"
        else:
            reason = ""

    owner_unlocked = _owner_unlocked()
    return jsonify({
        "status": "ok",
        "oauth_ready": _oauth_ready(),
        "logged_in": bool(member),
        "user": member or None,
        "owner_unlocked": owner_unlocked,
        "roles": ctx["role_ids"],
        "role_names": ctx["role_names"],
        "in_guild": ctx["in_guild"],
        "verified": bool(ctx["checked"]),
        "member_source": ctx["source"],
        "allowed": allowed,
        # The master key is a recovery hatch, not a silent grant: the panel shows
        # the denial anyway and offers this as a deliberate choice.
        "owner_override": bool(owner_unlocked and not allowed),
        "denied_reason": reason,
        "staff_title": _member_title(ctx["role_ids"], ctx["role_names"], titles=_load_titles(perms))
                        if member and ctx["in_guild"] else None,
        "access": access,
        "catalog": _access_catalog(),
        # What they may change in the role manager, resolved from their own
        # highest role, the bot's ceiling and the editor grant that let them in.
        "editor_limits": _actor_limits(ctx=ctx, access=access),
    })


# ==========================================================
# VERIFICATION PANEL (OWNER PANEL)
# ==========================================================

VERIFICATION_FIELDS = {
    "role_ids", "title", "description", "color", "button_label",
    "button_emoji", "button_style", "author_name", "author_icon_url",
    "thumbnail_url", "image_url", "footer_text", "footer_icon_url",
    "timestamp", "fields",
}

VERIFICATION_BUTTON_STYLES = {"primary", "success", "danger", "secondary"}


def _clean_verification_fields(raw):
    """Keep only usable embed fields: a name, a value and the inline flag."""
    if not isinstance(raw, list):
        return []
    cleaned = []
    for item in raw[:25]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:256]
        value = str(item.get("value") or "").strip()[:1024]
        if not name or not value:
            continue
        cleaned.append({"name": name, "value": value, "inline": bool(item.get("inline"))})
    return cleaned


@app.route("/api/verification/config", methods=["GET", "POST"])
def api_verification_config():
    if request.method == "GET":
        return jsonify(load_verification_config())

    data = request.json or {}
    patch = {key: value for key, value in data.items() if key in VERIFICATION_FIELDS}
    if not patch:
        return _fail("Nothing to save.")

    if "role_ids" in patch:
        role_ids = verification_role_ids(patch.get("role_ids"))
        if not role_ids:
            return _fail("Enter at least one verified role ID.")
        for rid in role_ids:
            if not str(rid).isdigit():
                return _fail(f"'{rid}' is not a valid role ID.")

    if "fields" in patch:
        patch["fields"] = _clean_verification_fields(patch.get("fields"))
    if "timestamp" in patch:
        patch["timestamp"] = bool(patch.get("timestamp"))
    if "button_emoji" in patch:
        patch["button_emoji"] = str(patch.get("button_emoji") or "").strip()[:64]
    if "button_style" in patch:
        style = str(patch.get("button_style") or "").strip().lower()
        patch["button_style"] = style if style in VERIFICATION_BUTTON_STYLES else "success"

    cfg = save_verification_config(patch)

    # Push the new look onto the panel that is already posted, if there is one.
    # Only build the coroutine when the bot loop exists, otherwise it would be
    # created and never awaited.
    panel_updated = False
    if bot is not None and getattr(bot, "loop", None) is not None:
        try:
            panel_updated = bool(_run(refresh_verification_panel(bot), timeout=15))
        except Exception:
            panel_updated = False

    message = "Verification settings saved."
    if panel_updated:
        message += " The live panel was updated."
    elif cfg.get("panel_channel_id"):
        message += " The existing panel could not be updated; redeploy it."
    return jsonify({
        "status": "success",
        "message": message,
        "config": cfg,
        "panel_updated": panel_updated,
    })


@app.route("/api/verification/deploy", methods=["POST"])
def api_verification_deploy():
    if bot is None:
        return _fail("The bot is not connected yet.")
    data = request.json or {}
    channel_id = data.get("channel_id")
    if not channel_id:
        return _fail("Pick a channel to post the verification panel in.")

    guild = bot.get_guild(int(data["guild_id"])) if data.get("guild_id") else None
    if guild is None:
        guild = bot.guilds[0] if bot.guilds else None
    if guild is None:
        return _fail("Guild not found.", 404)

    try:
        channel = guild.get_channel(int(channel_id))
    except (TypeError, ValueError):
        channel = None
    if channel is None or not isinstance(channel, discord.TextChannel):
        return _fail("Pick a text channel for the verification panel.", 404)

    async def do_deploy():
        return await deploy_verification_panel(channel, bot.user)

    try:
        _run(do_deploy(), timeout=20)
    except PanelError as e:
        return _fail(str(e), 503)
    except Exception as e:
        return _fail(f"Could not deploy the panel: {e}")

    return jsonify({
        "status": "success",
        "message": f"Verification panel deployed to #{channel.name}. It survives restarts now.",
    })


# ==========================================================
# TICKET PANEL (OWNER PANEL)
#
# The second editable panel. Its request and response shapes deliberately
# mirror the verification endpoints above, so the frontend editor is the same
# code pointed at a different URL.
# ==========================================================

# Identical key set to VERIFICATION_FIELDS, minus nothing: the editor reuses the
# same controls, so the two configs stay in step.
TICKET_PANEL_FIELDS = {
    "role_ids", "title", "description", "color", "button_label",
    "button_emoji", "button_style", "author_name", "author_icon_url",
    "thumbnail_url", "image_url", "footer_text", "footer_icon_url",
    "timestamp", "fields",
}


@app.route("/api/ticket-panel/config", methods=["GET", "POST"])
def api_ticket_panel_config():
    if request.method == "GET":
        cfg = load_ticket_panel_config()
        # The support link is derived, never stored, so the editor can always
        # show the exact address the button will open.
        cfg["support_url"] = ticket_support_url()
        return jsonify(cfg)

    data = request.json or {}
    patch = {key: value for key, value in data.items() if key in TICKET_PANEL_FIELDS}
    if not patch:
        return _fail("Nothing to save.")

    # A ticket panel grants no roles, so unlike the verification editor this
    # does not require a role ID — it just ignores anything passed in.
    patch.pop("role_ids", None)

    if "fields" in patch:
        patch["fields"] = _clean_verification_fields(patch.get("fields"))
    if "timestamp" in patch:
        patch["timestamp"] = bool(patch.get("timestamp"))
    if "button_emoji" in patch:
        patch["button_emoji"] = str(patch.get("button_emoji") or "").strip()[:64]
    if "button_style" in patch:
        style = str(patch.get("button_style") or "").strip().lower()
        patch["button_style"] = style if style in VERIFICATION_BUTTON_STYLES else "success"

    cfg = save_ticket_panel_config(patch)

    # Push the new look onto the panel that is already posted, if there is one.
    panel_updated = False
    if bot is not None and getattr(bot, "loop", None) is not None:
        try:
            panel_updated = bool(_run(refresh_ticket_panel(bot), timeout=15))
        except Exception:
            panel_updated = False

    message = "Ticket panel settings saved."
    if not ticket_support_url():
        message += " MEMBER_SITE_URL is not set, so the button will send plain text instead of a link."
    if panel_updated:
        message += " The live panel was updated."
    elif cfg.get("panel_channel_id"):
        message += " The existing panel could not be updated; redeploy it."
    return jsonify({
        "status": "success",
        "message": message,
        "config": cfg,
        "support_url": ticket_support_url(),
        "panel_updated": panel_updated,
    })


@app.route("/api/ticket-panel/deploy", methods=["POST"])
def api_ticket_panel_deploy():
    if bot is None:
        return _fail("The bot is not connected yet.")
    data = request.json or {}
    channel_id = data.get("channel_id")
    if not channel_id:
        return _fail("Pick a channel to post the ticket panel in.")

    guild = bot.get_guild(int(data["guild_id"])) if data.get("guild_id") else None
    if guild is None:
        guild = bot.guilds[0] if bot.guilds else None
    if guild is None:
        return _fail("Guild not found.", 404)

    try:
        channel = guild.get_channel(int(channel_id))
    except (TypeError, ValueError):
        channel = None
    if channel is None or not isinstance(channel, discord.TextChannel):
        return _fail("Pick a text channel for the ticket panel.", 404)

    async def do_deploy():
        return await deploy_ticket_panel(channel, bot.user)

    try:
        _run(do_deploy(), timeout=20)
    except PanelError as e:
        return _fail(str(e), 503)
    except Exception as e:
        return _fail(f"Could not deploy the panel: {e}")

    message = f"Ticket panel deployed to #{channel.name}. It survives restarts now."
    if not ticket_support_url():
        message += " Set MEMBER_SITE_URL so the button links to the support form."
    return jsonify({"status": "success", "message": message, "support_url": ticket_support_url()})


# ==========================================================
# CHANNEL SETTINGS & 3-STATE PERMISSION OVERWRITES
# ==========================================================

@app.route("/api/guild/<guild_id>/channel/<channel_id>/details", methods=["GET"])
def api_channel_details(guild_id, channel_id):
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return _fail("Channel not found.", 404)
    
    overwrites_data = []
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Role):
            target_type = "role"
            name = target.name
            color = str(target.color)
        elif isinstance(target, discord.Member):
            target_type = "member"
            name = str(target)
            color = "#99aab5"
        else:
            continue
        
        allow_perms, deny_perms = overwrite.pair()
        allow_dict = dict(allow_perms)
        deny_dict = dict(deny_perms)
        
        states = {}
        for perm, _ in allow_dict.items():
            if allow_dict.get(perm):
                states[perm] = "allow"
            elif deny_dict.get(perm):
                states[perm] = "deny"
            else:
                states[perm] = "neutral"
                
        overwrites_data.append({
            "id": str(target.id),
            "type": target_type,
            "name": name,
            "color": color,
            "states": states
        })
        
    # Discord reports bitrate in bits/s; the UI works in kbps.
    raw_bitrate = getattr(channel, "bitrate", None)
    vq_mode = getattr(channel, "video_quality_mode", None)
    return jsonify({
        "id": str(channel.id),
        "name": channel.name,
        "topic": getattr(channel, "topic", "") or "",
        "slowmode": getattr(channel, "slowmode_delay", 0),
        "nsfw": getattr(channel, "nsfw", False),
        # These two live in this app's own files, not in Discord.
        "media_only": is_media_only(guild.id, channel.id),
        "selfpromo": is_selfpromo_channel(guild.id, channel.id),
        "type": str(channel.type),
        "bitrate": round(raw_bitrate / 1000) if raw_bitrate else None,
        "user_limit": getattr(channel, "user_limit", None),
        "video_quality": ("720p" if vq_mode == discord.VideoQualityMode.full else "auto") if vq_mode is not None else None,
        "overwrites": overwrites_data
    })

@app.route("/api/guild/<guild_id>/channel/<channel_id>/update", methods=["POST"])
def api_channel_update(guild_id, channel_id):
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild: return _fail("Guild not found.", 404)
    channel = guild.get_channel(int(channel_id))
    if not channel: return _fail("Channel not found.", 404)
    
    data = request.json or {}
    kwargs = {}
    if "name" in data and data["name"].strip(): kwargs["name"] = data["name"].strip()
    if "topic" in data and hasattr(channel, "topic"): kwargs["topic"] = data["topic"]
    if "slowmode" in data and hasattr(channel, "slowmode_delay"): kwargs["slowmode_delay"] = int(data["slowmode"])
    if "nsfw" in data and hasattr(channel, "nsfw"): kwargs["nsfw"] = bool(data["nsfw"])
    # Voice-only fields. The UI sends kbps but Discord wants bits/s and rejects
    # anything under 8000, so convert and clamp instead of returning a 400.
    if "bitrate" in data and hasattr(channel, "bitrate"):
        try:
            kbps = int(float(data["bitrate"]))
        except (TypeError, ValueError):
            return _fail("Bitrate must be a number in kbps.")
        kwargs["bitrate"] = max(8000, kbps * 1000)
    if "user_limit" in data and hasattr(channel, "user_limit"):
        try:
            kwargs["user_limit"] = max(0, min(int(float(data["user_limit"])), 99))
        except (TypeError, ValueError):
            return _fail("User limit must be a number.")
    if "video_quality" in data and hasattr(channel, "video_quality_mode"):
        mode = str(data["video_quality"]).strip().lower()
        if mode in ("720p", "full", "2"):
            kwargs["video_quality_mode"] = discord.VideoQualityMode.full
        elif mode in ("auto", "1", ""):
            kwargs["video_quality_mode"] = discord.VideoQualityMode.auto
        else:
            return _fail("Video quality must be either auto or 720p.")
    
    async def do_update():
        await channel.edit(**kwargs)
        
    fut = asyncio.run_coroutine_threadsafe(do_update(), bot.loop)
    try:
        fut.result(timeout=10)
        return jsonify({"status": "success", "message": f"Updated #{channel.name}."})
    except Exception as e:
        return _fail(f"Failed to update channel: {e}")

@app.route("/api/guild/<guild_id>/channel/<channel_id>/overwrites", methods=["POST"])
def api_channel_overwrites(guild_id, channel_id):
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild: return _fail("Guild not found.", 404)
    channel = guild.get_channel(int(channel_id))
    if not channel: return _fail("Channel not found.", 404)
    
    data = request.json or {}
    raw_target_ids = data.get("target_ids") or ([data.get("target_id")] if data.get("target_id") else [])
    target_type = data.get("target_type", "role")
    perm_states = data.get("states", {})
    
    if not raw_target_ids: return _fail("No target roles or members specified.")

    overwrite = discord.PermissionOverwrite()
    for perm_name, state in perm_states.items():
        if hasattr(overwrite, perm_name):
            if state == "allow": setattr(overwrite, perm_name, True)
            elif state == "deny": setattr(overwrite, perm_name, False)
            elif state == "neutral": setattr(overwrite, perm_name, None)
            
    async def do_set_overwrites():
        updated_count = 0
        for tid in raw_target_ids:
            target = guild.get_role(int(tid)) if target_type == "role" else guild.get_member(int(tid))
            if target:
                await channel.set_permissions(target, overwrite=overwrite)
                updated_count += 1
        return updated_count
        
    fut = asyncio.run_coroutine_threadsafe(do_set_overwrites(), bot.loop)
    try:
        count = fut.result(timeout=10)
        return jsonify({"status": "success", "message": f"Saved permission overwrites for {count} target(s) in #{channel.name}."})
    except Exception as e:
        return _fail(f"Failed to set overwrites: {e}")


# ==========================================================
# MAX MODERATION SUITE API ENDPOINTS
# ==========================================================

# ==========================================================
# AUTOMOD SUITE
# ==========================================================

def _caller_access():
    """Who is asking the automod endpoints, and what they hold there.

    Returns (member, ctx, access) with the pieces the gates below need, or
    (None, None, None) when nobody is signed in.
    """
    member = _current_member()
    if not member:
        return None, None, None
    try:
        ctx = _member_context(member["user_id"])
    except Exception:
        ctx = {"in_guild": False, "role_ids": [], "labels": {}}
    perms = _load_dash_perms()
    access = _access_for_roles(ctx.get("role_ids") or [], perms, ctx.get("labels") or {})
    return member, ctx, access


def _require_access():
    """Any signed-in staffer who is allowed into the panel at all.

    Reading AutoMod state is gated by the tab itself, so this only refuses
    strangers and signed-out callers.
    """
    member, ctx, access = _caller_access()
    if not member:
        return _fail("Sign in with Discord first.", 403)
    if _owner_unlocked():
        return None
    if not (ctx or {}).get("in_guild"):
        return _fail("Your account is not in this server.", 403)
    if not (access or {}).get("allowed"):
        return _fail("Your roles do not grant dashboard access.", 403)
    return None


def _require_cap(caps, what):
    """Server-side twin of the UI's capability gate.

    `caps` is space-separated and means "any of these", exactly like canCap on
    the page. The master key answers yes to everything.
    """
    gate = _require_access()
    if gate is not None:
        return gate
    if _owner_unlocked():
        return None
    _member, _ctx, access = _caller_access()
    held = set((access or {}).get("caps") or [])
    wanted = [c for c in str(caps).split() if c]
    if held & set(wanted):
        return None
    return _fail("Your roles do not include " + what + ".", 403)


def _can_edit_automod():
    """Whether this caller may change safety settings (not just read them)."""
    if _owner_unlocked():
        return True
    _member, _ctx, access = _caller_access()
    return "member_mod" in set((access or {}).get("caps") or [])


def _automod_payload(guild_id):
    """Everything the suite draws, in one round trip."""
    cfg = automod_config.for_guild(guild_id)
    return {
        "status": "ok",
        "config": cfg,
        "stats": automod_config.stats(guild_id),
        "incidents": automod_config.incidents(guild_id, 60),
        "rules": AUTOMOD_RULES,
        "actions": AUTOMOD_ACTIONS,
        "can_edit": _can_edit_automod(),
    }


@app.route("/api/automod", methods=["GET"])
def api_automod_state():
    gate = _require_access()
    if gate is not None:
        return gate
    guild = _main_guild()
    if guild is None:
        return _fail("No server is reachable right now.", 404)
    return jsonify(_automod_payload(guild.id))


@app.route("/api/automod/config", methods=["POST"])
def api_automod_config():
    gate = _require_cap("member_mod", "AutoMod controls")
    if gate is not None:
        return gate
    guild = _main_guild()
    if guild is None:
        return _fail("No server is reachable right now.", 404)
    patch = request.json or {}
    if not isinstance(patch, dict) or not patch:
        return _fail("Nothing to save.")
    automod_config.update(guild.id, patch)
    payload = _automod_payload(guild.id)
    payload["message"] = "AutoMod settings saved."
    return jsonify(payload)


@app.route("/api/automod/test", methods=["POST"])
def api_automod_test():
    """Run a typed message through the real detectors.

    Same code the bot runs, so the answer to "would this be caught?" is the
    bot's answer and not a second implementation that can drift.
    """
    gate = _require_access()
    if gate is not None:
        return gate
    guild = _main_guild()
    if guild is None:
        return _fail("No server is reachable right now.", 404)
    body = request.json or {}
    text = str(body.get("text") or "")[:2000]
    system = get_anti_raid()
    # Testing an unsaved draft answers for what is on screen. Without this you
    # flip a rule off, run the test, and get the verdict for the old settings.
    draft = body.get("draft")
    if isinstance(draft, dict) and draft:
        cfg = automod_config.update(guild.id, draft, save=False)
    else:
        cfg = automod_config.for_guild(guild.id)
    hits = []
    if text.strip() and system is not None:
        rules = cfg.get("rules") or {}
        # Detection runs whether or not the rule is armed. A rule that is off is
        # still worth reporting — the panel's whole point is answering "would I
        # catch this?", and the answer to that should not change because you
        # have not switched the rule on yet. Whether it would be actioned comes
        # from `enabled_hits` below.
        words_rule = rules.get("words") or {}
        hit = system._words_hit(text, words_rule)
        if hit:
            hits.append({"rule": "words", "label": "Blocked words", "detail": "matched " + hit})
        caps_rule = rules.get("caps") or {}
        hit = system._caps_hit(text, caps_rule)
        if hit:
            hits.append({"rule": "caps", "label": "Excessive caps", "detail": hit})
        zalgo_rule = rules.get("zalgo") or {}
        hit = system._zalgo_hit(text, zalgo_rule)
        if hit:
            hits.append({"rule": "zalgo", "label": "Zalgo text", "detail": hit})
        emoji_rule = rules.get("emoji") or {}
        hit = system._emoji_hit(text, emoji_rule)
        if hit:
            hits.append({"rule": "emoji", "label": "Emoji flood", "detail": hit})
        # Links and invites are flood rules: one message can only ever count
        # toward them, never trip them, so they are labelled as contributions.
        if system.contains_harmful_link(text):
            hits.append({"rule": "links", "label": "Link spam",
                         "detail": "carries a non-media link — counts toward the flood rule"})
        if system.DISCORD_INVITE_REGEX.search(text):
            hits.append({"rule": "invites", "label": "Discord invites",
                         "detail": "carries a server invite — counts toward the flood rule"})
    armed = [h for h in hits if cfg.get("enabled") and (cfg.get("rules", {}).get(h["rule"], {}) or {}).get("enabled")
             and str((cfg.get("rules", {}).get(h["rule"], {}) or {}).get("action") or "off") != "off"]
    return jsonify({"status": "ok", "hits": hits, "enabled_hits": armed,
                    "config_enabled": bool(cfg.get("enabled")), "dry_run": bool(cfg.get("dry_run"))})


@app.route("/api/guild/<guild_id>/bans", methods=["GET", "POST"])
def api_guild_bans(guild_id):
    deny = _require_access() if request.method == "GET" else _require_cap("member_mod", "member actions")
    if deny is not None:
        return deny
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild: return _fail("Guild not found.", 404)
    
    if request.method == "POST":
        data = request.json or {}
        user_id = data.get("user_id")
        async def do_unban():
            user = await bot.fetch_user(int(user_id))
            await guild.unban(user, reason="Unbanned via Dashboard")
        fut = asyncio.run_coroutine_threadsafe(do_unban(), bot.loop)
        try:
            fut.result(timeout=10)
            return jsonify({"status": "success", "message": "User unbanned."})
        except Exception as e:
            return _fail(f"Failed to unban: {e}")
            
    async def fetch_bans():
        bans = []
        async for entry in guild.bans(limit=200):
            bans.append({
                "user_id": str(entry.user.id),
                "name": str(entry.user),
                "avatar": entry.user.display_avatar.url if entry.user.display_avatar else "",
                "reason": entry.reason or "No reason provided"
            })
        return bans
        
    fut = asyncio.run_coroutine_threadsafe(fetch_bans(), bot.loop)
    try:
        bans_list = fut.result(timeout=10)
        return jsonify({"bans": bans_list})
    except Exception as e:
        return jsonify({"bans": [], "error": str(e)})

# ==========================================================
# MEMBER MANAGEMENT: channel flags and cooldowns
# ==========================================================


def _decorate_channels(guild, entries):
    """Join a list of `{"channel_id", ...}` rows with live channel names."""
    out = []
    for row in entries:
        cid = int(row["channel_id"])
        channel = guild.get_channel(cid)
        out.append({
            "channel_id": str(cid),
            "name": getattr(channel, "name", f"deleted-channel-{cid}"),
            "type": str(getattr(channel, "type", "text")),
            "missing": channel is None,
            **{k: v for k, v in row.items() if k != "channel_id"},
        })
    return out


def _decorate_roles(guild, role_ids):
    """Role ids with the names and colours the panel draws them with."""
    out = []
    for rid in role_ids:
        role = guild.get_role(int(rid))
        out.append({
            "id": str(rid),
            "name": getattr(role, "name", f"deleted-role-{rid}"),
            "color": ("#" + format(role.color.value, "06x")) if role is not None and role.color else "",
            "missing": role is None,
        })
    return out


def _decorate_users(guild, user_ids):
    """User ids with display names, resolved from the member cache only."""
    out = []
    for uid in user_ids:
        member = guild.get_member(int(uid)) if str(uid).isdigit() else None
        out.append({
            "id": str(uid),
            "name": str(member) if member is not None else str(uid),
            "missing": member is None,
        })
    return out


@app.route("/api/guild/<guild_id>/media-only", methods=["GET"])
def api_media_only_list(guild_id):
    """Every media-only channel in one guild, with its name for the panel."""
    gate = _require_access()
    if gate is not None:
        return gate
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    rows = [{"channel_id": cid} for cid in media_only_store.ids(guild.id)]
    return jsonify({"status": "ok", "channels": _decorate_channels(guild, rows)})


@app.route("/api/guild/<guild_id>/channel/<channel_id>/media-only", methods=["POST"])
def api_media_only_set(guild_id, channel_id):
    """Flag or unflag one channel as media-only."""
    gate = _require_cap("channel_edit", "channel editing")
    if gate is not None:
        return gate
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    if guild.get_channel(int(channel_id)) is None:
        return _fail("Channel not found.", 404)
    enabled = bool((request.json or {}).get("enabled"))
    media_only_store.set_flag(guild.id, channel_id, enabled)
    return jsonify({
        "status": "success",
        "message": f"#{guild.get_channel(int(channel_id)).name} is "
                   + ("media-only now." if enabled else "no longer media-only."),
        "media_only": enabled,
    })


@app.route("/api/guild/<guild_id>/media-only/remove", methods=["POST"])
def api_media_only_remove(guild_id):
    """Drop one channel from the media-only list."""
    gate = _require_cap("channel_edit", "channel editing")
    if gate is not None:
        return gate
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    channel_id = str((request.json or {}).get("channel_id") or "").strip()
    if not channel_id.isdigit():
        return _fail("Invalid channel ID.")
    media_only_store.set_flag(guild.id, channel_id, False)
    return jsonify({"status": "success", "message": "Removed from the media-only list."})


@app.route("/api/guild/<guild_id>/selfpromo", methods=["GET"])
def api_selfpromo_list(guild_id):
    """Every self-promo channel with its allowlists joined for display."""
    gate = _require_access()
    if gate is not None:
        return gate
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    rows = []
    for cid, entry in selfpromo_store.listing(guild.id).items():
        rows.append({
            "channel_id": cid,
            "roles": _decorate_roles(guild, entry.get("roles") or []),
            "users": _decorate_users(guild, entry.get("users") or []),
        })
    return jsonify({"status": "ok", "channels": _decorate_channels(guild, rows)})


@app.route("/api/guild/<guild_id>/channel/<channel_id>/selfpromo", methods=["POST"])
def api_selfpromo_toggle(guild_id, channel_id):
    """Flag a channel for self-promo (empty allowlists) or drop it entirely."""
    gate = _require_cap("channel_edit", "channel editing")
    if gate is not None:
        return gate
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return _fail("Channel not found.", 404)
    enabled = bool((request.json or {}).get("enabled"))
    selfpromo_store.set_flag(guild.id, channel_id, enabled)
    return jsonify({
        "status": "success",
        "message": f"#{channel.name} "
                   + ("allows self-promo now." if enabled else "no longer allows self-promo."),
        "selfpromo": enabled,
    })


@app.route("/api/guild/<guild_id>/channel/<channel_id>/selfpromo/roles", methods=["POST"])
def api_selfpromo_roles(guild_id, channel_id):
    """Replace the allowed-role list for one self-promo channel."""
    gate = _require_cap("channel_edit", "channel editing")
    if gate is not None:
        return gate
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    ids = [str(v) for v in ((request.json or {}).get("role_ids") or []) if str(v).isdigit()]
    entry = selfpromo_store.set_list(guild.id, channel_id, "roles", ids)
    return jsonify({"status": "success", "message": "Allowed roles updated.",
                    "roles": _decorate_roles(guild, entry["roles"])})


@app.route("/api/guild/<guild_id>/channel/<channel_id>/selfpromo/users", methods=["POST"])
def api_selfpromo_users(guild_id, channel_id):
    """Replace the allowed-user list for one self-promo channel."""
    gate = _require_cap("channel_edit", "channel editing")
    if gate is not None:
        return gate
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    ids = [str(v) for v in ((request.json or {}).get("user_ids") or []) if str(v).isdigit()]
    entry = selfpromo_store.set_list(guild.id, channel_id, "users", ids)
    return jsonify({"status": "success", "message": "Allowed users updated.",
                    "users": _decorate_users(guild, entry["users"])})


@app.route("/api/guild/<guild_id>/cooldowns", methods=["GET"])
def api_mass_cooldowns(guild_id):
    """Active mass-action cooldowns, newest expiry last."""
    gate = _require_access()
    if gate is not None:
        return gate
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)
    now = time.time()
    rows = []
    for (actor, kind), expires in list(MASS_ACTION_COOLDOWN.items()):
        if expires <= now:
            MASS_ACTION_COOLDOWN.pop((actor, kind), None)
            continue
        member = guild.get_member(int(actor)) if actor.isdigit() else None
        rows.append({
            "actor_id": actor,
            "actor": str(member) if member is not None else actor,
            "kind": kind,
            "label": MASS_ACTION_LABELS.get(kind, kind),
            "seconds_left": int(expires - now),
            "expires_at": expires,
        })
    rows.sort(key=lambda r: r["seconds_left"])
    return jsonify({"status": "ok", "cooldowns": rows, "window_seconds": MASS_COOLDOWN_SECONDS,
                    "can_clear": _caller_has_bypass()})


@app.route("/api/guild/<guild_id>/cooldowns/clear", methods=["POST"])
def api_mass_cooldowns_clear(guild_id):
    """Wipe every active cooldown. Owner mode or the bypass capability only."""
    gate = _require_access()
    if gate is not None:
        return gate
    if not _caller_has_bypass():
        return _fail("Clearing cooldowns needs the member management bypass capability.", 403)
    cleared = len(MASS_ACTION_COOLDOWN)
    MASS_ACTION_COOLDOWN.clear()
    return jsonify({"status": "success",
                    "message": f"Cleared {cleared} active cooldown(s)."})


@app.route("/api/guild/<guild_id>/invites", methods=["GET", "POST"])
def api_guild_invites(guild_id):
    deny = _require_access() if request.method == "GET" else _require_cap("member_mod", "invite controls")
    if deny is not None:
        return deny
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild: return _fail("Guild not found.", 404)
    
    if request.method == "POST":
        data = request.json or {}
        code = data.get("code")
        async def do_revoke():
            invites = await guild.invites()
            for inv in invites:
                if inv.code == code:
                    await inv.delete(reason="Revoked via Dashboard")
                    return True
            return False
        fut = asyncio.run_coroutine_threadsafe(do_revoke(), bot.loop)
        try:
            res = fut.result(timeout=10)
            return jsonify({"status": "success" if res else "error", "message": "Invite revoked." if res else "Invite not found."})
        except Exception as e:
            return _fail(f"Failed to revoke invite: {e}")

    async def fetch_invites():
        invites = []
        for inv in await guild.invites():
            invites.append({
                "code": inv.code,
                "url": "https://discord.gg/" + str(inv.code),
                "channel": inv.channel.name if inv.channel else "Unknown",
                "channel_id": str(inv.channel.id) if inv.channel else "",
                "inviter": str(inv.inviter) if inv.inviter else "Unknown",
                "inviter_id": str(inv.inviter.id) if inv.inviter else "",
                "inviter_avatar": (inv.inviter.display_avatar.url if inv.inviter and inv.inviter.display_avatar else ""),
                "uses": inv.uses or 0,
                "max_uses": inv.max_uses or 0,
                "max_age": inv.max_age or 0,
                "temporary": bool(inv.temporary),
                "expires_at": inv.expires_at.isoformat() if getattr(inv, "expires_at", None) else "",
                "created_at": inv.created_at.isoformat() if inv.created_at else "",
            })
        return invites
    fut = asyncio.run_coroutine_threadsafe(fetch_invites(), bot.loop)
    try:
        inv_list = fut.result(timeout=10)
        return jsonify({"invites": inv_list})
    except Exception as e:
        return jsonify({"invites": [], "error": str(e)})

@app.route("/api/guild/<guild_id>/audit", methods=["GET"])
def api_guild_security_audit(guild_id):
    """A real audit: every finding carries the evidence behind it.

    `r.is_default` used to be read as an attribute instead of called, which made
    the check always true — every role was skipped and the report came back
    empty no matter how wide open the server was. It is a method.
    """
    deny = _require_access()
    if deny is not None:
        return deny
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)

    try:
        everyone = guild.default_role
    except Exception:
        everyone = None

    dangerous_flags = [
        ("administrator", "Administrator"),
        ("manage_guild", "Manage Server"),
        ("manage_roles", "Manage Roles"),
        ("manage_channels", "Manage Channels"),
        ("manage_webhooks", "Manage Webhooks"),
        ("ban_members", "Ban Members"),
        ("kick_members", "Kick Members"),
        ("mention_everyone", "Mention Everyone"),
    ]

    def held_flags(perms):
        out = []
        for flag, label in dangerous_flags:
            try:
                if getattr(perms, flag, False):
                    out.append(label)
            except Exception:
                continue
        return out

    admin_roles = []
    dangerous_roles = []
    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if role.is_default() or role.managed:
            continue
        flags = held_flags(role.permissions)
        if not flags:
            continue
        row = {"id": str(role.id), "name": role.name, "position": role.position,
               "members": len(role.members), "flags": flags,
               "color": _color_hex(role.color)}
        # Classify off the permission itself. `flags` holds display labels
        # ("Administrator"), so testing it for the lowercase flag id always came
        # back false and every admin role was filed as merely "elevated".
        if bool(getattr(role.permissions, "administrator", False)):
            admin_roles.append(row)
        else:
            dangerous_roles.append(row)

    everyone_flags = held_flags(everyone.permissions) if everyone is not None else []

    unverified_bots = []
    for member in guild.members:
        if not member.bot:
            continue
        verified = False
        try:
            verified = bool(member.public_flags.verified_bot)
        except Exception:
            verified = False
        if not verified:
            unverified_bots.append({"id": str(member.id), "name": str(member),
                                    "avatar": member.display_avatar.url if member.display_avatar else "",
                                    "joined_at": member.joined_at.isoformat() if member.joined_at else None})

    # Real guild settings, not guesses.
    mfa_level = int(getattr(guild, "mfa_level", 0) or 0)
    verification_level = str(getattr(getattr(guild, "verification_level", None), "name", "") or "none")
    content_filter = str(getattr(getattr(guild, "explicit_content_filter", None), "name", "") or "disabled")

    findings = []
    if mfa_level < 1:
        findings.append({"severity": "high", "title": "Two-factor is not required for moderators",
                         "detail": "Anyone with a stolen password can use their admin powers.",
                         "fix": "Server Settings -> Safety Setup -> Require 2FA for moderation."})
    if everyone_flags:
        findings.append({"severity": "high", "title": "@everyone holds sensitive permissions",
                         "detail": ", ".join(everyone_flags) + " granted to every member by default.",
                         "fix": "Move those permissions onto a staff role instead."})
    if len(admin_roles) > 2:
        findings.append({"severity": "medium", "title": str(len(admin_roles)) + " roles carry Administrator",
                         "detail": "Every one of them sidesteps the whole permission system.",
                         "fix": "Keep Administrator for one or two roles and grant the rest explicitly."})
    if unverified_bots:
        findings.append({"severity": "medium", "title": str(len(unverified_bots)) + " bot account(s) are unverified",
                         "detail": "Unverified bots cannot be trusted with message content.",
                         "fix": "Remove them, or ask the owner to verify the application."})
    if content_filter.lower() == "disabled":
        findings.append({"severity": "low", "title": "Explicit media filter is off",
                         "detail": "Media with explicit content is not scanned on upload.",
                         "fix": "Server Settings -> Safety Setup -> scan media content."})
    if verification_level.lower() in ("none", "low"):
        findings.append({"severity": "low", "title": "Server verification level is " + verification_level.lower(),
                         "detail": "Freshly made accounts can talk immediately.",
                         "fix": "Raise it to at least Medium for a public server."})
    if dangerous_roles:
        findings.append({"severity": "low", "title": str(len(dangerous_roles)) + " role(s) hold elevated permissions",
                         "detail": "Nothing wrong on its own, but each one is a way in if an account leaks.",
                         "fix": "Review the list below and remove what is not needed."})

    severity_cost = {"high": 18, "medium": 8, "low": 3}
    score = 100
    for finding in findings:
        score -= severity_cost.get(finding["severity"], 2)
    score = max(0, min(100, score))

    return jsonify({
        "status": "ok",
        "security_score": score,
        "grade": ("locked down" if score >= 90 else "solid" if score >= 75 else "needs work" if score >= 55 else "exposed"),
        "findings": findings,
        "admin_roles": admin_roles,
        "dangerous_roles": dangerous_roles,
        "everyone_flags": everyone_flags,
        "unverified_bots": unverified_bots,
        "mfa_level": mfa_level,
        "verification_level": verification_level,
        "content_filter": content_filter,
        "admin_count": sum(r["members"] for r in admin_roles),
        "member_count": guild.member_count or 0,
        "bot_count": sum(1 for m in guild.members if m.bot),
        "role_count": len([r for r in guild.roles if not r.is_default()]),
        "channel_count": len(guild.channels),
        "checked_at": datetime.datetime.utcnow().isoformat(),
    })

# ==========================================================
# MASS-ACTION COOLING-OFF
# ==========================================================

# (actor_id, kind) -> unix time the block lifts. In memory only: a restart
# wiping these is intentional, because this stops a spree, not an attack.
MASS_ACTION_COOLDOWN = {}
MASS_COOLDOWN_SECONDS = 300

# The vocabulary of mass actions. Kinds without a bulk endpoint yet are listed
# anyway, so the guard is already in place when one is added.
MASS_ACTION_LABELS = {
    "ban": "ban",
    "kick": "kick",
    "softban": "softban",
    "roleall": "role assignment",
    "purge_all": "purge",
    "lock_all": "lockdown",
    "channel_bulk": "channel edit",
    "role_bulk": "role edit",
}


def _caller_has_bypass():
    """Whether this caller may ignore the member-management limits.

    Owner mode always may; otherwise the resolved dashboard caps are asked.
    """
    if _owner_unlocked():
        return True
    _member, _ctx, access = _caller_access()
    return "member_management_bypass" in set((access or {}).get("caps") or [])


def _mass_actor_id():
    """Who is asking, as a string id. Signed-out callers borrow the body's."""
    return _current_member_id(request.json or {})


def _mass_cooldown_guard(kind):
    """Refuse a repeat of the same mass action by the same actor too soon.

    Returns a Flask response to send back, or None to carry on. One attempt
    starts the clock, no matter how many members were in the batch.
    """
    if kind not in MASS_ACTION_LABELS or _caller_has_bypass():
        return None
    actor = _mass_actor_id()
    if not actor.isdigit():
        return None
    left = MASS_ACTION_COOLDOWN.get((actor, kind), 0) - time.time()
    if left <= 0:
        return None
    minutes = max(1, math.ceil(left / 60))
    return jsonify({
        "status": "error",
        "message": f"Mass {MASS_ACTION_LABELS[kind]} was run recently. "
                   f"Wait {minutes} more minute(s), or ask an owner to bypass.",
    }), 429


def _mass_cooldown_record(kind):
    """Start the cooling-off window for one actor and one kind."""
    if kind not in MASS_ACTION_LABELS or _caller_has_bypass():
        return
    actor = _mass_actor_id()
    if actor.isdigit():
        MASS_ACTION_COOLDOWN[(actor, kind)] = time.time() + MASS_COOLDOWN_SECONDS


def _mass_cooldown_kind(action):
    """The cooldown kind a mass action maps to, or None for uncooled ones."""
    key = str(action or "").strip().lower()
    if key in MASS_ACTION_LABELS:
        return key
    # Only the removals are rate-limited from the mass endpoint; a batch of
    # timeouts is reversible and does not deserve the same brake.
    return None


@app.route("/api/guild/<guild_id>/mass", methods=["POST"])
def api_guild_mass_mod(guild_id):
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild: return _fail("Guild not found.", 404)
    
    data = request.json or {}
    action = data.get("action")
    user_ids = data.get("user_ids", [])
    reason = data.get("reason", "Mass Moderation Execution")

    kind = _mass_cooldown_kind(action)
    guard = _mass_cooldown_guard(kind)
    if guard is not None:
        return guard
    
    async def do_mass():
        count = 0
        for uid in user_ids:
            try:
                mid = int(uid)
                if action == "ban":
                    await guild.ban(discord.Object(id=mid), reason=reason)
                    count += 1
                elif action == "kick":
                    mem = guild.get_member(mid)
                    if mem: await mem.kick(reason=reason); count += 1
                elif action == "timeout":
                    mem = guild.get_member(mid)
                    if mem: await mem.timeout(datetime.timedelta(minutes=60), reason=reason); count += 1
            except Exception:
                pass
        return count
        
    fut = asyncio.run_coroutine_threadsafe(do_mass(), bot.loop)
    try:
        res = fut.result(timeout=20)
        _mass_cooldown_record(kind)
        return jsonify({"status": "success", "message": f"Mass {action} applied to {res} members."})
    except Exception as e:
        return _fail(f"Mass action failed: {e}")

VOICE_ACTIONS = {
    "mute": ("mute", True), "unmute": ("mute", False),
    "deafen": ("deafen", True), "undeafen": ("deafen", False),
}


@app.route("/api/guild/<guild_id>/voice", methods=["GET", "POST"])
def api_guild_voice_mod(guild_id):
    """Who is in voice, and the controls to move them around.

    GET reports each occupied channel with the people in it (mute/deaf state
    included) so the panel can target one member, one channel or everyone.
    POST takes `action`, an optional `channel_id` and an optional `user_id`.
    """
    deny = _require_access()
    if deny is not None:
        return deny
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild:
        return _fail("Guild not found.", 404)

    if request.method == "GET":
        rows = []
        for channel in guild.voice_channels:
            members = []
            for member in channel.members:
                members.append({
                    "id": str(member.id),
                    "name": member.display_name or member.name,
                    "username": member.name,
                    "avatar": member.display_avatar.url if member.display_avatar else "",
                    "self_mute": bool(getattr(member.voice, "self_mute", False)) if member.voice else False,
                    "self_deaf": bool(getattr(member.voice, "self_deaf", False)) if member.voice else False,
                    "server_mute": bool(getattr(member.voice, "mute", False)) if member.voice else False,
                    "server_deaf": bool(getattr(member.voice, "deaf", False)) if member.voice else False,
                    "streaming": bool(getattr(member.voice, "self_stream", False)) if member.voice else False,
                })
            if not members:
                continue
            rows.append({
                "id": str(channel.id), "name": channel.name,
                "user_limit": getattr(channel, "user_limit", 0) or 0,
                "bitrate": round((getattr(channel, "bitrate", 0) or 0) / 1000) or None,
                "members": members,
            })
        total = sum(len(row["members"]) for row in rows)
        return jsonify({"status": "ok", "channels": rows, "total": total,
                        "voice_channels": len([c for c in guild.voice_channels])})

    deny = _require_cap("member_mod", "voice controls")
    if deny is not None:
        return deny
    data = request.json or {}
    action = str(data.get("action") or "")
    target_channel_id = data.get("target_channel_id") or data.get("channel_id")
    target_user_id = data.get("user_id")

    try:
        target_channel = guild.get_channel(int(target_channel_id)) if target_channel_id else None
    except (TypeError, ValueError):
        target_channel = None

    async def do_voice():
        count = 0
        for channel in guild.voice_channels:
            if target_channel is not None and channel.id != target_channel.id:
                continue
            for member in channel.members:
                if target_user_id and str(member.id) != str(target_user_id):
                    continue
                try:
                    if action in VOICE_ACTIONS:
                        field, value = VOICE_ACTIONS[action]
                        await member.edit(**{field: value}, reason="Dashboard voice control")
                        count += 1
                    elif action == "disconnect":
                        await member.edit(voice_channel=None, reason="Dashboard voice control")
                        count += 1
                    elif action == "move" and target_channel is not None:
                        await member.edit(voice_channel=target_channel, reason="Dashboard voice control")
                        count += 1
                except Exception:
                    continue
        return count

    fut = asyncio.run_coroutine_threadsafe(do_voice(), bot.loop)
    try:
        res = fut.result(timeout=15)
        scope = ("that member" if target_user_id else
                 ("#" + target_channel.name if target_channel is not None else "every voice room"))
        return jsonify({"status": "success", "count": res,
                        "message": f"{action} applied to {res} member(s) in {scope}."})
    except Exception as e:
        return _fail(f"Voice action failed: {e}")

@app.route("/api/guild/<guild_id>/structure", methods=["GET"])
def api_guild_structure(guild_id):
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild: return _fail("Guild not found.", 404)
    
    roles = [{"id": str(r.id), "name": r.name, "color": str(r.color), "position": r.position} for r in guild.roles]
    channels = [{"id": str(c.id), "name": c.name, "type": str(c.type), "category": c.category.name if c.category else None} for c in guild.channels]
    
    return jsonify({
        "guild_name": guild.name,
        "guild_id": str(guild.id),
        "member_count": guild.member_count or 0,
        "roles": roles,
        "channels": channels,
        "exported_at": datetime.datetime.utcnow().isoformat()
    })

@app.route("/api/guild/<guild_id>/webhooks", methods=["GET", "POST"])
def api_guild_webhooks(guild_id):
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild: return jsonify({"webhooks": [], "error": "Guild not found"})

    if request.method == "POST":
        data = request.json or {}
        webhook_id = data.get("webhook_id")
        action = data.get("action", "delete")
        async def do_wh_action():
            whs = await guild.webhooks()
            for wh in whs:
                if str(wh.id) == str(webhook_id):
                    if action == "delete":
                        await wh.delete(reason="Deleted via Dashboard")
                        return True
            return False
        fut = asyncio.run_coroutine_threadsafe(do_wh_action(), bot.loop)
        try:
            res = fut.result(timeout=10)
            return jsonify({"status": "success" if res else "error", "message": "Webhook deleted." if res else "Webhook not found."})
        except Exception as e:
            return _fail(f"Webhook action failed: {e}")

    async def fetch_whs():
        whs = await guild.webhooks()
        out = []
        for wh in whs:
            # The avatar has to be resolved here: the UI shows it in the manager
            # and in the Ctrl+K palette, and it has no way to build the CDN URL
            # itself (avatar is an Asset or None, not a hash we can trust).
            try:
                avatar_url = wh.avatar.url if wh.avatar else ""
            except Exception:
                avatar_url = ""
            out.append({
                "id": str(wh.id),
                "name": wh.name,
                "channel_id": str(wh.channel_id),
                "channel_name": wh.channel.name if wh.channel else "Unknown",
                "url": wh.url,
                "avatar": avatar_url,
                "creator": str(wh.user) if wh.user else "Unknown",
            })
        return out

    fut = asyncio.run_coroutine_threadsafe(fetch_whs(), bot.loop)
    try:
        wh_list = fut.result(timeout=10)
        return jsonify({"webhooks": wh_list})
    except Exception as e:
        return jsonify({"webhooks": [], "error": str(e)})

@app.route("/api/guild/<guild_id>/webhooks/create", methods=["POST"])
def api_guild_create_webhook(guild_id):
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild: return _fail("Guild not found.", 404)
    data = request.json or {}
    name = data.get("name", "Dashboard Webhook")
    channel_id = data.get("channel_id")
    if not channel_id: return _fail("Channel required.")
    channel = guild.get_channel(int(channel_id))
    if not channel: return _fail("Channel not found.")

    async def do_create():
        wh = await channel.create_webhook(name=name, reason="Created via Dashboard")
        return {"id": str(wh.id), "name": wh.name, "url": wh.url}

    fut = asyncio.run_coroutine_threadsafe(do_create(), bot.loop)
    try:
        res = fut.result(timeout=10)
        return jsonify({"status": "success", "message": f"Webhook '{res['name']}' created successfully.", "webhook": res})
    except Exception as e:
        return _fail(f"Failed to create webhook: {e}")

async def _fetch_image_bytes(url, limit=8 * 1024 * 1024):
    """Download an image so it can be handed to Discord as a webhook avatar.

    discord.py wants raw bytes for ``Webhook.edit(avatar=...)``, so a plain URL
    from the dashboard cannot be passed through. Best effort: any failure just
    means the avatar is left untouched.
    """
    if not re.match(r"^https?://", (url or "").strip(), re.I):
        return None
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url.strip()) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        if not data or len(data) > limit:
            return None
        return data
    except Exception:
        return None


@app.route("/api/guild/<guild_id>/webhooks/<webhook_id>/update", methods=["POST"])
def api_guild_update_webhook(guild_id, webhook_id):
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild: return _fail("Guild not found.", 404)
    data = request.json or {}
    new_name = data.get("name")
    new_channel_id = data.get("channel_id")
    new_avatar_url = (data.get("avatar_url") or "").strip()

    async def do_update():
        whs = await guild.webhooks()
        for wh in whs:
            if str(wh.id) == str(webhook_id):
                kwargs = {}
                target_channel = None
                if new_name: kwargs["name"] = new_name
                if new_channel_id:
                    try:
                        ch = guild.get_channel(int(new_channel_id))
                    except (TypeError, ValueError):
                        ch = None
                    if ch:
                        kwargs["channel"] = ch
                        target_channel = ch
                avatar_warning = None
                if re.match(r"^https?://", new_avatar_url, re.I):
                    image = await _fetch_image_bytes(new_avatar_url)
                    if image:
                        kwargs["avatar"] = image
                    else:
                        avatar_warning = "Avatar image could not be downloaded, so it was left unchanged."
                await wh.edit(**kwargs)
                try:
                    avatar_url = wh.avatar.url if wh.avatar else ""
                except Exception:
                    avatar_url = ""
                # After edit() the cached channel can still point at the old one,
                # so report the channel we actually moved it to.
                moved = target_channel or wh.channel
                return {
                    "id": str(wh.id),
                    "name": wh.name,
                    "url": wh.url,
                    "channel_id": str(moved.id) if moved else str(wh.channel_id),
                    "channel_name": moved.name if moved else "Unknown",
                    "avatar": avatar_url,
                    "avatar_warning": avatar_warning,
                }
        return None

    fut = asyncio.run_coroutine_threadsafe(do_update(), bot.loop)
    try:
        res = fut.result(timeout=25)
        if res:
            warning = res.pop("avatar_warning", None)
            message = "Webhook updated successfully."
            if warning:
                message += f" {warning}"
            return jsonify({"status": "success", "message": message, "webhook": res})
        return _fail("Webhook not found.")
    except Exception as e:
        return _fail(f"Failed to update webhook: {e}")

@app.route("/api/guild/<guild_id>/roles/<role_id>/permissions", methods=["POST"])
def api_guild_role_permissions(guild_id, role_id):
    guild = bot.get_guild(int(guild_id)) if bot else None
    if not guild: return _fail("Guild not found.", 404)
    role = guild.get_role(int(role_id))
    if not role: return _fail("Role not found.", 404)
    data = request.json or {}
    perm_flags = data.get("permissions", {})

    limits = _actor_limits(guild)
    allowed, why = _actor_may_edit_role(limits, role)
    if not allowed:
        return _fail(why, 403)
    if not limits["actions"]["permissions"]:
        return _fail("Your dashboard access does not include editing permissions.", 403)

    perms, ignored = _apply_role_perm_request(limits, role, perm_flags)

    async def do_update_perms():
        await role.edit(permissions=perms, reason="Permissions updated via Dashboard")

    fut = asyncio.run_coroutine_threadsafe(do_update_perms(), bot.loop)
    try:
        fut.result(timeout=10)
        message = f"Updated permissions for role '{role.name}'."
        if ignored:
            message += f" Skipped {len(ignored)} you cannot grant: {', '.join(ignored[:4])}."
        return jsonify({"status": "success", "message": message, "ignored": ignored})
    except Exception as e:
        return _fail(f"Failed to update role permissions: {e}")


# ==========================================================
# SUPPORT TICKETS (member page)
# ==========================================================

TICKET_PATH = os.path.join(_HERE, "..", "logging", "tickets.json")
TICKET_LOCK = threading.Lock()
MAX_OPEN_TICKETS = 5
TICKET_CATEGORIES = {
    "account": "Account Support",
    "report": "Report a user",
    "bug": "Report a bug",
    "appeal": "Appeal a punishment",
    "apply": "Apply for Staff",
    "feedback": "Something else / feedback",
    "other": "Something else",
}


def _load_tickets():
    try:
        with open(TICKET_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_tickets(rows):
    tmp = TICKET_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, TICKET_PATH)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ==========================================================
# CATBOX UPLOADS (ticket attachments)
# ==========================================================

UPLOAD_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
UPLOAD_WINDOW = 600        # seconds
UPLOAD_LIMIT = 5           # uploads per window per session
UPLOAD_LOG = {}
UPLOAD_LOCK = threading.Lock()
CATBOX_PREFIX = "https://files.catbox.moe/"


def _upload_key():
    member = _current_member()
    if member:
        return "u:" + str(member["user_id"])
    key = session.get("upload_key")
    if not key:
        key = uuid.uuid4().hex
        session["upload_key"] = key
    return "s:" + key


def _upload_allowed(key):
    now = time.time()
    with UPLOAD_LOCK:
        hits = [t for t in UPLOAD_LOG.get(key, []) if now - t < UPLOAD_WINDOW]
        if len(hits) >= UPLOAD_LIMIT:
            UPLOAD_LOG[key] = hits
            return False, int(UPLOAD_WINDOW - (now - hits[0]))
        hits.append(now)
        UPLOAD_LOG[key] = hits
    return True, 0


def _catbox_upload(filename, content_type, blob):
    """POST a file to Catbox with a hand-built multipart body (no SDK)."""
    boundary = "----MemberHub" + uuid.uuid4().hex
    chunks = []

    def field(name, value):
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )

    field("reqtype", "fileupload")
    if CATBOX_USERHASH:
        field("userhash", CATBOX_USERHASH)

    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="fileToUpload"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    )
    chunks.append(blob)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        CATBOX_API,
        data=b"".join(chunks),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": OAUTH_HEADERS["User-Agent"],
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", "replace").strip()


@app.route("/api/tickets/upload", methods=["POST"])
def api_ticket_upload():
    if not CATBOX_USERHASH:
        return _fail("Uploads are not set up yet. Ask an admin to add the Catbox userhash.", 503)

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return _fail("Pick a file first.")

    mime = (upload.mimetype or "").split(";")[0].strip().lower()
    if mime not in UPLOAD_TYPES:
        return _fail("Only PNG, JPG, GIF and WEBP images are allowed.")

    ext = os.path.splitext(upload.filename)[1].lower()
    if ext and ext not in UPLOAD_EXTS:
        return _fail("That file name is not allowed.")

    limit_bytes = TICKET_ATTACH_MAX_MB * 1024 * 1024
    blob = upload.read(limit_bytes + 1)
    if len(blob) > limit_bytes:
        return _fail(f"That file is larger than {TICKET_ATTACH_MAX_MB} MB.")
    if not blob:
        return _fail("That file was empty.")

    allowed, wait = _upload_allowed(_upload_key())
    if not allowed:
        minutes = max(1, (wait + 59) // 60)
        return _fail(f"Too many uploads right now. Try again in about {minutes} minute(s).", 429)

    safe_name = "ticket" + (ext if ext in UPLOAD_EXTS else UPLOAD_TYPES[mime])
    try:
        url = _catbox_upload(safe_name, mime, blob)
    except urllib.error.HTTPError as exc:
        print(f"[upload] catbox rejected the file: {exc} {_http_body(exc)}")
        return _fail("The upload service rejected that file.")
    except Exception as exc:
        print(f"[upload] catbox failed: {exc}")
        return _fail("The upload service did not answer. Please try again.")

    if not url.startswith(CATBOX_PREFIX):
        print(f"[upload] unexpected catbox reply: {url[:200]!r}")
        return _fail("The upload service did not return a link.")

    return jsonify({"status": "ok", "url": url})


def _clean_attachments(raw):
    """Keep only Catbox links, capped, whatever the client sends."""
    if not isinstance(raw, list):
        return [], None
    if len(raw) > TICKET_ATTACH_MAX_FILES:
        return [], f"You can attach up to {TICKET_ATTACH_MAX_FILES} files."
    cleaned = []
    for item in raw:
        url = str(item or "").strip()
        if not url.startswith(CATBOX_PREFIX):
            continue
        cleaned.append(url[:300])
    return cleaned, None


def _ticket_excerpt(ticket):
    """First useful answer, trimmed for the ticket list."""
    answers = ticket.get("answers") or {}
    for key in ("What happened?", "What exactly did they say or do?", "Tell us more about it",
                "Why should we undo it?", "What did you expect to happen?", "What is this about?"):
        if answers.get(key):
            text = str(answers[key])
            break
    else:
        text = str(next(iter(answers.values()), ""))
    text = " ".join(text.split())
    return text[:120] + ("..." if len(text) > 120 else "")


def _ticket_title(ticket):
    name = (ticket.get("display_name") or ticket.get("username") or "").strip()
    if ticket.get("anonymous") or not name:
        return ticket.get("anon_label") or f"Anonymous-{ticket.get('number', 0)}"
    return name


# Discord is the only authority on who is in the server, and every lookup below
# keys on the numeric user id — never on a nickname, a display name or a
# username that a member is free to change. The cache is consulted first because
# it is live (a role change lands in it the moment the gateway reports it), but
# a cache miss is never treated as an answer: we ask Discord outright.
_MISSING_MEMBER_TTL = 45
_missing_member_at = {}


def _fetch_guild_member(guild, uid):
    """Ask Discord about one id, remembering the misses so polling stays cheap.

    Returns (member, answered). `answered` is only True when Discord really
    replied — a bot that is down or unreachable must never be mistaken for
    "this account is not in the server".
    """
    key = str(uid)
    now = time.monotonic()
    seen = _missing_member_at.get(key)
    if seen is not None and now - seen < _MISSING_MEMBER_TTL:
        return None, True
    if bot is None or getattr(bot, "loop", None) is None:
        return None, False
    loop = bot.loop
    if loop.is_closed() or not loop.is_running():
        return None, False
    try:
        member = asyncio.run_coroutine_threadsafe(guild.fetch_member(int(uid)), loop).result(timeout=6)
    except discord.NotFound:
        member = None
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) != 404:
            return None, False
        member = None
    except (TypeError, ValueError):
        member = None
    except Exception:
        return None, False
    if member is None:
        _missing_member_at[key] = now
        return None, True
    _missing_member_at.pop(key, None)
    return member, True


def _supabase_member(user_id):
    try:
        return fetch_member(str(user_id)) or None
    except Exception:
        return None


def _roles_of_member(user_id):
    """(role ids, role names) for a Discord member, empty when unknown.

    Highest role first, because that is the order the access check needs: the
    member's most senior role is the one that decides what they may do.
    """
    # Cache only: this runs once per ticket reply while listing a thread, and a
    # blocking Discord call per reply would make the list crawl.
    ctx = _member_context(user_id, deep=False)
    return ctx["role_ids"], ctx["role_names"]


def _member_context(user_id, deep=True):
    """Everything the admin panel needs to judge one Discord account.

    `in_guild` is only reported as False when something actually told us so —
    either Discord's API or the synced member table. `checked` says whether the
    answer is trustworthy at all, so the panel can say "couldn't verify" instead
    of wrongly accusing a member of not belonging to their own server.
    """
    ctx = {
        "in_guild": False, "role_ids": [], "role_names": [], "labels": {},
        "member": None, "checked": False, "source": "none", "live": False,
    }
    uid = str(user_id or "").strip()
    if not uid.isdigit():
        return ctx

    guild = _main_guild()
    gm = None
    if guild is not None:
        gm = guild.get_member(int(uid))
        if gm is not None:
            ctx["source"] = "cache"
        elif deep:
            gm, asked = _fetch_guild_member(guild, uid)
            if asked:
                ctx["source"] = "discord" if gm is not None else "none"
                ctx["checked"] = True

    if gm is None and deep and not ctx["checked"]:
        # Discord could not be reached, so fall back to the synced member table.
        # It is keyed on the same user id and carries role *names*, which the
        # keyword matcher can still use. A definitive "not a member" from
        # Discord is never overridden by a row that may not have been pruned yet.
        row = _supabase_member(uid)
        if row:
            if guild is None:
                ctx["source"] = "supabase"
            ctx["in_guild"] = True
            ctx["checked"] = True
            names = [str(r.get("name") or "") for r in (row.get("roles") or []) if isinstance(r, dict)]
            ctx["role_names"] = [n for n in names if n]
            if guild is not None:
                by_name = {r.name: str(r.id) for r in guild.roles}
                ctx["role_ids"] = [by_name[n] for n in ctx["role_names"] if n in by_name]
                ctx["labels"] = {rid: n for n, rid in by_name.items() if n in ctx["role_names"]}
            return ctx
        return ctx

    # Being a member is separate from holding roles: someone in the server with
    # no roles gets "no permissions", not "not in this server".
    ctx["in_guild"] = True
    ctx["checked"] = True
    ctx["live"] = True
    # getattr, not gm.roles: the cache can hand back a plain User when only the
    # account (not a guild member) is known, and that has no roles at all.
    own_roles = getattr(gm, "roles", None)
    if not own_roles:
        return ctx
    roles = sorted((r for r in own_roles if not r.is_default()), key=lambda r: r.position, reverse=True)
    ctx["role_ids"] = [str(r.id) for r in roles]
    ctx["role_names"] = [r.name for r in roles]
    ctx["labels"] = {str(r.id): r.name for r in roles}
    ctx["member"] = gm
    return ctx


def _replies_public(ticket):
    """Ticket replies with their author's staff badge resolved.

    Old replies have no stored author id, so they simply come back without a
    badge. The owner of an anonymous ticket never gets one either: a badge
    would out the person the ticket was meant to hide.
    """
    raw = ticket.get("replies") or []
    if not raw:
        return []
    titles = _load_titles()
    anonymous = bool(ticket.get("anonymous"))
    out = []
    for reply in raw:
        item = dict(reply) if isinstance(reply, dict) else {"body": str(reply)}
        if anonymous and not item.get("staff"):
            item.pop("author_id", None)
            item["staff_title"] = None
        else:
            uid = item.get("author_id")
            item["staff_title"] = _member_title(*_roles_of_member(uid), titles=titles) if uid else None
        out.append(item)
    return out


def _ticket_public(ticket):
    return {
        "id": ticket.get("id"),
        "number": ticket.get("number"),
        "title": _ticket_title(ticket),
        "excerpt": _ticket_excerpt(ticket),
        "anonymous": bool(ticket.get("anonymous")),
        "anon_label": ticket.get("anon_label") or "",
        "contact": ticket.get("contact") or "",
        "assigned_to": ticket.get("assigned_to") or None,
        "participants": ticket.get("participants") or {"users": [], "roles": []},
        "category": ticket.get("category"),
        "category_label": ticket.get("category_label"),
        "subject": ticket.get("subject"),
        "priority": ticket.get("priority"),
        "status": ticket.get("status", "open"),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
        "answers": ticket.get("answers") or {},
        "replies": _replies_public(ticket),
        "attachments": ticket.get("attachments") or [],
        "username": ticket.get("username"),
        "display_name": ticket.get("display_name"),
        # The author's id, so the dashboard can show exactly one member's
        # tickets from their profile. It is a staff-only view either way.
        "user_id": str(ticket.get("user_id") or ""),
        # Anonymous tickets never carry a face, so the dashboard can fall back
        # to its initial placeholder without risking an identity leak.
        "avatar_url": "" if ticket.get("anonymous") else (ticket.get("avatar_url") or ""),
    }


def _notify_staff(ticket):
    """Post the new ticket in the staff channel. Never breaks the request."""
    if not TICKET_CHANNEL_ID or bot is None or bot.loop is None:
        return

    async def send():
        channel = bot.get_channel(int(TICKET_CHANNEL_ID))
        if channel is None:
            return
        color = {"high": 0xC98A6E, "low": 0x8FA88B}.get(ticket.get("priority"), 0x6EA8C9)
        embed = discord.Embed(
            title=f"Ticket {ticket['id']} - {ticket['category_label']}",
            description=ticket.get("subject") or "",
            color=color,
        )
        who = f"@{ticket.get('username') or 'unknown'}"
        if ticket.get("anonymous"):
            origin = "Anonymous member"
            if ticket.get("contact"):
                origin += f"\nContact: {ticket['contact']}"
        else:
            origin = f"{ticket.get('display_name') or who}\n`{ticket['user_id']}`"
        embed.add_field(name="From", value=origin[:1024], inline=True)
        embed.add_field(name="Urgency", value=ticket.get("priority", "normal"), inline=True)
        embed.add_field(name="Status", value=ticket.get("status", "open"), inline=True)
        for label, value in list(ticket.get("answers", {}).items())[:12]:
            embed.add_field(name=str(label)[:256], value=str(value)[:1024], inline=False)
        shots = ticket.get("attachments") or []
        if shots:
            embed.add_field(name=f"Attachments ({len(shots)})", value="\n".join(shots)[:1024], inline=False)
            try:
                embed.set_image(url=shots[0])
            except Exception:
                pass
        embed.set_footer(text="Opened from the member page")
        embed.timestamp = discord.utils.utcnow()
        ping = f"<@&{TICKET_PING_ROLE_ID}>" if TICKET_PING_ROLE_ID else None
        await channel.send(content=ping, embed=embed, allowed_mentions=discord.AllowedMentions(roles=True))

    try:
        _run(send(), timeout=15)
    except Exception as exc:
        print(f"[tickets] staff notify failed: {exc}")


@app.route("/api/tickets", methods=["GET"])
def api_tickets_list():
    user_id = _current_member_id({"user_id": request.args.get("user_id")})
    if not user_id.isdigit():
        return _fail("Log in with Discord to see your tickets.", 403)
    with TICKET_LOCK:
        rows = _load_tickets()
    mine = [t for t in rows if str(t.get("user_id")) == user_id]
    mine.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    open_count = sum(1 for t in mine if t.get("status") != "closed")
    return jsonify({
        "status": "ok",
        "open_count": open_count,
        "tickets": [_ticket_public(t) for t in mine],
    })


@app.route("/api/tickets", methods=["POST"])
def api_ticket_create():
    data = request.json or {}
    member = _current_member()
    user_id = str(member.get("user_id") or data.get("user_id") or "").strip()
    anonymous = not (user_id.isdigit() and len(user_id) >= 15)
    if anonymous:
        user_id = ""

    category = str(data.get("category") or "").strip()
    if category not in TICKET_CATEGORIES:
        return _fail("Pick a topic for your ticket.")
    if category == "account" and anonymous:
        return _fail("Account Support needs a Discord login first.", 403)

    raw_answers = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    answers = {}
    for key, value in list(raw_answers.items())[:40]:
        text = str(value or "").strip()[:2000]
        if text:
            answers[str(key)[:80]] = text
    if not answers:
        return _fail("Please write something before sending.")

    attachments, attach_error = _clean_attachments(data.get("attachments"))
    if attach_error:
        return _fail(attach_error)

    priority = str(data.get("priority") or "normal").strip().lower()
    if priority not in ("low", "normal", "high"):
        priority = "normal"

    now = _utc_now()
    with TICKET_LOCK:
        rows = _load_tickets()
        mine_open = [t for t in rows if str(t.get("user_id")) == user_id and t.get("status") != "closed"]
        if not anonymous and len(mine_open) >= MAX_OPEN_TICKETS:
            return _fail(f"You already have {MAX_OPEN_TICKETS} open tickets. Please wait for a reply.")
        number = max([int(t.get("number") or 0) for t in rows] or [0]) + 1
        ticket = {
            "id": f"T{number:04d}",
            "number": number,
            "user_id": user_id,
            "anonymous": anonymous,
            "anon_label": f"Anonymous-{random.randint(100, 9999)}" if anonymous else "",
            "assigned_to": None,
            "participants": {"users": [], "roles": []},
            "contact": str(data.get("contact") or "").strip()[:200],
            "username": str(member.get("username") or data.get("username") or "")[:80],
            "display_name": str(member.get("display_name") or data.get("display_name") or "")[:80],
            "avatar_url": str(member.get("avatar_url") or data.get("avatar_url") or "")[:400],
            "category": category,
            "category_label": TICKET_CATEGORIES[category],
            "subject": str(data.get("subject") or "").strip()[:120] or TICKET_CATEGORIES[category],
            "priority": priority,
            "answers": answers,
            "attachments": attachments,
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "replies": [],
        }
        rows.append(ticket)
        _save_tickets(rows)

    _notify_staff(ticket)
    return jsonify({"status": "ok", "ticket": _ticket_public(ticket)})


@app.route("/api/tickets/<ticket_id>/reply", methods=["POST"])
def api_ticket_reply(ticket_id):
    data = request.json or {}
    user_id = _current_member_id(data)
    body = str(data.get("body") or "").strip()[:2000]
    if not body and not data.get("attachments"):
        return _fail("Write a message first.")

    attachments, attach_error = _clean_attachments(data.get("attachments"))
    if attach_error:
        return _fail(attach_error)

    member = _current_member() or {}
    with TICKET_LOCK:
        rows = _load_tickets()
        ticket = next((t for t in rows if t.get("id") == ticket_id), None)
        if ticket is None:
            return _fail("We could not find that ticket.", 404)
        if ticket.get("anonymous") or not user_id:
            return _fail("This ticket was sent without a login, so staff answer it in Discord.", 403)
        if str(ticket.get("user_id")) != user_id:
            return _fail("That ticket is not yours.", 403)
        if ticket.get("status") == "closed":
            return _fail("This ticket is closed. Open a new one if you still need help.")
        now = _utc_now()
        # The avatar rides along on every reply so both ticket views can show the
        # face next to the name without a second lookup.
        ticket.setdefault("replies", []).append({
            "author": member.get("display_name") or ticket.get("display_name") or ticket.get("username") or "Member",
            "author_id": member.get("user_id") or user_id,
            "staff": False,
            "avatar_url": member.get("avatar_url") or "",
            "body": body,
            "attachments": attachments,
            "at": now,
        })
        if ticket.get("status") == "answered":
            ticket["status"] = "open"
        ticket["updated_at"] = now
        _save_tickets(rows)

    return jsonify({"status": "ok", "ticket": _ticket_public(ticket)})


@app.route("/api/tickets/<ticket_id>/close", methods=["POST"])
def api_ticket_close(ticket_id):
    data = request.json or {}
    user_id = _current_member_id(data)
    with TICKET_LOCK:
        rows = _load_tickets()
        ticket = next((t for t in rows if t.get("id") == ticket_id), None)
        if ticket is None:
            return _fail("We could not find that ticket.", 404)
        if ticket.get("anonymous") or not user_id:
            return _fail("This ticket was sent without a login, so only staff can close it.", 403)
        if str(ticket.get("user_id")) != user_id:
            return _fail("That ticket is not yours.", 403)
        ticket["status"] = "closed"
        ticket["updated_at"] = _utc_now()
        _save_tickets(rows)

    return jsonify({"status": "ok", "ticket": _ticket_public(ticket)})


# ==========================================================
# STAFF TICKET CONSOLE (dashboard "View Tickets")
# ==========================================================

def _ticket_access(ticket, owner_key=""):
    """Who may read and answer this ticket: (allowed, reason).

    The mod who accepted it, anyone invited to it, anyone holding an
    invited role, and the owner (owner key from the owner panel).
    """
    if owner_key and owner_key == OWNER_KEY:
        return True, "owner"
    member = _current_member()
    if not member:
        return False, "login"
    uid = str(member.get("user_id"))
    assigned = str((ticket.get("assigned_to") or {}).get("user_id") or "")
    if uid == assigned:
        return True, "assigned"
    parts = ticket.get("participants") or {}
    users = parts.get("users") or []
    roles = parts.get("roles") or []
    if any(str(u.get("id")) == uid for u in users):
        return True, "invited"
    if roles and bot is not None and GUILD_ID:
        guild = bot.get_guild(int(GUILD_ID))
        member_obj = guild.get_member(int(uid)) if guild else None
        if member_obj:
            mine = {str(r.id) for r in member_obj.roles}
            if mine & {str(r.get("id")) for r in roles}:
                return True, "role"
    return False, "locked"


def _find_ticket(rows, ticket_id):
    return next((t for t in rows if t.get("id") == ticket_id), None)


@app.route("/api/staff/tickets", methods=["GET"])
def api_staff_tickets():
    with TICKET_LOCK:
        rows = _load_tickets()
    rows.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    tickets = [_ticket_public(t) for t in rows]
    counts = {
        "all": len(tickets),
        "open": sum(1 for t in tickets if t.get("status") == "open"),
        "answered": sum(1 for t in tickets if t.get("status") == "answered"),
        "closed": sum(1 for t in tickets if t.get("status") == "closed"),
        "anonymous": sum(1 for t in tickets if t.get("anonymous")),
    }
    member = _current_member()
    return jsonify({
        "status": "ok",
        "tickets": tickets,
        "counts": counts,
        "staff": member or None,
        "logged_in": bool(member),
        "oauth_ready": _oauth_ready(),
    })


@app.route("/api/staff/tickets/<ticket_id>/accept", methods=["POST"])
def api_staff_ticket_accept(ticket_id):
    member = _current_member()
    if not member:
        return _fail("Log in with Discord first.", 403)
    with TICKET_LOCK:
        rows = _load_tickets()
        ticket = _find_ticket(rows, ticket_id)
        if ticket is None:
            return _fail("Ticket not found.", 404)
        owner = (ticket.get("assigned_to") or {}).get("user_id")
        if owner and str(owner) != str(member["user_id"]):
            return _fail("Another mod already accepted this ticket.")
        ticket["assigned_to"] = {
            "user_id": str(member["user_id"]),
            "name": member.get("display_name") or member.get("username") or "Staff",
            "avatar_url": member.get("avatar_url") or "",
        }
        if ticket.get("status") == "closed":
            ticket["status"] = "open"
        ticket["updated_at"] = _utc_now()
        _save_tickets(rows)

    return jsonify({"status": "ok", "ticket": _ticket_public(ticket)})


@app.route("/api/staff/tickets/<ticket_id>/invite", methods=["POST"])
def api_staff_ticket_invite(ticket_id):
    data = request.json or {}
    owner_key = str(data.get("owner_key") or "")
    with TICKET_LOCK:
        rows = _load_tickets()
        ticket = _find_ticket(rows, ticket_id)
        if ticket is None:
            return _fail("Ticket not found.", 404)
        allowed, _ = _ticket_access(ticket, owner_key)
        if not allowed:
            return _fail("Accept this ticket first, then you can invite others.", 403)

        parts = ticket.setdefault("participants", {"users": [], "roles": []})
        users = parts.setdefault("users", [])
        roles = parts.setdefault("roles", [])
        known_users = {str(u.get("id")) for u in users}
        known_roles = {str(r.get("id")) for r in roles}

        for entry in (data.get("users") or [])[:50]:
            uid = str((entry or {}).get("id") or "").strip()
            if not uid or uid in known_users:
                continue
            users.append({"id": uid, "name": str((entry or {}).get("name") or uid)[:80]})
            known_users.add(uid)
        for entry in (data.get("roles") or [])[:50]:
            rid = str((entry or {}).get("id") or "").strip()
            if not rid or rid in known_roles:
                continue
            roles.append({
                "id": rid,
                "name": str((entry or {}).get("name") or rid)[:80],
                "color": str((entry or {}).get("color") or ""),
            })
            known_roles.add(rid)

        ticket["updated_at"] = _utc_now()
        _save_tickets(rows)

    return jsonify({"status": "ok", "ticket": _ticket_public(ticket)})


@app.route("/api/staff/tickets/<ticket_id>/staff-reply", methods=["POST"])
def api_staff_ticket_reply(ticket_id):
    data = request.json or {}
    body = str(data.get("body") or "").strip()[:2000]
    owner_key = str(data.get("owner_key") or "")
    if not body and not data.get("attachments"):
        return _fail("Write a message first.")

    attachments, attach_error = _clean_attachments(data.get("attachments"))
    if attach_error:
        return _fail(attach_error)

    with TICKET_LOCK:
        rows = _load_tickets()
        ticket = _find_ticket(rows, ticket_id)
        if ticket is None:
            return _fail("Ticket not found.", 404)
        allowed, _ = _ticket_access(ticket, owner_key)
        if not allowed:
            return _fail("Accept this ticket first, then you can talk to them.", 403)
        member = _current_member() or {}
        now = _utc_now()
        ticket.setdefault("replies", []).append({
            "author": member.get("display_name") or member.get("username") or "Staff",
            "author_id": member.get("user_id") or "",
            "staff": True,
            "avatar_url": member.get("avatar_url") or "",
            "body": body,
            "attachments": attachments,
            "at": now,
        })
        ticket["status"] = "answered"
        ticket["updated_at"] = now
        _save_tickets(rows)

    return jsonify({"status": "ok", "ticket": _ticket_public(ticket)})


@app.route("/api/staff/tickets/<ticket_id>/close", methods=["POST"])
def api_staff_ticket_close(ticket_id):
    data = request.json or {}
    with TICKET_LOCK:
        rows = _load_tickets()
        ticket = _find_ticket(rows, ticket_id)
        if ticket is None:
            return _fail("Ticket not found.", 404)
        allowed, _ = _ticket_access(ticket, str(data.get("owner_key") or ""))
        if not allowed:
            return _fail("Accept this ticket first, then you can close it.", 403)
        ticket["status"] = "closed"
        ticket["updated_at"] = _utc_now()
        _save_tickets(rows)

    return jsonify({"status": "ok", "ticket": _ticket_public(ticket)})


@app.route("/api/staff/tickets/<ticket_id>/delete", methods=["POST"])
def api_staff_ticket_delete(ticket_id):
    data = request.json or {}
    with TICKET_LOCK:
        rows = _load_tickets()
        ticket = _find_ticket(rows, ticket_id)
        if ticket is None:
            return _fail("Ticket not found.", 404)
        allowed, _ = _ticket_access(ticket, str(data.get("owner_key") or ""))
        if not allowed:
            return _fail("Accept this ticket first, then you can delete it.", 403)
        rows = [t for t in rows if t.get("id") != ticket_id]
        _save_tickets(rows)

    return jsonify({"status": "ok", "deleted": ticket_id})


# ==========================================================
# MEMBER DIRECTORY, LIVE STATUS, PERSONAL DASHBOARD
# ==========================================================

def _main_guild():
    if bot is None or bot.user is None:
        return None
    if GUILD_ID:
        guild = bot.get_guild(int(GUILD_ID))
        if guild:
            return guild
    return bot.guilds[0] if bot.guilds else None


# ==========================================================
# MEMBER PROFILE CUSTOMISATION (bio, name styling, list visibility)
# ==========================================================

PROFILE_PATH = os.path.join(_HERE, "..", "logging", "member_profiles.json")
PROFILE_LOCK = threading.Lock()
NAME_EFFECTS = {"none", "glow", "gradient", "rainbow"}


def _load_profiles():
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_profiles(data):
    tmp = PROFILE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, PROFILE_PATH)


def _clean_color(raw):
    value = str(raw or "").strip()[:160]
    if not value:
        return ""
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
        return value.lower()
    # up to four hex stops, comma separated, for gradients
    stops = [s.strip() for s in value.split(",")]
    if 2 <= len(stops) <= 4 and all(re.fullmatch(r"#[0-9a-fA-F]{3,8}", s) for s in stops):
        return ",".join(s.lower() for s in stops)
    return ""


def _clean_font(raw):
    value = str(raw or "").strip()[:60]
    return value if re.fullmatch(r"[A-Za-z0-9 '\-]+", value) else ""


@app.route("/api/me/profile", methods=["GET", "POST"])
def api_me_profile():
    me = _current_member()
    if not me:
        return _fail("Log in with Discord first.", 403)
    uid = str(me["user_id"])

    if request.method == "GET":
        with PROFILE_LOCK:
            rows = _load_profiles()
        return jsonify({"status": "ok", "profile": rows.get(uid) or {}})

    data = request.json or {}
    with PROFILE_LOCK:
        rows = _load_profiles()
        profile = rows.get(uid) or {}

        if "bio" in data:
            profile["bio"] = str(data.get("bio") or "").strip()[:300]
        if "show_nickname" in data:
            profile["show_nickname"] = bool(data.get("show_nickname"))
        if "name_color" in data:
            profile["name_color"] = _clean_color(data.get("name_color"))
        if "name_font" in data:
            profile["name_font"] = _clean_font(data.get("name_font"))
        if "name_effect" in data:
            effect = str(data.get("name_effect") or "none").strip().lower()
            profile["name_effect"] = effect if effect in NAME_EFFECTS else "none"
        profile["updated_at"] = _utc_now()

        rows[uid] = profile
        _save_profiles(rows)

    return jsonify({"status": "ok", "profile": profile})


@app.route("/api/status", methods=["GET"])
def api_status():
    """Small public health snapshot for the member page status widget."""
    guild = _main_guild()
    if guild is None:
        return jsonify({"status": "ok", "online": False})

    ping = 0 if (math.isnan(bot.latency) or math.isinf(bot.latency)) else round(bot.latency * 1000)
    try:
        online = sum(1 for m in guild.members if m.status is not discord.Status.offline)
    except Exception:
        online = 0
    try:
        proc = psutil.Process(os.getpid())
        memory_mb = round(proc.memory_info().rss / 1024 / 1024, 1)
        cpu = round(proc.cpu_percent(interval=None), 1)
    except Exception:
        memory_mb, cpu = 0, 0

    return jsonify({
        "status": "ok",
        "online": True,
        "ping": ping,
        "uptime": int(time.time() - BOT_START),
        "members": guild.member_count or len(guild.members),
        "online_members": online,
        "cpu": cpu,
        "memory_mb": memory_mb,
    })


@app.route("/api/directory", methods=["GET"])
def api_directory():
    """Trimmed member list for the public Members page."""
    guild = _main_guild()
    with PROFILE_LOCK:
        profiles = _load_profiles()
    if guild is None:
        return jsonify({"status": "ok", "members": [], "count": 0})

    titles = _load_titles()
    rows = []
    for member in guild.members[:MEMBER_LIST_CAP]:
        roles = [
            {
                "id": str(role.id),
                "name": role.name,
                "color": _color_hex(role.color),
                "tier": role.position,
            }
            for role in sorted(member.roles, key=lambda r: r.position, reverse=True)
            if not role.is_default()
        ]
        prof = profiles.get(str(member.id)) or {}
        show_nick = prof.get("show_nickname")
        display = member.display_name if show_nick is not False else member.name
        rows.append({
            "user_id": str(member.id),
            "username": member.name,
            "display_name": display,
            "avatar_url": member.display_avatar.url if member.display_avatar else "",
            "bio": prof.get("bio", ""),
            "name_color": prof.get("name_color", ""),
            "name_font": prof.get("name_font", ""),
            "name_effect": prof.get("name_effect", ""),
            "show_nickname": show_nick is not False,
            "staff_title": _member_title([r["id"] for r in roles], [r["name"] for r in roles], titles),
            "bot": bool(member.bot),
            "joined_at": member.joined_at.isoformat() if member.joined_at else None,
            "created_at": member.created_at.isoformat() if member.created_at else None,
            "roles": roles,
        })
    return jsonify({"status": "ok", "members": rows, "count": len(rows)})


@app.route("/api/staff-titles", methods=["GET"])
def api_staff_titles():
    """The staff badge rules, so the member page can draw them itself.

    Display-only data: a label, how it looks, and which roles or role names
    claim it. No dashboard settings ride along.
    """
    titles = [
        {**_title_public(t), "roles": t.get("roles") or [],
         "keywords": t.get("keywords") or [], "priority": t.get("priority") or 0}
        for t in _load_titles()
    ]
    return jsonify({"status": "ok", "titles": titles})


@app.route("/api/me/history", methods=["GET"])
def api_me_history():
    """Personal record for the logged-in member: actions, notes and tickets."""
    me = _current_member()
    if not me:
        return _fail("Log in with Discord first.", 403)
    guild = _main_guild()
    if guild is None:
        return _fail("The bot is not connected yet.", 503)
    try:
        uid = int(me["user_id"])
    except (TypeError, ValueError):
        return _fail("We could not read your user ID.", 400)

    tracked = history_tracker.get(guild.id, uid) or {}
    counts = {
        "warns": tracked.get("warns", 0),
        "kicks": tracked.get("kicks", 0),
        "bans": tracked.get("bans", 0),
        "timeouts": tracked.get("timeouts", 0),
        "softbans": tracked.get("softbans", 0),
    }

    actions = []
    for entry in (tracked.get("actions") or [])[::-1]:
        actions.append({
            "action": entry.get("action", "note"),
            "reason": entry.get("reason") or "No reason recorded",
            "timestamp": entry.get("timestamp", ""),
            "moderator": _resolve_actor(guild, entry.get("moderator_id")),
        })

    notes = []
    for note in notes_store.list(guild.id, uid):
        notes.append({
            "text": note.get("text", ""),
            "author": _resolve_actor(guild, note.get("author_id")),
            "timestamp": note.get("timestamp", ""),
        })

    tickets = []
    with TICKET_LOCK:
        rows = _load_tickets()
    for ticket in rows:
        if str(ticket.get("user_id")) != str(uid):
            continue
        tickets.append({
            "id": ticket.get("id"),
            "subject": ticket.get("subject") or ticket.get("category_label") or "Ticket",
            "status": ticket.get("status", "open"),
            "created_at": ticket.get("created_at", ""),
            "category_label": ticket.get("category_label", "Support"),
        })
    tickets.sort(key=lambda t: t.get("created_at") or "", reverse=True)

    banned = False
    if guild.me and guild.me.guild_permissions.ban_members:
        async def _check_ban():
            try:
                await guild.fetch_ban(discord.Object(id=uid))
                return True
            except Exception:
                return False
        try:
            banned = bool(_run(_check_ban(), timeout=10))
        except Exception:
            banned = False

    member_obj = guild.get_member(uid)
    return jsonify({
        "status": "ok",
        "counts": counts,
        "actions": actions,
        "notes": notes,
        "tickets": tickets,
        "banned": banned,
        "in_guild": member_obj is not None,
        "joined_at": member_obj.joined_at.isoformat() if member_obj and member_obj.joined_at else None,
        "account_created": member_obj.created_at.isoformat() if member_obj else None,
        "timed_out_until": (
            member_obj.timed_out_until.isoformat()
            if member_obj and member_obj.timed_out_until else None
        ),
    })


