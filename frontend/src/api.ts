// All calls go through the Vite proxy (/api -> :8910) with the dev placeholder key.
const KEY = import.meta.env.VITE_API_KEY ?? "dev-key-alice";

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<{ status: number; body: T; headers: Headers }> {
  const r = await fetch(`/api${path}`, {
    ...init,
    headers: { "X-API-Key": KEY, "content-type": "application/json", ...(init.headers ?? {}) },
  });
  const text = await r.text();
  return { status: r.status, body: text ? JSON.parse(text) : (null as T), headers: r.headers };
}

export const get = <T = any>(p: string) => api<T>(p).then((r) => r.body);
export const post = <T = any>(p: string, body?: unknown) =>
  api<T>(p, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
