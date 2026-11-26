# 教程：从零做一个租约协调的任务服务

先做出最小能跑的版本，再一步一步加功能。每一章对应一个 git tag，`git checkout step-NN` 就能回到那一步，那一步的测试是绿的。

| 章 | tag | 解决什么问题 | 视频 |
|---|---|---|---|
| [1. 最小能跑的任务服务，和一张写死的状态图](01-state-machine.md) | step-01 | 状态被乱改 | p1-submit |
| [2. 认领就是拿租约](02-lease-claim.md) | step-02 | 多个 worker 抢同一行；任务归谁、归到什么时候 | p3-claim |
| [3. 租约会过期：心跳、回收、fencing token](03-expiry-and-fencing.md) | step-03 | worker 死了或卡住以后 | p5-heartbeat, p6-fencing |
| [4. 延迟和周期任务：带索引的最小堆](04-scheduler-heap.md) | step-04 | 「晚点再跑」和错过的运行 | p2-heap |
| [5. 按集成限速：共享令牌桶](05-rate-limit.md) | step-05 | 外部系统的速率限制 | p4-ratelimit |
| [6. 结果缓存：O(1) LRU](06-lru-cache.md) | step-06 | 反复查结果打到数据库 | p7-cache |
| [7. 重试上限、超时、取消、依赖挂掉](07-retries-timeouts.md) | step-07 | 失败要有规矩 | p8-failures |
| [8. 健康检查、指标、卡住检测、重启恢复](08-observability.md) | step-08 | 自动的东西坏了要看得见 | p9-ops |
| [9. 把一个任务的一生摆在页面上](09-learn-page.md) | step-09 | 机制都在行里，看不见 | p0-overview |

章节顺序是「先能跑，再加功能」，所以和 learn 页面的阶段顺序不完全一样（页面按事情发生的顺序排）。对应关系在 `video/parts.json`。
