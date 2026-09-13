import os
import math
import asyncio
import datetime
import re
import random
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

from antiraid import (
    setup_anti_raid,
    deploy_verification_panel,
    history_tracker,
    log_index,
    notes_store,
    watchlist,
    log_config,
    thread_index,
    safe_refresh_verification_log,
    gather_moderation_history,
    account_risk_score,
    compute_risk,
    history_flags_from,
    risk_label,
    find_log_channel,
    find_logging_guild,
    ensure_logging_channels,
    fetch_user_thread_lines,
    build_message_history_pages,
    PaginatedLogView,
    post_system_log,
    flush_all_buffers,
)
from supabase_helper import (
    increment_command_usage,
    upsert_members,
    delete_member,
    fetch_all_member_ids,
)

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0))
POSTHELP_ROLE_ID = 1548496816989798400
VERIFIED_ROLE_ID = 1548559332063313921

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
intents.guilds = True
intents.moderation = True

bot = commands.Bot(command_prefix=">", intents=intents, help_command=None)


@bot.before_invoke
async def track_command_usage(ctx: commands.Context):
    if ctx.command is None:
        return
    try:
        increment_command_usage(ctx.command.qualified_name)
    except Exception as e:
        print(f"[SUPABASE] Usage increment failed: {e}")


STAFF_PERMS = (
    "manage_messages",
    "kick_members",
    "ban_members",
    "moderate_members",
    "manage_channels",
    "manage_roles",
    "manage_nicknames",
    "administrator",
)

PUBLIC_COMMANDS = {"ping", "serverinfo", "userinfo", "avatar", "banner", "roleinfo", "channelinfo", "uptime", "help"}

ROLES_BY_TIER = {
    "Owners": 100,
    "Admins": 80,
    "Moderators": 60,
    "Trial Mods": 40,
    "Staff": 30,
    "Helpers": 20,
    "Members": 0,
}


async def try_delete(ctx: commands.Context):
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.check
async def staff_only_gate(ctx: commands.Context):
    if ctx.command is None:
        return True
    name = ctx.command.qualified_name
    if name in PUBLIC_COMMANDS:
        return True
    if name == "posthelp":
        return True
    if ctx.guild is None:
        return False
    if ctx.author.id == ctx.guild.owner_id:
        return True
    perms = ctx.author.guild_permissions
    return any(getattr(perms, p, False) for p in STAFF_PERMS)


async def _build_member_row(member: discord.Member) -> dict:
    member_roles = []
    for r in member.roles:
        if r.name in ROLES_BY_TIER:
            member_roles.append({
                "name": r.name,
                "color": str(r.color),
                "tier": ROLES_BY_TIER[r.name],
            })

    if not member_roles:
        member_roles.append({"name": "Members", "color": "#96908a", "tier": 0})

    banner_url = ""
    try:
        user = await bot.fetch_user(member.id)
        if user.banner:
            banner_url = user.banner.url
    except Exception:
        pass

    bio = ""
    if member.premium_since:
        bio = f"Boosting since {member.premium_since.strftime('%b %d, %Y')}"

    return {
        "user_id": str(member.id),
        "username": member.name,
        "display_name": member.display_name,
        "avatar_url": member.display_avatar.url if member.display_avatar else "",
        "banner_url": banner_url,
        "bio": bio,
        "roles": member_roles,
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "created_at": member.created_at.isoformat() if member.created_at else None,
    }


async def _push_member_to_supabase(member: discord.Member):
    if member.bot:
        return
    try:
        row = await _build_member_row(member)
        upsert_members([row])
    except Exception as e:
        print(f"[SUPABASE] member sync failed for {member.id}: {e}")


@bot.command(name="verification")
@commands.has_permissions(administrator=True)
async def send_verification_panel(ctx: commands.Context, target_channel: discord.TextChannel):
    try:
        await deploy_verification_panel(target_channel, VERIFIED_ROLE_ID, bot.user)
        await ctx.send(f"Verification panel deployed to {target_channel.mention}.")
    except discord.Forbidden:
        await ctx.send("I do not have permission to send messages in that channel.")


@bot.command(name="slogs")
@commands.has_permissions(administrator=True)
async def slogs(ctx: commands.Context):
    await try_delete(ctx)

    logging_guild = find_logging_guild(bot)
    if not logging_guild:
        return await ctx.send(
            "[X] Could not find a server with the message-logs and system-logs categories. "
            f"Make sure the bot is in the server that has categories `{1548581331175350363}` and `{1548582743368147015}`."
        )

    msg_parent, sys_parent = await ensure_logging_channels(logging_guild)
    if not msg_parent or not sys_parent:
        return await ctx.send("[X] Failed to create log channels in the logging server. Check bot permissions.")

    if log_config.is_enabled(ctx.guild.id):
        log_config.disable(ctx.guild.id)
        await ctx.send(
            f"> Logging **disabled** for this server.\n"
            f"> Archive lives in **{logging_guild.name}** (`{msg_parent.name}`, `{sys_parent.name}`)."
        )
    else:
        log_config.enable(ctx.guild.id)
        await ctx.send(
            f"> Logging **enabled** for this server.\n"
            f"> Messages will be archived to **{logging_guild.name}** under `{msg_parent.name}`.\n"
            f"> System events go to `{sys_parent.name}`."
        )


@bot.command(name="syncmembers")
@commands.has_permissions(administrator=True)
async def syncmembers(ctx: commands.Context):
    await try_delete(ctx)

    msg = await ctx.send("> Syncing members to Supabase. This may take a moment...")

    current_members = {}
    async with ctx.typing():
        for member in ctx.guild.members:
            if member.bot:
                continue
            current_members[str(member.id)] = await _build_member_row(member)

    existing_ids = fetch_all_member_ids()
    current_ids = set(current_members.keys())

    to_upsert = list(current_members.values())
    to_delete = existing_ids - current_ids

    sent = 0
    failed = 0
    batch_size = 100
    for i in range(0, len(to_upsert), batch_size):
        batch = to_upsert[i:i + batch_size]
        status, raw = upsert_members(batch)
        if 200 <= status < 300:
            sent += len(batch)
        else:
            failed += len(batch)
            print(f"[SYNC] Upsert failed: {status} {raw}")

    removed = 0
    for uid in to_delete:
        status, _ = delete_member(uid)
        if 200 <= status < 300:
            removed += 1

    try:
        await msg.edit(content=(
            f"> Sync complete.\n"
            f"> Upserted: **{sent}** member(s)\n"
            f"> Failed: **{failed}**\n"
            f"> Removed (left server): **{removed}**"
        ))
    except Exception:
        await ctx.send(f"> Sync complete. Upserted {sent}, failed {failed}, removed {removed}.")


@bot.event
async def on_ready():
    print(f"- Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        target_guild = discord.Object(id=GUILD_ID)
        bot.tree.clear_commands(guild=target_guild)
        await bot.tree.sync(guild=target_guild)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("- Cleared global + guild slash commands (prefix-only mode)")
    except Exception as e:
        print(f"- Slash cleanup skipped: {e}")


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CheckFailure):
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"[!] Slow down. Try again in {error.retry_after:.1f}s.", delete_after=5)
        await try_delete(ctx)
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(f"[X] You don't have permission: `{', '.join(error.missing_permissions)}`")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send(f"[X] I'm missing permissions: `{', '.join(error.missing_permissions)}`")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"[X] Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("[X] Bad argument. Check your input.")
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        await ctx.send(f"[X] Error: `{error}`")


@bot.event
async def on_disconnect():
    try:
        await flush_all_buffers(bot)
        print("- Flushed pending log buffers before disconnect.")
    except Exception as e:
        print(f"- Flush on disconnect failed: {e}")


def parse_duration(s: str) -> int:
    if not s:
        raise ValueError("empty")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    m = re.fullmatch(r"(\d+)([smhdw])", s.lower().strip())
    if not m:
        raise ValueError("invalid format")
    return int(m.group(1)) * units[m.group(2)]


def human_delta(seconds: int) -> str:
    parts = []
    for label, secs in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= secs:
            parts.append(f"{seconds // secs}{label}")
            seconds %= secs
    return " ".join(parts) or "0s"


async def confirm(ctx: commands.Context, prompt: str, timeout: int = 20) -> bool:
    msg = await ctx.send(f"> {prompt} -- react with the check mark to confirm, or the cross mark to cancel.")
    await msg.add_reaction("\u2705")
    await msg.add_reaction("\u274C")

    def check(reaction, user):
        return reaction.message.id == msg.id and user == ctx.author and str(reaction.emoji) in ("\u2705", "\u274C")

    try:
        reaction, _ = await bot.wait_for("reaction_add", timeout=timeout, check=check)
        try:
            await msg.delete()
        except Exception:
            pass
        return str(reaction.emoji) == "\u2705"
    except asyncio.TimeoutError:
        try:
            await msg.delete()
        except Exception:
            pass
        return False


def can_moderate(ctx: commands.Context, target: discord.Member) -> bool:
    if ctx.author == ctx.guild.owner:
        return True
    if target == ctx.guild.owner:
        return False
    if target.top_role >= ctx.author.top_role:
        return False
    if target.top_role >= ctx.guild.me.top_role:
        return False
    return True


def has_any_perm(ctx: commands.Context, *perms: str) -> bool:
    p = ctx.author.guild_permissions
    return any(getattr(p, name, False) for name in perms)


def tier_header(title: str) -> str:
    return f"\u2554\u2550\u2550\u2550\u2550\u2550 {title} \u2550\u2550\u2550\u2550\u2550\u2557"


