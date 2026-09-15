"""
Dev Studio — Main Entry Point
"""

from __future__ import annotations

import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from math import log2
from pathlib import Path
from typing import Iterable

import flask
import psutil
from flask import jsonify, request

from bot import TOKEN, bot
from console import debug, error, info, warn
from dashboard import app, set_bot

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
else:
    import termios
    import tty

DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = SCRIPT_PATH.parent

IGNORED_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "env",
    "node_modules", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".next", ".nuxt", "coverage",
}
SCANNABLE_EXTENSIONS = {
    ".py", ".json", ".yaml", ".yml", ".js", ".jsx", ".ts", ".tsx",
    ".html", ".cfg", ".ini", ".toml",
}

GITIGNORE_DEFAULTS = (
    ".env",
    ".env.*",
    "!.env.example",
    "__pycache__/",
    "*.pyc",
    ".venv/",
    "venv/",
    "node_modules/",
)

KNOWN_SECRET_PREFIXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bMTA[A-Za-z0-9_-]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    re.compile(r"\bsk-proj-[A-Za-z0-9_-]{40,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]+\b"),
    re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b"),
    re.compile(r"\b(?:r|s)k_live_[0-9a-zA-Z]{24,}\b"),
    re.compile(r"\b(?:r|s)k_test_[0-9a-zA-Z]{24,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)

# Values that LOOK like secrets but are safe to publish. The scanner skips
# any line that contains one of these strings verbatim.
#
# Why this exists: Supabase's "anon" JWT is designed to be shipped in
# client-side code. It is a public identifier that grants access only to
# whatever RLS policies allow anonymous reads/writes on. The "service_role"
# key is the one that must stay secret — never add it here.
SCANNER_ALLOWLIST: tuple[str, ...] = (
    # Supabase anon (public) key — safe to appear in commands.html and
    # anywhere else the member site is served from.
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InV6ZWJ0dXptdnZsdWZ1eXdjeHZ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODkyNjc4MDYsImV4cCI6MjEwNDg0MzgwNn0.f9QhXHqQ7VBSTIjz9nBzcgnBk7x_dmmcuKflT5hGqNU",
)

SECRET_NAME_HINT = re.compile(
    r"""(?ix)
    \b(
        api[_-]?key |
        secret[_-]?key |
        access[_-]?token |
        auth[_-]?token |
        bearer[_-]?token |
        private[_-]?key |
        client[_-]?secret |
        refresh[_-]?token |
        password |
        passwd |
        passphrase
    )\b
    \s*[:=]\s*
    (?P<quote>["'`])
    (?P<value>[^"'`\n]{20,})
    (?P=quote)
    """
)

SAFE_VALUE_PREFIXES = (
    "memberhub.", "localstorage.", "sessionstorage.", "http://", "https://",
    "ws://", "wss://", "urn:", "mailto:", "data:", "application/", "text/",
    "image/", "audio/", "video/",
)

MIN_ENTROPY = 3.0
MIN_VALUE_LENGTH = 20

RESET = "\033[H\033[2J\033[3J"


@dataclass(frozen=True)
class SecretFinding:
    file: str
    line: int
    snippet: str


@dataclass
class GitResult:
    success: bool
    message: str = ""
    leaks: list[SecretFinding] = field(default_factory=list)


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum((c / length) * log2(c / length) for c in counts.values())


def _looks_like_secret_value(value: str) -> bool:
    if len(value) < MIN_VALUE_LENGTH:
        return False
    if value.lower().startswith(SAFE_VALUE_PREFIXES):
        return False
    if " " in value.strip():
        return False
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    if not (has_digit and has_alpha):
        return False
    return _shannon_entropy(value) >= MIN_ENTROPY


def _line_has_secret(line: str) -> bool:
    # Anything explicitly allowlisted short-circuits the whole check. This is
    # how the Supabase anon key passes cleanly without being weakened by a
    # broader rule.
    if any(allowed in line for allowed in SCANNER_ALLOWLIST):
        return False
    if any(p.search(line) for p in KNOWN_SECRET_PREFIXES):
        return True
    m = SECRET_NAME_HINT.search(line)
    if m and _looks_like_secret_value(m.group("value")):
        return True
    return False


logging.getLogger("werkzeug").setLevel(logging.ERROR)
flask.cli.show_server_banner = lambda *a, **k: None

app.config.update(
    TEMPLATES_AUTO_RELOAD=True,
    SEND_FILE_MAX_AGE_DEFAULT=0,
)


def _read_key_raw() -> str:
    if IS_WINDOWS:
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            msvcrt.getch()
            return ""
        try:
            return ch.decode("utf-8", errors="ignore").lower()
        except Exception:
            return ""

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch.lower()


def _drain_stdin() -> None:
    if not sys.stdin.isatty():
        return
    try:
        if IS_WINDOWS:
            while msvcrt.kbhit():
                msvcrt.getch()
        else:
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                import select as _select
                while _select.select([sys.stdin], [], [], 0)[0]:
                    if not sys.stdin.read(1):
                        break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


def read_key(prompt: str = "") -> str:
    if prompt:
        print(prompt, end="", flush=True)

    if not sys.stdin.isatty():
        return input().strip().lower()

    key = _read_key_raw()
    print(key)
    return key


def clear_terminal() -> None:
    try:
        if IS_WINDOWS:
            os.system("")
            print(RESET, end="", flush=True)
        else:
            sys.stdout.write(RESET)
            sys.stdout.flush()
    except Exception:
        os.system("cls" if IS_WINDOWS else "clear")


def get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _prompt(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def _confirm(prompt: str, default: bool = False) -> bool:
    marker = "Y/n" if default else "y/N"
    key = read_key(f"{prompt} ({marker}): ")
    if not key:
        return default
    if key in ("y", "1"):
        return True
    if key in ("n", "0"):
        return False
    return default


def ensure_env_ignored() -> None:
    gitignore = PROJECT_ROOT / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("\n".join(GITIGNORE_DEFAULTS) + "\n", encoding="utf-8")
        return
    content = gitignore.read_text(encoding="utf-8")
    missing = [e for e in GITIGNORE_DEFAULTS if e not in content]
    if missing:
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(missing) + "\n")


def _iter_scannable_files() -> Iterable[Path]:
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in files:
            path = Path(root, name)
            if path.suffix in SCANNABLE_EXTENSIONS:
                yield path


def scan_for_secrets() -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    seen: set[tuple[str, int]] = set()

    for path in _iter_scannable_files():
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue

        rel = str(path.relative_to(PROJECT_ROOT))
        for lineno, line in enumerate(lines, start=1):
            if _line_has_secret(line) and (rel, lineno) not in seen:
                seen.add((rel, lineno))
                findings.append(SecretFinding(file=rel, line=lineno, snippet=line.strip()))
    return findings


def _run_git(*args: str, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd or PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


def _is_git_repo(path: Path | None = None) -> bool:
    return (path or PROJECT_ROOT).joinpath(".git").is_dir()


def _has_changes() -> bool:
    return bool(_run_git("status", "--porcelain", check=False).stdout.strip())


def _current_branch() -> str:
    res = _run_git("rev-parse", "--abbrev-ref", "HEAD", check=False)
    return res.stdout.strip() or "(unknown)"


def _list_remotes() -> dict[str, str]:
    res = _run_git("remote", "-v", check=False)
    remotes: dict[str, str] = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in remotes:
            remotes[parts[0]] = parts[1]
    return remotes


def _set_remote_url(name: str, url: str) -> None:
    existing = _list_remotes()
    if name in existing:
        _run_git("remote", "set-url", name, url)
    else:
        _run_git("remote", "add", name, url)


def choose_push_remote() -> tuple[str, str] | None:
    remotes = _list_remotes()

    print("\n--- Git Remotes ---")
    if remotes:
        for idx, (name, url) in enumerate(remotes.items(), start=1):
            print(f"  [{idx}] {name} -> {url}")
    else:
        warn("No git remotes are configured yet.")

    print("  [n] Add a new remote")
    print("  [c] Cancel")

    key = read_key("\nSelect remote: ")

    if key == "c":
        info("Cancelled.")
        return None

    if key == "n" or not remotes:
        name = _prompt("Remote name", "origin")
        url = _prompt("Remote URL (e.g. https://github.com/user/repo.git)")
        if not url:
            error("No URL provided. Aborting.")
            return None
        info(f"Adding remote '{name}' -> {url}")
        _set_remote_url(name, url)
        return name, url

    if key.isdigit():
        idx = int(key) - 1
        if 0 <= idx < len(remotes):
            name, url = list(remotes.items())[idx]
            info(f"Selected remote: {name} -> {url}")
            return name, url

    error("Invalid selection.")
    return None


def confirm_remote(name: str, url: str) -> bool:
    branch = _current_branch()
    print()
    print("  ┌─ Push Summary ─────────────────────────────────")
    print(f"  │  Remote : {name}")
    print(f"  │  URL    : {url}")
    print(f"  │  Branch : {branch}")
    print(f"  │  Local  : {PROJECT_ROOT}")
    print("  └────────────────────────────────────────────────")
    return _confirm("\nPush to this remote?", default=False)


def perform_git_push(commit_message: str, remote_name: str) -> GitResult:
    leaks = scan_for_secrets()
    if leaks:
        return GitResult(success=False, message="Hardcoded secrets detected.", leaks=leaks)

    if not _is_git_repo():
        return GitResult(success=False, message="Not a git repository.")
    if not _has_changes():
        return GitResult(success=True, message="No changes to commit.")

    try:
        _run_git("add", ".")
        _run_git("commit", "-m", commit_message or "Dev Studio Auto-Commit")
        _run_git("push", remote_name)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or str(exc)
        return GitResult(success=False, message=f"Git operation failed: {stderr}")

    return GitResult(success=True, message=f"Successfully pushed to '{remote_name}'!")


def _print_findings(findings: list[SecretFinding]) -> None:
    error("Hardcoded secrets found:")
    for leak in findings:
        warn(f"  {leak.file}:{leak.line} -> {leak.snippet}")


def _run_scanner_only() -> None:
    info("Scanning project for hardcoded secrets...")
    leaks = scan_for_secrets()
    if not leaks:
        info("No hardcoded secrets detected.")
        return
    _print_findings(leaks)
    warn("Review the files above and move secrets into your .env file.")


def _run_push_only() -> None:
    info("Starting push pipeline (scan + commit + push)...")
    ensure_env_ignored()

    if not _is_git_repo():
        warn("Git repository is not initialized here.")
        if not _confirm("Initialize git now?", default=False):
            info("Aborting.")
            return
        subprocess.run(["git", "init"], cwd=PROJECT_ROOT, check=True)
        ensure_env_ignored()
        info("Initialized git and configured .gitignore.")

    selected = choose_push_remote()
    if selected is None:
        return
    remote_name, remote_url = selected

    if not confirm_remote(remote_name, remote_url):
        info("Push cancelled by user.")
        return

    info("Scanning for hardcoded secrets...")
    leaks = scan_for_secrets()
    if leaks:
        _print_findings(leaks)
        error("Push aborted to protect your credentials.")
        return
    info("No hardcoded secrets detected.")

    if not _has_changes():
        info("No changes to commit.")
        return

    print("\n--- Uncommitted Changes ---")
    print(_run_git("status", "--short").stdout)

    message = _prompt("Commit message", "Dev Studio Auto-Commit")

    if not _confirm(f"Commit and push to '{remote_name}'?", default=False):
        info("Push cancelled.")
        return

    result = perform_git_push(message, remote_name)
    (info if result.success else error)(result.message)


def run_github_cli() -> None:
    ensure_env_ignored()

    while True:
        print("\nGitHub Tools:")
        print("  [1] Push to GitHub (auto-scan + commit + push)")
        print("  [2] Run security scanner only")
        print("  [b] Back to main menu")

        key = read_key("\nPress 1, 2, or b: ")

        if key == "1":
            _run_push_only()
        elif key == "2":
            _run_scanner_only()
        elif key in ("b", "q", "\x1b"):
            return
        else:
            warn("Invalid key. Press 1, 2, or b.")


@app.get("/api/git/status")
def api_git_status():
    ensure_env_ignored()

    if not _is_git_repo():
        return jsonify({"setup": False, "reason": "Git repository not initialized."})

    remotes = _list_remotes()
    if not remotes:
        return jsonify({"setup": False, "reason": "No git remote configured."})

    raw = _run_git("status", "--porcelain", check=False).stdout
    return jsonify({
        "setup": True,
        "branch": _current_branch(),
        "remotes": remotes,
        "uncommitted_changes": bool(raw.strip()),
        "raw_status": raw.splitlines(),
    })


@app.post("/api/git/push")
def api_git_push():
    ensure_env_ignored()
    payload = request.get_json(silent=True) or {}
    message = payload.get("message", "Dev Studio Auto-Commit")
    remote = payload.get("remote")

    if not remote:
        remotes = _list_remotes()
        if not remotes:
            return jsonify({"success": False, "error": "No git remote configured."}), 400
        remote = next(iter(remotes))

    if remote not in _list_remotes():
        return jsonify({"success": False, "error": f"Unknown remote '{remote}'."}), 400

    result = perform_git_push(message, remote)

    if not result.success and result.leaks:
        return jsonify({
            "success": False,
            "error": "Security scan failed — potential hardcoded secrets.",
            "leaks": [leak.__dict__ for leak in result.leaks],
        }), 400

    status = 200 if result.success else 500
    return jsonify({"success": result.success, "message": result.message}), status


@app.post("/api/bot/reload-cogs")
def api_reload_cogs():
    reloaded, failed = [], []
    for extension in list(bot.extensions.keys()):
        try:
            bot.reload_extension(extension)
            reloaded.append(extension)
        except Exception as exc:
            failed.append({"cog": extension, "error": str(exc)})
    return jsonify({"reloaded": reloaded, "failed": failed})


def run_flask() -> None:
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False, use_reloader=False)


def _normalize(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_same_script(proc: psutil.Process) -> bool:
    try:
        cmdline = proc.info.get("cmdline") or []
        cwd = proc.cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    if len(cmdline) < 2:
        return False

    for raw_arg in cmdline[1:]:
        if not raw_arg:
            continue
        candidate = raw_arg if os.path.isabs(raw_arg) else os.path.join(cwd, raw_arg)
        if _normalize(candidate) == _normalize(SCRIPT_PATH):
            return True
    return False


def close_previous_instances() -> None:
    protected = {os.getpid(), os.getppid()}
    closed: list[int] = []

    for proc in psutil.process_iter(["pid", "cmdline"]):
        pid = proc.info.get("pid")
        if pid in protected or not _is_same_script(proc):
            continue

        try:
            proc.terminate()
            proc.wait(timeout=5)
            closed.append(pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.TimeoutExpired:
            try:
                proc.kill()
                closed.append(pid)
            except psutil.Error as exc:
                warn(f"Could not close previous instance (PID {pid}): {exc}")
        except psutil.Error as exc:
            warn(f"Could not close previous instance (PID {pid}): {exc}")

    if closed:
        info(f"Closed previous instance(s): {', '.join(map(str, closed))}")
    else:
        debug("No previous instance was running.")


def _bot_child_argv() -> list[str]:
    return [sys.executable, str(SCRIPT_PATH), "--bot-child"]


def run_bot_child() -> None:
    from bot import TOKEN as CHILD_TOKEN, bot as CHILD_BOT
    set_bot(CHILD_BOT)
    try:
        CHILD_BOT.run(CHILD_TOKEN)
    except KeyboardInterrupt:
        pass


MENU = """
Select Mode:
  [1] GitHub Tools
  [2] Start Bot & Web Dashboard
  [3] Exit
"""


def _start_bot_mode(flask_state: dict[str, object]) -> None:
    if not TOKEN:
        error("DISCORD_TOKEN is missing. Set it in your .env file.")
        return

    if not flask_state["started"]:
        threading.Thread(target=run_flask, daemon=True, name="flask-dashboard").start()
        flask_state["started"] = True
        info(f"Dev Dashboard active at http://{get_local_ip()}:{DASHBOARD_PORT}")

    if flask_state.get("bot_proc") and flask_state["bot_proc"].poll() is None:
        warn("Bot is already running. Press Ctrl+C here to stop it, or wait.")
        return

    info("Launching bot in child process...")
    proc = subprocess.Popen(_bot_child_argv(), cwd=PROJECT_ROOT)
    flask_state["bot_proc"] = proc

    try:
        proc.wait()
    except KeyboardInterrupt:
        info("Stopping bot via Ctrl+C...")
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception as exc:
            warn(f"Could not stop bot cleanly: {exc}")
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    info("Bot stopped. Returning to main menu...")
    flask_state["bot_proc"] = None
    _drain_stdin()


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        pass


def main() -> None:
    set_bot(bot)
    _install_signal_handlers()

    flask_state: dict[str, object] = {"started": False, "bot_proc": None}
    clear_terminal()

    while True:
        try:
            print(MENU)
            key = read_key("Press 1, 2, or 3: ")

            if key == "1":
                clear_terminal()
                run_github_cli()
                clear_terminal()
            elif key == "2":
                clear_terminal()
                _start_bot_mode(flask_state)
                clear_terminal()
            elif key in ("3", "q", "\x1b"):
                proc = flask_state.get("bot_proc")
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                clear_terminal()
                info("Goodbye.")
                return
            else:
                clear_terminal()
                warn("Invalid key. Press 1, 2, or 3.")
        except KeyboardInterrupt:
            _drain_stdin()
            clear_terminal()
            info("Returning to main menu...")


if __name__ == "__main__":
    if "--bot-child" in sys.argv:
        run_bot_child()
    else:
        main()