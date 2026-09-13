"""
Flask dashboard: all routes and API endpoints.
The actual HTML lives in templates/dashboard.html.

Edit this file when you want to add or modify API endpoints.
Edit templates/dashboard.html when you want to change the UI.
"""

import os
import math
import time
import datetime
import platform
import subprocess
import asyncio
import psutil
import discord
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
BOT_START = time.time()

# The bot instance is injected at startup by main.py
bot = None

def set_bot(b):
    global bot
    bot = b


# ==========================================================
# PAGE
# ==========================================================

@app.route("/")
def index():
    return render_template("dashboard.html")


# ==========================================================
# STATS / TELEMETRY
# ==========================================================

@app.route("/api/stats", methods=["GET"])
def api_stats():
    ping_val = 0 if math.isnan(bot.latency) else round(bot.latency * 1000)
    user_str = str(bot.user) if bot.user else "Connecting..."
    vm = psutil.virtual_memory()
    net = psutil.net_io_counters()
    proc = psutil.Process(os.getpid())
    try:
        proc_mem = round(proc.memory_info().rss / 1024 / 1024, 1)
        proc_cpu = round(proc.cpu_percent(interval=None), 1)
    except Exception:
        proc_mem, proc_cpu = 0, 0
    uptime_secs = int(time.time() - BOT_START)
    uptime_str = f"{uptime_secs // 3600}h {(uptime_secs % 3600) // 60}m {uptime_secs % 60}s"
    member_total = sum(g.member_count or 0 for g in bot.guilds)
    return jsonify({
        "cpu": psutil.cpu_percent(),
        "ram": vm.percent,
        "guilds": len(bot.guilds),
        "ping": ping_val,
        "user": user_str,
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_used_gb": round(vm.used / 1024 ** 3, 2),
        "ram_total_gb": round(vm.total / 1024 ** 3, 2),
        "member_total": member_total,
        "uptime": uptime_str,
        "os": f"{platform.system()} {platform.release()}",
        "hostname": platform.node(),
        "pid": os.getpid(),
        "proc_mem_mb": proc_mem,
        "proc_cpu": proc_cpu,
        "net_sent_mb": round(net.bytes_sent / 1024 / 1024, 2),
        "net_recv_mb": round(net.bytes_recv / 1024 / 1024, 2),
    })


# ==========================================================
# GUILDS / CHANNELS / USERS / ROLES
# ==========================================================

@app.route("/api/channels", methods=["GET"])
def api_channels():
    payload = []
    for guild in bot.guilds:
        channels_list = []
        for channel in guild.channels:
            if isinstance(channel, discord.TextChannel):
                ch_type = "announcement" if channel.is_news() else "text"
            elif isinstance(channel, discord.VoiceChannel):
                ch_type = "voice"
            else:
                continue
            channels_list.append({"id": str(channel.id), "name": channel.name, "type": ch_type})
        payload.append({
            "guild_id": str(guild.id),
            "guild_name": guild.name,
            "icon": str(guild.icon.url) if guild.icon else "",
            "member_count": guild.member_count or 0,
            "role_count": len(guild.roles),
            "channels": channels_list
        })
    return jsonify(payload)


