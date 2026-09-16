# Admin panel rebuild — working folder

Nothing here is live. The panel that runs today is still:

- `templates/dashboard.html` — the UI
- `referenced/dashboard.py` — the Flask routes
- served by the bot at `http://<your-ip>:5000/admin`

Both are byte-for-byte preserved here as `.orig` files, so nothing can be lost
while the rebuild happens.

## Why rebuild rather than patch

The current panel only works while the bot process is running on your PC. You
want your admins to reach it at `jamesheston.pages.dev/dashboard` without your
machine being on. Cloudflare Pages cannot run Python — it serves static files
and JavaScript Workers — so the panel has to become a static page talking to
Worker endpoints, the same way the member page already works.

That also removes the whole class of bug you hit repeatedly: the owner key,
`dash_permissions.json` and the per-browser state only exist because the panel
is a local Flask app. On Pages, permissions are checked server-side against
your Discord roles, and there is nothing to lose on refresh.

## What already exists to build on

| Piece | Where | State |
| --- | --- | --- |
| Discord login + signed session | `functions/auth/*` | live |
| Supabase access with the service key | `functions/_lib/core.js` | live |
| Members + economy + profiles | `/api/directory`, `/api/me` | live |
| Staff titles / badges | `/api/staff-titles` | live, empty table |
| Tickets (list, create, reply, close) | `/api/tickets*` | live |
| Moderation history | `/api/my-cases`, `/api/me/history` | live |

What is missing is the **admin side**: endpoints that read *everyone's* data
rather than the caller's own, each guarded by a role check.

## Shape of the rebuild

```
templates/dashboard/
  index.html          the new panel UI (start from the member page's theme)
  panel.js            UI logic, calls /api/admin/*
  panel.css

functions/api/admin/
  _guard.js           requireStaff(request, env) -> { uid, roles, perms } or 403
  overview.js         counts, bot status, recent activity
  members.js          full roster, search, filters, pagination
  member.js           one member: roles, notes, history, tickets
  mod.js              ban / kick / timeout / softban / warn  (+ logs to mod_cases)
  roles.js            list roles, edit permissions (Discord API via bot token)
  channels.js         list channels, permission overwrites
  webhooks.js         list / create / move webhooks
  tickets.js          the staff queue: accept, close, reply
  automod.js          rules
  verification.js     the verify panel config
  audit.js            mod log
```

### The guard

Every admin route starts with the same check, and it reads the caller's roles
from the `members` table that the bot already syncs:

1. `currentUser(request, env)` — are they signed in?
2. Look up their roles, take the **highest** tier, and resolve the capability
   set from a `dashboard_roles` table (one row per Discord role id).
3. No matching role → `403 {"message": "Permissions are required to access this panel."}`

That kills the "Member role blocks my Admin role" bug you hit, because it takes
the highest role rather than the first one that matches.

### Discord writes

Anything that changes Discord state (ban, kick, timeout, role edits, webhook
moves) needs a **bot token** on the Worker. That is `env.DISCORD_TOKEN` — a
Pages secret. The REST calls are plain `fetch`, no library needed:

```
PATCH  /guilds/{guild}/members/{user}          timeout / roles
PUT    /guilds/{guild}/bans/{user}             ban
DELETE /guilds/{guild}/bans/{user}             unban
DELETE /guilds/{guild}/members/{user}          kick
PATCH  /channels/{channel}                     permissions, bitrate, user limit
```

Two consequences worth deciding before this is built:

- **A bot token in the Worker can do anything the bot can do.** The capability
  check in step 2 is the only thing standing between an admin and a full
  nuke, so it has to be right, and the owner key should become a real role
  rather than a string in local storage.
- **Bitrate bug you hit** (`Invalid Form Body In bitrate: int32 value should be
  greater than or equal to 8000`) is simply a missing clamp — voice bitrate
  must be 8000–96000, and user limit 0–99. Any channel PATCH here clamps both.

## Order of work

Each phase is shippable on its own; nothing later depends on anything earlier
being finished.

1. **Scaffold** — `index.html` + login gate + the overview tab, reading
   `/api/admin/overview`. Proves the Pages-hosted panel works end to end.
2. **Members** — the roster table, search, and a member detail view reusing the
   member page's profile panel.
3. **Mod View** — the punishment bundle (ban / kick / softban / timeout / warn)
   with reason and duration, writing to `mod_cases` and calling the Discord API.
   This needs the bot token.
4. **Tickets** — the staff queue, accept + close, which also closes the loop the
   member page is waiting on (staff replies appearing in "My tickets").
5. **Roles** — the permission editor with categories, descriptions, and the
   role selector side panel you described.
6. **Channels & Broadcast** — the category tree, per-type settings (text vs
   voice vs voice-chat), and the permission editor button.
7. **Webhooks, automod, verification, audit** — the rest.

## What I need from you to start

1. Which **Discord role** counts as staff for the panel (name or id), and
   whether any role should be read-only.
2. Whether the admin bot's **token** can go into Cloudflare as a secret. Without
   it, phases 3, 5 and 6 can only *read* — no bans, no role edits, no channel
   changes.
3. Whether you want the panel at `/dashboard` (as asked) — note that a folder
   named `dashboard/` inside `templates/` would also publish `/dashboard/` as a
   static path once Pages picks it up, so I will build the new UI at
   `templates/panel/` and route `/dashboard` to it, to avoid serving this
   working folder by accident.
