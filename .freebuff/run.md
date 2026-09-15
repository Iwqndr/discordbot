# Running this project (and its live preview)

## 1. Reproducing the artifacts a fresh checkout needs

Nothing is generated: the app runs straight from the working tree.

- **Python 3.13/3.14** with the packages in `requirements.txt` already installed
  (`discord.py` 2.7, `Flask` 3.x, `psutil`). Check with
  `python -c "import discord, flask, psutil"`.
- **`.env`** — copied from the main checkout (never committed; `.gitignore` has
  it). `main.py` refuses to start without `DISCORD_TOKEN`; the preview server
  below does **not** need it.
- Runtime `*.json` files (`dash_permissions.json`, `tickets.json`,
  `moderation_history.json`, …) are gitignored state. They are created on first
  use; copy them from the main checkout if you want the same starting data.
- The project is a Discord bot, not a Node app — there is no `npm install`.

## 2. Running the servers

### The real one (bot + dashboard, port 5000)

```bash
python main.py        # or update.bat on Windows
```

`main.py` starts the bot, then Flask on `http://localhost:5000` (the member page
is `/member`). It runs with `debug=False, use_reloader=False`, so **Jinja caches
templates in the process** — restart it after editing anything in `templates/`.
`close_previous_instances()` kills an earlier `main.py` on startup, so do not
run two at once against the same bot token.

### The preview stub (no bot needed, port 5055)

```bash
python .freebuff/preview_server.py
```

Runs the real `dashboard.py` app and the real templates against a fake Discord
guild (10 members, 11 channels, 11 roles, the real role ids from
`dash_permissions.json`), signs itself in as the owner, and forces
`TEMPLATES_AUTO_RELOAD` on so edits to `templates/dashboard.html` show up on
refresh. It listens on `127.0.0.1:5055` — deliberately not 5000, so it never
collides with the instance the bot is running on.

- It writes owner-access saves to `.freebuff/preview-dash-permissions.json`
  (copied from `dash_permissions.json` on first run) so a preview never rewrites
  the real config. Delete that copy to start fresh.
- Fake data lives in `build_guild()` in that file. Add members/channels/roles
  there; role ids that appear in `dash_permissions.json` resolve to that saved
  access, and ids that do not resolve to the new derived-from-Discord default.
- `seed_automod_data()` fills the pages that read live Discord state, because a
  fresh checkout renders every one of them empty and they cannot be reviewed:
  three bans, four invites (one expiring, one already expired), four members
  across two voice rooms (muted / deafened / streaming), five armed rules, four
  blocked words and a seven-entry incident trail. It is applied on every start,
  and `reset` clears the trail first so restarts do not stack.
- AutoMod settings saved in the preview go to `.freebuff/preview-automod.json`,
  not `automod_config.json`, for the same reason the permissions copy exists.
- The channel flags follow the same rule: `.freebuff/preview-media-only.json`
  and `.freebuff/preview-selfpromo.json` stand in for `media_only_channels.json`
  and `selfpromo_channels.json`, so flagging a channel in the preview never
  changes what the running bot enforces. Delete those two files to start from
  an empty list.
- The stub also builds an `AntiRaidSystem` directly. The message tester runs the
  bot's real detectors and those only exist once the bot starts; going through
  `setup_anti_raid()` is not possible here because it registers gateway
  handlers, which needs a real client.
- Cases and tickets get preview stand-ins too:
  `.freebuff/preview-mod-cases.json` stands in for `mod_cases.json`, and
  `.freebuff/preview-tickets.json` for `tickets.json`, so a punishment handed
  out or a ticket sent while reviewing never touches what the bot enforces.
  Both are **seeded on first run** because several surfaces render nothing at
  all without them: the ticket console's APPLICATION badge and its case chip,
  the punishment log's case chips, and the member page's `Your cases` picker.
  Delete either file to re-seed from scratch.
- The seed deliberately gives cases to **both** `guild.members[0]` (the account
  the preview signs in as) and `guild.members[1]`. The signed-in account needs
  its own or `/api/my-cases` — and therefore the appeal form's case picker —
  comes up empty. The seeded tickets belong to that same account so they also
  appear in the member page's *My tickets* list, and the appeal quotes a case
  the sender actually owns.
- The case seed and the ticket seed share one store instance on purpose: a
  second `CaseStore` built after the seed would read its own copy of the file
  and the bound store would never see those records.

### Restarting after a change to a `.py` file

`TEMPLATES_AUTO_RELOAD` only covers the templates. A change to `dashboard.py`,
`antiraid.py` or the stub itself needs the process restarted, or new routes 404:
stop the pid listening on 5055, start it again with the same command, then
re-register the preview with the new pid.
