import asyncio
import datetime
import json
import os
import re
from collections import defaultdict
import discord


HISTORY_FILE = "moderation_history.json"
LOG_INDEX_FILE = "verification_logs.json"
NOTES_FILE = "mod_notes.json"
WATCHLIST_FILE = "watchlist.json"
LOG_CONFIG_FILE = "log_config.json"
THREAD_INDEX_FILE = "thread_index.json"
MESSAGE_LOG_DIR = "message_logs"
DELETED_LOG_DIR = "deleted_logs"
MESSAGE_LOG_MAX_LINES = 5000
THREAD_ROLLOVER_LIMIT = 1000
BUFFER_FLUSH_INTERVAL = 5.0
BUFFER_MAX_LINES = 30
BUFFER_MAX_CHARS = 1800

MESSAGE_CATEGORY_ID = 1548581331175350363
SYSTEM_CATEGORY_ID = 1548582743368147015


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

    def record(self, guild_id: int, user_id: int, action: str, moderator_id: int, reason: str):
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
        entry["actions"].append({
            "action": action,
            "moderator_id": moderator_id,
            "reason": reason,
            "timestamp": datetime.datetime.utcnow().isoformat()
        })
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
        print(f"[FALLBACK] Failed to write {path}: {e}")


def find_logging_guild(bot) -> discord.Guild:
    candidates = []
    for guild in bot.guilds:
        msg_cat = discord.utils.get(guild.categories, id=MESSAGE_CATEGORY_ID)
        sys_cat = discord.utils.get(guild.categories, id=SYSTEM_CATEGORY_ID)
        if msg_cat and sys_cat:
            candidates.append(guild)
    print(f"[FINDLOG] Guilds with both categories: {[g.id for g in candidates]}")
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    for g in candidates:
        ch = discord.utils.get(g.text_channels, name="user-message-logs")
        if ch:
            return g
    return candidates[0]


async def ensure_logging_channels(guild: discord.Guild):
    print(f"[SETUP] ensure_logging_channels for guild {guild.id}")
    msg_cat = discord.utils.get(guild.categories, id=MESSAGE_CATEGORY_ID)
    sys_cat = discord.utils.get(guild.categories, id=SYSTEM_CATEGORY_ID)
    print(f"[SETUP] msg_cat={msg_cat} sys_cat={sys_cat}")
    if not msg_cat or not sys_cat:
        return None, None

    msg_parent = discord.utils.get(guild.text_channels, name="user-message-logs")
    print(f"[SETUP] existing msg_parent={msg_parent}")
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
                "user-message-logs",
                category=msg_cat,
                overwrites=overwrites,
                reason="Log setup"
            )
            print(f"[SETUP] created msg_parent={msg_parent.id}")
        except Exception as e:
            print(f"[SETUP] Failed to create message parent channel: {type(e).__name__}: {e}")
            return None, None

    sys_parent = discord.utils.get(guild.text_channels, name="system-logs")
    print(f"[SETUP] existing sys_parent={sys_parent}")
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
                "system-logs",
                category=sys_cat,
                overwrites=overwrites,
                reason="Log setup"
            )
            print(f"[SETUP] created sys_parent={sys_parent.id}")
        except Exception as e:
            print(f"[SETUP] Failed to create system parent channel: {type(e).__name__}: {e}")
            return None, None

    log_config.update(
        logging_guild_id=guild.id,
        message_parent_channel_id=msg_parent.id,
        system_parent_channel_id=sys_parent.id
    )
    print(f"[SETUP] config updated: {log_config.get()}")
    return msg_parent, sys_parent


