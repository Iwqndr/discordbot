// GET /api/staff-titles
//
// The badge rules the member page applies to names. They live in the
// `staff_titles` table so the admin bot's title editor and this Worker read the
// same source.
//
// Expected row shape (long form, camelCase is not used):
//   { id, label, prefix, suffix, priority, roles: ["<role id>", ...],
//     keywords: ["moderator", ...], style: { <the admin editor's style object> } }

import {
  ok,
  route,
  supa,
} from "../_lib/core.js";

async function handleGet({ env }) {
  const res = await supa(env, "staff_titles?select=*&order=priority.desc");

  if (!res.ok || !Array.isArray(res.rows)) {
    // No table yet, or RLS says no. The page just renders names without badges.
    return ok({ titles: [] });
  }

  const titles = res.rows
    .filter((row) => row && row.label)
    .map((row) => ({
      id: String(row.id ?? row.label),
      label: row.label || "",
      prefix: row.prefix || "",
      suffix: row.suffix || "",
      priority: Number(row.priority || 0),
      roles: Array.isArray(row.roles) ? row.roles.map(String) : [],
      keywords: Array.isArray(row.keywords) ? row.keywords.map(String) : [],
      style: row.style && typeof row.style === "object" ? row.style : {},
    }));

  return ok({ titles });
}

export const onRequestGet = route("api/staff-titles", handleGet);
