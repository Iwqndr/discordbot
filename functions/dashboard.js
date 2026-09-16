// Panel link page for /dashboard.
//
// This deliberately does NOT proxy the admin panel. An earlier version did, and
// it fought the panel the whole way: its session cookies landed on the wrong
// domain, its redirects to `/admin` escaped the proxy, and its `/api/*` calls
// needed their own passthrough. Opening the panel at its own address removes
// every one of those problems, because the panel is then simply on the host it
// thinks it is on.
//
// So this page does one job: it reads the tunnel address the host machine
// published and offers it as a link.

import { currentUser, supa } from "./_lib/core.js";

/** The address `main.py` last published, or null. */
export async function tunnelInfo(env) {
  const res = await supa(env, "bot_status?id=eq.tunnel&select=version,updated_at&limit=1");
  const row = Array.isArray(res.rows) ? res.rows[0] : null;
  const url = String(row?.version ?? "").trim().replace(/\/+$/, "");
  if (!url) return null;
  return { url, seenAt: row?.updated_at || null };
}

function toHex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * The panel's signature over `uid.expires`, as hex HMAC-SHA256.
 *
 * The panel computes the same string with `hmac.new(secret, msg, sha256)` and
 * compares, so this has to match that exactly rather than invent a second
 * scheme. The secret is `PANEL_HANDOFF_SECRET`, held by both sides.
 */
async function handoffSignature(env, uid, expires) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(env.PANEL_HANDOFF_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const mac = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(`${uid}.${expires}`)
  );
  return toHex(mac);
}

/**
 * Redirect straight into the panel, already authenticated.
 *
 * Without this a member who signed in on the member page has to authorise with
 * Discord a second time to open the panel, because the two keep separate
 * sessions on separate origins. The panel verifies this signature and creates
 * its session from it, so one login covers both.
 *
 * Returns null when the handoff is not configured or the member is not signed
 * in, so the caller can fall back to the plain link page.
 */
async function handoffUrl(request, env, tunnelUrl, next = "/admin") {
  if (!env.PANEL_HANDOFF_SECRET) return null;

  const uid = await currentUser(request, env);
  if (!uid) return null;

  // Only local panel paths, so this can never be used as an open redirect.
  const target = next.startsWith("/") && !next.startsWith("//") ? next : "/admin";

  const expires = String(Math.floor(Date.now() / 1000) + 120);
  const sig = await handoffSignature(env, uid, expires).catch(() => null);
  if (!sig) return null;

  const params = new URLSearchParams({ uid: String(uid), expires, sig, next: target });
  return `${tunnelUrl}/auth/handoff?${params.toString()}`;
}

