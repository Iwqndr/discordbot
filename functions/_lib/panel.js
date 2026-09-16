// Shared proxy for the admin panel tunnel.
//
// The panel is a Flask app on the operator's machine, published through
// localtunnel. Two things have to be true for it to work under our own origin:
//
//   1. the request must carry `Bypass-Tunnel-Reminder`, or localtunnel shows a
//      browser a "Tunnel website ahead!" interstitial — a redirect cannot set
//      a header, which is why a proxy is needed at all;
//   2. the panel's own root-absolute paths (`/api/stats`, `/admin`, `/git`)
//      must also land on the tunnel, not on this site. The browser resolves
//      them against jamesheston.pages.dev, so they arrive here as ordinary
//      requests and are forwarded on by path.

import { currentUser, fail, supa } from "./core.js";

const HOP_BY_HOP = new Set([
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade", "host",
]);

/** Discord role ids allowed through. Empty means "any signed-in member". */
function allowedRoleIds(env) {
  return String(env.PANEL_ROLE_IDS ?? "")
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

export async function panelGate(request, env) {
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

export async function tunnelBase(env) {
  const res = await supa(env, "bot_status?id=eq.tunnel&select=version,updated_at&limit=1");
  const row = Array.isArray(res.rows) ? res.rows[0] : null;
  const base = String(row?.version ?? "").trim().replace(/\/+$/, "");
  return base || null;
}

/**
 * Forward `request` to the tunnel.
 *
 * `stripPrefix` is removed from the incoming path first: a request to
 * `/panel/admin` becomes `<tunnel>/admin`. Pass "" when the path already
 * matches the panel's own (for `/api/...`).
 */
export async function proxyToPanel(request, env, { stripPrefix, base = null }) {
  const tunnel = base || (await tunnelBase(env));
  if (!tunnel) {
    return fail("The admin panel is offline. Start main.py on the host machine and try again.", 503);
  }

  const inbound = new URL(request.url);
  const target = `${tunnel}${inbound.pathname.replace(stripPrefix, "") || "/"}${inbound.search}`;

  const headers = new Headers();
  for (const [key, value] of request.headers) {
    if (!HOP_BY_HOP.has(key.toLowerCase())) headers.set(key, value);
  }
  // The whole point of the proxy: without this localtunnel shows its reminder.
  headers.set("Bypass-Tunnel-Reminder", "true");
  headers.set("X-Forwarded-Host", inbound.host);
  headers.set("X-Forwarded-Proto", "https");

  const init = { method: request.method, headers, redirect: "manual" };
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
  // Localtunnel's framing headers would break the page under our origin.
  out.delete("content-security-policy");
  out.delete("content-encoding");
  out.delete("content-length");
  out.set("Cache-Control", "no-store");

  return new Response(upstream.body, { status: upstream.status, headers: out });
}

/** True when this request is a sub-resource of a page served from /panel. */
export function cameFromPanel(request) {
  const referer = request.headers.get("Referer") || "";
  if (!referer) return false;
  try {
    return new URL(referer).pathname.startsWith("/panel");
  } catch {
    return referer.includes("/panel");
  }
}
