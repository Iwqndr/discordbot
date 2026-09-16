// GET /api/tickets/<id>
//
// The page normally gets everything it needs from /api/tickets, so this exists
// for deep links and refreshes of a single conversation.

import { currentUser, fail, ok } from "../../_lib/core.js";
import { findTicket, ticketWithReplies } from "../../_lib/tickets.js";

export async function onRequestGet({ request, env, params }) {
  const uid = await currentUser(request, env);
  if (!uid) return fail("Log in with Discord first.", 401);

  const id = String(params.id || "").trim();
  if (!id) return fail("No ticket id supplied.");

  const row = await findTicket(env, uid, id);
  if (!row) return fail("That ticket is not yours, or it no longer exists.", 404);

  return ok({ ticket: await ticketWithReplies(env, uid, row), open_count: row.status === "closed" ? 0 : 1 });
}
