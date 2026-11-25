# 第 3 章：租约会过期：心跳、回收、fencing token

> 讲解视频：[p5-heartbeat](../videos/p5-heartbeat.mp4)，[p6-fencing](../videos/p6-fencing.mp4)

## 这一章解决什么问题

第 2 章的租约有过期时间，但没人看它。worker 被 `kill -9` 以后，它手上的任务永远停在 RUNNING。

修这个问题有三块，缺一块都不行：

1. **心跳**（heartbeat）：活着的 worker 每秒把 `lease_expires_at` 往后推；
2. **回收**（reclaim）：任何 worker 看到过期的租约，就把任务放回 PENDING，让大家重新抢；
3. **fencing token**：每次认领，token 加一。之后所有写入都带着自己的 token，旧 token 一律拒绝。

第三块最容易被省掉，也最要命。下面一节一节说。

## 心跳谁来发？

假设：job 自己在代码里隔一会儿续一次租。

不行。job 在算东西、在等网络的时候，就没人续租了。
所以 worker 起一个单独的 heartbeat 线程，用**自己的数据库连接**，每 `LEASEJOBS_HEARTBEAT_S` 秒做一次：

```sql
UPDATE jobs SET lease_expires_at = now() + ttl, heartbeat_at = now()
WHERE id = $1 AND state IN ('LEASED','RUNNING')
  AND lease_owner = $me AND fencing_token = $my_token
RETURNING lease_expires_at, cancel_requested, fencing_token
```

改到 0 行，说明租约已经不是我的了。线程把 `ctx.lease_lost` 置上，job 下一次 `ctx.check()` 就会停下。
顺带：`cancel_requested` 也从这里读回来，取消请求就是这样传到正在跑的任务里的（第 7 章）。

## 谁来回收？

没有单独的「回收服务」。每个 worker 在认领之前先跑一次 `reap_expired`：

```sql
SELECT ... FROM jobs
WHERE state IN ('LEASED','RUNNING') AND lease_expires_at < now()
ORDER BY lease_expires_at LIMIT 10 FOR UPDATE SKIP LOCKED
```

拿到的每一行，用 `store.transition` 改回 PENDING，条件里带着它看到的 token。
这样两个 worker 同时回收同一行，也只有一个成功。
最后一次尝试时过期的，不再重试，直接 FAILED_TERMINAL。否则一个总把 worker 搞死的任务会无限循环。

只要还有一个 worker 活着，过期的任务就会被捡起来。所有 worker 都死了呢？那时候没人能跑任务，回收也就没有意义。指标里有 `stalled` 告警（第 8 章）。

## 回收了，为什么还要 fencing？

假设：worker 只要被回收，就是死了。

错。被判定「死了」的 worker，可能只是卡住了：一次很长的 GC，一次虚拟机暂停，一次网络分区。
它醒过来的时候，并不知道自己的租约已经没了。它会接着把结果写出去。
这时候，任务已经被另一个 worker 重新跑过，甚至已经写过一次结果了。

fencing token 就是给这种情况准备的。每次认领 `fencing_token = fencing_token + 1`。
worker 的每一次写入都带着自己的 token：

- 写我们自己的库：`lease.py` 的 `fence()` 生成 `lease_owner = $me AND fencing_token = $tok`，心跳、开始、完成全都带上；
- 写外部系统：`call_integration` 把 token 放进 `X-Fencing-Token` 头。假的外部集成（`leasejobs/integration.py`）记住每个 key 见过的最大 token，比它小的直接 409。

第二点很关键。如果只在自己的库里挡，旧 worker 发给外部系统的那一次写入还是会落地。

## 真跑一次：SIGSTOP 一个 worker

`tests/test_recovery.py::test_sigstop_worker_revives_and_is_fenced_out` 做的事：

1. w1 认领任务，开始干活；
2. 测试用 w1 的 PID 给它发 `SIGSTOP`。进程整个冻住，心跳线程也冻住；
3. 起 w2。租约一点五秒后过期，w2 回收、认领（token 2），干完活，写外部集成，写结果；
4. 测试给 w1 发 `SIGCONT`。w1 醒来，继续往外写。

两个 worker 的日志（时间戳只留秒数的后两位）：

```
w1  33.985 claimed                    fencing_token=1
          （SIGSTOP）
w2  35.538 reclaimed expired lease    job_id=1 to=PENDING
w2  35.547 claimed                    fencing_token=2
w2  36.633 succeeded                  fencing_token=2
          （SIGCONT）
w1  36.684 lease lost                 job 1: lease with token 1 is gone
w1  36.708 fenced out by integration  integration refused token 1: {"error": "stale fencing token", "got": 1, "max_seen": 2}
```

外部集成那边，`acct-7` 只接受了 w2 的一次写入，拒绝了 token 1。

## 能失败的对照

两个开关，分别把回收和 fencing 关掉。`scripts/failing_controls.sh` 每个开关跑两次：关掉时必须红，打开时必须绿。实测输出（`docs/results/failing-controls.txt`）：

```
=== lease reclaim (kill -9 recovery)  LEASEJOBS_LAB_SABOTAGE=no_reclaim  expect=red got=red  [OK]
E   AssertionError: timed out after 15s waiting for another worker to reclaim and finish the job;
    saw {'state': 'RUNNING', 'lease_owner': 'w1', 'fencing_token': 1, 'expired_s_ago': Decimal('13.6')}

=== fencing token (in-process stale write)  LEASEJOBS_LAB_SABOTAGE=no_fencing  expect=red got=red  [OK]
E   AssertionError: stale worker overwrote the result: {'by': 'w-a'}

=== fencing token (SIGSTOP + revive, downstream)  LEASEJOBS_LAB_SABOTAGE=no_fencing  expect=red got=red  [OK]
E   assert ['w2', 'w1'] == ['w2']        # 外部集成收了两次写入
```

注意第一条：关掉回收以后，任务不是卡在 LEASED，而是卡在 RUNNING，因为 w1 死之前已经开始跑了。租约早过期十几秒了，却没有人动它。

## fencing 不能保证什么？

它保证的是「**最后落地的结果属于最新的租约**」，不是「副作用只发生一次」。
如果旧 worker 醒得早，赶在新 worker 写外部系统之前写了，外部系统会先收下旧 token 的那一次，再收下新 token 的那一次。
最终值是对的，但对方收到了两次。真要「只发生一次」，需要对方按 job id 做幂等（idempotency），这不是 fencing 能替你做的。

## 怎么 checkout 到这一步跑起来

```bash
git checkout step-03
.venv/bin/python -m leasejobs.migrate          # 003_fencing.sql
.venv/bin/python -m pytest -s tests/test_recovery.py
./scripts/failing_controls.sh                  # 三组对照，各红一次绿一次
```
