// GET /api/my-cases
//
// The case picker on the appeal form. Only the caller's own cases are returned,
// identified by their Discord id, and only the fields the picker renders.

import {
  currentUser,
  fail,
  ok,
  route,
  supa,
} from "../_lib/core.js";

async function onRequestGet({ request, env }) {
  const uid = await currentUser(request, env);
  if (!uid) return fail("Sign in with Discord to pick a case, or type the case id below.", 401);

  const res = await supa(
    env,
    `mod_cases?user_id=eq.${encodeURIComponent(uid)}&select=id,action,reason,moderator,timestamp,status&order=timestamp.desc&limit=50`
  );

  if (!res.ok || !Array.isArray(res.rows)) {
    // The page shows its own "could not load" message; an empty list is
    // friendlier than a hard failure when the table is simply absent.
    return ok({ cases: [] });
  }

  const cases = res.rows.map((row) => ({
    id: String(row.id ?? ""),
    action: row.action || "Action",
    reason: row.reason || "",
    moderator: row.moderator || "",
    timestamp: row.timestamp || null,
    status: row.status || "active",
  }));

  return ok({ cases });
}

export const onRequestGet = route("api/my-cases", onRequestGet);
