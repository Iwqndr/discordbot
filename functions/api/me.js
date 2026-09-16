// GET /api/me
//
// The member page calls this on every load: it decides whether the "Log in"
// button is usable and who the visitor is. It must never fail hard, because a
// 404 here is what made the page look broken before.

import {
  currentUser,
  hasEnv,
  ok,
  route,
  supa,
  titleForRoles,
} from "../_lib/core.js";

async function openTicketCount(env, uid) {
  const res = await supa(
    env,
    `member_tickets?discord_user_id=eq.${encodeURIComponent(uid)}&status=neq.closed&select=ticket_id`
  );
  if (!res.ok || !Array.isArray(res.rows)) return 0;
  return res.rows.length;
}

async function onRequestGet({ request, env }) {
  const oauthReady = hasEnv(env, "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "SESSION_SECRET");
  const uid = await currentUser(request, env);

  if (!uid) {
    return ok({ oauth_ready: oauthReady, logged_in: false, user: null, open_count: 0, staff_title: null });
  }

  const [memberRes, titleRes, openCount] = await Promise.all([
    supa(
      env,
      `members?user_id=eq.${encodeURIComponent(uid)}&select=user_id,username,display_name,avatar_url,banner_url,bio,roles&limit=1`
    ),
    supa(env, "staff_titles?select=*"),
    openTicketCount(env, uid),
  ]);

  const row = Array.isArray(memberRes.rows) ? memberRes.rows[0] : null;
  // A member the bot has never synced still gets a usable session; they just
  // show up without a synced avatar or nickname.
  const user = row
    ? {
        user_id: String(row.user_id),
        username: row.username || "",
        display_name: row.display_name || row.username || "",
        avatar_url: row.avatar_url || "",
      }
    : { user_id: uid, username: "", display_name: "Member", avatar_url: "" };

  const staffTitle = titleForRoles(titleRes.rows || [], row?.roles || []);

  return ok({
    oauth_ready: oauthReady,
    logged_in: true,
    user: { ...user, staff_title: staffTitle },
    open_count: openCount,
    staff_title: staffTitle,
  });
}

export const onRequestGet = route("api/me", onRequestGet);
