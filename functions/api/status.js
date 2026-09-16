// GET /api/status
//
// The member page polls this every few seconds for the status widget. It must
// answer fast and never throw: a Supabase hiccup just means "offline".
//
// This is the MEMBER site, so the member bot's heartbeat is the headline
// figure. The admin bot's row is only a fallback: it reports version 0.0.0 and
// all zeros until that bot's own heartbeat is wired up, and letting it win
// would paint a working site as offline.

import { ok, route, supa } from "../_lib/core.js";

const ONLINE_WINDOW_SECONDS = 180;
// The heartbeat runs once a minute, so this is the process's rough uptime.
const HEARTBEAT_SECONDS = 60;

function ageSeconds(stamp) {
  const then = new Date(stamp || 0).getTime();
  if (!Number.isFinite(then) || then <= 0) return Infinity;
  return (Date.now() - then) / 1000;
}

function humanUptime(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  if (days) return `${days}d ${hours}h ${minutes}m`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${s}s`;
}

/** A row that carries real numbers — not the seeded 0.0.0 placeholder. */
function looksReal(row) {
  if (!row) return false;
  return Number(row.member_count || 0) > 0 || Number(row.guild_count || 0) > 0;
}

async function handleGet({ env }) {
  const [statusRes, onlineRes] = await Promise.all([
    supa(env, "bot_status?select=*"),
    // Presence is optional: if the bot never added an `online` column this
    // returns nothing and the widget shows a dash, exactly as before.
    supa(env, "members?select=user_id&online=is.true"),
  ]);

  if (!statusRes.ok || !Array.isArray(statusRes.rows)) {
    return ok({ online: false, ping: 0, uptime: null, members: 0, online_members: null, cpu: 0, memory_mb: 0 });
  }

  const member = statusRes.rows.find((r) => r.id === "member") || null;
  const admin = statusRes.rows.find((r) => r.id === "admin") || null;
  const pool = [member, admin].filter(looksReal);

  const onlineMembers = onlineRes.ok && Array.isArray(onlineRes.rows) ? onlineRes.rows.length : null;

  const payload = {
    online: false,
    ping: 0,
    uptime: null,
    members: 0,
    online_members: onlineMembers,
    cpu: 0,
    memory_mb: 0,
  };

  if (pool.length) {
    const live = pool.filter((row) => ageSeconds(row.updated_at) < ONLINE_WINDOW_SECONDS);
    // Prefer a heartbeat that is actually alive; otherwise report the freshest
    // one, so "offline" still comes with the last known numbers.
    const source = live[0] || [...pool].sort((a, b) => ageSeconds(a.updated_at) - ageSeconds(b.updated_at))[0];
    const age = ageSeconds(source.updated_at);

    payload.online = age < ONLINE_WINDOW_SECONDS;
    payload.ping = Number(source.latency_ms || 0);
    payload.members = Number(source.member_count || 0);
    payload.uptime = source.uptime ? String(source.uptime) : null;
  }

  if (member) {
    const memberAge = ageSeconds(member.updated_at);
    const memberOnline = memberAge < ONLINE_WINDOW_SECONDS;
    payload.member_bot = {
      online: memberOnline,
      age_seconds: Number.isFinite(memberAge) ? Math.round(memberAge) : null,
      version: member.version || null,
      guilds: Number(member.guild_count || 0),
    };
    // There is no explicit uptime column, so derive one from how far back the
    // heartbeats have been arriving. Only meaningful while it is online.
    if (payload.uptime === null && memberOnline) {
      payload.uptime = humanUptime(memberAge + HEARTBEAT_SECONDS);
    }
  } else {
    payload.member_bot = { online: false, age_seconds: null, version: null, guilds: 0 };
  }

  return ok(payload);
}

export const onRequestGet = route("api/status", handleGet);
