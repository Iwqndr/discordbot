import asyncio
import datetime
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

import discord

from console import debug, error, warn
from config import (
    MESSAGE_CATEGORY_ID,
    SYSTEM_CATEGORY_ID,
    MESSAGE_LOG_PARENT_NAME,
    SYSTEM_LOG_PARENT_NAME,
    VERIFIED_ROLE_ID,
    SUPPORT_LINK,
    MEMBER_SITE_URL,
)


_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_LOG_DIR = _ROOT / "logging"

HISTORY_FILE = str(_LOG_DIR / "moderation_history.json")
LOG_INDEX_FILE = str(_LOG_DIR / "verification_logs.json")
NOTES_FILE = str(_LOG_DIR / "mod_notes.json")
WATCHLIST_FILE = str(_LOG_DIR / "watchlist.json")
LOG_CONFIG_FILE = str(_LOG_DIR / "log_config.json")
THREAD_INDEX_FILE = str(_LOG_DIR / "thread_index.json")
VERIFICATION_CONFIG_FILE = str(_LOG_DIR / "verification_config.json")
TICKET_PANEL_CONFIG_FILE = str(_LOG_DIR / "ticket_panel_config.json")
AUTOMOD_CONFIG_FILE = str(_LOG_DIR / "automod_config.json")
MEDIA_ONLY_FILE = str(_LOG_DIR / "media_only_channels.json")
SELFPROMO_FILE = str(_LOG_DIR / "selfpromo_channels.json")
MOD_CASES_FILE = str(_LOG_DIR / "mod_cases.json")
MESSAGE_LOG_DIR = str(_LOG_DIR / "message_logs")
DELETED_LOG_DIR = str(_LOG_DIR / "deleted_logs")
MESSAGE_LOG_MAX_LINES = 5000
THREAD_ROLLOVER_LIMIT = 1000
BUFFER_FLUSH_INTERVAL = 5.0
BUFFER_MAX_LINES = 30
BUFFER_MAX_CHARS = 1800

os.makedirs(MESSAGE_LOG_DIR, exist_ok=True)
os.makedirs(DELETED_LOG_DIR, exist_ok=True)


class JsonStore:
    def __init__(self, path: str):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        else:
            self.data = {}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception:
            pass


class ModerationHistory:
    def __init__(self, path: str = HISTORY_FILE):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        else:
            self.data = {}

    def save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.data, f)
        except Exception:
            pass

    def _key(self, guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    def record(self, guild_id: int, user_id: int, action: str, moderator_id: int, reason: str,
               case_id: str = None, moderator_tag: str = None):
        """Log one action.

        `case_id` is optional so the paths that never create a case — warns,
        timeouts, older call sites — keep working exactly as before; when it is
        there the dashboard can offer the case behind the entry.
        """
        key = self._key(guild_id, user_id)
        entry = self.data.setdefault(key, {"kicks": 0, "bans": 0, "softbans": 0, "timeouts": 0, "warns": 0, "actions": []})
        if action == "kick":
            entry["kicks"] += 1
        elif action == "ban":
            entry["bans"] += 1
        elif action == "softban":
            entry["softbans"] += 1
        elif action == "timeout":
            entry["timeouts"] += 1
        elif action == "warn":
            entry["warns"] += 1
        record = {
            "action": action,
            "moderator_id": moderator_id,
            "reason": reason,
            "timestamp": datetime.datetime.utcnow().isoformat()
        }
        if case_id:
            record["case_id"] = case_id
        if moderator_tag:
            record["moderator_tag"] = moderator_tag
        entry["actions"].append(record)
        if len(entry["actions"]) > 50:
            entry["actions"] = entry["actions"][-50:]
        self.save()

    def get(self, guild_id: int, user_id: int) -> dict:
        return self.data.get(self._key(guild_id, user_id), {
            "kicks": 0, "bans": 0, "softbans": 0, "timeouts": 0, "warns": 0, "actions": []
        })

    def clear(self, guild_id: int, user_id: int):
        self.data.pop(self._key(guild_id, user_id), None)
        self.save()


class CaseStore(JsonStore):
    """One numbered case per kick, ban, softban or tempban.

    The case id is the handle a punished member quotes back at support, so it
    is short, sequential and permanent: C-1042. `last_number` is kept in the
    same file, so ids never repeat even across restarts.
    """

    PREFIX = "C-"
    MAX_TRIES = 50

    def __init__(self, path: str = MOD_CASES_FILE):
        super().__init__(path)

    def _cases(self) -> dict:
        cases = self.data.get("cases")
        if not isinstance(cases, dict):
            cases = {}
            self.data["cases"] = cases
        return cases

    def mint_id(self) -> str:
        """The next unused case id, skipping any already in the file."""
        cases = self._cases()
        number = int(self.data.get("last_number") or 999)
        for _ in range(self.MAX_TRIES):
            number += 1
            candidate = f"{self.PREFIX}{number}"
            if candidate not in cases:
                self.data["last_number"] = number
                return candidate
        # Only reachable if the file was filled by hand; keep counting anyway.
        self.data["last_number"] = number
        return f"{self.PREFIX}{number}"

    def create(self, guild_id, user_id, action: str, reason: str, moderator_id,
               extra: dict = None) -> dict:
        """Open a case and return the stored record.

        `extra` carries whatever the caller happens to know: user_tag,
        moderator_tag, channel_id, message_id, expires_at, duration, notes.
        """
        extra = extra or {}
        case_id = self.mint_id()
        record = {
            "id": case_id,
            "guild_id": str(guild_id or ""),
            "user_id": str(user_id or ""),
            "user_tag": str(extra.get("user_tag") or ""),
            "action": str(action or ""),
            "reason": str(reason or "")[:900],
            "moderator_id": str(moderator_id or ""),
            "moderator_tag": str(extra.get("moderator_tag") or ""),
            "created_at": datetime.datetime.utcnow().isoformat() + "Z",
            "expires_at": extra.get("expires_at") or None,
            "evidence_url": extra.get("evidence_url") or None,
            "channel_id": str(extra.get("channel_id") or ""),
            "message_id": str(extra.get("message_id") or ""),
            "notes": str(extra.get("notes") or ""),
        }
        self._cases()[case_id] = record
        self.save()
        return record

    def get(self, case_id) -> dict:
        """One case by id, or None. Ids are matched case-insensitively."""
        key = str(case_id or "").strip().upper()
        record = self._cases().get(key)
        return record if isinstance(record, dict) else None

    def for_user(self, guild_id, user_id) -> list:
        """Every case for one member, newest first."""
        gid, uid = str(guild_id or ""), str(user_id or "")
        rows = [r for r in self._cases().values()
                if isinstance(r, dict) and r.get("guild_id") == gid and r.get("user_id") == uid]
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        return rows

    def update(self, case_id, patch: dict) -> dict:
        """Merge a patch into one case. Returns the record, or None if unknown."""
        record = self.get(case_id)
        if record is None:
            return None
        for field, value in (patch or {}).items():
            if field in record and field != "id":
                record[field] = value
        self.save()
        return record


class LogIndex:
    def __init__(self, path: str = LOG_INDEX_FILE):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}
        else:
            self.data = {}

    def save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.data, f)
        except Exception:
            pass

    def set(self, guild_id: int, user_id: int, channel_id: int, message_id: int):
        self.data[f"{guild_id}:{user_id}"] = {"channel_id": channel_id, "message_id": message_id}
        self.save()

    def get(self, guild_id: int, user_id: int):
        return self.data.get(f"{guild_id}:{user_id}")

    def remove(self, guild_id: int, user_id: int):
        self.data.pop(f"{guild_id}:{user_id}", None)
        self.save()


class NotesStore:
    def __init__(self, path: str = NOTES_FILE):
        self.store = JsonStore(path)

    def _key(self, guild_id: int, user_id: int) -> str:
        return f"{guild_id}:{user_id}"

    def add(self, guild_id: int, user_id: int, author_id: int, text: str) -> int:
        key = self._key(guild_id, user_id)
        notes = self.store.data.setdefault(key, [])
        notes.append({
            "author_id": author_id,
            "text": text,
            "timestamp": datetime.datetime.utcnow().isoformat()
        })
        self.store.save()
        return len(notes)

    def list(self, guild_id: int, user_id: int) -> list:
        return self.store.data.get(self._key(guild_id, user_id), [])

    def delete(self, guild_id: int, user_id: int, index: int) -> dict:
        key = self._key(guild_id, user_id)
        notes = self.store.data.get(key, [])
        if index < 1 or index > len(notes):
            return None
        removed = notes.pop(index - 1)
        self.store.data[key] = notes
        self.store.save()
        return removed

    def clear(self, guild_id: int, user_id: int):
        self.store.data.pop(self._key(guild_id, user_id), None)
        self.store.save()


class Watchlist:
    def __init__(self, path: str = WATCHLIST_FILE):
        self.store = JsonStore(path)

    def _key(self, guild_id: int) -> str:
        return str(guild_id)

    def add(self, guild_id: int, user_id: int, author_id: int, reason: str):
        lst = self.store.data.setdefault(self._key(guild_id), {})
        lst[str(user_id)] = {
            "author_id": author_id,
            "reason": reason,
            "timestamp": datetime.datetime.utcnow().isoformat()
        }
        self.store.save()

    def remove(self, guild_id: int, user_id: int) -> bool:
        lst = self.store.data.get(self._key(guild_id), {})
        if str(user_id) in lst:
            del lst[str(user_id)]
            self.store.data[self._key(guild_id)] = lst
            self.store.save()
            return True
        return False

    def get(self, guild_id: int, user_id: int):
        return self.store.data.get(self._key(guild_id), {}).get(str(user_id))

    def list(self, guild_id: int) -> dict:
        return self.store.data.get(self._key(guild_id), {})

    def is_watched(self, guild_id: int, user_id: int) -> bool:
        return str(user_id) in self.store.data.get(self._key(guild_id), {})


class LogConfig:
    def __init__(self, path: str = LOG_CONFIG_FILE):
        self.store = JsonStore(path)

    def get(self) -> dict:
        return self.store.data or {}

    def set_all(self, data: dict):
        self.store.data = data
        self.store.save()

    def update(self, **kwargs):
        data = self.get()
        data.update(kwargs)
        self.set_all(data)

    def is_enabled(self, source_guild_id: int) -> bool:
        sources = self.get().get("sources", [])
        return int(source_guild_id) in [int(s) for s in sources]

    def enable(self, source_guild_id: int):
        data = self.get()
        sources = data.get("sources", [])
        sources = [int(s) for s in sources]
        if int(source_guild_id) not in sources:
            sources.append(int(source_guild_id))
        data["sources"] = sources
        self.set_all(data)

    def disable(self, source_guild_id: int):
        data = self.get()
        sources = [int(s) for s in data.get("sources", [])]
        if int(source_guild_id) in sources:
            sources.remove(int(source_guild_id))
        data["sources"] = sources
        self.set_all(data)

    def sources(self) -> list:
        return [int(s) for s in self.get().get("sources", [])]


class ThreadIndex:
    def __init__(self, path: str = THREAD_INDEX_FILE):
        self.store = JsonStore(path)

    def _key(self, source_guild_id: int, user_id: int) -> str:
        return f"{source_guild_id}:{user_id}"

    def get_user(self, source_guild_id: int, user_id: int) -> dict:
        return self.store.data.get(self._key(source_guild_id, user_id), {"threads": []})

    def set_user(self, source_guild_id: int, user_id: int, data: dict):
        self.store.data[self._key(source_guild_id, user_id)] = data
        self.store.save()

    def newest_thread_id(self, source_guild_id: int, user_id: int):
        threads = self.get_user(source_guild_id, user_id).get("threads", [])
        if not threads:
            return None
        return threads[0]["id"]

    def push_thread(self, source_guild_id: int, user_id: int, thread_id: int, name: str):
        data = self.get_user(source_guild_id, user_id)
        threads = data.get("threads", [])
        threads.insert(0, {"id": thread_id, "name": name, "count": 0})
        data["threads"] = threads
        self.set_user(source_guild_id, user_id, data)

    def increment_newest(self, source_guild_id: int, user_id: int):
        data = self.get_user(source_guild_id, user_id)
        threads = data.get("threads", [])
        if not threads:
            return
        threads[0]["count"] = threads[0].get("count", 0) + 1
        data["threads"] = threads
        self.set_user(source_guild_id, user_id, data)

    def newest_count(self, source_guild_id: int, user_id: int) -> int:
        threads = self.get_user(source_guild_id, user_id).get("threads", [])
        if not threads:
            return 0
        return threads[0].get("count", 0)

    def all_thread_ids(self, source_guild_id: int, user_id: int) -> list:
        return [t["id"] for t in self.get_user(source_guild_id, user_id).get("threads", [])]


history_tracker = ModerationHistory()
log_index = LogIndex()
notes_store = NotesStore()
watchlist = Watchlist()
log_config = LogConfig()
thread_index = ThreadIndex()


