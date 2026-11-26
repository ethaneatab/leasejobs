#!/usr/bin/env node
// Check the smoke recording: every red box non-empty and inside the 1600x900 frame, then
// cut a frame at the middle of a few segments (default: s05 s18 s22 s32) into video/out/frames/
// so a human can look at them.  node video/check_frames.mjs [ids...]
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const tl = JSON.parse(readFileSync(join(HERE, "timeline.json"), "utf8"));
const [W, H] = tl.size;
const problems = [];
for (const s of tl.segments) {
  if (s.highlight && !s.rects.length) problems.push(`${s.id}: highlight ${s.highlight} drew no box`);
  for (const r of s.rects) {
    if (r.w < 2 || r.h < 2) problems.push(`${s.id}: empty box`);
    if (r.x - r.pad < -2 || r.y - r.pad < -2 || r.x + r.w + r.pad > W + 2 || r.y + r.h + r.pad > H + 2)
      problems.push(`${s.id}: box outside the frame ${JSON.stringify(r)}`);
  }
}
const ids = process.argv.slice(2).length ? process.argv.slice(2) : ["s05", "s18", "s22", "s32"];
mkdirSync(join(HERE, "out/frames"), { recursive: true });
for (const id of ids) {
  const s = tl.segments.find((x) => x.id === id);
  const t = (s.startMs + Math.min(s.durMs - 200, 2500)) / 1000;
  execFileSync("ffmpeg", ["-v", "error", "-y", "-ss", String(t), "-i", join(HERE, "out/learn.webm"), "-frames:v", "1",
                          join(HERE, `out/frames/${id}.png`)]);
  console.log(`frame ${id} at ${t.toFixed(1)}s -> video/out/frames/${id}.png  (${s.highlight}${s.text_match ? " / " + s.text_match : ""})`);
}
if (problems.length) { console.error("FAIL\n" + problems.join("\n")); process.exit(1); }
console.log(`boxes OK: ${tl.segments.filter((s) => s.rects.length).length} segments with a box, all inside ${W}x${H}`);
