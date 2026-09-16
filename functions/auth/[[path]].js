// /auth/* fallback — the admin panel's own Discord login, through our origin.
//
// The panel's login button links to `/auth/discord`, and Discord sends the
// browser back to whatever `DISCORD_REDIRECT_URI` says. Left alone that is the
// loca.lt address, so the panel's session cookie is written for the tunnel
// host — and our proxy only forwards Cloudflare's cookie, never the panel's, so
// a browser sitting on jamesheston.pages.dev never sees the login.
//
// Pointing `DISCORD_REDIRECT_URI` at this origin fixes it: the callback comes
// back here, is forwarded down the tunnel, and the panel's Set-Cookie lands on
// the domain the admin is actually browsing.
//
// `functions/auth/discord.js` and `auth/logout.js` are real files, so they win
// over this catch-all and the member page's own login is unaffected.

import { cameFromPanel, proxyToPanel } from "../_lib/panel.js";

/** The panel's callback, which must be served even when the referer is absent. */
function isPanelCallback(pathname) {
  return pathname.replace(/\/+$/, "").endsWith("/auth/discord/callback");
}

export async function onRequest(context) {
  const { request, env } = context;
  const pathname = new URL(request.url).pathname;

  // A cross-site return from Discord may arrive with no Referer at all, so the
  // callback path is trusted explicitly. Everything else must prove it came
  // from a panel page.
  if (!isPanelCallback(pathname) && !cameFromPanel(request)) {
    return new Response("Not Found", { status: 404 });
  }

  try {
    return await proxyToPanel(request, env, { stripPrefix: "" });
  } catch (err) {
    console.error(`[/auth passthrough] ${err && err.stack ? err.stack : String(err)}`);
    return new Response("Panel login is unavailable.", { status: 502 });
  }
}
