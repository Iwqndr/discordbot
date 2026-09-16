// GET /api/me/history
//
// Backs the "My dashboard" modal: the account record, punishment counts, the
// action log and the caller's own tickets.
//
// Anything the anon key cannot read (bans, timeouts) is reported honestly as
// unknown instead of guessed at.

import {
  currentUser,
  fail,
  ok,
  route,
  shapeTicket,
  supa,
} from "../../_lib/core.js";

async function onRequestGet({ request, env }) {
  const uid = await currentUser(request, env);
  if (!uid) return fail("Log in with Discord first.", 401);

  const enc = encodeURIComponent(uid);
  const [memberRes, casesRes, ticketRes] = await Promise.all([
    supa(
      env,
      `members?user_id=eq.${enc}&select=user_id,username,display_name,avatar_url,joined_at,created_at,roles&limit=1`
    ),
    supa(
      env,
      `mod_cases?user_id=eq.${enc}&select=id,action,reason,moderator,timestamp&order=timestamp.desc&limit=100`
    ),
    supa(
      env,
      `member_tickets?discord_user_id=eq.${enc}&select=*&order=created_at.desc&limit=50`
    ),
  ]);

  const member = Array.isArray(memberRes.rows) ? memberRes.rows[0] : null;
  const cases = Array.isArray(casesRes.rows) ? casesRes.rows : [];
  const tickets = Array.isArray(ticketRes.rows) ? ticketRes.rows.map(shapeTicket) : [];

  const counts = { warns: 0, kicks: 0, bans: 0, timeouts: 0 };
  for (const row of cases) {
    const action = String(row.action || "").toLowerCase();
    if (action.includes("warn")) counts.warns += 1;
    else if (action.includes("kick")) counts.kicks += 1;
    else if (action.includes("ban")) counts.bans += 1;
    else if (action.includes("timeout") || action.includes("mute")) counts.timeouts += 1;
  }

  return ok({
    user_id: uid,
    display_name: member?.display_name || member?.username || "Member",
    avatar_url: member?.avatar_url || "",
    joined_at: member?.joined_at || null,
    account_created: member?.created_at || null,
    // The bot's ban list lives behind the service key, so this page does not
    // pretend to know. `null` means "not checked", not "not banned".
    banned: null,
    timed_out_until: null,
    counts,
    actions: cases.map((row) => ({
      action: row.action || "Action",
      reason: row.reason || "",
      moderator: row.moderator || "",
      timestamp: row.timestamp || null,
    })),
    tickets,
  });
}

export const onRequestGet = route("api/me/history", onRequestGet);
