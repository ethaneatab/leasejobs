import { marked } from "marked";

export type Section = { level: number; title: string; id: string; body: string };

/** Split markdown into sections at headings of exactly `level` (e.g. 3 for "### "). */
export function sections(md: string, level: number): { intro: string; items: Section[] } {
  const mark = "#".repeat(level) + " ";
  const items: Section[] = [];
  let intro = "";
  let cur: Section | null = null;
  for (const line of md.split("\n")) {
    if (line.startsWith(mark)) {
      if (cur) items.push(cur);
      const title = line.slice(mark.length).trim();
      cur = { level, title, id: "", body: "" };
      continue;
    }
    if (cur) cur.body += line + "\n"; else intro += line + "\n";
  }
  if (cur) items.push(cur);
  for (const s of items) {
    const m = s.body.match(/<!--\s*id:\s*([\w-]+)\s*-->/);
    s.id = m ? m[1] : s.title.toLowerCase().replace(/[^\w]+/g, "-").replace(/^-|-$/g, "");
  }
  return { intro, items };
}

export const html = (md: string) => marked.parse(md, { async: false }) as string;
