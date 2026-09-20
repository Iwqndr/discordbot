"""Public tunnel for the admin panel.

`main.py` starts this alongside Flask, so the panel that only exists on this
machine gets a public https:// address that `jamesheston.pages.dev/dashboard`
can reach.

Two providers, in this order unless `TUNNEL_PROVIDER` says otherwise:

* **cloudflared, as a quick tunnel** (preferred). No account, no domain, no
  token: it asks the edge for a throwaway
  `https://<random>.trycloudflare.com` address. It is the default because
  nothing gets between the browser and the panel — the link opens straight
  away. The address is random and changes on every restart, which only matters
  for bookmarks: `jamesheston.pages.dev/dashboard` reads whichever address is
  current and redirects to it.
* **localtunnel** (fallback). A stable
  `https://<LOCALTUNNEL_SUBDOMAIN>.loca.lt` address, retried
  `LOCALTUNNEL_ATTEMPTS` times because its connection fails transiently (it can
  also go zombie — the process lives on while the registration dies — which is
  what the watchdog below is for). The catch: the hosted loca.lt service answers
  *browsers* with an HTTP 511 "Tunnel website ahead!" click-through page before
  forwarding anything, so it is a fallback rather than the link to share. Only a
  server-side request that sends `Bypass-Tunnel-Reminder: true` skips that page,
  which is why the health checks below send one.

Set `TUNNEL_PROVIDER=localtunnel` to prefer the stable loca.lt name anyway, and
accept that visitors click through loca.lt's notice once per browser.

The token-backed *named* cloudflared tunnel is opt-in
(`CLOUDFLARE_TUNNEL_MODE=named`): it carries no traffic until its hostname routes
to it inside the account that owns the token, so it must never be a silent
default. Whatever the provider, the other one is tried if the first fails.

The panel is opened at its own address rather than proxied through the domain,
so the browser talks to whichever provider is live; `jamesheston.pages.dev`
only reads the current address and redirects to it.

Whichever runs, the public address is published to the Supabase `bot_status`
row with id 'tunnel', and a watchdog polls that address and restarts the client
when it stops answering — because a tunnel can die without its process exiting.

Everything here is best-effort: if neither client is available the panel stays
local and the bot is unaffected.
"""

import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

from console import debug, error, info, warn

# Addresses each provider prints on startup.
_CLOUDFLARED_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com", re.I)
_LOCALTUNNEL_RE = re.compile(r"https://([a-z0-9][a-z0-9-]*)\.loca\.lt", re.I)

CLOUDFLARED_PATHS = (
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe",
    r"%LOCALAPPDATA%\cloudflared\cloudflared.exe",
    "~/scoop/shims/cloudflared.exe",
    "/usr/local/bin/cloudflared",
    "/usr/bin/cloudflared",
    # Linux installs: the .deb uses /usr/bin, snap uses /snap/bin, and a manual
    # download very often just sits in the home directory. A cloudflared that is
    # installed but missing from PATH (a service, a systemd unit, a snap-only
    # shell) is otherwise reported as "not found".
    "/snap/bin/cloudflared",
    "/opt/cloudflared/cloudflared",
    "~/.local/bin/cloudflared",
    "~/cloudflared",
)

DEFAULT_SUBDOMAIN = "jamesheston"

# localtunnel keeps the same address across restarts, so it is worth retrying
# before giving up on a public panel — but it is the fallback provider, because
# of loca.lt's click-through page.
LOCALTUNNEL_ATTEMPTS = 3
LOCALTUNNEL_RETRY_GAP = 5.0
# npx has to install the package the first time it is asked for, which takes
# noticeably longer than a restart where the npm cache is already warm.
LOCALTUNNEL_PATIENCE = 35.0
LOCALTUNNEL_RETRY_PATIENCE = 18.0

# Health watchdog. A dead tunnel is a visible outage, so two misses 20 seconds
# apart is enough to act on; waiting longer just means minutes of "Bad gateway".
HEALTH_INTERVAL = 20
HEALTH_FAILURES = 2
HEALTH_MAX_RESTARTS = 20

