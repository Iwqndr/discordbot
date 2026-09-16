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
    return offlineResponse();
  }

  // A 502/503 from loca.lt means the tunnel registration is gone — the machine
  // is off, or the tunnel is still starting. Passing that through shows the
  // browser a bare "Bad gateway", which says nothing; this explains it and
  // says where to look.
  if (upstream.status === 502 || upstream.status === 503) {
    console.error(`[panel] tunnel returned ${upstream.status} for ${target}`);
    return offlineResponse(upstream.status);
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

/** A page a person can read, rather than a JSON blob or a raw gateway error. */
function offlineResponse(status = 503) {
  const html = `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel offline</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
         background:#f5f3ef; color:#2b2723; }
  @media (prefers-color-scheme: dark) { body { background:#1a1815; color:#efe9e0; } }
  .card { max-width:430px; padding:32px 30px; border-radius:18px;
          background:rgba(255,255,255,.75); border:1px solid rgba(0,0,0,.07);
          box-shadow:0 12px 40px rgba(0,0,0,.08); text-align:center; }
  @media (prefers-color-scheme: dark) { .card { background:rgba(255,255,255,.04); border-color:rgba(255,255,255,.09); } }
  h1 { font-size:19px; margin:0 0 10px; }
  p { margin:0 0 8px; opacity:.8; }
  code { background:rgba(0,0,0,.06); padding:2px 7px; border-radius:6px; font-size:13px; }
  @media (prefers-color-scheme: dark) { code { background:rgba(255,255,255,.09); } }
</style></head>
<body><div class="card">
  <h1>The admin panel is offline</h1>
  <p>The panel runs on the host machine, and the tunnel to it is not answering.</p>
  <p>Start <code>main.py</code> on that machine and reload this page.</p>
</div></body></html>`;

  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
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
