#!/usr/bin/env node
// Three checks, all must pass (CI runs this):
//  1. every anchor id the frontend or video/problems.src.json refers to exists;
//  2. every anchor's line range is inside its file, with the markers right outside it;
//  3. frontend/src/generated/anchors.json is what learn_anchors.mjs would write now.
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join } from "node:path";
import { scan, ROOT, OUT } from "./learn_anchors.mjs";

const fail = [];
const { anchors, errors } = scan();
fail.push(...errors);

// 1. references
const refs = new Map();
function* walk(d) { for (const n of readdirSync(d)) { const p = join(d, n); if (n === "generated") continue;
  if (statSync(p).isDirectory()) yield* walk(p); else if (/\.(ts|tsx)$/.test(n)) yield p; } }
for (const f of walk(join(ROOT, "frontend/src"))) {
  for (const m of readFileSync(f, "utf8").matchAll(/anchor:\s*"([\w-]+)"|srcLink\("([\w-]+)"\)/g)) refs.set(m[1] || m[2], f);
}
const stagesPy = readFileSync(join(ROOT, "leasejobs/learn.py"), "utf8");
for (const m of stagesPy.matchAll(/\("[\w]+", "[^"]+", "p[\w-]+", "([\w-]+)"\)/g)) refs.set(m[1], "leasejobs/learn.py STAGES");
const probs = join(ROOT, "video/problems.src.json");
if (existsSync(probs)) for (const p of JSON.parse(readFileSync(probs, "utf8"))) {
  for (const k of ["code_from", "before_from"]) if (p[k]) refs.set(p[k].anchor, `problems.src.json ${p.id}.${k}`);
}
for (const [id, where] of refs) if (!anchors[id]) fail.push(`missing anchor "${id}" (used in ${where})`);

// 2. ranges and markers
for (const [id, a] of Object.entries(anchors)) {
  const lines = readFileSync(join(ROOT, a.path), "utf8").split("\n");
  if (a.start < 2 || a.end > lines.length || a.start > a.end) { fail.push(`${id}: bad range ${a.start}-${a.end} in ${a.path}`); continue; }
  if (!lines[a.start - 2].includes(`@learn:${id}`)) fail.push(`${id}: start marker not at line ${a.start - 1}`);
  if (!lines[a.end].includes(`@learn:end ${id}`)) fail.push(`${id}: end marker not at line ${a.end + 1}`);
}

// 3. freshness
const onDisk = existsSync(OUT) ? readFileSync(OUT, "utf8") : "";
if (onDisk !== JSON.stringify(anchors, null, 2) + "\n") fail.push("frontend/src/generated/anchors.json is stale: run node scripts/learn_anchors.mjs");

if (fail.length) { console.error("check_anchors: FAIL\n  " + fail.join("\n  ")); process.exit(1); }
console.log(`check_anchors: OK (${Object.keys(anchors).length} anchors, ${refs.size} references)`);
