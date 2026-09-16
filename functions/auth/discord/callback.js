// GET /auth/discord/callback — finish the OAuth dance and set the session.
//
// Everything the member page needs about the signed-in user comes back out of
// /api/me on the next request; this function only proves who they are and hands
// them a signed cookie.

import {
  avatarUrl,
  discordToken,
  discordUser,
  hasEnv,
  readState,
  sessionCookie,
  sign,
  supa,
  unsign,
} from "../../_lib/core.js";

function bounce(origin, query) {
  return Response.redirect(`${origin}/?${query}`, 302);
}

/**
 * Make sure the members table has a row for the account that just logged in.
 *
 * Insert only: the bot's `>sync` is the writer of record for nicknames, roles
 * and bios, so an existing row is left exactly as it is. This exists for the
 * case the bot has never seen someone, where the member page would otherwise
 * have no name to show and fall back to a bare "Member".
 *
 * A failure is logged and ignored — a missing display name must never cost
 * somebody their login.
 */
async function rememberMember(env, user) {
  const uid = encodeURIComponent(String(user.id));
  const existing = await supa(env, `members?user_id=eq.${uid}&select=user_id&limit=1`);
  if (existing.ok && Array.isArray(existing.rows) && existing.rows.length) return;

  const row = {
    user_id: String(user.id),
    username: user.username || "",
    display_name: user.global_name || user.username || "",
    avatar_url: avatarUrl(user),
    roles: [],
  };
  const res = await supa(env, "members", { method: "POST", body: row });
  if (!res.ok) {
    console.warn(`members insert failed for ${row.user_id}: ${res.status} ${res.text}`);
  }
}

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const origin = url.origin;

  // Nothing below may escape: an uncaught throw here is a Cloudflare 1101
  // ("Worker threw exception") instead of a login the member can retry.
  try {
    return await finishLogin(request, env, url, origin);
  } catch (err) {
    console.error("discord callback failed:", err && err.stack ? err.stack : String(err));
    return bounce(origin, "login=failed");
  }
}

async function finishLogin(request, env, url, origin) {
  if (!hasEnv(env, "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "SESSION_SECRET")) {
    return bounce(origin, "login=unavailable");
  }

  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const cookieState = readState(request);
  const expected = await unsign(env, cookieState);

  if (!code || !state || !cookieState || !expected) {
    return bounce(origin, "login=failed");
  }
  // The state on the query string must be byte-identical to the signed cookie
  // this browser was handed when the login started.
  if (state !== cookieState) {
    return bounce(origin, "login=failed");
  }
  if (Date.now() - Number(expected.at || 0) > 10 * 60 * 1000) {
    return bounce(origin, "login=failed");
  }

  const token = await discordToken(request, env, code);
  const user = token?.access_token ? await discordUser(token.access_token) : null;
  if (!user?.id) return bounce(origin, "login=failed");

  // Remember who this is. The bot's `>sync` only fills the members table when
  // somebody runs it, so without this the member page has no row to name the
  // visitor with and falls back to a bare "Member".
  await rememberMember(env, user);

  const session = await sign(env, { uid: String(user.id), at: Date.now() });
  if (!session) return bounce(origin, "login=failed");

  const next = String(expected.next || "/");
  const safeNext = next.startsWith("/") && !next.startsWith("//") ? next : "/";

  return new Response(null, {
    status: 302,
    headers: {
      Location: `${origin}${safeNext}${safeNext.includes("?") ? "&" : "?"}login=ok`,
      "Set-Cookie": sessionCookie(session),
      "Cache-Control": "no-store",
    },
  });
}
