// GET  /dashboard — the gate, then on to the admin panel
// POST /dashboard — accept the key and set the owner cookie
//
// The panel runs on the operator's machine and is reached through a tunnel.
// Before anyone gets there they must prove they hold the owner key, kept as a
// Pages secret `PANEL_OWNER_KEY` that never leaves the Worker.
//
// The form is deliberately understated: a small box tucked into the bottom-left
// corner that only appears when the pointer comes near it, saying nothing about
// what is behind it.
//
// If `PANEL_OWNER_KEY` is unset, `PANEL_ROLE_IDS` decides access instead. With
// neither set, nobody gets in — missing configuration should close the door,
// not open it.

import { currentUser, keyMatches, ownerCookie, ownerUnlocked } from "./_lib/core.js";

const ADMIN_TARGET = "/panel/admin";

function redirect(request, path, extraHeaders = {}) {
  return new Response(null, {
    status: 302,
    headers: {
      Location: new URL(path, request.url).toString(),
      "Cache-Control": "no-store",
      ...extraHeaders,
    },
  });
}

/** The hidden key box. Served instead of a redirect until the key is proven. */
function keyPage({ wrong = false } = {}) {
  const html = `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Loading…</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; background:#f5f3ef; color:#2b2723;
         font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif; }
  @media (prefers-color-scheme: dark) { body { background:#1a1815; color:#efe9e0; } }

  /* The whole control stays hidden until the pointer comes near. */
  .keyzone { position:fixed; left:0; bottom:0; width:240px; height:100px; z-index:50; }
  .keybox  { position:absolute; left:14px; bottom:14px; display:flex; gap:6px;
             opacity:0; transform:translateY(6px); pointer-events:none;
             transition:opacity .18s ease, transform .18s ease; }
  .keyzone:hover .keybox, .keyzone:focus-within .keybox {
             opacity:1; transform:none; pointer-events:auto; }

  input { width:150px; padding:7px 10px; border-radius:9px; font:inherit; font-size:13px;
          border:1px solid rgba(0,0,0,.16); background:rgba(255,255,255,.9); color:inherit; }
  @media (prefers-color-scheme: dark) {
    input { border-color:rgba(255,255,255,.18); background:rgba(255,255,255,.07); }
  }
  input:focus { outline:none; border-color:#6ea8c9; box-shadow:0 0 0 3px rgba(110,168,201,.22); }

  button { padding:7px 12px; border-radius:9px; font:inherit; font-size:13px; font-weight:600;
           border:1px solid rgba(0,0,0,.14); background:rgba(255,255,255,.9); color:inherit;
           cursor:pointer; }
  button:hover { border-color:#6ea8c9; }
  @media (prefers-color-scheme: dark) {
    button { border-color:rgba(255,255,255,.18); background:rgba(255,255,255,.07); }
  }

  .wrong { position:absolute; left:16px; bottom:54px; font-size:12px; color:#c25b5b;
           opacity:${wrong ? 1 : 0}; transition:opacity .18s ease; }
</style></head>
<body>
  <div class="keyzone">
    <div class="wrong">That key was not right.</div>
    <form class="keybox" method="POST" action="/dashboard" autocomplete="off">
      <input type="password" name="key" placeholder="Enter Key" aria-label="Enter Key"${wrong ? " autofocus" : ""}>
      <button type="submit">Go</button>
    </form>
  </div>
</body></html>`;

  return new Response(html, {
    status: wrong ? 401 : 200,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}

export async function onRequestGet({ request, env }) {
  const roleIds = String(env.PANEL_ROLE_IDS ?? "")
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);

  if (env.PANEL_OWNER_KEY) {
    if (!(await ownerUnlocked(request, env))) return keyPage();
    return redirect(request, ADMIN_TARGET);
  }

  if (roleIds.length) {
    // Role-based access: the proxy enforces it, so just send them along.
    const uid = await currentUser(request, env);
    if (!uid) return keyPage();
    return redirect(request, ADMIN_TARGET);
  }

  return new Response(
    `<!DOCTYPE html><html><body style="font:15px system-ui;padding:40px">
     <h1 style="font-size:18px">The panel is not configured</h1>
     <p>Set <code>PANEL_OWNER_KEY</code> or <code>PANEL_ROLE_IDS</code> in this Pages project.</p>
     </body></html>`,
    {
      status: 503,
      headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
    }
  );
}

export async function onRequestPost({ request, env }) {
  if (!env.PANEL_OWNER_KEY) return onRequestGet({ request, env });

  let supplied = "";
  try {
    const form = await request.formData();
    supplied = String(form.get("key") ?? "");
  } catch {
    // Fall through to the wrong-key branch.
  }

  if (!keyMatches(env, supplied)) {
    return keyPage({ wrong: true });
  }

  const cookie = await ownerCookie(env);
  if (!cookie) {
    console.error("[dashboard] owner cookie could not be signed — SESSION_SECRET missing?");
    return new Response("SESSION_SECRET is not set, so the key cannot be remembered.", { status: 503 });
  }
  return redirect(request, ADMIN_TARGET, { "Set-Cookie": cookie });
}
