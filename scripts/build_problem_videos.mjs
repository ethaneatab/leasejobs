#!/usr/bin/env node
// video/problems.src.json -> video/problems.walk.json for the walkthrough renderer.
// Code is never copied into the JSON by hand: code_from / before_from name a
// @learn anchor, and the code is cut out of the source file here. Nested
// marker lines (@learn:...) are dropped, and `lines` count from 1 in what is left.
// Checks: line numbers in range; narration has no code symbols; prints every
// highlighted line so a human can eyeball that the narration matches the code.
//   node scripts/build_problem_videos.mjs           build + print
//   node scripts/build_problem_videos.mjs --show ID print an anchor's numbered code
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { scan, ROOT } from "./learn_anchors.mjs";

const { anchors } = scan();
function cut(ref) {
  const a = anchors[ref.anchor];
  if (!a) throw new Error(`unknown anchor ${ref.anchor}`);
  if (ref.path && ref.path !== a.path) throw new Error(`anchor ${ref.anchor} lives in ${a.path}, not ${ref.path}`);
  const lines = readFileSync(join(ROOT, a.path), "utf8").split("\n").slice(a.start - 1, a.end)
    .filter((l) => !l.includes("@learn:"));
  const indent = Math.min(...lines.filter((l) => l.trim()).map((l) => l.match(/^ */)[0].length));
  return lines.map((l) => l.slice(indent));
}

if (process.argv[2] === "--show") {
  cut({ anchor: process.argv[3] }).forEach((l, i) => console.log(String(i + 1).padStart(2), l));
  process.exit(0);
}

const BAD = /[_`/\\<>=#*{}\[\]|]|->|==|\d/; // code symbols, markdown, and digits (numbers are spoken in Chinese)
const src = JSON.parse(readFileSync(join(ROOT, "video/problems.src.json"), "utf8"));
const out = [];
const errors = [];
for (const p of src) {
  const code = cut(p.code_from);
  const before = p.before_from ? cut(p.before_from) : [];
  const check = (segs, lines, label) => segs.forEach((s, i) => {
    if (BAD.test(s.text)) errors.push(`${p.id} ${label}[${i}] narration has a code symbol or digit: ${s.text}`);
    for (const n of s.lines) if (n < 1 || n > lines.length) errors.push(`${p.id} ${label}[${i}] line ${n} out of 1-${lines.length}`);
    console.log(`${p.id} ${label}[${i}] ${s.text}`);
    for (const n of s.lines) console.log(`      ${String(n).padStart(2)} | ${lines[n - 1] ?? "<out of range>"}`);
  });
  const total = (p.segments?.length ?? 0) + (p.before_segments?.length ?? 0);
  if (total < 6 || total > 14) errors.push(`${p.id}: ${total} segments, want 6-14`);
  check(p.before_segments ?? [], before, "before");
  check(p.segments, code, "after");
  out.push({ id: p.id, title: p.title, subtitle: p.subtitle, lang: p.lang, code: code.join("\n"),
             segments: p.segments, example_code: before.join("\n"), example_segments: p.before_segments ?? [] });
}
if (errors.length) { console.error("\nFAIL\n" + errors.join("\n")); process.exit(1); }
writeFileSync(join(ROOT, "video/problems.walk.json"), JSON.stringify(out, null, 2) + "\n");
console.log(`\nwrote video/problems.walk.json (${out.length} problems)`);
