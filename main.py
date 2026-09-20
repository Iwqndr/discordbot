import os
import sys

# Source files live in referenced/; main.py stays at the repository root as
# the entry point. Putting that folder on the path lets every module import
# its neighbours by their plain names.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "referenced"))
import os
import logging
import threading
import socket
import asyncio
import ssl
import aiohttp
import discord
import psutil
import flask
import github

from bot import bot, TOKEN
from dashboard import app, set_bot, start_queue_worker
from console import debug, error, info, warn

github.register(app)

set_bot(bot)

DASHBOARD_PORT = 5000
SCRIPT_PATH = os.path.abspath(__file__)

logging.getLogger("werkzeug").setLevel(logging.ERROR)
flask.cli.show_server_banner = lambda *args, **kwargs: None


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def run_flask():
    # Runs on a daemon thread, so a bind failure would otherwise vanish without
    # a word while the bot kept running with no panel.
    try:
        app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)
    except OSError as e:
        error(f"Web dashboard could not start on port {DASHBOARD_PORT}: {e}")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_same_script(proc) -> bool:
    args = [arg for arg in (proc.info.get("cmdline") or []) if arg]
    if len(args) < 2:
        return False
    try:
        other_dir = proc.cwd()
    except Exception:
        return False
    for arg in args[1:]:
        try:
            candidate = arg if os.path.isabs(arg) else os.path.join(other_dir, arg)
            if _norm(candidate) == _norm(SCRIPT_PATH):
                return True
        except Exception:
            continue
    return False


def _kill_pid(pid: int) -> bool:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    try:
        proc.terminate()
        proc.wait(timeout=5)
        return True
    except psutil.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3)
            return True
        except psutil.Error as exc:
            warn(f"Could not kill PID {pid}: {exc}")
            return False
    except psutil.Error as exc:
        warn(f"Could not stop PID {pid}: {exc}")
        return False


def close_previous_instances():
    protected = {os.getpid(), os.getppid()}
    closed = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        pid = proc.info.get("pid")
        if pid in protected or not _is_same_script(proc):
            continue
        if _kill_pid(pid):
            closed.append(pid)

    if closed:
        info(f"Closed previous instance(s): {', '.join(str(pid) for pid in closed)}")
    else:
        debug("No previous instance was running.")


def free_dashboard_port():
    """Kill anything holding the dashboard port, not just this script.

    `close_previous_instances` only sees copies of *this* file. A leftover
    Flask from an older build would silently keep serving the panel while the
    browser kept hitting a stale bot, and the new process's own bind would
    fail on a thread nobody watched. Reclaim the port by force so the URL
    always answers from the live process.
    """
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, RuntimeError):
        return

    for conn in conns:
        if conn.status != psutil.CONN_LISTEN:
            continue
        if not conn.laddr or conn.laddr.port != DASHBOARD_PORT:
            continue
        pid = conn.pid
        if not pid or pid == os.getpid():
            continue
        warn(f"Port {DASHBOARD_PORT} is held by PID {pid} — closing it…")
        _kill_pid(pid)


# ---------------------------------------------------------------------------
# Discord startup
# ---------------------------------------------------------------------------
# A TLS handshake to Discord can fail for a moment — an edge that answers
# "handshake failure", a network burp, a DNS hiccup — and discord.py reports that
# as a startup error. Ending the process on it is the wrong trade: Flask and the
# tunnel are already up, and the panel is exactly what is wanted when Discord is
# misbehaving. So the login is retried, and only errors that mean "try again" are
# caught, because a bad token must still fail immediately.

# Worth another attempt: connection, TLS, DNS and timeout failures.
_TRANSIENT_ERRORS = (
    aiohttp.ClientError,
    ssl.SSLError,
    socket.gaierror,
    ConnectionError,
    TimeoutError,
)

# Seconds to wait between login attempts; the last value repeats until it works.
_RETRY_DELAYS = (5, 15, 30, 60, 120, 300)


def start_tunnel() -> None:
    """Bring the panel's public tunnel up, on its own thread.

    `tunnel.start()` can spend twenty seconds proving that the address it just
    printed actually carries traffic, and none of that is Discord's business:
    blocking the main thread there delayed login (and a failed tunnel delayed it
    by longer still). Best-effort either way — if it fails, the panel simply
    stays reachable on this machine.
    """
    try:
        import tunnel

        tunnel.start(DASHBOARD_PORT)
    except Exception as exc:
        warn(f"Dashboard tunnel unavailable: {type(exc).__name__}: {exc}")