HELP_CATEGORIES = [
    {
        "name": "Utility",
        "emoji": "\u2139",
        "commands": [
            ("ping", "Check bot latency"),
            ("serverinfo", "Info about this server"),
            ("userinfo", "Info about a user"),
            ("avatar", "Show a user's avatar"),
            ("banner", "Show a user's banner"),
            ("roleinfo", "Info about a role"),
            ("channelinfo", "Info about a channel"),
            ("uptime", "Show bot uptime"),
            ("help", "Show this menu"),
        ],
        "requires": None,
    },
    {
        "name": "Purge",
        "emoji": "\u232B",
        "commands": [
            ("purge <amount>", "Delete recent messages"),
            ("purgeuser <user> [amount]", "Delete messages from a user"),
            ("purgebots [amount]", "Delete bot messages"),
            ("purgecontains <text>", "Delete messages containing a string"),
            ("purgeembeds [amount]", "Delete messages with embeds"),
            ("purgeattachments [amount]", "Delete messages with attachments"),
        ],
        "requires": ("manage_messages",),
    },
    {
        "name": "Kick / Ban",
        "emoji": "\u26A0",
        "commands": [
            ("kick <member> [reason]", "Kick a member"),
            ("ban <member> [reason]", "Ban a member"),
            ("unban <user_id> [reason]", "Unban a user by ID"),
            ("softban <member> [reason]", "Ban then unban to purge messages"),
            ("tempban <member> <duration> [reason]", "Temporary ban (e.g. 1h)"),
            ("banlist", "List banned users"),
        ],
        "requires": ("kick_members", "ban_members"),
    },
    {
        "name": "Timeout / Warn",
        "emoji": "\u23F1",
        "commands": [
            ("timeout <member> <duration> [reason]", "Timeout a member"),
            ("untimeout <member>", "Remove a timeout"),
            ("warn <member> [reason]", "Warn a member"),
            ("warnings <member>", "List warnings for a member"),
            ("delwarn <member> <index>", "Delete a specific warning"),
            ("clearwarnings <member>", "Clear all warnings (admin)"),
        ],
        "requires": ("moderate_members",),
    },
    {
        "name": "Channel Management",
        "emoji": "\u2699",
        "commands": [
            ("slowmode <duration> [channel]", "Set slowmode (0 to disable)"),
            ("lock [channel]", "Lock a channel"),
            ("unlock [channel]", "Unlock a channel"),
            ("hide [channel]", "Hide a channel"),
            ("unhide [channel]", "Unhide a channel"),
            ("lockall", "Lock every text channel"),
            ("unlockall", "Unlock every text channel"),
            ("purgechannel [channel]", "Delete and recreate a channel"),
            ("nuke [channel]", "Alias for purgechannel"),
            ("topic <text>", "Set a channel topic"),
            ("rename <name> [channel]", "Rename a channel"),
        ],
        "requires": ("manage_channels",),
    },
    {
        "name": "Roles / Nicknames",
        "emoji": "\u2694",
        "commands": [
            ("nick <member> [nickname]", "Change a member's nickname"),
            ("resetnick <member>", "Reset a member's nickname"),
            ("addrole <member> <role>", "Add a role to a member"),
            ("removerole <member> <role>", "Remove a role from a member"),
            ("roleall <role> [include_bots]", "Give a role to every member"),
            ("rolecreate <name> [color]", "Create a role"),
            ("roledelete <role>", "Delete a role"),
            ("rolecolor <role> <color>", "Change a role's color"),
        ],
        "requires": ("manage_roles", "manage_nicknames"),
    },
    {
        "name": "Messaging / Misc",
        "emoji": "\u2709",
        "commands": [
            ("dm <member> <message>", "DM a member"),
            ("say <text>", "Make the bot say something"),
            ("embed <title> <description>", "Send a quick embed"),
            ("poll <question>", "Yes/no poll"),
            ("massdm <message>", "DM every human member (admin)"),
            ("raidmode", "Toggle raid mode (admin)"),
        ],
        "requires": ("manage_messages",),
    },
    {
        "name": "Moderation Tools",
        "emoji": "\U0001F6E1",
        "commands": [
            ("checkbg <user>", "Full background check on a member"),
            ("vhistory <user>", "Browse all log threads for a user"),
            ("mhistory <user>", "Paginated message history"),
            ("rhistory <user>", "Paginated deleted message history"),
            ("ehistory <user>", "Paginated edit history"),
            ("history <user>", "Full moderation action history"),
            ("note <user> <text>", "Add a private note on a member"),
            ("notes <user>", "List notes on a member"),
            ("delnote <user> <index>", "Delete a specific note"),
            ("clearnotes <user>", "Clear all notes (admin)"),
            ("watch <user> <reason>", "Add user to the watchlist"),
            ("unwatch <user>", "Remove user from watchlist"),
            ("watchlist", "List all watched users"),
            ("quickwarn <user> <reason>", "Warn and DM in one step"),
            ("noteach <user>", "Quick note that says 'watch this user'"),
            ("slogs", "Toggle message logging for this server"),
            ("purgeuserall <user>", "Purge user's messages in all channels (admin)"),
            ("masskick <user1> <user2> ...", "Kick multiple members (admin)"),
            ("massban <user1> <user2> ...", "Ban multiple members (admin)"),
            ("softbanall <user1> <user2> ...", "Softban multiple members (admin)"),
            ("lockuser <user> <channel>", "Deny send in a channel"),
            ("unlockuser <user> <channel>", "Restore send in a channel"),
            ("recentjoins [count]", "List newest members"),
            ("newaccounts [days]", "List members under N days old"),
            ("joinposition <user>", "Show join order position"),
            ("id <user>", "Show raw user ID"),
            ("created <user>", "Show account creation date"),
            ("joined <user>", "Show server join date"),
            ("rolecount <role>", "Count members with a role"),
            ("perms <user>", "Show a user's key permissions"),
        ],
        "requires": ("moderate_members",),
    },
]

