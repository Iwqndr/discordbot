import os
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from math import log2
from pathlib import Path

import flask
import psutil
from flask import jsonify, request

from bot import bot, TOKEN
from dashboard import app, set_bot
from console import debug, error, info, warn

set_bot(bot)

DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
SCRIPT_PATH = os.path.abspath(__file__)
PROJECT_ROOT = Path(SCRIPT_PATH).parent

logging.getLogger("werkzeug").setLevel(logging.ERROR)
flask.cli.show_server_banner = lambda *args, **kwargs: None


# ─────────────────────────────────────────────────────────────────────────────
# Latency helper — Discord returns nan/inf before the first heartbeat, and a
# naive round() crashes the panel. Every reader routes through this.
# ─────────────────────────────────────────────────────────────────────────────

def safe_latency_ms() -> int:
    import math
    try:
        latency = bot.latency
    except Exception:
        return 0
    if latency is None:
        return 0
    try:
        if math.isnan(latency) or math.isinf(latency):
            return 0
        return max(0, round(latency * 1000))
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Secret scanner — kept identical to the Dev Studio version, minus the menu.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_COMMIT_MESSAGE = "new update"

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

KNOWN_SECRET_PREFIXES = (
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

# Lines that look like secrets but are safe to publish. Supabase's anon JWT
# is designed to ship in client code; the service_role key is the one that
# must never leave the .env.
SCANNER_ALLOWLIST = (
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
    if any(allowed in line for allowed in SCANNER_ALLOWLIST):
        return False
    if any(p.search(line) for p in KNOWN_SECRET_PREFIXES):
        return True
    m = SECRET_NAME_HINT.search(line)
    if m and _looks_like_secret_value(m.group("value")):
        return True
    return False


def _iter_scannable_files():
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in files:
            path = Path(root, name)
            if path.suffix in SCANNABLE_EXTENSIONS:
                yield path


def scan_for_secrets() -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    seen = set()

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


# ─────────────────────────────────────────────────────────────────────────────
# Git operations
# ─────────────────────────────────────────────────────────────────────────────

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


def _run_git(*args, check=False):
    try:
        return subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=check,
        )
    except FileNotFoundError:
        class _Missing:
            returncode = 127
            stdout = ""
            stderr = "git is not installed or not on PATH"
        return _Missing()


def _is_git_repo() -> bool:
    return (PROJECT_ROOT / ".git").is_dir()


def _has_changes() -> bool:
    return bool(_run_git("status", "--porcelain").stdout.strip())


def _current_branch() -> str:
    res = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    return res.stdout.strip() or "(unknown)"


def _list_remotes() -> dict:
    res = _run_git("remote", "-v")
    remotes = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in remotes:
            remotes[parts[0]] = parts[1]
    return remotes


def _list_tracked_files() -> list:
    res = _run_git("ls-files")
    if res.returncode != 0:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def _is_path_ignored(rel_path: str) -> bool:
    res = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", rel_path],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0


def _iter_ignored_but_tracked() -> list:
    if not _is_git_repo():
        return []
    return [p for p in _list_tracked_files() if _is_path_ignored(p)]


def _untrack_stale_ignored_files() -> list:
    """Remove ignore-matched files from git's index, keeping them on disk."""
    stale = _iter_ignored_but_tracked()
    if not stale:
        return []

    removed = []
    for rel in stale:
        res = subprocess.run(
            ["git", "rm", "-r", "--cached", "--quiet", "--", rel],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            removed.append(rel)

    if removed:
        info(f"Untracked {len(removed)} ignore-matched file(s):")
        for rel in removed:
            info(f"    - {rel}")

    return removed


def perform_git_push(commit_message: str, remote_name: str) -> GitResult:
    if not _is_git_repo():
        return GitResult(success=False, message="Not a git repository.")

    _untrack_stale_ignored_files()

    leaks = scan_for_secrets()
    if leaks:
        return GitResult(success=False, message="Hardcoded secrets detected.", leaks=leaks)

    if not _has_changes():
        return GitResult(success=True, message="No changes to commit.")

    try:
        r = _run_git("add", ".", check=True)
        if r.returncode != 0:
            return GitResult(success=False, message=f"git add failed: {r.stderr.strip()}")
        r = _run_git("commit", "-m", commit_message or DEFAULT_COMMIT_MESSAGE, check=True)
        if r.returncode != 0:
            return GitResult(success=False, message=f"git commit failed: {r.stderr.strip()}")
        r = _run_git("push", remote_name, check=True)
        if r.returncode != 0:
            return GitResult(success=False, message=f"git push failed: {r.stderr.strip()}")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or str(exc)
        return GitResult(success=False, message=f"Git operation failed: {stderr}")

    return GitResult(success=True, message=f"Successfully pushed to '{remote_name}'.")


# ─────────────────────────────────────────────────────────────────────────────
# Mini UI — /git
# ─────────────────────────────────────────────────────────────────────────────

GIT_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Push to GitHub</title>
<style>
  :root {
    --bg: #f4efe6; --card: #fdfbf6; --border: #c9c0ad; --border-soft: #e5ddcc;
    --text: #3a342a; --muted: #7a7161;
    --accent: #6ea8c9; --accent-dark: #4a7f9e; --accent-soft: #cde4f0;
    --good: #5a9a68; --bad: #bd5555; --warn: #c98f26;
  }
  [data-theme="dark"] {
    --bg: #1a1815; --card: #232019; --border: #3d3830; --border-soft: #302b25;
    --text: #f0ebe0; --muted: #9a9284;
    --accent: #7bb8d9; --accent-dark: #a8d4ec; --accent-soft: #2c4354;
    --good: #7bd88f; --bad: #e58a8a; --warn: #e5b23c;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    background-color: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    min-height: 100vh; line-height: 1.5; -webkit-font-smoothing: antialiased;
    transition: background-color 0.3s ease, color 0.3s ease;
  }
  .wrap {
    max-width: 520px; margin: 0 auto; padding: 60px 22px 80px;
    display: flex; flex-direction: column; align-items: center; gap: 26px;
  }
  .icon-btn {
    position: fixed; top: 18px; right: 18px;
    width: 40px; height: 40px; border-radius: 12px;
    background: var(--card); border: 2px solid var(--border); color: var(--text);
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; transition: transform 0.2s ease, border-color 0.2s ease;
  }
  .icon-btn:hover { transform: translateY(-2px); border-color: var(--accent-dark); }
  .icon-btn svg { width: 18px; height: 18px; }
  h1 {
    font-size: 30px; font-weight: 700; text-align: center;
    letter-spacing: -0.01em;
  }
  .sub { color: var(--muted); font-size: 15px; text-align: center; max-width: 340px; }
  .card {
    width: 100%; background: var(--card); border: 2px solid var(--border);
    border-radius: 20px; padding: 24px;
  }
  .repo {
    display: flex; flex-direction: column; gap: 8px;
    font-size: 13px; color: var(--muted); margin-bottom: 20px;
    padding-bottom: 18px; border-bottom: 1.5px solid var(--border-soft);
  }
  .repo b { color: var(--text); font-weight: 600; }
  .repo code {
    font-family: "SF Mono", Consolas, monospace; font-size: 12px;
    color: var(--text); background: var(--bg); padding: 2px 6px;
    border-radius: 6px; border: 1.5px solid var(--border-soft);
    word-break: break-all;
  }
  .push-btn {
    width: 100%; padding: 16px 24px; border-radius: 14px;
    background: var(--accent-dark); border: 2px solid var(--accent-dark);
    color: #fff; font-size: 16px; font-weight: 700; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 10px;
    transition: transform 0.15s ease, filter 0.15s ease;
  }
  .push-btn:hover:not(:disabled) { transform: translateY(-2px); filter: brightness(1.05); }
  .push-btn:active:not(:disabled) { transform: translateY(0); }
  .push-btn:disabled { opacity: 0.6; cursor: not-allowed; }
  .push-btn svg { width: 18px; height: 18px; }
  .status {
    margin-top: 18px; padding: 14px 16px; border-radius: 14px;
    font-size: 13.5px; font-weight: 600; display: none;
    border: 2px solid var(--border-soft); background: var(--bg);
    white-space: pre-wrap; word-break: break-word;
    max-height: 260px; overflow-y: auto;
    font-family: "SF Mono", Consolas, monospace;
  }
  .status.show { display: block; }
  .status.ok { border-color: var(--good); color: var(--good); }
  .status.err { border-color: var(--bad); color: var(--bad); }
  .status.working { border-color: var(--warn); color: var(--warn); }
  .leaks { margin-top: 14px; padding: 12px 14px; border-radius: 12px;
    background: var(--bg); border: 1.5px solid var(--bad);
    font-size: 12.5px; font-family: "SF Mono", Consolas, monospace;
    display: none;
  }
  .leaks.show { display: block; }
  .leaks div { padding: 4px 0; word-break: break-all; }
  .leaks b { color: var(--bad); }
  @keyframes spin { to { transform: rotate(360deg); } }
  .spin { animation: spin 0.9s linear infinite; }
</style>
</head>
<body>
<button class="icon-btn" id="themeBtn" title="Toggle theme" aria-label="Toggle theme">
  <svg id="themeIcon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="4"></circle>
    <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"></path>
  </svg>
</button>

<div class="wrap">
  <h1>Push to GitHub</h1>
  <p class="sub">Scans for secrets, then commits and pushes everything in this repo.</p>

  <div class="card">
    <div class="repo" id="repoInfo">
      <div>Loading repo info…</div>
    </div>

    <button class="push-btn" id="pushBtn">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <line x1="12" y1="19" x2="12" y2="5"></line>
        <polyline points="5 12 12 5 19 12"></polyline>
      </svg>
      <span id="pushBtnLabel">Push to GitHub</span>
    </button>

    <div class="status" id="status"></div>
    <div class="leaks" id="leaks"></div>
  </div>
</div>

<script>
  const THEME_KEY = "git-push-theme";
  const state = {
    theme: localStorage.getItem(THEME_KEY) || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'),
    pushing: false,
  };

  document.documentElement.setAttribute("data-theme", state.theme);
  paintTheme();

  function paintTheme() {
    const icon = document.getElementById("themeIcon");
    if (state.theme === "dark") {
      icon.innerHTML = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>';
    } else {
      icon.innerHTML = '<circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"></path>';
    }
  }

  document.getElementById("themeBtn").onclick = () => {
    state.theme = state.theme === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", state.theme);
    localStorage.setItem(THEME_KEY, state.theme);
    paintTheme();
  };

  const statusEl = document.getElementById("status");
  const leaksEl = document.getElementById("leaks");
  const pushBtn = document.getElementById("pushBtn");
  const pushBtnLabel = document.getElementById("pushBtnLabel");

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = "status show" + (kind ? " " + kind : "");
  }

  function setLeaks(findings) {
    if (!findings || !findings.length) {
      leaksEl.className = "leaks";
      leaksEl.innerHTML = "";
      return;
    }
    leaksEl.innerHTML = "<b>" + findings.length + " secret(s) found — push blocked:</b>" +
      findings.map(f => "<div>" + f.file + ":" + f.line + " → " + f.snippet + "</div>").join("");
    leaksEl.className = "leaks show";
  }

  async function loadRepoInfo() {
    try {
      const res = await fetch("/api/git/status");
      const data = await res.json();
      const el = document.getElementById("repoInfo");
      if (!data.setup) {
        el.innerHTML = "<div>⚠ " + (data.reason || "Git not set up.") + "</div>";
        return;
      }
      const remoteName = Object.keys(data.remotes)[0] || "(none)";
      const remoteUrl = data.remotes[remoteName] || "(none)";
      el.innerHTML =
        "<div><b>Branch</b> <code>" + data.branch + "</code></div>" +
        "<div><b>Remote</b> <code>" + remoteName + "</code></div>" +
        "<div><b>URL</b> <code>" + remoteUrl + "</code></div>" +
        "<div><b>Changes</b> " + (data.uncommitted_changes ? "Yes — will be committed." : "None — nothing to push.") + "</div>" +
        (data.stale_ignored && data.stale_ignored.length
          ? "<div><b>Ignored (will untrack)</b> " + data.stale_ignored.length + " file(s)</div>"
          : "");
    } catch (e) {
      document.getElementById("repoInfo").innerHTML = "<div>⚠ Could not load repo info.</div>";
    }
  }

  async function push() {
    if (state.pushing) return;
    state.pushing = true;
    pushBtn.disabled = true;
    pushBtnLabel.textContent = "Pushing…";
    pushBtn.querySelector("svg").classList.add("spin");
    setStatus("Scanning for secrets…", "working");
    setLeaks(null);

    try {
      const res = await fetch("/api/git/push", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: "new update" }),
      });
      const data = await res.json();
      if (res.ok && data.success) {
        setStatus(data.message || "Push complete.", "ok");
      } else {
        setStatus(data.error || data.message || "Push failed.", "err");
        if (data.leaks) setLeaks(data.leaks);
      }
    } catch (e) {
      setStatus("Network error: " + e.message, "err");
    } finally {
      state.pushing = false;
      pushBtn.disabled = false;
      pushBtnLabel.textContent = "Push to GitHub";
      pushBtn.querySelector("svg").classList.remove("spin");
      loadRepoInfo();
    }
  }

  pushBtn.onclick = push;
  loadRepoInfo();
