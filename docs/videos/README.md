# 讲解视频

每个教学阶段一段短片，`/learn` 页面里每个阶段旁边的「看讲解视频」播放的就是这里的文件。清单见 [`video/parts.json`](../../video/parts.json)：

| 文件 | 内容 | 对应教程 |
|---|---|---|
| [`p0-overview.mp4`](p0-overview.mp4) | 这个项目在解决什么：一个任务的一生 | [09](../tutorial/09-learn-page.md) |
| [`p1-submit.mp4`](p1-submit.mp4) | 提交任务和显式状态机 | [01](../tutorial/01-state-machine.md) |
| [`p2-heap.mp4`](p2-heap.mp4) | 延迟任务进调度堆 | [04](../tutorial/04-scheduler-heap.md) |
| [`p3-claim.mp4`](p3-claim.mp4) | 认领就是拿租约 | [02](../tutorial/02-lease-claim.md) |
| [`p4-ratelimit.mp4`](p4-ratelimit.mp4) | 按集成限速的共享令牌桶 | [05](../tutorial/05-rate-limit.md) |
| [`p5-heartbeat.mp4`](p5-heartbeat.mp4) | 心跳、卡死、租约过期 | [03](../tutorial/03-expiry-and-fencing.md) |
| [`p6-fencing.mp4`](p6-fencing.mp4) | 回收和 fencing token | [03](../tutorial/03-expiry-and-fencing.md) |
| [`p7-cache.mp4`](p7-cache.mp4) | 成功以后：LRU 结果缓存 | [06](../tutorial/06-lru-cache.md) |
| [`p8-failures.mp4`](p8-failures.mp4) | 重试、退避和尝试历史 | [07](../tutorial/07-retries-timeouts.md) |
| [`p9-ops.mp4`](p9-ops.mp4) | 杀掉一个 worker，看它怎么恢复 | [08](../tutorial/08-observability.md) |

画面由 `video/record.mjs` 在真实页面上录制（会话里的数据都是这次真实请求产生的），红框标出每一步该看的地方；旁白来自 `video/narration.json`。旁白是本地 TTS 合成的（CosyVoice 2 自带的示例音色，不调任何云端 API），成片用本地 whisper 逐段回验过念的是稿子上的话。

## 日常问题（`problems/`）

每个问题一段代码讲解：先看修复前的写法和现象，再看修复后的写法，讲到哪几行就用红框框出哪几行。内容来自 [`video/problems.walk.json`](../../video/problems.walk.json)（由 `scripts/build_problem_videos.mjs` 按锚点从源码生成），问题的来龙去脉见 [`docs/problems.md`](../problems.md)。

- [`q01-notify-deadlock.mp4`](problems/q01-notify-deadlock.mp4) — 预热线程卡死，日志里一个错都没有
- [`q02-defer-stampede.mp4`](problems/q02-defer-stampede.mp4) — 被限速的任务一起回来，又一起被拒
- [`q03-infinity-json.mp4`](problems/q03-infinity-json.mp4) — 没配限速的集成，让 worker 一直认领不到任务
- [`q04-zombie-lease.mp4`](problems/q04-zombie-lease.mp4) — worker 被杀以后，任务永远停在 RUNNING
- [`q05-stale-overwrite.mp4`](problems/q05-stale-overwrite.mp4) — 被判死的 worker 醒过来，覆盖了新结果
- [`q06-duplicate-downstream.mp4`](problems/q06-duplicate-downstream.mp4) — 外部系统收到了两次写入
- [`q07-runaway-job.mp4`](problems/q07-runaway-job.mp4) — 一个不守规矩的任务，让超时形同虚设
- [`q08-misfire-flood.mp4`](problems/q08-misfire-flood.mp4) — 调度器停了四十多分钟，恢复后一口气补了四十三个任务
- [`q09-backoff-invisible.mp4`](problems/q09-backoff-invisible.mp4) — 重试测试里，任务一直停在 PENDING
