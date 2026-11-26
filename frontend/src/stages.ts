// How each learn stage shows its single most important value (the "key"), in Chinese.
export type Stage = { id: string; label: string; part: string; anchor: string; status: string; at: string | null; data: any };

const t = (iso?: string) => (iso ? iso.slice(11, 23) : "");

export function keyText(s: Stage): string {
  const d = s.data;
  if (!d) return "";
  switch (s.id) {
    case "submit": return `job #${d.job.id} · ${d.transition} · run_at ${t(d.job.run_at)}`;
    case "heap": return `heap_size = ${d.heap_size} · 到期 ${t(d.run_at)}`;
    case "claim": return `lease_owner = ${d.lease_owner} · fencing_token = ${d.fencing_token}`;
    case "token": return d.event === "rate_token" ? `tokens_left = ${d.tokens_left} / ${d.capacity}` : `没拿到，推迟 ${d.wait_s}s`;
    case "heartbeat": return `第 ${d.heartbeats} 次续租 · lease_expires_at → ${t(d.last[d.last.length - 1].lease_expires_at)}`;
    case "stall": return `SIGSTOP pid ${d.pid} · ${d.seconds} 秒`;
    case "expire": return `${d.transition} · 过期 ${d.overdue_ms} ms 后被 ${d.reaped_by} 回收`;
    case "reclaim": return `fencing_token 1 → ${d.fencing_token} · ${d.lease_owner}`;
    case "stale": return `${d.op}: my_token ${d.my_token} ≠ current_token ${d.current_token} · 拒绝`;
    case "done": return `${d.transition} · by ${d.by}`;
    case "cache": return `X-Cache: ${d.x_cache} · hits ${d.stats.hits}`;
  }
  return "";
}

export const STATUS_ZH: Record<string, string> = { done: "完成", active: "进行中", pending: "等待" };
