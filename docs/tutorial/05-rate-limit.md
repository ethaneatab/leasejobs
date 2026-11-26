# 第 5 章：按集成限速：Postgres 里的共享令牌桶，拿不到就交回调度器

> 讲解视频：[p4-ratelimit](../videos/p4-ratelimit.mp4)

## 这一章解决什么问题

很多任务要调外部系统，比如账务接口、CRM。对方会限速：每秒两次，超了就 429，或者干脆封你。
限速是**对方**的规则，所以它属于「集成」（integration），不属于某个 worker。

这一章要回答两个问题：

1. 桶放在哪里，才能让十个 worker 加起来也不超过每秒两次？
2. 拿不到令牌的时候，worker 干什么？

## 桶放在哪里？

假设：每个 worker 进程里放一个令牌桶，每秒两个。

那三个 worker 加起来就是每秒六个。扩容到十个就是二十个。配置写的是二，实际是二乘 N，而且每次扩缩容都在变。

所以桶放在 Postgres 的 `integrations` 表里，一个集成一行：容量 `capacity`、每秒补充 `refill_per_s`、当前令牌 `tokens`、上次更新时间 `updated_at`。

## 不用后台线程，令牌怎么补？

懒补充（lazy refill）。没有任何东西在后台一秒一秒地加令牌。每次有人来拿，在**同一条语句**里先算「距离上次过了多久，应该补多少」，封顶到容量，再扣一个：

```sql
WITH b AS (
    SELECT name, capacity, refill_per_s,
           LEAST(capacity, tokens + extract(epoch FROM now() - updated_at) * refill_per_s) AS avail
    FROM integrations WHERE name = %(name)s FOR UPDATE)
UPDATE integrations i
   SET tokens = CASE WHEN b.avail >= %(n)s THEN b.avail - %(n)s ELSE b.avail END, updated_at = now()
  FROM b WHERE i.name = b.name
RETURNING b.avail >= %(n)s AS ok, i.tokens, i.capacity, i.refill_per_s
```

`FOR UPDATE` 把这一行锁住几毫秒，所有 worker 在这一行上排队。代价是每次调用前多一次数据库往返。被限速的调用本来就慢，这点代价可以接受。

## 拿不到令牌，worker 干什么？

假设：等一下，等到有令牌再调。

那这个 worker 线程就被一个限速的集成占住了。如果队列里还有别的任务（不需要这个集成的），它们也跟着等。

所以不等。`LeaseManager.defer` 把任务从 LEASED 改回 SCHEDULED，`run_at` 设成「大约什么时候会有令牌」，放掉租约。
任务回到调度器的堆里（第 4 章的触发器会自动 NOTIFY），worker 马上去认领下一个任务。
推迟不算尝试次数：`attempts` 只在真正开始执行时加一，`deferrals` 另记。

`tests/test_ratelimit.py::test_no_token_defers_instead_of_blocking` 证明两件事：被拒的任务回到了 SCHEDULED、attempts 还是零；同一个 worker 紧接着就能认领下一个任务。

## 怎么证明全局速率真的守住了？

`test_shared_bucket_holds_the_global_rate_across_three_workers`：三个 worker 进程、一个调度器进程，十六个调同一个集成的任务，桶容量三、每秒补二。
假的外部集成记下每一次被接受的调用时间。判据是：从第一次调用开始，任何时刻累计调用数都不超过 `容量 + 速率 × 已过时间`，再给一次的时钟误差。

这个判据来自配置，不是本机调出来的数字。打开和关掉限速（`LEASEJOBS_LAB_SABOTAGE=no_ratelimit`）各跑一次：

```
限速开：16 calls over 6.85s; worst excess over the bucket bound 0.03; 20 deferrals
限速关：16 calls over 0.14s; worst excess over the bucket bound 12.72; 0 deferrals
E       AssertionError: more calls than the bucket allows
```

## 一个真踩到的坑：被拒的任务一起回来，又一起被拒

第一版推迟时间是「下一个令牌还要多久」，再加百分之二十以内的抖动（jitter）。测出来十六个任务产生了九十一次推迟。

为什么？十几个任务同一时刻被拒，拿到的提示都是「零点五秒后有一个令牌」。零点五秒后它们一起回来，只有一个拿到，其余的又被推迟。如此反复。

改法：推迟时间在「前面已经排着的任务数加一，除以补充速率」这个范围里均匀撒开。也就是按桶的真实服务能力把它们错开。三次实测（`docs/results/ratelimit-deferrals.txt`）：

```
推迟次数中位数   91  ->  22
总耗时中位数     6.54s -> 6.96s     （理论下限 6.5s）
```

推迟少了四分之三，总时间慢了零点四秒。撒开以后，有些任务回来得比令牌晚一点，这是换来的代价。

## 怎么 checkout 到这一步跑起来

```bash
git checkout step-05
.venv/bin/python -m leasejobs.migrate          # 005_rate_limits.sql，带 ledger-api 和 slow-crm 两个桶
.venv/bin/python -m pytest -s tests/test_ratelimit.py
```
