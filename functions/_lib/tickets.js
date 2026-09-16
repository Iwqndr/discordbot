// GET  /api/tickets/<id> — one ticket, with its replies
// POST /api/tickets/<id>/reply, /close — the two actions the page performs
//
// Everything here is scoped to the signed-in caller's Discord id: a ticket id
// belonging to somebody else answers 404, not 403, so the endpoint cannot be
// used to discover which ids exist.

import { currentUser, fail, ok, shapeTicket, supa } from "./core.js";

export async function findTicket(env, uid, id) {
  const res = await supa(
    env,
    `member_tickets?discord_user_id=eq.${encodeURIComponent(uid)}` +
      `&ticket_id=eq.${encodeURIComponent(id)}&select=*&limit=1`
  );
  if (!res.ok || !Array.isArray(res.rows) || !res.rows.length) return null;
  return res.rows[0];
}

export async function ticketWithReplies(env, uid, row) {
  const ticket = shapeTicket(row);
  const res = await supa(
    env,
    `ticket_reply_queue?discord_user_id=eq.${encodeURIComponent(uid)}` +
      `&ticket_id=eq.${encodeURIComponent(ticket.id)}&select=*&order=created_at.asc`
  );
  const queued = res.ok && Array.isArray(res.rows) ? res.rows : [];
  ticket.replies = [
    ...(ticket.replies || []),
    ...queued.map((reply) => ({
      author: reply.discord_tag || "You",
      body: reply.body || "",
      at: reply.created_at || null,
      attachments: Array.isArray(reply.attachments) ? reply.attachments : [],
      staff: false,
    })),
  ];
  return ticket;
}

export async function patchTicket(env, uid, id, patch) {
  const res = await supa(
    env,
    `member_tickets?discord_user_id=eq.${encodeURIComponent(uid)}&ticket_id=eq.${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      body: { ...patch, updated_at: new Date().toISOString() },
    }
  );
  if (!res.ok || !Array.isArray(res.rows) || !res.rows.length) return null;
  return res.rows[0];
}

export function requireUser(uid) {
  return uid ? null : fail("Log in with Discord first.", 401);
}
