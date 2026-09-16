// GET /dashboard — send a signed-in member to the admin panel.
//
// The panel is a Flask app on the operator's own machine, published through
// localtunnel. It is served through `/panel/*` rather than linked directly,
// because that proxy is what adds the header localtunnel needs — a direct
// loca.lt link shows every browser a "Tunnel website ahead!" interstitial,
// which reads like a security warning.
//
// The gate (signed in, and holding a role listed in PANEL_ROLE_IDS) lives in
// the proxy, so it covers every panel path rather than just this entry point.

export async function onRequestGet({ request }) {
  return new Response(null, {
    status: 302,
    headers: {
      Location: new URL("/panel/", request.url).toString(),
      "Cache-Control": "no-store",
    },
  });
}
