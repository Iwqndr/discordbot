// /panel/* — a proxy in front of the localtunnel admin panel.
//
// Why this exists: localtunnel shows a "Tunnel website ahead!" page to real
// browsers unless the request carries a `Bypass-Tunnel-Reminder` header. A
// redirect cannot set a header, so pointing /dashboard straight at loca.lt
// would land every admin on that page first — which reads like a security
// warning and is exactly what we are trying to avoid.
//
// Serving it through a Worker fixes that: the browser talks to Cloudflare, the
// Worker adds the header, and localtunnel hands over the real panel. Nobody
// ever sees the interstitial, and the address stays https://jamesheston.pages.dev/panel
// instead of a random loca.lt hostname.
//
// The tunnel address is read from the `bot_status` row with id 'tunnel', which
// main.py keeps up to date.

import { currentUser, fail, supa } from "../_lib/core.js";

const HOP_BY_HOP = new Set([
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade", "host",
]);

function allowedRoleIds(env) {
  return String(env.PANEL_ROLE_IDS ?? "")
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

async function gate(request, env) {
  const uid = await currentUser(request, env);
  if (!uid) return { error: fail("Sign in with Discord first.", 401) };

  const roleIds = allowedRoleIds(env);
  if (!roleIds.length) return { uid };

  const member = await supa(
    env,
    `members?user_id=eq.${encodeURIComponent(uid)}&select=roles&limit=1`
  );
  const roles = Array.isArray(member.rows) && member.rows[0]?.roles ? member.rows[0].roles : [];
  const mine = new Set(roles.map((r) => String(r?.id ?? "")));
  if (!roleIds.some((id) => mine.has(id))) {
    return { error: fail("Permissions are required to access this panel.", 403) };
  }
  return { uid };
}

async function tunnelBase(env) {
  const res = await supa(env, "bot_status?id=eq.tunnel&select=version&limit=1");
  const row = Array.isArray(res.rows) ? res.rows[0] : null;
  const base = String(row?.version ?? "").trim().replace(/\/+$/, "");
  return base || null;
}

async function handle(context) {
  const { request, env } = context;
  const gated = await gate(request, env);
  if (gated.error) return gated.error;

  const base = await tunnelBase(env);
  if (!base) {
    return fail("The admin panel is offline. Start main.py on the host machine and try again.", 503);
  }

  const inbound = new URL(request.url);
  // /panel/roles -> <tunnel>/roles, so every panel asset keeps working.
  const suffix = inbound.pathname.replace(/^\/panel/, "");
  const target = `${base}${suffix || "/"}${inbound.search}`;

  const headers = new Headers();
  for (const [key, value] of request.headers) {
    if (!HOP_BY_HOP.has(key.toLowerCase())) headers.set(key, value);
  }
  // The whole point of the proxy. Without it localtunnel serves its reminder.
  headers.set("Bypass-Tunnel-Reminder", "true");
  headers.set("X-Forwarded-Host", inbound.host);
  headers.set("X-Forwarded-Proto", "https");

  const init = {
    method: request.method,
    headers,
    redirect: "manual", // let the browser follow the panel's own redirects
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  let upstream;
  try {
    upstream = await fetch(target, init);
  } catch (err) {
    console.error(`[panel] upstream failed for ${target}: ${err}`);
    return fail("The admin panel did not answer. It may have just restarted.", 502);
  }

  const out = new Headers();
  for (const [key, value] of upstream.headers) {
    if (!HOP_BY_HOP.has(key.toLowerCase())) out.append(key, value);
  }
  // Localtunnel's own framing headers would break the page under our origin.
  out.delete("content-security-policy");
  out.delete("content-encoding");
  out.delete("content-length");
  out.set("Cache-Control", "no-store");

  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const onRequest = async (context) => {
  try {
    return await handle(context);
  } catch (err) {
    console.error(`[/panel] ${err && err.stack ? err.stack : String(err)}`);
    return fail("Could not reach the admin panel.", 502);
  }
};
