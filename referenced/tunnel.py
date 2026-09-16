"""localtunnel for the admin panel.

`main.py` starts this alongside Flask, so the panel that only exists on this
machine gets a stable public address:

    https://<LOCALTUNNEL_SUBDOMAIN>.loca.lt

`jamesheston.pages.dev/dashboard` redirects into a Worker proxy, which forwards
to that address and adds the header localtunnel wants — see
`functions/panel-proxy.js`. Without that header a browser gets localtunnel's
"Tunnel website ahead!" page first, which is what the proxy exists to hide.

The subdomain is fixed, so unlike a quick Cloudflare tunnel the address does not
change between restarts. It only changes if you change the setting.

Set `LOCALTUNNEL_SUBDOMAIN=jamesheston` (the default) and, if you want a
different local port, `LOCALTUNNEL_PORT`.

Everything here is best-effort: if npx is missing or the tunnel fails, the panel
keeps working locally and the bot is unaffected.
"""

import os
import re
import subprocess
import sys
import threading
import time

from console import debug, error, info, warn

# "your url is: https://jamesheston.loca.lt"
_URL_RE = re.compile(r"https://([a-z0-9][a-z0-9-]*)\.loca\.lt", re.I)

DEFAULT_SUBDOMAIN = "jamesheston"
_process = None
_public_url = None
_lock = threading.Lock()


def subdomain() -> str:
    return (os.getenv("LOCALTUNNEL_SUBDOMAIN") or "").strip() or DEFAULT_SUBDOMAIN


def public_url() -> str:
    with _lock:
        return _public_url or ""


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _publish(url: str) -> None:
    """Record the address in Supabase so the Pages proxy can find it.

    Uses the `bot_status` row with id 'tunnel', so no extra migration is needed.
    """
    global _public_url
    with _lock:
        _public_url = url

    try:
        import supabase_helper

        supabase_helper._request(
            "POST",
            "bot_status?on_conflict=id",
            data={"id": "tunnel", "version": url, "updated_at": _now()},
            extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        info(f"Panel tunnel live: {url}  (reached at /dashboard)")
    except Exception as exc:
        warn(f"Could not publish the tunnel address: {type(exc).__name__}: {exc}")


def _unpublish() -> None:
    global _public_url
    with _lock:
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
    """Watch the client's output for the address it was given."""
    stream = process.stdout or process.stderr
    if stream is None:
        return
    for raw in iter(stream.readline, ""):
        line = raw.strip()
        if not line:
            continue
        debug(f"localtunnel: {line}")
        match = _URL_RE.search(line)
        if match and not public_url():
            _publish(match.group(0).rstrip("/"))


def start(port: int) -> bool:
    """Start the tunnel to `http://localhost:<port>`.

    The address is known before the process starts — localtunnel uses whatever
    subdomain you ask for — so it is published immediately rather than waiting
    for output. The output reader is still there to catch a rejection (an
    already-taken subdomain) and clear it again.
    """
    if os.getenv("DASHBOARD_TUNNEL", "1").strip().lower() in ("0", "false", "off", "no"):
        info("Panel tunnel: disabled (DASHBOARD_TUNNEL=0)")
        return False

    name = subdomain()
    port_override = (os.getenv("LOCALTUNNEL_PORT") or "").strip()
    target = int(port_override) if port_override.isdigit() else port

    args = [
        "npx", "--yes", "localtunnel",
        "--port", str(target),
        "--subdomain", name,
    ]

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
            shell=(sys.platform == "win32"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except Exception as exc:
        warn(f"Panel tunnel could not start: {type(exc).__name__}: {exc}")
        return False

    threading.Thread(target=_read_output, args=(_process,), daemon=True).start()
    threading.Thread(target=_watch, args=(_process,), daemon=True).start()

    info(f"Panel tunnel: starting https://{name}.loca.lt -> localhost:{target}")
    _publish(f"https://{name}.loca.lt")

    # npx needs a moment on a cold cache; if it dies straight away, say so.
    for _ in range(24):
        if _process.poll() is not None:
            break
        time.sleep(0.25)

    if _process.poll() is not None:
        warn("Panel tunnel exited immediately. Run `npx localtunnel --port "
             f"{target}` by hand to see why.")
        _unpublish()
        return False
    return True


def _watch(process: subprocess.Popen) -> None:
    process.wait()
    _unpublish()
    code = process.returncode
    if code not in (0, None):
        error(f"Panel tunnel stopped (exit {code}). The panel is local only until it restarts.")


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