@app.route("/api/guild/<guild_id>/users", methods=["GET"])
def api_guild_users(guild_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify([])
    users = []
    for m in guild.members[:500]:
        users.append({
            "id": str(m.id),
            "name": str(m),
            "avatar": str(m.display_avatar.url) if m.display_avatar else ""
        })
    return jsonify(users)


@app.route("/api/guild/<guild_id>/roles", methods=["GET"])
def api_guild_roles(guild_id):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify([])
    roles = []
    for r in sorted(guild.roles, key=lambda x: x.position, reverse=True):
        if r.name == "@everyone":
            continue
        roles.append({
            "id": str(r.id),
            "name": r.name,
            "color": str(r.color) if str(r.color) != "#000000" else "#3f3f46"
        })
    return jsonify(roles)


# ==========================================================
# BROADCAST / DISPATCH
# ==========================================================

@app.route("/api/dispatch", methods=["POST"])
def api_dispatch():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    content = data.get("content", "")
    is_embed = data.get("is_embed", False)
    tts = data.get("tts", False)
    delete_after = data.get("delete_after")
    channel_delay = data.get("channel_delay", 0) / 1000.0

    embed = None
    if is_embed:
        title = data.get("embed_title", "")
        color_hex = (data.get("embed_color", "#5865F2") or "#5865F2").replace("#", "")
        try:
            color_int = int(color_hex, 16)
        except ValueError:
            color_int = 0x5865F2
        embed = discord.Embed(title=title or None, description=content, color=color_int)
        author = data.get("embed_author")
        if author:
            embed.set_author(name=author)
        if data.get("embed_thumbnail"):
            embed.set_thumbnail(url=data["embed_thumbnail"])
        if data.get("embed_image"):
            embed.set_image(url=data["embed_image"])
        if data.get("embed_footer"):
            embed.set_footer(text=data["embed_footer"])
        if data.get("embed_timestamp"):
            embed.timestamp = discord.utils.utcnow()

    async def do_send():
        sent = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if not ch:
                continue
            try:
                kwargs = {"tts": tts}
                if delete_after:
                    kwargs["delete_after"] = delete_after
                if is_embed and embed:
                    await ch.send(embed=embed, **kwargs)
                else:
                    await ch.send(content, **kwargs)
                sent += 1
                if channel_delay > 0:
                    await asyncio.sleep(channel_delay)
            except Exception:
                pass
        return sent

    try:
        sent = asyncio.run_coroutine_threadsafe(do_send(), bot.loop).result(timeout=30)
    except Exception:
        sent = 0
    if sent > 0:
        return jsonify({"status": "success", "message": f"Transmitted to {sent} channel(s)."})
    return jsonify({"status": "error", "message": "Failed to send to any channel."})


# ==========================================================
# MODERATION (channels)
# ==========================================================

@app.route("/api/purge", methods=["POST"])
def api_purge():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    amount = data.get("amount", 10)
    bots_only = data.get("bots_only", False)

    async def do_purge():
        total = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if not ch:
                continue
            try:
                def check(m):
                    return m.author.bot if bots_only else True
                deleted = await ch.purge(limit=amount, check=check)
                total += len(deleted)
            except Exception:
                pass
        return total

    try:
        total = asyncio.run_coroutine_threadsafe(do_purge(), bot.loop).result(timeout=30)
    except Exception:
        total = 0
    return jsonify({"status": "success", "message": f"Purged {total} message(s)."})


@app.route("/api/slowmode", methods=["POST"])
def api_slowmode():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    seconds = data.get("seconds", 0)

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    await ch.edit(slowmode_delay=seconds)
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Slowmode updated in {n} channel(s)."})


@app.route("/api/lockdown", methods=["POST"])
def api_lockdown():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if not ch:
                continue
            try:
                ow = ch.overwrites_for(ch.guild.default_role)
                ow.send_messages = False if ow.send_messages is not False else None
                await ch.set_permissions(ch.guild.default_role, overwrite=ow)
                n += 1
            except Exception:
                pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Toggled lockdown in {n} channel(s)."})


@app.route("/api/rename", methods=["POST"])
def api_rename():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    name = data.get("name", "")

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    await ch.edit(name=name)
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Renamed {n} channel(s)."})


@app.route("/api/topic", methods=["POST"])
def api_topic():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    topic = data.get("topic", "")

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch and isinstance(ch, discord.TextChannel):
                try:
                    await ch.edit(topic=topic)
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Topic updated in {n} channel(s)."})


@app.route("/api/nsfw", methods=["POST"])
def api_nsfw():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    state = data.get("state", False)

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch and isinstance(ch, discord.TextChannel):
                try:
                    await ch.edit(nsfw=state)
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"NSFW set in {n} channel(s)."})


