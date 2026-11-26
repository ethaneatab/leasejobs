import { useEffect, useState } from "react";
import { api, get, post } from "../api";

const DEFAULTS: Record<string, object> = {
  call_integration: { integration: "ledger-api", work_s: 4, amount: 10 },
  sleep: { seconds: 5 },
  report: { rows: 20000, seed: 7 },
  flaky: { fail_times: 2 },
  noop: {},
  block: { seconds: 3 },
};

function usePoll<T>(path: string, ms: number, dep: unknown = null): T | null {
  const [v, setV] = useState<T | null>(null);
  useEffect(() => {
    let live = true;
    const tick = () => get<T>(path).then((x) => live && setV(x)).catch(() => {});
    tick();
    const h = window.setInterval(tick, ms);
    return () => { live = false; window.clearInterval(h); };
  }, [path, ms, dep]);
  return v;
}

function SubmitForm({ onDone }: { onDone: (id: number) => void }) {
  const [type, setType] = useState("call_integration");
  const [payload, setPayload] = useState(JSON.stringify(DEFAULTS.call_integration));
  const [delay, setDelay] = useState("0");
  const [maxAttempts, setMax] = useState("3");
  const [timeout, setTimeoutS] = useState("30");
  const [msg, setMsg] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    let body: any;
    try { body = JSON.parse(payload); } catch { setMsg("payload 不是合法 JSON"); return; }
    const r = await post("/jobs", { type, payload: body, delay_s: Number(delay) || undefined,
                                    max_attempts: Number(maxAttempts), timeout_s: Number(timeout) });
    if (r.status >= 300) setMsg(`HTTP ${r.status}: ${JSON.stringify((r.body as any).detail)}`);
    else { setMsg(`已提交 job #${(r.body as any).id}，状态 ${(r.body as any).state}`); onDone((r.body as any).id); }
  };
  return (
    <form className="card form" onSubmit={submit} data-testid="submit-form">
      <h2>提交任务</h2>
      <label>类型
        <select value={type} data-testid="job-type"
                onChange={(e) => { setType(e.target.value); setPayload(JSON.stringify(DEFAULTS[e.target.value] ?? {})); }}>
          {Object.keys(DEFAULTS).map((k) => <option key={k}>{k}</option>)}
        </select>
      </label>
      <label>payload<textarea value={payload} rows={3} data-testid="job-payload" onChange={(e) => setPayload(e.target.value)} /></label>
      <div className="row3">
        <label>延迟秒<input value={delay} onChange={(e) => setDelay(e.target.value)} data-testid="job-delay" /></label>
        <label>最多尝试<input value={maxAttempts} onChange={(e) => setMax(e.target.value)} /></label>
        <label>超时秒<input value={timeout} onChange={(e) => setTimeoutS(e.target.value)} /></label>
      </div>
      <button className="primary" type="submit" data-testid="job-submit">提交</button>
      {msg && <p className="msg" data-testid="submit-msg">{msg}</p>}
    </form>
  );
}

function JobDetail({ id }: { id: number }) {
  const job = usePoll<any>(`/jobs/${id}`, 1000, id);
  const attempts = usePoll<any[]>(`/jobs/${id}/attempts`, 1000, id);
  const events = usePoll<any[]>(`/jobs/${id}/events`, 1000, id);
  if (!job) return null;
  const owners = Array.from(new Set((events ?? []).map((e) => e.actor).filter((a: string) => a.startsWith("worker"))));
  const rows: any[] = [];
  for (const e of events ?? []) {   // fold runs of heartbeats into one line
    const last = rows[rows.length - 1];
    if (e.kind === "heartbeat" && last?.kind === "heartbeat" && last.actor === e.actor) { last.n += 1; last.lastAt = e.at; last.detail = e.detail; }
    else rows.push({ ...e, n: 1, lastAt: e.at });
  }
  return (
    <div className="card detail" data-testid="job-detail">
      <h2>job #{job.id} <span className={`pill s-${job.state}`}>{job.state}</span></h2>
      <p className="muted">{job.type} · attempts {job.attempts}/{job.max_attempts} · fencing_token {job.fencing_token}
        {job.last_error && <> · <span className="err">{job.last_error}</span></>}</p>
      <h3>尝试历史</h3>
      <table className="grid" data-testid="attempts">
        <thead><tr><th>#</th><th>worker</th><th>token</th><th>outcome</th><th>error</th></tr></thead>
        <tbody>{(attempts ?? []).map((a) => (
          <tr key={a.attempt_no}><td>{a.attempt_no}</td><td>{a.worker_id}</td><td>{a.fencing_token}</td>
            <td><span className={`pill o-${a.outcome}`}>{a.outcome}</span></td><td className="err">{a.error}</td></tr>))}</tbody>
      </table>
      <h3>租约时间线</h3>
      <ol className="lease-line" data-testid="lease-timeline">
        {rows.map((e) => (
          <li key={e.id} className={`ev k-${e.kind}`} style={{ marginLeft: `${Math.max(0, owners.indexOf(e.actor)) * 14}px` }}>
            <time>{e.at.slice(11, 23)}</time>
            <b>{e.kind}{e.n > 1 ? ` ×${e.n}` : ""}</b>
            {e.from_state || e.to_state ? <span className="tr">{e.from_state ?? "∅"} → {e.to_state}</span> : null}
            <span className="actor">{e.actor}</span>
            {e.detail?.fencing_token !== undefined && <span className="tok">token {e.detail.fencing_token}</span>}
            {e.kind === "heartbeat" && <span className="muted">至 {e.detail.lease_expires_at?.slice(11, 23)}</span>}
            {e.kind === "stale_write_rejected" && <span className="err">{e.detail.op}: {e.detail.my_token} &lt; {e.detail.current_token}</span>}
          </li>))}
      </ol>
    </div>
  );
}

