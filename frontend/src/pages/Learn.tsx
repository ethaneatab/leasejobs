import { useEffect, useRef, useState } from "react";
import { get, post, api } from "../api";
import { srcLink } from "../source";
import { keyText, STATUS_ZH, type Stage } from "../stages";
import { VideoSlot } from "../VideoSlot";

type View = { run: number; job_id: number; state: string; stages: Stage[]; finished: boolean };

export default function Learn() {
  const [view, setView] = useState<View | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [video, setVideo] = useState<string | null>(null);
  const [query, setQuery] = useState<{ cache: string | null; status: number } | null>(null);
  const timer = useRef<number | undefined>(undefined);

  const poll = (run: number) => {
    window.clearTimeout(timer.current);
    get<View>(`/learn/runs/${run}`).then((v) => {
      setView(v);
      if (!v.finished) timer.current = window.setTimeout(() => poll(run), 500);
    });
  };
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const start = async () => {
    setBusy(true);
    setOpen({});
    setQuery(null);
    const r = await post<View>("/learn/runs");
    setView(r.body);
    setBusy(false);
    poll(r.body.run);
  };

  const ask = async () => {
    if (!view) return;
    const r = await api(`/jobs/${view.job_id}/result`);
    setQuery({ cache: r.headers.get("x-cache"), status: r.status });
    poll(view.run);
  };

  const done = view?.stages.filter((s) => s.status === "done").length ?? 0;
  const claim = view?.stages.find((s) => s.id === "reclaim")?.data ?? view?.stages.find((s) => s.id === "claim")?.data;

  return (
    <div className="learn">
      <section className="learn-head card">
        <div>
          <h1>教学模式：一个任务的一生</h1>
          <p className="muted">点「提交样例」，后端排一个两秒后运行的任务。第一次尝试快写完时，持有它的 worker 会给自己发 SIGSTOP。
            下面每一步都在读数据库里真实的行，亮起来的顺序就是事情发生的顺序。</p>
        </div>
        <div className="learn-actions">
          <button className="primary" data-testid="submit-sample" onClick={start} disabled={busy}>提交样例</button>
          {view && (
            <div className="run-facts" data-testid="run-facts">
              <span>run #{view.run}</span><span>job #{view.job_id}</span>
              <span className={`pill s-${view.state}`} data-testid="job-state">{view.state}</span>
              <span>{done} / {view.stages.length}</span>
              {claim && <span className="lease-now">持有者 {claim.lease_owner} · token {claim.fencing_token}</span>}
            </div>
          )}
        </div>
      </section>

      {!view && <p className="muted empty">还没有运行。点右上角的按钮开始。</p>}
      <ol className="rail">
        {view?.stages.map((s, i) => (
          <li key={s.id} className={`stage ${s.status}`} data-stage={s.id} data-status={s.status}>
            <div className="dot">{i + 1}</div>
            <div className="stage-body card">
              <div className="stage-top">
                <h3>{s.label}</h3>
                <span className={`status ${s.status}`}>{STATUS_ZH[s.status]}</span>
                {s.at && <time>{s.at.slice(11, 23)}</time>}
              </div>
              {s.data && <div className="stage-key" data-key={s.id}>{keyText(s)}</div>}
              {s.id === "cache" && (
                <div className="query-row">
                  <button data-testid="query-result" onClick={ask}
                          disabled={view.stages.find((x) => x.id === "done")?.status !== "done"}>查询结果</button>
                  {query && <span className="query-answer" data-testid="query-answer">HTTP {query.status} · X-Cache: {query.cache}</span>}
                </div>
              )}
              <div className="stage-tools">
                <button className="ghost" data-testid={`expand-${s.id}`} disabled={!s.data}
                        onClick={() => setOpen({ ...open, [s.id]: !open[s.id] })}>{open[s.id] ? "收起数据" : "展开数据"}</button>
                <a className="src-link" data-anchor={s.anchor} href={srcLink(s.anchor)} target="_blank" rel="noreferrer">看源码</a>
                <button className="ghost" data-testid={`video-${s.id}`}
                        onClick={() => setVideo(video === s.id ? null : s.id)}>看讲解视频</button>
              </div>
              {open[s.id] && s.data && <pre className="json" data-json={s.id}>{JSON.stringify(s.data, null, 2)}</pre>}
              {video === s.id && <VideoSlot src={`/videos/${s.part}.mp4`} label={s.part} />}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
