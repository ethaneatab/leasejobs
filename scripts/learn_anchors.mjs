#!/usr/bin/env node
// Scan the repo for "@learn:<id>" ... "@learn:end <id>" comment pairs and write
// frontend/src/generated/anchors.json as {id: {path, start, end}}.
// start/end are the first and last line BETWEEN the two marker lines (1-based),
// so a link #L<start>-L<end> highlights exactly the code, not the markers.
import { readFileSync, writeFileSync, mkdirSync, readdirSync, statSync } from "node:fs";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
export const OUT = join(ROOT, "frontend/src/generated/anchors.json");
const SCAN = ["leasejobs", "sql", "docs/problems/snippets", "frontend/src", "scripts", "tests"];
const EXT = /\.(py|sql|ts|tsx|mjs|sh)$/;
const SKIP = /(node_modules|__pycache__|generated|\.venv)/;

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (SKIP.test(p)) continue;
    if (statSync(p).isDirectory()) yield* files(p);
    else if (EXT.test(name)) yield p;
  }
}

export function scan() {
  const anchors = {};
  const errors = [];
  for (const top of SCAN) {
    let it;
    try { it = [...files(join(ROOT, top))]; } catch { continue; }
    for (const file of it) {
      if (file.endsWith("learn_anchors.mjs") || file.endsWith("check_anchors.mjs")) continue;
      const rel = relative(ROOT, file);
      const lines = readFileSync(file, "utf8").split("\n");
      const open = {};
      lines.forEach((line, i) => {
        const end = line.match(/@learn:end\s+([\w-]+)/);
        const start = !end && line.match(/@learn:([\w-]+)/);
        if (start) {
          const id = start[1];
          if (anchors[id] || open[id]) errors.push(`${rel}:${i + 1} duplicate anchor ${id}`);
          open[id] = i + 1;
        } else if (end) {
          const id = end[1];
          if (!open[id]) { errors.push(`${rel}:${i + 1} end without start: ${id}`); return; }
          anchors[id] = { path: rel, start: open[id] + 1, end: i };
          delete open[id];
        }
      });
      for (const id of Object.keys(open)) errors.push(`${rel}:${open[id]} anchor ${id} never ends`);
    }
  }
  const sorted = Object.fromEntries(Object.keys(anchors).sort().map((k) => [k, anchors[k]]));
  return { anchors: sorted, errors };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const { anchors, errors } = scan();
  if (errors.length) { console.error(errors.join("\n")); process.exit(1); }
  mkdirSync(dirname(OUT), { recursive: true });
  writeFileSync(OUT, JSON.stringify(anchors, null, 2) + "\n");
  console.log(`wrote ${Object.keys(anchors).length} anchors to ${relative(ROOT, OUT)}`);
}
