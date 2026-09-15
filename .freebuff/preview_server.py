"""Preview-only stub server for the admin dashboard.

Runs the *real* Flask app from dashboard.py and the real templates, but with a
fake Discord bot standing in for the live connection. That means the dashboard
can be opened in a browser without a bot token, without a second gateway
connection, and without touching the instance the user is actually running on
port 5000.

Nothing here is used by main.py / update.bat. It exists so the panel is
reviewable and testable while the real bot is offline or busy.

Run it detached (the preview tab does this for you):

    python .freebuff/preview_server.py

It listens on 127.0.0.1:5055 and serves the same template the real dashboard
serves, so template edits show up on refresh (auto_reload is forced on).
"""

import asyncio
import datetime
import json
import os
import sys
import threading

import discord

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import dashboard as dash  # noqa: E402  (needs the path set up first)
from flask import session  # noqa: E402

PORT = 5055
GUILD_ID = 1423000000000000000

# Role ids are the real ones from dash_permissions.json, so the access matrix in
# this preview resolves exactly the way it does on the live server.
ROLE_IDS = {
    "Owner": "1548496816989798400",
    "Muted": "1548593951336833034",
    "Moderator": "1549235255737712700",
    "Helper": "1549204698249171024",
    "Admin": "1548497068987916411",
    "Bot Manager": "1548422170126979083",
    "Trial Mod": "1548537314811060315",
    "Member": "1548864856034381844",
    "Verified": "1548851915805433897",
    # Deliberately left out of dash_permissions.json: these two show what a role
    # nobody has configured yet resolves to (capabilities read from Discord).
    "DJ": "1549900000000000011",
    "Newcomer": "1549900000000000012",
}


def mk(cls, **attrs):
    """Build a discord.py object without running __init__.

    discord.py models are slotted, and dashboard.py type-checks with isinstance
    (discord.Role, discord.TextChannel, ...), so the fakes have to be real
    instances of those classes rather than duck-typed stand-ins.
    """
    obj = cls.__new__(cls)
    for key, value in attrs.items():
        # A few of these are read-only properties on the real class. They are
        # shadowed by a subclass slot where it matters, and skipped here.
        try:
            setattr(obj, key, value)
        except AttributeError:
            pass
    return obj


class FakeMember(discord.Member):
    """Member with the read-only bits it computes from state as plain slots."""

    __slots__ = ("top_role", "status", "guild_permissions")


