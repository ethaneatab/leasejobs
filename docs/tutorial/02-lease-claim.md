# 第 2 章：认领就是拿租约：一条 UPDATE 加 SKIP LOCKED

> 讲解视频：[p3-claim](../videos/p3-claim.mp4)

## 这一章解决什么问题

第 1 章的 worker 先 SELECT 候选，再逐个 compare-and-set。它是对的，但是多个 worker 会挤在同一行上：谁都先看到队头那一行，只有一个人能赢，其他人白跑一次 UPDATE。

更重要的是，第 1 章的 LEASED 只记了「谁拿着」，没记「拿到什么时候」。worker 一旦死掉，任务就永远是它的。

这一章做两件事：

1. 认领改成**一条语句**：`UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`；
2. 认领的同时写下**租约**（lease）：`lease_owner` 加上 `lease_expires_at`。

租约过期以后怎么办，是第 3 章的事。这一章先把租约「立起来」。

## 为什么是一条语句？

假设：先 SELECT 再 UPDATE，中间只要加个状态判断就安全。

安全，但是浪费。真正的问题是第二个 worker 看到的还是同一行。
`FOR UPDATE SKIP LOCKED` 的意思是：这一行已经被别的事务锁住了，就当它不存在，直接看下一行。
所以四个 worker 同时进来，会各自锁到四个不同的行。

代码在 `leasejobs/lease.py` 的 `claim`：

```sql
UPDATE jobs SET state = 'LEASED', lease_owner = %(me)s, leased_at = now(),
       lease_expires_at = now() + make_interval(secs => %(ttl)s)
WHERE id = (SELECT id FROM jobs
            WHERE state = 'PENDING' AND run_at <= now()
            ORDER BY priority DESC, run_at, id
            LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING *
```

同一个事务里再写一条 `claim` 事件，带上 `lease_owner` 和 `lease_expires_at`。

## 时间用谁的钟？

用数据库的钟。`now()` 是 Postgres 的时间，不是 worker 的时间。
几台机器的时钟总会差一点。如果每个 worker 用自己的钟判断「租约过期没有」，差几百毫秒就会有两个人同时认为自己持有租约。
统一用一个钟，这个问题就没有了。代价是每次判断都要问数据库，而我们本来就要问。

## 怎么证明每个任务只跑一次？

新加一个任务类型 `tally`：执行一次，就往 `tally` 表插一行。这是一个能数的副作用（side effect）。

`tests/test_claim_concurrency.py::test_four_processes_run_each_job_exactly_once` 起四个真的 worker 进程，排四百个 tally 任务，等它们空闲一秒后自己退出，然后数：

- `tally` 里每个 job_id 正好一行；
- 四百个任务全是 SUCCEEDED，attempts 全是 1；
- 至少两个 worker 分到了活。

真跑的输出：

```
400 jobs / 4 worker processes in 3.27s (incl. startup); per worker: cw-0=100, cw-1=100, cw-2=100, cw-3=100
```

另一个测试直接证明「跳过」这件事：一个事务先锁住队头那一行不放，另一个连接设 `statement_timeout = 2s` 去认领。
如果它在等锁，两秒后就会报错；实际上它立刻拿到了第二行。

## SKIP LOCKED 快多少？

实测：没有快。把它换成普通的 `FOR UPDATE`（开关 `LEASEJOBS_LAB_SABOTAGE=no_skip_locked`），同一个测试跑三次：

```
SKIP LOCKED   3.01s  2.77s  3.28s
FOR UPDATE    3.12s  3.01s  3.16s
```

差别在噪音里。原因是这里每次认领的事务只有一条 UPDATE，锁只持有一两毫秒，排队的人等得很短。
所以在这个仓库里，SKIP LOCKED 的价值是「不排队」这个性质，不是一个能写进 README 的提速数字。
它会在认领事务变长的时候拉开差距（比如认领时还要读别的表）。这里没测，就不写数字。

## 怎么 checkout 到这一步跑起来

```bash
git checkout step-02
.venv/bin/python -m leasejobs.migrate      # 应用 002_leases.sql
.venv/bin/python -m pytest -s tests/test_claim_concurrency.py
```
