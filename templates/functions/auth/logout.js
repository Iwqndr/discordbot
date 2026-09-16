// GET /auth/logout — drop the session and land back on the member page.
//
// member.html links here as `/auth/logout`; it never passes `next`, so a plain
// local path is all that is needed.

import { clearSessionCookie } from "../_lib/core.js";

export async function onRequestGet({ request }) {
  const origin = new URL(request.url).origin;
  const next = new URL(request.url).searchParams.get("next") || "/";
  const safeNext = next.startsWith("/") && !next.startsWith("//") ? next : "/";

  return new Response(null, {
    status: 302,
    headers: {
      Location: `${origin}${safeNext}`,
      "Set-Cookie": clearSessionCookie(),
      "Cache-Control": "no-store",
    },
  });
}
