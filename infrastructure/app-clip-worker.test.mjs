import assert from "node:assert/strict";
import test from "node:test";

import worker from "./app-clip-worker.js";

test("serves the App Clip and universal-link associations", async () => {
  const response = await worker.fetch(new Request(
    "https://clip.booklib.cloudstarsoftware.com/.well-known/apple-app-site-association"
  ));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "application/json");

  const association = await response.json();
  assert.deepEqual(association.appclips.apps, [
    "F346PUMJ54.com.cloudstarsoftware.booklib.CheckoutClip",
  ]);
  assert.deepEqual(association.applinks.details[0].appIDs, [
    "F346PUMJ54.com.cloudstarsoftware.booklib",
  ]);
});

test("shows a confirmation page for a valid root server", async () => {
  const response = await worker.fetch(new Request(
    "https://clip.booklib.cloudstarsoftware.com/open?server=https%3A%2F%2Fbooks.example.com"
  ));
  const html = await response.text();

  assert.equal(response.status, 200);
  assert.match(html, /books\.example\.com/);
  assert.match(html, /href="https:\/\/books\.example\.com\/"/);
  assert.match(html, /app-id=6796158777/);
  assert.match(html, /app-clip-bundle-id=com\.cloudstarsoftware\.booklib\.CheckoutClip/);
});

test("resolves short production and review App Clip Code aliases", async () => {
  const cases = [
    ["/p", "booklib.cloudstarsoftware.com"],
    ["/r", "booklib-review.cloudstarsoftware.com"],
  ];
  for (const [path, host] of cases) {
    const response = await worker.fetch(new Request(
      `https://c.cloudstarsoftware.com${path}`
    ));
    const html = await response.text();
    assert.equal(response.status, 200);
    assert.match(html, new RegExp(host.replaceAll(".", "\\.")));
  }
});

test("rejects unsafe or unsupported server URLs", async () => {
  const invalidServers = [
    "http://books.example.com",
    "https://user:password@books.example.com",
    "https://books.example.com/booklib",
    "not a URL",
  ];

  for (const server of invalidServers) {
    const invocation = new URL("https://clip.booklib.cloudstarsoftware.com/open");
    invocation.searchParams.set("server", server);
    const response = await worker.fetch(new Request(invocation));
    assert.equal(response.status, 400, server);
  }
});
