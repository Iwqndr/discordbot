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

export async function panelGate(request, env, { wantsHtml = false } = {}) {
  const ownerConfigured = Boolean(env.PANEL_OWNER_KEY);
  const roleIds = allowedRoleIds(env);

  // Neither gate configured: nobody gets in. A missing setting should close the
  // door rather than quietly let every signed-in member through.
  if (!ownerConfigured && !roleIds.length) return { error: lockedOut(wantsHtml) };

  if (ownerConfigured) {
    if (await ownerUnlocked(request, env)) return { owner: true };
    // Role access is still honoured when both are configured.
    if (!roleIds.length) return { error: lockedOut(wantsHtml) };
  }

  const uid = await currentUser(request, env);
  if (!uid) return { error: fail("Sign in with Discord first.", 401) };

  if (roleIds.length) {
    const member = await supa(
      env,
      `members?user_id=eq.${encodeURIComponent(uid)}&select=roles&limit=1`
    );
    const roles = Array.isArray(member.rows) && member.rows[0]?.roles ? member.rows[0].roles : [];
    const mine = new Set(roles.map((r) => String(r?.id ?? "")));
    if (!roleIds.some((id) => mine.has(id))) {
      return { error: fail("Permissions are required to access this panel.", 403) };
    }
  }

  return { uid };
}

/** A page pointing at the key box, or a plain 403 for a programmatic caller. */
function lockedOut(wantsHtml) {
  if (!wantsHtml) return fail("Owner key required.", 403);
  const html = `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Not available</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
         background:#f5f3ef; color:#2b2723; }
  @media (prefers-color-scheme: dark) { body { background:#1a1815; color:#efe9e0; } }
  .card { max-width:400px; padding:30px 28px; border-radius:18px; text-align:center;
          background:rgba(255,255,255,.75); border:1px solid rgba(0,0,0,.07); }
  @media (prefers-color-scheme: dark) { .card { background:rgba(255,255,255,.04); border-color:rgba(255,255,255,.09); } }
  h1 { font-size:18px; margin:0 0 8px; }
  p { margin:0; opacity:.8; }
</style></head>
<body><div class="card">
  <h1>Not available here</h1>
  <p>Open <a href="/dashboard">/dashboard</a> instead.</p>
</div></body></html>`;
  return new Response(html, {
    status: 403,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}

export async function tunnelBase(env) {
  const res = await supa(env, "bot_status?id=eq.tunnel&select=version,updated_at&limit=1");
  const row = Array.isArray(res.rows) ? res.rows[0] : null;
  const base = String(row?.version ?? "").trim().replace(/\/+$/, "");
  if (!base) return null;
  return { url: base, seenAt: row?.updated_at || null };
}

export async function proxyToPanel(request, env, { stripPrefix, base = null }) {
  const tunnel = base || (await tunnelBase(env));
  if (!tunnel) {
    return offlineResponse(503, "The host machine has not published a tunnel address yet.");
  }
  const tunnelUrl = typeof tunnel === "string" ? tunnel : tunnel.url;
  const seenAt = typeof tunnel === "string" ? null : tunnel.seenAt;

  const inbound = new URL(request.url);
  const target = `${tunnelUrl}${inbound.pathname.replace(stripPrefix, "") || "/"}${inbound.search}`;

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
    return offlineResponse(502, describe(tunnelUrl, seenAt, String(err)));
  }

  // 5xx from the tunnel provider means its registration is gone — the machine is
  // off, or the tunnel is still starting. Passing that through shows the browser
  // a bare "Bad gateway", which says nothing; this explains it and says where
  // to look.
  if (upstream.status >= 500) {
    console.error(`[panel] tunnel returned ${upstream.status} for ${target}`);
    return offlineResponse(503, describe(tunnelUrl, seenAt, `upstream HTTP ${upstream.status}`));
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

  // The panel's own redirects are root-absolute (`/admin`, `/auth/discord`).
  // Sent as-is the browser leaves the proxy and lands on a 404 — which is
  // exactly what "/admin?login=ok could not be found" was. Keeping them inside
  // /panel holds the browser on a path this Worker serves.
  const location = out.get("Location");
  if (
    location &&
    location.startsWith("/") &&
    !location.startsWith("//") &&
    !location.startsWith("/panel")
  ) {
    out.set("Location", `/panel${location}`);
  }

  return new Response(upstream.body, { status: upstream.status, headers: out });
}

/** One short line of context, so a failure is diagnosable from the page. */
function describe(url, seenAt, reason) {
  let host = "none";
  try {
    host = url ? new URL(url).host : "none";
  } catch {
    host = url || "none";
  }
  let when = "never";
  if (seenAt) {
    const seconds = Math.max(0, Math.round((Date.now() - new Date(seenAt).getTime()) / 1000));
    when = seconds < 90 ? `${seconds}s ago` : `${Math.round(seconds / 60)}m ago`;
  }
  return `${host} · last seen ${when} · ${reason}`;
}

/** A page a person can read, rather than a JSON blob or a raw gateway error. */
function offlineResponse(status = 503, detail = "") {
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
  .card { max-width:460px; padding:32px 30px; border-radius:18px;
          background:rgba(255,255,255,.75); border:1px solid rgba(0,0,0,.07);
          box-shadow:0 12px 40px rgba(0,0,0,.08); text-align:center; }
  @media (prefers-color-scheme: dark) { .card { background:rgba(255,255,255,.04); border-color:rgba(255,255,255,.09); } }
  h1 { font-size:19px; margin:0 0 10px; }
  p { margin:0 0 8px; opacity:.8; }
  code { background:rgba(0,0,0,.06); padding:2px 7px; border-radius:6px; font-size:13px; }
  @media (prefers-color-scheme: dark) { code { background:rgba(255,255,255,.09); } }
  .detail { margin-top:16px; padding-top:14px; border-top:1px solid rgba(0,0,0,.07);
            font-size:11.5px; opacity:.55; word-break:break-all; }
  @media (prefers-color-scheme: dark) { .detail { border-top-color:rgba(255,255,255,.1); } }
</style></head>
<body><div class="card">
  <h1>The admin panel is offline</h1>
  <p>The panel runs on the host machine, and the tunnel to it is not answering.</p>
  <p>Start <code>main.py</code> on that machine and reload this page.</p>
  ${detail ? `<div class="detail">${detail}</div>` : ""}
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
