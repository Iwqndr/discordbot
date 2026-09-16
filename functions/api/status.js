// GET /api/status
//
// The member page polls this every few seconds for the status widget. It must
// answer fast and never throw: a Supabase hiccup just means "offline".

import { ok, supa } from "../_lib/core.js";

const ONLINE_WINDOW_SECONDS = 180;

function ageSeconds(stamp) {
  const then = new Date(stamp || 0).getTime();
  if (!Number.isFinite(then) || then <= 0) return Infinity;
  return (Date.now() - then) / 1000;
}

export async function onRequestGet({ env }) {
  const res = await supa(env, "bot_status?select=*");

  if (!res.ok || !Array.isArray(res.rows)) {
    return ok({ online: false, ping: 0, uptime: null, members: 0, online_members: null, cpu: 0, memory_mb: 0 });
  }

  const member = res.rows.find((r) => r.id === "member") || null;
  const admin = res.rows.find((r) => r.id === "admin") || null;

  const payload = {
    online: false,
    ping: 0,
    uptime: null,
    members: 0,
    online_members: null,
    cpu: 0,
    memory_mb: 0,
  };

  if (admin) {
    payload.online = ageSeconds(admin.updated_at) < ONLINE_WINDOW_SECONDS;
    payload.ping = Number(admin.latency_ms || 0);
    payload.uptime = admin.uptime || null;
    payload.members = Number(admin.member_count || 0);
    payload.online_members = Number(admin.online_member_count ?? admin.members_online ?? 0) || null;
    payload.cpu = Number(admin.cpu_percent || 0);
    payload.memory_mb = Number(admin.memory_mb || 0);
  }

  if (member) {
    const memberAge = ageSeconds(member.updated_at);
    // The member bot is the one this page belongs to, so its heartbeat wins
    // when the admin bot has never reported anything.
    if (!admin) {
      payload.online = memberAge < ONLINE_WINDOW_SECONDS;
      payload.ping = Number(member.latency_ms || 0);
      payload.members = Number(member.member_count || 0);
    }
    payload.member_bot = {
      online: memberAge < ONLINE_WINDOW_SECONDS,
      age_seconds: Number.isFinite(memberAge) ? Math.round(memberAge) : null,
      version: member.version || null,
      guilds: Number(member.guild_count || 0),
    };
  } else {
    payload.member_bot = { online: false, age_seconds: null, version: null, guilds: 0 };
  }

  return ok(payload);
}
