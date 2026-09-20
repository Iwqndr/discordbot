import os
import json
import datetime
import urllib.request
import urllib.error
from dotenv import load_dotenv

from config import MONEY_MAX

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")


def _request(method, path, data=None, use_service=True, extra_headers=None, timeout=10):
    key = SUPABASE_SERVICE_KEY if use_service else SUPABASE_ANON_KEY
    if not SUPABASE_URL or not key:
        raise RuntimeError("Supabase not configured")

    url = f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    if extra_headers:
        headers.update(extra_headers)

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return e.code, raw
    except Exception as e:
        return 0, str(e)


def increment_command_usage(command_name):
    status, _ = _request(
        "POST",
        "rpc/increment_command_usage",
        data={"cmd": command_name},
        use_service=True,
    )
    return 200 <= status < 300


# ---------------------------------------------------------------------------
# MEMBER ROSTER
# ---------------------------------------------------------------------------
# The `members` table is the mirror the public member page draws: names, avatars,
# banners and roles, one row per person. It used to be fed by every server the
# bot is in at once, so a nickname change in one of them rewrote the row the
# other one was showing — which is why the page blended two servers together.
#
# Every row now carries the `guild_id` it came from, and only the server picked
# in Owner Panel > Member Management > Member site server is mirrored (see
# bot.py). Readers filter on that value; rows written before the column existed
# carry no value and are simply rebuilt by the next roster sync.

# `members.guild_id` arrives with section 8 of pages/schema.sql. Until that has
# been run, PostgREST rejects any row that mentions it, so the first rejection
# latches this off and the roster keeps syncing without the stamp instead of
# failing on every write.
_MEMBERS_GUILD_SUPPORTED = True
_GUILD_COLUMN_WARNED = False


def members_guild_supported():
    """Whether this Supabase project has `members.guild_id` yet."""
    return _MEMBERS_GUILD_SUPPORTED


def _marks_missing_guild_column(status, raw):
    text = str(raw or "")
    return 400 <= status < 500 and "guild_id" in text and (
        "PGRST204" in text or "column" in text.lower() or "schema cache" in text.lower()
    )


def upsert_members(rows):
    global _MEMBERS_GUILD_SUPPORTED, _GUILD_COLUMN_WARNED
    if not rows:
        return 200, "no rows"

    payload = rows
    if not _MEMBERS_GUILD_SUPPORTED:
        payload = [{k: v for k, v in row.items() if k != "guild_id"} for row in rows]

    status, raw = _request(
        "POST",
        "members",
        data=payload,
        use_service=True,
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )

    if _MEMBERS_GUILD_SUPPORTED and _marks_missing_guild_column(status, raw):
        # One warning, then carry on without the stamp rather than dropping every
        # member write until somebody runs the SQL.
        _MEMBERS_GUILD_SUPPORTED = False
        if not _GUILD_COLUMN_WARNED:
            _GUILD_COLUMN_WARNED = True
            print(
                "[supabase] members.guild_id is missing — run pages/schema.sql to "
                "let the member page show a single server."
            )
        payload = [{k: v for k, v in row.items() if k != "guild_id"} for row in rows]
        return _request(
            "POST",
            "members",
            data=payload,
            use_service=True,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )

    return status, raw


def delete_member(user_id):
    return _request("DELETE", f"members?user_id=eq.{user_id}", use_service=True)


def delete_members_outside_guild(guild_id):
    """Drop every row that did not come from `guild_id` — including the rows that
    predate the column and therefore carry no server at all.

    Used when the member page is pointed at a different server, so the mirror
    holds exactly one roster instead of a blend. Returns (ok, removed, error);
    `removed` is what PostgREST reports back, so it is 0 on older setups where
    the column does not exist yet — nothing is deleted in that case.
    """
    gid = str(guild_id or "").strip()
    if not gid:
        return False, 0, "No server to keep."
    status, raw = _request(
        "DELETE",
        f"members?or=(guild_id.is.null,guild_id.neq.{gid})",
        use_service=True,
        extra_headers={"Prefer": "return=representation"},
    )
    if not (200 <= status < 300):
        return False, 0, f"Supabase rejected the cleanup ({status}): {str(raw)[:160]}"
    try:
        return True, len(json.loads(raw)), ""
    except Exception:
        return True, 0, ""


