import raw from "../../../docs/problems.md?raw";
import { html, sections } from "../md";
import { VideoSlot } from "../VideoSlot";

export default function Problems() {
  const { intro, items } = sections(raw, 2);
  return (
    <div className="doc" data-testid="problems">
      <div className="card doc-head"><div dangerouslySetInnerHTML={{ __html: html(intro) }} /></div>
      {items.map((p) => (
        <details key={p.id} className="card problem" data-problem={p.id}>
          <summary>{p.title}</summary>
          <VideoSlot src={`/videos/problems/${p.id}.mp4`} label={p.id} />
          <div className="answer" dangerouslySetInnerHTML={{ __html: html(p.body) }} />
        </details>))}
    </div>
  );
}
