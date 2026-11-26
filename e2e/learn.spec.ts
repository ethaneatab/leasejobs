import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const ORDER = ["submit", "heap", "claim", "token", "heartbeat", "stall", "expire", "reclaim", "stale", "done"];

test("learn: stages light up in order with real data, source links point at real lines", async ({ page }) => {
  await page.goto("/learn");
  await page.getByTestId("submit-sample").click();
  await expect(page.locator('[data-stage="submit"]')).toHaveAttribute("data-status", "done");

  // Watch the page (not the API) and write down when each stage first turns done.
  const seen: string[] = [];
  const deadline = Date.now() + 45_000;
  while (seen.length < ORDER.length && Date.now() < deadline) {
    for (const id of ORDER) {
      if (seen.includes(id)) continue;
      if ((await page.locator(`[data-stage="${id}"]`).getAttribute("data-status")) === "done") seen.push(id);
    }
    await page.waitForTimeout(100);
  }
  // Stages that finish within one poll can appear together; the order among
  // them still has to match the page order.
  expect(seen).toEqual(ORDER);

  await expect(page.locator('[data-key="claim"]')).toContainText("fencing_token = 1");
  await expect(page.locator('[data-key="reclaim"]')).toContainText("fencing_token 1 → 2");
  await expect(page.locator('[data-key="stale"]')).toContainText("my_token 1 ≠ current_token 2");
  await expect(page.locator('[data-key="done"]')).toContainText("RUNNING → SUCCEEDED");

  // real row JSON behind a stage
  await page.getByTestId("expand-expire").click();
  await expect(page.locator('[data-json="expire"]')).toContainText('"expired_owner"');

  // the final stage only lights when the backend sees a cache hit
  await page.getByTestId("query-result").click();
  await expect(page.getByTestId("query-answer")).toContainText("X-Cache: HIT");
  await expect(page.locator('[data-stage="cache"]')).toHaveAttribute("data-status", "done");

  // every "看源码" link: right repo, pinned sha, and the lines exist with markers around them
  const links = page.locator("a.src-link");
  expect(await links.count()).toBe(11);
  for (const a of await links.all()) {
    const href = (await a.getAttribute("href"))!;
    const anchor = (await a.getAttribute("data-anchor"))!;
    const m = href.match(/^https:\/\/github\.com\/ethaneatab\/leasejobs\/blob\/([0-9a-f]{40})\/(.+)#L(\d+)-L(\d+)$/);
    expect(m, href).not.toBeNull();
    const [, , path, s, e] = m!;
    const lines = readFileSync(join(ROOT, path), "utf8").split("\n");
    expect(Number(e)).toBeLessThanOrEqual(lines.length);
    expect(lines[Number(s) - 2]).toContain(`@learn:${anchor}`);
    expect(lines[Number(e)]).toContain(`@learn:end ${anchor}`);
  }

  // video slot never errors: placeholder when the file isn't there yet
  await page.getByTestId("video-claim").click();
  await expect(page.locator('[data-stage="claim"] .video-slot')).toBeVisible();
});

test("interview tab: open one question and read the answer", async ({ page }) => {
  await page.goto("/learn/interview");
  const q = page.locator("details.qa").first();
  await q.locator("summary").click();
  await expect(q.locator(".answer")).toBeVisible();
  await expect(q.locator(".answer")).toContainText("一句话");
});

test("problems tab: expand one problem", async ({ page }) => {
  await page.goto("/learn/problems");
  const p = page.locator("details.problem").first();
  await p.locator("summary").click();
  await expect(p.locator(".answer")).toContainText("现象");
  await expect(p.locator(".video-slot")).toBeVisible();
});

test("dashboard: submit a flaky job, see retries; kill a worker from the UI", async ({ page, request }) => {
  await page.goto("/");
  await page.getByTestId("job-type").selectOption("flaky");
  await page.getByTestId("job-payload").fill('{"fail_times": 1}');
  await page.getByTestId("job-submit").click();
  await expect(page.getByTestId("submit-msg")).toContainText("已提交");
  const attempts = page.getByTestId("attempts");
  await expect(attempts.locator("tr")).toHaveCount(3, { timeout: 20_000 }); // header + failed + succeeded
  await expect(attempts).toContainText("succeeded");
  await expect(page.getByTestId("lease-timeline")).toContainText("retry_scheduled");

  const kill = page.locator('[data-testid^="kill-"]').first();
  const id = (await kill.getAttribute("data-testid"))!.slice(5);
  await kill.click();
  await expect(page.locator(`[data-worker="${id}"]`)).toHaveClass(/dead/, { timeout: 10_000 });
  // the supervisor brings up a new incarnation of that slot
  const slot = id.split(".")[0];
  const gen = (w: string) => Number(w.split(".g")[1] ?? 1);
  await expect(async () => {
    const alive = await page.locator(`[data-worker^="${slot}"].alive`).evaluateAll((els) => els.map((e) => e.getAttribute("data-worker")!));
    expect(alive.some((w) => gen(w) > gen(id))).toBe(true);
  }).toPass({ timeout: 15_000 });
});
