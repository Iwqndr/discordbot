// /panel/* — the admin panel, served from the operator's machine.
//
// Everything under /panel is forwarded to the localtunnel address, which the
// shared proxy in _lib/panel.js also injects the header localtunnel needs.

import { fail } from "../_lib/core.js";
import { panelGate, proxyToPanel } from "../_lib/panel.js";

async function handle({ request, env }) {
  const gated = await panelGate(request, env);
  if (gated.error) return gated.error;

  return proxyToPanel(request, env, { stripPrefix: /^\/panel/ });
}

export const onRequest = async (context) => {
  try {
    return await handle(context);
  } catch (err) {
    console.error(`[/panel] ${err && err.stack ? err.stack : String(err)}`);
    return fail("Could not reach the admin panel.", 502);
  }
};
