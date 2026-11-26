# 第 7 章：失败也要有规矩：重试上限、超时、取消、依赖挂掉

> 讲解视频：[p8-failures](../videos/p8-failures.mp4)

## 这一章解决什么问题

到第 6 章为止，任何异常都直接 FAILED_TERMINAL。这太粗了：

- 对方接口抖了一下，返回 503，下一秒就好了。应该重试；
- 对方说「这个账号不存在」，返回 400。重试一百次也没用；
- 任务卡住不返回。总得有个时间上限；
- 用户点了取消，任务还在跑。得让它停下来。

这一章给每种失败定一条规矩，全部落在状态图上已有的边里。

## 什么样的错误值得重试？

`leasejobs/jobs.py` 里两种错误：`RetryableError`（可能会好）和 `TerminalError`（不会好）。
`call_integration` 这样分：连不上、5xx 是 Retryable；4xx 是 Terminal；409 是 `FencedOut`，说明别人已经接手了，这次尝试直接放弃，不算失败。
没见过的异常默认可重试。payload 校验失败（ValueError）不重试。

决定写在 `Worker.on_failure`：

- 可重试，而且 `attempts < max_attempts`：RUNNING → FAILED_RETRYABLE → SCHEDULED；
- 可重试但次数用完：超时的进 TIMED_OUT，其它进 FAILED_TERMINAL；
- 不可重试：FAILED_TERMINAL。

## 重试为什么要经过调度器？

假设：失败了，worker 原地 sleep 一下再试。

那就又回到了第 5 章的问题：worker 被一个等待中的任务占住。而且 worker 一旦在 sleep 时被杀，这次重试就丢了。
所以重试也是「改成 SCHEDULED，设一个 run_at」，交给调度器的堆。worker 立刻去干别的。

退避（backoff）是指数增长加「一半固定一半随机」的抖动：第 n 次失败后等 `min(上限, 基数 × 2^(n-1))` 的一半，再加上零到另一半之间的随机数。`test_backoff_grows_and_is_capped` 检查每一档的范围。

两步状态变更放在**一个事务**里。否则 worker 恰好在两步之间崩溃，任务会永远停在 FAILED_RETRYABLE，没有人会再碰它。

一次真跑（外部集成被设成前两次返回 503）：

```
claimed                         job_id=1 fencing_token=1
attempt failed, retry scheduled attempt=1 backoff_s=0.757 error="RetryableError: integration 503: ..."
claimed                         job_id=1 fencing_token=2
attempt failed, retry scheduled attempt=2 backoff_s=1.384 error="RetryableError: integration 503: ..."
```

注意每次重试 fencing token 都加一。重试也是一次新的认领。

## 超时靠谁来执行？

假设：job 自己定期调 `ctx.check()`，超时了就抛异常。

守规矩的 job 确实会这样做，`ctx.sleep` 也会顺手检查。但只要有一个 job 在某个阻塞调用里不出来，这个约定就破了。

所以 worker 不依赖 job 的自觉。`Worker.run_with_deadline` 把 job 放在单独的线程里跑，主线程最多等「截止时间加零点五秒」。
到点了线程还活着，就放弃它：标记 lost，按超时处理，继续干别的。
Python 的线程杀不掉，被放弃的线程可能还会醒来写东西。但这次尝试已经结束，下一次认领 token 会更大，它的写入会像第 3 章的旧 worker 一样被拒。

`test_a_job_that_ignores_its_deadline_is_abandoned` 用一个故意不检查的 `block` 任务（阻塞三秒，超时零点三秒），断言 worker 不到三秒就放弃了它，状态是 TIMED_OUT。

## 取消怎么传到正在跑的任务里？

还没开始的（SCHEDULED、PENDING）：API 直接改成 CANCELLED。
已经被 worker 拿着的：API 只把 `cancel_requested` 设成 true，返回 202。
worker 的心跳每次续租都会读回这个字段（第 3 章），看到 true 就设 `ctx.cancelled`，job 下次 check 时停下，worker 写 RUNNING → CANCELLED。
`test_cancel_reaches_a_running_job_through_the_heartbeat` 用真的 worker 进程测这条路径。

## 依赖挂掉的三种情况

`tests/test_failures.py` 里各有一个：

- 503 两次：前两次 failed，第三次 succeeded；
- 400 一次：一次就 FAILED_TERMINAL，不重试；
- 端口上根本没人：每次都是 `integration unreachable`，两次用完以后 FAILED_TERMINAL。

## 怎么 checkout 到这一步跑起来

```bash
git checkout step-07
.venv/bin/python -m pytest tests/test_failures.py
```