</script>
</body>
</html>
"""


@app.get("/git")
def git_ui():
    return flask.Response(GIT_UI_HTML, mimetype="text/html")


@app.get("/api/git/status")
def api_git_status():
    ensure_env_ignored()

    if not _is_git_repo():
        return jsonify({"setup": False, "reason": "Git repository not initialized."})

    remotes = _list_remotes()
    if not remotes:
        return jsonify({"setup": False, "reason": "No git remote configured."})

    raw = _run_git("status", "--porcelain").stdout
    return jsonify({
        "setup": True,
        "branch": _current_branch(),
        "remotes": remotes,
        "uncommitted_changes": bool(raw.strip()),
        "raw_status": raw.splitlines(),
        "stale_ignored": _iter_ignored_but_tracked(),
    })


@app.post("/api/git/push")
def api_git_push():
    ensure_env_ignored()
    payload = request.get_json(silent=True) or {}
    message = payload.get("message") or DEFAULT_COMMIT_MESSAGE
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


# ─────────────────────────────────────────────────────────────────────────────
# Existing plumbing
# ─────────────────────────────────────────────────────────────────────────────

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
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False, use_reloader=False)


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
    Flask from an older build would silently keep serving the panel and the
    browser would keep hitting a stale bot. Reclaim the port by force so the
    URL always answers from the live process.
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

    ensure_env_ignored()
    close_previous_instances()
    free_dashboard_port()

    local_ip = get_local_ip()
    threading.Thread(target=run_flask, daemon=True).start()
    info(f"Web dashboard listening on http://{local_ip}:{DASHBOARD_PORT}")
    info(f"Simple push UI: http://{local_ip}:{DASHBOARD_PORT}/git")

    bot.run(TOKEN)