function page({ url, seenAt, stale }) {
  const host = (() => {
    try {
      return new URL(url).host;
    } catch {
      return url;
    }
  })();

  let lastSeen = "";
  if (seenAt) {
    const seconds = Math.max(0, Math.round((Date.now() - new Date(seenAt).getTime()) / 1000));
    lastSeen = seconds < 90 ? `${seconds} seconds ago` : `${Math.round(seconds / 60)} minutes ago`;
  }

  const html = `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin panel</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:grid; place-items:center;
         font:15px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif;
         background:#f5f3ef; color:#2b2723; }
  @media (prefers-color-scheme: dark) { body { background:#1a1815; color:#efe9e0; } }

  .card { max-width:440px; padding:34px 32px; border-radius:20px; text-align:center;
          background:rgba(255,255,255,.78); border:1px solid rgba(0,0,0,.07);
          box-shadow:0 14px 44px rgba(0,0,0,.09); }
  @media (prefers-color-scheme: dark) {
    .card { background:rgba(255,255,255,.045); border-color:rgba(255,255,255,.09); }
  }

  h1 { font-size:20px; margin:0 0 10px; letter-spacing:-.01em; }
  p { margin:0 0 10px; opacity:.82; }
  .muted { font-size:13px; opacity:.6; }

  a.open { display:inline-flex; align-items:center; gap:9px; margin:20px 0 6px;
           padding:13px 24px; border-radius:12px; text-decoration:none; font-weight:650;
           color:#12232b; background:#9fd0e6; border:1px solid rgba(0,0,0,.08);
           transition:transform .16s ease, box-shadow .16s ease, filter .16s ease; }
  a.open:hover { transform:translateY(-1px); filter:brightness(1.04);
                 box-shadow:0 10px 24px rgba(0,0,0,.14); }
  a.open:active { transform:translateY(0); }
  a.open svg { width:17px; height:17px; }

  .note { margin-top:18px; padding-top:16px; border-top:1px solid rgba(0,0,0,.07);
          font-size:12.5px; opacity:.6; text-align:left; }
  @media (prefers-color-scheme: dark) { .note { border-top-color:rgba(255,255,255,.1); } }
  .note p { margin:0 0 6px; }
  code { background:rgba(0,0,0,.06); padding:1px 6px; border-radius:5px; font-size:12px; }
  @media (prefers-color-scheme: dark) { code { background:rgba(255,255,255,.09); } }

  .stale { margin-top:14px; font-size:12.5px; color:#b4703a; }
</style></head>
<body><div class="card">
  <h1>Admin panel</h1>
  <p>If you are trying to reach the admin page, open it with the button below.</p>

  <a class="open" href="${url}/admin" target="_blank" rel="noopener noreferrer">
    Open the admin panel
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
         stroke-linecap="round" stroke-linejoin="round">
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
      <polyline points="15 3 21 3 21 9"></polyline>
      <line x1="10" y1="14" x2="21" y2="3"></line>
    </svg>
  </a>
  <div class="muted">Opens in a new tab, at <code>jamesheston.pages.dev/dashboard</code></div>

  ${stale ? `<div class="stale">This address was last seen ${lastSeen}, so the host machine may be
    offline. If the page does not load, start <code>main.py</code> and try again.</div>` : ""}

  <div class="note">
    <p>It runs on the host machine, so it only answers while that machine is on.</p>
    <p>You may see a one-time "Tunnel website ahead" notice first. That is the
       tunnel service asking for a click, not a problem with your account — once
       you have clicked through it, it stops appearing.</p>
  </div>
</div></body></html>`;

  return new Response(html, {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}

export async function onRequestGet({ request, env }) {
  let info = null;
  try {
    info = await tunnelInfo(env);
  } catch (err) {
    console.error(`[dashboard] tunnel lookup failed: ${err}`);
  }

  if (!info) {
    return new Response(
      `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
       <meta name="viewport" content="width=device-width,initial-scale=1">
       <title>Admin panel</title></head>
       <body style="margin:0;min-height:100vh;display:grid;place-items:center;
                    font:15px/1.6 system-ui,sans-serif;background:#f5f3ef;color:#2b2723">
       <div style="max-width:400px;padding:32px;text-align:center">
         <h1 style="font-size:19px;margin:0 0 10px">The admin panel is offline</h1>
         <p style="opacity:.8;margin:0">The host machine has not published an address.
            Start <code>main.py</code> and reload this page.</p>
       </div></body></html>`,
      {
        status: 503,
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      }
    );
  }

  const age = info.seenAt ? (Date.now() - new Date(info.seenAt).getTime()) / 1000 : Infinity;

  // Signed in on the member site already? Go straight in — this is what lets a
  // moderator reach the panel without ever authorising on it separately, and it
  // is why the tunnel's address changing on restart does not matter: the
  // handoff reads whatever address is current instead of relying on a callback
  // registered with Discord.
  const requested = new URL(request.url).searchParams.get("next") || "/admin";
  const direct = await handoffUrl(request, env, info.url, requested).catch(() => null);
  if (direct) {
    return new Response(null, {
      status: 302,
      headers: { Location: direct, "Cache-Control": "no-store" },
    });
  }

  return page({ ...info, stale: age > 600 });
}
