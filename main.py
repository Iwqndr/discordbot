import os
import logging
import threading
import socket
import psutil
import flask
import github

from bot import bot, TOKEN
from dashboard import app, set_bot
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


if __name__ == "__main__":
    if not TOKEN:
        error("DISCORD_TOKEN is missing. Set it in your .env file.")
        raise SystemExit(1)

    close_previous_instances()
    free_dashboard_port()

    local_ip = get_local_ip()
    threading.Thread(target=run_flask, daemon=True).start()
    info(f"Web dashboard listening on http://{local_ip}:{DASHBOARD_PORT}")
    info(f"GitHub pusher:  http://{local_ip}:{DASHBOARD_PORT}/git")

    # bot.run() blocks on Discord's event loop and is what keeps this process
    # alive. Without it __main__ returns immediately, Python exits, and the
    # daemon Flask thread is torn down with it -- the whole app "shuts down"
    # a second after it prints its startup lines.
    bot.run(TOKEN)
