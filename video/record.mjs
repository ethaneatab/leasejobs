#!/usr/bin/env node
// Record the walkthrough: one 1600x900 webm of the real app, driven by video/narration.json.
// Each segment: run its action -> draw a red box around `highlight` -> stay for the segment's
// duration (video/durations.json {id: seconds}; 4 s when absent).
// Output: video/out/learn.webm and video/timeline.json {video, segments:[{id, part, startMs, ...}]}.
// Needs ./scripts/demo.sh running, and nothing else (tests) using this stack.
import { chromium } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, renameSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const BASE = process.env.RECORD_BASE_URL ?? "http://127.0.0.1:5191";
const OUT = join(HERE, "out");
const segs = JSON.parse(readFileSync(join(HERE, "narration.json"), "utf8"));
const durFile = join(HERE, "durations.json");
const durations = existsSync(durFile) ? JSON.parse(readFileSync(durFile, "utf8")) : {};
const DEFAULT_S = 4;

// narration rules, checked before we spend minutes recording
const bad = segs.filter((s) => /[0-9_`/\\=<>#*]|->/.test(s.text));
if (bad.length) { console.error("narration has digits or code symbols:", bad.map((s) => s.id)); process.exit(1); }
const parts = segs.map((s) => s.part);
for (let i = 1; i < parts.length; i++) {
  if (parts[i] !== parts[i - 1] && parts.slice(0, i).includes(parts[i])) { console.error(`part ${parts[i]} is not contiguous`); process.exit(1); }
}

const BOX_CSS = `
#rec-box { position: fixed; pointer-events: none; z-index: 99999; border: 3px solid #e5383b; border-radius: 10px;
  box-shadow: 0 0 0 6px rgba(229,56,59,.25); animation: rec-pop .45s ease-out, rec-pulse 1.6s ease-in-out .45s infinite; }
#rec-box.circle { border-radius: 50%; }
@keyframes rec-pop { from { transform: scale(1.25); opacity: 0 } to { transform: scale(1); opacity: 1 } }
@keyframes rec-pulse { 0%,100% { box-shadow: 0 0 0 6px rgba(229,56,59,.25) } 50% { box-shadow: 0 0 0 11px rgba(229,56,59,.12) } }
#rec-caption { position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%); z-index: 99999; max-width: 1300px;
  background: rgba(20,24,36,.92); color: #fff; font: 15px/1.4 Menlo, monospace; padding: 8px 14px; border-radius: 8px; }`;

async function rectOf(page, selector, textMatch) {
  return page.evaluate(([sel, txt]) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    el.scrollIntoView({ block: "center", inline: "nearest" });
    let r = el.getBoundingClientRect();
    if (txt) { // shrink the box to the first occurrence of txt inside el
      const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      for (let n = walk.nextNode(); n; n = walk.nextNode()) {
        const i = n.textContent.indexOf(txt);
        if (i >= 0) {
          const range = document.createRange();
          const line = n.textContent.indexOf("\n", i);
          range.setStart(n, i);
          range.setEnd(n, line > i ? line : n.textContent.length);
          r = range.getBoundingClientRect();
          break;
        }
      }
    }
    return { x: r.x, y: r.y, w: r.width, h: r.height };
  }, [selector, textMatch ?? null]);
}

async function box(page, selector, shape, textMatch) {
  await page.evaluate(() => document.getElementById("rec-box")?.remove());
  if (!selector) return null;
  await page.waitForSelector(selector, { timeout: 20000 });
  await page.waitForTimeout(250); // let scrollIntoView settle
  const r = await rectOf(page, selector, textMatch);
  if (!r || r.w < 2 || r.h < 2) throw new Error(`empty box for ${selector} ${textMatch ?? ""}`);
  const pad = shape === "circle" ? 14 : 6;
  await page.evaluate(([r, pad, shape]) => {
    const d = document.createElement("div");
    d.id = "rec-box";
    if (shape === "circle") d.className = "circle";
    Object.assign(d.style, { left: `${r.x - pad}px`, top: `${r.y - pad}px`, width: `${r.w + 2 * pad}px`, height: `${r.h + 2 * pad}px` });
    document.body.appendChild(d);
  }, [r, pad, shape]);
  return { ...r, pad };
}