def _retry_delay(attempt: int) -> int:
    return _RETRY_DELAYS[min(max(attempt, 1) - 1, len(_RETRY_DELAYS) - 1)]


def _describe(exc: BaseException) -> str:
    """`TypeName: message`, without letting a broken `__str__` escape.

    Formatting an exception can itself raise (an aiohttp connector error built
    without a connection key blows up in its own `__str__`), and a crash while
    reporting a crash would take the panel down with it.
    """
    try:
        return f"{type(exc).__name__}: {exc}"
    except Exception:
        return type(exc).__name__


async def _retry_bot_call(what: str, func, *args, **kwargs) -> None:
    """Run a Discord call, riding out the failures worth retrying.

    `login()` and `connect()` are the two calls that run before anything is
    working, and both fail transiently here: a TLS handshake to Discord answers
    "handshake failure" some of the time on this network.

    The AttributeError case is discord.py's own hole. When the *first* websocket
    connection fails, its resume path still reads `self.ws.sequence` — and
    `self.ws` is None, because no connection was ever established — so one failed
    handshake ends the process with "NoneType has no attribute 'sequence'". That
    exact case is retried instead; any other AttributeError is a real bug and is
    raised.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            await func(*args, **kwargs)
            return
        except _TRANSIENT_ERRORS as exc:
            reason = _describe(exc)
        except AttributeError as exc:
            reason = _describe(exc)
            if bot.ws is not None or "sequence" not in reason:
                raise
            reason = f"discord.py's first-connection bug ({reason})"

        delay = _retry_delay(attempt)
        warn(f"Discord {what} failed ({reason}). The panel is still up; retrying in {delay}s.")
        await asyncio.sleep(delay)


async def _start_bot() -> None:
    """`bot.run()`, with the two startup calls made retryable.

    The retry sits inside the client's own context, rather than around `run()`:
    `run()` closes the HTTP session on its way out, so a second call dies with
    "Session is closed", while a call that failed leaves the session perfectly
    usable. `login()` + `connect()` is exactly what `start()` — and therefore
    `run()` — does internally.
    """
    async with bot:
        await _retry_bot_call("login", bot.login, TOKEN)
        await _retry_bot_call("gateway connection", bot.connect, reconnect=True)

        if bot.is_closed():
            # discord.py closes the client on its way out of a connection it
            # treats as fatal, and a connect() on a closed client returns
            # immediately — so this would otherwise look like a clean shutdown
            # and take the panel down without a word.
            error("Discord closed the connection and the client with it, so this run cannot "
                  "continue. Start the bot again — the panel is local only until then.")
            raise SystemExit(75)


if __name__ == "__main__":
    if not TOKEN:
        error("DISCORD_TOKEN is missing. Set it in your .env file.")
        raise SystemExit(1)

    close_previous_instances()
    free_dashboard_port()

    local_ip = get_local_ip()
    threading.Thread(target=run_flask, daemon=True).start()
    info(f"Member hub:    http://{local_ip}:{DASHBOARD_PORT}/")
    info(f"Staff panel:   http://{local_ip}:{DASHBOARD_PORT}/admin")
    info(f"GitHub pusher: http://{local_ip}:{DASHBOARD_PORT}/git")

    # Give the panel a public address so jamesheston.pages.dev/dashboard can
    # redirect to it. On its own thread, so the bot logs in immediately instead
    # of waiting on the tunnel's health check.
    threading.Thread(target=start_tunnel, daemon=True).start()

    # Tickets opened on the member page are filed in Supabase, not here, so
    # something has to collect them: this worker drains that queue and posts each
    # one to the staff channel. It also keeps the heartbeat the member page reads
    # to decide whether a new ticket is "open" or still "processing".
    start_queue_worker()

    # _start_bot() blocks on Discord's event loop and is what keeps this process
    # alive. Without it __main__ returns immediately, Python exits, and the
    # daemon Flask thread is torn down with it -- the whole app "shuts down"
    # a second after it prints its startup lines.
    #
    # `bot.run()` would set the library's logging up for us; driving the same
    # login/connect pair by hand means doing that one line explicitly.
    discord.utils.setup_logging()
    try:
        asyncio.run(_start_bot())
    except KeyboardInterrupt:
        # Same as `Client.run()`: Ctrl+C is a normal way to stop.
        pass
    except discord.errors.LoginFailure as exc:
        error(f"Discord rejected DISCORD_TOKEN ({exc}). Fix it in .env and start again.")
        raise SystemExit(1)
