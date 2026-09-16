"""Public tunnel for the admin panel.

`main.py` starts this alongside Flask, so the panel that only exists on this
machine gets a public https:// address that `jamesheston.pages.dev/dashboard`
can reach.

Two providers, tried in this order unless `TUNNEL_PROVIDER` says otherwise:

* **localtunnel** (default). You pick the subdomain, so the address is the same
  every restart — `https://<LOCALTUNNEL_SUBDOMAIN>.loca.lt`. Its connection is
  less durable than cloudflared's and can go zombie (the process lives on while
  the registration dies), which is what the watchdog below is for.
* **cloudflared** (fallback). Tried when localtunnel is unavailable, or when
  `TUNNEL_PROVIDER=cloudflared`. A quick tunnel needs no account and holds its
  connection better, but the hostname is random on every start.

The panel is now opened at its own address rather than proxied through the
domain, so a browser hitting loca.lt directly sees localtunnel's one-time
"Tunnel website ahead" notice. That is expected.

Whichever runs, the public address is published to the Supabase `bot_status`
row with id 'tunnel', and a watchdog polls that address and restarts the client
when it stops answering — because a tunnel can die without its process exiting.

Everything here is best-effort: if neither client is available the panel stays
local and the bot is unaffected.
"""

import os
import re
import subprocess
import sys
import threading
import time

from console import debug, error, info, warn

# Addresses each provider prints on startup.
_CLOUDFLARED_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com", re.I)
_LOCALTUNNEL_RE = re.compile(r"https://([a-z0-9][a-z0-9-]*)\.loca\.lt", re.I)

CLOUDFLARED_PATHS = (
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
    "/usr/local/bin/cloudflared",
    "/usr/bin/cloudflared",
)

DEFAULT_SUBDOMAIN = "jamesheston"

# Health watchdog. A dead tunnel is a visible outage, so two misses 20 seconds
# apart is enough to act on; waiting longer just means minutes of "Bad gateway".
HEALTH_INTERVAL = 20
HEALTH_FAILURES = 2
HEALTH_MAX_RESTARTS = 20

_process = None
_public_url = None
_provider = None
_lock = threading.Lock()
_early_output: list = []
_restarts = 0
_health_thread = None


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _safe(text: str) -> str:
    """Strip characters the host console cannot encode.

    cloudflared and localtunnel both print box-drawing and bullet characters.
    On a cp1252 console those raise UnicodeEncodeError *inside* the logging
    call, which kills the reader thread before it ever parses the URL — so the
    tunnel looks like it silently failed.
    """
    try:
        text.encode(sys.stdout.encoding or "utf-8")
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode("ascii", "replace").decode("ascii")


def public_url() -> str:
    with _lock:
        return _public_url or ""


def provider() -> str:
    with _lock:
        return _provider or ""


