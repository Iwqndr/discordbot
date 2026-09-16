// POST /api/tickets/<id>/reply
//
// A member reply is queued for the admin bot, which is the only process that
// writes to the ticket the staff see. The queue row is what the member page
// reads back, so their own message appears immediately.

import { currentUser, fail, ok } from "../../../_lib/core.js";
import { findTicket, ticketWithReplies } from "../../../_lib/tickets.js";
import { supa } from "../../../_lib/core.js";

export async function onRequestPost({ request, env, params }) {
  const uid = await currentUser(request, env);
  if (!uid) return fail("Log in with Discord first.", 401);

  const id = String(params.id || "").trim();
  if (!id) return fail("No ticket id supplied.");

  const row = await findTicket(env, uid, id);
  if (!row) return fail("That ticket is not yours, or it no longer exists.", 404);
  if (String(row.status) === "closed") return fail("That ticket is closed. Open a new one instead.");

  let body = {};
  try {
    body = await request.json();
  } catch {
    return fail("Expected a JSON body.");
  }

  const text = String(body.body || "").trim().slice(0, 2000);
  const attachments = Array.isArray(body.attachments)
    ? body.attachments.filter((u) => typeof u === "string" && u).slice(0, 3)
    : [];
  if (!text && !attachments.length) return fail("Write something before sending.");

  const write = await supa(env, "ticket_reply_queue", {
    method: "POST",
    body: {
      ticket_id: id,
      body: text || null,
      attachments,
      discord_user_id: uid,
      discord_tag: String(body.discord_tag || "").slice(0, 100) || null,
      status: "queued",
    },
  });
  if (!write.ok) return fail(`Your reply did not send (${write.status}).`, 502);

  const fresh = await findTicket(env, uid, id);
  return ok({ ticket: await ticketWithReplies(env, uid, fresh || row) });
}