def fetch_member(user_id):
    """Return the stored members row for a user, or None if unavailable."""
    try:
        status, raw = _request("GET", f"members?user_id=eq.{user_id}&limit=1", use_service=True)
    except RuntimeError:
        return None
    if status != 200:
        return None
    try:
        rows = json.loads(raw)
        return rows[0] if rows else None
    except Exception:
        return None


def fetch_stored_banners():
    """`{user_id: banner_url}` for every member that has a banner stored.

    Read before a member row is written, because Discord only returns a banner for
    accounts with Nitro: without this, the empty value it gives for everybody else
    would overwrite a banner somebody set by hand every time they were synced.
    """
    out = {}
    offset = 0
    while offset < 100000:
        try:
            status, raw = _request(
                "GET",
                "members?select=user_id,banner_url&banner_url=not.is.null"
                f"&limit=1000&offset={offset}",
                use_service=True,
            )
        except Exception:
            # Not configured, DNS, TLS: a caller only wants the banners it can get,
            # and a push must not fail because this lookup did.
            return out
        if status != 200:
            return out
        try:
            rows = json.loads(raw)
        except Exception:
            return out
        if not rows:
            break
        for row in rows:
            uid = str(row.get("user_id") or "")
            banner = str(row.get("banner_url") or "").strip()
            if uid and banner:
                out[uid] = banner
        if len(rows) < 1000:
            break
        offset += 1000
    return out


def fetch_all_member_ids(guild_id=None):
    """Every mirrored member id, optionally only those from one server.

    The guild filter is what keeps `>syncmembers` in one server from deleting the
    other server's rows; without it a sync was a "everyone else left" sweep.
    """
    path = "members?select=user_id"
    gid = str(guild_id or "").strip()
    if gid and _MEMBERS_GUILD_SUPPORTED:
        path += f"&guild_id=eq.{gid}"
    status, raw = _request("GET", path, use_service=True)
    if status != 200:
        return set()
    try:
        return {row["user_id"] for row in json.loads(raw)}
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# PANEL SETTINGS
# ---------------------------------------------------------------------------
# One row per panel-wide setting. The admin panel writes with the service key;
# the member page reads `hub_guild` with the anon key to know which server it is
# showing, which is why anon has SELECT (see section 9 of pages/schema.sql).

HUB_GUILD_KEY = "hub_guild"