async def get_or_create_thread(bot, source_guild_id: int, user: discord.User) -> discord.Thread | None:
    logging_guild = find_logging_guild(bot)
    print(f"[THREAD] Logging guild resolved: {logging_guild.id if logging_guild else None}")
    if not logging_guild:
        return None

    parent = None
    cfg = log_config.get()
    print(f"[THREAD] Config: {cfg}")
    if cfg.get("message_parent_channel_id"):
        parent = logging_guild.get_channel(cfg["message_parent_channel_id"])
        if parent is None:
            try:
                parent = await bot.fetch_channel(cfg["message_parent_channel_id"])
            except Exception as e:
                print(f"[THREAD] fetch parent failed: {e}")
                parent = None
    if not parent:
        parent, _ = await ensure_logging_channels(logging_guild)
    print(f"[THREAD] Parent channel: {parent}")
    if not parent:
        return None

    perms = parent.permissions_for(logging_guild.me)
    print(f"[THREAD] Bot perms - send={perms.send_messages}, create_threads={perms.create_public_threads}, send_in_threads={perms.send_messages_in_threads}, manage_threads={perms.manage_threads}")
    if not perms.send_messages or not perms.create_public_threads:
        print("[THREAD] Missing permission to create threads in parent")
        return None

    newest_id = thread_index.newest_thread_id(source_guild_id, user.id)
    newest_count = thread_index.newest_count(source_guild_id, user.id)
    print(f"[THREAD] Existing newest thread: {newest_id} (count={newest_count})")

    if newest_id:
        thread = logging_guild.get_thread(newest_id)
        if thread is None:
            try:
                thread = await bot.fetch_channel(newest_id)
            except Exception as e:
                print(f"[THREAD] Could not fetch existing thread: {e}")
                thread = None
        if thread and newest_count < THREAD_ROLLOVER_LIMIT:
            if thread.archived:
                try:
                    await thread.edit(archived=False)
                except Exception as e:
                    print(f"[THREAD] Failed to unarchive: {e}")
            return thread

    rollover = len(thread_index.all_thread_ids(source_guild_id, user.id)) + 1
    name = f"{user.name[:20]}-{user.id}-p{rollover}"
    print(f"[THREAD] Creating new thread: {name}")
    try:
        thread = await parent.create_thread(name=name)
        print(f"[THREAD] Created thread: {thread.id}")
    except Exception as e:
        print(f"[THREAD] Failed to create thread: {type(e).__name__}: {e}")
        return None

    thread_index.push_thread(source_guild_id, user.id, thread.id, name)
    return thread


async def flush_user_buffer(bot, source_guild_id: int, user_id: int):
    key = (source_guild_id, user_id)
    lines = _pending_lines.pop(key, [])
    _flush_tasks.pop(key, None)
    print(f"[FLUSH] Flushing {len(lines)} line(s) for {user_id} in {source_guild_id}")
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
            print(f"[FLUSH] Fetched user {user_id} via API")
        except Exception as e:
            print(f"[FLUSH] Could not resolve user {user_id}: {e}")
            user = None
    if user is None:
        for line in lines:
            _fallback_append(_msg_log_path(source_guild_id, user_id), line + "\n")
        print(f"[FLUSH] No user object, fell back to txt")
        return

    thread = await get_or_create_thread(bot, source_guild_id, user)
    print(f"[FLUSH] Thread resolved: {thread}")
    if thread is None:
        for line in lines:
            _fallback_append(_msg_log_path(source_guild_id, user_id), line + "\n")
        print(f"[FLUSH] No thread, fell back to txt")
        return

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 1900:
            try:
                await thread.send(chunk)
                thread_index.increment_newest(source_guild_id, user_id)
                print(f"[FLUSH] Sent chunk to thread {thread.id}")
            except Exception as e:
                print(f"[FLUSH] Send to thread failed: {type(e).__name__}: {e}")
                for l in chunk.split("\n"):
                    if l:
                        _fallback_append(_msg_log_path(source_guild_id, user_id), l + "\n")
            chunk = ""
        chunk += line + "\n"

    if chunk.strip():
        try:
            await thread.send(chunk.strip())
            thread_index.increment_newest(source_guild_id, user_id)
            print(f"[FLUSH] Sent final chunk to thread {thread.id}")
        except Exception as e:
            print(f"[FLUSH] Send to thread failed: {type(e).__name__}: {e}")
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
            print(f"[FLUSH] Runner error: {type(e).__name__}: {e}")
        finally:
            _flush_tasks.pop(key, None)

    _flush_tasks[key] = asyncio.create_task(_runner())


