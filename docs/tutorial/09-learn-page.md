# 第 9 章：把一个任务的一生摆在页面上

> 讲解视频：[p0-overview](../videos/p0-overview.mp4)

## 这一章解决什么问题

前八章的机制都在数据库的行里：租约、token、事件、尝试历史。它们是真的，但是看不见。
这一章做一个前端（React + Vite + TypeScript），有两部分：

1. 控制台 `/`：提交任务、看列表和尝试历史、看租约时间线、杀掉一个 worker、看调度堆和限速桶；
2. 教学模式 `/learn`：一个按钮「提交样例」，十一个阶段依次亮起，每一步旁边是那一刻的真实数据。

## 阶段怎么做到「真」的亮起？

假设：前端知道整个流程大约十二秒，按时间表一格一格点亮就行。

那就是动画，不是系统。只要后端慢一点或者出了错，页面照样会「成功」。

所以亮不亮由后端决定。`leasejobs/learn.py` 的 `build_stages` 每次被调用都去读这个任务的 `job_events`、`job_attempts`、限速桶和缓存，某一步的数据存在了，这一步才是 done。前端每五百毫秒轮询一次 `GET /learn/runs/{id}`，只负责画。

## 样例任务为什么一定会走到「旧 worker 被拒」？

样例是一个普通的 `call_integration` 任务，只多了一个故障注入点（failpoint）：

```json
"_failpoint": {"at": "before_commit", "attempt": 1, "freeze_s": 6.0}
```

第一次尝试做完工作、写结果之前，worker 调 `freeze_self(6)`（`@learn:freeze`）：起一个分离的 shell，六秒后给自己的 PID 发 SIGCONT，然后给自己发 SIGSTOP。整个进程冻住，心跳线程也冻住。

时间是这样算的：延迟两秒，工作三秒，租约四秒。冻住以后，大约第九秒租约过期，另一个 worker 回收，token 变成 2，再工作三秒。第一个 worker 在第十一秒左右醒来，这时第二个还在干活，所以它的写入一定撞上 token 2。

一次真跑（时间只留到毫秒）：

```
submit    14:52.875   (new) → SCHEDULED
heap      14:52.879   heap_size = 1
claim     14:55.053   lease_owner = worker-3 · fencing_token = 1
token     14:55.057   tokens_left = 4.0 / 5.0
heartbeat 14:56.467   第 3 次续租
stall     14:58.102   SIGSTOP pid 18354 · 6 秒
expire    15:01.516   RUNNING → PENDING · 过期 36 ms 后被 worker-2 回收
reclaim   15:01.520   fencing_token 1 → 2 · worker-2
stale     15:04.125   finish: my_token 1 ≠ current_token 2 · 拒绝
done      15:04.568   RUNNING → SUCCEEDED · by worker-2
cache                 X-Cache: HIT
```

最后一步要用户点「查询结果」。它亮起的条件是后端的 LRU 记到了这个任务的一次命中。因为成功通知已经预热过缓存，第一次查就是 HIT。

## 「看源码」链接怎么不过期？

源码里关键的地方包着两行注释：`# @learn:claim` 和 `# @learn:end claim`。`scripts/learn_anchors.mjs` 扫出每个 id 的文件和行号，写进 `frontend/src/generated/anchors.json`。链接是：

```
https://github.com/ethaneatab/leasejobs/blob/<构建时的 git sha>/leasejobs/lease.py#L<start>-L<end>
```

仓库地址只在 `frontend/vite.config.ts` 里写一次（`VITE_REPO_URL`），sha 在构建时取 `git rev-parse HEAD`，不写死 main，所以代码改了，旧链接指向的还是旧代码的那几行。

`scripts/check_anchors.mjs` 检查三件事：前端和 `learn.py` 引用的 id 都存在；行号在文件范围内，而且标记正好在范围外面一行；生成文件没有过期。

## e2e 怎么证明页面没骗人？

`e2e/learn.spec.ts` 用 Playwright（headless chromium）真点一遍：

- 点「提交样例」，然后每一百毫秒看一次页面，记下每个阶段**第一次**变成 done 的顺序，必须和页面顺序一致；
- 关键值真的出现：`fencing_token = 1`、`fencing_token 1 → 2`、`my_token 1 ≠ current_token 2`；
- 展开「租约过期」那一步，JSON 里有 `expired_owner`；
- 点「查询结果」，看到 `X-Cache: HIT`，最后一步亮起；
- 十一个「看源码」链接：格式对、sha 是四十位、文件里那几行真的存在，标记就在边上；
- 控制台：提交一个前一次会失败的 flaky 任务，尝试历史出现两行；点「杀掉」，那个 worker 变灰，supervisor 拉起的下一代变绿。

## 怎么 checkout 到这一步跑起来

```bash
git checkout step-09
./scripts/demo.sh                 # 最后一步起 Vite，http://localhost:5191/learn
npm ci && npx playwright test -c e2e
./scripts/stop.sh
```