POSTHELP_TIERS = [
    {
        "title": "OWNERS",
        "color": (220, 60, 60),
        "commands": [
            ("purge <amount>", "Delete recent messages"),
            ("purgeuser <user> [amount]", "Delete messages from a user"),
            ("purgebots [amount]", "Delete bot messages"),
            ("purgecontains <text>", "Delete messages containing a string"),
            ("purgeembeds [amount]", "Delete messages with embeds"),
            ("purgeattachments [amount]", "Delete messages with attachments"),
            ("kick <member> [reason]", "Kick a member"),
            ("ban <member> [reason]", "Ban a member"),
            ("unban <user_id> [reason]", "Unban a user by ID"),
            ("softban <member> [reason]", "Ban then unban to purge messages"),
            ("tempban <member> <duration> [reason]", "Temporary ban (e.g. 1h)"),
            ("banlist", "List banned users"),
            ("timeout <member> <duration> [reason]", "Timeout a member"),
            ("untimeout <member>", "Remove a timeout"),
            ("warn <member> [reason]", "Warn a member"),
            ("warnings <member>", "List warnings for a member"),
            ("delwarn <member> <index>", "Delete a specific warning"),
            ("clearwarnings <member>", "Clear all warnings"),
            ("slowmode <duration> [channel]", "Set slowmode (0 to disable)"),
            ("lock [channel]", "Lock a channel"),
            ("unlock [channel]", "Unlock a channel"),
            ("hide [channel]", "Hide a channel"),
            ("unhide [channel]", "Unhide a channel"),
            ("lockall", "Lock every text channel"),
            ("unlockall", "Unlock every text channel"),
            ("purgechannel [channel]", "Delete and recreate a channel"),
            ("nuke [channel]", "Alias for purgechannel"),
            ("topic <text>", "Set a channel topic"),
            ("rename <name> [channel]", "Rename a channel"),
            ("nick <member> [nickname]", "Change a member's nickname"),
            ("resetnick <member>", "Reset a member's nickname"),
            ("addrole <member> <role>", "Add a role to a member"),
            ("removerole <member> <role>", "Remove a role from a member"),
            ("roleall <role> [include_bots]", "Give a role to every member"),
            ("rolecreate <name> [color]", "Create a role"),
            ("roledelete <role>", "Delete a role"),
            ("rolecolor <role> <color>", "Change a role's color"),
            ("dm <member> <message>", "DM a member"),
            ("say <text>", "Make the bot say something"),
            ("embed <title> <description>", "Send a quick embed"),
            ("poll <question>", "Yes/no poll"),
            ("massdm <message>", "DM every human member"),
            ("raidmode", "Toggle raid mode"),
            ("posthelp <channel_id>", "Publish the tiered command list"),
            ("serverinfo", "Info about this server"),
            ("userinfo", "Info about a user"),
            ("channelinfo", "Info about a channel"),
            ("roleinfo", "Info about a role"),
            ("slogs", "Toggle message logging"),
            ("checkbg <user>", "Full background check"),
            ("vhistory <user>", "Browse log threads"),
            ("mhistory <user>", "Message history"),
            ("rhistory <user>", "Deleted message history"),
            ("ehistory <user>", "Edit history"),
            ("history <user>", "Moderation history"),
            ("note <user> <text>", "Add a note"),
            ("notes <user>", "List notes"),
            ("delnote <user> <index>", "Delete a note"),
            ("clearnotes <user>", "Clear notes"),
            ("watch <user> <reason>", "Add to watchlist"),
            ("unwatch <user>", "Remove from watchlist"),
            ("watchlist", "List watchlist"),
            ("quickwarn <user> <reason>", "Warn + DM"),
            ("purgeuserall <user>", "Purge everywhere"),
            ("masskick <users...>", "Kick multiple"),
            ("massban <users...>", "Ban multiple"),
            ("softbanall <users...>", "Softban multiple"),
            ("lockuser <user> <channel>", "Deny send"),
            ("unlockuser <user> <channel>", "Restore send"),
            ("recentjoins [count]", "Newest members"),
            ("newaccounts [days]", "Newest accounts"),
            ("joinposition <user>", "Join position"),
            ("id <user>", "Raw user ID"),
            ("created <user>", "Account creation"),
            ("joined <user>", "Server join"),
            ("rolecount <role>", "Count with role"),
            ("perms <user>", "Key permissions"),
        ],
    },
    {
        "title": "ADMINS",
        "color": (230, 120, 40),
        "commands": [
            ("purge <amount>", "Delete recent messages"),
            ("purgeuser <user> [amount]", "Delete messages from a user"),
            ("purgebots [amount]", "Delete bot messages"),
            ("purgecontains <text>", "Delete messages containing a string"),
            ("purgeembeds [amount]", "Delete messages with embeds"),
            ("purgeattachments [amount]", "Delete messages with attachments"),
            ("kick <member> [reason]", "Kick a member"),
            ("ban <member> [reason]", "Ban a member"),
            ("unban <user_id> [reason]", "Unban a user by ID"),
            ("softban <member> [reason]", "Ban then unban to purge messages"),
            ("tempban <member> <duration> [reason]", "Temporary ban (e.g. 1h)"),
            ("banlist", "List banned users"),
            ("timeout <member> <duration> [reason]", "Timeout a member"),
            ("untimeout <member>", "Remove a timeout"),
            ("warn <member> [reason]", "Warn a member"),
            ("warnings <member>", "List warnings for a member"),
            ("delwarn <member> <index>", "Delete a specific warning"),
            ("clearwarnings <member>", "Clear all warnings"),
            ("slowmode <duration> [channel]", "Set slowmode (0 to disable)"),
            ("lock [channel]", "Lock a channel"),
            ("unlock [channel]", "Unlock a channel"),
            ("hide [channel]", "Hide a channel"),
            ("unhide [channel]", "Unhide a channel"),
            ("lockall", "Lock every text channel"),
            ("unlockall", "Unlock every text channel"),
            ("purgechannel [channel]", "Delete and recreate a channel"),
            ("nuke [channel]", "Alias for purgechannel"),
            ("topic <text>", "Set a channel topic"),
            ("rename <name> [channel]", "Rename a channel"),
            ("nick <member> [nickname]", "Change a member's nickname"),
            ("resetnick <member>", "Reset a member's nickname"),
            ("addrole <member> <role>", "Add a role to a member"),
            ("removerole <member> <role>", "Remove a role from a member"),
            ("rolecreate <name> [color]", "Create a role"),
            ("roledelete <role>", "Delete a role"),
            ("rolecolor <role> <color>", "Change a role's color"),
            ("dm <member> <message>", "DM a member"),
            ("say <text>", "Make the bot say something"),
            ("embed <title> <description>", "Send a quick embed"),
            ("poll <question>", "Yes/no poll"),
            ("raidmode", "Toggle raid mode"),
            ("serverinfo", "Info about this server"),
            ("userinfo", "Info about a user"),
            ("channelinfo", "Info about a channel"),
            ("roleinfo", "Info about a role"),
            ("slogs", "Toggle message logging"),
            ("checkbg <user>", "Full background check"),
            ("vhistory <user>", "Browse log threads"),
            ("mhistory <user>", "Message history"),
            ("rhistory <user>", "Deleted message history"),
            ("ehistory <user>", "Edit history"),
            ("history <user>", "Moderation history"),
            ("note <user> <text>", "Add a note"),
            ("notes <user>", "List notes"),
            ("delnote <user> <index>", "Delete a note"),
            ("clearnotes <user>", "Clear notes"),
            ("watch <user> <reason>", "Add to watchlist"),
            ("unwatch <user>", "Remove from watchlist"),
            ("watchlist", "List watchlist"),
            ("quickwarn <user> <reason>", "Warn + DM"),
            ("purgeuserall <user>", "Purge everywhere"),
            ("masskick <users...>", "Kick multiple"),
            ("massban <users...>", "Ban multiple"),
            ("softbanall <users...>", "Softban multiple"),
            ("lockuser <user> <channel>", "Deny send"),
            ("unlockuser <user> <channel>", "Restore send"),
            ("recentjoins [count]", "Newest members"),
            ("newaccounts [days]", "Newest accounts"),
            ("joinposition <user>", "Join position"),
            ("id <user>", "Raw user ID"),
            ("created <user>", "Account creation"),
            ("joined <user>", "Server join"),
            ("rolecount <role>", "Count with role"),
            ("perms <user>", "Key permissions"),
        ],
    },
    {
        "title": "MODERATORS",
        "color": (230, 200, 40),
        "commands": [
            ("purge <amount>", "Delete recent messages"),
            ("purgeuser <user> [amount]", "Delete messages from a user"),
            ("purgebots [amount]", "Delete bot messages"),
            ("purgecontains <text>", "Delete messages containing a string"),
            ("purgeembeds [amount]", "Delete messages with embeds"),
            ("purgeattachments [amount]", "Delete messages with attachments"),
            ("kick <member> [reason]", "Kick a member"),
            ("ban <member> [reason]", "Ban a member"),
            ("unban <user_id> [reason]", "Unban a user by ID"),
            ("softban <member> [reason]", "Ban then unban to purge messages"),
            ("tempban <member> <duration> [reason]", "Temporary ban (e.g. 1h)"),
            ("banlist", "List banned users"),
            ("timeout <member> <duration> [reason]", "Timeout a member"),
            ("untimeout <member>", "Remove a timeout"),
            ("warn <member> [reason]", "Warn a member"),
            ("warnings <member>", "List warnings for a member"),
            ("delwarn <member> <index>", "Delete a specific warning"),
            ("slowmode <duration> [channel]", "Set slowmode (0 to disable)"),
            ("lock [channel]", "Lock a channel"),
            ("unlock [channel]", "Unlock a channel"),
            ("hide [channel]", "Hide a channel"),
            ("unhide [channel]", "Unhide a channel"),
            ("purgechannel [channel]", "Delete and recreate a channel"),
            ("nuke [channel]", "Alias for purgechannel"),
            ("topic <text>", "Set a channel topic"),
            ("rename <name> [channel]", "Rename a channel"),
            ("nick <member> [nickname]", "Change a member's nickname"),
            ("resetnick <member>", "Reset a member's nickname"),
            ("addrole <member> <role>", "Add a role to a member"),
            ("removerole <member> <role>", "Remove a role from a member"),
            ("dm <member> <message>", "DM a member"),
            ("say <text>", "Make the bot say something"),
            ("embed <title> <description>", "Send a quick embed"),
            ("poll <question>", "Yes/no poll"),
            ("serverinfo", "Info about this server"),
            ("userinfo", "Info about a user"),
            ("channelinfo", "Info about a channel"),
            ("roleinfo", "Info about a role"),
            ("checkbg <user>", "Full background check"),
            ("vhistory <user>", "Browse log threads"),
            ("mhistory <user>", "Message history"),
            ("rhistory <user>", "Deleted message history"),
            ("ehistory <user>", "Edit history"),
            ("history <user>", "Moderation history"),
            ("note <user> <text>", "Add a note"),
            ("notes <user>", "List notes"),
            ("delnote <user> <index>", "Delete a note"),
            ("watch <user> <reason>", "Add to watchlist"),
            ("unwatch <user>", "Remove from watchlist"),
            ("watchlist", "List watchlist"),
            ("quickwarn <user> <reason>", "Warn + DM"),
            ("lockuser <user> <channel>", "Deny send"),
            ("unlockuser <user> <channel>", "Restore send"),
            ("recentjoins [count]", "Newest members"),
            ("newaccounts [days]", "Newest accounts"),
            ("joinposition <user>", "Join position"),
            ("id <user>", "Raw user ID"),
            ("created <user>", "Account creation"),
            ("joined <user>", "Server join"),
            ("rolecount <role>", "Count with role"),
            ("perms <user>", "Key permissions"),
        ],
    },
    {
        "title": "TRIAL MODS",
        "color": (120, 200, 120),
        "commands": [
            ("purge <amount>", "Delete recent messages"),
            ("purgeuser <user> [amount]", "Delete messages from a user"),
            ("purgebots [amount]", "Delete bot messages"),
            ("purgeembeds [amount]", "Delete messages with embeds"),
            ("timeout <member> <duration> [reason]", "Timeout a member"),
            ("untimeout <member>", "Remove a timeout"),
            ("warn <member> [reason]", "Warn a member"),
            ("warnings <member>", "List warnings for a member"),
            ("slowmode <duration> [channel]", "Set slowmode (0 to disable)"),
            ("lock [channel]", "Lock a channel"),
            ("unlock [channel]", "Unlock a channel"),
            ("hide [channel]", "Hide a channel"),
            ("unhide [channel]", "Unhide a channel"),
            ("nick <member> [nickname]", "Change a member's nickname"),
            ("say <text>", "Make the bot say something"),
            ("serverinfo", "Info about this server"),
            ("userinfo", "Info about a user"),
            ("channelinfo", "Info about a channel"),
            ("roleinfo", "Info about a role"),
            ("checkbg <user>", "Full background check"),
            ("vhistory <user>", "Browse log threads"),
            ("mhistory <user>", "Message history"),
            ("rhistory <user>", "Deleted message history"),
            ("ehistory <user>", "Edit history"),
            ("history <user>", "Moderation history"),
            ("notes <user>", "List notes"),
            ("quickwarn <user> <reason>", "Warn + DM"),
            ("id <user>", "Raw user ID"),
            ("created <user>", "Account creation"),
            ("joined <user>", "Server join"),
            ("joinposition <user>", "Join position"),
        ],
    },
    {
        "title": "STAFF",
        "color": (120, 160, 230),
        "commands": [
            ("purge <amount>", "Delete recent messages"),
            ("purgebots [amount]", "Delete bot messages"),
            ("warn <member> [reason]", "Warn a member"),
            ("warnings <member>", "List warnings for a member"),
            ("slowmode <duration> [channel]", "Set slowmode (0 to disable)"),
            ("lock [channel]", "Lock a channel"),
            ("unlock [channel]", "Unlock a channel"),
            ("say <text>", "Make the bot say something"),
            ("poll <question>", "Yes/no poll"),
            ("serverinfo", "Info about this server"),
            ("userinfo", "Info about a user"),
            ("channelinfo", "Info about a channel"),
            ("roleinfo", "Info about a role"),
            ("checkbg <user>", "Full background check"),
            ("vhistory <user>", "Browse log threads"),
            ("mhistory <user>", "Message history"),
            ("history <user>", "Moderation history"),
            ("id <user>", "Raw user ID"),
        ],
    },
    {
        "title": "HELPERS",
        "color": (170, 130, 220),
        "commands": [
            ("warn <member> [reason]", "Warn a member"),
            ("warnings <member>", "List warnings for a member"),
            ("say <text>", "Make the bot say something"),
            ("poll <question>", "Yes/no poll"),
            ("serverinfo", "Info about this server"),
            ("userinfo", "Info about a user"),
            ("channelinfo", "Info about a channel"),
            ("roleinfo", "Info about a role"),
            ("id <user>", "Raw user ID"),
            ("joined <user>", "Server join"),
        ],
    },
    {
        "title": "MEMBERS",
        "color": (130, 130, 140),
        "commands": [
            ("ping", "Check bot latency"),
            ("serverinfo", "Info about this server"),
            ("userinfo", "Info about a user"),
            ("avatar", "Show a user's avatar"),
            ("banner", "Show a user's banner"),
            ("roleinfo", "Info about a role"),
            ("channelinfo", "Info about a channel"),
            ("uptime", "Show bot uptime"),
            ("help", "Show your personal command list"),
            ("roll", "Roll dice (e.g. 2d20)"),
            ("coinflip", "Flip a coin"),
            ("8ball", "Ask the magic 8-ball"),
        ],
    },
]


def build_help_embed(ctx: commands.Context) -> discord.Embed:
    embed = discord.Embed(
        title="Command List",
        description=f"Prefix: `>`. Showing commands available to **{ctx.author.display_name}**.",
        color=discord.Color.blurple(),
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)

    accessible = 0
    for cat in HELP_CATEGORIES:
        req = cat["requires"]
        if req is not None and not has_any_perm(ctx, *req):
            continue
        lines = [f"`>{cmd}` -- {desc}" for cmd, desc in cat["commands"]]
        embed.add_field(
            name=f"{cat['emoji']} {cat['name']}",
            value="\n".join(lines)[:1020],
            inline=False,
        )
        accessible += 1

    if accessible == 0:
        embed.add_field(name="No commands", value="You don't have access to any commands.", inline=False)

    embed.set_footer(text="This message is only visible to you.")
    return embed


@bot.command(name="help")
@commands.cooldown(rate=1, per=30, type=commands.BucketType.user)
async def help_cmd(ctx: commands.Context):
    embed = build_help_embed(ctx)
    await try_delete(ctx)
    try:
        await ctx.send(embed=embed, delete_after=30)
    except Exception:
        await ctx.send(embed=embed)