def snowflake(when):
    """A Discord id that decodes to this date, so account/joined ages are real."""
    ms = int(when.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    return (ms - 1420070400000) << 22


def perms_int(**flags):
    return int(discord.Permissions(**flags).value) if flags else 0


class FakeGuild:
    def __init__(self, gid, name):
        self.id = gid
        self.name = name
        self.icon = None
        self.roles = []
        self.members = []
        # discord.py reads role.members off guild._members and member.roles off
        # a SnowflakeList of role ids, so both have to exist in that shape.
        self._members = {}
        self.channels = []
        self.categories = []
        self.member_count = 0
        self.owner_id = None
        self.me = None
        self.default_role = None
        self.emojis = []
        # Populated by seed_automod_data(): the AutoMod pages render these.
        self._voice_states = {}
        self._sample_bans = []
        self._sample_invites = []

    # --- lookups -------------------------------------------------------
    def get_member(self, uid):
        return self._members.get(int(uid))

    def _voice_state_for(self, user_id):
        # discord.Member.voice reads this off the guild, so the fake needs it or
        # every member.voice lookup raises AttributeError.
        return self._voice_states.get(int(user_id))

    def get_role(self, rid):
        rid = int(getattr(rid, "id", rid))
        for r in self.roles:
            if int(r.id) == rid:
                return r
        return None

    def get_channel(self, cid):
        cid = int(cid)
        for c in self.channels + self.categories:
            if int(c.id) == cid:
                return c
        return None

    @property
    def text_channels(self):
        return [c for c in self.channels if isinstance(c, discord.TextChannel)]

    @property
    def voice_channels(self):
        return [c for c in self.channels if isinstance(c, discord.VoiceChannel)]

    # --- async surface the dashboard awaits through run_coroutine_threadsafe
    async def bans(self, limit=None):
        # discord.py's Guild.bans() is an async *iterator*, and every caller in
        # this project does `async for entry in guild.bans(...)`. A coroutine
        # that returns a list would raise TypeError and the panel would report
        # an empty vault, so this yields the same way the real one does.
        for entry in list(self._sample_bans):
            yield entry

    async def invites(self):
        return list(self._sample_invites)

    async def webhooks(self):
        return []

    async def audit_logs(self, limit=None, **kwargs):
        return []

    async def fetch_member(self, uid):
        member = self.get_member(uid)
        if member is None:
            raise discord.NotFound(_Resp(404), "Unknown Member")
        return member

    async def fetch_ban(self, uid):
        raise discord.NotFound(_Resp(404), "Unknown Ban")

    async def ban(self, user, **kwargs):
        return None

    async def unban(self, user, **kwargs):
        return None

    async def kick(self, user, **kwargs):
        return None

    async def create_role(self, **kwargs):
        role = mk(
            discord.Role,
            guild=self,
            id=1549999999999999999,
            name=kwargs.get("name") or "new role",
            _colour=int(kwargs.get("colour") or 0),
            position=1,
            _permissions=perms_int(),
            hoist=False,
            mentionable=False,
            managed=False,
            _icon=None,
            tags=None,
            unicode_emoji=None,
        )
        self.roles.append(role)
        return role

    async def edit_role_positions(self, positions, **kwargs):
        return list(self.roles)

    async def create_invite(self, channel, **kwargs):
        return None


class _Resp:
    """Minimal stand-in for aiohttp's response, for discord.HTTPException."""

    def __init__(self, status):
        self.status = status
        self.reason = "Not Found"


class FakeBot:
    def __init__(self, guild):
        self._guild = guild
        self.user = mk(
            discord.User,
            id=snowflake(datetime.datetime(2023, 3, 4)),
            name="nebula-guard",
            global_name="nebula guard",
            discriminator="0",
            bot=True,
            _avatar=None,
            _banner=None,
            _state=None,
            _flags=0,
        )
        self.latency = 0.041
        self.guilds = [guild]
        self.emojis = []
        self.tree = _FakeTree()
        self.loop = _LOOP

    def get_guild(self, gid):
        return self._guild if int(gid) == int(self._guild.id) else None

    def get_channel(self, cid):
        return self._guild.get_channel(cid)

    def get_user(self, uid):
        return self._guild.get_member(uid)

    def get_member(self, uid):
        return self._guild.get_member(uid)

    async def fetch_user(self, uid):
        return self._guild.get_member(uid)

    async def change_presence(self, **kwargs):
        return None

    async def close(self):
        return None


class _FakeTree:
    async def sync(self, *args, **kwargs):
        return []


def build_guild():
    guild = FakeGuild(GUILD_ID, "Nebula Keep")
    everyone = mk(
        discord.Role,
        guild=guild,
        id=GUILD_ID,
        name="@everyone",
        _colour=0,
        position=0,
        _permissions=perms_int(view_channel=True, send_messages=True, read_message_history=True),
        hoist=False,
        mentionable=False,
        managed=False,
        _icon=None,
        tags=None,
        unicode_emoji=None,
    )

    role_spec = [
        ("Owner", 0x8B5CF6, 20, dict(administrator=True)),
        ("Admin", 0xE06C6C, 18, dict(administrator=True)),
        ("Moderator", 0x6EA8C9, 15, dict(kick_members=True, ban_members=True, moderate_members=True,
                                          manage_messages=True, view_audit_log=True)),
        ("Bot Manager", 0x7A8CE0, 13, dict(manage_webhooks=True, manage_guild=True)),
        ("Trial Mod", 0x5A9A68, 11, dict(moderate_members=True, manage_messages=True)),
        ("Helper", 0x6EC9C9, 9, dict(view_audit_log=True, manage_messages=True)),
        ("DJ", 0xC98F26, 7, dict(manage_channels=True)),
        ("Verified", 0x4A7F9E, 5, dict()),
        ("Member", 0x99AAB5, 3, dict()),
        ("Muted", 0x4A4F55, 1, dict()),
    ]
    roles = {"@everyone": everyone}
    for name, colour, position, flags in role_spec:
        roles[name] = mk(
            discord.Role,
            guild=guild,
            id=int(ROLE_IDS[name]),
            name=name,
            _colour=colour,
            position=position,
            _permissions=perms_int(**flags),
            hoist=position >= 11,
            mentionable=False,
            managed=False,
            _icon=None,
            tags=None,
            unicode_emoji=None,
        )
    guild.roles = [roles["@everyone"]] + [roles[n] for n, *_ in role_spec]
    guild.default_role = everyone

    member_spec = [
        # name, display, roles, joined, created, bot
        ("felic", "felic", ["Owner"], datetime.datetime(2024, 2, 11), datetime.datetime(2019, 6, 2), False),
        ("luna.dev", "luna", ["Admin", "Bot Manager"], datetime.datetime(2024, 3, 2), datetime.datetime(2020, 1, 9), False),
        ("kay", "kay ✦", ["Moderator", "Verified"], datetime.datetime(2024, 6, 21), datetime.datetime(2021, 4, 14), False),
        ("mireille", "mimi", ["Trial Mod", "Helper", "Verified"], datetime.datetime(2024, 9, 3), datetime.datetime(2022, 7, 30), False),
        ("oskar", "oskar", ["Helper", "DJ", "Verified"], datetime.datetime(2025, 1, 17), datetime.datetime(2023, 2, 2), False),
        ("tomas", "tomas", ["Member", "Verified"], datetime.datetime(2025, 4, 8), datetime.datetime(2023, 11, 19), False),
        ("priya", "priya", ["Member"], datetime.datetime(2025, 8, 30), datetime.datetime(2024, 5, 26), False),
        ("quin", "quin", ["Newcomer"], datetime.datetime(2026, 8, 2), datetime.datetime(2026, 6, 11), False),
        ("quiet.member", "quiet", ["Newcomer", "Member"], datetime.datetime(2026, 8, 19), datetime.datetime(2026, 7, 28), False),
        ("GuardBot", "GuardBot", ["Bot Manager"], datetime.datetime(2024, 1, 1), datetime.datetime(2023, 3, 4), True),
    ]
    members = []
    for name, display, role_names, joined, created, is_bot in member_spec:
        uid = snowflake(created)
        user = mk(
            discord.User,
            id=uid,
            name=name,
            global_name=display,
            discriminator="0",
            bot=is_bot,
            # Default avatars, not made-up hashes: the real CDN answers 400 for
            # an avatar hash that does not exist, which shows up as a console
            # error and a broken image.
            _avatar=None,
            _banner=None,
            _state=None,
            _flags=0,
        )
        held = [roles[r] for r in role_names if r in roles]
        member = mk(
            FakeMember,
            guild=guild,
            _user=user,
            _roles=discord.utils.SnowflakeList([int(r.id) for r in held]),
            joined_at=joined,
            created_at=created,
            premium_since=None,
            pending=False,
            nick=None,
            timed_out_until=None,
            _permissions=perms_int(view_channel=True, send_messages=True),
            client_status={"desktop": "online"},
            activities=[],
            _state=None,
            _avatar=None,
            _banner=None,        _flags=0,
        _avatar_decoration_data=None,
        status=discord.Status.online,
        top_role=max(held, key=lambda r: r.position) if held else everyone,
        guild_permissions=perms_int(view_channel=True, send_messages=True),
    )
        members.append(member)
    guild.members = members
    guild._members = {int(m.id): m for m in members}
    guild.member_count = len(members)
    guild.owner_id = int(members[0].id)
    guild.me = mk(
        FakeMember,
        guild=guild,
        _user=FakeBot(guild).user,
        _roles=discord.utils.SnowflakeList([int(roles["Admin"].id)]),
        joined_at=datetime.datetime(2024, 1, 1),
        created_at=datetime.datetime(2023, 3, 4),
        premium_since=None,
        pending=False,
        nick=None,
        timed_out_until=None,
        _permissions=int(discord.Permissions.all().value),
        client_status={"desktop": "online"},
        activities=[],
        _state=None,
        _avatar=None,
        _banner=None,
        _flags=0,
        _avatar_decoration_data=None,
        status=discord.Status.online,
        top_role=roles["Admin"],
        guild_permissions=discord.Permissions.all(),
    )

    # --- channels -------------------------------------------------------
    def category(cid, name, position):
        cat = mk(
            discord.CategoryChannel,
            guild=guild,
            id=cid,
            name=name,
            position=position,
            _type=discord.ChannelType.category,
            nsfw=False,
            _state=None,
            _overwrites={},
        )
        guild.categories.append(cat)
        return cat

    information = category(1423000000000000100, "information", 1)
    general = category(1423000000000000200, "general", 5)
    staff = category(1423000000000000300, "staff only", 9)
    voice = category(1423000000000000400, "voice", 13)

    def text(cid, name, cat, position, topic="", slowmode=0, nsfw=False, ch_type=None):
        ch = mk(
            discord.TextChannel,
            guild=guild,
            id=cid,
            name=name,
            topic=topic,
            nsfw=nsfw,
            category_id=cat.id,
            position=position,
            slowmode_delay=slowmode,
            _overwrites={},
            _type=ch_type or discord.ChannelType.text,
            last_message_id=None,
            default_auto_archive_duration=1440,
            default_thread_slowmode_delay=0,
            _state=None,
        )
        guild.channels.append(ch)
        return ch

    def voice_channel(cid, name, cat, position, limit=0, bitrate=64000):
        ch = mk(
            discord.VoiceChannel,
            guild=guild,
            id=cid,
            name=name,
            nsfw=False,
            category_id=cat.id,
            position=position,
            slowmode_delay=0,
            _overwrites={},
            bitrate=bitrate,
            user_limit=limit,
            rtc_region=None,
            video_quality_mode=discord.VideoQualityMode.auto,
            last_message_id=None,
            _state=None,
        )
        guild.channels.append(ch)
        return ch

    text(1423000000000000101, "rules", information, 2, topic="Read me first.", slowmode=0)
    text(1423000000000000102, "announcements", information, 3,
         topic="Server news.", ch_type=discord.ChannelType.news)
    text(1423000000000000201, "general-chat", general, 6,
         topic="Talk about anything, keep it kind.", slowmode=5)
    text(1423000000000000202, "media", general, 7, topic="Screenshots and clips.", slowmode=10)
    text(1423000000000000203, "bot-spam", general, 8, topic="Commands go here.")
    text(1423000000000000301, "staff-chat", staff, 10, topic="Internal only.", nsfw=False)
    text(1423000000000000302, "mod-logs", staff, 11, topic="Action log mirror.")
    text(1423000000000000303, "tickets", staff, 12, topic="Support tickets.")
    voice_channel(1423000000000000401, "General VC", voice, 14)
    voice_channel(1423000000000000402, "Music", voice, 15, limit=10, bitrate=96000)
    voice_channel(1423000000000000403, "AFK", voice, 16, limit=1)
    return guild


# The dashboard awaits coroutines on the bot's loop, so give it a real one.
_LOOP = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(_LOOP)
    _LOOP.run_forever()


threading.Thread(target=_run_loop, daemon=True).start()

GUILD = build_guild()
BOT = FakeBot(GUILD)
dash.set_bot(BOT)


# --------------------------------------------------------------------------
# Sample AutoMod data
#
# The suite has four pages that read live Discord state (bans, invites, voice)
# and three that read the AutoMod store. None of that exists in a fresh
# checkout, so every one of them would render its empty state and the pages
# could not actually be looked at. These fill them in.
# --------------------------------------------------------------------------

import antiraid  # noqa: E402  (after the path is set up above)

# Same reasoning as the permissions copy: a preview must never rewrite the real
# automod_config.json that the running bot reads.
_PREVIEW_AUTOMOD = os.path.join(HERE, "preview-automod.json")
dash.automod_config = antiraid.AutoModConfig(_PREVIEW_AUTOMOD)

# The channel flags and the owner access matrix are the same story: both are
# files the running bot owns, so the preview keeps its own copies and the real
# ones stay untouched.
_PREVIEW_MEDIA_ONLY = os.path.join(HERE, "preview-media-only.json")
_PREVIEW_SELFPROMO = os.path.join(HERE, "preview-selfpromo.json")
antiraid.media_only_store = antiraid.MediaOnlyStore(_PREVIEW_MEDIA_ONLY)
antiraid.selfpromo_store = antiraid.SelfPromoStore(_PREVIEW_SELFPROMO)
dash.media_only_store = antiraid.media_only_store
dash.selfpromo_store = antiraid.selfpromo_store

# Cases and tickets are the same story one more time. A case created while
# clicking around the preview must not land in the real mod_cases.json, and the
# ticket console needs an application and an appeal to have anything to show.
_PREVIEW_CASES = os.path.join(HERE, "preview-mod-cases.json")
_PREVIEW_TICKETS = os.path.join(HERE, "preview-tickets.json")
dash.TICKET_PATH = _PREVIEW_TICKETS


def _seed_cases(guild):
    """Write the sample cases and return the store that holds them.

    The store has to be handed back rather than built on the side here: a
    second instance constructed after the seed would read its own copy of the
    file, and the bound store would never see these records.
    """
    store = antiraid.CaseStore(_PREVIEW_CASES)
    if store.data.get("cases"):
        return store
    actor = guild.members[0]
    target = guild.members[1]
    ban = store.create(guild.id, target.id, "ban", "Posted a fake Nitro link in #general",
                       actor.id, {"user_tag": str(target), "moderator_tag": str(actor),
                                  "channel_id": "1423000000000000201",
                                  "notes": "AutoMod: link-spam"})
    store.create(guild.id, target.id, "kick", "Spamming the same message in #general",
                 actor.id, {"user_tag": str(target), "moderator_tag": str(actor),
                            "channel_id": "1423000000000000201"})
    # The preview signs itself in as members[0], so that account needs cases of
    # its own or the member page's case picker renders its empty state.
    store.create(guild.id, actor.id, "kick", "Repeatedly ignoring the slowmode rule in #general",
                 target.id, {"user_tag": str(actor), "moderator_tag": str(target),
                             "channel_id": "1423000000000000201"})
    store.create(guild.id, actor.id, "tempban", "Ban evasion on an alt account", target.id,
                 {"user_tag": str(actor), "moderator_tag": str(target),
                  "duration": "7d (7 days)", "expires_at": "2026-09-22 12:00 UTC",
                  "channel_id": "1423000000000000201"})
    print(f"seeded preview cases (appeal one is {ban['id']})")
    return store


antiraid.case_store = _seed_cases(GUILD)
dash.case_store = antiraid.case_store


def _seed_tickets(store, guild):
    """Fill the ticket console, which a fresh checkout renders empty.

    Its APPLICATION badge and its case chip only appear when there really is an
    application and an appeal to show, so both are written into the preview's
    own tickets file. They belong to the account the preview signs in as, which
    is also what puts them in the member page's own ticket list.
    """
    actor = guild.members[0]
    target = guild.members[1]
    if not os.path.exists(_PREVIEW_TICKETS):
        # An appeal has to quote a case the *sender* owns, or the case picker on
        # the member page has nothing to offer them.
        own = [c for c in (store.data.get("cases") or {}).values()
               if str(c.get("user_id")) == str(actor.id)]
        own.sort(key=lambda c: str(c.get("created_at") or ""))
        case_id = own[0]["id"] if own else "C-1001"
        now = datetime.datetime.utcnow().isoformat() + "+00:00"
        rows = [
            {
                "id": "T0001", "number": 1, "user_id": str(target.id), "anonymous": False,
                "anon_label": "", "assigned_to": None,
                "participants": {"users": [], "roles": []}, "contact": "",
                "username": str(target), "display_name": str(target),
                "avatar_url": target.display_avatar.url,
                "category": "apply", "category_label": "Apply for Staff",
                "subject": "apply for staff", "priority": "normal",
                "answers": {
                    "What position are you applying for?": "Moderator",
                    "How old are you?": "18-21",
                    "How long have you been in this server?": "6-12 months",
                    "Roughly how many hours a week can you be active?": "10-20 hours",
                    "Do you have any previous moderation or staff experience?":
                        "I moderated a 500-member server for a year.",
                    "Why do you want to join the team?":
                        "I am in here every day and I would rather spend that time helping than watching. "
                        "I already answer questions in support when I can and I would like to do it properly.",
                },
                "status": "open", "created_at": now, "updated_at": now, "replies": [],
                "attachments": [],
            },
            {
                "id": "T0002", "number": 2, "user_id": str(target.id), "anonymous": False,
                "anon_label": "", "assigned_to": None,
                "participants": {"users": [], "roles": []}, "contact": "",
                "username": str(target), "display_name": str(target),
                "avatar_url": target.display_avatar.url,
                "category": "appeal", "category_label": "Appeal a punishment",
                "subject": "ban appeal", "priority": "normal",
                "answers": {
                    "Which punishment did you get?": "Ban",
                    "Which account was punished?": str(target),
                    "Case ID": case_id,
                    "Why should we undo it?": "It was a joke link between friends and I did not know it was banned.",
                    "Have you appealed this before?": "No",
                },
                "status": "open", "created_at": now, "updated_at": now, "replies": [],
                "attachments": [],
            },
            {
                "id": "T0003", "number": 3, "user_id": str(actor.id), "anonymous": False,
                "anon_label": "", "assigned_to": None,
                "participants": {"users": [], "roles": []}, "contact": "",
                "username": str(actor), "display_name": str(actor),
                "avatar_url": actor.display_avatar.url,
                "category": "appeal", "category_label": "Appeal a punishment",
                "subject": "appeal for my kick", "priority": "normal",
                "answers": {
                    "Which punishment did you get?": "Kick",
                    "Which account was punished?": str(actor),
                    "Case ID": case_id,
                    "Case reason": "Repeatedly ignoring the slowmode rule in #general",
                    "Why should we undo it?": "I had a slow connection and the same message sent twice.",
                    "Have you appealed this before?": "No",
                },
                "status": "answered", "created_at": now, "updated_at": now,
                "attachments": [],
                "replies": [{
                    "author": str(target), "author_id": str(target.id), "staff": True,
                    "avatar_url": target.display_avatar.url,
                    "at": now, "body": "Thanks for the appeal. Do you have a screenshot of the duplicate message?",
                    "attachments": [],
                }],
            },
        ]
        with open(_PREVIEW_TICKETS, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
        print(f"seeded preview tickets (appeal quotes {case_id}, which the sender owns)")


_seed_tickets(antiraid.case_store, GUILD)


class _FakeBanEntry:
    """What `guild.bans()` yields: something with a `.user` and a `.reason`."""

    def __init__(self, user, reason):
        self.user = user
        self.reason = reason


def _fake_user(uid, name, display):
    return mk(
        discord.User,
        id=uid,
        name=name,
        global_name=display,
        discriminator="0",
        bot=False,
        _avatar=None,
        _banner=None,
        _state=None,
        _flags=0,
    )


def _seed_automod_data(guild):
    now = datetime.datetime.utcnow()

    # --- bans vault -----------------------------------------------------
    guild._sample_bans = [
        _FakeBanEntry(_fake_user(snowflake(now - datetime.timedelta(days=40)), "raidalt", "raidalt"),
                      "Raid account — joined with 30 others"),
        _FakeBanEntry(_fake_user(snowflake(now - datetime.timedelta(days=120)), "nitro-shill", "nitro shill"),
                      "Posted a fake Nitro link"),
        _FakeBanEntry(_fake_user(snowflake(now - datetime.timedelta(days=260)), "old.scammer", "scammer"),
                      "Chargeback scam in #media"),
    ]

    # --- invites --------------------------------------------------------
    general = guild.get_channel(1423000000000000201)
    tickets = guild.get_channel(1423000000000000303)
    inviter = guild.members[2]  # kay
    helper = guild.get_role(ROLE_IDS["Helper"])
    guild._sample_invites = [
        mk(discord.Invite, max_age=0, code="nebulaKEEP", guild=guild, revoked=False,
           created_at=now - datetime.timedelta(days=90), uses=47, temporary=False, max_uses=0,
           inviter=inviter, channel=general, target_user=None, target_type=None,
           _state=None, approximate_member_count=0, approximate_presence_count=0,
           target_application=None, expires_at=None, scheduled_event=None,
           scheduled_event_id=None, type=discord.InviteTarget.unknown, _flags=0),
        mk(discord.Invite, max_age=604800, code="nebulaWEEK", guild=guild, revoked=False,
           created_at=now - datetime.timedelta(days=2), uses=5, temporary=False, max_uses=25,
           inviter=guild.members[1], channel=general, target_user=None, target_type=None,
           _state=None, approximate_member_count=0, approximate_presence_count=0,
           target_application=None, expires_at=now + datetime.timedelta(days=5),
           scheduled_event=None, scheduled_event_id=None, type=discord.InviteTarget.unknown, _flags=0),
        mk(discord.Invite, max_age=86400, code="nebulaTEMP", guild=guild, revoked=False,
           created_at=now - datetime.timedelta(hours=20), uses=1, temporary=True, max_uses=1,
           inviter=guild.members[3], channel=tickets, target_user=None, target_type=None,
           _state=None, approximate_member_count=0, approximate_presence_count=0,
           target_application=None, expires_at=now + datetime.timedelta(hours=4),
           scheduled_event=None, scheduled_event_id=None, type=discord.InviteTarget.unknown, _flags=0),
        mk(discord.Invite, max_age=0, code="nebulaOLD", guild=guild, revoked=False,
           created_at=now - datetime.timedelta(days=400), uses=210, temporary=False, max_uses=0,
           inviter=guild.members[4], channel=general, target_user=None, target_type=None,
           _state=None, approximate_member_count=0, approximate_presence_count=0,
           target_application=None, expires_at=None, scheduled_event=None,
           scheduled_event_id=None, type=discord.InviteTarget.unknown, _flags=0),
    ]

    # --- voice rooms ----------------------------------------------------
    general_vc = guild.get_channel(1423000000000000401)
    music_vc = guild.get_channel(1423000000000000402)

    def state(channel, mute=False, deaf=False, self_mute=False, self_deaf=False, stream=False):
        return mk(discord.VoiceState, session_id="preview", deaf=deaf, mute=mute,
                  self_mute=self_mute, self_stream=stream, self_video=False,
                  self_deaf=self_deaf, afk=False, channel=channel,
                  requested_to_speak_at=None, suppress=False)

    guild._voice_states = {
        int(guild.members[2].id): state(general_vc, self_mute=True),
        int(guild.members[5].id): state(general_vc),
        int(guild.members[6].id): state(general_vc, mute=True, deaf=True),
        int(guild.members[4].id): state(music_vc, stream=True),
    }

    # --- the automod store ----------------------------------------------
    # `reset` first so a restart never stacks last run's incidents onto the new
    # trail — the seed is meant to be the same every time.
    dash.automod_config.update(int(guild.id), {
        "reset": True,
        "enabled": True,
        "dry_run": False,
        "timeout_seconds": 600,
        "purge_count": 20,
        "dm_member": True,
        "exempt_roles": [str(helper.id)] if helper else [],
        "exempt_channels": [],
        "rules": {
            "words": {"enabled": True, "action": "delete", "whole_word": True, "regex": False,
                      "list": ["free nitro", "crypto giveaway", "dm me to buy", "kys"]},
            "caps": {"enabled": True, "action": "log", "count": 14, "ratio": 75},
            "emoji": {"enabled": True, "action": "delete", "count": 10},
            "invites": {"enabled": True, "action": "timeout", "count": 2, "window": 10},
            "mentions": {"enabled": True, "action": "timeout", "count": 6, "window": 5},
        },
    })

    trail = [
        dict(hours=12, rule="words", label="Blocked words", action="delete",
             trigger="matched 'free nitro'", user="nitro-shill", channel="general-chat", ok=True,
             result="message deleted"),
        dict(hours=44, rule="caps", label="Excessive caps", action="log",
             trigger="82% capitals over 31 characters", user="tomas", channel="general-chat", ok=True,
             result="logged only"),
        dict(hours=95, rule="invites", label="Discord invites", action="timeout",
             trigger="3 invites inside 10s", user="raidalt", channel="media", ok=True,
             result="timed out for 10m"),
        dict(hours=170, rule="emoji", label="Emoji flood", action="delete",
             trigger="19 emoji in one line", user="priya", channel="bot-spam", ok=True,
             result="message deleted"),
        dict(hours=260, rule="words", label="Blocked words", action="delete",
             trigger="matched 'kys'", user="quin", channel="general-chat", ok=False,
             result="failed — missing Manage Messages"),
        dict(hours=410, rule="mentions", label="Ping flood", action="timeout",
             trigger="9 mentions inside 5s", user="quiet", channel="general-chat", ok=True,
             result="timed out for 10m"),
        dict(hours=700, rule="links", label="Link spam", action="timeout", dry_run=True,
             trigger="4 links inside 5s", user="oskar", channel="media", ok=True,
             result="dry run — nothing changed"),
    ]
    for row in reversed(trail):
        entry = dict(row)
        hours_ago = entry.pop("hours")
        entry["at"] = (now - datetime.timedelta(hours=hours_ago)).isoformat()
        entry.setdefault("dry_run", False)
        dash.automod_config.record_incident(int(guild.id), entry)


_seed_automod_data(GUILD)

# The message tester runs the bot's real detectors, and those only exist after
# setup_anti_raid() has run. That helper also registers gateway handlers, which
# needs a real client, so build the system directly — the detectors themselves
# need nothing from the bot.
try:
    antiraid._anti_raid_system = antiraid.AntiRaidSystem(BOT)
except Exception as exc:  # pragma: no cover - preview-only
    print(f"anti-raid stub unavailable: {exc}")

app = dash.app
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
if not dash.app.secret_key:
    app.secret_key = "preview-only-not-a-real-secret"

SIGNED_IN = GUILD.members[0]

# Saving anything in the preview must not rewrite the real config, so the owner
# access matrix is read from a copy and written back to that copy.
_PREVIEW_PERMS = os.path.join(HERE, "preview-dash-permissions.json")
if not os.path.exists(_PREVIEW_PERMS) and os.path.exists(dash.PERMISSIONS_FILE):
    with open(dash.PERMISSIONS_FILE, encoding="utf-8") as src:
        with open(_PREVIEW_PERMS, "w", encoding="utf-8") as dst:
            dst.write(src.read())
dash.PERMISSIONS_FILE = _PREVIEW_PERMS


@app.before_request
def _preview_login():
    """Sign the preview in as the owner, with the master key already accepted.

    The real panel is gated behind Discord OAuth; this preview exists to look at
    the screens, so the gate is satisfied up front.
    """
    session["member"] = {
        "user_id": str(SIGNED_IN.id),
        "username": SIGNED_IN.name,
        "display_name": SIGNED_IN.display_name,
        "avatar_url": SIGNED_IN.display_avatar.url,
    }
    session["owner_ok"] = True
    session["owner_ok_for"] = str(SIGNED_IN.id)
    session.permanent = True


if __name__ == "__main__":
    print(f"preview stub bot: {BOT.user} · guild {GUILD.name} ({GUILD.id})")
    print(f"listening on http://127.0.0.1:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)