def _publish(url: str, kind: str) -> None:
    """Record the address in Supabase so the Pages proxy can find it.

    Uses the `bot_status` row with id 'tunnel', so no extra migration is needed.
    """
    global _public_url, _provider
    with _lock:
        _public_url = url
        _provider = kind

    try:
        import supabase_helper

        supabase_helper._request(
            "POST",
            "bot_status?on_conflict=id",
            data={"id": "tunnel", "version": url, "updated_at": _now()},
            extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        info(f"Panel tunnel live ({kind}): {url}  (reached at /dashboard)")
    except Exception as exc:
        warn(f"Could not publish the tunnel address: {type(exc).__name__}: {exc}")


def _unpublish() -> None:
    """Clear the address so the site shows "panel offline" instead of 502s."""
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


def _read_output(process: subprocess.Popen, pattern: re.Pattern, kind: str) -> None:
    stream = process.stdout or process.stderr
    if stream is None:
        return
    for raw in iter(stream.readline, ""):
        line = _safe(raw.strip())
        if not line:
            continue
        debug(f"{kind}: {line}")
        with _lock:
            _early_output.append(line)
            del _early_output[:-8]
        match = pattern.search(line)
        if match and not public_url():
            found = match.group(0).rstrip("/")
            # localtunnel silently hands back a *different* subdomain when the
            # one asked for is already taken by another user. Publishing what
            # the client actually says keeps the site pointed at a live tunnel
            # instead of a dead address.
            wanted = (os.getenv("LOCALTUNNEL_SUBDOMAIN") or "").strip() or DEFAULT_SUBDOMAIN
            if kind == "localtunnel" and f"//{wanted}.loca.lt" not in found:
                warn(f"Panel tunnel: '{wanted}' was not available, so localtunnel gave "
                     f"'{found}'. Set LOCALTUNNEL_SUBDOMAIN to something else, or "
                     f"TUNNEL_PROVIDER=cloudflared, for a stable address.")
            _publish(found, kind)


def _spawn(args: list, kind: str, pattern: re.Pattern):
    """Start a client, watching its output. Returns the Popen or None."""
    global _process, _early_output
    with _lock:
        _early_output = []
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
        warn(f"Panel tunnel could not start ({kind}): {type(exc).__name__}: {exc}")
        return None

    threading.Thread(target=_read_output, args=(_process, pattern, kind), daemon=True).start()
    threading.Thread(target=_watch, args=(_process,), daemon=True).start()
    return _process


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


def find_cloudflared() -> str:
    from shutil import which

    found = which("cloudflared")
    if found:
        return found
    for path in CLOUDFLARED_PATHS:
        if os.path.isfile(path):
            return path
    return ""


def _npx_command() -> list:
    """`npx` as an argv list that works on Windows.

    On Windows `npx` is a `.cmd` shim, so `subprocess` needs `shell=True` to
    find it — and with `shell=True` you cannot reliably capture its output,
    which is how startup errors get swallowed.
    """
    from shutil import which

    for name in ("npx.cmd", "npx.exe", "npx"):
        found = which(name)
        if found:
            return [found]
    return ["npx"]


def _start_cloudflared(port: int) -> bool:
    binary = find_cloudflared()
    if not binary:
        return False

    token = (os.getenv("CLOUDFLARE_TUNNEL_TOKEN") or "").strip()
    if token:
        args = [binary, "tunnel", "--no-autoupdate", "run", "--token", token]
        fixed = (os.getenv("CLOUDFLARE_TUNNEL_URL") or "").strip().rstrip("/")
        if fixed:
            _publish(fixed, "cloudflared")
    else:
        args = [binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]

    if _spawn(args, "cloudflared", _CLOUDFLARED_RE) is None:
        return False
    return True


def _start_localtunnel(port: int) -> bool:
    name = (os.getenv("LOCALTUNNEL_SUBDOMAIN") or "").strip() or DEFAULT_SUBDOMAIN
    port_override = (os.getenv("LOCALTUNNEL_PORT") or "").strip()
    target = int(port_override) if port_override.isdigit() else port

    args = _npx_command() + ["--yes", "localtunnel", "--port", str(target), "--subdomain", name]
    if _spawn(args, "localtunnel", _LOCALTUNNEL_RE) is None:
        return False

    # localtunnel's address is known up front, but it is only published after a
    # health check proves the connection actually carries traffic — otherwise a
    # taken subdomain leaves a dead address in Supabase.
    info(f"Panel tunnel: starting https://{name}.loca.lt -> localhost:{target}")
    return True


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def _wait_for_address(process, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        if public_url():
            return True
        time.sleep(0.25)
    return process.poll() is None


def start(port: int) -> bool:
    """Start the preferred tunnel to `http://localhost:<port>`."""
    if os.getenv("DASHBOARD_TUNNEL", "1").strip().lower() in ("0", "false", "off", "no"):
        info("Panel tunnel: disabled (DASHBOARD_TUNNEL=0)")
        return False

    # Starting twice would orphan the first client and publish a second address,
    # leaving one of them stranded. One tunnel per process, always.
    if _process is not None and _process.poll() is None:
        info("Panel tunnel: already running, not starting another.")
        return True

    wanted = (os.getenv("TUNNEL_PROVIDER") or "").strip().lower()
    order = ["localtunnel", "cloudflared"]
    if wanted in ("cloudflared", "localtunnel"):
        order = [wanted]

    # Clear any client left over from a previous run before starting a new one,
    # so exactly one tunnel is ever alive and the published address belongs to
    # the process we are about to watch.
    kill_orphans()

    for kind in order:
        if kind == "cloudflared":
            if not find_cloudflared():
                warn("Panel tunnel: cloudflared not found, trying localtunnel instead.")
                continue
            info("Panel tunnel: starting cloudflared (quick tunnel).")
            ok = _start_cloudflared(port)
            patience = 25.0
            # A named tunnel has a fixed public address, so there is nothing to
            # wait for on stdout.
            if (os.getenv("CLOUDFLARE_TUNNEL_TOKEN") or "").strip():
                patience = 6.0
        else:
            ok = _start_localtunnel(port)
            patience = 20.0

        if not ok:
            continue

        if not _wait_for_address(_process, patience):
            warn(f"Panel tunnel ({kind}) produced no address. Output:")
            for line in list(_early_output)[:6]:
                warn(f"    {line}")
            stop()
            continue

        if not public_url():
            # localtunnel: publish only once it is proven reachable.
            name = (os.getenv("LOCALTUNNEL_SUBDOMAIN") or "").strip() or DEFAULT_SUBDOMAIN
            candidate = f"https://{name}.loca.lt"
            if _wait_healthy(candidate):
                _publish(candidate, "localtunnel")
            else:
                warn("Panel tunnel (localtunnel) is not reachable yet; not publishing its address.")
                stop()
                continue
        elif kind == "cloudflared" and not _wait_healthy(public_url()):
            warn("Panel tunnel (cloudflared) came up but is not answering yet; "
                 "publishing it anyway so the watchdog can recover it.")

        global _health_thread
        if _health_thread is None or not _health_thread.is_alive():
            _health_thread = threading.Thread(target=_health_loop, args=(port,), daemon=True)
            _health_thread.start()
        return True

    warn("Panel tunnel could not be established. The panel is local only.")
    return False


def _watch(process: subprocess.Popen) -> None:
    process.wait()
    _unpublish()
    code = process.returncode
    if code not in (0, None):
        error(f"Panel tunnel stopped (exit {code}). The panel is local only until it restarts.")


def _healthy(url: str, timeout: int = 15) -> bool:
    """Can the public address actually reach this tunnel right now?

    A tunnel can die without its process exiting: the connection drops, the
    client stays up, and the provider starts answering "Unavailable" while
    `start()` still believes everything is fine. The only reliable check is to
    ask the public address.
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


def _wait_healthy(url: str, attempts: int = 5, gap: float = 4.0) -> bool:
    """Give a freshly started tunnel a few moments to become reachable.

    A quick tunnel prints its address before the edge finishes wiring it up, so
    a single immediate check says "unhealthy" for a tunnel that is perfectly
    fine. Retrying briefly avoids restarting something that just came up.
    """
    for index in range(attempts):
        if _healthy(url):
            return True
        if index < attempts - 1:
            time.sleep(gap)
    return False


def _health_loop(port: int) -> None:
    """Restart the tunnel when it stops carrying traffic."""
    global _restarts
    misses = 0
    restarts = 0
    healthy_runs = 0

    while True:
        time.sleep(HEALTH_INTERVAL)
        url = public_url()

        if url and _healthy(url):
            healthy_runs += 1
            misses = 0
            # Only a genuinely stable spell clears the restart history, so a
            # tunnel that flaps forever cannot reset its own counter and loop.
            if healthy_runs >= 15:
                restarts = 0
            continue

        healthy_runs = 0
        misses += 1
        if url:
            warn(f"Panel tunnel health check failed ({misses}/{HEALTH_FAILURES}).")
        if misses < HEALTH_FAILURES:
            continue

        misses = 0
        restarts += 1
        if restarts > HEALTH_MAX_RESTARTS:
            error(f"Panel tunnel keeps failing, so automatic restarts have stopped after "
                  f"{HEALTH_MAX_RESTARTS} attempts. The panel is local only. "
                  f"Run the tunnel by hand to see the underlying error.")
            _unpublish()
            return

        # Clear the address before restarting so the site shows "panel offline"
        # rather than forwarding to a dead host for the duration.
        _unpublish()
        stop()
        warn(f"Panel tunnel is not carrying traffic — restarting "
             f"({restarts}/{HEALTH_MAX_RESTARTS}).")

        if not start(port):
            backoff = min(300, 5 * (2 ** min(restarts, 6)))
            warn(f"Panel tunnel restart failed; waiting {backoff}s before the next attempt.")
            time.sleep(backoff)


def stop() -> None:
    """Kill the tunnel client and everything it spawned.

    `terminate()` alone is not enough on Windows: `npx` starts localtunnel as a
    child process, and killing only the shim leaves the real client holding the
    connection. That is how three cloudflared clients ended up running at once,
    each with its own tunnel, after a few restarts.
    """
    global _process
    process = _process
    if process is None:
        return
    _process = None
    _kill_tree(process)
    _unpublish()


def _kill_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def kill_orphans() -> int:
    """Kill tunnel clients left behind by an earlier run of this script.

    `close_previous_instances()` in main.py only sees copies of main.py, so a
    tunnel from a crashed or killed run keeps its connection — and each stale
    client holds a *different* public address, which is why the site could point
    at a tunnel that no longer belongs to the running process.
    """
    if sys.platform != "win32":
        return 0

    killed = 0
    for image in ("cloudflared.exe", "cloudflared"):
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", image],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            continue
        if result.returncode == 0:
            killed += 1
            debug(f"tunnel: closed a leftover {image}")
    if killed:
        info(f"Panel tunnel: closed {killed} leftover tunnel client(s) from a previous run.")
    return killed