def build_posthelp_embeds() -> list[discord.Embed]:
    embeds = []

    intro = discord.Embed(
        title="\u2554\u2550\u2550\u2550\u2550\u2550\u2550 COMMAND REFERENCE \u2550\u2550\u2550\u2550\u2550\u2550\u2557",
        description=(
            "The full command list, grouped by tier.\n"
            "Find your highest role below and use the commands listed for it.\n"
            "Higher tiers include everything from the tiers below them."
        ),
        color=discord.Color.from_rgb(90, 100, 240),
    )
    if bot.user:
        intro.set_thumbnail(url=bot.user.display_avatar.url)
    embeds.append(intro)

    for tier in POSTHELP_TIERS:
        lines = []
        for cmd, desc in tier["commands"]:
            lines.append(f"\u00BB `>{cmd}`\n\u2003\u2003\u2022 *{desc}*")
        chunk = "\n".join(lines)
        if len(chunk) > 4000:
            chunk = chunk[:4000]

        embed = discord.Embed(
            title=tier_header(tier["title"]),
            description=chunk,
            color=discord.Color.from_rgb(*tier["color"]),
        )
        embeds.append(embed)
    return embeds


def is_posthelp_authorized(ctx: commands.Context) -> bool:
    if not isinstance(ctx.author, discord.Member):
        return False
    if ctx.author.id == ctx.guild.owner_id:
        return True
    return any(r.id == POSTHELP_ROLE_ID for r in ctx.author.roles)


@bot.command(name="posthelp")
async def posthelp(ctx: commands.Context, channel_id: int):
    if not is_posthelp_authorized(ctx):
        await try_delete(ctx)
        return
    target = ctx.guild.get_channel(channel_id)
    if target is None:
        try:
            target = await bot.fetch_channel(channel_id)
        except Exception:
            return await ctx.send("[X] I can't find that channel.")
    if not isinstance(target, discord.TextChannel):
        return await ctx.send("[X] That channel isn't a text channel.")
    await try_delete(ctx)

    embeds = build_posthelp_embeds()
    for emb in embeds:
        try:
            await target.send(embed=emb)
        except Exception as e:
            return await ctx.send(f"[X] Failed to send: {e}")

    try:
        await ctx.send(f"> Posted the command reference in {target.mention}.", delete_after=8)
    except Exception:
        pass


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    latency = 0 if math.isnan(bot.latency) else round(bot.latency * 1000)
    await ctx.send(f"> Latency: **{latency}ms**")


@bot.command(name="serverinfo")
async def serverinfo(ctx: commands.Context):
    g = ctx.guild
    embed = discord.Embed(title=g.name, color=discord.Color.blurple())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Owner", value=g.owner.mention if g.owner else "Unknown")
    embed.add_field(name="ID", value=str(g.id))
    embed.add_field(name="Members", value=str(g.member_count))
    embed.add_field(name="Channels", value=str(len(g.channels)))
    embed.add_field(name="Roles", value=str(len(g.roles)))
    embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
    await ctx.send(embed=embed)


@bot.command(name="userinfo")
async def userinfo(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=str(member), color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=str(member.id))
    embed.add_field(name="Bot", value=str(member.bot))
    embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "?")
    embed.add_field(name="Created", value=discord.utils.format_dt(member.created_at, "R"))
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    embed.add_field(name="Roles", value=", ".join(roles[:10]) or "None", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="avatar")
async def avatar(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(member.display_avatar.url)


@bot.command(name="banner")
async def banner(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    user = await bot.fetch_user(member.id)
    if not user.banner:
        return await ctx.send("> [!] That user has no banner.")
    await ctx.send(user.banner.url)


@bot.command(name="roleinfo")
async def roleinfo(ctx: commands.Context, *, role: discord.Role):
    embed = discord.Embed(title=role.name, color=role.color)
    embed.add_field(name="ID", value=str(role.id))
    embed.add_field(name="Color", value=str(role.color))
    embed.add_field(name="Members", value=str(len(role.members)))
    embed.add_field(name="Mentionable", value=str(role.mentionable))
    embed.add_field(name="Hoisted", value=str(role.hoist))
    embed.add_field(name="Position", value=str(role.position))
    await ctx.send(embed=embed)


@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True)
async def purge(ctx: commands.Context, amount: int = 10):
    if amount < 1 or amount > 1000:
        await try_delete(ctx)
        return await ctx.send("[X] Amount must be 1-1000.", delete_after=5)
    await try_delete(ctx)
    deleted = await ctx.channel.purge(limit=amount)
    msg = await ctx.send(f"> Purged **{len(deleted)}** message(s).")
    await asyncio.sleep(3)
    try:
        await msg.delete()
    except Exception:
        pass


@bot.command(name="purgeuser")
@commands.has_permissions(manage_messages=True)
async def purgeuser(ctx: commands.Context, user: discord.Member, amount: int = 50):
    await try_delete(ctx)
    deleted = await ctx.channel.purge(limit=amount, check=lambda m: m.author == user)
    await ctx.send(f"> Purged **{len(deleted)}** message(s) from {user.mention}.", delete_after=5)


@bot.command(name="purgebots")
@commands.has_permissions(manage_messages=True)
async def purgebots(ctx: commands.Context, amount: int = 50):
    await try_delete(ctx)
    deleted = await ctx.channel.purge(limit=amount, check=lambda m: m.author.bot)
    await ctx.send(f"> Purged **{len(deleted)}** bot message(s).", delete_after=5)


@bot.command(name="purgecontains")
@commands.has_permissions(manage_messages=True)
async def purgecontains(ctx: commands.Context, *, substring: str):
    await try_delete(ctx)
    deleted = await ctx.channel.purge(limit=200, check=lambda m: substring.lower() in m.content.lower())
    await ctx.send(f"> Purged **{len(deleted)}** matching message(s).", delete_after=5)


@bot.command(name="purgeembeds")
@commands.has_permissions(manage_messages=True)
async def purgeembeds(ctx: commands.Context, amount: int = 50):
    await try_delete(ctx)
    deleted = await ctx.channel.purge(limit=amount, check=lambda m: bool(m.embeds))
    await ctx.send(f"> Purged **{len(deleted)}** embed message(s).", delete_after=5)


@bot.command(name="purgeattachments")
@commands.has_permissions(manage_messages=True)
async def purgeattachments(ctx: commands.Context, amount: int = 50):
    await try_delete(ctx)
    deleted = await ctx.channel.purge(limit=amount, check=lambda m: bool(m.attachments))
    await ctx.send(f"> Purged **{len(deleted)}** attachment message(s).", delete_after=5)


@bot.command(name="purgeuserall")
@commands.has_permissions(administrator=True)
async def purgeuserall(ctx: commands.Context, user: discord.Member):
    await try_delete(ctx)
    if not await confirm(ctx, f"Purge **{user}**'s messages across every text channel?"):
        return await ctx.send("> Cancelled.")
    total = 0
    async with ctx.typing():
        for ch in ctx.guild.text_channels:
            try:
                deleted = await ch.purge(limit=200, check=lambda m: m.author == user)
                total += len(deleted)
            except Exception:
                pass
    await ctx.send(f"> Purged **{total}** message(s) from {user.mention} across all channels.")


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    await try_delete(ctx)
    if not can_moderate(ctx, member):
        return await ctx.send("[X] You cannot moderate this member (role hierarchy).", delete_after=5)
    await member.kick(reason=reason)
    history_tracker.record(ctx.guild.id, member.id, "kick", ctx.author.id, reason)
    await safe_refresh_verification_log(bot, ctx.guild, member.id)
    await ctx.send(f"> Kicked **{member}** -- {reason}")


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    await try_delete(ctx)
    if not can_moderate(ctx, member):
        return await ctx.send("[X] You cannot moderate this member (role hierarchy).", delete_after=5)
    await member.ban(reason=reason)
    history_tracker.record(ctx.guild.id, member.id, "ban", ctx.author.id, reason)
    await safe_refresh_verification_log(bot, ctx.guild, member.id)
    await ctx.send(f"> Banned **{member}** -- {reason}")


@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user_id: int, *, reason: str = "No reason provided"):
    await try_delete(ctx)
    try:
        user = discord.Object(id=user_id)
        await ctx.guild.unban(user, reason=reason)
        await ctx.send(f"> Unbanned `{user_id}` -- {reason}")
    except discord.NotFound:
        await ctx.send("[X] That user isn't banned.", delete_after=5)


@bot.command(name="softban")
@commands.has_permissions(ban_members=True)
async def softban(ctx: commands.Context, member: discord.Member, *, reason: str = "Softban"):
    await try_delete(ctx)
    if not can_moderate(ctx, member):
        return await ctx.send("[X] You cannot moderate this member.", delete_after=5)
    await member.ban(reason=reason, delete_message_days=7)
    await ctx.guild.unban(discord.Object(id=member.id))
    history_tracker.record(ctx.guild.id, member.id, "softban", ctx.author.id, reason)
    await safe_refresh_verification_log(bot, ctx.guild, member.id)
    await ctx.send(f"> Softbanned **{member}** (messages purged).")


