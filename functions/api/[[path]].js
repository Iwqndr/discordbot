// /api/* fallback — routes the panel's own API calls to the operator's machine.
//
// The panel HTML is written with root-absolute paths (`fetch("/api/stats")`,
// `fetch("/api/admin/me")`). Loading it under our origin means the browser
// resolves those against jamesheston.pages.dev, so they land here instead of on
// the tunnel — which is why the panel reported "the keys are not set up".
//
// Only requests that came from a /panel page are forwarded. Everything else
// gets the 404 it would have got anyway, so the member page's real endpoints
// (which have their own more specific routes) are untouched.
//
// This file only ever runs for /api paths no dedicated route claims.

import { cameFromPanel, proxyToPanel } from "../_lib/panel.js";

export async function onRequest(context) {
  const { request, env } = context;

  if (!cameFromPanel(request)) {
    return new Response("Not Found", { status: 404 });
  }

  try {
    // The path already matches the panel's own, so nothing is stripped.
    return await proxyToPanel(request, env, { stripPrefix: "" });
  } catch (err) {
    console.error(`[/api passthrough] ${err && err.stack ? err.stack : String(err)}`);
    return new Response(JSON.stringify({ status: "error", message: "Panel API unreachable." }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}