# One definition of every AutoMod rule, shared by the bot that enforces it and
# the dashboard that edits it, so the panel can never describe a rule the bot
# does not actually have.
AUTOMOD_RULES = [
    {
        "id": "links", "label": "Link spam", "icon": "link", "group": "Flood control",
        "desc": "Non-media links posted one after another.", "kind": "flood",
        "count": {"label": "Links", "min": 1, "max": 10, "default": 3},
        "window": {"label": "Seconds", "min": 1, "max": 60, "default": 5},
        "action": "timeout", "enabled": True,
    },
    {
        "id": "invites", "label": "Discord invites", "icon": "ticket", "group": "Flood control",
        "desc": "Other servers' invite links, once they stack up.", "kind": "flood",
        "count": {"label": "Invites", "min": 1, "max": 10, "default": 2},
        "window": {"label": "Seconds", "min": 1, "max": 60, "default": 10},
        "action": "timeout", "enabled": True,
    },
    {
        "id": "mentions", "label": "Ping flood", "icon": "at-sign", "group": "Flood control",
        "desc": "User, role and @everyone mentions in a burst.", "kind": "flood",
        "count": {"label": "Mentions", "min": 2, "max": 25, "default": 5},
        "window": {"label": "Seconds", "min": 1, "max": 60, "default": 5},
        "action": "timeout", "enabled": True,
    },
    {
        "id": "repeat", "label": "Repeated messages", "icon": "copy", "group": "Flood control",
        "desc": "The same line pasted over and over.", "kind": "flood",
        "count": {"label": "Repeats", "min": 2, "max": 10, "default": 3},
        "window": {"label": "Seconds", "min": 1, "max": 60, "default": 10},
        "action": "timeout", "enabled": False,
    },
    {
        "id": "webhooks", "label": "Rogue webhooks", "icon": "webhook", "group": "Flood control",
        "desc": "A webhook blasting messages gets found and deleted.", "kind": "flood",
        "count": {"label": "Messages", "min": 2, "max": 20, "default": 3},
        "window": {"label": "Seconds", "min": 1, "max": 60, "default": 3},
        "action": "delete", "enabled": True,
    },
    {
        "id": "words", "label": "Blocked words", "icon": "message-square-x", "group": "Content filters",
        "desc": "Your own list, matched against every message.", "kind": "words",
        "action": "delete", "enabled": False, "list": [], "regex": False, "whole_word": True,
    },
    {
        "id": "caps", "label": "Excessive caps", "icon": "case-upper", "group": "Content filters",
        "desc": "SHOUTING, once the message is long enough to be sure.", "kind": "ratio",
        "count": {"label": "Min length", "min": 6, "max": 60, "default": 12},
        "ratio": {"label": "% caps", "min": 40, "max": 100, "default": 70},
        "action": "delete", "enabled": False,
    },
    {
        "id": "zalgo", "label": "Zalgo text", "icon": "type", "group": "Content filters",
        "desc": "Stacked diacritics that break the chat layout.", "kind": "ratio",
        "ratio": {"label": "% marks", "min": 10, "max": 90, "default": 30},
        "action": "delete", "enabled": False,
    },
    {
        "id": "emoji", "label": "Emoji flood", "icon": "smile-plus", "group": "Content filters",
        "desc": "A message that is mostly emoji.", "kind": "count",
        "count": {"label": "Emoji", "min": 3, "max": 40, "default": 8},
        "action": "delete", "enabled": False,
    },
]

# What the bot does about a trigger, in escalating order. "off" keeps a rule out
# of the way without losing its settings.
AUTOMOD_ACTIONS = [
    {"id": "off", "label": "Ignore", "icon": "volume-off", "desc": "Take no action at all.", "danger": 0},
    {"id": "log", "label": "Log only", "icon": "scroll-text", "desc": "Write it to the system log, change nothing.", "danger": 0},
    {"id": "delete", "label": "Delete", "icon": "trash-2", "desc": "Remove the message and note who sent it.", "danger": 1},
    {"id": "timeout", "label": "Timeout", "icon": "timer", "desc": "Delete it and mute the member for the timeout length.", "danger": 2},
    {"id": "kick", "label": "Kick", "icon": "user-minus", "desc": "Delete it and remove them from the server.", "danger": 3},
    {"id": "ban", "label": "Ban", "icon": "gavel", "desc": "Delete it and ban the account.", "danger": 4},
]

_AUTOMOD_DEFAULTS = {
    "enabled": True,
    "dry_run": False,
    "timeout_seconds": 300,
    "purge_count": 15,
    "dm_member": False,
    "exempt_roles": [],
    "exempt_channels": [],
}

_AUTOMOD_ACTION_IDS = {a["id"] for a in AUTOMOD_ACTIONS}


def _automod_fresh() -> dict:
    cfg = dict(_AUTOMOD_DEFAULTS)
    cfg["rules"] = {}
    for spec in AUTOMOD_RULES:
        rule = {"enabled": bool(spec.get("enabled")), "action": spec.get("action", "delete")}
        for key in ("count", "window", "ratio"):
            block = spec.get(key)
            if block:
                rule[key] = block["default"]
        if spec["kind"] == "words":
            rule["list"] = list(spec.get("list") or [])
            rule["regex"] = bool(spec.get("regex"))
            rule["whole_word"] = bool(spec.get("whole_word", True))
        cfg["rules"][spec["id"]] = rule
    return cfg


def _automod_rule_spec(rule_id: str):
    for spec in AUTOMOD_RULES:
        if spec["id"] == rule_id:
            return spec
    return None


def _automod_clamp(value, block):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return block.get("default", 0)
    return max(int(block.get("min", 0)), min(int(block.get("max", 999)), number))


class AutoModConfig(JsonStore):
    """Per-guild AutoMod settings, edited from the dashboard.

    Rule *definitions* live in AUTOMOD_RULES; this file only stores what a guild
    changed, plus the most recent incidents so the panel has something real to
    show instead of a log it would have to parse.
    """

    INCIDENT_KEEP = 60

    def __init__(self, path: str = AUTOMOD_CONFIG_FILE):
        super().__init__(path)

    def for_guild(self, guild_id, create: bool = True) -> dict:
        """The guild's config, filled out against the current rule catalog.

        Reading never mutates the file: a guild that has never been edited gets
        defaults, and a rule added since the last save appears with its own
        defaults rather than going missing.
        """
        key = str(guild_id)
        stored = self.data.get(key) if isinstance(self.data.get(key), dict) else {}
        cfg = _automod_fresh()
        for field, fallback in _AUTOMOD_DEFAULTS.items():
            cfg[field] = stored.get(field, fallback)
        rules_in = stored.get("rules") if isinstance(stored.get("rules"), dict) else {}
        for rule_id, rule in cfg["rules"].items():
            saved = rules_in.get(rule_id)
            if not isinstance(saved, dict):
                continue
            spec = _automod_rule_spec(rule_id) or {}
            rule["enabled"] = bool(saved.get("enabled", rule["enabled"]))
            action = str(saved.get("action") or rule["action"])
            rule["action"] = action if action in _AUTOMOD_ACTION_IDS else rule["action"]
            for key_name in ("count", "window", "ratio"):
                if key_name in rule and key_name in saved:
                    block = spec.get(key_name) or {"min": 0, "max": 999, "default": rule[key_name]}
                    rule[key_name] = _automod_clamp(saved[key_name], block)
            if spec.get("kind") == "words":
                words = saved.get("list")
                if isinstance(words, list):
                    rule["list"] = [str(w).strip() for w in words if str(w).strip()][:200]
                rule["regex"] = bool(saved.get("regex", rule.get("regex")))
                rule["whole_word"] = bool(saved.get("whole_word", rule.get("whole_word", True)))
        incidents = stored.get("incidents")
        cfg["incidents"] = incidents if isinstance(incidents, list) else []
        cfg["updated_at"] = stored.get("updated_at") or ""
        return cfg

    def update(self, guild_id, patch: dict, save: bool = True) -> dict:
        """Merge a dashboard patch into the stored config and save it.

        `save=False` merges without persisting, so the message tester can answer
        for the settings currently on screen instead of the last saved ones.
        """
        key = str(guild_id)
        current = self.for_guild(guild_id)
        if patch.get("reset"):
            current = _automod_fresh()
            current["incidents"] = []
        if isinstance(patch.get("rules"), dict):
            for rule_id, incoming in patch["rules"].items():
                if rule_id not in current["rules"] or not isinstance(incoming, dict):
                    continue
                rule = current["rules"][rule_id]
                spec = _automod_rule_spec(rule_id) or {}
                if "enabled" in incoming:
                    rule["enabled"] = bool(incoming["enabled"])
                if "action" in incoming and str(incoming["action"]) in _AUTOMOD_ACTION_IDS:
                    rule["action"] = str(incoming["action"])
                for key_name in ("count", "window", "ratio"):
                    if key_name in rule and key_name in incoming:
                        block = spec.get(key_name) or {"min": 0, "max": 999, "default": rule[key_name]}
                        rule[key_name] = _automod_clamp(incoming[key_name], block)
                if isinstance(incoming.get("list"), list):
                    rule["list"] = [str(w).strip() for w in incoming["list"] if str(w).strip()][:200]
                for flag in ("regex", "whole_word"):
                    if flag in incoming:
                        rule[flag] = bool(incoming[flag])
        for field in _AUTOMOD_DEFAULTS:
            if field not in patch:
                continue
            if field in ("exempt_roles", "exempt_channels"):
                values = patch[field] if isinstance(patch[field], list) else []
                current[field] = [str(v) for v in values if str(v)][:100]
            elif field == "timeout_seconds":
                try:
                    current[field] = max(10, min(2419200, int(patch[field] or 300)))
                except (TypeError, ValueError):
                    current[field] = 300
            elif field == "purge_count":
                try:
                    current[field] = max(0, min(100, int(patch[field] or 0)))
                except (TypeError, ValueError):
                    current[field] = 0
            else:
                current[field] = bool(patch[field])
        current["updated_at"] = datetime.datetime.utcnow().isoformat()
        if save:
            self.data[key] = current
            self.save()
            return self.for_guild(guild_id)
        return current

    def record_incident(self, guild_id, entry: dict):
        """Remember one trigger, newest last, capped so the file cannot grow."""
        key = str(guild_id)
        cfg = self.for_guild(guild_id)
        incidents = list(cfg.get("incidents") or [])
        incidents.append(entry)
        stored = self.data.setdefault(key, {})
        stored["incidents"] = incidents[-self.INCIDENT_KEEP:]
        stored.setdefault("rules", cfg.get("rules") or {})
        self.save()

    def incidents(self, guild_id, limit: int = 40) -> list:
        rows = self.for_guild(guild_id).get("incidents") or []
        return list(reversed(rows))[:limit]

    def stats(self, guild_id) -> dict:
        cfg = self.for_guild(guild_id)
        armed = [rid for rid, rule in (cfg.get("rules") or {}).items()
                 if rule.get("enabled") and str(rule.get("action") or "off") != "off"]
        incidents = cfg.get("incidents") or []
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        recent = 0
        for row in incidents:
            try:
                when = datetime.datetime.fromisoformat(str(row.get("at") or ""))
            except ValueError:
                continue
            if when >= cutoff:
                recent += 1
        return {
            "armed": armed,
            "armed_count": len(armed),
            "rule_total": len(cfg.get("rules") or {}),
            "incidents_24h": recent,
            "incidents_total": len(incidents),
            "words": len((cfg.get("rules", {}).get("words") or {}).get("list") or []),
            "last_incident": incidents[-1] if incidents else None,
        }


automod_config = AutoModConfig()
verification_config = JsonStore(VERIFICATION_CONFIG_FILE)
ticket_panel_config = JsonStore(TICKET_PANEL_CONFIG_FILE)
case_store = CaseStore()


# ---------------------------------------------------------------------------
# Channel flags: "media only" and "self-promo"
# ---------------------------------------------------------------------------

# A direct media file, matched against the path only. MEDIA_EXCLUSIONS is
# hostname-oriented and would miss https://example.com/a.png?x=1, so this check
# gets its own pattern and the query string is dropped before it runs.
MEDIA_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|mp4|webm|mov)$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Media-only warning DM ratelimit. In memory only: this is a nuisance limiter,
# not a control worth persisting.
MEDIA_WARN_WINDOW = 60.0
MEDIA_WARN_THRESHOLD = 3
MEDIA_WARN_MUTE = 600.0
_media_warn_hits: dict[int, list[float]] = defaultdict(list)
_media_warn_muted_until: dict[int, float] = {}


class MediaOnlyStore(JsonStore):
    """Channels where only media may be posted: `{guild_id: [channel_id, ...]}`.

    `has()` runs on every message, so the file is read once at import and then
    kept in memory; only a write goes back to disk.
    """

    def __init__(self, path: str = MEDIA_ONLY_FILE):
        super().__init__(path)

    def ids(self, guild_id) -> list:
        raw = self.data.get(str(guild_id))
        if not isinstance(raw, list):
            return []
        return [str(v) for v in raw if str(v)]

    def has(self, guild_id, channel_id) -> bool:
        return str(channel_id) in set(self.ids(guild_id))

    def set_flag(self, guild_id, channel_id, on: bool) -> list:
        """Flag or unflag one channel. Returns the guild's remaining list."""
        key = str(guild_id)
        current = self.ids(guild_id)
        target = str(channel_id)
        if on and target not in current:
            current.append(target)
        elif not on:
            current = [c for c in current if c != target]
        if current:
            self.data[key] = current
        else:
            self.data.pop(key, None)
        self.save()
        return current