def fetch_hub_setting():
    """The server the member page shows, as stored by the panel.

    Returns (value, error). `value` is None when nothing has been picked yet,
    which is also what every caller treats as "leave the page unscoped".
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None, "Supabase is not configured."
    try:
        status, raw = _request(
            "GET",
            f"panel_settings?key=eq.{HUB_GUILD_KEY}&select=value&limit=1",
            use_service=True,
        )
    except RuntimeError as e:
        return None, str(e)
    if status != 200:
        return None, (
            f"Supabase could not be read ({status}). "
            "If the table is missing, run pages/schema.sql."
        )
    try:
        rows = json.loads(raw)
    except Exception as e:
        return None, f"Supabase sent something unreadable: {e}"
    if not rows:
        return None, ""
    value = rows[0].get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None, ""
    return (value if isinstance(value, dict) else None), ""


def save_hub_setting(value):
    """Publish (or clear, with None) which server the member page shows.

    Returns (ok, error).
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return False, "Supabase is not configured."
    data = value if isinstance(value, dict) else {}
    status, raw = _request(
        "POST",
        f"panel_settings?on_conflict=key",
        data={
            "key": HUB_GUILD_KEY,
            "value": data,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        use_service=True,
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if not (200 <= status < 300):
        return False, (
            f"Supabase rejected the setting ({status}): {str(raw)[:160]}. "
            "Run pages/schema.sql if panel_settings does not exist yet."
        )
    return True, ""


# ---------------------------------------------------------------------------
# CURRENCY
# ---------------------------------------------------------------------------
# The `economy` table is the one place the member bot and this process can both
# reach, so it is the hand-off for a balance change: written here (the panel's
# Currency button), or by the admin bot's money commands, and read back by the
# member bot on its next pass — see referenced/wispcord/members.py.


def fetch_economy(user_id):
    """The stored economy row for a member, or None when it cannot be read."""
    try:
        status, raw = _request(
            "GET",
            f"economy?select=user_id,balance,bank,xp,level,wins,losses,streak"
            f"&user_id=eq.{user_id}&limit=1",
            use_service=True,
        )
    except RuntimeError:
        return None
    if status != 200:
        return None
    try:
        rows = json.loads(raw)
        return rows[0] if rows else None
    except Exception:
        return None


def add_currency(user_id, delta):
    """Add currency — a negative delta takes it away.

    Returns (ok, before, after, error). The balance is clamped to 0..MONEY_MAX,
    so a removal can empty a wallet but never puts anyone in debt, and nobody
    can be handed more than the ceiling.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return False, 0, 0, "Supabase is not configured, so currency cannot be changed."

    uid = str(user_id or "").strip()
    if not uid.isdigit():
        return False, 0, 0, "That member has no usable Discord id."

    try:
        delta = int(delta)
    except Exception:
        return False, 0, 0, "That amount is not a number."

    before = int((fetch_economy(uid) or {}).get("balance") or 0)
    after = max(0, min(MONEY_MAX, before + delta))

    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    status, raw = _request(
        "POST",
        "economy?on_conflict=user_id",
        data={"user_id": uid, "balance": after, "updated_at": stamp},
        use_service=True,
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if not (200 <= status < 300):
        return False, before, before, f"Supabase rejected the change ({status}): {raw[:160]}"
    return True, before, after, ""


# ---------------------------------------------------------------------------
# STAFF TITLES
# ---------------------------------------------------------------------------


def upsert_staff_titles(rows):
    """Mirror the panel's badge rules into `staff_titles`, for the member page.

    The dashboard keeps its own copy in `logging/dash_permissions.json`; the
    member page reads this table, which is why a badge used to show up in the
    panel and nowhere else. The list is the whole truth, so anything no longer in
    it is deleted as well.

    Returns (ok, error).
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return False, "Supabase is not configured."

    payload = []
    keep = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("id") or "").strip()
        label = str(row.get("label") or "").strip()
        if not tid or not label:
            continue
        keep.append(tid)
        payload.append({
            "id": tid,
            "label": label,
            "prefix": str(row.get("prefix") or ""),
            "suffix": str(row.get("suffix") or ""),
            "priority": int(row.get("priority") or 0),
            "roles": [str(r) for r in (row.get("roles") or [])],
            "keywords": [str(k) for k in (row.get("keywords") or [])],
            "style": row.get("style") if isinstance(row.get("style"), dict) else {},
        })

    if payload:
        status, raw = _request(
            "POST",
            "staff_titles?on_conflict=id",
            data=payload,
            use_service=True,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if not (200 <= status < 300):
            return False, f"Supabase rejected the titles ({status}): {raw[:160]}"

    # A badge deleted in the panel has to stop showing on the member page too,
    # so the mirror deletes whatever is no longer in the list.
    if keep:
        quoted = ",".join('"%s"' % tid.replace('"', "") for tid in keep)
        path = f"staff_titles?id=not.in.({quoted})"
    else:
        path = "staff_titles?id=not.is.null"
    status, raw = _request("DELETE", path, use_service=True)
    if not (200 <= status < 300):
        return False, f"Supabase would not drop the retired titles ({status}): {raw[:160]}"
    return True, ""