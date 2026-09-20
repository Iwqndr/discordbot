# Cloudflare Pages Functions for the member page

Everything under `functions/` is what turns the `404`s on
`jamesheston.pages.dev/api/*` into real endpoints. Cloudflare Pages picks the
folder up automatically: no config, no build step, one route per file.

`functions/api/status.js` → `https://jamesheston.pages.dev/api/status`

## Install

1. Copy the whole `functions/` folder into the **root of your Pages repo** —
   the repo that deploys to `jamesheston.pages.dev`, next to the `index.html`
   and `member.html` it already serves.
2. Open `pages/schema.sql`, paste it into the Supabase SQL editor, press Run.
3. Add the environment variables below in the Pages dashboard
   (Settings → Environment variables → Production **and** Preview).
4. Commit and push. Pages deploys the functions with the site. **Every file under
   `functions/` is part of the deploy** — adding `authorize.js` or updating
   `_lib/core.js` and forgetting to copy it is the usual reason a login stops
   working, because the browser only sees a plain 404 from the site.
5. Open `https://jamesheston.pages.dev/api/status` — JSON means it works.
6. Open `https://jamesheston.pages.dev/authorize` — a short page saying it is only
   for panel logins (HTTP 403) means the panel's login route is deployed. A 404
   means it is not, and panel logins will dead-end; the bot's console says so too
   the first time somebody tries to sign in.

## Environment variables

| Name | Value | Notes |
| --- | --- | --- |
| `SUPABASE_URL` | `https://uzebtuzmvvlufuywcxvw.supabase.co` | plain |
| `SUPABASE_ANON_KEY` | the key already in `member.html` | plain |
| `SUPABASE_SERVICE_KEY` | the service role key | **secret** — required; the Worker cannot read or write tickets without it |
| `DISCORD_CLIENT_ID` | your admin bot's client id | plain |
| `DISCORD_CLIENT_SECRET` | your admin bot's client secret | **secret** |
| `SESSION_SECRET` | any long random string (32+ chars) | **secret** |
| `DISCORD_REDIRECT_URI` | `https://jamesheston.pages.dev/auth/discord/callback` | optional — inferred from the request origin if omitted |
| `PANEL_HANDOFF_SECRET` | any long random string | **secret**, optional — see below |

Two redirect URIs go in the Discord application (OAuth2 → Redirects):

| Redirect | Used by |
| --- | --- |
| `/auth/discord/callback` | this site's own login |
| `/authorize` | the admin panel's login |

Without both, the login comes back with `login=failed`.

### Why the panel needs `/authorize`

The admin panel runs on the host machine behind a cloudflared **quick tunnel**,
so its address is new on every restart — a redirect registered for it would have
to be edited in the Discord app every time the bot restarted. Instead the panel
asks Discord for this one fixed address, and `functions/authorize.js` — which is
what serves it — reads the current tunnel address out of Supabase and forwards
the login to it (`<tunnel>/auth/discord/callback`), which is where the panel sets
its own session.

That route only ever finishes a login the panel started. The panel mints a
`state` naming itself as the audience, with an expiry and the path to land on;
`/authorize` refuses anything that is not that shape, so a bare visit to it, or
this site's own login, is told what the page is for rather than being signed in.

`PANEL_HANDOFF_SECRET` is optional and belongs in **both** the Pages environment
and the bot's `.env`: with it, that state is also signed and the signature is
checked, and the panel can additionally be entered straight from
`/dashboard` (see below) without a second Discord round trip. With it set on only
one side, the panel's login simply uses the signed-out flow — nothing breaks.

### Entering the panel from this site

`/dashboard` hands a member who is already signed in here straight into the
panel's `/auth/handoff`, signed with `PANEL_HANDOFF_SECRET`, so they do not have
to authorise with Discord a second time. That is why `/dashboard` has to work out
the panel's current address rather than remembering one.

`SUPABASE_SERVICE_KEY` is what makes "your own tickets only" real. The anon key
is public — it is baked into `member.html` — so if the Worker used it, anyone
could call PostgREST directly and read every ticket. Every table in
`schema.sql` therefore has RLS on with no anon policy, and the Worker reads them
with the service key, applying the session cookie itself.

## Endpoints

| Route | Method | Backs |
| --- | --- | --- |
| `/api/me` | GET | who is signed in, open-ticket count, staff badge |
| `/api/me/profile` | GET / POST | bio, name colour/font/effect |
| `/api/me/history` | GET | the "My dashboard" modal |
| `/api/my-cases` | GET | case picker on the appeal form |
| `/api/status` | GET | status widget (polls every few seconds) |
| `/api/directory` | GET | Members page roster |
| `/api/staff-titles` | GET | name badges |
| `/api/tickets` | GET / POST | your tickets / open a new one |
| `/api/tickets/<id>` | GET | one ticket with replies |
| `/api/tickets/<id>/reply` | POST | send a reply |
| `/api/tickets/<id>/close` | POST | close a ticket |
| `/api/tickets/upload` | POST | image attachments |
| `/auth/discord` | GET | start Discord login |
| `/auth/discord/callback` | GET | finish it, set the session cookie |
| `/auth/logout` | GET | drop the session |
| `/authorize` | GET | finish the **admin panel's** login, then forward it to the live tunnel |
| `/dashboard` | GET | hand a signed-in member into the panel, else show the panel link |

`templates/member.html` is **not modified** by any of this. Every fetch it
already makes now lands on a real route.

## Two things to know

**The member bot still needs `MEMBER_SITE_URL` pointed at this site.** Its
ticket-panel button links to `{MEMBER_SITE_URL}?open=support`, and the page
handles `?open=support` itself — no change needed on the bot side.

**The admin bot needs to mirror tickets into `member_tickets`.** The Worker
writes a ticket to `ticket_queue` (the bot's work queue, unchanged) *and* to
`member_tickets` (what the member page reads back), so a member can follow a
ticket immediately. For staff replies and closures to show up in "My tickets",
the admin bot should append to `member_tickets.replies` and set `status` when it
handles a ticket. Until that loop is closed, the member sees their own side of
the conversation, which is what the page shows today.

**Ticket attachments need the storage bucket.** `schema.sql` creates a public
`ticket-attachments` bucket. If you would rather keep using Catbox, point
`SUPABASE_ANON_KEY` uploads at your own endpoint instead — the page only needs
`{ status: "ok", url }` back.

## What is still not wired

- `dash.data.banned` and `timed_out_until` come back as `null` (the page prints
  `-`). Ban state lives in the bot's own list behind the service key; read it
  there and add a column if you want it on the page.
- `command_usage` and `members` are still fetched straight from Supabase by the
  page with the anon key, so they need SELECT policies for anon — `members`
  already has one, and `command_usage` needs the same.
