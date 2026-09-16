import os
import json
import urllib.request
import urllib.error
from dotenv import load_dotenv

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


def upsert_members(rows):
    if not rows:
        return 200, "no rows"
    return _request(
        "POST",
        "members",
        data=rows,
        use_service=True,
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )


def delete_member(user_id):
    return _request("DELETE", f"members?user_id=eq.{user_id}", use_service=True)


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


def fetch_all_member_ids():
    status, raw = _request("GET", "members?select=user_id", use_service=True)
    if status != 200:
        return set()
    try:
        return {row["user_id"] for row in json.loads(raw)}
    except Exception:
        return set()