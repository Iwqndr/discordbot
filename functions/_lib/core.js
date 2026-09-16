// Small shared plumbing for every Pages Function: JSON replies, Supabase
// REST access, the signed session cookie and the Discord OAuth helpers.
//
// Nothing here talks to Discord's gateway — these functions only ever read
// Supabase with the anon key, which is exactly what RLS is configured for.

export function json(data, init = {}) {
  const headers = new Headers(init.headers || {});
  headers.set("Content-Type", "application/json; charset=utf-8");
  headers.set("Cache-Control", "no-store");
  return new Response(JSON.stringify(data), { ...init, headers });
}

export function fail(message, status = 400, extra = {}) {
  return json({ status: "error", message, ...extra }, { status });
}

export function ok(data = {}) {
  return json({ status: "ok", ...data });
}

/** True when every named env var is present and non-empty. */
export function hasEnv(env, ...names) {
  return names.every((name) => String(env?.[name] ?? "").trim() !== "");
}

// ---------------------------------------------------------------------------
// Supabase (PostgREST over fetch — no client library)
// ---------------------------------------------------------------------------

function supabaseConfig(env, service = false) {
  const url = String(env.SUPABASE_URL ?? "").trim().replace(/\/+$/, "");
  const raw = service ? env.SUPABASE_SERVICE_KEY : env.SUPABASE_ANON_KEY;
  const key = String(raw ?? "").trim();
  if (!url || !key) return null;
  return { url, key };
}

/**
 * Read through the Worker rather than from the browser.
 *
 * `service: true` (the default) uses SUPABASE_SERVICE_KEY, which never reaches
 * the browser — that is what makes "your own tickets only" enforceable, because
 * the caller cannot query the table directly. `service: false` uses the public
 * anon key, which is only appropriate for tables the page already reads itself
 * (`bot_status`, `members`).
 */
export async function supa(env, path, { method = "GET", body, headers = {}, service = true } = {}) {
  const cfg = supabaseConfig(env, service);
  if (!cfg) return { ok: false, status: 0, rows: null, text: "supabase not configured" };

  const init = {
    method,
    headers: {
      apikey: cfg.key,
      Authorization: `Bearer ${cfg.key}`,
      "Content-Type": "application/json",
      Prefer: "return=representation",
      ...headers,
    },
  };
  if (body !== undefined) init.body = JSON.stringify(body);

  try {
    const res = await fetch(`${cfg.url}/rest/v1/${path.replace(/^\/+/, "")}`, init);
    const text = await res.text();
    let rows = null;
    try {
      rows = text ? JSON.parse(text) : null;
    } catch {
      rows = null;
    }
    return { ok: res.ok, status: res.status, rows, text };
  } catch (err) {
    return { ok: false, status: 0, rows: null, text: String(err) };
  }
}

/** POST with merge-duplicates, so one call both inserts and updates. */
export function supaUpsert(env, table, row) {
  return supa(env, table, {
    method: "POST",
    body: row,
    headers: { Prefer: "resolution=merge-duplicates,return=representation" },
  });
}

// ---------------------------------------------------------------------------
// Session cookie — base64url payload plus an HMAC made with SESSION_SECRET
// ---------------------------------------------------------------------------

const COOKIE = "wc_session";
const STATE_COOKIE = "wc_state";
const MAX_AGE = 60 * 60 * 24 * 30; // 30 days

function b64url(bytes) {
  let binary = "";
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function unb64url(text) {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
  return out;
}

async function hmacKey(secret) {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(String(secret)),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"]
  );
}

export async function sign(env, payload) {
  const secret = env.SESSION_SECRET;
  if (!secret) return null;
  const body = b64url(new TextEncoder().encode(JSON.stringify(payload)));
  const key = await hmacKey(secret);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  return `${body}.${b64url(mac)}`;
}

export async function unsign(env, token) {
  const secret = env.SESSION_SECRET;
  if (!secret || !token) return null;
  const [body, mac] = String(token).split(".");
  if (!body || !mac) return null;
  try {
    const key = await hmacKey(secret);
    const valid = await crypto.subtle.verify(
      "HMAC",
      key,
      unb64url(mac),
      new TextEncoder().encode(body)
    );
    if (!valid) return null;
    return JSON.parse(new TextDecoder().decode(unb64url(body)));
  } catch {
    return null;
  }
}

function readCookie(request, name) {
  const header = request.headers.get("Cookie") || "";
  for (const part of header.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return decodeURIComponent(rest.join("="));
  }
  return null;
}

function cookieHeader(name, value, maxAge) {
  const attrs = [
    `${name}=${encodeURIComponent(value)}`,
    "Path=/",
    "HttpOnly",
    "Secure",
    "SameSite=Lax",
  ];
  if (maxAge > 0) attrs.push(`Max-Age=${maxAge}`);
  return attrs.join("; ");
}

export function sessionCookie(token) {
  return cookieHeader(COOKIE, token, MAX_AGE);
}

export function clearSessionCookie() {
  return cookieHeader(COOKIE, "", 0);
}

export function stateCookie(token) {
  return cookieHeader(STATE_COOKIE, token, 600);
}

export function readState(request) {
  return readCookie(request, STATE_COOKIE);
}

/** The signed-in member, or null. Never throws. */
export async function currentUser(request, env) {
  const session = await unsign(env, readCookie(request, COOKIE));
  if (!session || !session.uid) return null;
  return String(session.uid);
}

