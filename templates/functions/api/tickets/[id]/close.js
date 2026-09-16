// POST /api/tickets/<id>/close
//
// Closing is a status flip on the member's own row. It does not delete
// anything, so staff history survives.

import { currentUser, fail, ok } from "../../../_lib/core.js";
import { findTicket, patchTicket, ticketWithReplies } from "../../../_lib/tickets.js";

export async function onRequestPost({ request, env, params }) {
  const uid = await currentUser(request, env);
  if (!uid) return fail("Log in with Discord first.", 401);

  const id = String(params.id || "").trim();
  if (!id) return fail("No ticket id supplied.");

  const row = await findTicket(env, uid, id);
  if (!row) return fail("That ticket is not yours, or it no longer exists.", 404);

  if (String(row.status) === "closed") {
    return ok({ ticket: await ticketWithReplies(env, uid, row) });
  }

  const updated = await patchTicket(env, uid, id, { status: "closed", closed_at: new Date().toISOString() });
  if (!updated) return fail("Could not close that ticket.", 502);

  return ok({ ticket: await ticketWithReplies(env, uid, updated) });
}