_process = None
_public_url = None
_provider = None
# Set to "http2" once a quick tunnel has come up silent, so later restarts start
# there instead of re-discovering the same dead UDP path (and rotating through
# yet another address).
_prefer_protocol = ""
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
            wanted = _localtunnel_subdomain()
            if kind == "localtunnel" and f"//{wanted}.loca.lt" not in found:
                warn(f"Panel tunnel: '{wanted}' was not available, so localtunnel gave "
                     f"'{found}'. Set LOCALTUNNEL_SUBDOMAIN to a name nobody else has taken "
                     f"to get a predictable address back.")
            _publish(found, kind)


def _spawn(args: list, kind: str, pattern: re.Pattern):
    """Start a client, watching its output. Returns the Popen or None."""
    global _process, _early_output
    with _lock:
        _early_output = []
    launch = {}
    if sys.platform == "win32":
        launch["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        # Its own process group, so `_kill_tree` can signal the whole
        # `npx` -> `node localtunnel` chain at once instead of leaving the real
        # client behind holding the subdomain.
        launch["start_new_session"] = True

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
            **launch,
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

    # An explicit path always wins, for the installs none of the guesses below
    # are ever going to match (a downloaded binary, a service account's home).
    explicit = (os.getenv("CLOUDFLARED_PATH") or "").strip().strip('"').strip("'")
    if explicit:
        candidate = os.path.expanduser(os.path.expandvars(explicit))
        if os.path.isfile(candidate):
            return candidate
        found = which(explicit)
        if found:
            return found
        warn(f"Panel tunnel: CLOUDFLARED_PATH={explicit!r} is not a file, continuing the search.")

    found = which("cloudflared")
    if found:
        return found
    for path in CLOUDFLARED_PATHS:
        path = os.path.expanduser(os.path.expandvars(path))
        if os.path.isfile(path):
            return path
    return ""


def _dotenv_provider() -> str:
    """What `TUNNEL_PROVIDER` says in .env, regardless of the live environment.

    `load_dotenv()` never overwrites a variable that is already set, so an old
    `TUNNEL_PROVIDER` left in the shell, a systemd unit or a wrapper script wins
    over .env — and "I set it to cloudflared" then quietly runs the other
    provider. Reading the file lets the log say which one is really in charge.
    """
    try:
        from dotenv import dotenv_values
    except Exception:
        return ""
    path = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    )
    try:
        if not os.path.isfile(path):
            return ""
        return (dotenv_values(path).get("TUNNEL_PROVIDER") or "").strip().lower()
    except Exception:
        return ""


def _localtunnel_subdomain() -> str:
    return (os.getenv("LOCALTUNNEL_SUBDOMAIN") or "").strip() or DEFAULT_SUBDOMAIN


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


def _use_named_tunnel() -> bool:
    """Run cloudflared against the token instead of a quick tunnel?

    Only when asked for. A quick tunnel needs no account and no domain, so it is
    the only mode that can work out of the box; a named one silently produces
    nothing but the address in .env unless its hostname routes to the tunnel in
    the account that owns the token. Set `CLOUDFLARE_TUNNEL_MODE=named` to use
    the token again.
    """
    mode = (os.getenv("CLOUDFLARE_TUNNEL_MODE") or "").strip().lower()
    if mode not in ("named", "token"):
        return False
    return bool((os.getenv("CLOUDFLARE_TUNNEL_TOKEN") or "").strip())


def _forced_protocol() -> str:
    """`CLOUDFLARE_TUNNEL_PROTOCOL`, when the operator has picked one."""
    chosen = (os.getenv("CLOUDFLARE_TUNNEL_PROTOCOL") or "").strip().lower()
    return chosen if chosen in ("auto", "http2", "quic") else ""