export default function Dashboard() {
  const [selected, setSelected] = useState<number | null>(null);
  const [bump, setBump] = useState(0);
  const jobs = usePoll<any[]>("/jobs?limit=25", 1000, bump);
  const workers = usePoll<any[]>("/workers", 1000);
  const sched = usePoll<any>("/scheduler", 1000);
  const buckets = usePoll<any[]>("/integrations", 1000);
  const cache = usePoll<any>("/cache", 2000);
  const stalled = usePoll<any[]>("/stalled", 2000);
  const kill = async (id: string) => { await api(`/workers/${id}/kill`, { method: "POST" }); };
  return (
    <div className="dash">
      <div className="col">
        <SubmitForm onDone={(id) => { setSelected(id); setBump(bump + 1); }} />
        <div className="card">
          <h2>任务</h2>
          <table className="grid" data-testid="jobs">
            <thead><tr><th>id</th><th>type</th><th>state</th><th>try</th><th>holder</th><th>token</th></tr></thead>
            <tbody>{(jobs ?? []).map((j) => (
              <tr key={j.id} data-job={j.id} className={selected === j.id ? "sel" : ""} onClick={() => setSelected(j.id)}>
                <td>{j.id}</td><td>{j.type}</td><td><span className={`pill s-${j.state}`}>{j.state}</span></td>
                <td>{j.attempts}/{j.max_attempts}</td><td>{j.lease_owner ?? ""}</td><td>{j.fencing_token}</td></tr>))}</tbody>
          </table>
        </div>
        {selected && <JobDetail id={selected} />}
      </div>
      <div className="col side">
        <div className="card" data-testid="workers">
          <h2>Workers</h2>
          {(workers ?? []).filter((w) => w.alive || w.seen_s_ago < 30).map((w) => (
            <div key={w.worker_id} className={`worker ${w.alive ? "alive" : "dead"}${w.alive && w.current_job ? " busy" : ""}`} data-worker={w.worker_id}>
              <span className="wname">{w.worker_id}</span>
              <span className="muted">pid {w.pid} · :{w.port ?? "-"}</span>
              <span>{w.alive ? (w.current_job ? `跑 #${w.current_job}` : "空闲") : w.status}</span>
              {w.alive && w.port && <button className="danger" data-testid={`kill-${w.worker_id}`} onClick={() => kill(w.worker_id)}>杀掉</button>}
            </div>))}
        </div>
        <div className="card" data-testid="heap">
          <h2>调度堆 <span className="muted">size {sched?.heap_size ?? 0} · leader {sched?.leader ?? "无"}</span></h2>
          <p className="muted small">堆里最先到期的几项（调度器每轮写一次快照）</p>
          <ol className="heap">
            {(sched?.top ?? []).map((e: any, i: number) => (
              <li key={e.key} style={{ ["--w" as any]: `${Math.min(100, Math.max(4, 100 - e.in_s * 3))}%` }}>
                <span className="hk">{i === 0 ? "▲ " : ""}{e.key}</span><span className="muted">{e.in_s}s</span></li>))}
          </ol>
        </div>
        <div className="card" data-testid="buckets">
          <h2>限速桶</h2>
          {(buckets ?? []).map((b) => (
            <div key={b.name} className="bucket" data-bucket={b.name}>
              <span>{b.name}</span>
              <div className="bar"><i style={{ width: `${(100 * b.tokens_now) / b.capacity}%` }} /></div>
              <span className="muted">{b.tokens_now.toFixed(1)} / {b.capacity} · +{b.refill_per_s}/s</span>
            </div>))}
        </div>
        <div className="card" data-testid="cache">
          <h2>结果缓存 <span className="muted">LRU</span></h2>
          {cache && <p className="muted">size {cache.size}/{cache.capacity} · hits {cache.hits} · misses {cache.misses} · evictions {cache.evictions}</p>}
          {cache && <p className="mono small">新 → 旧：{cache.keys_mru.join(" · ") || "（空）"}</p>}
        </div>
        <div className="card" data-testid="stalled">
          <h2>卡住的任务</h2>
          {(stalled ?? []).length === 0 ? <p className="muted">没有</p> :
            (stalled ?? []).map((s) => <p key={s.reason + s.id} className="err">#{s.id} {s.reason} {Math.round(s.seconds)}s</p>)}
        </div>
      </div>
    </div>
  );
}