@bot.command(name="tempban")
@commands.has_permissions(ban_members=True)
async def tempban(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason"):
    await try_delete(ctx)
    try:
        secs = parse_duration(duration)
    except ValueError:
        return await ctx.send("[X] Invalid duration. Use `10m`, `2h`, `1d`.", delete_after=5)
    if not can_moderate(ctx, member):
        return await ctx.send("[X] You cannot moderate this member.", delete_after=5)
    await member.ban(reason=f"[{duration}] {reason}")
    history_tracker.record(ctx.guild.id, member.id, "ban", ctx.author.id, f"[tempban {duration}] {reason}")
    await safe_refresh_verification_log(bot, ctx.guild, member.id)
    await ctx.send(f"> Temp-banned **{member}** for {human_delta(secs)}.")
    await asyncio.sleep(secs)
    try:
        await ctx.guild.unban(discord.Object(id=member.id), reason="Tempban expired")
        await ctx.send(f"> Tempban expired for **{member}**.")
    except Exception:
        pass


@bot.command(name="banlist")
@commands.has_permissions(ban_members=True)
async def banlist(ctx: commands.Context):
    await try_delete(ctx)
    bans = [entry async for entry in ctx.guild.bans(limit=100)]
    if not bans:
        return await ctx.send("> No bans on record.", delete_after=10)
    lines = [f"`{e.user.id}` -- {e.user} ({e.reason or 'no reason'})" for e in bans]
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n... truncated"
    await ctx.send(f"> Bans ({len(bans)}):\n{text}")


@bot.command(name="masskick")
@commands.has_permissions(administrator=True)
async def masskick(ctx: commands.Context, members: commands.Greedy[discord.Member], *, reason: str = "Mass kick"):
    await try_delete(ctx)
    if not members:
        return await ctx.send("[X] Provide at least one member.", delete_after=5)
    if not await confirm(ctx, f"Kick **{len(members)}** member(s)?"):
        return await ctx.send("> Cancelled.")
    n = 0
    for m in members:
        if not can_moderate(ctx, m):
            continue
        try:
            await m.kick(reason=reason)
            history_tracker.record(ctx.guild.id, m.id, "kick", ctx.author.id, reason)
            await safe_refresh_verification_log(bot, ctx.guild, m.id)
            n += 1
        except Exception:
            pass
    await ctx.send(f"> Kicked **{n}** member(s).")


@bot.command(name="massban")
@commands.has_permissions(administrator=True)
async def massban(ctx: commands.Context, members: commands.Greedy[discord.Member], *, reason: str = "Mass ban"):
    await try_delete(ctx)
    if not members:
        return await ctx.send("[X] Provide at least one member.", delete_after=5)
    if not await confirm(ctx, f"Ban **{len(members)}** member(s)?"):
        return await ctx.send("> Cancelled.")
    n = 0
    for m in members:
        if not can_moderate(ctx, m):
            continue
        try:
            await m.ban(reason=reason)
            history_tracker.record(ctx.guild.id, m.id, "ban", ctx.author.id, reason)
            await safe_refresh_verification_log(bot, ctx.guild, m.id)
            n += 1
        except Exception:
            pass
    await ctx.send(f"> Banned **{n}** member(s).")


@bot.command(name="softbanall")
@commands.has_permissions(administrator=True)
async def softbanall(ctx: commands.Context, members: commands.Greedy[discord.Member], *, reason: str = "Mass softban"):
    await try_delete(ctx)
    if not members:
        return await ctx.send("[X] Provide at least one member.", delete_after=5)
    if not await confirm(ctx, f"Softban **{len(members)}** member(s)?"):
        return await ctx.send("> Cancelled.")
    n = 0
    for m in members:
        if not can_moderate(ctx, m):
            continue
        try:
            await m.ban(reason=reason, delete_message_days=7)
            await ctx.guild.unban(discord.Object(id=m.id))
            history_tracker.record(ctx.guild.id, m.id, "softban", ctx.author.id, reason)
            await safe_refresh_verification_log(bot, ctx.guild, m.id)
            n += 1
        except Exception:
            pass
    await ctx.send(f"> Softbanned **{n}** member(s).")


@bot.command(name="timeout")
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def timeout(ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason"):
    await try_delete(ctx)
    try:
        secs = parse_duration(duration)
    except ValueError:
        return await ctx.send("[X] Invalid duration. Use `10m`, `2h`, `1d`.", delete_after=5)
    if not can_moderate(ctx, member):
        return await ctx.send("[X] You cannot moderate this member.", delete_after=5)
    until = discord.utils.utcnow() + datetime.timedelta(seconds=secs)
    await member.timeout(until, reason=reason)
    history_tracker.record(ctx.guild.id, member.id, "timeout", ctx.author.id, reason)
    await safe_refresh_verification_log(bot, ctx.guild, member.id)
    await ctx.send(f"> Timed out **{member}** for {human_delta(secs)} -- {reason}")


@bot.command(name="untimeout")
@commands.has_permissions(moderate_members=True)
async def untimeout(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    await member.timeout(None)
    await ctx.send(f"> Timeout removed for **{member}**.")


WARNS: dict[int, dict[int, list[dict]]] = {}


@bot.command(name="warn")
@commands.has_permissions(moderate_members=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason"):
    await try_delete(ctx)
    if not can_moderate(ctx, member):
        return await ctx.send("[X] You cannot warn this member.", delete_after=5)
    entry = {
        "reason": reason,
        "moderator": ctx.author.id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }
    WARNS.setdefault(ctx.guild.id, {}).setdefault(member.id, []).append(entry)
    history_tracker.record(ctx.guild.id, member.id, "warn", ctx.author.id, reason)
    await safe_refresh_verification_log(bot, ctx.guild, member.id)
    count = len(WARNS[ctx.guild.id][member.id])
    await ctx.send(f"> Warned **{member}** ({count} total) -- {reason}")
    try:
        await member.send(f"> You were warned in **{ctx.guild.name}**: {reason}")
    except Exception:
        pass


@bot.command(name="warnings")
@commands.has_permissions(moderate_members=True)
async def warnings(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    entries = WARNS.get(ctx.guild.id, {}).get(member.id, [])
    if not entries:
        return await ctx.send(f"> **{member}** has no warnings.")
    lines = [f"`{i+1}.` {e['reason']} -- <@{e['moderator']}> ({e['timestamp'][:19]})" for i, e in enumerate(entries)]
    await ctx.send(f"> Warnings for **{member}** ({len(entries)}):\n" + "\n".join(lines))


@bot.command(name="clearwarnings")
@commands.has_permissions(administrator=True)
async def clearwarnings(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    WARNS.get(ctx.guild.id, {}).pop(member.id, None)
    await ctx.send(f"> Cleared warnings for **{member}**.")


@bot.command(name="delwarn")
@commands.has_permissions(moderate_members=True)
async def delwarn(ctx: commands.Context, member: discord.Member, index: int):
    await try_delete(ctx)
    entries = WARNS.get(ctx.guild.id, {}).get(member.id, [])
    if index < 1 or index > len(entries):
        return await ctx.send("[X] Invalid warning index.", delete_after=5)
    removed = entries.pop(index - 1)
    await ctx.send(f"> Removed warning {index} for **{member}** -- {removed['reason']}")


@bot.command(name="quickwarn")
@commands.has_permissions(moderate_members=True)
async def quickwarn(ctx: commands.Context, member: discord.Member, *, reason: str):
    await try_delete(ctx)
    if not can_moderate(ctx, member):
        return await ctx.send("[X] You cannot warn this member.", delete_after=5)
    entry = {
        "reason": reason,
        "moderator": ctx.author.id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }
    WARNS.setdefault(ctx.guild.id, {}).setdefault(member.id, []).append(entry)
    history_tracker.record(ctx.guild.id, member.id, "warn", ctx.author.id, reason)
    await safe_refresh_verification_log(bot, ctx.guild, member.id)
    count = len(WARNS[ctx.guild.id][member.id])
    await ctx.send(f"> Quick-warned **{member}** ({count} total) -- {reason}")
    try:
        await member.send(
            f"> You have been warned in **{ctx.guild.name}** by a staff member.\n"
            f"> Reason: {reason}\n"
            f"> This is warning #{count} on your record."
        )
    except Exception:
        pass


@bot.command(name="slowmode")
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx: commands.Context, duration: str, channel: discord.TextChannel = None):
    await try_delete(ctx)
    channel = channel or ctx.channel
    if duration == "0":
        secs = 0
    else:
        try:
            secs = parse_duration(duration)
        except ValueError:
            return await ctx.send("[X] Invalid duration. Use `10s`, `1m`, `0` to disable.", delete_after=5)
    if secs > 21600:
        return await ctx.send("[X] Max slowmode is 6 hours (21600s).", delete_after=5)
    await channel.edit(slowmode_delay=secs)
    if secs == 0:
        await ctx.send(f"> Slowmode disabled in {channel.mention}.")
    else:
        await ctx.send(f"> Slowmode set to {human_delta(secs)} in {channel.mention}.")


@bot.command(name="lock")
@commands.has_permissions(manage_channels=True)
async def lock(ctx: commands.Context, channel: discord.TextChannel = None):
    await try_delete(ctx)
    channel = channel or ctx.channel
    ow = channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = False
    await channel.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f"> Locked {channel.mention}.")


@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context, channel: discord.TextChannel = None):
    await try_delete(ctx)
    channel = channel or ctx.channel
    ow = channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = None
    await channel.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f"> Unlocked {channel.mention}.")


@bot.command(name="lockuser")
@commands.has_permissions(moderate_members=True)
async def lockuser(ctx: commands.Context, member: discord.Member, channel: discord.TextChannel = None):
    await try_delete(ctx)
    channel = channel or ctx.channel
    ow = channel.overwrites_for(member)
    ow.send_messages = False
    await channel.set_permissions(member, overwrite=ow)
    await ctx.send(f"> Locked {member.mention} out of {channel.mention}.")


@bot.command(name="unlockuser")
@commands.has_permissions(moderate_members=True)
async def unlockuser(ctx: commands.Context, member: discord.Member, channel: discord.TextChannel = None):
    await try_delete(ctx)
    channel = channel or ctx.channel
    ow = channel.overwrites_for(member)
    ow.send_messages = None
    await channel.set_permissions(member, overwrite=ow)
    await ctx.send(f"> Restored send access for {member.mention} in {channel.mention}.")


@bot.command(name="hide")
@commands.has_permissions(manage_channels=True)
async def hide(ctx: commands.Context, channel: discord.TextChannel = None):
    await try_delete(ctx)
    channel = channel or ctx.channel
    ow = channel.overwrites_for(ctx.guild.default_role)
    ow.view_channel = False
    await channel.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f"> Hid {channel.mention}.")


@bot.command(name="unhide")
@commands.has_permissions(manage_channels=True)
async def unhide(ctx: commands.Context, channel: discord.TextChannel = None):
    await try_delete(ctx)
    channel = channel or ctx.channel
    ow = channel.overwrites_for(ctx.guild.default_role)
    ow.view_channel = None
    await channel.set_permissions(ctx.guild.default_role, overwrite=ow)
    await ctx.send(f"> Unhid {channel.mention}.")


@bot.command(name="lockall")
@commands.has_permissions(administrator=True)
async def lockall(ctx: commands.Context):
    await try_delete(ctx)
    if not await confirm(ctx, f"This will lock every text channel in {ctx.guild.name}. Continue?"):
        return await ctx.send("> Cancelled.")
    n = 0
    for ch in ctx.guild.text_channels:
        try:
            ow = ch.overwrites_for(ctx.guild.default_role)
            ow.send_messages = False
            await ch.set_permissions(ctx.guild.default_role, overwrite=ow)
            n += 1
        except Exception:
            pass
    await ctx.send(f"> Locked {n} channel(s).")


@bot.command(name="unlockall")
@commands.has_permissions(administrator=True)
async def unlockall(ctx: commands.Context):
    await try_delete(ctx)
    n = 0
    for ch in ctx.guild.text_channels:
        try:
            ow = ch.overwrites_for(ctx.guild.default_role)
            ow.send_messages = None
            await ch.set_permissions(ctx.guild.default_role, overwrite=ow)
            n += 1
        except Exception:
            pass
    await ctx.send(f"> Unlocked {n} channel(s).")


@bot.command(name="nick")
@commands.has_permissions(manage_nicknames=True)
async def nick(ctx: commands.Context, member: discord.Member, *, nickname: str = None):
    await try_delete(ctx)
    if not can_moderate(ctx, member):
        return await ctx.send("[X] You cannot rename this member.", delete_after=5)
    await member.edit(nick=nickname)
    if nickname:
        await ctx.send(f"> Renamed **{member}** to `{nickname}`.")
    else:
        await ctx.send(f"> Reset nickname for **{member}**.")


@bot.command(name="resetnick")
@commands.has_permissions(manage_nicknames=True)
async def resetnick(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    await member.edit(nick=None)
    await ctx.send(f"> Reset nickname for **{member}**.")


@bot.command(name="addrole")
@commands.has_permissions(manage_roles=True)
async def addrole(ctx: commands.Context, member: discord.Member, *, role: discord.Role):
    await try_delete(ctx)
    if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        return await ctx.send("[X] That role is higher than yours.", delete_after=5)
    await member.add_roles(role)
    await ctx.send(f"> Added **{role.name}** to **{member}**.")


@bot.command(name="removerole")
@commands.has_permissions(manage_roles=True)
async def removerole(ctx: commands.Context, member: discord.Member, *, role: discord.Role):
    await try_delete(ctx)
    await member.remove_roles(role)
    await ctx.send(f"> Removed **{role.name}** from **{member}**.")


@bot.command(name="roleall")
@commands.has_permissions(administrator=True)
async def roleall(ctx: commands.Context, role: discord.Role, include_bots: bool = False):
    await try_delete(ctx)
    if not await confirm(ctx, f"Give **{role.name}** to all {'members' if include_bots else 'humans'}? This can take a while."):
        return await ctx.send("> Cancelled.")
    n = 0
    async with ctx.typing():
        for m in ctx.guild.members:
            if not include_bots and m.bot:
                continue
            if role in m.roles:
                continue
            try:
                await m.add_roles(role)
                n += 1
            except Exception:
                pass
    await ctx.send(f"> Gave **{role.name}** to {n} member(s).")


@bot.command(name="rolecreate")
@commands.has_permissions(manage_roles=True)
async def rolecreate(ctx: commands.Context, name: str, color: str = "#99AAB5"):
    await try_delete(ctx)
    color_hex = color.replace("#", "")
    try:
        col = discord.Color(int(color_hex, 16))
    except ValueError:
        col = discord.Color.default()
    role = await ctx.guild.create_role(name=name, color=col)
    await ctx.send(f"> Created role **{role.name}** (`{role.id}`).")


@bot.command(name="roledelete")
@commands.has_permissions(manage_roles=True)
async def roledelete(ctx: commands.Context, *, role: discord.Role):
    await try_delete(ctx)
    if not await confirm(ctx, f"Delete role **{role.name}**?"):
        return await ctx.send("> Cancelled.")
    await role.delete()
    await ctx.send(f"> Deleted role **{role.name}**.")


@bot.command(name="rolecolor")
@commands.has_permissions(manage_roles=True)
async def rolecolor(ctx: commands.Context, role: discord.Role, color: str):
    await try_delete(ctx)
    try:
        col = discord.Color(int(color.replace("#", ""), 16))
    except ValueError:
        return await ctx.send("[X] Invalid hex color.", delete_after=5)
    await role.edit(color=col)
    await ctx.send(f"> Set color of **{role.name}** to `{color}`.")


@bot.command(name="purgechannel")
@commands.has_permissions(manage_channels=True)
async def purgechannel(ctx: commands.Context, channel: discord.TextChannel = None):
    await try_delete(ctx)
    channel = channel or ctx.channel
    if not await confirm(ctx, f"Recreate {channel.mention}? This deletes all messages."):
        return await ctx.send("> Cancelled.")
    new = await channel.clone(reason="purgechannel")
    await channel.delete()
    await new.send("> Channel recreated.")


@bot.command(name="nuke")
@commands.has_permissions(manage_channels=True)
async def nuke(ctx: commands.Context, channel: discord.TextChannel = None):
    await purgechannel(ctx, channel)


@bot.command(name="topic")
@commands.has_permissions(manage_channels=True)
async def topic(ctx: commands.Context, *, text: str):
    await try_delete(ctx)
    await ctx.channel.edit(topic=text)
    await ctx.send("> Topic updated.")


@bot.command(name="rename")
@commands.has_permissions(manage_channels=True)
async def rename(ctx: commands.Context, name: str, channel: discord.TextChannel = None):
    await try_delete(ctx)
    channel = channel or ctx.channel
    await channel.edit(name=name)
    await ctx.send(f"> Renamed to `{name}`.")


@bot.command(name="channelinfo")
async def channelinfo(ctx: commands.Context, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    embed = discord.Embed(title=channel.name, color=discord.Color.blurple())
    embed.add_field(name="ID", value=str(channel.id))
    embed.add_field(name="Type", value=str(channel.type))
    embed.add_field(name="Slowmode", value=f"{channel.slowmode_delay}s")
    embed.add_field(name="NSFW", value=str(channel.nsfw))
    embed.add_field(name="Category", value=channel.category.name if channel.category else "None")
    embed.add_field(name="Created", value=discord.utils.format_dt(channel.created_at, "R"))
    if channel.topic:
        embed.add_field(name="Topic", value=channel.topic[:1000], inline=False)
    await ctx.send(embed=embed)


@bot.command(name="dm")
@commands.has_permissions(moderate_members=True)
async def dm(ctx: commands.Context, member: discord.Member, *, message: str):
    await try_delete(ctx)
    try:
        await member.send(message)
        await ctx.send(f"> DM sent to **{member}**.")
    except discord.Forbidden:
        await ctx.send("[X] Could not DM that user (DMs disabled).", delete_after=5)


@bot.command(name="say")
@commands.has_permissions(manage_messages=True)
async def say(ctx: commands.Context, *, text: str):
    await try_delete(ctx)
    await ctx.send(text)


@bot.command(name="embed")
@commands.has_permissions(manage_messages=True)
async def embed(ctx: commands.Context, title: str, *, description: str):
    await try_delete(ctx)
    e = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    await ctx.send(embed=e)


@bot.command(name="poll")
@commands.has_permissions(manage_messages=True)
async def poll(ctx: commands.Context, *, question: str):
    await try_delete(ctx)
    msg = await ctx.send(f"> **Poll:** {question}\nReact to vote.")
    await msg.add_reaction("\u2705")
    await msg.add_reaction("\u274C")
    await msg.add_reaction("\u2753")


@bot.command(name="massdm")
@commands.has_permissions(administrator=True)
async def massdm(ctx: commands.Context, *, message: str):
    await try_delete(ctx)
    if not await confirm(ctx, f"DM every human member in {ctx.guild.name}? This is rate-limited and slow."):
        return await ctx.send("> Cancelled.")
    sent, failed = 0, 0
    async with ctx.typing():
        for m in ctx.guild.members:
            if m.bot:
                continue
            try:
                await m.send(message)
                sent += 1
                await asyncio.sleep(1.0)
            except Exception:
                failed += 1
    await ctx.send(f"> DM results -- sent: {sent}, failed: {failed}")


RAID_MODE = set()


@bot.command(name="raidmode")
@commands.has_permissions(administrator=True)
async def raidmode(ctx: commands.Context):
    await try_delete(ctx)
    if ctx.guild.id in RAID_MODE:
        RAID_MODE.discard(ctx.guild.id)
        await ctx.send("> Raid mode DISABLED.")
    else:
        RAID_MODE.add(ctx.guild.id)
        await ctx.send("> Raid mode ENABLED. New accounts (<1 day) will be kicked on join.")


@bot.command(name="roll")
async def roll(ctx: commands.Context, dice: str = "1d6"):
    m = re.fullmatch(r"(\d+)d(\d+)", dice.lower())
    if not m:
        return await ctx.send("[X] Format: NdN, e.g. `2d20`")
    n, sides = int(m.group(1)), int(m.group(2))
    if n < 1 or n > 20 or sides < 2 or sides > 1000:
        return await ctx.send("[X] Out of range.")
    rolls = [random.randint(1, sides) for _ in range(n)]
    await ctx.send(f"> Rolled {dice}: `{rolls}` (total {sum(rolls)})")


@bot.command(name="coinflip")
async def coinflip(ctx: commands.Context):
    await ctx.send(f"> {random.choice(['Heads', 'Tails'])}")


@bot.command(name="8ball")
async def eightball(ctx: commands.Context, *, question: str):
    responses = [
        "It is certain.", "It is decidedly so.", "Without a doubt.", "Yes definitely.",
        "You may rely on it.", "As I see it, yes.", "Most likely.", "Outlook good.",
        "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
        "Better not tell you now.", "Cannot predict now.", "Concentrate and ask again.",
        "Don't count on it.", "My reply is no.", "My sources say no.",
        "Outlook not so good.", "Very doubtful.",
    ]
    await ctx.send(f"> Q: {question}\n> A: {random.choice(responses)}")


@bot.command(name="uptime")
async def uptime(ctx: commands.Context):
    if not hasattr(bot, "_start_time"):
        return await ctx.send("[!] Uptime not tracked yet.")
    delta = datetime.datetime.utcnow() - bot._start_time
    await ctx.send(f"> Uptime: {human_delta(int(delta.total_seconds()))}")


async def _resolve_target(ctx: commands.Context, query: str):
    if query.startswith("<@") and query.endswith(">"):
        try:
            return await commands.MemberConverter().convert(ctx, query)
        except Exception:
            return None

    if query.isdigit():
        member = ctx.guild.get_member(int(query))
        if member:
            return member
        try:
            return await ctx.guild.fetch_member(int(query))
        except Exception:
            return None

    matches = [m for m in ctx.guild.members if m.name.lower() == query.lower() or m.display_name.lower() == query.lower()]
    if len(matches) == 1:
        return matches[0]
    return None


@bot.command(name="checkbg")
@commands.has_permissions(moderate_members=True)
async def checkbg(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)

    history = history_tracker.get(ctx.guild.id, member.id)
    live = await gather_moderation_history(ctx.guild, member)
    risk_score, risk_text, risk_color, risk_reasons = compute_risk(member, live)

    account_age = (discord.utils.utcnow() - member.created_at).days
    joined_at = member.joined_at.strftime("%b %d, %Y at %H:%M UTC") if member.joined_at else "Unknown"
    created_at = member.created_at.strftime("%b %d, %Y at %H:%M UTC")
    verified_role = ctx.guild.get_role(VERIFIED_ROLE_ID)
    is_verified = verified_role in member.roles if verified_role else False

    embed = discord.Embed(
        title=f"Background Check: {member}",
        description=f"Full moderation and identity record for {member.mention}.",
        color=risk_color,
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    identity = (
        f"**User:** {member.mention}\n"
        f"**Username:** `{member.name}`\n"
        f"**Display Name:** `{member.display_name}`\n"
        f"**User ID:** `{member.id}`\n"
        f"**Bot Account:** {'Yes' if member.bot else 'No'}\n"
        f"**Account Age:** `{account_age} days`\n"
        f"**Account Created:** `{created_at}`\n"
        f"**Joined Server:** `{joined_at}`\n"
        f"**Verified:** {'Yes' if is_verified else 'No'}\n"
        f"**Boosting Since:** `{member.premium_since.strftime('%b %d, %Y') if member.premium_since else 'Not Boosting'}`"
    )

    role_list = [r.mention for r in member.roles if r.name != "@everyone"]
    roles_text = ", ".join(role_list) if role_list else "None"
    top_role = member.top_role.mention if member.top_role else "None"

    roles_block = (
        f"**Highest Role:** {top_role}\n"
        f"**Role Count:** `{len(role_list)}`\n"
        f"**Timed Out:** {'Yes' if member.is_timed_out() else 'No'}\n"
        f"**Pending Verification:** {'Yes' if member.pending else 'No'}\n"
        f"**Roles:** {roles_text[:900]}"
    )

    total_actions = len(history.get("actions", []))
    history_lines = [
        f"**Total Tracked Actions:** `{total_actions}`",
        f"**Bans:** `{live['tracked_bans']}`",
        f"**Kicks:** `{live['tracked_kicks']}`",
        f"**Softbans:** `{live['tracked_softbans']}`",
        f"**Timeouts:** `{live['tracked_timeouts']}`",
        f"**Warnings:** `{live['tracked_warns']}`",
        f"**Currently Banned:** {'Yes' if live['actual_ban'] else 'No'}",
    ]
    if live["ban_reason"]:
        history_lines.append(f"**Ban Reason:** `{live['ban_reason']}`")

    history_block = "\n".join(history_lines)

    recent_actions = history.get("actions", [])
    if recent_actions:
        lines = []
        for entry in recent_actions[-10:][::-1]:
            action = entry.get("action", "?").capitalize()
            reason = entry.get("reason", "No reason")
            mod = entry.get("moderator_id", "?")
            ts = entry.get("timestamp", "")[:19].replace("T", " ")
            lines.append(f"`{ts}` **{action}** by <@{mod}> -- *{reason}*")
        recent_block = "\n".join(lines)[:1024]
    else:
        recent_block = "No recorded moderation actions on file."

    risk_block = (
        f"**Score:** `{risk_score}/100`\n"
        f"**Level:** `{risk_text}`\n"
        f"**Factors:**\n" + ("\n".join(f"- {r}" for r in risk_reasons) if risk_reasons else "None detected")
    )

    embed.add_field(name="Identity", value=identity[:1024], inline=False)
    embed.add_field(name="Roles & Status", value=roles_block[:1024], inline=False)
    embed.add_field(name="Moderation Summary", value=history_block[:1024], inline=False)
    embed.add_field(name="Recent Actions", value=recent_block[:1024], inline=False)
    embed.add_field(name="Risk Assessment", value=risk_block[:1024], inline=False)

    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)

    log_entry = log_index.get(ctx.guild.id, member.id)
    if log_entry:
        embed.add_field(
            name="Verification Log",
            value=f"[Jump to original log](https://discord.com/channels/{ctx.guild.id}/{log_entry['channel_id']}/{log_entry['message_id']})",
            inline=False
        )

    try:
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"[X] Failed to send background check: {e}")


class OpenLogsModal(discord.ui.Modal, title="Open a Log Thread"):
    thread_number = discord.ui.TextInput(
        label="Thread number",
        placeholder="e.g. 1 for the newest thread, 2 for the next, etc.",
        required=True,
        max_length=5,
    )

    def __init__(self, threads: list, source_guild_id: int, user_id: int, logging_guild_id: int):
        super().__init__()
        self.threads = threads
        self.source_guild_id = source_guild_id
        self.user_id = user_id
        self.logging_guild_id = logging_guild_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.thread_number.value.strip()
        if not raw.isdigit():
            return await interaction.response.send_message(
                "Invalid input. Enter a whole number like `1`, `2`, `3`.",
                ephemeral=True
            )

        idx = int(raw) - 1
        if idx < 0 or idx >= len(self.threads):
            return await interaction.response.send_message(
                f"Out of range. You must pick a number between `1` and `{len(self.threads)}`.",
                ephemeral=True
            )

        thread = self.threads[idx]
        thread_id = thread.get("id")
        name = thread.get("name", "thread")

        link = f"https://discord.com/channels/{self.logging_guild_id}/{thread_id}"
        embed = discord.Embed(
            title="Log Thread",
            description=f"**Thread:** `{name}`\n**Number:** `{idx + 1}` of `{len(self.threads)}`\n\n[Click here to open the thread]({link})",
            color=discord.Color.from_rgb(88, 101, 242)
        )
        embed.set_footer(text="Only you can see this message.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class OpenLogsView(discord.ui.View):
    def __init__(self, threads: list, source_guild_id: int, user_id: int, logging_guild_id: int, author_id: int, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.threads = threads
        self.source_guild_id = source_guild_id
        self.user_id = user_id
        self.logging_guild_id = logging_guild_id
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This menu belongs to someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Open Logs", style=discord.ButtonStyle.primary)
    async def open_logs(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            OpenLogsModal(
                threads=self.threads,
                source_guild_id=self.source_guild_id,
                user_id=self.user_id,
                logging_guild_id=self.logging_guild_id,
            )
        )

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


@bot.command(name="vhistory")
@commands.has_permissions(moderate_members=True)
async def vhistory(ctx: commands.Context, *, query: str):
    await try_delete(ctx)

    target = await _resolve_target(ctx, query)
    if target is None:
        return await ctx.send("[X] Could not resolve that user.")

    threads = thread_index.get_user(ctx.guild.id, target.id).get("threads", [])
    if not threads:
        return await ctx.send(f"> No log threads on file for **{target}**.")

    logging_guild = find_logging_guild(bot)
    if not logging_guild:
        return await ctx.send("[X] Could not locate the logging server.")

    total_messages = sum(t.get("count", 0) for t in threads)

    embed = discord.Embed(
        title=f"Log Threads: {target}",
        description=(
            f"**User:** {target.mention}\n"
            f"**User ID:** `{target.id}`\n"
            f"**Total Threads:** `{len(threads)}`\n"
            f"**Total Messages:** `{total_messages}`\n\n"
            f"Click **Open Logs** and enter a thread number to jump to a specific thread.\n"
            f"Thread `1` is the newest, `{len(threads)}` is the oldest."
        ),
        color=discord.Color.from_rgb(88, 101, 242),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    lines = []
    for i, t in enumerate(threads, start=1):
        name = t.get("name", "unknown")
        count = t.get("count", 0)
        lines.append(f"`{i}.` `{name}` -- `{count}` message(s)")
    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:4000] + "\n... (truncated)"
    embed.add_field(name="Threads", value=body[:1024], inline=False)
    if len(body) > 1024:
        embed.add_field(name="Threads (continued)", value=body[1024:2048], inline=False)

    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)

    view = OpenLogsView(
        threads=threads,
        source_guild_id=ctx.guild.id,
        user_id=target.id,
        logging_guild_id=logging_guild.id,
        author_id=ctx.author.id,
    )

    try:
        await ctx.send(embed=embed, view=view)
    except Exception as e:
        await ctx.send(f"[X] Failed to send thread history: {e}")


@bot.command(name="mhistory")
@commands.has_permissions(moderate_members=True)
async def mhistory(ctx: commands.Context, *, query: str):
    await try_delete(ctx)

    target = await _resolve_target(ctx, query)
    if target is None:
        return await ctx.send("[X] Could not resolve that user.")

    lines = await fetch_user_thread_lines(bot, ctx.guild.id, target.id)
    lines = [l for l in lines if "[DELETED]" not in l and "[EDITED]" not in l]
    if not lines:
        return await ctx.send(f"> No message history on file for **{target}**.")

    pages = build_message_history_pages(
        target=target,
        lines=lines,
        title_prefix="Message History",
        color=discord.Color.from_rgb(88, 101, 242),
        per_page=10
    )

    if len(pages) == 1:
        return await ctx.send(embed=pages[0])

    view = PaginatedLogView(pages=pages, author_id=ctx.author.id)
    await ctx.send(embed=pages[0], view=view)


@bot.command(name="rhistory")
@commands.has_permissions(moderate_members=True)
async def rhistory(ctx: commands.Context, *, query: str):
    await try_delete(ctx)

    target = await _resolve_target(ctx, query)
    if target is None:
        return await ctx.send("[X] Could not resolve that user.")

    all_lines = await fetch_user_thread_lines(bot, ctx.guild.id, target.id)
    lines = [l for l in all_lines if "[DELETED]" in l]
    if not lines:
        return await ctx.send(f"> No deleted-message history on file for **{target}**.")

    pages = build_message_history_pages(
        target=target,
        lines=lines,
        title_prefix="Deleted Message History",
        color=discord.Color.from_rgb(180, 60, 60),
        per_page=10
    )

    if len(pages) == 1:
        return await ctx.send(embed=pages[0])

    view = PaginatedLogView(pages=pages, author_id=ctx.author.id)
    await ctx.send(embed=pages[0], view=view)


@bot.command(name="ehistory")
@commands.has_permissions(moderate_members=True)
async def ehistory(ctx: commands.Context, *, query: str):
    await try_delete(ctx)

    target = await _resolve_target(ctx, query)
    if target is None:
        return await ctx.send("[X] Could not resolve that user.")

    all_lines = await fetch_user_thread_lines(bot, ctx.guild.id, target.id)
    lines = [l for l in all_lines if "[EDITED]" in l]
    if not lines:
        return await ctx.send(f"> No edit history on file for **{target}**.")

    pages = build_message_history_pages(
        target=target,
        lines=lines,
        title_prefix="Edit History",
        color=discord.Color.from_rgb(250, 166, 26),
        per_page=10
    )

    if len(pages) == 1:
        return await ctx.send(embed=pages[0])

    view = PaginatedLogView(pages=pages, author_id=ctx.author.id)
    await ctx.send(embed=pages[0], view=view)


@bot.command(name="history")
@commands.has_permissions(moderate_members=True)
async def history(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)

    tracked = history_tracker.get(ctx.guild.id, member.id)
    actions = tracked.get("actions", [])

    embed = discord.Embed(
        title=f"Moderation History: {member}",
        color=discord.Color.from_rgb(88, 101, 242),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    summary = (
        f"**Bans:** `{tracked.get('bans', 0)}`\n"
        f"**Kicks:** `{tracked.get('kicks', 0)}`\n"
        f"**Softbans:** `{tracked.get('softbans', 0)}`\n"
        f"**Timeouts:** `{tracked.get('timeouts', 0)}`\n"
        f"**Warnings:** `{tracked.get('warns', 0)}`\n"
        f"**Total Actions:** `{len(actions)}`"
    )
    embed.add_field(name="Summary", value=summary, inline=False)

    if actions:
        recent = actions[-15:][::-1]
        lines = []
        for e in recent:
            action = e.get("action", "?").capitalize()
            reason = e.get("reason", "No reason")
            mod = e.get("moderator_id", "?")
            ts = e.get("timestamp", "")[:19].replace("T", " ")
            lines.append(f"`{ts}` **{action}** by <@{mod}> -- *{reason}*")
        body = "\n".join(lines)
        if len(body) > 1024:
            body = body[:1024]
        embed.add_field(name="Recent Actions", value=body, inline=False)
    else:
        embed.add_field(name="Recent Actions", value="No actions on record.", inline=False)

    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="note")
@commands.has_permissions(moderate_members=True)
async def note(ctx: commands.Context, member: discord.Member, *, text: str):
    await try_delete(ctx)
    count = notes_store.add(ctx.guild.id, member.id, ctx.author.id, text)
    await ctx.send(f"> Added note #{count} for **{member}**.")


@bot.command(name="notes")
@commands.has_permissions(moderate_members=True)
async def notes(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    entries = notes_store.list(ctx.guild.id, member.id)
    if not entries:
        return await ctx.send(f"> **{member}** has no notes.")

    embed = discord.Embed(
        title=f"Notes: {member}",
        color=discord.Color.from_rgb(120, 160, 230),
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    lines = []
    for i, e in enumerate(entries, start=1):
        ts = e.get("timestamp", "")[:19].replace("T", " ")
        lines.append(f"`{i}.` <@{e.get('author_id')}> ({ts}) -- {e.get('text')}")
    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:4000] + "\n... (truncated)"
    embed.description = body
    embed.set_footer(text=f"Total notes: {len(entries)}")
    await ctx.send(embed=embed)


@bot.command(name="delnote")
@commands.has_permissions(moderate_members=True)
async def delnote(ctx: commands.Context, member: discord.Member, index: int):
    await try_delete(ctx)
    removed = notes_store.delete(ctx.guild.id, member.id, index)
    if not removed:
        return await ctx.send("[X] Invalid note index.", delete_after=5)
    await ctx.send(f"> Removed note {index} for **{member}** -- {removed.get('text')}")


@bot.command(name="clearnotes")
@commands.has_permissions(administrator=True)
async def clearnotes(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    if not await confirm(ctx, f"Clear all notes on **{member}**?"):
        return await ctx.send("> Cancelled.")
    notes_store.clear(ctx.guild.id, member.id)
    await ctx.send(f"> Cleared notes for **{member}**.")


@bot.command(name="watch")
@commands.has_permissions(moderate_members=True)
async def watch(ctx: commands.Context, member: discord.Member, *, reason: str):
    await try_delete(ctx)
    watchlist.add(ctx.guild.id, member.id, ctx.author.id, reason)
    await ctx.send(f"> Added **{member}** to the watchlist -- {reason}")


@bot.command(name="unwatch")
@commands.has_permissions(moderate_members=True)
async def unwatch(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    if watchlist.remove(ctx.guild.id, member.id):
        await ctx.send(f"> Removed **{member}** from the watchlist.")
    else:
        await ctx.send(f"> **{member}** is not on the watchlist.")


@bot.command(name="watchlist")
@commands.has_permissions(moderate_members=True)
async def watchlist_cmd(ctx: commands.Context):
    await try_delete(ctx)
    entries = watchlist.list(ctx.guild.id)
    if not entries:
        return await ctx.send("> Watchlist is empty.")

    embed = discord.Embed(
        title=f"Watchlist: {ctx.guild.name}",
        color=discord.Color.from_rgb(250, 166, 26),
        timestamp=discord.utils.utcnow()
    )
    lines = []
    for uid, info in entries.items():
        ts = info.get("timestamp", "")[:19].replace("T", " ")
        lines.append(f"<@{uid}> (`{uid}`) -- {info.get('reason')} | added by <@{info.get('author_id')}> on {ts}")
    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:4000] + "\n... (truncated)"
    embed.description = body
    embed.set_footer(text=f"Total: {len(entries)}")
    await ctx.send(embed=embed)


@bot.command(name="noteach")
@commands.has_permissions(moderate_members=True)
async def noteach(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    count = notes_store.add(ctx.guild.id, member.id, ctx.author.id, "Flagged by staff for review.")
    await ctx.send(f"> Flagged **{member}** (note #{count}).")


@bot.command(name="recentjoins")
@commands.has_permissions(moderate_members=True)
async def recentjoins(ctx: commands.Context, count: int = 10):
    await try_delete(ctx)
    if count < 1 or count > 50:
        return await ctx.send("[X] Count must be 1-50.", delete_after=5)

    members = sorted(
        [m for m in ctx.guild.members if m.joined_at],
        key=lambda m: m.joined_at,
        reverse=True
    )[:count]

    embed = discord.Embed(
        title=f"Recent Joins: {ctx.guild.name}",
        color=discord.Color.from_rgb(88, 101, 242),
        timestamp=discord.utils.utcnow()
    )
    lines = []
    for i, m in enumerate(members, start=1):
        age = (discord.utils.utcnow() - m.joined_at).days
        lines.append(f"`{i}.` {m.mention} -- joined `{age}d ago` -- account `{(discord.utils.utcnow() - m.created_at).days}d`")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Top {len(members)} newest members")
    await ctx.send(embed=embed)


@bot.command(name="newaccounts")
@commands.has_permissions(moderate_members=True)
async def newaccounts(ctx: commands.Context, days: int = 7):
    await try_delete(ctx)
    if days < 1 or days > 365:
        return await ctx.send("[X] Days must be 1-365.", delete_after=5)

    cutoff = discord.utils.utcnow() - datetime.timedelta(days=days)
    members = [m for m in ctx.guild.members if m.created_at > cutoff]

    if not members:
        return await ctx.send(f"> No members with accounts younger than {days} days.")

    members.sort(key=lambda m: m.created_at, reverse=True)
    members = members[:25]

    embed = discord.Embed(
        title=f"Accounts under {days} days old",
        color=discord.Color.from_rgb(250, 166, 26),
        timestamp=discord.utils.utcnow()
    )
    lines = []
    for m in members:
        age = (discord.utils.utcnow() - m.created_at).days
        lines.append(f"{m.mention} (`{m.id}`) -- `{age}d old`")
    embed.description = "\n".join(lines)[:4000]
    embed.set_footer(text=f"Showing {len(members)} of {len(members)}")
    await ctx.send(embed=embed)


@bot.command(name="joinposition")
@commands.has_permissions(moderate_members=True)
async def joinposition(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    ordered = sorted(
        [m for m in ctx.guild.members if m.joined_at],
        key=lambda m: m.joined_at
    )
    try:
        pos = ordered.index(member) + 1
    except ValueError:
        return await ctx.send("[X] Could not determine join position.")

    embed = discord.Embed(
        title=f"Join Position: {member}",
        color=discord.Color.from_rgb(88, 101, 242)
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Position", value=f"`{pos}` of `{len(ordered)}`", inline=True)
    embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "?", inline=True)
    embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="id")
@commands.has_permissions(moderate_members=True)
async def id_cmd(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    await ctx.send(f"> **{member}** -- `{member.id}`")


@bot.command(name="created")
@commands.has_permissions(moderate_members=True)
async def created(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    age = (discord.utils.utcnow() - member.created_at).days
    await ctx.send(f"> **{member}**'s account was created <t:{int(member.created_at.timestamp())}:F> ({age} days ago).")


@bot.command(name="joined")
@commands.has_permissions(moderate_members=True)
async def joined(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    if not member.joined_at:
        return await ctx.send(f"> **{member}** has no join timestamp on record.")
    age = (discord.utils.utcnow() - member.joined_at).days
    await ctx.send(f"> **{member}** joined <t:{int(member.joined_at.timestamp())}:F> ({age} days ago).")


@bot.command(name="rolecount")
@commands.has_permissions(moderate_members=True)
async def rolecount(ctx: commands.Context, *, role: discord.Role):
    await try_delete(ctx)
    count = len(role.members)
    total = ctx.guild.member_count or 1
    pct = round((count / total) * 100, 1)
    await ctx.send(f"> **{role.name}** -- `{count}` members ({pct}% of server).")


@bot.command(name="perms")
@commands.has_permissions(moderate_members=True)
async def perms(ctx: commands.Context, member: discord.Member):
    await try_delete(ctx)
    p = member.guild_permissions
    keys = [
        ("administrator", "Administrator"),
        ("manage_guild", "Manage Server"),
        ("manage_roles", "Manage Roles"),
        ("manage_channels", "Manage Channels"),
        ("manage_messages", "Manage Messages"),
        ("kick_members", "Kick Members"),
        ("ban_members", "Ban Members"),
        ("moderate_members", "Timeout Members"),
        ("manage_nicknames", "Manage Nicknames"),
        ("mention_everyone", "Mention Everyone"),
    ]
    lines = []
    for attr, label in keys:
        val = getattr(p, attr, False)
        lines.append(f"{'Yes' if val else 'No'} -- {label}")

    embed = discord.Embed(
        title=f"Key Permissions: {member}",
        description="\n".join(lines),
        color=discord.Color.from_rgb(88, 101, 242)
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"User ID: {member.id}")
    await ctx.send(embed=embed)


@bot.event
async def on_member_join(member: discord.Member):
    await _push_member_to_supabase(member)

    if member.guild.id in RAID_MODE:
        age = discord.utils.utcnow() - member.created_at
        if age.total_seconds() < 86400:
            try:
                await member.kick(reason="Raid mode: new account")
                history_tracker.record(member.guild.id, member.id, "kick", bot.user.id, "Raid mode: new account")
                await safe_refresh_verification_log(bot, member.guild, member.id)
            except Exception:
                pass


@bot.event
async def on_member_remove(member: discord.Member):
    if member.bot:
        return
    try:
        delete_member(str(member.id))
    except Exception as e:
        print(f"[SUPABASE] member delete failed for {member.id}: {e}")


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    before_ids = {r.id for r in before.roles}
    after_ids = {r.id for r in after.roles}
    if before_ids == after_ids and before.display_name == after.display_name:
        return
    await _push_member_to_supabase(after)


@bot.event
async def on_connect():
    if not hasattr(bot, "_start_time"):
        bot._start_time = datetime.datetime.utcnow()


setup_anti_raid(bot)

if __name__ == "__main__":
    bot.run(TOKEN)