def _start_cloudflared(port: int, named: bool = False, protocol: str = "") -> bool:
    binary = find_cloudflared()
    if not binary:
        return False

    args = [binary, "tunnel", "--no-autoupdate"]

    # Quick tunnels reach the edge over QUIC/UDP 7844 by default. A VPS often
    # lets the handshake through and then drops the packets that follow, so
    # http2 — which goes out over 443 — is tried when that happens.
    chosen = (protocol or _forced_protocol()).strip().lower()
    if chosen in ("auto", "http2", "quic"):
        args += ["--protocol", chosen]

    if named:
        token = (os.getenv("CLOUDFLARE_TUNNEL_TOKEN") or "").strip()
        args += ["run", "--token", token]
        fixed = (os.getenv("CLOUDFLARE_TUNNEL_URL") or "").strip().rstrip("/")
        if fixed:
            _publish(fixed, "cloudflared")
    else:
        # 127.0.0.1 rather than `localhost`: on Linux `localhost` can resolve to
        # ::1 first, and Flask is bound to IPv4 only, so the tunnel comes up and
        # then answers 502 for everything.
        args += ["--url", f"http://127.0.0.1:{port}"]

    return _spawn(args, "cloudflared", _CLOUDFLARED_RE) is not None


def _start_localtunnel(port: int) -> bool:
    name = _localtunnel_subdomain()
    port_override = (os.getenv("LOCALTUNNEL_PORT") or "").strip()
    target = int(port_override) if port_override.isdigit() else port

    args = _npx_command() + ["--yes", "localtunnel", "--port", str(target), "--subdomain", name]

    # localtunnel's address is known up front, but it is only published after a
    # health check proves the connection actually carries traffic — otherwise a
    # taken subdomain leaves a dead address in Supabase.
    info(f"Panel tunnel: starting https://{name}.loca.lt -> localhost:{target}")

    if _spawn(args, "localtunnel", _LOCALTUNNEL_RE) is None:
        return False
    return True


def _try_localtunnel(port: int, patience: float = LOCALTUNNEL_PATIENCE) -> bool:
    """One localtunnel attempt. True once it has produced a live address."""
    if not _start_localtunnel(port):
        return False

    if not _wait_for_address(_process, patience):
        warn("Panel tunnel (localtunnel) produced no address. Output:")
        for line in list(_early_output)[:6]:
            warn(f"    {line}")
        stop()
        return False

    url = public_url()
    if not url:
        # Nothing on stdout, so fall back to the subdomain we asked for.
        url = f"https://{_localtunnel_subdomain()}.loca.lt"
        if not _wait_healthy(url, attempts=4):
            warn(f"Panel tunnel (localtunnel) is not reachable at {url}.")
            stop()
            return False
        _publish(url, "localtunnel")

    if not _wait_healthy(url, attempts=3):
        # Either the subdomain was taken by someone else or the registration
        # died on the way up. Counting this as a failed attempt is what lets the
        # caller retry — and eventually move to cloudflared.
        warn(f"Panel tunnel (localtunnel) is not carrying traffic at {url}.")
        stop()
        return False
    return True


def _try_cloudflared(port: int, named: bool = False, protocol: str = "") -> str:
    """One cloudflared start.

    Returns "live" (an address that carries traffic), "silent" (an address that
    answers nothing) or "failed" (no address at all). The caller decides what a
    "silent" quick tunnel is worth, because "publish it and let the watchdog
    retry" and "retry right now over HTTP/2" are different answers.
    """
    # A named tunnel's address comes from .env rather than stdout, so there is
    # nothing to wait for on the output stream.
    patience = 6.0 if named else 25.0

    if not _start_cloudflared(port, named=named, protocol=protocol):
        return "failed"

    if named and not public_url():
        warn("Panel tunnel: CLOUDFLARE_TUNNEL_TOKEN is set but CLOUDFLARE_TUNNEL_URL is "
             "not, so the named tunnel has no address to publish.")
        stop()
        return "failed"

    if not _wait_for_address(_process, patience):
        warn("Panel tunnel (cloudflared) produced no address. Output:")
        for line in list(_early_output)[:6]:
            warn(f"    {line}")
        stop()
        return "failed"

    url = public_url()
    if url and _wait_healthy(url):
        return "live"

    if named:
        warn(f"Panel tunnel: the named address {url} is not answering. A named tunnel only "
             f"carries traffic once its hostname routes to it in the account that owns the "
             f"token — without a domain of your own, use a quick tunnel instead.")
        stop()
        return "failed"

    return "silent"


