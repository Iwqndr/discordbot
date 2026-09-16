// GET /api/directory
//
// The roster behind the Members page. Rows are read with the anon key, so the
// `members` table needs a SELECT policy for anon and the column list has to be
// one anon may see.
//
// The bot drops bots before syncing, so there is no `bot` column to filter on.

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

async function handleGet({ env }) {
  const [res, profileRes] = await Promise.all([
    supa(env, `members?select=${COLUMNS}&order=display_name.asc`),
    // The member page saves its own bio / name styling to member_profiles,
    // which the bot never touches. Without this merge those changes stay
    // invisible everywhere the roster is drawn.
    supa(env, "member_profiles?select=user_id,bio,name_color,name_font,name_effect,show_nickname"),
  ]);

  if (!res.ok || !Array.isArray(res.rows)) {
    // The page falls back to its own Supabase fetch (and then to an empty
    // roster), so an empty list is a safe answer rather than an error.
    return ok({ members: [] });
  }

  const profiles = new Map();
  if (profileRes.ok && Array.isArray(profileRes.rows)) {
    for (const row of profileRes.rows) profiles.set(String(row.user_id), row);
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

  return ok({ members });
}

export const onRequestGet = route("api/directory", handleGet);
