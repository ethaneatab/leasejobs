import anchors from "./generated/anchors.json";

type Anchor = { path: string; start: number; end: number };
const A = anchors as Record<string, Anchor>;
export const REPO_URL: string = import.meta.env.VITE_REPO_URL;
export const GIT_SHA: string = import.meta.env.VITE_GIT_SHA;

/** GitHub link to the exact lines of one @learn anchor, pinned to the build's commit. */
export function srcLink(id: string): string {
  const a = A[id];
  if (!a) return "#missing-anchor-" + id;
  return `${REPO_URL}/blob/${GIT_SHA}/${a.path}#L${a.start}-L${a.end}`;
}

export function anchorInfo(id: string): Anchor | undefined {
  return A[id];
}
