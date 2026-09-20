"""
Central configuration.

Every value here can be overridden with an environment variable (see
.env.example). The defaults are the IDs this deployment already used, so an
existing setup keeps working without any .env changes.
"""

import os

from dotenv import load_dotenv

from console import warn

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        warn(f"{name}={raw!r} is not a number, falling back to {default}")
        return default


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = _env_int("GUILD_ID", 0)
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX") or ">"

# Role that may publish the tiered command list, and the role verification grants.
POSTHELP_ROLE_ID = _env_int("POSTHELP_ROLE_ID", 1548496816989798400)
VERIFIED_ROLE_ID = _env_int("VERIFIED_ROLE_ID", 1548559332063313921)

# Categories inside the archive guild that hold the log channels.
MESSAGE_CATEGORY_ID = _env_int("MESSAGE_CATEGORY_ID", 1548581331175350363)
SYSTEM_CATEGORY_ID = _env_int("SYSTEM_CATEGORY_ID", 1548582743368147015)

# Support tickets from the member page (templates/member.html).
# TICKET_CHANNEL_ID is the channel the bot posts new tickets in.
# TICKET_PING_ROLE_ID is an optional role to ping for every new ticket.
TICKET_CHANNEL_ID = _env_int("TICKET_CHANNEL_ID", 0)
TICKET_PING_ROLE_ID = _env_int("TICKET_PING_ROLE_ID", 0)

# Discord login on the member page. Create an app at
# https://discord.com/developers/applications, then add
# DISCORD_REDIRECT_URI as an allowed redirect. Only "identify" is used,
# so the member page never sees anything except the name, tag and avatar.
DISCORD_CLIENT_ID = (os.getenv("DISCORD_CLIENT_ID") or "").strip()
DISCORD_CLIENT_SECRET = (os.getenv("DISCORD_CLIENT_SECRET") or "").strip()
DISCORD_REDIRECT_URI = (
    os.getenv("DISCORD_REDIRECT_URI") or "http://localhost:5000/auth/discord/callback"
).strip()

# Signs the member login cookie. Set your own value to keep logins valid
# across restarts.
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or "member-hub-change-this-secret"

# Catbox.moe hosting for ticket attachments. Server-side only: the userhash
# must never reach the browser.
CATBOX_USERHASH = (os.getenv("CATBOX_USERHASH") or "").strip()
CATBOX_API = "https://catbox.moe/user/api.php"
TICKET_ATTACH_MAX_FILES = _env_int("TICKET_ATTACH_MAX_FILES", 3)
TICKET_ATTACH_MAX_MB = _env_int("TICKET_ATTACH_MAX_MB", 5)

# Where "contact support" points in the DM a banned or kicked member gets.
# Point it at your invite or support server. When it is empty the same message
# is sent without a link, so a punishment DM is never built around a None URL.
SUPPORT_LINK = (os.getenv("SUPPORT_LINK") or "").strip()

# Names of the log channels created under those categories.
MESSAGE_LOG_PARENT_NAME = os.getenv("MESSAGE_LOG_PARENT_NAME") or "user-message-logs"
SYSTEM_LOG_PARENT_NAME = os.getenv("SYSTEM_LOG_PARENT_NAME") or "system-logs"

# ---------------------------------------------------------------------------
# The member bot (the sibling `wispcord/` project, deployed to Wispbyte)
# ---------------------------------------------------------------------------
# The member bot is a SEPARATE Discord application with its own token. It is
# never the admin bot, so it must never be handed DISCORD_TOKEN. Keep this empty
# here: the admin process only reads it to warn when the ticket panel's "create
# ticket" button points at a site URL that was never configured.
MEMBER_BOT_TOKEN = (os.getenv("MEMBER_BOT_TOKEN") or "").strip()

# Public address of the member page (the Cloudflare deployment). The ticket
# panel button builds `{MEMBER_SITE_URL}?open=support` from this, and the panel
# preview in the dashboard shows the same link, so both halves always agree.
MEMBER_SITE_URL = (os.getenv("MEMBER_SITE_URL") or "").strip().rstrip("/")

# Where Discord sends a panel login once it is approved.
#
# This is a value you register ONCE with Discord (OAuth2 -> Redirects), so it has
# to be an address that never changes. The panel's own address cannot be used:
# its tunnel address is new on every restart, and every restart would then need
# the Discord app edited to match. Pointing it at the member site's `/authorize`
# instead means the fixed address answers, then forwards the login on to whatever
# tunnel is live at that moment.
#
# Defaults to `<member site>/authorize`, which is what this deployment uses.
PANEL_AUTHORIZE_URI = (
    os.getenv("PANEL_AUTHORIZE_URI")
    or (f"{MEMBER_SITE_URL}/authorize" if MEMBER_SITE_URL else DISCORD_REDIRECT_URI)
).strip()

# A permanent address for the panel itself, if one is set up.
#
# The tunnel address changes on every restart, and because a hostname that is
# seconds old is answered "does not exist" by a lot of resolvers (mobile carriers
# and home routers especially), visitors on those networks get a blank page until
# their resolver catches up — while the machine hosting the panel works fine. The
# way out is to publish a fixed address that forwards to whatever tunnel is live,
# which is what `workers/panel-proxy.js` is: deploy that Worker, then put its
# address here. The panel then logs this address as the one to share, and the
# member site's /dashboard uses it too.
#
# Leave it empty and nothing changes: the raw tunnel address is used as before.
PANEL_PUBLIC_URL = (os.getenv("PANEL_PUBLIC_URL") or "").strip().rstrip("/")
