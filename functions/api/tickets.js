// GET  /api/tickets — the caller's own tickets, with replies
// POST /api/tickets — open a new one
//
// Tickets are read back with the anon key, which means RLS has to scope
// `ticket_queue`, `ticket_reply_queue` and `member_tickets` to the caller's own
// Discord id. The session cookie is the only thing that decides which id that
// is, so a member can never ask for someone else's conversation.
//
// The rows also have to satisfy the filter below, so the Worker cannot be
// tricked into returning another user's tickets by a crafted query string.

import {
  currentUser,
  fail,
  newTicketId,
  ok,
  route,
  shapeTicket,
  supa,
} from "../_lib/core.js";

/** How long a bot heartbeat stays "fresh". The worker writes one every 45s. */
const ADMIN_ONLINE_WINDOW_SECONDS = 180;

/**
 * Is the bot that drains `ticket_queue` running?
 *
 * That heartbeat is the *admin* bot's: it is the admin bot that owns the staff
 * channel and the ticket store, so it is the one whose being up or down decides
 * whether a new ticket is created now or queued for later. Silence means down —
 * the newest row is missing, stale, or unreadable.
 */
async function adminListening(env) {
  try {
    const res = await supa(env, "bot_status?id=eq.admin&select=updated_at&limit=1");
    const row = res.ok && Array.isArray(res.rows) ? res.rows[0] : null;
    const at = new Date(row?.updated_at || 0).getTime();
    if (!Number.isFinite(at) || at <= 0) return false;
    return (Date.now() - at) / 1000 < ADMIN_ONLINE_WINDOW_SECONDS;
  } catch (err) {
    console.warn(`ticket queue: could not read the admin heartbeat (${err})`);
    return false;
  }
}

function normaliseTier(value) {
  const text = String(value || "").trim().toLowerCase();
  return ["low", "normal", "high", "urgent"].includes(text) ? text : "normal";
}

function cleanList(value, limit) {
  if (!Array.isArray(value)) return [];
  return value.filter((v) => typeof v === "string" && v).slice(0, limit);
}

async function repliesFor(env, uid, ticketIds) {
  if (!ticketIds.length) return {};
  const list = ticketIds.map((id) => `"${String(id).replace(/"/g, "")}"`).join(",");
  const res = await supa(
    env,
    `ticket_reply_queue?discord_user_id=eq.${encodeURIComponent(uid)}` +
      `&ticket_id=in.(${encodeURIComponent(list)})&select=*&order=created_at.asc`
  );
  const grouped = {};
  for (const row of res.ok && Array.isArray(res.rows) ? res.rows : []) {
    const key = String(row.ticket_id);
    (grouped[key] ||= []).push({
      author: row.discord_tag || "You",
      body: row.body || "",
      at: row.created_at || null,
      attachments: Array.isArray(row.attachments) ? row.attachments : [],
      staff: false,
    });
  }
  return grouped;
}

async function handleGet({ request, env }) {
  const uid = await currentUser(request, env);
  if (!uid) return fail("Log in with Discord first.", 401);

  const enc = encodeURIComponent(uid);
  const res = await supa(
    env,
    `member_tickets?discord_user_id=eq.${enc}&select=*&order=created_at.desc&limit=50`
  );

  if (!res.ok || !Array.isArray(res.rows)) {
    return ok({ tickets: [], open_count: 0 });
  }

  const tickets = res.rows.map(shapeTicket);
  const replies = await repliesFor(env, uid, tickets.map((t) => t.id));
  for (const ticket of tickets) {
    ticket.replies = [...(ticket.replies || []), ...(replies[ticket.id] || [])];
  }

  return ok({
    tickets,
    open_count: tickets.filter((t) => t.status !== "closed").length,
  });
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

  const subject = String(body.subject || "").trim().slice(0, 200);
  const category = String(body.category || "").trim().slice(0, 80);
  if (!category) return fail("Pick a topic before sending.");

  const answers = body.answers && typeof body.answers === "object" ? body.answers : {};
  const attachments = cleanList(body.attachments, 3);

  const row = {
    ticket_id: newTicketId(),
    category,
    category_label: String(body.category_label || "").slice(0, 120) || null,
    subject: subject || null,
    contact: String(body.contact || "").slice(0, 200) || null,
    priority: normaliseTier(body.priority),
    answers,
    attachments,
    discord_user_id: uid,
    discord_tag: String(body.discord_tag || "").slice(0, 100) || null,
    anonymous: false,
    // With the bot listening this lands in the staff channel within seconds, so
    // the member is told it is open. With it down the ticket waits in the queue,
    // and "processing" is the honest thing to show: the row stays greyed and
    // tagged until the bot comes back and creates it.
    status: (await adminListening(env)) ? "open" : "processing",
  };

  // Two writes on purpose:
  //   ticket_queue     — the bot's work queue, the only route into the staff
  //                      channel, because nothing here can talk to Discord
  //   member_tickets   — the index the member page reads back through the Worker
  // Nearly the same row, but not the same table: `member_tickets` has no
  // `anonymous` column, and PostgREST rejects an insert over one unknown field,
  // so sending the queue row to both used to file every ticket in the queue and
  // show nothing under "My tickets".
  const queued = await supa(env, "ticket_queue", { method: "POST", body: row });
  if (!queued.ok) {
    return fail(`We could not send your ticket (${queued.status}). Please try again.`, 502);
  }

  const indexRow = { ...row };
  delete indexRow.anonymous;
  const indexed = await supa(env, "member_tickets", { method: "POST", body: indexRow });
  if (!indexed.ok) {
    // The ticket is safely queued, so this is not a user-facing failure — the
    // page just will not show it in "My tickets" until the bot syncs it back.
    console.warn(`member_tickets insert failed: ${indexed.status} ${indexed.text}`);
  }

  const stored = Array.isArray(indexed.rows) ? indexed.rows[0] : indexRow;
  return ok({ ticket: shapeTicket(stored) });
}

export const onRequestGet = route("api/tickets", handleGet);

export const onRequestPost = route("api/tickets", handlePost);
