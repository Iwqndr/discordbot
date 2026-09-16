// GET /dashboard — send a signed-in member to the admin panel.
//
// The panel itself is a Flask app on the operator's own machine, reachable
// publicly through a Cloudflare tunnel. `main.py` publishes the tunnel's
// current address into the `bot_status` row with id 'tunnel', so this route
// always redirects to the live one instead of a stale bookmark.
//
// A quick tunnel gets a new hostname every restart, which is exactly why the
// address is looked up per request rather than baked in.

import { currentUser, fail, supa } from "./_lib/core.js";

// Roles allowed through the gate. Set PANEL_ROLE_IDS in the Pages environment
// to a comma-separated list of Discord role ids; with it unset the route only
// requires that somebody is signed in, which is enough to keep it out of
// search engines but not enough to call it staff-only.
function allowedRoleIds(env) {
  return String(env.PANEL_ROLE_IDS ?? "")
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

async function handleGet({ request, env }) {
  const uid = await currentUser(request, env);
  if (!uid) return fail("Sign in with Discord first.", 401);

  const roleIds = allowedRoleIds(env);
  if (roleIds.length) {
    const member = await supa(
      env,
      `members?user_id=eq.${encodeURIComponent(uid)}&select=roles&limit=1`
    );
    const roles = Array.isArray(member.rows) && member.rows[0]?.roles ? member.rows[0].roles : [];
    const mine = new Set(roles.map((r) => String(r?.id ?? "")));
    if (!roleIds.some((id) => mine.has(id))) {
      return fail("Permissions are required to access this panel.", 403);
    }
  }

  const res = await supa(env, "bot_status?id=eq.tunnel&select=version,updated_at&limit=1");
  const row = Array.isArray(res.rows) ? res.rows[0] : null;
  const base = String(row?.version ?? "").trim().replace(/\/+$/, "");

  if (!base) {
    return fail(
      "The admin panel is offline. Start main.py on the host machine and try again.",
      503
    );
  }

  // Stale addresses are common after a crash, so say when it was last seen.
  const age = row?.updated_at ? (Date.now() - new Date(row.updated_at).getTime()) / 1000 : null;
  const stale = age !== null && age > 3600;

  return new Response(null, {
    status: 302,
    headers: {
      Location: `${base}/admin`,
      "Cache-Control": "no-store",
      ...(stale ? { "X-Panel-Stale": "1" } : {}),
    },
  });
}

export const onRequestGet = async (context) => {
  try {
    return await handleGet(context);
  } catch (err) {
    console.error(`[/dashboard] ${err && err.stack ? err.stack : String(err)}`);
    return fail("Could not reach the admin panel.", 502);
  }
};