def _try_quick_tunnel(port: int) -> str:
    """A quick tunnel, retried once over HTTP/2 when its UDP path is dead.

    The failure this exists for looks maddening from the outside: cloudflared
    prints an address, the edge accepts the tunnel, and *nothing* that travels
    through it ever arrives. On a VPS that is usually QUIC/UDP 7844 being
    dropped after the handshake (Oracle Cloud and friends drop it in various
    places), which HTTP/2 avoids by going out over 443.

    So before trusting a silent quick tunnel, the panel is checked locally: if
    the panel itself is down there is nothing to forward and no protocol will
    save it, and the message should say so rather than blaming the tunnel.
    """
    global _prefer_protocol

    status = _try_cloudflared(port, named=False, protocol=_prefer_protocol)
    if status != "silent" or _forced_protocol() or _prefer_protocol == "http2":
        return status

    if not _healthy(f"http://127.0.0.1:{port}", timeout=5):
        warn(f"Panel tunnel: the panel is not answering on http://127.0.0.1:{port}, so the "
             f"tunnel has nothing to forward. Fix the Flask error above, not the tunnel.")
        return status

    if not public_url() or not _resolves(public_url()):
        warn(f"Panel tunnel: {public_url() or 'the address'} does not even resolve from "
             f"this machine, which can be its DNS caching the miss rather than the tunnel.")

    _prefer_protocol = "http2"
    warn("Panel tunnel: the quick tunnel is up but carrying no traffic, while the panel "
         "answers locally — retrying over HTTP/2 (a VPS commonly drops cloudflared's "
         "QUIC/UDP 7844 traffic after the handshake).")
    stop()
    info("Panel tunnel: starting cloudflared (quick tunnel, http2).")
    return _try_cloudflared(port, named=False, protocol="http2")


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


def _localtunnel_with_retries(port: int) -> bool:
    """localtunnel, retried a few times because it fails transiently.

    The connection can drop during registration, the subdomain can be taken,
    and `npx` can fail on the first run while it downloads the package — all of
    which come good on a retry. Only after LOCALTUNNEL_ATTEMPTS of them does the
    caller give up on it.
    """
    attempts = max(1, _env_attempts())
    for attempt in range(1, attempts + 1):
        # The first try may have to download the package; later ones cannot.
        patience = LOCALTUNNEL_PATIENCE if attempt == 1 else LOCALTUNNEL_RETRY_PATIENCE
        if _try_localtunnel(port, patience):
            return True
        if attempt < attempts:
            warn(f"Panel tunnel: localtunnel attempt {attempt}/{attempts} failed, "
                 f"retrying in {LOCALTUNNEL_RETRY_GAP:.0f}s…")
            time.sleep(LOCALTUNNEL_RETRY_GAP)
    warn(f"Panel tunnel: localtunnel did not come up after {attempts} attempt(s).")
    return False


def _env_attempts() -> int:
    raw = (os.getenv("LOCALTUNNEL_ATTEMPTS") or "").strip()
    if not raw.isdigit():
        return LOCALTUNNEL_ATTEMPTS
    return _clamp(int(raw), 1, 10)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _cloudflared_with_fallback(port: int) -> bool:
    """cloudflared: the named tunnel only if asked for, else a quick tunnel."""
    binary = find_cloudflared()
    if not binary:
        warn("Panel tunnel: no cloudflared binary was found, so the public link has to "
             "come from localtunnel instead. Install cloudflared, or set "
             "CLOUDFLARED_PATH to the binary.")
        return False
    debug(f"Panel tunnel: cloudflared is {binary}")

    if _use_named_tunnel():
        info("Panel tunnel: starting cloudflared (named tunnel).")
        if _try_cloudflared(port, named=True) == "live":
            return True
        warn("Panel tunnel: the named tunnel did not come up; using a quick tunnel instead.")

    info("Panel tunnel: starting cloudflared (quick tunnel).")
    status = _try_quick_tunnel(port)

    if status == "live":
        return True
    if status == "silent":
        # The address is real and the edge knows it, so it is still worth
        # publishing: the watchdog keeps poking it and restarts the client.
        warn("Panel tunnel (cloudflared) came up but is not answering yet; "
             "publishing it anyway so the watchdog can recover it.")
        return True
    return False


