"""Cloudflare tunnel for the admin panel.

`main.py` starts this alongside Flask, so the panel that only exists on this
machine becomes reachable at a public https:// address — which is what
`jamesheston.pages.dev/dashboard` redirects to.

Two ways to run:

* **Quick tunnel** (default, no account needed). `cloudflared tunnel --url
  http://localhost:5000` prints a random `https://<words>.trycloudflare.com`
  on every start. That address is published to Supabase so the Pages site can
  always find the current one.
* **Named tunnel** (needs a domain on Cloudflare). Set `CLOUDFLARE_TUNNEL_TOKEN`
  and the hostname is stable, so the redirect never has to change.

Everything here is best-effort: if cloudflared is missing or the tunnel fails,
the panel keeps working locally and the bot is unaffected.
"""

import os
import re
import subprocess
import sys
import threading
import time

from console import debug, error, info, warn

# The public address cloudflared prints, e.g. https://calm-river-1234.trycloudflare.com
_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com", re.I)

# Common install locations, checked when `cloudflared` is not on PATH. The
# Windows entry is the MSI default.
_CANDIDATE_PATHS = (
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
    "/usr/local/bin/cloudflared",
    "/usr/bin/cloudflared",
)

_process = None
_public_url = None
_lock = threading.Lock()


def find_cloudflared() -> str:
    """Absolute path to cloudflared, or "" when it is not installed."""
    from shutil import which

    found = which("cloudflared")
    if found:
        return found
    for path in _CANDIDATE_PATHS:
        if os.path.isfile(path):
            return path
    return ""


def public_url() -> str:
    with _lock:
        return _public_url or ""


def _publish(url: str) -> None:
    """Record the current address in Supabase so the Pages site can find it.

    Goes in `bot_status` (id 'tunnel') rather than a new table, so nobody has
    to run another migration.
    """
    with _lock:
        global _public_url
        _public_url = url

    try:
        import supabase_helper

        supabase_helper._request(
            "POST",
            "bot_status?on_conflict=id",
            data={"id": "tunnel", "version": url, "updated_at": _now()},
            extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        info(f"Dashboard tunnel published: {url}/admin")
    except Exception as exc:
        warn(f"Could not publish the tunnel address: {type(exc).__name__}: {exc}")


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _unpublish() -> None:
    with _lock:
        global _public_url
        _public_url = None
    try:
        import supabase_helper

        supabase_helper._request(
            "POST",
            "bot_status?on_conflict=id",
            data={"id": "tunnel", "version": "", "updated_at": _now()},
            extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
    except Exception:
        pass


def _read_output(process: subprocess.Popen) -> None:
    """Watch cloudflared's output for the address it was given."""
    stream = process.stderr or process.stdout
    if stream is None:
        return
    for raw in iter(stream.readline, ""):
        line = raw.strip()
        if not line:
            continue
        debug(f"cloudflared: {line}")
        match = _URL_RE.search(line)
        if match and not public_url():
            _publish(match.group(0))


def start(port: int) -> bool:
    """Start a quick tunnel to `http://localhost:<port>`.

    Returns False when the tunnel could not be started. A named tunnel is used
    when CLOUDFLARE_TUNNEL_TOKEN is set, otherwise a quick tunnel.
    """
    if os.getenv("DASHBOARD_TUNNEL", "1").strip().lower() in ("0", "false", "off", "no"):
        info("Dashboard tunnel: disabled (DASHBOARD_TUNNEL=0)")
        return False

    binary = find_cloudflared()
    if not binary:
        warn("Dashboard tunnel: cloudflared not found, so the panel stays local only.")
        return False

    token = os.getenv("CLOUDFLARE_TUNNEL_TOKEN", "").strip()
    if token:
        args = [binary, "tunnel", "--no-autoupdate", "run", "--token", token]
        # A named tunnel has a fixed hostname; publish it if we are told what it is.
        fixed = os.getenv("CLOUDFLARE_TUNNEL_URL", "").strip().rstrip("/")
        if fixed:
            _publish(fixed)
        info("Dashboard tunnel: starting a named tunnel.")
    else:
        args = [binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]
        info("Dashboard tunnel: starting a quick tunnel.")

    global _process
    try:
        _process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except Exception as exc:
        warn(f"Dashboard tunnel could not start: {type(exc).__name__}: {exc}")
        return False

    threading.Thread(target=_read_output, args=(_process,), daemon=True).start()
    threading.Thread(target=_watch, args=(_process,), daemon=True).start()

    # Give it a moment so the address is usually known before the bot is ready.
    for _ in range(20):
        if public_url() or _process.poll() is not None:
            break
        time.sleep(0.25)

    if _process.poll() is not None:
        warn("Dashboard tunnel exited immediately. Run cloudflared by hand to see why.")
        return False
    if not public_url():
        info("Dashboard tunnel: no address yet — it will be published when cloudflared offers one.")
    return True


def _watch(process: subprocess.Popen) -> None:
    process.wait()
    _unpublish()
    code = process.returncode
    if code not in (0, None):
        error(f"Dashboard tunnel stopped (exit {code}). The panel is local only until it restarts.")


def stop() -> None:
    global _process
    if _process is None:
        return
    try:
        _process.terminate()
    except Exception:
        pass
    _process = None
    _unpublish()
