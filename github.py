"""
GitHub pusher — scanner, git operations, richer UI.

Imported once by main.py so the routes register on the shared Flask app.
Holds no state the rest of the bot needs; safe to reload, safe to delete,
safe to leave in place.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from math import log2
from pathlib import Path

import flask
from flask import jsonify, request

from console import debug, info, warn, error

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_COMMIT_MESSAGE = "new update"

HISTORY_FILE = PROJECT_ROOT / "github_history.json"
HISTORY_LOCK = threading.Lock()
HISTORY_KEEP = 100


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Secret scanner
# ─────────────────────────────────────────────────────────────────────────────

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


def _run_git(*args, check=False, timeout=30):
    try:
        return subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )
    except FileNotFoundError:
        class _Missing:
            returncode = 127
            stdout = ""
            stderr = "git is not installed or not on PATH"
        return _Missing()
    except subprocess.TimeoutExpired:
        class _Timeout:
            returncode = 124
            stdout = ""
            stderr = "git command timed out"
        return _Timeout()


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


def _recent_commits(limit: int = 8) -> list:
    res = _run_git("log", f"-{limit}", "--pretty=format:%h|%s|%ar|%an|%at")
    if res.returncode != 0 or not res.stdout.strip():
        return []
    out = []
    for line in res.stdout.splitlines():
        parts = line.split("|", 4)
        if len(parts) < 5:
            continue
        out.append({
            "hash": parts[0],
            "subject": parts[1],
            "when": parts[2],
            "author": parts[3],
            "ts": int(parts[4]) if parts[4].isdigit() else 0,
        })
    return out


def _changed_files() -> list:
    """Every file git sees as changed, added, deleted or renamed.

    Uses `--porcelain` which gives us a 2-char status per line. Renders
    cleanly into the file-picker tab.
    """
    res = _run_git("status", "--porcelain")
    if res.returncode != 0:
        return []
    files = []
    for line in res.stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:].strip()
        # Handle `R  old -> new` renames
        if "->" in path:
            path = path.split("->", 1)[1].strip()
        files.append({
            "code": code.strip() or "??",
            "status": _status_label(code),
            "path": path,
        })
    return files


def _status_label(code: str) -> str:
    x, y = code[0], code[1] if len(code) > 1 else " "
    if code == "??":
        return "untracked"
    if "R" in code:
        return "renamed"
    if "A" in code:
        return "added"
    if "D" in code or x == "D" or y == "D":
        return "deleted"
    if "M" in code:
        return "modified"
    return "changed"


def _diff_stat() -> dict:
    """Additions/deletions across the whole working tree."""
    res = _run_git("diff", "--shortstat", "HEAD")
    out = {"additions": 0, "deletions": 0, "files": 0}
    if res.returncode != 0:
        return out
    text = res.stdout.strip()
    m = re.search(r"(\d+) files? changed", text)
    if m:
        out["files"] = int(m.group(1))
    m = re.search(r"(\d+) insertions?\(\+\)", text)
    if m:
        out["additions"] = int(m.group(1))
    m = re.search(r"(\d+) deletions?\(-\)", text)
    if m:
        out["deletions"] = int(m.group(1))
    return out


def _diff_for_file(path: str, max_lines: int = 400) -> dict:
    """Unified diff for one path, capped so a giant file can't lock the UI."""
    res = _run_git("diff", "HEAD", "--", path)
    if res.returncode != 0 or not res.stdout:
        # Untracked files have no diff against HEAD — show the whole content.
        p = PROJECT_ROOT / path
        if p.exists() and p.is_file():
            try:
                lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                return {"path": path, "lines": [], "truncated": False}
            lines = [{"type": "add", "text": "+" + l} for l in lines[:max_lines]]
            return {"path": path, "lines": lines, "truncated": len(lines) >= max_lines}
        return {"path": path, "lines": [], "truncated": False}

    lines = []
    truncated = False
    for raw in res.stdout.splitlines():
        if len(lines) >= max_lines:
            truncated = True
            break
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("@@"):
            lines.append({"type": "hunk", "text": raw})
        elif raw.startswith("+"):
            lines.append({"type": "add", "text": raw})
        elif raw.startswith("-"):
            lines.append({"type": "del", "text": raw})
        else:
            lines.append({"type": "ctx", "text": raw})
    return {"path": path, "lines": lines, "truncated": truncated}


def _list_branches() -> dict:
    local_res = _run_git("branch", "--format=%(refname:short)|%(upstream:short)|%(committerdate:relative)")
    remote_res = _run_git("branch", "-r", "--format=%(refname:short)|%(committerdate:relative)")
    local, remote = [], []
    for line in local_res.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) >= 1 and parts[0]:
            local.append({
                "name": parts[0],
                "upstream": parts[1] if len(parts) > 1 else "",
                "when": parts[2] if len(parts) > 2 else "",
            })
    for line in remote_res.stdout.splitlines():
        parts = line.split("|", 1)
        if parts and parts[0] and "HEAD" not in parts[0]:
            remote.append({
                "name": parts[0],
                "when": parts[1] if len(parts) > 1 else "",
            })
    return {"local": local, "remote": remote, "current": _current_branch()}


