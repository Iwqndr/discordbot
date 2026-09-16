// GET  /api/me/profile — the caller's saved page profile
// POST /api/me/profile — save it
//
// Stored in `member_profiles`, one row per Discord id. The page keeps a
// localStorage copy as well, so a failure here degrades to "saved on this
// browser only" rather than losing what they typed.

import {
  currentUser,
  fail,
  ok,
  route,
  supa,
  supaUpsert,
} from "../../_lib/core.js";

const FIELDS = ["bio", "show_nickname", "name_color", "name_font", "name_effect"];

async function handleGet({ request, env }) {
  const uid = await currentUser(request, env);
  if (!uid) return fail("Log in with Discord first.", 401);

  const res = await supa(
    env,
    `member_profiles?user_id=eq.${encodeURIComponent(uid)}&select=*&limit=1`
  );
  const row = Array.isArray(res.rows) ? res.rows[0] : null;

  return ok({ profile: row ? pick(row) : null });
}

async function handlePost({ request, env }) {
  const uid = await currentUser(request, env);
  if (!uid) return fail("Log in with Discord first.", 401);

  let body = {};
  try {
    body = await request.json();
  } catch {
    return fail("Expected a JSON body.");
  }

  const row = { user_id: uid, updated_at: new Date().toISOString(), ...pick(body) };

  const res = await supaUpsert(env, "member_profiles", row);
  if (!res.ok) {
    return fail(`Could not save your profile (${res.status}).`, 502);
  }

  return ok({ profile: pick(row) });
}

function pick(source) {
  const out = {};
  for (const key of FIELDS) {
    if (source?.[key] !== undefined) out[key] = source[key];
  }
  if (out.bio !== undefined) out.bio = String(out.bio).slice(0, 400);
  return out;
}

export const onRequestGet = route("api/me/profile", handleGet);

export const onRequestPost = route("api/me/profile", handlePost);
