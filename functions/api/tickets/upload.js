// POST /api/tickets/upload
//
// The member page posts a single image here with XMLHttpRequest (so it can show
// a progress bar) and expects `{ status: "ok", url }` back.
//
// Files go to a public Supabase Storage bucket. The anon key may only write
// into `ticket-attachments`, and only with an image content type, so a bad key
// cannot be used as general-purpose file hosting.

import {
  currentUser,
  fail,
  ok,
  route,
} from "../../_lib/core.js";

const BUCKET = "ticket-attachments";
const MAX_BYTES = 5 * 1024 * 1024;
const ALLOWED = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

function storageConfig(env) {
  const url = String(env.SUPABASE_URL ?? "").trim().replace(/\/+$/, "");
  const key = String(env.SUPABASE_ANON_KEY ?? "").trim();
  if (!url || !key) return null;
  return { url, key };
}

function extensionFor(type) {
  switch (type) {
    case "image/jpeg":
      return "jpg";
    case "image/gif":
      return "gif";
    case "image/webp":
      return "webp";
    default:
      return "png";
  }
}

async function onRequestPost({ request, env }) {
  const uid = await currentUser(request, env);
  if (!uid) return fail("Log in with Discord first.", 401);

  const cfg = storageConfig(env);
  if (!cfg) return fail("File uploads are not configured.", 503);

  let form;
  try {
    form = await request.formData();
  } catch {
    return fail("That upload could not be read.");
  }

  const file = form.get("file");
  if (!file || typeof file === "string") return fail("No file was attached.");
  if (!ALLOWED.has(file.type)) return fail("Only PNG, JPEG, GIF or WebP images are accepted.");
  if (file.size > MAX_BYTES) return fail("That image is larger than 5 MB.");

  const bytes = new Uint8Array(await file.arrayBuffer());
  const name = `${uid}/${Date.now()}-${Math.random().toString(36).slice(2, 8)}.${extensionFor(file.type)}`;

  let res;
  try {
    res = await fetch(`${cfg.url}/storage/v1/object/${BUCKET}/${name}`, {
      method: "POST",
      headers: {
        apikey: cfg.key,
        Authorization: `Bearer ${cfg.key}`,
        "Content-Type": file.type,
        "x-upsert": "false",
      },
      body: bytes,
    });
  } catch {
    return fail("The upload did not go through. Check your connection.", 502);
  }

  if (!res.ok) {
    return fail(`That file did not upload (${res.status}).`, 502);
  }

  return ok({ url: `${cfg.url}/storage/v1/object/public/${BUCKET}/${name}` });
}

/** Some proxies probe with OPTIONS first. */
export function onRequestOptions() {
  return new Response(null, {
    status: 204,
    headers: { Allow: "POST, OPTIONS", "Cache-Control": "no-store" },
  });
}

export const onRequestPost = route("api/tickets/upload", onRequestPost);
