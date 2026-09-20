// GET /api/directory
//
// The roster behind the Members page. Rows are read with the anon key, so the
// `members` table needs a SELECT policy for anon and the column list has to be
// one anon may see.
//
// The bot drops bots before syncing, so there is no `bot` column to filter on.
//
// One server at a time: the admin panel picks which server the member page
// shows (Owner Panel > Member Management > Member site server) and stores it in
// `panel_settings`; the bot mirrors only that server's roster and stamps every
// row with its id. The filter below is what keeps the other server's people off
// this page. Before the schema in pages/schema.sql has been run there is no
// column to filter on, so the read falls back to the whole table rather than
// answering with an empty roster.

import {
  ok,
  route,
  supa,
} from "../_lib/core.js";

const COLUMNS = [
  "user_id",
  "username",
  "display_name",
  "avatar_url",
  "banner_url",
  "bio",
  "roles",
  "joined_at",
  "created_at",
].join(",");

const HUB_KEY = "hub_guild";

/** The server the member page shows, or null when none has been picked. */
async function siteServer(env) {
  const res = await supa(env, `panel_settings?select=value&key=eq.${HUB_KEY}&limit=1`);
  if (!res.ok || !Array.isArray(res.rows) || !res.rows.length) return null;
  const value = res.rows[0].value;
  if (!value || typeof value !== "object" || !value.guild_id) return null;
  return {
    guild_id: String(value.guild_id),
    guild_name: String(value.guild_name || ""),
  };
}

async function handleGet({ env }) {
  const hub = await siteServer(env);
  const order = "order=display_name.asc";
  const membersPath = `members?select=${COLUMNS}&${order}`
    + (hub ? `&guild_id=eq.${encodeURIComponent(hub.guild_id)}` : "");

  const [scopedRes, profileRes, economyRes] = await Promise.all([
    supa(env, membersPath),
    // The member page saves its own bio / name styling to member_profiles,
    // which the bot never touches. Without this merge those changes stay
    // invisible everywhere the roster is drawn.
    supa(env, "member_profiles?select=user_id,bio,name_color,name_font,name_effect,show_nickname"),
    // The member bot's economy mirror, so the profile panel can show coins and
    // level without a second round trip when somebody clicks a member.
    supa(env, "economy?select=user_id,balance,bank,xp,level,wins,losses,streak"),
  ]);

  let res = scopedRes;
  if (hub && (!res.ok || !Array.isArray(res.rows))) {
    // The column is missing (pages/schema.sql has not been run). Everyone is a
    // better answer than nobody, and the next roster sync stamps the rows anyway.
    res = await supa(env, `members?select=${COLUMNS}&${order}`);
  }

  if (!res.ok || !Array.isArray(res.rows)) {
    // The page falls back to its own Supabase fetch (and then to an empty
    // roster), so an empty list is a safe answer rather than an error.
    return ok({ members: [], hub });
  }

  const profiles = new Map();
  if (profileRes.ok && Array.isArray(profileRes.rows)) {
    for (const row of profileRes.rows) profiles.set(String(row.user_id), row);
  }

  const wallets = new Map();
  if (economyRes.ok && Array.isArray(economyRes.rows)) {
    for (const row of economyRes.rows) {
      wallets.set(String(row.user_id), {
        balance: Number(row.balance || 0),
        bank: Number(row.bank || 0),
        xp: Number(row.xp || 0),
        level: Number(row.level || 1),
        wins: Number(row.wins || 0),
        losses: Number(row.losses || 0),
        streak: Number(row.streak || 0),
        total: Number(row.balance || 0) + Number(row.bank || 0),
      });
    }
  }

  const members = res.rows.map((row) => {
    const profile = profiles.get(String(row.user_id)) || {};
    return {
      user_id: String(row.user_id),
      username: row.username || "",
      display_name: row.display_name || row.username || "",
      avatar_url: row.avatar_url || "",
      banner_url: row.banner_url || "",
      // A bio the member wrote themselves wins over the bot-synced one.
      bio: profile.bio || row.bio || "",
      name_color: profile.name_color || "",
      name_font: profile.name_font || "",
      name_effect: profile.name_effect || "",
      show_nickname: profile.show_nickname !== false,
      economy: wallets.get(String(row.user_id)) || null,
      joined_at: row.joined_at || null,
      created_at: row.created_at || null,
      bot: false,
      roles: Array.isArray(row.roles)
        ? row.roles.map((r) => ({
            id: r?.id ? String(r.id) : undefined,
            name: r?.name || "",
            color: r?.color || null,
            tier: typeof r?.tier === "number" ? r.tier : undefined,
          }))
        : [],
    };
  });

  return ok({ members, hub });
}

export const onRequestGet = route("api/directory", handleGet);
