"""
Colored console output for the bot process.

Windows terminals only render ANSI escape codes once virtual terminal
processing is enabled, so that is switched on when this module is imported.
Color is dropped automatically when stdout is not a terminal (a redirected
file, a service manager, a pipe) unless FORCE_COLOR=1 is set; NO_COLOR=1 turns
it off everywhere.

Levels
    debug  per-message logging detail (threads, buffers, flushes). Hidden
           unless LOG_DEBUG=1, since it fires several times per message.
    info   normal lifecycle events.
    ok     something that worked.
    warn   recoverable problem; the bot kept going.
    error  something failed.
"""

import datetime
import os
import sys

RESET = "\033[0m"
GREY = "\033[90m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def _enable_ansi() -> bool:
    """Turn on ANSI escape handling for the Windows console."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


_VT_ENABLED = _enable_ansi()

# Terminals that take ANSI codes directly without needing the console-mode
# switch: Git Bash/mintty (TERM), Windows Terminal (WT_SESSION), ConEmu and
# ANSICON. Without one of these, an un-enable-able console gets plain text.
_ANSI_TERMINAL = bool(
    os.getenv("TERM") or os.getenv("WT_SESSION") or os.getenv("ConEmuANSI") or os.getenv("ANSICON")
)


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _colors_enabled() -> bool:
    if _env_flag("NO_COLOR"):
        return False
    if _env_flag("FORCE_COLOR"):
        return True
    if not (_VT_ENABLED or _ANSI_TERMINAL):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def debug_enabled() -> bool:
    return _env_flag("LOG_DEBUG")


def _paint(text: str, color: str) -> str:
    if not _colors_enabled():
        return text
    return f"{color}{text}{RESET}"


def _emit(tag: str, color: str, message: str):
    stamp = _paint(f"[{datetime.datetime.now().strftime('%H:%M:%S')}]", GREY)
    print(f"{stamp} {_paint(tag, color)} {message}", flush=True)


def debug(message: str):
    """Per-message logging detail. No-op unless LOG_DEBUG is set."""
    if debug_enabled():
        _emit("DBUG", GREY, message)


def info(message: str):
    _emit("INFO", CYAN, message)


def ok(message: str):
    _emit(" OK ", GREEN, message)


def warn(message: str):
    _emit("WARN", YELLOW, message)


def error(message: str):
    _emit("FAIL", RED, message)


def plain(message: str = ""):
    """Uncolored line, for banners and blank separators."""
    print(message, flush=True)