class SelfPromoStore(JsonStore):
    """Channels where allow-listed members may post links.

    Shape: `{guild_id: {channel_id: {"roles": [...], "users": [...]}}}`. The
    same read-once/keep-in-memory rule applies.
    """

    def __init__(self, path: str = SELFPROMO_FILE):
        super().__init__(path)

    def _guild(self, guild_id, create: bool = False) -> dict:
        key = str(guild_id)
        current = self.data.get(key)
        if not isinstance(current, dict):
            if not create:
                return {}
            current = {}
            self.data[key] = current
        return current

    def entry(self, guild_id, channel_id) -> dict:
        """The stored allowlist for one channel, or an empty one."""
        entry = self._guild(guild_id).get(str(channel_id))
        if not isinstance(entry, dict):
            return {"roles": [], "users": []}
        return {
            "roles": [str(r) for r in (entry.get("roles") or []) if str(r)],
            "users": [str(u) for u in (entry.get("users") or []) if str(u)],
        }

    def has(self, guild_id, channel_id) -> bool:
        return str(channel_id) in self._guild(guild_id)

    def set_flag(self, guild_id, channel_id, on: bool):
        """Flag the channel (with empty lists) or drop it entirely."""
        guild = self._guild(guild_id, create=True)
        key = str(channel_id)
        if on:
            guild.setdefault(key, {"roles": [], "users": []})
        else:
            guild.pop(key, None)
        if not guild:
            self.data.pop(str(guild_id), None)
        self.save()

    def set_list(self, guild_id, channel_id, field: str, values: list):
        """Replace one of the two allowlists on a flagged channel."""
        if field not in ("roles", "users"):
            raise ValueError("Unknown allowlist field.")
        guild = self._guild(guild_id, create=True)
        key = str(channel_id)
        entry = guild.get(key)
        if not isinstance(entry, dict):
            entry = {"roles": [], "users": []}
            guild[key] = entry
        entry[field] = [str(v) for v in (values or []) if str(v)][:100]
        self.save()
        return self.entry(guild_id, channel_id)

    def listing(self, guild_id) -> dict:
        return {cid: self.entry(guild_id, cid) for cid in self._guild(guild_id)}


media_only_store = MediaOnlyStore()
selfpromo_store = SelfPromoStore()


def is_media_only(guild_id, channel_id) -> bool:
    """Whether one channel is flagged media-only. Cheap: never touches disk."""
    if channel_id is None or guild_id is None:
        return False
    return media_only_store.has(guild_id, channel_id)


def is_selfpromo_channel(guild_id, channel_id) -> bool:
    """Whether one channel is flagged for self-promo. Never touches disk."""
    if channel_id is None or guild_id is None:
        return False
    return selfpromo_store.has(guild_id, channel_id)


def member_is_selfpromo_allowed(guild_id, channel_id, member) -> bool:
    """Whether this member may post links in a self-promo channel.

    Three ways in: an allow-listed role, an explicit user id, or the
    `manage_messages` permission, so staff are never caught out by it.
    """
    if not is_selfpromo_channel(guild_id, channel_id):
        return False
    entry = selfpromo_store.entry(guild_id, channel_id)
    uid = str(getattr(member, "id", "") or "")
    if uid and uid in entry["users"]:
        return True
    allowed_roles = set(entry["roles"])
    if allowed_roles:
        for role in (getattr(member, "roles", None) or []):
            if str(getattr(role, "id", "")) in allowed_roles:
                return True
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and perms.manage_messages:
        return True
    return False


def is_media_message(message) -> bool:
    """Whether a message counts as media for a media-only channel.

    An attachment always counts. A link counts when its path ends in a direct
    media extension — the query string is dropped first, so
    `https://example.com/a.png?x=1` still passes.
    """
    if getattr(message, "attachments", None):
        return True
    content = (getattr(message, "content", "") or "").strip()
    if not content:
        return False
    for url in _URL_RE.findall(content):
        if MEDIA_EXT_RE.search(url.split("?", 1)[0]):
            return True
    return False


_pending_lines: dict[tuple[int, int], list[str]] = defaultdict(list)
_flush_tasks: dict[tuple[int, int], asyncio.Task] = {}


def _msg_log_path(guild_id: int, user_id: int) -> str:
    return os.path.join(MESSAGE_LOG_DIR, f"{guild_id}_{user_id}.txt")


def _del_log_path(guild_id: int, user_id: int) -> str:
    return os.path.join(DELETED_LOG_DIR, f"{guild_id}_{user_id}.txt")


def _fallback_append(path: str, line: str):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        warn(f"[FALLBACK] Failed to write {path}: {e}")


def find_logging_guild(bot) -> discord.Guild:
    candidates = []
    for guild in bot.guilds:
        msg_cat = discord.utils.get(guild.categories, id=MESSAGE_CATEGORY_ID)
        sys_cat = discord.utils.get(guild.categories, id=SYSTEM_CATEGORY_ID)
        if msg_cat and sys_cat:
            candidates.append(guild)
    debug(f"[FINDLOG] Guilds with both categories: {[g.id for g in candidates]}")
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    for g in candidates:
        ch = discord.utils.get(g.text_channels, name=MESSAGE_LOG_PARENT_NAME)
        if ch:
            return g
    return candidates[0]


async def ensure_logging_channels(guild: discord.Guild):
    debug(f"[SETUP] ensure_logging_channels for guild {guild.id}")
    msg_cat = discord.utils.get(guild.categories, id=MESSAGE_CATEGORY_ID)
    sys_cat = discord.utils.get(guild.categories, id=SYSTEM_CATEGORY_ID)
    debug(f"[SETUP] msg_cat={msg_cat} sys_cat={sys_cat}")
    if not msg_cat or not sys_cat:
        return None, None

    msg_parent = discord.utils.get(guild.text_channels, name=MESSAGE_LOG_PARENT_NAME)
    debug(f"[SETUP] existing msg_parent={msg_parent}")
    if not msg_parent:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True,
                    manage_threads=True,
                    create_public_threads=True,
                    send_messages_in_threads=True,
                    embed_links=True,
                    read_message_history=True,
                ),
            }
            msg_parent = await guild.create_text_channel(
                MESSAGE_LOG_PARENT_NAME,
                category=msg_cat,
                overwrites=overwrites,
                reason="Log setup"
            )
            debug(f"[SETUP] created msg_parent={msg_parent.id}")
        except Exception as e:
            warn(f"[SETUP] Failed to create message parent channel: {type(e).__name__}: {e}")
            return None, None

    sys_parent = discord.utils.get(guild.text_channels, name=SYSTEM_LOG_PARENT_NAME)
    debug(f"[SETUP] existing sys_parent={sys_parent}")
    if not sys_parent:
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(
                    read_messages=True,
                    send_messages=True,
                    embed_links=True,
                    read_message_history=True,
                ),
            }
            sys_parent = await guild.create_text_channel(
                SYSTEM_LOG_PARENT_NAME,
                category=sys_cat,
                overwrites=overwrites,
                reason="Log setup"
            )
            debug(f"[SETUP] created sys_parent={sys_parent.id}")
        except Exception as e:
            warn(f"[SETUP] Failed to create system parent channel: {type(e).__name__}: {e}")
            return None, None

    log_config.update(
        logging_guild_id=guild.id,
        message_parent_channel_id=msg_parent.id,
        system_parent_channel_id=sys_parent.id
    )
    debug(f"[SETUP] config updated: {log_config.get()}")
    return msg_parent, sys_parent


async def get_or_create_thread(bot, source_guild_id: int, user: discord.User) -> discord.Thread | None:
    logging_guild = find_logging_guild(bot)
    debug(f"[THREAD] Logging guild resolved: {logging_guild.id if logging_guild else None}")
    if not logging_guild:
        return None

    parent = None
    cfg = log_config.get()
    debug(f"[THREAD] Config: {cfg}")
    if cfg.get("message_parent_channel_id"):
        parent = logging_guild.get_channel(cfg["message_parent_channel_id"])
        if parent is None:
            try:
                parent = await bot.fetch_channel(cfg["message_parent_channel_id"])
            except Exception as e:
                warn(f"[THREAD] fetch parent failed: {e}")
                parent = None
    if not parent:
        parent, _ = await ensure_logging_channels(logging_guild)
    debug(f"[THREAD] Parent channel: {parent}")
    if not parent:
        return None

    perms = parent.permissions_for(logging_guild.me)
    debug(f"[THREAD] Bot perms - send={perms.send_messages}, create_threads={perms.create_public_threads}, send_in_threads={perms.send_messages_in_threads}, manage_threads={perms.manage_threads}")
    if not perms.send_messages or not perms.create_public_threads:
        warn("[THREAD] Missing permission to create threads in parent")
        return None

    newest_id = thread_index.newest_thread_id(source_guild_id, user.id)
    newest_count = thread_index.newest_count(source_guild_id, user.id)
    debug(f"[THREAD] Existing newest thread: {newest_id} (count={newest_count})")

    if newest_id:
        thread = logging_guild.get_thread(newest_id)
        if thread is None:
            try:
                thread = await bot.fetch_channel(newest_id)
            except Exception as e:
                warn(f"[THREAD] Could not fetch existing thread: {e}")
                thread = None
        if thread and newest_count < THREAD_ROLLOVER_LIMIT:
            if thread.archived:
                try:
                    await thread.edit(archived=False)
                except Exception as e:
                    warn(f"[THREAD] Failed to unarchive: {e}")
            return thread

    rollover = len(thread_index.all_thread_ids(source_guild_id, user.id)) + 1
    name = f"{user.name[:20]}-{user.id}-p{rollover}"
    debug(f"[THREAD] Creating new thread: {name}")
    try:
        thread = await parent.create_thread(name=name)
        debug(f"[THREAD] Created thread: {thread.id}")
    except Exception as e:
        warn(f"[THREAD] Failed to create thread: {type(e).__name__}: {e}")
        return None

    thread_index.push_thread(source_guild_id, user.id, thread.id, name)
    return thread


async def flush_user_buffer(bot, source_guild_id: int, user_id: int):
    key = (source_guild_id, user_id)
    lines = _pending_lines.pop(key, [])
    _flush_tasks.pop(key, None)
    debug(f"[FLUSH] Flushing {len(lines)} line(s) for {user_id} in {source_guild_id}")
    if not lines:
        return

    user = None
    for g in bot.guilds:
        if g.id == source_guild_id:
            user = g.get_member(user_id)
            if user:
                break
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
            debug(f"[FLUSH] Fetched user {user_id} via API")
        except Exception as e:
            warn(f"[FLUSH] Could not resolve user {user_id}: {e}")
            user = None
    if user is None:
        for line in lines:
            _fallback_append(_msg_log_path(source_guild_id, user_id), line + "\n")
        debug(f"[FLUSH] No user object, fell back to txt")
        return

    thread = await get_or_create_thread(bot, source_guild_id, user)
    debug(f"[FLUSH] Thread resolved: {thread}")
    if thread is None:
        for line in lines:
            _fallback_append(_msg_log_path(source_guild_id, user_id), line + "\n")
        debug(f"[FLUSH] No thread, fell back to txt")
        return

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 1900:
            try:
                await thread.send(chunk)
                thread_index.increment_newest(source_guild_id, user_id)
                debug(f"[FLUSH] Sent chunk to thread {thread.id}")
            except Exception as e:
                warn(f"[FLUSH] Send to thread failed: {type(e).__name__}: {e}")
                for l in chunk.split("\n"):
                    if l:
                        _fallback_append(_msg_log_path(source_guild_id, user_id), l + "\n")
            chunk = ""
        chunk += line + "\n"

    if chunk.strip():
        try:
            await thread.send(chunk.strip())
            thread_index.increment_newest(source_guild_id, user_id)
            debug(f"[FLUSH] Sent final chunk to thread {thread.id}")
        except Exception as e:
            warn(f"[FLUSH] Send to thread failed: {type(e).__name__}: {e}")
            for l in chunk.split("\n"):
                if l:
                    _fallback_append(_msg_log_path(source_guild_id, user_id), l + "\n")


def schedule_flush(bot, source_guild_id: int, user_id: int):
    key = (source_guild_id, user_id)
    existing = _flush_tasks.get(key)
    if existing and not existing.done():
        return

    async def _runner():
        try:
            await asyncio.sleep(BUFFER_FLUSH_INTERVAL)
            await flush_user_buffer(bot, source_guild_id, user_id)
        except Exception as e:
            error(f"[FLUSH] Runner error: {type(e).__name__}: {e}")
        finally:
            _flush_tasks.pop(key, None)

    _flush_tasks[key] = asyncio.create_task(_runner())


