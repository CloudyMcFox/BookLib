---
layout: page
title: App Clip QR links
permalink: /app-clip/
---

BookLib server operators can create a library-level QR code for the BookLib App
Clip. Encode the public HTTPS server URL as the `server` query parameter:

```text
https://clip.booklib.cloudstarsoftware.com/open?server=https%3A%2F%2Fbooks.example.com
```

The App Clip opens the named server, requests a temporary guest session, scans
the ISBN barcode on a book, and asks for the borrower's name before checkout.

The server must:

- Be reachable through HTTPS with a valid public certificate
- Serve BookLib from the origin root, not from a URL subpath
- Run a BookLib release that supports App Clip guest checkout
- Have `GUEST_ACCESS_ENABLED=true`

Users without App Clip support see a confirmation page before continuing to the
server's normal web interface.

The invocation service is stateless. The server address stays in the QR URL; the
service does not store server registrations, credentials, tokens, library
contents, or borrower information.

## Deploying the invocation Worker

1. Create a Cloudflare Worker using
   `infrastructure/app-clip-worker.js` in module-worker mode.
2. Add the custom domain `clip.booklib.cloudstarsoftware.com`.
3. Confirm that both association URLs return JSON directly with no redirect:

   ```text
   https://clip.booklib.cloudstarsoftware.com/apple-app-site-association
   https://clip.booklib.cloudstarsoftware.com/.well-known/apple-app-site-association
   ```

4. Test an invocation URL in a browser and confirm that it displays the
   destination hostname before offering to continue.

The Worker can be tested locally with Node.js:

```bash
node --test infrastructure/app-clip-worker.test.mjs
```
