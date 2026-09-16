// GET /auth/discord — start the OAuth dance.
//
// The member page sends people here from the "Log in" button. If the two
// Discord credentials are missing we bounce straight back with
// `?login=unavailable`, which is the message member.html already knows how to
// show.

import { hasEnv, redirectUri, routeRedirect, sign, stateCookie } from "../_lib/core.js";

async function startLogin({ request, env }) {
  const origin = new URL(request.url).origin;
  const next = new URL(request.url).searchParams.get("next") || "/";

  if (!hasEnv(env, "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "SESSION_SECRET")) {
    return Response.redirect(`${origin}/?login=unavailable`, 302);
  }

  // `prompt=none` is deliberately absent: it suppresses the consent screen, so
  // Discord answers with an error for anyone who has not authorised this app
  // before — which is every first-time login.
  const state = await sign(env, { nonce: crypto.randomUUID(), next, at: Date.now() });
  const params = new URLSearchParams({
    client_id: env.DISCORD_CLIENT_ID,
    redirect_uri: redirectUri(request, env),
    response_type: "code",
    scope: "identify",
    state: state || "",
  });

  return new Response(null, {
    status: 302,
    headers: {
      Location: `https://discord.com/api/oauth2/authorize?${params.toString()}`,
      "Set-Cookie": stateCookie(state || ""),
      "Cache-Control": "no-store",
    },
  });
}

export const onRequestGet = routeRedirect("auth/discord", startLogin);
