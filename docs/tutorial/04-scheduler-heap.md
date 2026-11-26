# 第 4 章：延迟任务和周期任务：一个带索引的最小堆

> 讲解视频：[p2-heap](../videos/p2-heap.mp4)

## 这一章解决什么问题

到目前为止，任务一提交就是 PENDING，马上能跑。但很多任务要「晚点再跑」：

- 延迟任务：三十秒后发提醒，明早九点出报表；
- 周期任务：每五分钟同步一次，每个工作日早上九点跑一次；
- 还有后面两章要用的：重试的退避（第 7 章），拿不到限速令牌的推迟（第 5 章）。

这一章加一个 scheduler 进程。它只做一件事：**到点的时候，把 SCHEDULED 改成 PENDING**。剩下的照旧交给 worker。

## 为什么不让 worker 直接查 run_at？

假设：worker 认领的时候加一个 `run_at <= now()` 就行了，不需要调度器。

对延迟任务来说，这确实能跑。但它有两个问题：

1. 周期任务没有「行」可查。下一次运行的那个任务还不存在，得有人按时把它造出来；
2. 每个 worker 都在轮询所有未来的任务，大部分查询都是白查。

所以拆开：worker 只看 PENDING；scheduler 手里有一个按时间排的堆，只关心堆顶什么时候到期。

## 堆里放什么？为什么要「带索引」？

堆里的元素是 `("job", id)` 或者 `("schedule", id)`，优先级是到期的时间戳。

Python 自带的 `heapq` 只能 push 和 pop。可是调度器还需要：

- 一个延迟任务被取消了，要把它**删掉**；
- 一个周期任务的下一次时间变了，要**改优先级**。

用 `heapq` 只能留一个墓碑，等它浮到堆顶再扔。所以 `leasejobs/heap.py` 自己写了一个 indexed min-heap：数组之外再存一个 `key → 下标` 的字典，删除和改优先级都是 O(log n)。
同一个时间到期的两个元素，按插入顺序出来（每个元素带一个自增序号）。

怎么证明它对？`tests/test_heap.py` 用 hypothesis 随机生成三百组 push、remove、pop 的序列，每一步都和「字典加排序」的朴素模型比一次。

## 调度器什么时候醒？

不轮询。它睡到「堆顶到期」和「一秒」两者中较早的那个，或者被 NOTIFY 叫醒。

NOTIFY 是谁发的？`sql/004_scheduler.sql` 里的触发器：任何一行 jobs 变成 SCHEDULED，提交的时候自动 `pg_notify('leasejobs_scheduler', 'job:<id>:<到期时间>')`。
API、重试、限速推迟，谁把任务放进 SCHEDULED 都一样，不用每个地方记得去叫调度器。

万一 NOTIFY 丢了呢（比如调度器正好在重连）？每十五秒，调度器从库里整个重建一次堆。库是真相，堆只是索引。

真跑一次：插三个延迟任务（四十秒、八秒、二十秒），调度器收到通知，写下一条 `heap_push` 事件：

```json
{"job_id": 4, "kind": "heap_push", "actor": "scheduler-1",
 "detail": {"run_at": "...02:46:26", "heap_size": 3,
            "heap_top": [{"key": "job:3", "due": "...02:46:14"},
                         {"key": "job:4", "due": "...02:46:26"},
                         {"key": "job:2", "due": "...02:46:46"}]}}
```

八秒那个排在最前面，虽然它是第二个插进来的。

到期以后，`release` 做一次 SCHEDULED → PENDING，条件是 `run_at <= 当前时间`。端到端测试（真的调度器进程加真的 worker 进程）的输出：

```
released 1 ms after run_at
```

这个数字只说明这次在本机上很快。测试里断言的是「没有早于 run_at 释放」，不断言具体毫秒。

## 调度器挂了一段时间，错过的运行怎么办？

这叫 misfire。每个 schedule 自己选策略（`leasejobs/schedules.py` 的 `due_slots`）：

- `skip`（默认）：错过多少次都只补**一次**，补的是最近的那个时间点，然后跳到下一个未来的时间点。适合「把状态同步到最新」的任务，比如报表、同步。连跑四十次旧的没意义。
- `catch_up`：每个错过的时间点都补，但最多补 `max_catchup` 个，保留最近的。适合「每个时间窗都要算」的任务，比如每小时的汇总。上限是为了防止停了一周以后把队列冲爆。

测试里，一个每分钟的 schedule 错过了四十二分半：skip 补一个；catch_up 上限五，补最近的五个，间隔正好一分钟。

## 补跑会不会补出两份？

调度器可能在「插入任务」和「更新 next_run_at」之间崩溃。重启后，它会把同一个时间点再触发一次。
所以 jobs 表上有唯一索引 `(schedule_id, scheduled_for)`，插入用 `ON CONFLICT DO NOTHING`。`test_refiring_the_same_slot_creates_no_second_job` 就是模拟这个崩溃。

## 起两个调度器会怎样？

只有一个干活。调度器启动时抢一个 Postgres advisory lock（`pg_try_advisory_lock`），抢不到就当备胎，每两秒再试。
锁跟着数据库会话走：主调度器的连接断了，锁自动释放，备胎接手。这比自己写选主（leader election）简单得多。

## cron 表达式

`leasejobs/cron.py` 是一个五个字段的小解析器：分、时、日、月、星期，支持 `*`、范围、列表和步长。
有一条老规矩要注意：日和星期**都**限定的时候，满足其一就算。`0 12 13 * 5` 是「每月十三号，或者每个星期五」，不是「十三号恰好是星期五」。测试里有这一条。

## 怎么 checkout 到这一步跑起来

```bash
git checkout step-04
.venv/bin/python -m leasejobs.migrate
.venv/bin/python -m pytest tests/test_heap.py tests/test_cron.py tests/test_misfire.py tests/test_scheduler.py -s
.venv/bin/python -m leasejobs.scheduler --id scheduler-1 &
curl -s -XPOST localhost:8910/jobs -H 'X-API-Key: dev-key-alice' -H 'content-type: application/json' \
     -d '{"type":"noop","delay_s":5}'
```