def queue_log_line(bot, source_guild_id: int, user_id: int, line: str):
    key = (source_guild_id, user_id)
    _pending_lines[key].append(line)
    total_chars = sum(len(l) for l in _pending_lines[key])
    print(f"[QUEUE] Buffered for {user_id} in {source_guild_id} (buffer={len(_pending_lines[key])}, chars={total_chars})")

    if len(_pending_lines[key]) >= BUFFER_MAX_LINES or total_chars >= BUFFER_MAX_CHARS:
        asyncio.create_task(flush_user_buffer(bot, source_guild_id, user_id))
        return

    schedule_flush(bot, source_guild_id, user_id)


async def flush_all_buffers(bot):
    keys = list(_pending_lines.keys())
    print(f"[FLUSHALL] Flushing {len(keys)} buffers")
    for key in keys:
        try:
            await flush_user_buffer(bot, key[0], key[1])
        except Exception as e:
            print(f"[FLUSHALL] error: {type(e).__name__}: {e}")


def format_line(timestamp: datetime.datetime, channel_name: str, message_id: int, content: str, tag: str = None) -> str:
    ts = timestamp.strftime("%Y-%m-%d %H:%M:%S")
    safe = (content or "[no text]").replace("\n", "\\n").replace("\r", "")
    prefix = f"[{tag}] " if tag else ""
    return f"[{ts}] [#{channel_name}] (id={message_id}) {prefix}{safe}"


def log_message(bot, source_guild_id: int, user_id: int, channel_name: str, content: str, message_id: int):
    line = format_line(datetime.datetime.utcnow(), channel_name, message_id, content)
    print(f"[LOG] log_message guild={source_guild_id} user={user_id}")
    queue_log_line(bot, source_guild_id, user_id, line)


def log_deleted_message(bot, source_guild_id: int, user_id: int, channel_name: str, content: str, message_id: int):
    line = format_line(datetime.datetime.utcnow(), channel_name, message_id, content, tag="DELETED")
    print(f"[LOG] log_deleted guild={source_guild_id} user={user_id}")
    queue_log_line(bot, source_guild_id, user_id, line)


def log_edited_message(bot, source_guild_id: int, user_id: int, channel_name: str, original: str, edited: str, message_id: int):
    safe_original = (original or "[unknown]").replace("\n", "\\n")
    safe_edited = (edited or "[empty]").replace("\n", "\\n")
    content = f"{safe_original} ⇒ {safe_edited}"
    line = format_line(datetime.datetime.utcnow(), channel_name, message_id, content, tag="EDITED")
    print(f"[LOG] log_edited guild={source_guild_id} user={user_id}")
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
    print(f"[FETCH] thread_ids={thread_ids}")
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
                print(f"[FETCH] fetch thread {tid} failed: {e}")
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
            print(f"[FETCH] history failed for {tid}: {e}")
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
        print("[SYSLOG] No logging guild found")
        return

    cfg = log_config.get()
    channel_id = cfg.get("system_parent_channel_id")
    channel = logging_guild.get_channel(channel_id) if channel_id else None
    if not channel:
        _, channel = await ensure_logging_channels(logging_guild)
    if not channel:
        print("[SYSLOG] No system channel")
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
        print(f"[SYSLOG] Sent to {channel.id}")
    except Exception as e:
        print(f"[SYSLOG] Failed: {type(e).__name__}: {e}")


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
        print(f"[VERIFY] Failed to edit log: {type(e).__name__}: {e}")
        return False