// ---------------------------------------------------------------------------
// Discord REST
// ---------------------------------------------------------------------------

export function avatarUrl(user) {
  const id = String(user?.id ?? "");
  const hash = user?.avatar;
  if (id && hash) {
    const ext = String(hash).startsWith("a_") ? "gif" : "png";
    return `https://cdn.discordapp.com/avatars/${id}/${hash}.${ext}?size=128`;
  }
  const index = /^\d+$/.test(id) ? (BigInt(id) >> 22n) % 6n : 0n;
  return `https://cdn.discordapp.com/embed/avatars/${index}.png`;
}

export function publicMember(user) {
  if (!user) return null;
  return {
    user_id: String(user.id),
    username: user.username || "",
    display_name: user.global_name || user.username || "",
    avatar_url: avatarUrl(user),
  };
}

export function redirectUri(request, env) {
  const configured = String(env.DISCORD_REDIRECT_URI ?? "").trim();
  if (configured) return configured;
  return `${new URL(request.url).origin}/auth/discord/callback`;
}

export async function discordToken(request, env, code) {
  const body = new URLSearchParams({
    client_id: env.DISCORD_CLIENT_ID,
    client_secret: env.DISCORD_CLIENT_SECRET,
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri(request, env),
  });
  const res = await fetch("https://discord.com/api/oauth2/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) return null;
  return res.json().catch(() => null);
}

export async function discordUser(accessToken) {
  const res = await fetch("https://discord.com/api/users/@me", {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!res.ok) return null;
  return res.json().catch(() => null);
}

// ---------------------------------------------------------------------------
// Ticket helpers
// ---------------------------------------------------------------------------

/**
 * Staff tickets live in the admin bot's store and are not readable with the
 * anon key, so the member's own view is built from what the Worker may read:
 * the local queue tables plus the synced members row. `status` and the staff
 * side of a conversation come through when the admin bot mirrors them into
 * `member_tickets`; until then the page shows what the member sent.
 */
export function shapeTicket(row) {
  return {
    id: String(row.ticket_id || row.id || ""),
    title: row.subject || row.category || "Ticket",
    avatar_url: row.avatar_url || "",
    subject: row.subject || "",
    category: row.category || "",
    category_label: row.category_label || prettyCategory(row.category),
    status: row.status || "open",
    priority: row.priority || "normal",
    contact: row.contact || "",
    answers: row.answers || {},
    attachments: Array.isArray(row.attachments) ? row.attachments : [],
    created_at: row.created_at || null,
    updated_at: row.updated_at || row.created_at || null,
    replies: Array.isArray(row.replies) ? row.replies : [],
  };
}

export function prettyCategory(value) {
  const text = String(value || "").replace(/[-_]+/g, " ").trim();
  if (!text) return "Ticket";
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** A short, human ticket number: W-XXXXXX. */
export function newTicketId() {
  const n = Math.floor(Math.random() * 0xffffff).toString(16).toUpperCase().padStart(6, "0");
  return `W-${n}`;
}

// ---------------------------------------------------------------------------
// Staff titles
// ---------------------------------------------------------------------------

/**
 * Pick the best-matching staff title for a member's roles. `titles` comes from
 * the `staff_titles` table; a missing table simply yields no badges.
 */
export function titleForRoles(titles, roles) {
  if (!Array.isArray(titles) || !titles.length) return null;
  const ids = new Set((roles || []).map((r) => String(r?.id ?? r?.name ?? "")));
  const names = (roles || []).map((r) => String(r?.name ?? "").toLowerCase()).filter(Boolean);

  let best = null;
  for (const entry of titles) {
    const byId = (entry.roles || []).some((r) => ids.has(String(r)));
    const byName = (entry.keywords || []).some((k) => {
      const needle = String(k).toLowerCase();
      return names.some((n) => n === needle || n.includes(needle));
    });
    if (!byId && !byName) continue;
    if (!best || Number(entry.priority || 0) > Number(best.priority || 0)) best = entry;
  }
  return best;
}

// ---------------------------------------------------------------------------
// Route wrapper
// ---------------------------------------------------------------------------

/**
 * Wrap a handler so an unexpected throw becomes a JSON 502 instead of a
 * Cloudflare 1101 "Worker threw exception" page. The full stack still goes to
 * `console.error`, which is what the Pages project's Logs tab shows.
 *
 * The `/auth/*` redirects use their own version, because a browser navigating
 * there needs a redirect rather than a JSON body.
 */
export function route(name, handler) {
  return async (context) => {
    try {
      return await handler(context);
    } catch (err) {
      console.error(`[${name}] ${err && err.stack ? err.stack : String(err)}`);
      return fail(`${name} failed unexpectedly — see the Worker logs.`, 502);
    }
  };
}

/**
 * The `/auth/*` variant: a browser navigates there, so a thrown error has to
 * land back on the member page with `?login=failed` rather than a JSON body.
 */
export function routeRedirect(name, handler) {
  return async (context) => {
    const origin = new URL(context.request.url).origin;
    try {
      return await handler(context);
    } catch (err) {
      console.error(`[${name}] ${err && err.stack ? err.stack : String(err)}`);
      return Response.redirect(`${origin}/?login=failed`, 302);
    }
  };
}
