// GET / — send the bare domain to the member page.
//
// Pages serves member.html at /member (with /member.html redirecting to it), so
// the site root is the only address that has no page of its own. This also
// matters for the Discord login: the callback finishes on `/?login=ok`, and
// that has to land on the member page rather than a 404.
//
// A 302 rather than a rewrite, so the member page's own canonical URL is what
// the browser settles on.

export async function onRequestGet({ request }) {
  const url = new URL(request.url);
  return Response.redirect(`${url.origin}/member${url.search}`, 302);
}