@app.route("/api/archive", methods=["POST"])
def api_archive():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    archive = data.get("archive", True)

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.edit(archived=archive)
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Archive state updated in {n} channel(s)."})


@app.route("/api/clone", methods=["POST"])
def api_clone():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    await ch.clone()
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Cloned {n} channel(s)."})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if ch:
                try:
                    await ch.delete()
                    n += 1
                except Exception:
                    pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Deleted {n} channel(s)."})


@app.route("/api/permissions", methods=["POST"])
def api_permissions():
    data = request.json or {}
    channel_ids = data.get("channel_ids", [])
    role_id = int(data.get("role_id", 0))
    perm = data.get("permission", "send_messages")
    state = data.get("state", "allow")

    state_val = {"allow": True, "deny": False, "neutral": None}[state]

    async def do():
        n = 0
        for cid in channel_ids:
            ch = bot.get_channel(int(cid))
            if not ch:
                continue
            target = ch.guild.get_role(role_id) or ch.guild.get_member(role_id)
            if not target:
                continue
            try:
                ow = ch.overwrites_for(target)
                setattr(ow, perm, state_val)
                await ch.set_permissions(target, overwrite=ow)
                n += 1
            except Exception:
                pass
        return n

    try:
        n = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
    except Exception:
        n = 0
    return jsonify({"status": "success", "message": f"Permissions applied to {n} channel(s)."})


# ==========================================================
# MEMBERS
# ==========================================================

@app.route("/api/member/ban", methods=["POST"])
def api_member_ban():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    try:
        user = discord.Object(id=int(data.get("user_id", 0)))
    except Exception:
        return jsonify({"status": "error", "message": "Invalid user ID."})

    async def do():
        await guild.ban(user, reason=data.get("reason") or None, delete_message_days=data.get("delete_days", 0))
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": f"Banned {data.get('user_id')}."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Ban failed: {e}"})


@app.route("/api/member/kick", methods=["POST"])
def api_member_kick():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    member = guild.get_member(int(data.get("user_id", 0)))
    if not member:
        return jsonify({"status": "error", "message": "Member not found."})

    async def do():
        await member.kick(reason=data.get("reason") or None)
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": f"Kicked {member}."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Kick failed: {e}"})


@app.route("/api/member/timeout", methods=["POST"])
def api_member_timeout():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    member = guild.get_member(int(data.get("user_id", 0)))
    if not member:
        return jsonify({"status": "error", "message": "Member not found."})
    seconds = int(data.get("seconds", 0))
    until = None if seconds == 0 else discord.utils.utcnow() + datetime.timedelta(seconds=seconds)

    async def do():
        await member.timeout(until)
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        msg = "Timeout removed." if seconds == 0 else f"Timed out for {seconds}s."
        return jsonify({"status": "success", "message": msg})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Timeout failed: {e}"})


@app.route("/api/member/dm", methods=["POST"])
def api_member_dm():
    data = request.json or {}
    user_id = int(data.get("user_id", 0))
    content = data.get("content", "")
    if not content:
        return jsonify({"status": "error", "message": "Empty message."})

    async def do():
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(content)
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": "DM sent."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"DM failed: {e}"})


# ==========================================================
# ROLES
# ==========================================================

@app.route("/api/role/create", methods=["POST"])
def api_role_create():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    color_hex = (data.get("color", "#5865F2") or "#5865F2").replace("#", "")
    try:
        color = discord.Color(int(color_hex, 16))
    except ValueError:
        color = discord.Color.default()

    async def do():
        return await guild.create_role(
            name=data.get("name") or "new-role",
            color=color,
            hoist=data.get("hoist", False),
            mentionable=data.get("mentionable", True),
        )

    try:
        role = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": f"Created role '{role.name}'."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Create failed: {e}"})


