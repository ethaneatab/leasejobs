import { useEffect, useState } from "react";

/** Plays src if the file exists; otherwise says the video is still to be made. Never throws. */
export function VideoSlot({ src, label }: { src: string; label: string }) {
  const [ok, setOk] = useState<boolean | null>(null);
  useEffect(() => {
    let live = true;
    fetch(src, { method: "HEAD" })
      .then((r) => live && setOk(r.ok && (r.headers.get("content-type") ?? "").startsWith("video")))
      .catch(() => live && setOk(false));
    return () => { live = false; };
  }, [src]);
  if (ok === null) return <div className="video-slot muted">检查视频…</div>;
  if (!ok) return <div className="video-slot pending" data-video={src}>视频待生成 · {label}</div>;
  return <video className="video-slot" src={src} controls preload="metadata" data-video={src} />;
}
