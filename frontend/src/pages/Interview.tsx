import { useState } from "react";
import raw from "../../../docs/interview.md?raw";
import { html, sections } from "../md";

export default function Interview() {
  const groups = sections(raw, 2).items;
  const [q, setQ] = useState("");
  return (
    <div className="doc" data-testid="interview">
      <div className="doc-head card">
        <h1>面试题</h1>
        <input placeholder="搜索题目" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {groups.map((g) => {
        const items = sections(g.body, 3).items.filter((s) => !q || s.title.includes(q) || s.body.includes(q));
        if (!items.length) return null;
        return (
          <section key={g.title} className="card">
            <h2>{g.title}</h2>
            {items.map((s) => (
              <details key={s.title} className="qa" data-q={s.id}>
                <summary>{s.title}</summary>
                <div className="answer" dangerouslySetInnerHTML={{ __html: html(s.body) }} />
              </details>))}
          </section>);
      })}
    </div>
  );
}
