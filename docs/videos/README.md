# 视频位

这里放切好的讲解短片，learn 页面直接从这里读（Vite 在开发时把这个目录挂在 `/videos`，构建时拷进 `dist/videos`）：

- `docs/videos/<part>.mp4`：`video/parts.json` 里的十个 part，由 `video/record.mjs` 录的整段 webm 按 `video/timeline.json` 切出来；
- `docs/videos/problems/<id>.mp4`：`docs/problems.md` 的九个问题，素材是 `video/problems.walk.json`。

文件不存在时，页面显示「视频待生成」。
