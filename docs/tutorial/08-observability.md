# 第 8 章：看得见才敢上线：健康检查、指标、卡住检测、重启恢复

> 讲解视频：[p9-ops](../videos/p9-ops.mp4)

## 这一章解决什么问题

前七章的保护都是「自动」的：租约过期自动回收、旧写入自动被拒、失败自动重试。
自动的东西坏了，往往也是悄悄地坏。这一章让它们看得见：

1. 每个进程分清 liveness（活着）和 readiness（能干活）；
2. Prometheus 指标：任务年龄、积压、尝试次数、失败率；
3. **卡住检测**（stalled-job detection）：告诉你「哪个部件」坏了，而不只是「有任务卡住了」；
4. 一个 supervisor，worker 死了就拉起来，用一个新的身份。

## healthz 和 readyz 有什么不一样？

- `/healthz`：进程还能回答。回答不了，就该重启它；
- `/readyz`：数据库连得上，而且主循环最近十秒内转过（或者正在跑任务）。不 ready，就别给它派活，但重启不一定有用。

数据库挂了的时候，所有 worker 都会 not ready，但它们都活着。这时候重启它们只会让情况更乱。分开两个端点，就是为了让编排系统做对的事。

## 指标放在谁身上？

两类，放在不同的地方（`leasejobs/metrics.py`）：

- **进程计数器**，在 worker 和调度器自己身上：`leasejobs_attempts_total{type,outcome}`、尝试耗时直方图、回收次数、旧写入被拒次数、限速推迟次数。失败率用 PromQL 算：非 succeeded 的速率除以全部的速率。
- **队列状态**，由 API 在每次被抓取时查一次 Postgres：按状态的积压 `leasejobs_jobs{state}`、最老的可运行任务等了多久 `leasejobs_oldest_runnable_age_seconds`、活着的 worker 数、卡住的任务数。

为什么队列状态不放在 worker 上？三个 worker 各报一遍同一个积压，加起来就是三倍。这种「全局事实」只该有一个出处。

## 「卡住了」分几种？

一条 SQL（`STALLED_SQL`）查三种，每种指向一个不同的坏掉的部件：

| reason | 意思 | 说明谁坏了 |
|---|---|---|
| `lease_expired_unreclaimed` | 租约过期超过两个 TTL，还没人回收 | 没有活着的 worker 在循环 |
| `running_over_timeout` | 还在心跳，但已经超过 timeout_s | worker 没执行自己的截止时间 |
| `scheduled_overdue` | 到期三十秒了还是 SCHEDULED | 没有调度器 leader |

`GET /stalled` 返回具体的任务，`leasejobs_stalled_jobs{reason}` 给告警用。告警规则在 `deploy/prometheus/alerts.yml`，处理步骤在 `docs/troubleshooting.md`。

`tests/test_observability.py` 分别造出这三种情况，断言 reason 和任务 id 一一对上。

## 杀掉一个 worker，真跑一次

demo 起了三个 worker。提交一个要跑六秒的任务，从 API 杀掉持有它的那个 worker：

```
$ curl -s -XPOST localhost:8910/workers/worker-3/kill -H "$H"
{"worker":"worker-3","pid":16123,"held_job":5,"killing":16123}
```

API 不会按名字去杀进程。它找到这个 worker 注册的管理端口，请它对**自己的 PID** 发 SIGKILL。

这个任务的事件（心跳省略）：

```
03:09:35.817 claim         PENDING -> LEASED   worker-3     fencing_token 1
03:09:35.821 rate_token                        worker-3     tokens_left 4.0
03:09:35.824 start         LEASED  -> RUNNING  worker-3
03:09:37.265 worker_killed                     api:alice    pid 16123
03:09:41.270 lease_expired RUNNING -> PENDING  worker-3.g2  expired_owner worker-3, overdue_ms 126
03:09:41.275 claim         PENDING -> LEASED   worker-3.g2  fencing_token 2
03:09:47.317 finish        RUNNING -> SUCCEEDED worker-3.g2
```

有意思的是回收它的人：supervisor 两秒后拉起了 `worker-3.g2`，恰好是它先跑到了回收那一步。
supervisor 并没有把旧任务「交接」给新进程。新进程只是一个普通的 worker，和别人一样去抢过期的租约。
恢复不依赖死掉的那个进程，也不依赖 supervisor。这是故意的。

Prometheus（9211）在 colima 里通过 `host.docker.internal` 抓宿主机上的六个目标，全部 `up`。

## 怎么 checkout 到这一步跑起来

```bash
git checkout step-08
./scripts/demo.sh
curl -s localhost:8910/metrics | grep -E '^leasejobs_(stalled|oldest|workers_alive)'
open http://localhost:9211/alerts
./scripts/stop.sh
```