@app.route("/api/role/assign", methods=["POST"])
def api_role_assign():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    role = guild.get_role(int(data.get("role_id", 0)))
    member = guild.get_member(int(data.get("user_id", 0)))
    if not role or not member:
        return jsonify({"status": "error", "message": "Role or member not found."})
    action = data.get("action", "add")

    async def do():
        if action == "add":
            await member.add_roles(role)
        else:
            await member.remove_roles(role)
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": f"{action.capitalize()}ed role '{role.name}' for {member}."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Role update failed: {e}"})


@app.route("/api/role/delete", methods=["POST"])
def api_role_delete():
    data = request.json or {}
    guild = bot.get_guild(int(data.get("guild_id", 0)))
    if not guild:
        return jsonify({"status": "error", "message": "Guild not found."})
    role = guild.get_role(int(data.get("role_id", 0)))
    if not role:
        return jsonify({"status": "error", "message": "Role not found."})

    async def do():
        await role.delete()
        return True

    try:
        asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=15)
        return jsonify({"status": "success", "message": f"Deleted role '{role.name}'."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Delete failed: {e}"})


# ==========================================================
# PRESENCE
# ==========================================================

@app.route("/api/presence", methods=["POST"])
def api_presence():
    data = request.json or {}
    activity_type = data.get("type", "playing")
    activity_text = data.get("activity", "")
    status = data.get("status", "online")
    url = data.get("url") or None
    reset = data.get("reset", False)

    type_map = {
        "playing": discord.ActivityType.playing,
        "listening": discord.ActivityType.listening,
        "watching": discord.ActivityType.watching,
        "competing": discord.ActivityType.competing,
        "streaming": discord.ActivityType.streaming,
        "custom": discord.ActivityType.custom,
    }

    status_map = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "invisible": discord.Status.invisible,
    }

    if reset:
        activity = discord.Activity(type=discord.ActivityType.playing, name="")
        status_obj = discord.Status.online
    elif activity_type == "streaming":
        activity = discord.Streaming(name=activity_text or "Streaming", url=url or "https://twitch.tv/")
        status_obj = status_map.get(status, discord.Status.online)
    elif activity_type == "custom":
        activity = discord.CustomActivity(name=activity_text or None)
        status_obj = status_map.get(status, discord.Status.online)
    else:
        activity = discord.Activity(type=type_map.get(activity_type, discord.ActivityType.playing), name=activity_text or "")
        status_obj = status_map.get(status, discord.Status.online)

    asyncio.run_coroutine_threadsafe(bot.change_presence(activity=activity, status=status_obj), bot.loop)
    return jsonify({"status": "success", "message": "Presence updated."})


# ==========================================================
# SYSTEM
# ==========================================================

@app.route("/api/exec", methods=["POST"])
def api_exec():
    data = request.json or {}
    command = data.get("command")
    if not command:
        return jsonify({"status": "error", "output": "No command provided."})
    try:
        output = subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT, timeout=15).decode('utf-8', errors='replace')
        return jsonify({"status": "success", "output": output})
    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "output": f"Command failed (exit {e.returncode}):\n{e.output.decode('utf-8', errors='replace')}"})
    except Exception as e:
        return jsonify({"status": "error", "output": str(e)})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    from bot import GUILD_ID
    async def do():
        try:
            target_guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=target_guild)
            synced = await bot.tree.sync(guild=target_guild)
            return f"Synced {len(synced)} command(s) to guild {GUILD_ID}."
        except Exception as e:
            return f"Sync error: {e}"

    try:
        output = asyncio.run_coroutine_threadsafe(do(), bot.loop).result(timeout=30)
        return jsonify({"status": "success", "message": "Commands synced.", "output": output})
    except Exception as e:
        return jsonify({"status": "error", "message": "Sync failed.", "output": str(e)})


@app.route("/api/commands", methods=["GET"])
def api_commands():
    cmds = []
    for cmd in bot.tree.get_commands():
        cmds.append({
            "name": cmd.name,
            "description": getattr(cmd, "description", ""),
            "type": str(cmd.type) if hasattr(cmd, "type") else "unknown",
        })
    return jsonify(cmds)