def start(port: int) -> bool:
    """Start the preferred tunnel to `http://localhost:<port>`.

    A cloudflared quick tunnel first, so the address a browser opens is the
    panel itself with nothing in front of it; localtunnel second, because its
    stable address is worth falling back to even though loca.lt makes visitors
    click through a notice page.
    """
    if os.getenv("DASHBOARD_TUNNEL", "1").strip().lower() in ("0", "false", "off", "no"):
        info("Panel tunnel: disabled (DASHBOARD_TUNNEL=0)")
        return False

    # Starting twice would orphan the first client and publish a second address,
    # leaving one of them stranded. One tunnel per process, always.
    if _process is not None and _process.poll() is None:
        info("Panel tunnel: already running, not starting another.")
        return True

    wanted = (os.getenv("TUNNEL_PROVIDER") or "").strip().lower()

    # Say which setting won. "I set TUNNEL_PROVIDER=cloudflared and it still ran
    # localtunnel" is almost always .env losing to an environment variable that
    # was exported earlier, or .env never being loaded at all.
    from_file = _dotenv_provider()
    if from_file and from_file != wanted:
        if wanted:
            warn(f"Panel tunnel: the environment sets TUNNEL_PROVIDER={wanted}, so .env's "
                 f"{from_file} is ignored — an already-set variable is never overwritten by "
                 f".env. Clear it to let .env decide.")
        else:
            warn(f"Panel tunnel: .env sets TUNNEL_PROVIDER={from_file}, but it did not reach "
                 f"the process, so the default order is being used. Start the bot from the "
                 f"repository root so its .env is loaded.")
    if wanted in ("cloudflared", "localtunnel"):
        info(f"Panel tunnel: TUNNEL_PROVIDER={wanted} (trying {wanted} first).")

    # cloudflared first: a quick tunnel is the one whose address a browser opens
    # directly, because loca.lt intercepts browsers with its own notice page.
    order = ["cloudflared", "localtunnel"]
    if wanted == "localtunnel":
        order = ["localtunnel", "cloudflared"]
        info("Panel tunnel: TUNNEL_PROVIDER=localtunnel, so the address stays "
             "https://<name>.loca.lt — visitors then click through loca.lt's "
             "\"Tunnel website ahead\" page before the panel loads.")
    elif wanted and wanted != "cloudflared":
        warn(f"Panel tunnel: TUNNEL_PROVIDER={wanted!r} is not a provider, "
             f"using the default order.")

    # Clear any client left over from a previous run before starting a new one,
    # so exactly one tunnel is ever alive and the published address belongs to
    # the process we are about to watch.
    kill_orphans()

    started = False
    for kind in order:
        if kind == "localtunnel":
            started = _localtunnel_with_retries(port)
        else:
            started = _cloudflared_with_fallback(port)
        if started:
            break

    if not started:
        warn("Panel tunnel could not be established. The panel is local only.")
        return False

    global _health_thread
    if _health_thread is None or not _health_thread.is_alive():
        _health_thread = threading.Thread(target=_health_loop, args=(port,), daemon=True)
        _health_thread.start()
    return True


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


def _resolves(url: str) -> bool:
    """Does this address resolve at all from here?

    A quick tunnel's hostname takes a moment to appear in DNS, and a resolver
    that has already cached the miss keeps answering NXDOMAIN — so "the tunnel
    answers nothing" is sometimes "this machine cannot look the address up yet".
    The two want different fixes, so the log says which one it is.
    """
    host = urllib.parse.urlsplit(url or "").hostname or ""
    if not host:
        return False
    try:
        return bool(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
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
            # `npx localtunnel` is a wrapper around a `node` child; signalling
            # only the wrapper leaves the client running and still holding the
            # subdomain, which is how one restart ends up with two tunnels.
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except OSError:
                process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=5)
    except Exception:
        try:
            if sys.platform == "win32":
                process.kill()
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except OSError:
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