def queue_log_line(bot, source_guild_id: int, user_id: int, line: str):
    key = (source_guild_id, user_id)
    _pending_lines[key].append(line)
    total_chars = sum(len(l) for l in _pending_lines[key])
    debug(f"[QUEUE] Buffered for {user_id} in {source_guild_id} (buffer={len(_pending_lines[key])}, chars={total_chars})")

    if len(_pending_lines[key]) >= BUFFER_MAX_LINES or total_chars >= BUFFER_MAX_CHARS:
        asyncio.create_task(flush_user_buffer(bot, source_guild_id, user_id))
        return

    schedule_flush(bot, source_guild_id, user_id)


async def flush_all_buffers(bot):
    keys = list(_pending_lines.keys())
    debug(f"[FLUSHALL] Flushing {len(keys)} buffers")
    for key in keys:
        try:
            await flush_user_buffer(bot, key[0], key[1])
        except Exception as e:
            error(f"[FLUSHALL] error: {type(e).__name__}: {e}")


def format_line(timestamp: datetime.datetime, channel_name: str, message_id: int, content: str, tag: str = None) -> str:
    ts = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    safe = (content or "[no text]").replace("\n", "\\n").replace("\r", "")
    prefix = f"[{tag}] " if tag else ""
    return f"[{ts}] [#{channel_name}] (id={message_id}) {prefix}{safe}"


def log_message(bot, source_guild_id: int, user_id: int, channel_name: str, content: str, message_id: int):
    line = format_line(datetime.datetime.utcnow(), channel_name, message_id, content)
    debug(f"[LOG] log_message guild={source_guild_id} user={user_id}")
    queue_log_line(bot, source_guild_id, user_id, line)


def log_deleted_message(bot, source_guild_id: int, user_id: int, channel_name: str, content: str, message_id: int):
    line = format_line(datetime.datetime.utcnow(), channel_name, message_id, content, tag="DELETED")
    debug(f"[LOG] log_deleted guild={source_guild_id} user={user_id}")
    queue_log_line(bot, source_guild_id, user_id, line)


def log_edited_message(bot, source_guild_id: int, user_id: int, channel_name: str, original: str, edited: str, message_id: int):
    safe_original = (original or "[unknown]").replace("\n", "\\n")
    safe_edited = (edited or "[empty]").replace("\n", "\\n")
    content = f"{safe_original} ⇒ {safe_edited}"
    line = format_line(datetime.datetime.utcnow(), channel_name, message_id, content, tag="EDITED")
    debug(f"[LOG] log_edited guild={source_guild_id} user={user_id}")
    queue_log_line(bot, source_guild_id, user_id, line)


async def find_original_line(bot, source_guild_id: int, user_id: int, message_id: int) -> str | None:
    thread_ids = thread_index.all_thread_ids(source_guild_id, user_id)
    if not thread_ids:
        return None

    logging_guild = find_logging_guild(bot)
    if not logging_guild:
        return None

    needle = f"(id={message_id})"
    for tid in thread_ids:
        thread = logging_guild.get_thread(tid)
        if thread is None:
            try:
                thread = await bot.fetch_channel(tid)
            except Exception:
                continue
        if thread is None:
            continue

        try:
            async for msg in thread.history(limit=200, oldest_first=False):
                for raw_line in msg.content.split("\n"):
                    if needle in raw_line and "[EDITED]" not in raw_line and "[DELETED]" not in raw_line:
                        idx = raw_line.find(needle)
                        if idx == -1:
                            continue
                        after = raw_line[idx + len(needle):]
                        if after.startswith(") "):
                            after = after[2:]
                        elif after.startswith(")"):
                            after = after[1:]
                        return after.strip()
        except Exception:
            continue
    return None


async def fetch_user_thread_lines(bot, source_guild_id: int, user_id: int, max_lines: int = 1000) -> list[str]:
    thread_ids = thread_index.all_thread_ids(source_guild_id, user_id)
    debug(f"[FETCH] thread_ids={thread_ids}")
    if not thread_ids:
        return []

    logging_guild = find_logging_guild(bot)
    if not logging_guild:
        return []

    collected: list[str] = []
    for tid in thread_ids:
        thread = logging_guild.get_thread(tid)
        if thread is None:
            try:
                thread = await bot.fetch_channel(tid)
            except Exception as e:
                warn(f"[FETCH] fetch thread {tid} failed: {e}")
                continue
        if thread is None:
            continue
        try:
            async for msg in thread.history(limit=500, oldest_first=False):
                for line in reversed(msg.content.split("\n")):
                    if line.strip():
                        collected.append(line)
                        if len(collected) >= max_lines:
                            return collected
        except Exception as e:
            warn(f"[FETCH] history failed for {tid}: {e}")
            continue
    return collected


def find_log_channel(guild: discord.Guild):
    channel = discord.utils.get(guild.text_channels, name="security-logs")
    if channel:
        return channel
    return discord.utils.find(
        lambda c: c.name.lower().replace("_", "-") == "security-logs",
        guild.text_channels
    )


async def resolve_invite_used(member: discord.Member):
    try:
        if not member.guild.me.guild_permissions.manage_guild:
            return None

        invites_before = await member.guild.invites()
        await asyncio.sleep(1.5)
        invites_after = await member.guild.invites()

        before_map = {inv.code: inv for inv in invites_before}
        for inv in invites_after:
            prev = before_map.get(inv.code)
            if prev is None:
                continue
            if inv.uses > prev.uses:
                return inv
    except Exception:
        return None
    return None


def account_risk_score(member: discord.Member) -> tuple[int, list[str]]:
    if getattr(member, 'bot', False):
        return (0, [])
    score = 0
    reasons = []

    age_days = (discord.utils.utcnow() - member.created_at).days

    if age_days < 1:
        score += 50
        reasons.append("Account is less than 1 day old")
    elif age_days < 7:
        score += 30
        reasons.append("Account is less than 7 days old")
    elif age_days < 30:
        score += 15
        reasons.append("Account is less than 30 days old")

    if member.avatar is None:
        score += 20
        reasons.append("No custom avatar (default avatar)")

    if not member.display_name or member.display_name == member.name:
        score += 5
        reasons.append("No custom display name set")

    if re.search(r"\d{4,}", member.name):
        score += 10
        reasons.append("Username contains a long digit sequence")

    if member.bot:
        score += 25
        reasons.append("Account is a bot")

    return score, reasons


def risk_label(score: int) -> tuple[str, discord.Color]:
    if score >= 60:
        return "High Risk", discord.Color.from_rgb(237, 66, 69)
    if score >= 30:
        return "Medium Risk", discord.Color.from_rgb(250, 166, 26)
    if score >= 10:
        return "Low Risk", discord.Color.from_rgb(254, 231, 92)
    return "Clean", discord.Color.from_rgb(87, 242, 135)


async def gather_moderation_history(guild: discord.Guild, member: discord.Member) -> dict:
    result = {
        "tracked_kicks": 0,
        "tracked_bans": 0,
        "tracked_softbans": 0,
        "tracked_timeouts": 0,
        "tracked_warns": 0,
        "actual_ban": False,
        "ban_reason": None,
    }

    tracked = history_tracker.get(guild.id, member.id)
    result["tracked_kicks"] = tracked.get("kicks", 0)
    result["tracked_bans"] = tracked.get("bans", 0)
    result["tracked_softbans"] = tracked.get("softbans", 0)
    result["tracked_timeouts"] = tracked.get("timeouts", 0)
    result["tracked_warns"] = tracked.get("warns", 0)

    if guild.me.guild_permissions.ban_members:
        try:
            async for entry in guild.bans(limit=None):
                if entry.user.id == member.id:
                    result["actual_ban"] = True
                    result["ban_reason"] = entry.reason or "No reason recorded"
                    break
        except Exception:
            pass

    return result


def compute_risk(member: discord.Member, history: dict) -> tuple[int, str, discord.Color, list[str]]:
    risk_score, risk_reasons = account_risk_score(member)

    if history["actual_ban"]:
        risk_score += 60
        risk_reasons.append("Banned user returned to server")

    total_history = history["tracked_bans"] + history["tracked_kicks"] + history["tracked_softbans"]
    if total_history >= 5:
        risk_score += 40
        risk_reasons.append(f"{total_history} severe prior moderation actions")
    elif total_history >= 3:
        risk_score += 25
        risk_reasons.append(f"{total_history} severe prior moderation actions")
    elif total_history >= 1:
        risk_score += 10
        risk_reasons.append(f"{total_history} severe prior moderation actions")

    if history["tracked_timeouts"] >= 5:
        risk_score += 15
        risk_reasons.append(f"{history['tracked_timeouts']} prior timeouts")
    elif history["tracked_timeouts"] >= 2:
        risk_score += 8
        risk_reasons.append(f"{history['tracked_timeouts']} prior timeouts")

    if history["tracked_warns"] >= 5:
        risk_score += 10
        risk_reasons.append(f"{history['tracked_warns']} prior warnings")
    elif history["tracked_warns"] >= 2:
        risk_score += 5
        risk_reasons.append(f"{history['tracked_warns']} prior warnings")

    risk_score = min(risk_score, 100)
    risk_text, risk_color = risk_label(risk_score)
    return risk_score, risk_text, risk_color, risk_reasons


def history_flags_from(history: dict) -> list[str]:
    flags = []
    if history["actual_ban"]:
        flags.append("Currently/recently banned from this server")
    if history["tracked_bans"] > 0:
        flags.append(f"{history['tracked_bans']} prior ban(s) issued by bot")
    if history["tracked_kicks"] > 0:
        flags.append(f"{history['tracked_kicks']} prior kick(s) issued by bot")
    if history["tracked_softbans"] > 0:
        flags.append(f"{history['tracked_softbans']} prior softban(s)")
    if history["tracked_timeouts"] > 0:
        flags.append(f"{history['tracked_timeouts']} prior timeout(s)")
    if history["tracked_warns"] > 0:
        flags.append(f"{history['tracked_warns']} prior warning(s)")
    return flags


def build_verification_embed(member: discord.Member, invite, risk_score: int, risk_text: str, risk_color: discord.Color,
                              risk_reasons: list, history: dict, history_flags: list, guild: discord.Guild) -> discord.Embed:
    account_age = (discord.utils.utcnow() - member.created_at).days
    joined_at = member.joined_at.strftime("%b %d, %Y at %H:%M UTC") if member.joined_at else "Unknown"
    created_at = member.created_at.strftime("%b %d, %Y at %H:%M UTC")

    member_info = (
        f"**User:** {member.mention}\n"
        f"**Username:** `{member.name}`\n"
        f"**Display Name:** `{member.display_name}`\n"
        f"**User ID:** `{member.id}`\n"
        f"**Bot Account:** {'Yes' if member.bot else 'No'}\n"
        f"**Account Age:** `{account_age} days`"
    )

    timeline_info = (
        f"**Account Created:** `{created_at}`\n"
        f"**Joined Server:** `{joined_at}`\n"
        f"**Boosting Since:** `{member.premium_since.strftime('%b %d, %Y') if member.premium_since else 'Not Boosting'}`"
    )

    if invite:
        inviter = invite.inviter
        inviter_str = f"{inviter.mention} (`{inviter.id}`)" if inviter else "Unknown"
        inviter_age = "Unknown"
        inviter_joined = "Unknown"
        if inviter:
            inviter_age = f"{(discord.utils.utcnow() - inviter.created_at).days} days"
            if isinstance(inviter, discord.Member) and inviter.joined_at:
                inviter_joined = inviter.joined_at.strftime("%b %d, %Y")
        invite_info = (
            f"**Invite Code:** `{invite.code}`\n"
            f"**Invite Uses:** `{invite.uses}`\n"
            f"**Invite Channel:** {invite.channel.mention if invite.channel else 'Unknown'}\n"
            f"**Inviter:** {inviter_str}\n"
            f"**Inviter Account Age:** `{inviter_age}`\n"
            f"**Inviter Joined Server:** `{inviter_joined}`\n"
            f"**Invite URL:** https://discord.gg/{invite.code}"
        )
    else:
        invite_info = "Could not be resolved (invite tracking unavailable or permission missing)"

    if history_flags:
        history_summary = "Yes\n" + "\n".join(f"- {f}" for f in history_flags)
    else:
        history_summary = "No prior moderation history found"

    if history["ban_reason"]:
        history_info = (
            f"**Previous Bans / Kicks:** {history_summary}\n"
            f"**Currently Banned:** {'Yes' if history['actual_ban'] else 'No'}\n"
            f"**Ban Reason:** `{history['ban_reason']}`\n"
            f"**Tracked Bans:** `{history['tracked_bans']}`\n"
            f"**Tracked Kicks:** `{history['tracked_kicks']}`\n"
            f"**Tracked Softbans:** `{history['tracked_softbans']}`\n"
            f"**Tracked Timeouts:** `{history['tracked_timeouts']}`\n"
            f"**Tracked Warnings:** `{history['tracked_warns']}`"
        )
    else:
        history_info = (
            f"**Previous Bans / Kicks:** {history_summary}\n"
            f"**Currently Banned:** {'Yes' if history['actual_ban'] else 'No'}\n"
            f"**Ban Reason:** N/A\n"
            f"**Tracked Bans:** `{history['tracked_bans']}`\n"
            f"**Tracked Kicks:** `{history['tracked_kicks']}`\n"
            f"**Tracked Softbans:** `{history['tracked_softbans']}`\n"
            f"**Tracked Timeouts:** `{history['tracked_timeouts']}`\n"
            f"**Tracked Warnings:** `{history['tracked_warns']}`"
        )

    risk_info = (
        f"**Score:** `{risk_score}/100`\n"
        f"**Level:** `{risk_text}`\n"
        f"**Factors:**\n" + ("\n".join(f"- {r}" for r in risk_reasons) if risk_reasons else "None detected")
    )

    embed = discord.Embed(
        title="Member Verified",
        color=risk_color,
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(name="Member Info", value=member_info[:1024], inline=False)
    embed.add_field(name="Timeline", value=timeline_info[:1024], inline=False)
    embed.add_field(name="Invite Information", value=invite_info[:1024], inline=False)
    embed.add_field(name="Moderation History", value=history_info[:1024], inline=False)
    embed.add_field(name="Risk Assessment", value=risk_info[:1024], inline=False)

    embed.set_footer(text="Anti-Raid Verification System", icon_url=guild.me.display_avatar.url)
    return embed


async def post_system_log(bot, title: str, description: str, fields: list = None, color: discord.Color = discord.Color.red(), thumbnail_url: str = None):
    logging_guild = find_logging_guild(bot)
    if not logging_guild:
        warn("[SYSLOG] No logging guild found")
        return

    cfg = log_config.get()
    channel_id = cfg.get("system_parent_channel_id")
    channel = logging_guild.get_channel(channel_id) if channel_id else None
    if not channel:
        _, channel = await ensure_logging_channels(logging_guild)
    if not channel:
        warn("[SYSLOG] No system channel")
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow()
    )
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="System Log")

    try:
        await channel.send(embed=embed)
        debug(f"[SYSLOG] Sent to {channel.id}")
    except Exception as e:
        error(f"[SYSLOG] Failed: {type(e).__name__}: {e}")