async function caption(page, text) {
  await page.evaluate((t) => {
    document.getElementById("rec-caption")?.remove();
    if (!t) return;
    const d = document.createElement("div");
    d.id = "rec-caption";
    d.textContent = t;
    document.body.appendChild(d);
  }, text);
}

const state = { jobId: null, holder: null };
const stageDone = (page, id) => page.waitForSelector(`[data-stage="${id}"][data-status="done"]`, { timeout: 45000 });

// Actions. "pre" ones run before the box appears; "click" ones box first, pause, then click.
const ACTIONS = {
  none: {},
  open_learn: { pre: async (p) => { await p.goto(`${BASE}/learn`); await p.waitForSelector(".learn-head h1"); } },
  click_submit: { click: async (p) => { await p.click('[data-testid="submit-sample"]'); await stageDone(p, "submit"); } },
  wait_stage: { pre: async (p, s) => stageDone(p, s.arg) },
  expand: { click: async (p, s) => { await p.click(`[data-testid="expand-${s.arg}"]`); await p.waitForSelector(`[data-json="${s.arg}"]`); } },
  show_src: { click: async (p, s) => {
    const href = await p.getAttribute(`[data-stage="${s.arg}"] a.src-link`, "href");
    await caption(p, `看源码 → ${href}`);
  } },
  click_query: { pre: async (p) => stageDone(p, "done"),
                 click: async (p) => { await p.click('[data-testid="query-result"]'); await p.waitForSelector('[data-testid="query-answer"]'); } },
  click_video: { click: async (p, s) => { await p.click(`[data-testid="video-${s.arg}"]`); } },
  dash_pick_flaky: { pre: async (p) => {
    await caption(p, null);
    await p.click('a[data-tab="/"]');
    await p.waitForSelector('[data-testid="job-type"]');
  }, click: async (p) => {
    await p.selectOption('[data-testid="job-type"]', "flaky");
    await p.fill('[data-testid="job-payload"]', '{"fail_times": 1}');
  } },
  dash_submit: { click: async (p) => { await p.click('[data-testid="job-submit"]'); await p.waitForSelector('[data-testid="job-detail"]'); } },
  wait_attempts: { pre: async (p) => p.waitForFunction(() =>
    document.querySelectorAll('[data-testid="attempts"] tbody tr').length >= 2 &&
    document.querySelector('[data-testid="attempts"]').textContent.includes("succeeded"), null, { timeout: 30000 }) },
  dash_long_job: { pre: async (p) => {
    await p.selectOption('[data-testid="job-type"]', "call_integration");
    await p.fill('[data-testid="job-payload"]', '{"integration": "ledger-api", "work_s": 8}');
  }, click: async (p) => {
    const before = await p.locator('[data-testid="submit-msg"]').textContent().catch(() => "");
    await p.click('[data-testid="job-submit"]');
    await p.waitForFunction((b) => { const t = document.querySelector('[data-testid="submit-msg"]')?.textContent ?? "";
      return t.includes("已提交") && t !== b; }, before, { timeout: 10000 });
    const msg = await p.locator('[data-testid="submit-msg"]').textContent();
    state.jobId = Number(msg.match(/#(\d+)/)[1]);
    await p.waitForFunction((id) => [...document.querySelectorAll('[data-testid="workers"] .worker.busy')]
      .some((w) => w.textContent.includes(`#${id}`)), state.jobId, { timeout: 20000 });
  } },
  kill_holder: {
    pre: async (p) => {
      // point the highlight at the exact worker holding our job
      await p.evaluate((id) => {
        document.querySelectorAll("[data-rec-target]").forEach((e) => e.removeAttribute("data-rec-target"));
        const w = [...document.querySelectorAll('[data-testid="workers"] .worker.busy')].find((x) => x.textContent.includes(`#${id}`));
        w.querySelector("button").setAttribute("data-rec-target", "1");
        window.__recHolder = w.getAttribute("data-worker");
      }, state.jobId);
      state.holder = await p.evaluate(() => window.__recHolder);
    },
    highlight: '[data-rec-target="1"]',
    click: async (p) => {
      await p.click('[data-rec-target="1"]');
      await p.waitForSelector(`[data-worker="${state.holder}"].dead`, { timeout: 10000 });
    },
    after: () => `[data-worker="${state.holder}"]`,
  },
  wait_dead: { pre: async (p) => p.waitForSelector(`[data-worker="${state.holder}"].dead`, { timeout: 15000 }),
               highlight: () => `[data-worker="${state.holder}"]` },
  wait_reclaimed: { pre: async (p) => p.waitForFunction(() =>
    document.querySelector('[data-testid="lease-timeline"]')?.textContent.includes("lease_expired"), null, { timeout: 30000 }) },
};

mkdirSync(OUT, { recursive: true });
for (const f of readdirSync(OUT)) if (f.endsWith(".webm")) rmSync(join(OUT, f));
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 900 }, recordVideo: { dir: OUT, size: { width: 1600, height: 900 } } });
await context.addInitScript((css) => {
  addEventListener("DOMContentLoaded", () => { const s = document.createElement("style"); s.textContent = css; document.head.appendChild(s); });
}, BOX_CSS);
const page = await context.newPage();
const t0 = Date.now();
const timeline = [];
for (const s of segs) {
  const planned = (durations[s.id] ?? DEFAULT_S) * 1000;
  const start = Date.now();
  const act = ACTIONS[s.action];
  if (!act) throw new Error(`unknown action ${s.action} in ${s.id}`);
  if (s.action !== "show_src") await caption(page, null);
  if (act.pre) await act.pre(page, s);
  const hl = typeof act.highlight === "function" ? act.highlight() : (act.highlight ?? s.highlight);
  let rect = await box(page, hl, s.shape, s.text_match);
  const rects = rect ? [rect] : [];
  if (act.click) {
    await page.waitForTimeout(Math.min(1200, planned * 0.35)); // show the box, then click
    await act.click(page, s);
    if (s.action !== "click_submit" && s.action !== "dash_long_job") {
      // re-box after the click: the element may have moved, or (kill) gone away
      const next = act.after ? act.after() : hl;
      rect = await box(page, next, act.after ? "box" : s.shape, act.after ? null : s.text_match).catch(() => null);
      if (rect) rects.push(rect);
      else await page.evaluate(() => document.getElementById("rec-box")?.remove()); // never leave an empty box
    }
  }
  const left = planned - (Date.now() - start);
  if (left > 0) await page.waitForTimeout(left);
  timeline.push({ id: s.id, part: s.part, startMs: start - t0, durMs: Date.now() - start, plannedMs: planned,
                  highlight: hl, shape: s.shape ?? "box", text_match: s.text_match ?? null, rects });
  console.log(`${s.id} ${s.part} ${s.action} ${((Date.now() - start) / 1000).toFixed(1)}s`);
}
await page.evaluate(() => document.getElementById("rec-box")?.remove());
await page.waitForTimeout(600);
const videoPath = await page.video().path();
await context.close();
await browser.close();
const final = join(OUT, "learn.webm");
renameSync(videoPath, final);
writeFileSync(join(HERE, "timeline.json"), JSON.stringify({ video: "video/out/learn.webm", size: [1600, 900], segments: timeline }, null, 2) + "\n");
console.log(`wrote ${final} and video/timeline.json (${timeline.length} segments, ${((Date.now() - t0) / 1000).toFixed(0)} s)`);