def _repo_stats() -> dict:
    total = _run_git("rev-list", "--count", "HEAD").stdout.strip()
    try:
        total = int(total)
    except ValueError:
        total = 0

    authors = {}
    res = _run_git("shortlog", "-sne", "HEAD")
    for line in res.stdout.splitlines():
        m = re.match(r"\s*(\d+)\s+(.+)$", line)
        if m:
            authors[m.group(2).strip()] = int(m.group(1))

    size_res = _run_git("count-objects", "-vH")
    size = ""
    for line in size_res.stdout.splitlines():
        if line.startswith("size-pack:"):
            size = line.split(":", 1)[1].strip()
            break

    tracked = len(_list_tracked_files())

    return {
        "commits": total,
        "contributors": sorted(
            [{"name": k, "commits": v} for k, v in authors.items()],
            key=lambda x: x["commits"], reverse=True,
        )[:10],
        "tracked_files": tracked,
        "size": size or "—",
        "branches": len(_list_branches()["local"]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Push history — persisted to disk
# ─────────────────────────────────────────────────────────────────────────────

def _load_history() -> list:
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save_history(rows: list) -> None:
    with HISTORY_LOCK:
        try:
            HISTORY_FILE.write_text(
                json.dumps(rows[-HISTORY_KEEP:], indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            warn(f"Could not save push history: {exc}")


def _record_push(entry: dict) -> None:
    rows = _load_history()
    rows.append(entry)
    _save_history(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Push pipeline
# ─────────────────────────────────────────────────────────────────────────────

def perform_git_push(commit_message: str, remote_name: str,
                     files: list | None = None, force: bool = False) -> GitResult:
    if not _is_git_repo():
        return GitResult(success=False, message="Not a git repository.")

    _untrack_stale_ignored_files()

    leaks = scan_for_secrets()
    if leaks:
        _record_push({
            "at": datetime.datetime.utcnow().isoformat(),
            "ok": False,
            "message": "Blocked by secret scanner",
            "leaks": len(leaks),
            "files": 0,
        })
        return GitResult(success=False, message="Hardcoded secrets detected.", leaks=leaks)

    if not _has_changes():
        return GitResult(success=True, message="No changes to commit — everything is already pushed.")

    # Optional selective commit: only stage the paths the operator ticked.
    if files:
        add_args = ["add", "--"] + [f for f in files if f]
        r = _run_git(*add_args, check=True)
    else:
        r = _run_git("add", ".", check=True)

    if r.returncode != 0:
        return GitResult(success=False, message=f"git add failed: {r.stderr.strip()}")

    r = _run_git("commit", "-m", commit_message or DEFAULT_COMMIT_MESSAGE, check=True)
    if r.returncode != 0:
        return GitResult(success=False, message=f"git commit failed: {r.stderr.strip()}")

    push_args = ["push", remote_name]
    if force:
        push_args.append("--force-with-lease")

    r = _run_git(*push_args, check=True, timeout=120)
    if r.returncode != 0:
        _record_push({
            "at": datetime.datetime.utcnow().isoformat(),
            "ok": False,
            "message": f"push failed: {r.stderr.strip()[:200]}",
            "files": len(files) if files else 0,
        })
        return GitResult(success=False, message=f"git push failed: {r.stderr.strip()}")

    # Look up the commit we just made to give the history entry real numbers.
    stat = _diff_stat()
    head = _run_git("rev-parse", "--short", "HEAD").stdout.strip()
    _record_push({
        "at": datetime.datetime.utcnow().isoformat(),
        "ok": True,
        "hash": head,
        "subject": commit_message or DEFAULT_COMMIT_MESSAGE,
        "remote": remote_name,
        "additions": stat["additions"],
        "deletions": stat["deletions"],
        "files_changed": stat["files"],
        "forced": force,
    })

    return GitResult(success=True, message=f"Pushed to '{remote_name}'.")


# ─────────────────────────────────────────────────────────────────────────────
# The UI
# ─────────────────────────────────────────────────────────────────────────────

GIT_UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitHub Pusher</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600;700&family=Nunito:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    /* Base palette */
    --bg: #f4efe6; --bg-dot: #d9d2c4;
    --card: #fdfbf6; --card-alt: #f9f5ec; --card-deep: #efe8da;
    --border: #c9c0ad; --border-soft: #e5ddcc;
    --text: #3a342a; --muted: #7a7161; --dim: #9e9584;

    /* Accent — darker for legible white text on buttons */
    --accent: #5a8fb0;
    --accent-dark: #3d6f8c;
    --accent-soft: #cde0ec;
    --accent-text: #2a4f66;

    /* Semantic */
    --good: #4a8a5c; --good-soft: #d6ebda; --good-text: #2a5a38;
    --bad: #b34848; --bad-soft: #f4dcdc; --bad-text: #7a2a2a;
    --warn: #b87d1e; --warn-soft: #f4e8cc; --warn-text: #6b4810;

    /* Code */
    --code-bg: #2b2620; --code-text: #f4efe6;

    --shadow-sm: 0 2px 0 rgba(58,52,42,0.05);
    --shadow: 0 4px 0 rgba(58,52,42,0.06);
    --shadow-lg: 0 10px 30px -8px rgba(58,52,42,0.18);
  }
  [data-theme="dark"] {
    --bg: #1a1815; --bg-dot: #2a2622;
    --card: #232019; --card-alt: #1e1c17; --card-deep: #2b2620;
    --border: #3d3830; --border-soft: #302b25;
    --text: #f0ebe0; --muted: #9a9284; --dim: #787063;

    --accent: #5a8fb0;
    --accent-dark: #7bafcc;
    --accent-soft: #2c4354;
    --accent-text: #a8d4ec;

    --good: #6fbf82; --good-soft: #1e3024; --good-text: #a3ddb1;
    --bad: #d97a7a; --bad-soft: #3a2020; --bad-text: #f0b8b8;
    --warn: #d4a03a; --warn-soft: #3a2f1a; --warn-text: #f0d488;

    --code-bg: #14120f; --code-text: #f0ebe0;

    --shadow-sm: 0 2px 0 rgba(0,0,0,0.25);
    --shadow: 0 4px 0 rgba(0,0,0,0.3);
    --shadow-lg: 0 10px 30px -8px rgba(0,0,0,0.5);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { scroll-behavior: smooth; }
  html, body {
    background-color: var(--bg);
    background-image: radial-gradient(var(--bg-dot) 1px, transparent 1px);
    background-size: 16px 16px;
    color: var(--text);
    font-family: "Nunito", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    min-height: 100vh; line-height: 1.5; -webkit-font-smoothing: antialiased;
    transition: background-color 0.3s ease, color 0.3s ease;
  }

  ::selection { background: var(--accent-soft); color: var(--accent-text); }
  ::-webkit-scrollbar { width: 9px; height: 9px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb {
    background: var(--border); border-radius: 99px;
    border: 2px solid transparent; background-clip: content-box;
  }
  ::-webkit-scrollbar-thumb:hover { background: var(--muted); background-clip: content-box; }

  button { font-family: inherit; color: inherit; cursor: pointer; }
  code, pre { font-family: "JetBrains Mono", monospace; }

  .wrap {
    max-width: 900px; margin: 0 auto;
    padding: 32px 22px 80px;
    display: flex; flex-direction: column; gap: 20px;
  }

  /* ─────────────────────── Top bar ─────────────────────── */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px; animation: fadeUp 0.5s ease both;
  }
  .brand {
    font-family: "Fredoka", sans-serif; font-weight: 600; font-size: 15px;
    color: var(--muted); letter-spacing: 0.02em;
    display: flex; align-items: center; gap: 9px;
  }
  .brand-dot {
    width: 9px; height: 9px; background: var(--accent-dark);
    border-radius: 50%; animation: pulse 2.4s ease-in-out infinite;
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-dark) 20%, transparent);
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.55; transform: scale(0.82); }
  }
  .topbar-right { display: flex; gap: 8px; }
  .icon-btn {
    width: 38px; height: 38px; border-radius: 11px;
    background: var(--card); border: 2px solid var(--border); color: var(--text);
    display: flex; align-items: center; justify-content: center;
    transition: transform 0.2s ease, border-color 0.2s ease, background 0.2s ease;
    -webkit-tap-highlight-color: transparent;
  }
  .icon-btn:hover { transform: translateY(-2px); border-color: var(--accent-dark); }
  .icon-btn.active { border-color: var(--accent-dark); background: var(--accent-soft); color: var(--accent-text); }
  .icon-btn svg { width: 17px; height: 17px; }

  /* ─────────────────────── Hero ─────────────────────── */
  .hero { text-align: center; animation: fadeUp 0.55s ease both; }
  h1 {
    font-family: "Fredoka", sans-serif; font-weight: 600;
    font-size: clamp(34px, 6vw, 46px);
    letter-spacing: -0.015em; margin-bottom: 8px; line-height: 1.05;
  }
  h1 .accent { color: var(--accent-dark); }
  .sub { color: var(--muted); font-size: 15px; font-weight: 600; }

  /* ─────────────────────── Tabs ─────────────────────── */
  .tabs {
    display: flex; gap: 6px; padding: 6px; border-radius: 16px;
    background: var(--card-alt); border: 2px solid var(--border);
    animation: fadeUp 0.6s ease both; overflow-x: auto;
    scrollbar-width: none;
  }
  .tabs::-webkit-scrollbar { display: none; }
  .tab {
    flex: 1; min-width: 90px;
    padding: 11px 14px; border-radius: 11px; border: none; background: transparent;
    font-size: 13.5px; font-weight: 700; color: var(--muted);
    display: flex; align-items: center; justify-content: center; gap: 7px;
    transition: background 0.2s ease, color 0.2s ease, transform 0.15s ease;
    white-space: nowrap;
  }
  .tab:hover { color: var(--text); }
  .tab.active {
    background: var(--accent-dark); color: #fff;
    box-shadow: 0 3px 0 rgba(0,0,0,0.12);
  }
  .tab svg { width: 15px; height: 15px; }
  .tab .count {
    font-size: 10.5px; font-weight: 800; padding: 1px 7px;
    border-radius: 999px; background: var(--card); color: var(--muted);
    min-width: 20px; text-align: center;
  }
  .tab.active .count { background: rgba(255,255,255,0.25); color: #fff; }

  /* ─────────────────────── Panels ─────────────────────── */
  .panel { display: none; animation: fadeUp 0.4s ease both; }
  .panel.active { display: flex; flex-direction: column; gap: 16px; }

  /* ─────────────────────── Cards ─────────────────────── */
  .card {
    background: var(--card); border: 2px solid var(--border);
    border-radius: 18px; padding: 20px;
    box-shadow: var(--shadow);
  }
  .card-title {
    font-family: "Fredoka", sans-serif; font-weight: 600;
    font-size: 15.5px; display: flex; align-items: center; gap: 9px;
  }
  .card-title svg { width: 17px; height: 17px; color: var(--accent-dark); flex-shrink: 0; }
  .card-sub {
    font-size: 12.5px; color: var(--muted); font-weight: 600;
    margin-top: 2px; line-height: 1.5;
  }

  /* ─────────────────────── Info rows ─────────────────────── */
  .info-grid { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
  .info-row {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 13px; border-radius: 12px;
    background: var(--card-alt); border: 1.5px solid var(--border-soft);
    font-size: 13px;
    transition: border-color 0.18s ease;
  }
  .info-row:hover { border-color: var(--border); }
  .info-row .k {
    font-size: 10.5px; font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase; color: var(--muted);
    min-width: 76px; flex-shrink: 0;
  }
  .info-row .v {
    flex: 1; min-width: 0; font-weight: 600; color: var(--text);
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  .info-row code {
    font-size: 12px; background: var(--bg); padding: 3px 8px;
    border-radius: 7px; border: 1.5px solid var(--border-soft);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    max-width: 100%;
  }
  .copy-btn {
    background: transparent; border: 1.5px solid var(--border);
    color: var(--muted); border-radius: 8px;
    width: 24px; height: 24px; display: flex; align-items: center; justify-content: center;
    transition: all 0.18s ease; flex-shrink: 0;
  }
  .copy-btn:hover { border-color: var(--accent-dark); color: var(--accent-dark); }
  .copy-btn.copied { border-color: var(--good); color: var(--good); }
  .copy-btn svg { width: 12px; height: 12px; }

  /* ─────────────────────── Badges ─────────────────────── */
  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 11.5px; font-weight: 800; padding: 3px 10px;
    border-radius: 999px; white-space: nowrap;
    border: 1.5px solid var(--border);
    background: var(--card-alt); color: var(--muted);
  }
  .badge.good { color: var(--good-text); border-color: var(--good); background: var(--good-soft); }
  .badge.bad { color: var(--bad-text); border-color: var(--bad); background: var(--bad-soft); }
  .badge.warn { color: var(--warn-text); border-color: var(--warn); background: var(--warn-soft); }
  .badge.accent { color: var(--accent-text); border-color: var(--accent); background: var(--accent-soft); }
  .badge .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }

  /* ─────────────────────── Push button ─────────────────────── */
  .push-btn {
    width: 100%; padding: 18px 24px; border-radius: 16px;
    background: var(--accent-dark); border: 2px solid var(--accent-dark);
    color: #fff; font-family: "Fredoka", sans-serif; font-weight: 600;
    font-size: 17px; letter-spacing: 0.01em;
    display: flex; align-items: center; justify-content: center; gap: 12px;
    transition: transform 0.18s cubic-bezier(0.34, 1.56, 0.64, 1),
                filter 0.18s ease, box-shadow 0.18s ease;
    box-shadow: 0 4px 0 color-mix(in srgb, var(--accent-dark) 70%, #000 30%);
    -webkit-tap-highlight-color: transparent;
  }
  .push-btn:hover:not(:disabled) {
    transform: translateY(-2px); filter: brightness(1.08);
    box-shadow: 0 6px 0 color-mix(in srgb, var(--accent-dark) 70%, #000 30%);
  }
  .push-btn:active:not(:disabled) {
    transform: translateY(2px);
    box-shadow: 0 1px 0 color-mix(in srgb, var(--accent-dark) 70%, #000 30%);
  }
  .push-btn:disabled { opacity: 0.65; cursor: not-allowed; }
  .push-btn svg { width: 20px; height: 20px; }

  .btn-row { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
  .btn {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 10px 16px; border-radius: 12px; font-size: 13px; font-weight: 700;
    background: var(--card); border: 2px solid var(--border); color: var(--text);
    transition: transform 0.16s ease, border-color 0.16s ease, background 0.16s ease;
    box-shadow: var(--shadow-sm);
  }
  .btn:hover:not(:disabled) { transform: translateY(-1px); border-color: var(--accent-dark); }
  .btn:disabled { opacity: 0.55; cursor: not-allowed; }
  .btn svg { width: 15px; height: 15px; }
  .btn.danger { color: var(--bad-text); border-color: var(--bad); }
  .btn.danger:hover:not(:disabled) { background: var(--bad-soft); }
  .btn.primary { background: var(--accent-dark); border-color: var(--accent-dark); color: #fff; }
  .btn.primary:hover:not(:disabled) { filter: brightness(1.08); }
  .btn.sm { padding: 7px 12px; font-size: 12px; border-radius: 10px; }

  /* ─────────────────────── Inputs ─────────────────────── */
  .field { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
  .field label {
    font-size: 10.5px; font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase; color: var(--muted);
  }
  .input, .textarea, .select {
    width: 100%; font-family: inherit; font-size: 13.5px; font-weight: 600;
    color: var(--text); background: var(--bg); border: 2px solid var(--border);
    border-radius: 12px; padding: 11px 14px; outline: none;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }
  .textarea { resize: vertical; min-height: 74px; line-height: 1.5; }
  .input:focus, .textarea:focus, .select:focus {
    border-color: var(--accent-dark);
    box-shadow: 0 0 0 3px var(--accent-soft);
  }

  /* ─────────────────────── File picker ─────────────────────── */
  .file-list { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
  .file-row {
    display: flex; align-items: center; gap: 11px;
    padding: 10px 13px; border-radius: 12px;
    background: var(--card-alt); border: 1.5px solid var(--border-soft);
    cursor: pointer; transition: border-color 0.16s ease, background 0.16s ease;
    animation: fadeUp 0.35s ease both;
    animation-delay: calc(var(--i, 0) * 25ms);
  }
  .file-row:hover { border-color: var(--accent-dark); background: var(--card); }
  .file-row.on { border-color: var(--accent-dark); background: var(--accent-soft); }
  .file-check {
    width: 18px; height: 18px; border-radius: 6px; flex-shrink: 0;
    border: 2px solid var(--border); background: var(--card);
    display: grid; place-items: center; color: transparent;
    transition: all 0.15s ease;
  }
  .file-row.on .file-check {
    background: var(--accent-dark); border-color: var(--accent-dark); color: #fff;
  }
  .file-check svg { width: 12px; height: 12px; }
  .file-status {
    font-family: "JetBrains Mono", monospace; font-size: 10px; font-weight: 700;
    padding: 2px 8px; border-radius: 6px; text-transform: uppercase;
    letter-spacing: 0.04em; flex-shrink: 0;
    border: 1.5px solid var(--border); color: var(--muted); background: var(--card);
  }
  .file-status.added { color: var(--good-text); border-color: var(--good); background: var(--good-soft); }
  .file-status.modified { color: var(--warn-text); border-color: var(--warn); background: var(--warn-soft); }
  .file-status.deleted { color: var(--bad-text); border-color: var(--bad); background: var(--bad-soft); }
  .file-status.untracked { color: var(--accent-text); border-color: var(--accent); background: var(--accent-soft); }
  .file-path {
    flex: 1; min-width: 0; font-family: "JetBrains Mono", monospace;
    font-size: 12.5px; font-weight: 500; color: var(--text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .file-diff-btn {
    background: transparent; border: 1.5px solid var(--border);
    color: var(--muted); border-radius: 7px; width: 26px; height: 26px;
    display: grid; place-items: center; flex-shrink: 0;
    transition: all 0.16s ease;
  }
  .file-diff-btn:hover { border-color: var(--accent-dark); color: var(--accent-dark); }
  .file-diff-btn svg { width: 13px; height: 13px; }

  /* ─────────────────────── Diff modal ─────────────────────── */
  .modal {
    position: fixed; inset: 0; z-index: 100;
    display: flex; align-items: center; justify-content: center;
    padding: 20px; opacity: 0; pointer-events: none;
    transition: opacity 0.25s ease;
  }
  .modal.open { opacity: 1; pointer-events: auto; }
  .modal-bg {
    position: absolute; inset: 0; background: rgba(0,0,0,0.55);
    backdrop-filter: blur(4px);
  }
  .modal-card {
    position: relative; z-index: 2;
    width: min(960px, 96vw); height: min(760px, 88vh);
    background: var(--card); border: 2px solid var(--border);
    border-radius: 20px; display: flex; flex-direction: column; overflow: hidden;
    box-shadow: var(--shadow-lg);
    transform: scale(0.96); transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .modal.open .modal-card { transform: scale(1); }
  .modal-head {
    padding: 16px 20px; display: flex; align-items: center; gap: 12px;
    border-bottom: 2px solid var(--border-soft);
  }
  .modal-head .t {
    flex: 1; min-width: 0;
    font-family: "Fredoka", sans-serif; font-weight: 600; font-size: 15.5px;
    display: flex; align-items: center; gap: 9px;
  }
  .modal-head .t code {
    font-size: 12.5px; font-weight: 500; color: var(--muted);
    background: var(--bg); padding: 3px 9px; border-radius: 7px;
    border: 1.5px solid var(--border-soft);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    max-width: 100%;
  }
  .modal-body {
    flex: 1; overflow-y: auto; padding: 0;
    background: var(--code-bg); color: var(--code-text);
  }
  .diff-line {
    display: flex; padding: 2px 16px; font-size: 12.5px;
    font-family: "JetBrains Mono", monospace; line-height: 1.55;
    white-space: pre; overflow-x: auto;
  }
  .diff-line.add { background: rgba(74, 138, 92, 0.16); color: #a3ddb1; }
  .diff-line.del { background: rgba(179, 72, 72, 0.16); color: #f0b8b8; }
  .diff-line.hunk { background: rgba(90, 143, 176, 0.14); color: #a8d4ec; font-weight: 600; }
  .diff-line.ctx { color: var(--code-text); opacity: 0.72; }
  .diff-truncated {
    padding: 12px 16px; text-align: center; font-size: 12px;
    color: var(--muted); font-weight: 600;
    border-top: 1.5px solid var(--border-soft);
    background: var(--card);
  }

  /* ─────────────────────── History timeline ─────────────────────── */
  .timeline { display: flex; flex-direction: column; gap: 10px; margin-top: 12px; }
  .timeline-item {
    display: flex; gap: 12px; padding: 12px 14px; border-radius: 14px;
    background: var(--card-alt); border: 2px solid var(--border-soft);
    animation: fadeUp 0.4s ease both;
    animation-delay: calc(var(--i, 0) * 40ms);
    transition: border-color 0.18s ease;
  }
  .timeline-item:hover { border-color: var(--border); }
  .timeline-item.ok { border-left: 4px solid var(--good); }
  .timeline-item.fail { border-left: 4px solid var(--bad); }
  .timeline-icon {
    width: 34px; height: 34px; border-radius: 10px; flex-shrink: 0;
    display: grid; place-items: center;
    background: var(--card); border: 1.5px solid var(--border-soft);
  }
  .timeline-item.ok .timeline-icon { color: var(--good); border-color: var(--good); background: var(--good-soft); }
  .timeline-item.fail .timeline-icon { color: var(--bad); border-color: var(--bad); background: var(--bad-soft); }
  .timeline-icon svg { width: 16px; height: 16px; }
  .timeline-body { flex: 1; min-width: 0; }
  .timeline-top {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  .timeline-hash {
    font-family: "JetBrains Mono", monospace; font-size: 11.5px; font-weight: 600;
    background: var(--bg); padding: 2px 8px; border-radius: 6px;
    border: 1.5px solid var(--border-soft); color: var(--accent-dark);
  }
  .timeline-subject {
    font-weight: 700; font-size: 13.5px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    flex: 1; min-width: 0;
  }
  .timeline-meta {
    font-size: 11.5px; color: var(--muted); font-weight: 600;
    display: flex; gap: 12px; margin-top: 3px; flex-wrap: wrap;
  }
  .timeline-stats {
    display: flex; gap: 8px; font-family: "JetBrains Mono", monospace;
    font-size: 11px; font-weight: 600;
  }
  .timeline-stats .add { color: var(--good-text); }
  .timeline-stats .del { color: var(--bad-text); }

  /* ─────────────────────── Branches ─────────────────────── */
  .branch-list { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
  .branch-row {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 13px; border-radius: 12px;
    background: var(--card-alt); border: 1.5px solid var(--border-soft);
    font-size: 13px;
  }
  .branch-row.current { border-color: var(--accent-dark); background: var(--accent-soft); }
  .branch-row svg { width: 14px; height: 14px; color: var(--muted); flex-shrink: 0; }
  .branch-row.current svg { color: var(--accent-dark); }
  .branch-name {
    flex: 1; min-width: 0; font-family: "JetBrains Mono", monospace;
    font-size: 12.5px; font-weight: 600;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .branch-meta { font-size: 11px; color: var(--muted); font-weight: 600; }

  /* ─────────────────────── Stats grid ─────────────────────── */
  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px; margin-top: 12px;
  }
  .stat-tile {
    padding: 14px 16px; border-radius: 14px;
    background: var(--card-alt); border: 2px solid var(--border-soft);
    display: flex; flex-direction: column; gap: 3px;
    transition: transform 0.18s ease, border-color 0.18s ease;
    animation: fadeUp 0.4s ease both;
    animation-delay: calc(var(--i, 0) * 30ms);
  }
  .stat-tile:hover { transform: translateY(-2px); border-color: var(--accent-dark); }
  .stat-tile .k {
    font-size: 10.5px; font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase; color: var(--muted);
  }
  .stat-tile b {
    font-family: "Fredoka", sans-serif; font-weight: 600;
    font-size: 22px; line-height: 1.1; color: var(--text);
  }
  .stat-tile .sub { font-size: 11.5px; color: var(--muted); font-weight: 600; margin-top: 2px; }

  .contrib {
    display: flex; align-items: center; gap: 10px;
    padding: 9px 12px; border-radius: 11px;
    background: var(--card-alt); border: 1.5px solid var(--border-soft);
    margin-bottom: 6px;
  }
  .contrib-rank {
    width: 22px; height: 22px; border-radius: 7px; flex-shrink: 0;
    display: grid; place-items: center;
    font-family: "Fredoka", sans-serif; font-size: 11px; font-weight: 600;
    background: var(--card); border: 1.5px solid var(--border-soft);
    color: var(--muted);
  }
  .contrib-name { flex: 1; min-width: 0; font-weight: 700; font-size: 13px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .contrib-count {
    font-family: "JetBrains Mono", monospace; font-size: 11.5px;
    font-weight: 600; color: var(--accent-dark);
    padding: 2px 9px; border-radius: 6px;
    background: var(--bg); border: 1.5px solid var(--border-soft);
  }

  /* ─────────────────────── Toast ─────────────────────── */
  .toasts {
    position: fixed; bottom: 22px; right: 22px; z-index: 200;
    display: flex; flex-direction: column; gap: 8px; pointer-events: none;
  }
  .toast {
    pointer-events: auto;
    display: flex; align-items: center; gap: 10px;
    padding: 12px 16px; border-radius: 14px;
    background: var(--card); border: 2px solid var(--border);
    color: var(--text); font-size: 13px; font-weight: 700;
    box-shadow: var(--shadow-lg); max-width: 380px;
    animation: slideIn 0.3s cubic-bezier(0.16, 1, 0.3, 1) both;
  }
  .toast.ok { border-color: var(--good); color: var(--good-text); background: var(--good-soft); }
  .toast.err { border-color: var(--bad); color: var(--bad-text); background: var(--bad-soft); }
  .toast.info { border-color: var(--accent); color: var(--accent-text); background: var(--accent-soft); }
  .toast svg { width: 16px; height: 16px; flex-shrink: 0; }
  @keyframes slideIn {
    from { opacity: 0; transform: translateX(30px); }
    to { opacity: 1; transform: translateX(0); }
  }
  .toast.leaving { animation: slideOut 0.25s ease forwards; }
  @keyframes slideOut {
    to { opacity: 0; transform: translateX(30px); }
  }

  /* ─────────────────────── Loading skeleton ─────────────────────── */
  .skeleton {
    background: linear-gradient(
      90deg,
      var(--card-alt) 0%,
      var(--card-deep) 50%,
      var(--card-alt) 100%
    );
    background-size: 220% 100%;
    animation: shimmer 1.4s linear infinite;
    border-radius: 12px;
  }
  .skeleton.row { height: 44px; margin-bottom: 8px; }
  .skeleton.title { height: 20px; width: 40%; margin-bottom: 12px; }
  @keyframes shimmer {
    from { background-position: 140% 0; }
    to { background-position: -40% 0; }
  }

  /* ─────────────────────── Empty state ─────────────────────── */
  .empty {
    text-align: center; padding: 34px 18px; color: var(--muted);
    display: flex; flex-direction: column; align-items: center; gap: 8px;
  }
  .empty svg { width: 30px; height: 30px; opacity: 0.6; }
  .empty .t { font-weight: 700; color: var(--text); font-size: 14px; }
  .empty .s { font-size: 12.5px; font-weight: 600; max-width: 320px; }

  /* ─────────────────────── Footer ─────────────────────── */
  footer {
    text-align: center; color: var(--muted); font-size: 12px; font-weight: 600;
    margin-top: 16px;
  }
  footer code {
    font-size: 11.5px; background: var(--card); padding: 3px 8px;
    border-radius: 6px; border: 1.5px solid var(--border-soft);
  }

  /* ─────────────────────── Animations ─────────────────────── */
  @keyframes fadeUp {
    from { opacity: 0; transform: translateY(12px); }
    to { opacity: 1; transform: translateY(0); }
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .spin { animation: spin 0.85s linear infinite; }

  @media (max-width: 620px) {
    .wrap { padding: 20px 14px 60px; }
    .info-row .k { min-width: 62px; font-size: 10px; }
    .tab { min-width: auto; padding: 9px 10px; font-size: 12.5px; }
    .tab span:not(.count) { display: none; }
    .push-btn { font-size: 15.5px; padding: 16px 20px; }
  }
</style>
</head>
<body>

<div class="wrap">
  <div class="topbar">
    <div class="brand"><span class="brand-dot"></span>github pusher</div>
    <div class="topbar-right">
      <button class="icon-btn" id="pullBtn" title="Pull from remote">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="12" y1="5" x2="12" y2="19"></line>
          <polyline points="19 12 12 19 5 12"></polyline>
        </svg>
      </button>
      <button class="icon-btn" id="refreshBtn" title="Refresh data">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="23 4 23 10 17 10"></polyline>
          <polyline points="1 20 1 14 7 14"></polyline>
          <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>
        </svg>
      </button>
      <button class="icon-btn" id="themeBtn" title="Toggle theme">
        <svg id="themeIcon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="4"></circle>
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"></path>
        </svg>
      </button>
    </div>
  </div>

  <div class="hero">
    <h1>push to <span class="accent">github</span></h1>
    <p class="sub">Everything in one place. Scans, commits, pushes, tracks.</p>
  </div>

  <div class="tabs" id="tabs">
    <button class="tab active" data-tab="push">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>
      <span>Push</span>
    </button>
    <button class="tab" data-tab="files">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
      <span>Files</span>
      <span class="count" id="filesCount">0</span>
    </button>
    <button class="tab" data-tab="history">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
      <span>History</span>
    </button>
    <button class="tab" data-tab="branches">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>
      <span>Branches</span>
    </button>
    <button class="tab" data-tab="stats">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line></svg>
      <span>Stats</span>
    </button>
  </div>

  <!-- ══════════════════ PUSH TAB ══════════════════ -->
  <div class="panel active" id="panel-push">
    <div class="card">
      <div class="card-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"></path><path d="M7 12l4-4 4 4 6-6"></path></svg>
        Repository
      </div>
      <div class="card-sub">Where this push is going.</div>
      <div class="info-grid" id="repoInfo">
        <div class="skeleton title"></div>
        <div class="skeleton row"></div>
        <div class="skeleton row"></div>
        <div class="skeleton row"></div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"></polyline><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path></svg>
        Commit
      </div>
      <div class="card-sub">Scans for secrets before pushing. Blocks on any finding.</div>

      <div class="field">
        <label>Commit message</label>
        <input class="input" id="commitMsg" value="new update" placeholder="new update">
      </div>

      <div class="field">
        <label>Remote</label>
        <select class="select" id="remoteSelect">
          <option>Loading…</option>
        </select>
      </div>

      <label class="btn" style="margin-top:14px;cursor:pointer;user-select:none">
        <input type="checkbox" id="forcePush" style="width:16px;height:16px;accent-color:var(--bad)">
        Force push (--force-with-lease)
      </label>

      <button class="push-btn" id="pushBtn" style="margin-top:16px">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <line x1="12" y1="19" x2="12" y2="5"></line>
          <polyline points="5 12 12 5 19 12"></polyline>
        </svg>
        <span id="pushBtnLabel">Push to GitHub</span>
      </button>

      <div class="btn-row">
        <button class="btn sm" id="stashBtn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12H3"></path><path d="M18 6H6"></path><path d="M15 18H9"></path></svg>
          Stash changes
        </button>
        <button class="btn sm" id="stashPopBtn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 15 12 6 21 15"></polyline></svg>
          Pop stash
        </button>
        <button class="btn sm danger" id="discardBtn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path></svg>
          Discard all changes
        </button>
      </div>
    </div>
  </div>

  <!-- ══════════════════ FILES TAB ══════════════════ -->
  <div class="panel" id="panel-files">
    <div class="card">
      <div class="card-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
        Changed files
      </div>
      <div class="card-sub" id="filesSub">Tick the ones you want to commit — or leave them all unticked to commit everything.</div>
      <div class="btn-row">
        <button class="btn sm" id="filesAll">Select all</button>
        <button class="btn sm" id="filesNone">Clear all</button>
      </div>
      <div class="file-list" id="fileList"></div>
    </div>
  </div>

  <!-- ══════════════════ HISTORY TAB ══════════════════ -->
  <div class="panel" id="panel-history">
    <div class="card">
      <div class="card-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
        Push log
      </div>
      <div class="card-sub">Every push through this panel, newest first.</div>
      <div class="timeline" id="pushLog"></div>
    </div>

    <div class="card">
      <div class="card-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"></circle><line x1="1.05" y1="12" x2="7" y2="12"></line><line x1="17.01" y1="12" x2="22.96" y2="12"></line></svg>
        Recent commits
      </div>
      <div class="card-sub">The last few things you pushed, straight from git log.</div>
      <div class="timeline" id="commitList"></div>
    </div>
  </div>

  <!-- ══════════════════ BRANCHES TAB ══════════════════ -->
  <div class="panel" id="panel-branches">
    <div class="card">
      <div class="card-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>
        Local branches
      </div>
      <div class="card-sub">Click any branch to check it out.</div>
      <div class="branch-list" id="localBranches"></div>
    </div>

    <div class="card">
      <div class="card-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path></svg>
        Remote branches
      </div>
      <div class="card-sub">What exists on the remote. Read-only.</div>
      <div class="branch-list" id="remoteBranches"></div>
    </div>
  </div>

  <!-- ══════════════════ STATS TAB ══════════════════ -->
  <div class="panel" id="panel-stats">
    <div class="card">
      <div class="card-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line></svg>
        Repo stats
      </div>
      <div class="stats-grid" id="statsGrid"></div>
    </div>

    <div class="card">
      <div class="card-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg>
        Top contributors
      </div>
      <div class="card-sub">By commit count across the whole history.</div>
      <div id="contribList" style="margin-top:12px"></div>
    </div>
  </div>

  <footer>
    Serving on <code>0.0.0.0</code> · reachable from any device on your network
  </footer>
</div>

<!-- Diff modal -->
<div class="modal" id="diffModal">
  <div class="modal-bg" id="diffBg"></div>
  <div class="modal-card">
    <div class="modal-head">
      <div class="t">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:17px;height:17px;color:var(--accent-dark);flex-shrink:0"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>
        <code id="diffPath">—</code>
      </div>
      <button class="icon-btn" id="diffClose" title="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
      </button>
    </div>
    <div class="modal-body" id="diffBody"></div>
  </div>
</div>

<div class="toasts" id="toasts"></div>

<script>
const THEME_KEY = "gitpush-theme";
const state = {
  theme: localStorage.getItem(THEME_KEY) ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
  pushing: false,
  tab: "push",
  selectedFiles: new Set(),
  repoInfo: null,
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

// ─── Utils ─────────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
  }[c]));
}

function toast(msg, kind) {
  const wrap = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = "toast " + (kind || "info");
  const icon = kind === "ok" ? "check-circle-2" :
               kind === "err" ? "alert-circle" : "info";
  el.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
    (kind === "ok"
      ? '<polyline points="20 6 9 17 4 12"></polyline>'
      : kind === "err"
        ? '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>'
        : '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line>') +
    '</svg><span>' + esc(msg) + '</span>';
  wrap.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 250);
  }, kind === "err" ? 4500 : 2800);
}

function bindCopyButtons(root) {
  root.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.onclick = async () => {
      const text = btn.getAttribute("data-copy");
      try { await navigator.clipboard.writeText(text); }
      catch (e) {
        const ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta);
        ta.select(); document.execCommand("copy"); ta.remove();
      }
      btn.classList.add("copied");
      setTimeout(() => btn.classList.remove("copied"), 1200);
    };
  });
}

function infoRow(label, valueHtml, opts = {}) {
  const copyBtn = opts.copyValue
    ? '<button class="copy-btn" data-copy="' + esc(opts.copyValue) + '" title="Copy"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg></button>'
    : "";
  return '<div class="info-row"><span class="k">' + esc(label) + '</span><span class="v">' + valueHtml + copyBtn + '</span></div>';
}

// ─── Tabs ─────────────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach(t => {
  t.onclick = () => {
    state.tab = t.dataset.tab;
    document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === t));
    document.querySelectorAll(".panel").forEach(p => {
      p.classList.toggle("active", p.id === "panel-" + state.tab);
    });
  };
});

// ─── Fetch helpers ────────────────────────────────────────────────────
async function getJSON(path) {
  const res = await fetch(path);
  return res.json().catch(() => ({}));
}

async function postJSON(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, data };
}

// ─── Repo info ────────────────────────────────────────────────────────
async function loadRepoInfo() {
  const el = document.getElementById("repoInfo");
  try {
    const data = await getJSON("/api/git/status");
    state.repoInfo = data;
    if (!data.setup) {
      el.innerHTML = '<div class="empty">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>' +
        '<div class="t">Not ready</div>' +
        '<div class="s">' + esc(data.reason || "Git not set up.") + '</div></div>';
      return;
    }
    const remotes = Object.entries(data.remotes || {});
    const remoteName = remotes.length ? remotes[0][0] : "(none)";
    const remoteUrl = remotes.length ? remotes[0][1] : "(none)";

    const changeBadge = data.uncommitted_changes
      ? '<span class="badge warn"><span class="dot"></span>' +
        (data.raw_status ? data.raw_status.length : 0) + ' uncommitted</span>'
      : '<span class="badge good"><span class="dot"></span>Up to date</span>';

    let html = "";
    html += infoRow("Branch", '<code>' + esc(data.branch) + '</code>', { copyValue: data.branch });
    html += infoRow("Remote", '<code>' + esc(remoteName) + '</code>');
    html += infoRow("URL", '<code>' + esc(remoteUrl) + '</code>', { copyValue: remoteUrl });
    html += infoRow("Status", changeBadge);

    if (data.stale_ignored && data.stale_ignored.length) {
      html += infoRow("Ignored", '<span class="badge accent"><span class="dot"></span>' +
        data.stale_ignored.length + ' will be untracked</span>');
    }

    el.innerHTML = html;
    bindCopyButtons(el);

    // Populate remote dropdown
    const sel = document.getElementById("remoteSelect");
    const current = sel.value;
    sel.innerHTML = remotes.map(([name, url]) =>
      '<option value="' + esc(name) + '">' + esc(name) + ' — ' + esc(url) + '</option>'
    ).join("") || '<option value="origin">origin</option>';
    if (current) sel.value = current;
  } catch (e) {
    el.innerHTML = '<div class="empty"><div class="t">Could not load repo info</div><div class="s">' + esc(e.message) + '</div></div>';
  }
}

// ─── Files ────────────────────────────────────────────────────────────
async function loadFiles() {
  const el = document.getElementById("fileList");
  const count = document.getElementById("filesCount");
  try {
    const data = await getJSON("/api/git/files");
    const files = data.files || [];
    count.textContent = files.length;

    if (!files.length) {
      el.innerHTML = '<div class="empty">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>' +
        '<div class="t">Nothing changed</div>' +
        '<div class="s">Working tree is clean. Push has nothing to do.</div></div>';
      return;
    }

    el.innerHTML = files.map((f, i) => {
      const on = state.selectedFiles.has(f.path);
      const cls = (f.status || "").replace(/\s+/g, "");
      return '<div class="file-row ' + (on ? "on" : "") + '" data-path="' + esc(f.path) + '" style="--i:' + i + '">' +
        '<div class="file-check"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg></div>' +
        '<span class="file-status ' + esc(cls) + '">' + esc(f.status) + '</span>' +
        '<span class="file-path">' + esc(f.path) + '</span>' +
        '<button class="file-diff-btn" data-diff="' + esc(f.path) + '" title="View diff"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><polyline points="19 12 12 19 5 12"></polyline></svg></button>' +
        '</div>';
    }).join("");

    el.querySelectorAll(".file-row").forEach(row => {
      row.onclick = (e) => {
        if (e.target.closest("[data-diff]")) return;
        const path = row.dataset.path;
        if (state.selectedFiles.has(path)) state.selectedFiles.delete(path);
        else state.selectedFiles.add(path);
        row.classList.toggle("on", state.selectedFiles.has(path));
      };
    });
    el.querySelectorAll("[data-diff]").forEach(btn => {
      btn.onclick = (e) => { e.stopPropagation(); openDiff(btn.dataset.diff); };
    });

  } catch (e) {
    el.innerHTML = '<div class="empty"><div class="t">Could not load files</div><div class="s">' + esc(e.message) + '</div></div>';
  }
}

document.getElementById("filesAll").onclick = () => {
  state.selectedFiles.clear();
  (state.repoInfo?.raw_status || []).forEach(() => {});
  document.querySelectorAll(".file-row").forEach(row => {
    state.selectedFiles.add(row.dataset.path);
    row.classList.add("on");
  });
};
document.getElementById("filesNone").onclick = () => {
  state.selectedFiles.clear();
  document.querySelectorAll(".file-row").forEach(row => row.classList.remove("on"));
};

// ─── Diff modal ───────────────────────────────────────────────────────
async function openDiff(path) {
  const modal = document.getElementById("diffModal");
  const body = document.getElementById("diffBody");
  document.getElementById("diffPath").textContent = path;
  body.innerHTML = '<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px">Loading diff…</div>';
  modal.classList.add("open");

  try {
    const data = await getJSON("/api/git/diff?path=" + encodeURIComponent(path));
    const lines = data.lines || [];
    if (!lines.length) {
      body.innerHTML = '<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px">No changes for this file.</div>';
      return;
    }
    body.innerHTML = lines.map(l =>
      '<div class="diff-line ' + esc(l.type) + '">' + esc(l.text) + '</div>'
    ).join("") + (data.truncated
      ? '<div class="diff-truncated">Diff truncated — too many lines to display.</div>'
      : "");
  } catch (e) {
    body.innerHTML = '<div style="padding:24px;text-align:center;color:var(--bad);font-size:13px">Could not load diff: ' + esc(e.message) + '</div>';
  }
}
document.getElementById("diffClose").onclick = () => document.getElementById("diffModal").classList.remove("open");
document.getElementById("diffBg").onclick = () => document.getElementById("diffModal").classList.remove("open");
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.getElementById("diffModal").classList.remove("open");
});

// ─── History ──────────────────────────────────────────────────────────
async function loadHistory() {
  // Push log
  const logEl = document.getElementById("pushLog");
  try {
    const data = await getJSON("/api/git/history");
    const rows = (data.pushes || []).slice().reverse();
    if (!rows.length) {
      logEl.innerHTML = '<div class="empty">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>' +
        '<div class="t">No pushes yet</div>' +
        '<div class="s">Every push through this panel will be logged here.</div></div>';
    } else {
      logEl.innerHTML = rows.map((row, i) => {
        const ok = row.ok !== false;
        const cls = ok ? "ok" : "fail";
        const when = row.at ? new Date(row.at).toLocaleString() : "";
        const hash = row.hash ? '<span class="timeline-hash">' + esc(row.hash) + '</span>' : "";
        const stats = ok && (row.additions || row.deletions)
          ? '<div class="timeline-stats">' +
              (row.additions ? '<span class="add">+' + row.additions + '</span>' : '') +
              (row.deletions ? '<span class="del">−' + row.deletions + '</span>' : '') +
            '</div>'
          : "";
        return '<div class="timeline-item ' + cls + '" style="--i:' + i + '">' +
          '<div class="timeline-icon">' +
            (ok
              ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>'
              : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>') +
          '</div>' +
          '<div class="timeline-body">' +
            '<div class="timeline-top">' + hash +
              '<span class="timeline-subject">' + esc(row.subject || row.message || "Push") + '</span>' +
            '</div>' +
            '<div class="timeline-meta"><span>' + esc(when) + '</span>' +
              (row.remote ? '<span>→ ' + esc(row.remote) + '</span>' : '') +
              (row.files_changed ? '<span>' + row.files_changed + ' files</span>' : '') +
              (row.forced ? '<span style="color:var(--bad)">forced</span>' : '') +
            '</div>' +
          '</div>' +
          stats +
        '</div>';
      }).join("");
    }
  } catch (e) {
    logEl.innerHTML = '<div class="empty"><div class="t">Could not load push log</div></div>';
  }

  // Recent commits
  const commitEl = document.getElementById("commitList");
  try {
    const data = await getJSON("/api/git/commits");
    const rows = data.commits || [];
    if (!rows.length) {
      commitEl.innerHTML = '<div class="empty"><div class="t">No commits yet</div></div>';
      return;
    }
    commitEl.innerHTML = rows.map((c, i) =>
      '<div class="timeline-item" style="--i:' + i + '">' +
        '<div class="timeline-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"></circle></svg></div>' +
        '<div class="timeline-body">' +
          '<div class="timeline-top">' +
            '<span class="timeline-hash">' + esc(c.hash) + '</span>' +
            '<span class="timeline-subject">' + esc(c.subject) + '</span>' +
          '</div>' +
          '<div class="timeline-meta"><span>' + esc(c.author) + '</span><span>' + esc(c.when) + '</span></div>' +
        '</div>' +
      '</div>'
    ).join("");
  } catch (e) {
    commitEl.innerHTML = '<div class="empty"><div class="t">Could not load commits</div></div>';
  }
}

// ─── Branches ─────────────────────────────────────────────────────────
async function loadBranches() {
  const localEl = document.getElementById("localBranches");
  const remoteEl = document.getElementById("remoteBranches");
  try {
    const data = await getJSON("/api/git/branches");
    const local = data.local || [];
    const remote = data.remote || [];
    const current = data.current || "";

    if (!local.length) {
      localEl.innerHTML = '<div class="empty"><div class="t">No local branches</div></div>';
    } else {
      localEl.innerHTML = local.map((b, i) => {
        const on = b.name === current;
        return '<div class="branch-row ' + (on ? "current" : "") + '" data-branch="' + esc(b.name) + '" style="cursor:' + (on ? "default" : "pointer") + '">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg>' +
          '<span class="branch-name">' + esc(b.name) + '</span>' +
          (on ? '<span class="badge accent" style="font-size:10px;padding:2px 8px">current</span>' : '') +
          (b.upstream ? '<span class="branch-meta">→ ' + esc(b.upstream) + '</span>' : '') +
          (b.when ? '<span class="branch-meta">' + esc(b.when) + '</span>' : '') +
          '</div>';
      }).join("");
      localEl.querySelectorAll("[data-branch]").forEach(row => {
        if (row.classList.contains("current")) return;
        row.onclick = () => switchBranch(row.dataset.branch);
      });
    }

    if (!remote.length) {
      remoteEl.innerHTML = '<div class="empty"><div class="t">No remote branches</div></div>';
    } else {
      remoteEl.innerHTML = remote.map(b =>
        '<div class="branch-row">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path></svg>' +
          '<span class="branch-name">' + esc(b.name) + '</span>' +
          (b.when ? '<span class="branch-meta">' + esc(b.when) + '</span>' : '') +
        '</div>'
      ).join("");
    }
  } catch (e) {
    localEl.innerHTML = '<div class="empty"><div class="t">Could not load branches</div></div>';
    remoteEl.innerHTML = '';
  }
}

async function switchBranch(name) {
  const { ok, data } = await postJSON("/api/git/checkout", { branch: name });
  if (ok && data.success) {
    toast(data.message || "Switched to " + name, "ok");
    refreshAll();
  } else {
    toast(data.error || data.message || "Could not switch branch", "err");
  }
}

// ─── Stats ────────────────────────────────────────────────────────────
async function loadStats() {
  const grid = document.getElementById("statsGrid");
  const contrib = document.getElementById("contribList");
  try {
    const data = await getJSON("/api/git/stats");
    const s = data.stats || {};
    const tiles = [
      { k: "Commits", v: (s.commits || 0).toLocaleString(), sub: "on this branch" },
      { k: "Contributors", v: String((s.contributors || []).length), sub: "unique authors" },
      { k: "Branches", v: String(s.branches || 0), sub: "local" },
      { k: "Tracked files", v: (s.tracked_files || 0).toLocaleString(), sub: "in the index" },
      { k: "Repo size", v: s.size || "—", sub: "packed objects" },
    ];
    grid.innerHTML = tiles.map((t, i) =>
      '<div class="stat-tile" style="--i:' + i + '"><span class="k">' + esc(t.k) + '</span>' +
      '<b>' + esc(t.v) + '</b><span class="sub">' + esc(t.sub) + '</span></div>'
    ).join("");

    const list = s.contributors || [];
    if (!list.length) {
      contrib.innerHTML = '<div class="empty"><div class="t">No contributors</div></div>';
    } else {
      contrib.innerHTML = list.map((c, i) =>
        '<div class="contrib">' +
          '<span class="contrib-rank">' + (i + 1) + '</span>' +
          '<span class="contrib-name">' + esc(c.name) + '</span>' +
          '<span class="contrib-count">' + c.commits + '</span>' +
        '</div>'
      ).join("");
    }
  } catch (e) {
    grid.innerHTML = '<div class="empty"><div class="t">Could not load stats</div></div>';
    contrib.innerHTML = '';
  }
}

// ─── Actions ──────────────────────────────────────────────────────────
async function push() {
  if (state.pushing) return;
  state.pushing = true;
  const btn = document.getElementById("pushBtn");
  const label = document.getElementById("pushBtnLabel");
  btn.disabled = true;
  label.textContent = "Pushing…";
  btn.querySelector("svg").classList.add("spin");

  const files = state.selectedFiles.size ? Array.from(state.selectedFiles) : null;
  const { ok, data } = await postJSON("/api/git/push", {
    message: document.getElementById("commitMsg").value || "new update",
    remote: document.getElementById("remoteSelect").value || null,
    files: files,
    force: document.getElementById("forcePush").checked,
  });

  state.pushing = false;
  btn.disabled = false;
  label.textContent = "Push to GitHub";
  btn.querySelector("svg").classList.remove("spin");

  if (ok && data.success) {
    toast(data.message || "Pushed.", "ok");
    state.selectedFiles.clear();
    refreshAll();
  } else if (data.leaks && data.leaks.length) {
    toast("Push blocked — " + data.leaks.length + " secret(s) found.", "err");
    // Jump to a visible summary; the leaks list is rendered below the push card on demand
    const summary = data.leaks.map(l => l.file + ":" + l.line).join(", ");
    toast(summary, "err");
  } else {
    toast(data.error || data.message || "Push failed.", "err");
  }
}

async function pull() {
  const btn = document.getElementById("pullBtn");
  btn.classList.add("active");
  btn.disabled = true;
  const { ok, data } = await postJSON("/api/git/pull", {});
  btn.disabled = false;
  btn.classList.remove("active");
  if (ok && data.success) toast(data.message || "Pulled latest.", "ok");
  else toast(data.error || data.message || "Pull failed.", "err");
  refreshAll();
}

async function stash() {
  const { ok, data } = await postJSON("/api/git/stash", {});
  if (ok && data.success) toast(data.message || "Changes stashed.", "ok");
  else toast(data.error || data.message || "Could not stash.", "err");
  refreshAll();
}

async function stashPop() {
  const { ok, data } = await postJSON("/api/git/stash/pop", {});
  if (ok && data.success) toast(data.message || "Stash restored.", "ok");
  else toast(data.error || data.message || "Could not pop stash.", "err");
  refreshAll();
}

async function discard() {
  if (!confirm("This will PERMANENTLY discard all uncommitted changes. Continue?")) return;
  if (!confirm("Are you really sure? There is no undo.")) return;
  const { ok, data } = await postJSON("/api/git/discard", {});
  if (ok && data.success) toast(data.message || "Changes discarded.", "ok");
  else toast(data.error || data.message || "Could not discard.", "err");
  refreshAll();
}

// ─── Wiring ───────────────────────────────────────────────────────────
document.getElementById("pushBtn").onclick = push;
document.getElementById("pullBtn").onclick = pull;
document.getElementById("stashBtn").onclick = stash;
document.getElementById("stashPopBtn").onclick = stashPop;
document.getElementById("discardBtn").onclick = discard;
document.getElementById("refreshBtn").onclick = () => { refreshAll(); toast("Refreshed.", "info"); };

document.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key === "Enter" && state.tab === "push") {
    e.preventDefault();
    push();
  }
});

// ─── Boot ─────────────────────────────────────────────────────────────
function refreshAll() {
  loadRepoInfo();
  loadFiles();
  loadHistory();
  loadBranches();
  loadStats();
}

refreshAll();
setInterval(() => {
  // Quiet background refresh for the file count only — the rest is on demand.
  if (document.hidden) return;
  getJSON("/api/git/files").then(d => {
    const n = (d.files || []).length;
    document.getElementById("filesCount").textContent = n;
  }).catch(() => {});
}, 20000);
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────

def register(app: flask.Flask) -> None:
    """Attach every /git route to the shared Flask app."""
    ensure_env_ignored()

    @app.get("/git")
    def git_ui():
        return flask.Response(GIT_UI_HTML, mimetype="text/html")

    @app.get("/api/git/status")
    def api_git_status():
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

    @app.get("/api/git/files")
    def api_git_files():
        return jsonify({"files": _changed_files()})

    @app.get("/api/git/diff")
    def api_git_diff():
        path = request.args.get("path", "").strip()
        if not path:
            return jsonify({"path": "", "lines": [], "truncated": False})
        # Guard against path traversal; only allow paths inside the repo.
        try:
            resolved = (PROJECT_ROOT / path).resolve()
            resolved.relative_to(PROJECT_ROOT.resolve())
        except (ValueError, OSError):
            return jsonify({"path": path, "lines": [], "truncated": False}), 400
        return jsonify(_diff_for_file(path))

    @app.get("/api/git/commits")
    def api_git_commits():
        return jsonify({"commits": _recent_commits(10)})

    @app.get("/api/git/history")
    def api_git_history():
        return jsonify({"pushes": _load_history()})

    @app.get("/api/git/branches")
    def api_git_branches():
        return jsonify(_list_branches())

    @app.get("/api/git/stats")
    def api_git_stats():
        return jsonify({"stats": _repo_stats()})

    @app.post("/api/git/push")
    def api_git_push():
        payload = request.get_json(silent=True) or {}
        message = payload.get("message") or DEFAULT_COMMIT_MESSAGE
        remote = payload.get("remote")
        files = payload.get("files") or None
        force = bool(payload.get("force"))

        if not remote:
            remotes = _list_remotes()
            if not remotes:
                return jsonify({"success": False, "error": "No git remote configured."}), 400
            remote = next(iter(remotes))

        if remote not in _list_remotes():
            return jsonify({"success": False, "error": f"Unknown remote '{remote}'."}), 400

        result = perform_git_push(message, remote, files=files, force=force)

        if not result.success and result.leaks:
            return jsonify({
                "success": False,
                "error": "Security scan failed — potential hardcoded secrets.",
                "leaks": [leak.__dict__ for leak in result.leaks],
            }), 400

        status = 200 if result.success else 500
        return jsonify({"success": result.success, "message": result.message}), status

    @app.post("/api/git/pull")
    def api_git_pull():
        if not _is_git_repo():
            return jsonify({"success": False, "error": "Not a git repository."}), 400
        branch = _current_branch()
        res = _run_git("pull", "--ff-only", timeout=60)
        if res.returncode != 0:
            return jsonify({"success": False, "error": res.stderr.strip() or "Pull failed."}), 500
        return jsonify({"success": True, "message": f"Pulled latest on {branch}."})

    @app.post("/api/git/checkout")
    def api_git_checkout():
        payload = request.get_json(silent=True) or {}
        branch = (payload.get("branch") or "").strip()
        if not branch:
            return jsonify({"success": False, "error": "No branch supplied."}), 400
        if _has_changes():
            return jsonify({
                "success": False,
                "error": "Uncommitted changes — commit or stash before switching.",
            }), 400
        res = _run_git("checkout", branch, timeout=30)
        if res.returncode != 0:
            return jsonify({"success": False, "error": res.stderr.strip() or "Checkout failed."}), 500
        return jsonify({"success": True, "message": f"Switched to {branch}."})

    @app.post("/api/git/stash")
    def api_git_stash():
        res = _run_git("stash", "push", "-u", "-m", f"auto-stash {int(time.time())}")
        if res.returncode != 0:
            return jsonify({"success": False, "error": res.stderr.strip() or "Stash failed."}), 500
        return jsonify({"success": True, "message": "Changes stashed."})

    @app.post("/api/git/stash/pop")
    def api_git_stash_pop():
        res = _run_git("stash", "pop")
        if res.returncode != 0:
            return jsonify({"success": False, "error": res.stderr.strip() or "Stash pop failed."}), 500
        return jsonify({"success": True, "message": "Stash restored."})

    @app.post("/api/git/discard")
    def api_git_discard():
        # Reset tracked changes, then clean untracked files. Both are destructive.
        r1 = _run_git("checkout", "--", ".")
        r2 = _run_git("clean", "-fd")
        if r1.returncode != 0 or r2.returncode != 0:
            return jsonify({
                "success": False,
                "error": (r1.stderr or r2.stderr or "Discard failed.").strip(),
            }), 500
        return jsonify({"success": True, "message": "Working tree reset."})

    info("GitHub pusher routes registered (/git)")