async def refresh_verification_log(bot, guild: discord.Guild, user_id: int):
    entry = log_index.get(guild.id, user_id)
    if not entry:
        return False

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return False

    channel = guild.get_channel(entry["channel_id"])
    if not channel:
        return False

    try:
        message = await channel.fetch_message(entry["message_id"])
    except Exception:
        log_index.remove(guild.id, user_id)
        return False

    try:
        invite = await resolve_invite_used(member)
    except Exception:
        invite = None

    history = await gather_moderation_history(guild, member)
    risk_score, risk_text, risk_color, risk_reasons = compute_risk(member, history)
    history_flags = history_flags_from(history)

    new_embed = build_verification_embed(
        member=member,
        invite=invite,
        risk_score=risk_score,
        risk_text=risk_text,
        risk_color=risk_color,
        risk_reasons=risk_reasons,
        history=history,
        history_flags=history_flags,
        guild=guild
    )

    try:
        await message.edit(embed=new_embed)
        return True
    except Exception as e:
        warn(f"[VERIFY] Failed to edit log: {type(e).__name__}: {e}")
        return False


async def safe_refresh_verification_log(bot, guild: discord.Guild, user_id: int):
    try:
        await refresh_verification_log(bot, guild, user_id)
    except Exception as e:
        error(f"[VERIFY] safe_refresh failed: {type(e).__name__}: {e}")


# ----------------------------------------------------------
# VERIFICATION PANEL CONFIG
#
# Persisted to verification_config.json so both the panel's look and the role
# IDs it grants survive a restart. The view is registered as a persistent view
# on startup, which is what keeps the Verify button working after `python
# main.py` is run again without redeploying the panel.
# ----------------------------------------------------------

DEFAULT_VERIFICATION_CONFIG = {
    "role_ids": [str(VERIFIED_ROLE_ID)],
    "title": "Server Verification",
    "description": (
        "**Welcome.**\n\n"
        "This server is protected by an automated anti-raid system. "
        "To gain access to the rest of the server, you must complete a quick verification.\n\n"
        "**How to verify**\n"
        "> Click the **Verify** button below.\n"
        "> You will instantly be granted the Verified role.\n\n"
        "**Why verify?**\n"
        "> Protects the community from raids and automated bots\n"
        "> Keeps channels clean and free of spam\n"
        "> Takes less than a second\n\n"
        "**Privacy**\n"
        "> Your account information is only used for verification logging "
        "and is visible to server staff only.\n\n"
        "*If the button does not work, please contact a staff member.*"
    ),
    "color": "#5865F2",
    "button_label": "Verify",
    "button_emoji": "",
    "button_style": "success",
    "author_name": "",
    "author_icon_url": "",
    "thumbnail_url": "",
    "image_url": "",
    "footer_text": "Anti-Raid Security System",
    "footer_icon_url": "",
    "timestamp": False,
    "fields": [],
    "panel_channel_id": None,
    "panel_message_id": None,
}


def verification_role_ids(raw=None) -> list:
    """Parse role IDs from a comma/space separated string, or a list of them."""
    if raw is None:
        return []
    parts = [str(item) for item in raw] if isinstance(raw, (list, tuple, set)) else [str(raw)]
    out = []
    for part in parts:
        for chunk in re.split(r"[,\s]+", part):
            chunk = chunk.strip()
            if chunk:
                out.append(chunk)
    return out


def load_verification_config() -> dict:
    cfg = dict(DEFAULT_VERIFICATION_CONFIG)
    cfg["role_ids"] = list(DEFAULT_VERIFICATION_CONFIG["role_ids"])
    stored = verification_config.data if isinstance(verification_config.data, dict) else {}
    for key, value in stored.items():
        if key == "role_ids":
            parsed = verification_role_ids(value)
            if parsed:
                cfg["role_ids"] = parsed
        elif key in cfg:
            cfg[key] = value
    return cfg


def save_verification_config(patch: dict) -> dict:
    cfg = load_verification_config()
    for key, value in (patch or {}).items():
        if key not in cfg:
            continue
        if key == "role_ids":
            parsed = verification_role_ids(value)
            if parsed:
                cfg["role_ids"] = parsed
        else:
            cfg[key] = value
    verification_config.data = cfg
    verification_config.save()
    return cfg


def verified_role_ids_int() -> list:
    """The configured verified role IDs as ints, skipping anything unusable."""
    out = []
    for rid in verification_role_ids(load_verification_config().get("role_ids")):
        try:
            out.append(int(rid))
        except (TypeError, ValueError):
            continue
    return out


def member_is_verified(member) -> bool:
    """True when a member already holds one of the configured verified roles."""
    roles = getattr(member, "roles", None)
    if not roles:
        return False
    configured = set(verified_role_ids_int())
    return any(getattr(role, "id", None) in configured for role in roles)


_VERIFICATION_BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "secondary": discord.ButtonStyle.secondary,
}


def _verification_emoji(raw):
    """Turn a configured emoji string into something Button(emoji=...) accepts.

    Custom emoji come through as ``<:name:id>``; anything else has to be a single
    unicode emoji, so stray text (which Discord would reject) is dropped.
    """
    emoji = str(raw or "").strip()
    if not emoji:
        return None
    try:
        if re.match(r"^<a?:\w+:\d+>$", emoji):
            return discord.PartialEmoji.from_str(emoji)
        if not any(ch.isspace() for ch in emoji) and len(emoji) <= 8:
            return emoji
    except Exception:
        pass
    return None


def _verification_color(value) -> discord.Color:
    try:
        text = str(value or "").strip().lstrip("#")
        if text:
            return discord.Color(int(text, 16))
    except Exception:
        pass
    return discord.Color.from_rgb(88, 101, 242)


def _clip(text, limit: int) -> str:
    return ("" if text is None else str(text))[:limit]


class VerificationView(discord.ui.View):
    """Persistent verify panel.

    Settings are read from verification_config.json at interaction time, so a
    dashboard edit applies to the already-posted panel, and re-registering this
    view on startup keeps the button alive across restarts.
    """

    def __init__(self, bot_avatar: str = None):
        super().__init__(timeout=None)
        self.bot_avatar = bot_avatar
        cfg = load_verification_config()
        label = (cfg.get("button_label") or "Verify")
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "raid_verification_button":
                child.label = _clip(label, 80) or "Verify"
                child.emoji = _verification_emoji(cfg.get("button_emoji"))
                child.style = _VERIFICATION_BUTTON_STYLES.get(
                    str(cfg.get("button_style") or "success").lower(),
                    discord.ButtonStyle.success,
                )

    def build_panel_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = load_verification_config()
        embed = discord.Embed(
            title=_clip(cfg.get("title") or "Server Verification", 256),
            description=_clip(cfg.get("description") or "", 4096),
            color=_verification_color(cfg.get("color")),
        )

        author_name = (cfg.get("author_name") or "").strip()
        if author_name:
            embed.set_author(
                name=_clip(author_name, 256),
                icon_url=(cfg.get("author_icon_url") or "").strip() or None,
            )
        elif guild and guild.icon:
            embed.set_author(name=guild.name, icon_url=guild.icon.url)

        thumbnail = (cfg.get("thumbnail_url") or "").strip()
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        elif guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        image = (cfg.get("image_url") or "").strip()
        if image:
            embed.set_image(url=image)

        for field in (cfg.get("fields") or []):
            if len(embed.fields) >= 25:
                break
            if not isinstance(field, dict):
                continue
            name = _clip(field.get("name") or "", 256)
            value = _clip(field.get("value") or "", 1024)
            if not name or not value:
                continue
            embed.add_field(name=name, value=value, inline=bool(field.get("inline")))

        if cfg.get("timestamp"):
            embed.timestamp = discord.utils.utcnow()

        footer_text = (cfg.get("footer_text") or "").strip()
        footer_icon = (cfg.get("footer_icon_url") or "").strip() or self.bot_avatar
        if footer_text:
            embed.set_footer(text=_clip(footer_text, 2048), icon_url=footer_icon or None)
        elif self.bot_avatar:
            embed.set_footer(text="Anti-Raid Security System", icon_url=self.bot_avatar)
        return embed

    @discord.ui.button(
        label="Verify",
        style=discord.ButtonStyle.success,
        custom_id="raid_verification_button"
    )
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        if not guild or not isinstance(member, discord.Member):
            return await interaction.response.send_message(
                "Verification must be completed in a server.",
                ephemeral=True
            )

        roles = []
        for rid in verification_role_ids(load_verification_config().get("role_ids")):
            try:
                role = guild.get_role(int(rid))
            except (TypeError, ValueError):
                role = None
            if role and role not in roles:
                roles.append(role)

        if not roles:
            return await interaction.response.send_message(
                "Verification unavailable. The configured verified role is missing. Please inform an admin.",
                ephemeral=True
            )

        missing = [role for role in roles if role not in member.roles]
        if not missing:
            return await interaction.response.send_message(
                "You are already verified. You already have access to the server.",
                ephemeral=True
            )

        try:
            await member.add_roles(*missing, reason="Passed anti-raid verification.")
            await interaction.response.send_message(
                f"Verification complete. Welcome to {guild.name}, {member.mention}. You now have full access.",
                ephemeral=True
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Verification failed. The bot lacks permission to assign the verified role. Please contact an admin.",
                ephemeral=True
            )

        try:
            invite = await resolve_invite_used(member)
        except Exception as e:
            warn(f"[VERIFY] Invite resolution failed: {e}")
            invite = None

        history = await gather_moderation_history(guild, member)
        risk_score, risk_text, risk_color, risk_reasons = compute_risk(member, history)
        history_flags = history_flags_from(history)

        embed = build_verification_embed(
            member=member,
            invite=invite,
            risk_score=risk_score,
            risk_text=risk_text,
            risk_color=risk_color,
            risk_reasons=risk_reasons,
            history=history,
            history_flags=history_flags,
            guild=guild
        )

        await post_system_log(
            interaction.client,
            title="Member Verified",
            description=f"{member.mention} has passed verification.",
            fields=[
                ("User", f"{member.mention} (`{member.id}`)", True),
                ("Risk Score", f"`{risk_score}/100`", True),
                ("Risk Level", f"`{risk_text}`", True),
            ],
            color=risk_color,
            thumbnail_url=member.display_avatar.url
        )

        log_channel = find_log_channel(guild)
        if log_channel:
            perms = log_channel.permissions_for(guild.me)
            if perms.send_messages and perms.embed_links:
                try:
                    sent = await log_channel.send(embed=embed)
                    log_index.set(guild.id, member.id, log_channel.id, sent.id)
                except Exception as e:
                    warn(f"[VERIFY] Failed to send log embed: {type(e).__name__}: {e}")


async def deploy_verification_panel(channel: discord.TextChannel, bot_user: discord.User = None, record: bool = True):
    """Post the verify panel and remember where it was posted.

    Recording the channel/message lets the bot re-render the same panel after a
    restart, so `>verification` only has to be run once.
    Deletes any previously stored panel message in this channel to prevent duplicates.
    """
    cfg = load_verification_config()
    old_msg_id = cfg.get("panel_message_id")
    old_chan_id = cfg.get("panel_channel_id")
    if old_msg_id and (not old_chan_id or str(old_chan_id) == str(channel.id)):
        try:
            old_msg = await channel.fetch_message(int(old_msg_id))
            await old_msg.delete()
        except Exception:
            pass

    view = VerificationView(bot_avatar=bot_user.display_avatar.url if bot_user else None)
    embed = view.build_panel_embed(channel.guild)
    message = await channel.send(embed=embed, view=view)
    if record:
        save_verification_config({
            "panel_channel_id": str(channel.id),
            "panel_message_id": str(message.id),
        })
    return message


