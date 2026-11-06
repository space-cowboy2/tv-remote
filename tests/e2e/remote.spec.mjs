// End-to-end check of the web app against the dev hub + simulated TV.
// Run: python -m tvremote.devserver &  then  node tests/e2e/remote.spec.mjs
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium, devices } = require("playwright");
import assert from "node:assert/strict";

const BASE = process.env.BASE_URL || "http://localhost:8080";
const SHOTS = process.env.SHOTS_DIR;

const browser = await chromium.launch();
const results = [];
async function step(name, fn) {
  try { await fn(); results.push(`ok   ${name}`); }
  catch (e) { results.push(`FAIL ${name}: ${e.message}`); process.exitCode = 1; }
}

for (const [label, opts] of [["phone", devices["iPhone 13"]], ["desktop", { viewport: { width: 1280, height: 800 } }]]) {
  for (const scheme of ["dark", "light"]) {
    const ctx = await browser.newContext({ ...opts, colorScheme: scheme });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const keyRequests = [];
    page.on("websocket", (ws) => ws.on("framesent", (f) => keyRequests.push(String(f.payload))));

    await step(`${label}/${scheme}: shows sign-in`, async () => {
      await page.goto(BASE);
      await page.locator("#login").waitFor({ state: "visible" });
    });
    await step(`${label}/${scheme}: wrong key rejected`, async () => {
      await page.fill("#key", "wrong");
      await page.click("#login-form button");
      await page.locator("#login-error", { hasText: "wrong access key" }).waitFor();
    });
    await step(`${label}/${scheme}: sign in`, async () => {
      await page.fill("#key", "dev");
      await page.click("#login-form button");
      await page.locator("#app").waitFor({ state: "visible" });
      await page.locator("#status.on").waitFor();
    });
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/${label}-${scheme}-remote.png` });

    await step(`${label}/${scheme}: buttons send keys over websocket`, async () => {
      await page.waitForTimeout(300);
      await page.click('[data-key="KEY_UP"]');
      await page.click('[data-key="KEY_ENTER"]');
      await page.click('[data-key="KEY_VOLUP"]');
      await page.waitForTimeout(300);
      const sent = keyRequests.join(" ");
      for (const k of ["KEY_UP", "KEY_ENTER", "KEY_VOLUP"]) assert.ok(sent.includes(k), `${k} not sent`);
    });
    await step(`${label}/${scheme}: no horizontal scroll`, async () => {
      const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      assert.ok(over <= 0, `overflow ${over}px`);
    });
    await step(`${label}/${scheme}: apps tab lists and launches`, async () => {
      await page.click('[data-tab="apps"]');
      await page.locator("#apps button", { hasText: "Netflix" }).click();
      await page.locator("#toast", { hasText: "Opening Netflix" }).waitFor();
    });
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/${label}-${scheme}-apps.png` });
    await step(`${label}/${scheme}: keyboard tab sends text`, async () => {
      await page.click('[data-tab="keyboard"]');
      await page.fill("#text", "stranger things");
      await page.click("#text-form button[type=submit]");
      await page.locator("#toast", { hasText: "Sent" }).waitFor();
    });
    await step(`${label}/${scheme}: ask tab explains when Claude is not set up`, async () => {
      await page.click('[data-tab="ask"]');
      await page.fill("#ask", "open twitch");
      await page.click("#ask-send");
      await page.locator("#ask-log li.error", { hasText: "not set up" }).waitFor();
    });
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/${label}-${scheme}-ask.png` });
    await step(`${label}/${scheme}: power off then on`, async () => {
      await page.click('[data-tab="remote"]');
      await page.click("#power");
      await page.locator("#status.standby").waitFor({ timeout: 8000 });
      await page.click("#power");
      await page.locator("#status.on").waitFor({ timeout: 15000 });
    });
    await step(`${label}/${scheme}: sign out`, async () => {
      await page.click('[data-tab="keyboard"]');
      await page.click("#logout");
      await page.locator("#login").waitFor({ state: "visible" });
    });
    await step(`${label}/${scheme}: no JS errors`, async () => assert.deepEqual(errors, []));
    await ctx.close();
  }
}
await browser.close();
console.log(results.join("\n"));
