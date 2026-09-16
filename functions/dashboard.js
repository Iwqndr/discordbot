// GET /dashboard — send a signed-in member to the admin panel.
//
// Points at `/panel/admin`, not `/panel/`: the tunnel root serves the bot's
// crew page, so `/panel/` landed people on the events hub instead of the panel
// they asked for. `/admin` is the panel itself.
//
// The gate (signed in, and holding a role listed in PANEL_ROLE_IDS) lives in
// the proxy, so it covers every panel path rather than just this entry point.

export async function onRequestGet({ request }) {
  return new Response(null, {
    status: 302,
    headers: {
      Location: new URL("/panel/admin", request.url).toString(),
      "Cache-Control": "no-store",
    },
  });
}