async def safe_refresh_verification_log(bot, guild: discord.Guild, user_id: int):
    try:
        await refresh_verification_log(bot, guild, user_id)
    except Exception as e:
        print(f"[VERIFY] safe_refresh failed: {type(e).__name__}: {e}")


class VerificationView(discord.ui.View):
    def __init__(self, verified_role_id: int, bot_avatar: str = None):
        super().__init__(timeout=None)
        self.verified_role_id = verified_role_id
        self.bot_avatar = bot_avatar

    def build_panel_embed(self, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(
            title="Server Verification",
            description=(
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
            color=discord.Color.from_rgb(88, 101, 242)
        )
        if guild and guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
            embed.set_author(name=guild.name, icon_url=guild.icon.url)
        if self.bot_avatar:
            embed.set_footer(text="Anti-Raid Security System", icon_url=self.bot_avatar)
        else:
            embed.set_footer(text="Anti-Raid Security System")
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

        role = guild.get_role(self.verified_role_id)
        if not role:
            return await interaction.response.send_message(
                "Verification unavailable. The verified role is missing. Please inform an admin.",
                ephemeral=True
            )

        if role in member.roles:
            return await interaction.response.send_message(
                "You are already verified. You already have access to the server.",
                ephemeral=True
            )

        try:
            await member.add_roles(role, reason="Passed anti-raid verification.")
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
            print(f"[VERIFY] Invite resolution failed: {e}")
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
                    print(f"[VERIFY] Failed to send log embed: {type(e).__name__}: {e}")


async def deploy_verification_panel(channel: discord.TextChannel, verified_role_id: int, bot_user: discord.User = None):
    guild = channel.guild
    view = VerificationView(
        verified_role_id=verified_role_id,
        bot_avatar=bot_user.display_avatar.url if bot_user else None
    )
    embed = view.build_panel_embed(guild)
    await channel.send(embed=embed, view=view)


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


class AntiRaidSystem:
    def __init__(self, bot):
        self.bot = bot

        self.webhook_tracker = defaultdict(list)
        self.link_tracker = defaultdict(list)
        self.mention_tracker = defaultdict(list)
        self.invites_tracker = defaultdict(list)

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

        self.MEDIA_EXCLUSIONS = re.compile(
            r'(tenor\.com|giphy\.com|gfycat\.com|imgur\.com|'
            r'media\.discordapp\.net|cdn\.discordapp\.com|'
            r'\.(gif|png|jpg|jpeg|webp|mp4|webm|mov)(\?.*)?$)',
            re.IGNORECASE
        )

    def contains_harmful_link(self, content: str) -> bool:
        urls = self.URL_REGEX.findall(content)
        if not urls:
            return False

        for url in urls:
            if self.MEDIA_EXCLUSIONS.search(url):
                continue
            return True

        return False

    async def execute_punishment(self, message: discord.Message, trigger_type: str, trigger_info: str, purge_check=None):
        user = message.author
        guild = message.guild
        channel = message.channel

        try:
            await message.delete()
        except Exception:
            pass

        if purge_check:
            try:
                await channel.purge(limit=15, check=purge_check)
            except Exception:
                pass

        timeout_success = False
        try:
            until = discord.utils.utcnow() + datetime.timedelta(seconds=self.TIMEOUT_DURATION)
            await user.timeout(until, reason=f"Anti-Raid: {trigger_type}")
            timeout_success = True
            history_tracker.record(guild.id, user.id, "timeout", self.bot.user.id, f"Anti-Raid: {trigger_type}")
            await safe_refresh_verification_log(self.bot, guild, user.id)
        except Exception:
            pass

        await post_system_log(
            self.bot,
            title=f"Anti-Spam Triggered: {trigger_type}",
            description=f"Automated moderation response in #{channel.name}.",
            fields=[
                ("Offender", f"{user.mention} (`{user.id}`)", True),
                ("Channel", channel.mention, True),
                ("Action", "5-Min Timeout" if timeout_success else "Purge only", True),
                ("Trigger", trigger_info, False)
            ],
            color=discord.Color.orange(),
            thumbnail_url=user.display_avatar.url
        )

    async def process_user_messages(self, message: discord.Message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return

        enabled = log_config.is_enabled(message.guild.id)
        print(f"[ONMSG] from {message.author.id} in guild {message.guild.id} | enabled={enabled}")

        if enabled:
            log_message(
                self.bot,
                message.guild.id,
                message.author.id,
                message.channel.name if hasattr(message.channel, "name") else "unknown",
                message.content or "",
                message.id
            )

        if message.author.guild_permissions.administrator:
            return

        now = asyncio.get_event_loop().time()
        guild_id = message.guild.id
        user_id = message.author.id

        if self.contains_harmful_link(message.content):
            key = (guild_id, user_id)
            timestamps = self.link_tracker[key]
            timestamps[:] = [t for t in timestamps if now - t < self.LINK_WINDOW]
            timestamps.append(now)

            if len(timestamps) >= self.LINK_MAX_COUNT:
                self.link_tracker.pop(key, None)
                await self.execute_punishment(
                    message=message,
                    trigger_type="Link Spam",
                    trigger_info=f"Sent `{self.LINK_MAX_COUNT}` non-media links in under `{self.LINK_WINDOW}`s",
                    purge_check=lambda m: m.author.id == user_id and self.contains_harmful_link(m.content)
                )
                return

        if self.DISCORD_INVITE_REGEX.search(message.content):
            key = (guild_id, user_id)
            timestamps = self.invites_tracker[key]
            timestamps[:] = [t for t in timestamps if now - t < self.INVITE_WINDOW]
            timestamps.append(now)

            if len(timestamps) >= self.INVITE_MAX_COUNT:
                self.invites_tracker.pop(key, None)
                await self.execute_punishment(
                    message=message,
                    trigger_type="Discord Invite Spam",
                    trigger_info=f"Posted `{self.INVITE_MAX_COUNT}` Discord invites in under `{self.INVITE_WINDOW}`s",
                    purge_check=lambda m: m.author.id == user_id and self.DISCORD_INVITE_REGEX.search(m.content)
                )
                return

        total_mentions = len(message.mentions) + len(message.role_mentions)
        if message.mention_everyone:
            total_mentions += 1

        if total_mentions > 0:
            key = (guild_id, user_id)
            timestamps = self.mention_tracker[key]
            timestamps[:] = [t for t in timestamps if now - t < self.MENTION_WINDOW]
            for _ in range(total_mentions):
                timestamps.append(now)

            if len(timestamps) >= self.MENTION_MAX_COUNT:
                self.mention_tracker.pop(key, None)
                await self.execute_punishment(
                    message=message,
                    trigger_type="Mass Mention / Ping Spam",
                    trigger_info=f"Exceeded `{self.MENTION_MAX_COUNT}` user/role mentions in under `{self.MENTION_WINDOW}`s",
                    purge_check=lambda m: m.author.id == user_id and (len(m.mentions) > 0 or len(m.role_mentions) > 0 or m.mention_everyone)
                )
                return

    async def process_webhook_message(self, message: discord.Message):
        if not message.webhook_id or not message.guild:
            return

        now = asyncio.get_event_loop().time()
        key = (message.guild.id, message.webhook_id)

        timestamps = self.webhook_tracker[key]
        timestamps[:] = [t for t in timestamps if now - t < self.WEBHOOK_WINDOW]
        timestamps.append(now)

        if len(timestamps) >= self.WEBHOOK_MAX_MSG:
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
                await channel.purge(limit=10, check=lambda m: m.webhook_id == webhook_id)
            except Exception:
                pass

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


def setup_anti_raid(bot):
    system = AntiRaidSystem(bot)

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