// GET /authorize — finish a panel login that Discord sent here.
//
// This is the ONE redirect URI registered with Discord for the admin panel, and
// it is on purpose not the panel's own address: the panel lives behind a quick
// tunnel whose address is new on every restart, so a redirect registered for it
// would mean editing the Discord application every single time. Asking Discord
// for a fixed address instead means the panel's address can change as often as it
// likes, and this route forwards the login to whatever tunnel is live right now.
//
// It only does that for a login that started ON THE PANEL. The panel mints a
// `state` carrying `aud: "panel"`, an expiry and where to land; a visit to this
// address on its own, or the member site's own login (a different state format,
// signed with a different secret), is told what this page is for instead. Without
// that check this would be a login gateway that anyone browsing the site could
// use, which is exactly what it must not be.

import { decodeB64urlJson, hmacHex, panelBase, tunnelInfo } from "./_lib/core.js";

/** How long a login may sit at Discord before its state is refused. */
const MAX_STATE_AGE_SECONDS = 600;

/** So the "no shared secret configured" warning is logged once, not per login. */
let warnedUnsigned = false;

/** Constant-time-ish string comparison, so a wrong MAC leaks nothing. */
function sameString(a, b) {
  const left = String(a ?? "");
  const right = String(b ?? "");
  if (!left || left.length !== right.length) return false;
  let diff = 0;
  for (let i = 0; i < left.length; i += 1) diff |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return diff === 0;
}

/**
 * The panel's own claims from `state`, or null when this is not a panel login.
 *
 * The payload is what matters: it has to decode, name the panel as its audience
 * and still be in date. When `PANEL_HANDOFF_SECRET` is configured the signature
 * is checked too — and then it is required, because a state without one did not
 * come from a panel holding that secret.
 */
async function panelClaims(env, state) {
  const raw = String(state ?? "").trim();
  if (!raw) return { reason: "shape" };

  const now = Math.floor(Date.now() / 1000);
  const [body, mac] = raw.split(".");
  const claims = decodeB64urlJson(body);
  if (!claims || typeof claims !== "object") return { reason: "shape" };
  if (claims.aud !== "panel" || Number(claims.v) !== 1) return { reason: "shape" };
  if (!(Number(claims.exp) > now)) return { reason: "expired" };
  if (Number(claims.exp) > now + MAX_STATE_AGE_SECONDS + 60) return { reason: "shape" };

  const secret = String(env.PANEL_HANDOFF_SECRET ?? "").trim();
  if (secret) {
    // A state without a signature cannot have come from a panel holding that
    // secret, and a signature that does not match usually means the two sides
    // hold different values — a setup mistake worth naming rather than hiding
    // behind "login failed".
    if (!mac) return { reason: "signature" };
    const expected = await hmacHex(secret, body).catch(() => null);
    if (!expected || !sameString(mac, expected)) return { reason: "signature" };
  } else if (!warnedUnsigned) {
    // Said once per Worker isolate, not once per login.
    warnedUnsigned = true;
    console.warn(
      "[authorize] panel states are not signed: set PANEL_HANDOFF_SECRET here and in the " +
        "bot's .env, and a forged state cannot reach this route at all."
    );
  }
  return { claims };
}

function page(title, message, { status = 400, link = null } = {}) {
  return new Response(
    `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
     <meta name="viewport" content="width=device-width,initial-scale=1">
     <title>${title}</title></head>
     <body style="margin:0;min-height:100vh;display:grid;place-items:center;
                  font:15px/1.65 system-ui,-apple-system,'Segoe UI',sans-serif;
                  background:#f5f3ef;color:#2b2723">
     <div style="max-width:430px;padding:32px;text-align:center">
       <h1 style="font-size:19px;margin:0 0 10px">${title}</h1>
       <p style="opacity:.8;margin:0 0 12px">${message}</p>
       ${link ? `<p style="margin:0"><a href="${link.href}" style="color:#2b6c86">${link.label}</a></p>` : ""}
     </div></body></html>`,
    {
      status,
      headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
    }
  );
}

/** A bare visit, a state that did not come from the panel, or a stale one. */
function refusal(reason) {
  if (reason === "expired") {
    return page(
      "This login expired",
      "It was started more than ten minutes ago, so it can no longer be finished. Start " +
        "again from the panel's “Log in with Discord” button.",
      { status: 403, link: { href: "/", label: "Go to the member site" } }
    );
  }

  if (reason === "signature") {
    return page(
      "The panel's login signature did not match",
      "This site and the bot are using different `PANEL_HANDOFF_SECRET` values, so the login " +
        "cannot be trusted and was refused. Set the same value in the Pages environment " +
        "variables and in the bot's <code>.env</code> — or remove it from both — and try again.",
      { status: 403, link: { href: "/", label: "Go to the member site" } }
    );
  }

  return page(
    "This address is only for panel logins",
    "It finishes a sign-in that was started in the admin panel. Nothing here can sign you " +
      "in on its own — open the panel and use its “Log in with Discord” button, and Discord " +
      "will bring you back through this page.",
    { status: 403, link: { href: "/", label: "Go to the member site" } }
  );
}

function panelOffline() {
  return page(
    "The admin panel is offline",
    "The host machine has not published an address, so there is nowhere to finish this " +
      "login. Start <code>main.py</code> and begin the sign-in again from the panel.",
    { status: 503 }
  );
}

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const oauthError = url.searchParams.get("error");

  try {
    const { claims, reason } = await panelClaims(env, state);
    if (!claims) {
      console.warn(`[authorize] refused a ${reason} login (${url.search || "no query"})`);
      return refusal(reason);
    }

    const info = await tunnelInfo(env).catch((err) => {
      console.error(`[authorize] tunnel lookup failed: ${err}`);
      return null;
    });
    // The published address is what proves the host machine is actually up; the
    // permanent address is where the visitor is sent.
    if (!info) return panelOffline();
    console.log(`[authorize] forwarding a login for ${claims.next} to ${panelBase(env, info.url)}`);

    // Everything Discord sent rides along to the panel's callback, including a
    // refusal (`error=access_denied`), so its own page is what reports the
    // outcome — one login, one place that explains it.
    //
    // `panelBase` prefers the panel's permanent address over the raw tunnel when
    // one is configured, because the visitor's device is the thing that has to
    // resolve it: a quick-tunnel hostname that is seconds old does not exist yet
    // as far as many mobile and home resolvers are concerned.
    const target = new URL(`${panelBase(env, info.url)}/auth/discord/callback`);
    if (code) target.searchParams.set("code", code);
    if (oauthError) target.searchParams.set("error", oauthError);
    target.searchParams.set("state", String(state || ""));

    return new Response(null, {
      status: 302,
      headers: { Location: target.toString(), "Cache-Control": "no-store" },
    });
  } catch (err) {
    console.error(`[authorize] ${err && err.stack ? err.stack : String(err)}`);
    return page(
      "The login could not be completed",
      "Something went wrong handing this sign-in back to the panel. Try again from the " +
        "panel's “Log in with Discord” button.",
      { status: 502 }
    );
  }
}
