/**
 * Stateless invocation service for the BookLib App Clip.
 *
 * Deploy as a Cloudflare Worker custom domain at:
 *   clip.booklib.cloudstarsoftware.com
 *
 * App Clip QR URL:
 *   https://clip.booklib.cloudstarsoftware.com/open?server=https%3A%2F%2Fbooks.example.com
 *
 * Apple fetches the AASA file without redirects or authentication. Browsers
 * receive a confirmation page rather than an automatic open redirect.
 */

const APP_ID = "F346PUMJ54.com.cloudstarsoftware.booklib.CheckoutClip";
const APP_STORE_ID = "6796158777";
const APP_CLIP_BUNDLE_ID = "com.cloudstarsoftware.booklib.CheckoutClip";
const SERVER_ALIASES = {
  "/p": "https://booklib.cloudstarsoftware.com",
  "/r": "https://booklib-review.cloudstarsoftware.com",
};
const AASA_PATHS = new Set([
  "/apple-app-site-association",
  "/.well-known/apple-app-site-association",
]);

const ASSOCIATION = {
  applinks: {
    details: [
      {
        appIDs: ["F346PUMJ54.com.cloudstarsoftware.booklib"],
        components: [{ "/": "/open" }],
      },
    ],
  },
  appclips: {
    apps: [APP_ID],
  },
};

function normalizedServer(raw) {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.hash ||
      (url.pathname !== "/" && url.pathname !== "")
    ) {
      return null;
    }
    url.pathname = "/";
    url.search = "";
    return url;
  } catch {
    return null;
  }
}

function escapeHTML(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function responseHeaders(contentType) {
  return {
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action https:",
    "content-type": contentType,
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
  };
}

function page(title, body, status = 200) {
  return new Response(
    `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="apple-itunes-app" content="app-id=${APP_STORE_ID}, app-clip-bundle-id=${APP_CLIP_BUNDLE_ID}">
  <title>${escapeHTML(title)}</title>
  <style>
    :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f2f2f7; color: #111; }
    main { width: min(32rem, calc(100% - 2rem)); box-sizing: border-box; padding: 2rem; border-radius: 1.25rem; background: #fff; box-shadow: 0 1rem 3rem #0002; }
    h1 { margin-top: 0; }
    .host { overflow-wrap: anywhere; font-weight: 600; }
    a.button { display: block; margin-top: 1.5rem; padding: .9rem 1rem; border-radius: .75rem; background: #0a84ff; color: white; text-align: center; text-decoration: none; font-weight: 600; }
    .note { color: #666; font-size: .9rem; }
    @media (prefers-color-scheme: dark) {
      body { background: #000; color: #fff; }
      main { background: #1c1c1e; }
      .note { color: #aaa; }
    }
  </style>
</head>
<body><main>${body}</main></body>
</html>`,
    { status, headers: responseHeaders("text/html; charset=utf-8") },
  );
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (AASA_PATHS.has(url.pathname)) {
      return new Response(JSON.stringify(ASSOCIATION), {
        headers: {
          "cache-control": "public, max-age=3600",
          "content-type": "application/json",
        },
      });
    }

    if (url.pathname !== "/open" && !SERVER_ALIASES[url.pathname]) {
      return page(
        "BookLib App Clip",
        "<h1>BookLib</h1><p>Scan a BookLib checkout QR code to open a library.</p>",
        404,
      );
    }

    const server = normalizedServer(
      SERVER_ALIASES[url.pathname] || url.searchParams.get("server"),
    );
    if (!server) {
      return page(
        "Invalid BookLib link",
        "<h1>Invalid BookLib link</h1><p>This QR code does not contain a valid HTTPS BookLib server.</p>",
        400,
      );
    }

    const destination = escapeHTML(server.href);
    const host = escapeHTML(server.host);
    return page(
      "Open BookLib",
      `<h1>Open this BookLib library?</h1>
       <p>The QR code points to:</p>
       <p class="host">${host}</p>
       <a class="button" href="${destination}">Continue to BookLib</a>
       <p class="note">Only continue if you recognize and trust this server.</p>`,
    );
  },
};
