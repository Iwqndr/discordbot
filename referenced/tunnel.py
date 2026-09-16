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
# Last few lines localtunnel printed, kept so a start that dies instantly can
# report why instead of just "it failed".
_early_output: list = []

# Health watchdog. A tunnel can go dead while its process stays alive, so the
# public address is polled and the client restarted when it stops answering.
HEALTH_INTERVAL = 60
HEALTH_FAILURES = 3
HEALTH_MAX_RESTARTS = 5
_restarts = 0
_health_thread = None


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


def _safe(text: str) -> str:
    """Strip characters the host console cannot encode.

    localtunnel prints a `●` in its ready banner. On a Windows console using
    cp1252 that raises UnicodeEncodeError *inside* the logging call, which
    aborts the reader thread before it ever parses the URL — so the tunnel looks
    like it silently failed. Anything unprintable is replaced here instead.
    """
    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode("ascii", "replace").decode("ascii")


def _read_output(process: subprocess.Popen) -> None:
    """Watch the client's output for the address it was given."""
    stream = process.stdout or process.stderr
    if stream is None:
        return
    for raw in iter(stream.readline, ""):
        line = _safe(raw.strip())
        if not line:
            continue
        debug(f"localtunnel: {line}")
        with _lock:
            _early_output.append(line)
            del _early_output[:-8]
        match = _URL_RE.search(line)
        if match and not public_url():
            _publish(match.group(0).rstrip("/"))


def _npx_command() -> list:
    """`npx` as an argv list that works on Windows.

    On Windows `npx` is a `.cmd` shim, so `subprocess` needs `shell=True` to
    find it — and with `shell=True` you cannot reliably redirect its output,
    which is how the startup error gets swallowed. Resolving the shim and
    calling it directly avoids both problems.
    """
    from shutil import which

    for name in ("npx.cmd", "npx.exe", "npx"):
        found = which(name)
        if found:
            return [found]
    return ["npx"]


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

    args = _npx_command() + [
        "--yes", "localtunnel",
        "--port", str(target),
        "--subdomain", name,
    ]

    global _process
    try:
        _process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
    except Exception as exc:
        warn(f"Panel tunnel could not start: {type(exc).__name__}: {exc}")
        return False

    threading.Thread(target=_read_output, args=(_process,), daemon=True).start()
    threading.Thread(target=_watch, args=(_process,), daemon=True).start()

    info(f"Panel tunnel: starting https://{name}.loca.lt -> localhost:{target}")
    _publish(f"https://{name}.loca.lt")

    # npx needs a moment on a cold cache; if it dies straight away the reason is
    # in its output, so keep the first few lines to print.
    early = []
    deadline = time.time() + 8
    while time.time() < deadline:
        if _process.poll() is not None:
            break
        time.sleep(0.25)
        with _lock:
            if _early_output and not early:
                early = list(_early_output)

    if _process.poll() is not None:
        warn("Panel tunnel exited immediately. Output:")
        for line in (early or _early_output)[:6]:
            warn(f"    {line}")
        warn("Run `npx localtunnel --port "
             f"{target} --subdomain {name}` by hand to reproduce it.")
        _unpublish()
        return False

    global _health_thread
    if _health_thread is None or not _health_thread.is_alive():
        _health_thread = threading.Thread(target=_health_loop, args=(target,), daemon=True)
        _health_thread.start()

    return True


def _watch(process: subprocess.Popen) -> None:
    process.wait()
    _unpublish()
    code = process.returncode
    if code not in (0, None):
        error(f"Panel tunnel stopped (exit {code}). The panel is local only until it restarts.")


def _healthy(url: str, timeout: int = 12) -> bool:
    """Can loca.lt actually reach this tunnel right now?

    A tunnel can die without its process exiting: the connection drops, the
    client stays up, and loca.lt starts answering "503 Tunnel Unavailable"
    while `start()` still believes everything is fine. The only reliable check
    is to ask the public address.
    """
    import urllib.request

    try:
        req = urllib.request.Request(
            url + "/",
            headers={"User-Agent": "wispcord-tunnel-health", "Bypass-Tunnel-Reminder": "true"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status < 500
    except Exception:
        return False


def _health_loop(port: int) -> None:
    """Restart the tunnel when it stops carrying traffic.

    Three consecutive failures before acting: one miss is usually a blip, and
    restarting on it would thrash the subdomain.
    """
    global _restarts
    misses = 0

    while True:
        time.sleep(HEALTH_INTERVAL)
        url = public_url()
        if not url:
            continue
        if _healthy(url):
            misses = 0
            continue

        misses += 1
        warn(f"Panel tunnel health check failed ({misses}/{HEALTH_FAILURES}).")
        if misses < HEALTH_FAILURES:
            continue

        misses = 0
        _restarts += 1
        if _restarts > HEALTH_MAX_RESTARTS:
            error("Panel tunnel keeps failing to stay up, so automatic restarts have stopped. "
                  f"Run `npx localtunnel --port {port}` by hand to see the error. "
                  "The panel is local only until then.")
            _unpublish()
            return

        warn(f"Panel tunnel is not carrying traffic — restarting it "
             f"({_restarts}/{HEALTH_MAX_RESTARTS}).")
        stop()
        if not start(port):
            return


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
