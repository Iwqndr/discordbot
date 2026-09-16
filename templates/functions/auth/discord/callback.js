// GET /auth/discord/callback — finish the OAuth dance and set the session.
//
// Everything the member page needs about the signed-in user comes back out of
// /api/me on the next request; this function only proves who they are and hands
// them a signed cookie.

import {
  discordToken,
  discordUser,
  hasEnv,
  publicMember,
  readState,
  sessionCookie,
  unsign,
} from "../../_lib/core.js";

function bounce(origin, query) {
  return Response.redirect(`${origin}/?${query}`, 302);
}

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const origin = url.origin;

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