def register_verification_view(bot) -> bool:
    """Re-attach the persistent Verify button so it keeps working after a restart."""
    try:
        avatar = bot.user.display_avatar.url if bot.user else None
        bot.add_view(VerificationView(bot_avatar=avatar))
        return True
    except Exception as e:
        warn(f"[VERIFY] Could not register persistent view: {type(e).__name__}: {e}")
        return False


async def refresh_verification_panel(bot) -> bool:
    """Re-render the stored panel message with whatever the config now says."""
    cfg = load_verification_config()
    channel_id = cfg.get("panel_channel_id")
    message_id = cfg.get("panel_message_id")
    if not channel_id or not message_id:
        return False
    try:
        channel = bot.get_channel(int(channel_id))
    except (TypeError, ValueError):
        return False
    if channel is None:
        return False
    try:
        message = await channel.fetch_message(int(message_id))
    except Exception:
        return False

    view = VerificationView(bot_avatar=bot.user.display_avatar.url if bot.user else None)
    try:
        await message.edit(embed=view.build_panel_embed(channel.guild), view=view)
        return True
    except Exception as e:
        warn(f"[VERIFY] Could not refresh panel: {type(e).__name__}: {e}")
        return False


# ----------------------------------------------------------
# TICKET PANEL — the "Create Ticket" button
#
# A second editable panel that mirrors the verification panel in every respect:
# its own config file, its own persistent view, its own deploy and refresh
# functions, and its own editor in the dashboard's owner panel. The existing
# verification code above is untouched.
#
# The one behavioural difference: this button does not create anything. It
# replies ephemerally and sends the member to the support form on the member
# site, so tickets keep landing in Supabase through the web flow that already
# works.
# ----------------------------------------------------------

# The custom_id the member bot's persistent view listens for. It must not
# collide with the verification button's id, or Discord would route a click to
# whichever view registered first.
TICKET_BUTTON_CUSTOM_ID = "ticket_creation_button"

# Same keys as DEFAULT_VERIFICATION_CONFIG so the dashboard editor can reuse the
# same controls. `role_ids` is carried for shape compatibility only — a ticket
# panel grants nothing, so it stays empty and is never required.
DEFAULT_TICKET_PANEL_CONFIG = {
    "role_ids": [],
    "title": "Need help?",
    "description": (
        "**Support tickets**\n\n"
        "Open a ticket and a member of staff will pick it up. You can attach "
        "screenshots and check back on the reply at any time.\n\n"
        "**How to open one**\n"
        "> Click the **Create Ticket** button below.\n"
        "> You will be sent to our support page with the form already open.\n\n"
        "**Before you ask**\n"
        "> Read the FAQ first — most questions are answered there.\n"
        "> Appeals need your case ID, which is in the DM we sent you.\n\n"
        "*If the button does not work, ask a staff member for a link.*"
    ),
    "color": "#5865F2",
    "button_label": "Create Ticket",
    "button_emoji": "",
    "button_style": "success",
    "author_name": "",
    "author_icon_url": "",
    "thumbnail_url": "",
    "image_url": "",
    "footer_text": "Support",
    "footer_icon_url": "",
    "timestamp": False,
    "fields": [],
    "panel_channel_id": None,
    "panel_message_id": None,
}


def load_ticket_panel_config() -> dict:
    """Read the ticket panel config, layered over its defaults."""
    cfg = dict(DEFAULT_TICKET_PANEL_CONFIG)
    cfg["role_ids"] = list(DEFAULT_TICKET_PANEL_CONFIG["role_ids"])
    cfg["fields"] = list(DEFAULT_TICKET_PANEL_CONFIG["fields"])
    stored = ticket_panel_config.data if isinstance(ticket_panel_config.data, dict) else {}
    for key, value in stored.items():
        if key == "role_ids":
            cfg["role_ids"] = verification_role_ids(value)
        elif key in cfg:
            cfg[key] = value
    return cfg


def save_ticket_panel_config(patch: dict) -> dict:
    """Merge a patch into the ticket panel config and persist it."""
    cfg = load_ticket_panel_config()
    for key, value in (patch or {}).items():
        if key not in cfg:
            continue
        if key == "role_ids":
            cfg["role_ids"] = verification_role_ids(value)
        else:
            cfg[key] = value
    ticket_panel_config.data = cfg
    ticket_panel_config.save()
    return cfg


def ticket_support_url() -> str:
    """The exact URL the panel button sends a member to.

    The member page watches for `?open=support` on load and opens the support
    modal by itself, so the bot never has to know how the site is built.
    """
    if not MEMBER_SITE_URL:
        return ""
    return f"{MEMBER_SITE_URL}?open=support"


class TicketPanelView(discord.ui.View):
    """The persistent Create Ticket panel.

    Settings are re-read from ticket_panel_config.json at interaction time, so a
    dashboard edit reaches the panel that is already posted, and re-registering
    this view on startup keeps the button alive across restarts.
    """

    def __init__(self, bot_avatar: str = None):
        super().__init__(timeout=None)
        self.bot_avatar = bot_avatar
        cfg = load_ticket_panel_config()
        label = (cfg.get("button_label") or "Create Ticket")
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == TICKET_BUTTON_CUSTOM_ID:
                child.label = _clip(label, 80) or "Create Ticket"
                child.emoji = _verification_emoji(cfg.get("button_emoji"))
                child.style = _VERIFICATION_BUTTON_STYLES.get(
                    str(cfg.get("button_style") or "success").lower(),
                    discord.ButtonStyle.success,
                )

    def build_panel_embed(self, guild: discord.Guild) -> discord.Embed:
        """The posted embed, rebuilt from config every time it is rendered."""
        cfg = load_ticket_panel_config()
        embed = discord.Embed(
            title=_clip(cfg.get("title") or "Need help?", 256),
            description=_clip(cfg.get("description") or "", 4096),
            color=_verification_color(cfg.get("color")),
        )

        author_name = (cfg.get("author_name") or "").strip()
        if author_name:
            embed.set_author(
                name=_clip(author_name, 256),
                icon_url=(cfg.get("author_icon_url") or "").strip() or None,
            )
        elif guild and guild.icon:
            embed.set_author(name=guild.name, icon_url=guild.icon.url)

        thumbnail = (cfg.get("thumbnail_url") or "").strip()
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        elif guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        image = (cfg.get("image_url") or "").strip()
        if image:
            embed.set_image(url=image)

        for field in (cfg.get("fields") or []):
            if len(embed.fields) >= 25:
                break
            if not isinstance(field, dict):
                continue
            name = _clip(field.get("name") or "", 256)
            value = _clip(field.get("value") or "", 1024)
            if not name or not value:
                continue
            embed.add_field(name=name, value=value, inline=bool(field.get("inline")))

        if cfg.get("timestamp"):
            embed.timestamp = discord.utils.utcnow()

        footer_text = (cfg.get("footer_text") or "").strip()
        footer_icon = (cfg.get("footer_icon_url") or "").strip() or self.bot_avatar
        if footer_text:
            embed.set_footer(text=_clip(footer_text, 2048), icon_url=footer_icon or None)
        elif self.bot_avatar:
            embed.set_footer(text="Support", icon_url=self.bot_avatar)
        return embed

    @discord.ui.button(
        label="Create Ticket",
        style=discord.ButtonStyle.success,
        custom_id=TICKET_BUTTON_CUSTOM_ID
    )
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Send the member to the support form. Never creates a ticket itself."""
        url = ticket_support_url()
        if url:
            message = f"Head to our [support page]({url}) to open a Ticket."
        else:
            # No MEMBER_SITE_URL configured: keep the sentence readable rather
            # than emitting a link with nothing in it.
            message = "Head to our support page to open a Ticket."
        try:
            await interaction.response.send_message(message, ephemeral=True, suppress_embeds=True)
        except Exception as e:
            warn(f"[TICKET] Could not reply to the panel button: {type(e).__name__}: {e}")


async def deploy_ticket_panel(channel: discord.TextChannel, bot_user: discord.User = None, record: bool = True):
    """Post the Create Ticket panel and remember where it went.

    Mirrors deploy_verification_panel: any previously stored panel message in
    this channel is deleted first, so redeploying never leaves two panels.
    """
    cfg = load_ticket_panel_config()
    old_msg_id = cfg.get("panel_message_id")
    old_chan_id = cfg.get("panel_channel_id")
    if old_msg_id and (not old_chan_id or str(old_chan_id) == str(channel.id)):
        try:
            old_msg = await channel.fetch_message(int(old_msg_id))
            await old_msg.delete()
        except Exception:
            pass

    view = TicketPanelView(bot_avatar=bot_user.display_avatar.url if bot_user else None)
    embed = view.build_panel_embed(channel.guild)
    message = await channel.send(embed=embed, view=view)
    if record:
        save_ticket_panel_config({
            "panel_channel_id": str(channel.id),
            "panel_message_id": str(message.id),
        })
    return message


def register_ticket_panel_view(bot) -> bool:
    """Re-attach the persistent Create Ticket button after a restart."""
    try:
        avatar = bot.user.display_avatar.url if bot.user else None
        bot.add_view(TicketPanelView(bot_avatar=avatar))
        return True
    except Exception as e:
        warn(f"[TICKET] Could not register persistent view: {type(e).__name__}: {e}")
        return False


async def refresh_ticket_panel(bot) -> bool:
    """Re-render the stored ticket panel with whatever the config now says."""
    cfg = load_ticket_panel_config()
    channel_id = cfg.get("panel_channel_id")
    message_id = cfg.get("panel_message_id")
    if not channel_id or not message_id:
        return False
    try:
        channel = bot.get_channel(int(channel_id))
    except (TypeError, ValueError):
        return False
    if channel is None:
        return False
    try:
        message = await channel.fetch_message(int(message_id))
    except Exception:
        return False

    view = TicketPanelView(bot_avatar=bot.user.display_avatar.url if bot.user else None)
    try:
        await message.edit(embed=view.build_panel_embed(channel.guild), view=view)
        return True
    except Exception as e:
        warn(f"[TICKET] Could not refresh panel: {type(e).__name__}: {e}")
        return False


class PaginatedLogView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], author_id: int, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author_id = author_id
        self.index = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.index == 0
        self.next_button.disabled = self.index >= len(self.pages) - 1
        self.page_button.label = f"Page {self.index + 1}/{len(self.pages)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This menu belongs to someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.primary, disabled=True)
    async def page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < len(self.pages) - 1:
            self.index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


def build_message_history_pages(target: discord.User, lines: list[str], title_prefix: str, color: discord.Color, per_page: int = 10) -> list[discord.Embed]:
    if not lines:
        embed = discord.Embed(
            title=f"{title_prefix}: {target}",
            description="No messages on file for this user.",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"User ID: {target.id}")
        return [embed]

    reversed_lines = list(reversed(lines))
    total = len(reversed_lines)
    total_pages = (total + per_page - 1) // per_page
    pages = []
    for i in range(total_pages):
        chunk = reversed_lines[i * per_page:(i + 1) * per_page]
        body = "\n".join(chunk)
        if len(body) > 4000:
            body = body[:4000] + "\n... (truncated)"
        embed = discord.Embed(
            title=f"{title_prefix}: {target}",
            description=body,
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text=f"User ID: {target.id}  |  Page {i + 1}/{total_pages}  |  Total: {total}")
        pages.append(embed)
    return pages


# Every punishment the bot hands out, with the words and colour its DM uses.
# Keys are the action strings the call sites already pass.
PUNISHMENT_KINDS = {
    "kick": {"label": "Kick", "title": "You have been kicked from {server}", "color": "orange"},
    "ban": {"label": "Ban", "title": "You have been banned from {server}", "color": "red"},
    "hardban": {"label": "Ban", "title": "You have been banned from {server}", "color": "red"},
    "softban": {"label": "Softban", "title": "You have been softbanned from {server}", "color": "red"},
    "tempban": {"label": "Tempban", "title": "You have been temporarily banned from {server}", "color": "red"},
}


def punishment_kind(action) -> str:
    """Normalise any action string to one of the keys in PUNISHMENT_KINDS."""
    key = str(action or "").strip().lower()
    if key in PUNISHMENT_KINDS:
        return key
    if "soft" in key and "ban" in key:
        return "softban"
    if "temp" in key and "ban" in key:
        return "tempban"
    if "kick" in key:
        return "kick"
    return "ban"


def punishment_label(action) -> str:
    """The human label for an action, e.g. "Tempban"."""
    return PUNISHMENT_KINDS[punishment_kind(action)]["label"]


def penalties_for(action) -> bool:
    """Whether this action is one the case system covers."""
    return str(action or "").strip().lower() in PUNISHMENT_KINDS


def _resolve_member(guild, thing):
    """Best-effort member object from a member, a user, or a bare id."""
    if isinstance(thing, (discord.Member, discord.User)):
        return thing
    if guild is None or thing is None:
        return thing
    raw = getattr(thing, "id", thing)
    if str(raw).isdigit() and hasattr(guild, "get_member"):
        try:
            return guild.get_member(int(raw))
        except Exception:
            return thing
    return thing


def _tag_of(thing) -> str:
    """The name to print for a member, user, or id."""
    if thing is None:
        return ""
    for attr in ("name", "display_name"):
        value = getattr(thing, attr, None)
        if value:
            return str(value)
    return str(getattr(thing, "id", thing))


def record_case(guild, target, action: str, reason: str, moderator=None, extra: dict = None) -> dict:
    """Open a case for one punishment, filling in the names it can resolve.

    Takes bare ids as happily as member objects, so the dashboard (which often
    has only an id) and the commands (which always have members) can share it.
    """
    extra = dict(extra or {})
    gid = getattr(guild, "id", guild)
    target_id = getattr(target, "id", target)
    moderator_id = getattr(moderator, "id", moderator)
    if not extra.get("user_tag"):
        extra["user_tag"] = _tag_of(_resolve_member(guild, target))
    if not extra.get("moderator_tag"):
        extra["moderator_tag"] = _tag_of(_resolve_member(guild, moderator))
    return case_store.create(gid, target_id, action, reason, moderator_id, extra)


async def send_punishment_dm(target, guild, action: str, reason: str, case_id: str = None,
                              moderator=None, extra: dict = None) -> bool:
    """DM a member the embed explaining why they were punished.

    Best-effort by design: a closed DM box must never stop a moderation
    action, so every failure is swallowed and only logged. Callers send this
    *before* the punishment lands, because a ban removes the shared guild the
    DM would otherwise need.

    The case id is the point of the whole embed — it is what the member quotes
    when they appeal — so it is the first field and it is repeated in the
    footer, where it survives a thumbnail-only screenshot.

    Returns True when the message was accepted by Discord.
    """
    if target is None or guild is None:
        return False

    kind = punishment_kind(action)
    spec = PUNISHMENT_KINDS[kind]
    extra = extra or {}
    color = discord.Color.orange() if spec["color"] == "orange" else discord.Color.red()

    if SUPPORT_LINK:
        appeal = f"If you believe this was a mistake, please [appeal here]({SUPPORT_LINK})."
    else:
        appeal = "If you believe this was a mistake, please contact support."

    embed = discord.Embed(
        title=spec["title"].format(server=guild.name),
        description=f"**Reason:** {reason or 'No reason provided'}\n\n{appeal}",
        color=color,
    )
    # Case id first: everything else is context, this is the handle they need.
    embed.add_field(name="Case ID", value=f"`{case_id or 'not recorded'}`", inline=False)
    embed.add_field(name="Punishment", value=spec["label"], inline=True)
    moderator_tag = extra.get("moderator_tag") or _tag_of(_resolve_member(guild, moderator))
    embed.add_field(name="Issued by", value=moderator_tag or "Server staff", inline=True)
    if kind == "tempban":
        if extra.get("duration"):
            embed.add_field(name="Length", value=str(extra["duration"]), inline=True)
        if extra.get("expires_at"):
            embed.add_field(name="Expires", value=str(extra["expires_at"]), inline=True)

    icon_url = guild.icon.url if guild.icon else None
    embed.set_footer(text=f"{guild.name} · Case {case_id}" if case_id else guild.name, icon_url=icon_url)
    if icon_url:
        embed.set_thumbnail(url=icon_url)

    try:
        await target.send(embed=embed)
        return True
    except discord.Forbidden:
        warn(f"Could not DM {getattr(target, 'id', target)} about a {spec['label']}: DMs are closed.")
    except Exception as exc:
        warn(f"Could not DM {getattr(target, 'id', target)} about a {spec['label']}: {type(exc).__name__}")
    return False


class AntiRaidSystem:
    def __init__(self, bot):
        self.bot = bot

        self.webhook_tracker = defaultdict(list)
        self.link_tracker = defaultdict(list)
        self.mention_tracker = defaultdict(list)
        self.invites_tracker = defaultdict(list)
        self.repeat_tracker = defaultdict(list)

        # Kept as names so anything that referenced them still resolves; the
        # live numbers now come from each guild's saved config.
        self.WEBHOOK_MAX_MSG = 3
        self.WEBHOOK_WINDOW = 3.0
        self.LINK_MAX_COUNT = 3
        self.LINK_WINDOW = 5.0
        self.MENTION_MAX_COUNT = 5
        self.MENTION_WINDOW = 5.0
        self.INVITE_MAX_COUNT = 2
        self.INVITE_WINDOW = 10.0
        self.TIMEOUT_DURATION = 300

        self.URL_REGEX = re.compile(r'https?://[^\s]+', re.IGNORECASE)
        self.DISCORD_INVITE_REGEX = re.compile(r'(discord(?:app)?\.gg|discord\.com/invite)/[a-zA-Z0-9]+', re.IGNORECASE)
        self.EMOJI_REGEX = re.compile(
            '['
            '\U0001F300-\U0001FAFF'
            '\U00002600-\U000027BF'
            '\U0001F000-\U0001F2FF'
            ']|<a?:[a-zA-Z0-9_]+:[0-9]+>'
        )
        self.ZALGO_REGEX = re.compile('[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]')

        self.MEDIA_EXCLUSIONS = re.compile(
            r'(tenor\.com|giphy\.com|gfycat\.com|imgur\.com|'
            r'media\.discordapp\.net|cdn\.discordapp\.com|'
            r'\.(gif|png|jpg|jpeg|webp|mp4|webm|mov)(\?.*)?$)',
            re.IGNORECASE
        )

    # ----------------------------------------------------------------
    # rule helpers
    # ----------------------------------------------------------------

    def _cfg(self, guild_id) -> dict:
        return automod_config.for_guild(guild_id)

    def _rule(self, cfg: dict, rule_id: str) -> dict:
        return (cfg.get("rules") or {}).get(rule_id) or {}

    @staticmethod
    def _uses(rule: dict) -> bool:
        """A rule counts as on only when it is enabled and has an action."""
        return bool(rule.get("enabled")) and str(rule.get("action") or "off") != "off"

    def _exempt(self, message: discord.Message, cfg: dict) -> bool:
        """Administrators, and anything the guild listed, are never touched."""
        author = message.author
        try:
            if author.guild_permissions.administrator:
                return True
        except Exception:
            pass
        ids = {str(r.id) for r in getattr(author, "roles", []) or []}
        if ids & {str(r) for r in (cfg.get("exempt_roles") or [])}:
            return True
        return str(getattr(message.channel, "id", "")) in {str(c) for c in (cfg.get("exempt_channels") or [])}

    def _trip(self, key, count: int, window: float) -> bool:
        """Sliding window counter: True once `count` hits land inside `window`."""
        now = asyncio.get_event_loop().time()
        stamps = self._bucket(key)
        stamps[:] = [t for t in stamps if now - t < window]
        stamps.append(now)
        if len(stamps) >= count:
            stamps.clear()
            return True
        return False

    def _bucket(self, key):
        name = key[0]
        store = getattr(self, name, None)
        if store is None:
            store = defaultdict(list)
            setattr(self, name, store)
        pair = (key[1], key[2])
        return store[pair]

    def _words_hit(self, content: str, rule: dict):
        """The first blocked word in the message, or None."""
        terms = [str(w) for w in (rule.get("list") or []) if str(w).strip()]
        if not terms:
            return None
        if rule.get("regex"):
            for term in terms:
                try:
                    if re.search(term, content, re.IGNORECASE):
                        return term
                except re.error:
                    continue
            return None
        haystack = content.lower()
        for term in terms:
            needle = term.lower()
            if rule.get("whole_word", True):
                if re.search(r'(?<![\w])' + re.escape(needle) + r'(?![\w])', haystack):
                    return term
            elif needle in haystack:
                return term
        return None

    @staticmethod
    def _caps_hit(content: str, rule: dict):
        letters = [c for c in content if c.isalpha()]
        minimum = int(rule.get("count") or 12)
        if len(letters) < minimum:
            return None
        upper = sum(1 for c in letters if c.isupper())
        ratio = upper / len(letters) * 100
        if ratio >= int(rule.get("ratio") or 70):
            return f"{upper}/{len(letters)} letters capitalised"
        return None

    def _zalgo_hit(self, content: str, rule: dict):
        if len(content) < 4:
            return None
        marks = len(self.ZALGO_REGEX.findall(content))
        if marks and marks / len(content) * 100 >= int(rule.get("ratio") or 30):
            return f"{marks} stacked diacritics"
        return None

    def _emoji_hit(self, content: str, rule: dict):
        found = len(self.EMOJI_REGEX.findall(content))
        if found >= int(rule.get("count") or 8):
            return f"{found} emoji in one message"
        return None

    def contains_harmful_link(self, content: str) -> bool:
        urls = self.URL_REGEX.findall(content)
        if not urls:
            return False

        for url in urls:
            if self.MEDIA_EXCLUSIONS.search(url):
                continue
            return True

        return False

    @staticmethod
    def _action_label(action: str, cfg: dict) -> str:
        seconds = int(cfg.get("timeout_seconds") or 300)
        if action == "timeout":
            if seconds % 3600 == 0:
                return f"{seconds // 3600}h timeout"
            if seconds % 60 == 0:
                return f"{seconds // 60}m timeout"
            return f"{seconds}s timeout"
        return {
            "log": "Logged only", "delete": "Message deleted",
            "kick": "Kicked", "ban": "Banned", "off": "Ignored",
        }.get(action, action)

    async def _apply_action(self, message: discord.Message, action: str, cfg: dict, trigger: str,
                            dm: bool = None) -> tuple[bool, str]:
        """Carry out one configured action. Returns (worked, what happened)."""
        user = message.author
        guild = message.guild
        if action in ("off", "log"):
            return True, self._action_label(action, cfg)

        try:
            await message.delete()
        except Exception:
            pass

        if action == "delete":
            return True, self._action_label(action, cfg)

        # The rich explanation goes out before the punishment, while the bot and
        # the member still share a guild. `dm_member` is the extra gate here;
        # every other caller path always attempts it.
        should_dm = cfg.get("dm_member") if dm is None else dm
        if should_dm and action in ("kick", "ban"):
            await send_punishment_dm(user, guild, action, f"AutoMod: {trigger}")

        try:
            if action == "timeout":
                until = discord.utils.utcnow() + datetime.timedelta(seconds=int(cfg.get("timeout_seconds") or 300))
                await user.timeout(until, reason=f"AutoMod: {trigger}")
                history_tracker.record(guild.id, user.id, "timeout", self.bot.user.id, f"AutoMod: {trigger}")
            elif action == "kick":
                await user.kick(reason=f"AutoMod: {trigger}")
                history_tracker.record(guild.id, user.id, "kick", self.bot.user.id, f"AutoMod: {trigger}")
            elif action == "ban":
                await guild.ban(user, reason=f"AutoMod: {trigger}", delete_message_days=1)
                history_tracker.record(guild.id, user.id, "ban", self.bot.user.id, f"AutoMod: {trigger}")
            else:
                return False, f"Unknown action '{action}'"
        except Exception as exc:
            # The message is already gone, so this is a partial success and the
            # log says so instead of pretending it worked.
            return False, f"{self._action_label(action, cfg)} failed ({type(exc).__name__})"

        try:
            await safe_refresh_verification_log(self.bot, guild, user.id)
        except Exception:
            pass
        return True, self._action_label(action, cfg)

    async def execute_punishment(self, message: discord.Message, trigger_type: str, trigger_info: str,
                                 purge_check=None, rule_id: str = "", action: str = "timeout", dm: bool = None):
        cfg = self._cfg(message.guild.id)
        user = message.author
        guild = message.guild
        channel = message.channel

        # A dry run is a real rehearsal: it reports exactly what it would have
        # done and changes nothing.
        if cfg.get("dry_run"):
            automod_config.record_incident(guild.id, {
                "at": datetime.datetime.utcnow().isoformat(), "rule": rule_id or trigger_type,
                "label": trigger_type, "user_id": str(user.id), "user": str(user),
                "channel": getattr(channel, "name", "unknown"), "trigger": trigger_info,
                "action": cfg.get("rules", {}).get(rule_id, {}).get("action", action),
                "result": "dry run — nothing happened", "dry_run": True,
            })
            await post_system_log(
                self.bot,
                title=f"AutoMod (dry run): {trigger_type}",
                description=f"Would have acted on a message in #{getattr(channel, 'name', 'unknown')}.",
                fields=[("Offender", f"{user.mention} (`{user.id}`)", True),
                        ("Would do", self._action_label(action, cfg), True),
                        ("Trigger", trigger_info, False)],
                color=discord.Color.from_rgb(120, 140, 190),
                thumbnail_url=user.display_avatar.url if user.display_avatar else None,
            )
            return False

        worked, outcome = await self._apply_action(message, action, cfg, trigger_type, dm=dm)

        purge_count = int(cfg.get("purge_count") or 0)
        if purge_check and purge_count > 0 and action != "log":
            try:
                await channel.purge(limit=purge_count, check=purge_check)
            except Exception:
                pass

        should_dm = cfg.get("dm_member") if dm is None else dm
        # Kick and ban already sent the full embed with the appeal link, so the
        # plain one-liner would just be a second DM about the same thing.
        if should_dm and action not in ("kick", "ban"):
            try:
                await user.send(f"AutoMod in **{guild.name}** removed one of your messages: {trigger_info}.")
            except Exception:
                pass

        automod_config.record_incident(guild.id, {
            "at": datetime.datetime.utcnow().isoformat(), "rule": rule_id or trigger_type,
            "label": trigger_type, "user_id": str(user.id), "user": str(user),
            "avatar": user.display_avatar.url if user.display_avatar else "",
            "channel": getattr(channel, "name", "unknown"), "trigger": trigger_info,
            "action": action, "result": outcome, "ok": worked, "dry_run": False,
        })

        await post_system_log(
            self.bot,
            title=f"AutoMod: {trigger_type}",
            description=f"Automated moderation response in #{getattr(channel, 'name', 'unknown')}.",
            fields=[
                ("Offender", f"{user.mention} (`{user.id}`)", True),
                ("Channel", channel.mention, True),
                ("Action", outcome, True),
                ("Trigger", trigger_info, False)
            ],
            color=discord.Color.orange(),
            thumbnail_url=user.display_avatar.url if user.display_avatar else None
        )
        return worked

    async def _enforce_media_only(self, message: discord.Message) -> bool:
        """Delete a text post in a media-only channel. True means "handled".

        Staff holding `manage_messages` are exempt, so a moderator can still
        type an announcement in a media channel.
        """
        channel = message.channel
        if not is_media_only(message.guild.id, getattr(channel, "id", None)):
            return False
        perms = getattr(message.author, "guild_permissions", None)
        if perms is not None and perms.manage_messages:
            return False
        if is_media_message(message):
            return False
        try:
            await message.delete()
        except Exception:
            # Without delete permission there is nothing useful to do here, so
            # let the ordinary rules take their turn instead of claiming this
            # was handled.
            return False
        await self._warn_media_only(message)
        return True

    async def _warn_media_only(self, message: discord.Message):
        """Best-effort DM, muted for 10 minutes after 3 hits inside a minute.

        Without the mute, someone determined to talk in a media channel gets a
        DM for every attempt, which is worse than the thing being prevented.
        """
        uid = message.author.id
        now = time.monotonic()
        if now < _media_warn_muted_until.get(uid, 0.0):
            return
        recent = [t for t in _media_warn_hits.get(uid, []) if now - t <= MEDIA_WARN_WINDOW]
        recent.append(now)
        _media_warn_hits[uid] = recent
        if len(recent) >= MEDIA_WARN_THRESHOLD:
            _media_warn_muted_until[uid] = now + MEDIA_WARN_MUTE
            return
        name = getattr(message.channel, "name", "this channel")
        try:
            await message.author.send(f"**#{name}** is a media-only channel — your message was removed.")
        except Exception:
            pass

    async def process_user_messages(self, message: discord.Message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return

        enabled = log_config.is_enabled(message.guild.id)
        debug(f"[ONMSG] from {message.author.id} in guild {message.guild.id} | enabled={enabled}")

        if enabled:
            log_message(
                self.bot,
                message.guild.id,
                message.author.id,
                message.channel.name if hasattr(message.channel, "name") else "unknown",
                message.content or "",
                message.id
            )

        # A media-only channel is a property of the channel, not a configured
        # rule, so it is enforced before the suite's own switch is consulted —
        # otherwise turning AutoMod off would quietly reopen every media channel.
        if await self._enforce_media_only(message):
            return

        cfg = self._cfg(message.guild.id)
        if not cfg.get("enabled"):
            return
        if self._exempt(message, cfg):
            return

        guild_id = message.guild.id
        user_id = message.author.id
        channel_id = message.channel.id
        content = message.content or ""

        async def trip(rule_id, label, info, purge_check=None, dm=None):
            rule = self._rule(cfg, rule_id)
            return await self.execute_punishment(
                message=message, trigger_type=label, trigger_info=info,
                purge_check=purge_check, rule_id=rule_id,
                action=str(rule.get("action") or "delete"), dm=dm,
            )

        # --- word list first: an owner's explicit list outranks the heuristics
        words_rule = self._rule(cfg, "words")
        if self._uses(words_rule):
            hit = self._words_hit(content, words_rule)
            if hit:
                await trip("words", "Blocked word",
                           f"Message matched the blocked term `{hit}`",
                           purge_check=lambda m: self._words_hit(m.content, words_rule) is not None)
                return

        # --- flood rules
        rules = cfg.get("rules") or {}
        link_rule = rules.get("links") or {}
        # The one place a link from an allow-listed member is left alone:
        # a self-promo channel. Everywhere else the link rule is unchanged.
        if (
            self._uses(link_rule)
            and self.contains_harmful_link(content)
            and not (
                is_selfpromo_channel(guild_id, channel_id)
                and member_is_selfpromo_allowed(guild_id, channel_id, message.author)
            )
        ):
            if self._trip(("link_tracker", guild_id, user_id), int(link_rule.get("count") or 3), float(link_rule.get("window") or 5)):
                await trip("links", "Link spam",
                           f"Sent {link_rule.get('count')} non-media links in under {link_rule.get('window')}s",
                           purge_check=lambda m: m.author.id == user_id and self.contains_harmful_link(m.content))
                return

        invite_rule = rules.get("invites") or {}
        if self._uses(invite_rule) and self.DISCORD_INVITE_REGEX.search(content):
            if self._trip(("invites_tracker", guild_id, user_id), int(invite_rule.get("count") or 2), float(invite_rule.get("window") or 10)):
                await trip("invites", "Discord invite spam",
                           f"Posted {invite_rule.get('count')} Discord invites in under {invite_rule.get('window')}s",
                           purge_check=lambda m: m.author.id == user_id and self.DISCORD_INVITE_REGEX.search(m.content))
                return

        mention_rule = rules.get("mentions") or {}
        if self._uses(mention_rule):
            total_mentions = len(message.mentions) + len(message.role_mentions) + (1 if message.mention_everyone else 0)
            if total_mentions > 0:
                key = ("mention_tracker", guild_id, user_id)
                stamps = self._bucket(key)
                window = float(mention_rule.get("window") or 5)
                now = asyncio.get_event_loop().time()
                stamps[:] = [t for t in stamps if now - t < window]
                stamps.extend([now] * total_mentions)
                if len(stamps) >= int(mention_rule.get("count") or 5):
                    stamps.clear()
                    await trip("mentions", "Mass mention / ping spam",
                               f"Exceeded {mention_rule.get('count')} mentions in under {mention_rule.get('window')}s",
                               purge_check=lambda m: m.author.id == user_id and (m.mentions or m.role_mentions or m.mention_everyone))
                    return

        repeat_rule = rules.get("repeat") or {}
        if self._uses(repeat_rule) and content.strip():
            key = ("repeat_tracker", guild_id, user_id)
            window = float(repeat_rule.get("window") or 10)
            now = asyncio.get_event_loop().time()
            stamps = self._bucket(key)
            # Keep the actual text with the stamp so only *identical* lines count.
            stamps[:] = [s for s in stamps if now - s[0] < window]
            stamps.append((now, content.strip().lower()))
            needle = content.strip().lower()
            same = sum(1 for s in stamps if s[1] == needle)
            if same >= int(repeat_rule.get("count") or 3):
                stamps.clear()
                await trip("repeat", "Repeated messages",
                           f"Posted the same line {same} times in under {repeat_rule.get('window')}s",
                           purge_check=lambda m: (m.author.id == user_id and (m.content or "").strip().lower() == needle))
                return

        # --- content heuristics
        caps_rule = rules.get("caps") or {}
        if self._uses(caps_rule):
            hit = self._caps_hit(content, caps_rule)
            if hit:
                await trip("caps", "Excessive caps", hit)
                return

        zalgo_rule = rules.get("zalgo") or {}
        if self._uses(zalgo_rule):
            hit = self._zalgo_hit(content, zalgo_rule)
            if hit:
                await trip("zalgo", "Zalgo text", hit)
                return

        emoji_rule = rules.get("emoji") or {}
        if self._uses(emoji_rule):
            hit = self._emoji_hit(content, emoji_rule)
            if hit:
                await trip("emoji", "Emoji flood", hit)
                return

    async def process_webhook_message(self, message: discord.Message):
        if not message.webhook_id or not message.guild:
            return

        cfg = self._cfg(message.guild.id)
        rule = self._rule(cfg, "webhooks")
        if not cfg.get("enabled") or not self._uses(rule) or cfg.get("dry_run"):
            return

        max_msgs = int(rule.get("count") or self.WEBHOOK_MAX_MSG)
        window = float(rule.get("window") or self.WEBHOOK_WINDOW)
        now = asyncio.get_event_loop().time()
        key = (message.guild.id, message.webhook_id)

        timestamps = self.webhook_tracker[key]
        timestamps[:] = [t for t in timestamps if now - t < window]
        timestamps.append(now)

        if len(timestamps) >= max_msgs:
            self.webhook_tracker.pop(key, None)
            guild = message.guild
            webhook_id = message.webhook_id
            channel = message.channel

            creator_info = "Unknown (Audit Log unreachable or expired)"

            if guild.me.guild_permissions.view_audit_log:
                try:
                    async for entry in guild.audit_logs(action=discord.AuditLogAction.webhook_create, limit=15):
                        if entry.target and entry.target.id == webhook_id:
                            creator_info = f"{entry.user.mention} (`{entry.user.id}`)"
                            break
                except Exception:
                    pass

            webhook_deleted = False
            if guild.me.guild_permissions.manage_webhooks:
                try:
                    webhooks = await channel.webhooks()
                    for wh in webhooks:
                        if wh.id == webhook_id:
                            await wh.delete(reason="Anti-Raid: Webhook Spam Protection")
                            webhook_deleted = True
                            break
                except Exception:
                    pass

            try:
                await channel.purge(limit=max(1, int(cfg.get("purge_count") or 10)), check=lambda m: m.webhook_id == webhook_id)
            except Exception:
                pass

            automod_config.record_incident(guild.id, {
                "at": datetime.datetime.utcnow().isoformat(), "rule": "webhooks",
                "label": "Rogue webhook", "user_id": "", "user": creator_info,
                "channel": getattr(channel, "name", "unknown"),
                "trigger": f"Webhook posted {max_msgs} messages in under {window:g}s",
                "action": str(rule.get("action") or "delete"),
                "result": "Webhook deleted" if webhook_deleted else "Webhook could not be deleted",
                "ok": bool(webhook_deleted), "dry_run": False,
            })

            await post_system_log(
                self.bot,
                title="Rogue Webhook Nuked",
                description=f"Webhook in #{channel.name} exceeded message threshold.",
                fields=[
                    ("Channel", channel.mention, True),
                    ("Webhook ID", f"`{webhook_id}`", True),
                    ("Creator", creator_info, False),
                    ("Deleted", "Yes" if webhook_deleted else "No", True)
                ],
                color=discord.Color.red()
            )


# The one running detector set, so the dashboard can run a typed message through
# the exact same checks the bot applies to real ones.
_anti_raid_system = None


def get_anti_raid():
    """The live AntiRaidSystem, or None before the bot has started."""
    return _anti_raid_system


def setup_anti_raid(bot):
    global _anti_raid_system
    system = AntiRaidSystem(bot)
    _anti_raid_system = system

    @bot.event
    async def on_message(message: discord.Message):
        if message.webhook_id:
            await system.process_webhook_message(message)
        else:
            await system.process_user_messages(message)

        await bot.process_commands(message)

    @bot.event
    async def on_message_delete(message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        if not log_config.is_enabled(message.guild.id):
            return

        log_deleted_message(
            bot,
            message.guild.id,
            message.author.id,
            message.channel.name if hasattr(message.channel, "name") else "unknown",
            message.content or "[no text content]",
            message.id
        )

    @bot.event
    async def on_message_edit(before: discord.Message, after: discord.Message):
        if not after.guild or after.author.bot:
            return
        if not isinstance(after.author, discord.Member):
            return
        if not log_config.is_enabled(after.guild.id):
            return
        if before.content == after.content:
            return

        original = await find_original_line(bot, after.guild.id, after.author.id, after.id)
        if original is None:
            original = before.content or "[unknown]"

        log_edited_message(
            bot,
            after.guild.id,
            after.author.id,
            after.channel.name if hasattr(after.channel, "name") else "unknown",
            original,
            after.content or "[empty]",
            after.id
        )

    @bot.event
    async def on_member_join(member: discord.Member):
        if watchlist.is_watched(member.guild.id, member.id):
            entry = watchlist.get(member.guild.id, member.id)
            await post_system_log(
                bot,
                title="Watchlisted Member Joined",
                description=f"{member.mention} is on the watchlist.",
                fields=[
                    ("User", f"{member.mention} (`{member.id}`)", True),
                    ("Added By", f"<@{entry['author_id']}>", True),
                    ("Reason", entry["reason"][:1000], False)
                ],
                color=discord.Color.from_rgb(250, 166, 26),
                thumbnail_url=member.display_avatar.url
            )

    return system