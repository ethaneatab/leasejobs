# 面试题

每题的结构：**一句话答案** → **展开** → **追问**（带答法）。答案都落在这个仓库的代码和实测数据上。
代码位置写成「文件 · `@learn` 锚点」，learn 页面的「看源码」会跳到对应的行。
实测数字来自 `docs/results/`，测量环境是一台笔记本、Postgres 跑在 colima（4 核共享）里，**不是**生产环境。

## 项目概述

### 讲讲这个项目（一分钟版）

**一句话答案**：一个用 Postgres 做协调的 Python 后台任务服务，核心是租约加 fencing token，保证 worker 崩溃或卡住时任务能被别人接手，而且旧 worker 迟到的写入一定落不了地。

**展开**：API 用 FastAPI，提交、查询、取消任务，看尝试历史和租约时间线。worker 用 `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)` 认领，认领时 fencing token 加一，执行时心跳续租。租约过期，任何活着的 worker 都会回收；旧 worker 醒来，它的写入在我们库里和下游集成里都会因为 token 过期被拒。旁边还有三样东西：一个带索引的最小堆调度器（延迟和 cron 任务，带补跑策略），一个放在 Postgres 里、所有 worker 共享的令牌桶（按外部集成限速，拿不到就交回调度器），一个手写的 O(1) LRU 结果缓存。状态机在 Python 和 SQL 触发器里各定义一遍，测试保证两份一致。

**追问**：
- *为什么不用 Celery / RQ？* 它们解决的是「把任务发出去」，这个项目要展示的是「任务被谁持有、持有到什么时候、失去持有以后会发生什么」。这些在 Celery 里是 broker 的 visibility timeout 加 acks_late，行为藏在 broker 里，而且没有 fencing。这里每一步都是一行 SQL，能直接测。
- *最难的部分是什么？* 不是回收，是 fencing：要承认「被判死」只是猜测，然后让所有写入都带上 token，包括写外部系统的那一次。见 [Q5](problems.md#q5-被判死的-worker-醒过来覆盖了新结果) 和 [Q6](problems.md#q6-外部系统收到了两次写入)。

### 一个任务从提交到完成，经过哪些状态？

**一句话答案**：SCHEDULED（可选）→ PENDING → LEASED → RUNNING → SUCCEEDED，失败分 FAILED_RETRYABLE（回到 SCHEDULED）、FAILED_TERMINAL、TIMED_OUT，以及 CANCELLED。

**展开**：边表在 `leasejobs/states.py · @learn:state-machine`，同一张表在 `sql/001_core.sql · @learn:sql-transitions` 里由触发器强制。LEASED → PENDING 和 RUNNING → PENDING 是租约过期回收；LEASED → SCHEDULED 是没拿到限速令牌；FAILED_RETRYABLE → SCHEDULED 是退避重试。所有改状态的地方都走 `store.transition`，SQL 一定带 `AND state = <from>`，改不到就返回 None。

**追问**：
- *为什么 FAILED_RETRYABLE 不直接回 PENDING？* 因为重试要等退避时间，「等」是调度器的事。worker 不 sleep。
- *两步状态（RUNNING → FAILED_RETRYABLE → SCHEDULED）中间崩了怎么办？* 两步在一个事务里（`Worker.on_failure`），要么都成，要么都不成。

### 这个仓库里哪些数字是实测的？

**一句话答案**：README 和 `docs/results/` 里的全部数字都是这台机器上跑出来的，多次运行取中位数，比如 kill -9 后多久被回收、认领吞吐、LRU 每次操作的纳秒数、限速推迟次数。

**展开**：`scripts/bench.py` 每项跑五次取中位数，写进 `docs/results/bench.txt`；失败对照写进 `docs/results/failing-controls.txt`；限速改进前后的对比在 `docs/results/ratelimit-deferrals.txt`。测量时同一个 colima 里还跑着别的项目，所以只看相对比较，不把一次偶然的好数字当结论。

**追问**：
- *和简历上的数字什么关系？* 没有关系，见本页最后一节。

## 设计取舍

### 为什么用 Postgres 做队列，而不是 Kafka / Redis / SQS？

**一句话答案**：这里要的是「行级的持有关系加强一致的条件写」，Postgres 的行锁、`SKIP LOCKED` 和事务正好给出这些，而且任务状态、尝试历史、结果本来就要存在库里。

**展开**：租约就是一行上的三个字段，认领、续租、回收、写结果都是一条带条件的 UPDATE，和状态机、事件表在同一个事务里。换成 Kafka，分区级的消费位点表达不了「这一个任务被谁持有到几点」；换成 Redis，要自己保证锁和状态的原子性，还要另存历史。代价是吞吐上限受单个 Postgres 限制，这一点在扩展类问题里讲。

**追问**：
- *什么时候应该换？* 每秒上万个很短的任务，或者需要跨地域，Postgres 单点就不合适了。那时可以保留「租约 + token」的语义，把存储换成支持条件写的分布式 KV（比如 DynamoDB 的条件更新）。

### 认领为什么用 FOR UPDATE SKIP LOCKED？

**一句话答案**：让多个 worker 同时认领时各自锁到不同的行，而不是排队等同一行。

**展开**：`leasejobs/lease.py · @learn:claim` 是一条语句：子查询 `SELECT id … ORDER BY priority DESC, run_at, id LIMIT 1 FOR UPDATE SKIP LOCKED`，外层 UPDATE 设持有者、过期时间，token 加一。`test_skip_locked_skips_a_row_another_transaction_holds` 用一个事务锁住队头，另一个连接设两秒语句超时去认领，立刻拿到第二行。

**追问**：
- *它快了多少？* 看并发度。`docs/results/bench.txt` 里两千个空任务、五次中位数：一个和四个 worker 时两者一样（四个时 285 对 293 个每秒）；八个 worker 时 SKIP LOCKED 478，普通 `FOR UPDATE` 332。认领事务只有一条语句，并发低时锁等待可以忽略。
- *普通 FOR UPDATE 会出错吗？* 不会多跑，但被阻塞的那个在锁释放后重新检查条件，发现行已经不是 PENDING，可能直接返回空，白跑一轮。

### 租约的时间用谁的钟？

**一句话答案**：数据库的钟。`lease_expires_at = now() + ttl`，判断过期也用 `now()`。

**展开**：worker 之间的时钟总有偏差，用各自的钟判断就可能出现两个人都认为自己持有租约。统一用一个钟，偏差问题就消失了。调度器例外：它用本地时间加上每次重建堆时测得的偏移（`Scheduler.skew`）来决定睡多久，但真正改状态时的条件仍然是数据库时间。

**追问**：
- *TTL 怎么定？* 默认四秒，心跳一秒。TTL 越短，崩溃后恢复越快；但太接近心跳周期，一次 GC 或网络抖动就会让活着的 worker 丢租约，表现为 `stale_writes_rejected` 变多。告警 `LeasejobsStaleWritesSpike` 就是盯这个。

### 令牌桶为什么放在 Postgres 里，而不是每个 worker 一个？

**一句话答案**：限速是外部集成的规则，必须是全局的；每个 worker 一个桶，真实速率就是配置的 N 倍，而且随扩缩容变化。

**展开**：`leasejobs/ratelimit.py · @learn:bucket` 一个集成一行，懒补充：同一条语句里先算「过了多久该补多少」，封顶，再扣一个。行锁让所有 worker 在这一行上串行，代价是每次调用多一次数据库往返。`test_shared_bucket_holds_the_global_rate_across_three_workers` 用三个 worker 进程验证：任何时刻累计调用数不超过「容量 + 速率 × 时间 + 1」。关掉限速后同一测试十六次调用挤在零点一四秒里，超出界限十二点七。

**追问**：
- *这一行会不会成为热点？* 会，所有调用同一集成的 worker 都在抢它。但被限速的集成本来每秒只允许几次调用，这行每秒也就被更新几十次（含被拒的），远低于 Postgres 单行的承受能力。
- *Redis 不是更合适吗？* 如果已经有 Redis，用 Lua 脚本做同样的事更轻。这里不引入新组件，状态都在一处。

### 拿不到令牌为什么不等，而是交回调度器？

**一句话答案**：等待会占住 worker，让不相关的任务也排队；交回调度器以后 worker 马上去干别的。

**展开**：`LeaseManager.defer`（`@learn:defer`）把任务 LEASED → SCHEDULED，`run_at` 设成「大约什么时候有令牌」，放掉租约，`deferrals` 加一，不算尝试次数。`test_no_token_defers_instead_of_blocking` 断言同一个 worker 紧接着就能认领下一个任务。

**追问**：
- *推迟的任务会不会扎堆回来？* 会，第一版就扎堆了：十六个任务推迟了九十一次。现在按「排队数加一除以补充速率」撒开，三次实测中位数降到二十二次，代价是总时间多零点四秒。见 [Q2](problems.md#q2-被限速的任务一起回来又一起被拒)。

### 调度器为什么自己写一个带索引的堆？

**一句话答案**：调度器要能删除和修改堆里的元素（取消的延迟任务、改了时间的 schedule），`heapq` 只能 push/pop。

**展开**：`leasejobs/heap.py` 数组之外维护 `key → 下标`，删除和改优先级都是 O(log n)；同优先级按插入顺序出。hypothesis 生成三百组随机操作，每一步和「字典加排序」的模型对比。堆只是索引，真相在 Postgres：启动和每十五秒从库里重建；平时靠 `pg_notify` 触发器（`@learn:sql-notify`）唤醒。

**追问**：
- *不用 heapq 加墓碑行不行？* 行，但被取消的元素会一直占着位置，直到浮到堆顶。延迟任务大量被取消时，堆会膨胀。
- *两个调度器同时跑会怎样？* 只有拿到 `pg_try_advisory_lock` 的那个干活，另一个每两秒重试。主调度器连接断开，锁自动释放。

### 为什么自己写 LRU，不用 functools.lru_cache？

**一句话答案**：要展示 O(1) 是怎么来的：哈希表找节点，双向链表维护新旧顺序，两个哨兵节点省掉所有边界判断。

**展开**：`leasejobs/cache.py · @learn:lru-core`。结果在 SUCCEEDED 之后不可变，所以缓存不需要失效，只需要容量。有两条填充路径：读穿（`X-Cache: MISS` 然后 `HIT`）和成功通知预热。正确性靠和 OrderedDict 版本的状态机差分测试；O(1) 靠同机相对比较：一千条 567 纳秒、十万条 699 纳秒每次操作（`docs/results/bench.txt`，五次中位数）。

**追问**：
- *比 OrderedDict 快吗？* 不快，慢两到四倍（同一测试 143 和 290 纳秒）。OrderedDict 的链表是 C 写的，我的是 Python 对象和属性访问。写它是为了把结构讲清楚，生产上我会直接用 OrderedDict。
- *多个 API 进程怎么办？* 每个进程一份，互不同步。因为结果不可变，不同步也不会读到旧值，只是命中率低一些。要共享就换 Redis。
- *线程安全吗？* 一把锁包住 get/put。FastAPI 的同步接口跑在线程池里，所以需要。

## 正确性和故障

### worker 被 kill -9，它手上的任务怎么办？

**一句话答案**：心跳停了，租约在 TTL 后过期；下一个跑到「回收」那一步的活着的 worker 把它放回 PENDING，然后正常认领，token 加一。

**展开**：`@learn:reap`。回收和认领都用 `SKIP LOCKED`，带着看到的 token 做条件转移，两人同时回收也只有一个成功。尝试历史里旧的那次记为 `lease_expired`。`test_kill9_worker_job_is_reclaimed_with_history_intact` 用真进程测：历史是 `[(1,'w1',1,'lease_expired'), (2,'w2',2,'succeeded')]`。kill 到被另一个 worker 认领，五次中位数 3.59 秒（TTL 四秒、心跳一秒，`docs/results/bench.txt`），基本等于「TTL 减去距离上次心跳的时间」再加一次轮询。

**追问**：
- *最后一次尝试时被杀呢？* 直接 FAILED_TERMINAL，`last_error` 是 `lease expired on the last attempt`。否则一个总把 worker 搞死的任务会无限循环。
- *所有 worker 都死了呢？* 没人回收，`leasejobs_stalled_jobs{reason="lease_expired_unreclaimed"}` 变成非零，`LeasejobsNoWorkers` 告警。

### worker 没死，只是卡住了（GC、虚拟机暂停），醒来会怎样？

**一句话答案**：它的每一次写入都带着旧 token，在我们库里改到零行，在下游集成那里收到 409，结果落不了地。

**展开**：`@learn:fence` 生成 `lease_owner = me AND fencing_token = my_token`。learn 页面的样例就是这个场景：worker 在写结果前给自己发 SIGSTOP（`@learn:freeze`），六秒后醒来，心跳和写结果都被拒，事件表里两条 `stale_write_rejected`，`my_token 1`、`current_token 2`。

**追问**：
- *醒来以后先检查一下租约再写，不就行了？* 检查和写之间还是可能被暂停。这正是 fencing 存在的原因：让写入本身就是检查。
- *关掉 fencing 会怎样？* `test_stale_holder_cannot_heartbeat_or_finish` 红：`stale worker overwrote the result: {'by': 'w-a'}`。见 [Q5](problems.md#q5-被判死的-worker-醒过来覆盖了新结果)。

### fencing 能保证外部副作用只发生一次吗？

**一句话答案**：不能。它保证最后落地的值属于最新的租约，不保证对方只收到一次。

**展开**：旧 worker 如果醒得早，赶在新 worker 之前写了外部系统，对方会先收下 token 1，再收下 token 2。最终值对，但收到两次。要真正一次，需要下游按 job id 做幂等。仓库里的假集成（`@learn:integration-fence`）只做了 token 单调，这一点写在 README 的局限里。

**追问**：
- *那 fencing 有什么用？* 防止的是更糟的情况：旧的写在新的后面，把正确结果覆盖掉。`no_fencing` 下的 SIGSTOP 测试就抓到了下游收了 w2 又收了 w1。见 [Q6](problems.md#q6-外部系统收到了两次写入)。

### 同一个任务会不会被两个 worker 同时跑？

**一句话答案**：在租约有效期内不会；租约过期后有可能短暂重叠（旧的卡住、新的已接手），这时由 fencing 保证只有新的能写。

**展开**：`test_four_processes_run_each_job_exactly_once` 四个进程跑四百个 `tally` 任务，每个 job 在副作用表里正好一行，尝试次数全是一。bench 里每轮两千个任务也检查了没有重复尝试。重叠场景由 SIGSTOP 测试覆盖。

**追问**：
- *能做到严格不重叠吗？* 不能在不可靠网络上做到。持有者无法确定自己还持有，这是 lease 的本质。所以设计目标是「重叠无害」，不是「不重叠」。

### 任务超时怎么执行？任务自己不配合呢？

**一句话答案**：worker 把任务放在单独线程，最多等到截止时间加半秒，然后放弃它、按超时处理。

**展开**：`@learn:hard-timeout`。守规矩的任务通过 `ctx.check()` 自己停；不守规矩的（`block` 类型）会被放弃。Python 线程杀不掉，被放弃的线程如果以后再写，会因为 token 过期被拒。最后一次尝试超时是 TIMED_OUT，之前的是 FAILED_RETRYABLE 再退避。

**追问**：
- *被放弃的线程会泄漏吗？* 会，直到它自己结束。真要硬隔离，要把每次尝试放进子进程，到点就杀。这是已知局限。见 [Q7](problems.md#q7-一个不守规矩的任务让超时形同虚设)。

### 依赖挂了会怎样？

**一句话答案**：5xx 和连不上算可重试，指数退避加抖动；4xx 不重试直接终态；409 说明别人接手了，放弃这次尝试。

**展开**：`call_integration` 的分类在 `@learn:call-integration`，决定在 `@learn:on-failure`。`tests/test_failures.py` 用假集成注入：503 两次后第三次成功；400 一次就终态；端口上没人时两次用完 FAILED_TERMINAL，`last_error` 是 `integration unreachable`。

**追问**：
- *重试为什么不在 worker 里 sleep？* 交回调度器，worker 不被占住，而且 worker 在 sleep 时被杀，重试也不会丢。

### 调度器挂了一段时间，错过的周期任务怎么办？

**一句话答案**：每个 schedule 选策略：`skip` 只补最近一次，`catch_up` 每个窗口都补但有上限。

**展开**：`@learn:misfire`。另有唯一索引 `(schedule_id, scheduled_for)`，调度器在「插入任务」和「更新下次时间」之间崩溃再重放，也不会补出两份（`test_refiring_the_same_slot_creates_no_second_job`）。见 [Q8](problems.md#q8-调度器停了四十多分钟恢复后一口气补了四十三个任务)。

**追问**：
- *延迟任务呢？* 它们是 SCHEDULED 的行，调度器重启时从库里重建堆，不会丢。调度器一直不在时，`scheduled_overdue` 卡住检测会报。

### 取消一个正在跑的任务，怎么传到 worker？

**一句话答案**：API 只设 `cancel_requested = true` 并返回 202；worker 的心跳每次续租都读回这个字段，看到就让任务停下，写 RUNNING → CANCELLED。

**展开**：没开始的任务（SCHEDULED、PENDING）API 直接改成 CANCELLED；已经终态的返回 409。`test_cancel_reaches_a_running_job_through_the_heartbeat` 用真 worker 进程验证。

**追问**：
- *任务不检查呢？* 那就要等到它自己结束或超时。取消和超时一样，是协作式的，硬截止只对超时做了。

### 「卡住的任务」怎么发现？

**一句话答案**：一条 SQL 查三种卡住，每种指向一个不同的坏部件：租约过期没人收（没有活 worker）、运行超时还在心跳（worker 没执行截止时间）、到期很久还是 SCHEDULED（没有调度器）。

**展开**：`@learn:stalled-sql`，`GET /stalled` 返回明细，`leasejobs_stalled_jobs{reason}` 给告警。`tests/test_observability.py` 分别造出三种情况。处理步骤写在 `docs/troubleshooting.md`。

**追问**：
- *为什么按原因分，而不是一个总数？* 总数只告诉你「有事」，原因告诉你「去看哪里」。三种的处理完全不一样。

## 扩展

### 流量放大十倍，瓶颈在哪？

**一句话答案**：先是 worker 数量，然后是 Postgres 上认领和心跳的写入。

**展开**：每个任务至少有认领、开始、完成三次写，加上每秒一次心跳和事件表的插入。bench 里八个 worker 跑空任务是每秒 478 个（`docs/results/bench.txt`），那是协调开销的上限，不是业务吞吐。十倍时先加 worker 进程（每个进程一次一个任务）。心跳事件现在每次都写一行 `job_events`，这是最先该砍的：心跳只更新任务行，事件表只记状态变化。

**追问**：
- *一百倍呢？* 单个 Postgres 扛不住每秒几万次带锁的更新。要按 owner 或任务类型分片到多个库，每个分片有自己的 worker 池；或者换成支持条件写的分布式存储，保留租约和 token 的语义。
- *认领能批量吗？* 能，`LIMIT k` 一次拿多个，但租约就成了批量的，一个任务慢会拖住整批的续租。

### 心跳会不会把数据库打满？

**一句话答案**：会是第一个压力点：N 个运行中的任务，每秒 N 次更新加 N 次事件插入。

**展开**：现在心跳每次都写 `job_events`，是为了在 learn 页面和租约时间线上看得到续租。生产上应该只更新任务行，或者按 worker 批量续租：一个 worker 一条 UPDATE 续它持有的所有租约。再往后可以把 TTL 拉长、心跳放慢，换来的代价是恢复变慢。

**追问**：
- *事件表会无限增长吗？* 会。需要按时间分区或定期清理终态任务的事件。这个仓库没有做，写在局限里。

### 限速的那一行会成为全局瓶颈吗？

**一句话答案**：对单个集成来说是串行点，但它的负载上限由「被限的速率」决定，本来就很低；不同集成是不同的行，互不影响。

**展开**：真正的风险是被拒后的重新排队：推迟的任务越多，认领、推迟的往返越多。Q2 那次改进就是在减少这种无效往返。

**追问**：
- *集成多到上千个呢？* 每个集成一行，没有问题。问题在于 `acquire_token` 里数排队数的那条查询要走 payload 的 JSON 字段，上量后要加一列 integration 并建索引。

## 测试

### 你怎么证明它是对的？

**一句话答案**：每一个保护都有一个真进程的测试，而且每个测试都有一个关掉保护后必须变红的对照。

**展开**：一百六十多个 pytest 用真 Postgres：状态机（九乘九组合加触发器）、并发认领（四进程四百任务）、kill -9 恢复、SIGSTOP 再恢复、调度和补跑、限速的全局界限、重试和超时、取消、依赖失败、卡住检测、supervisor 重启。纯逻辑（堆、cron、补跑、LRU）用 hypothesis 和模型对比。界面上 Playwright 从 learn 页面点一遍，断言阶段依次亮起、源码链接指向真实的行。

**追问**：
- *时间相关的测试会不会不稳定？* 断言都写成相对的或者来自配置的：限速用「容量加速率乘时间」，LRU 用「大的不超过小的三倍」，延迟任务只断言「没有早于 run_at」。不写本机调出来的绝对毫秒数。

### 那个「能失败的对照」是怎么设计的？

**一句话答案**：`LEASEJOBS_LAB_SABOTAGE` 开关把一个保护关掉，对应的硬核测试必须变红；打开后同一测试必须变绿；两次输出都贴在 README 里。

**展开**：四个开关：`no_reclaim`（kill -9 恢复测试超时，任务卡在 RUNNING，租约过期十三秒多）、`no_fencing`（旧 worker 覆盖结果；下游收了两次）、`no_ratelimit`（超出桶界限十二点七）、`no_skip_locked`（只用于性能对比，不是正确性对照）。`scripts/failing_controls.sh` 一键跑完，结果写进 `docs/results/failing-controls.txt`。拼错的开关名会直接报错，防止「以为关了其实没关」造出一个假的绿灯。

**追问**：
- *为什么要对照？* 绿灯有两种成因：代码对，或者测试根本测不到。对照证明是前一种。

### 测试怎么杀 worker？

**一句话答案**：只按自己启动的子进程的 PID 发信号，从不按名字。

**展开**：`tests/helpers.py` 的 `Proc` 记下 `Popen` 的 PID，`kill9()`、`signal(SIGSTOP)` 都是 `os.kill(self.pid, …)`。演示里「杀掉 worker」按钮让 worker 对自己的 PID 发 SIGKILL。supervisor 停止时也只停自己记下的子进程。

**追问**：
- *为什么这么较真？* 同一台机器上还有别的项目在跑，按名字杀进程会误伤。

## 系统设计延伸

### 上生产还缺什么？

**一句话答案**：认证授权、事件表清理、多分片、真正的进程隔离、告警通知渠道，以及对下游的幂等约定。

**展开**：现在鉴权是 API key 的 sha256 查表，没有轮换和权限分级；worker 的管理端口只绑本机、没鉴权。事件表和尝试表没有保留策略。任务在线程里跑，硬隔离要子进程或容器。Prometheus 有告警规则但没接 Alertmanager。部署是本机进程加两个容器，没有 Dockerfile 和编排清单。

**追问**：
- *先做哪一个？* 事件表清理和心跳不写事件。这两个不做，表会在几周内成为最大的问题。

### 换成 AWS，你会怎么改？

**一句话答案**：Postgres 换 RDS / Aurora，语义不用变；或者换 DynamoDB，用条件写实现同样的租约和 token。

**展开**：DynamoDB 版本：认领是 `UpdateItem` 带 `ConditionExpression state = PENDING`，token 用 `ADD fencing_token 1`；续租和写结果都带 `fencing_token = :mine`。没有 `SKIP LOCKED`，要靠按状态分区的 GSI 加随机起点来分散冲突。调度可以交给 EventBridge Scheduler，限速桶用 DynamoDB 一行或 ElastiCache。worker 跑在 ECS，supervisor 的职责交给服务的期望数量。

**追问**：
- *SQS 的 visibility timeout 不就是租约吗？* 是租约，但没有 fencing：消息重新可见以后，旧消费者依然可以把结果写到你的数据库里。你仍然要在写入端做 token 检查。

## 简历对应

### 简历上写「restart recovery」，面试官会怎么问？

**一句话答案**：会问「重启后任务怎么不丢、怎么不重复」，我用 kill -9 测试和 SIGSTOP 测试来回答，并说清楚 fencing 的边界。

**展开**：不丢：状态和尝试历史都在 Postgres，租约过期后任何 worker 回收，supervisor 重启出来的新进程（`worker-3.g2`）和别人一样去抢，第 8 章有一次真跑的事件序列。不失控重复：fencing token 让旧持有者的写入落不了地。要主动说的边界：下游真正只收一次需要幂等。

**追问**：
- *简历上的数字是这个仓库测的吗？* 不是。简历上的数字是在公司生产环境里的结果，这个仓库没有那样的负载，也不声称复现它们。这个仓库能说的是它自己测出来的：kill -9 到被回收中位 3.59 秒、八个 worker 空任务每秒 478 个、四组对照的红绿结果，都在 `docs/results/`。

### 简历上写「min-heap scheduling、O(1) LRU、per-integration token bucket」，怎么证明不是背的？

**一句话答案**：打开对应文件讲实现，再讲各自的测试：堆和 LRU 都有和参照模型的随机差分测试，令牌桶有三进程的全局界限测试和关掉后的红灯。

**展开**：堆是带索引的（支持删除和改优先级），为什么需要它可以用「取消延迟任务」说明。LRU 是哈希表加双向链表加哨兵，O(1) 有同机的规模对比。令牌桶放在 Postgres 里共享，拿不到交回调度器，Q2 是我在它上面真踩到的坑和修复前后的数字。

**追问**：
- *为什么限速的推迟次数是二十二而不是零？* 推迟本身就是限速在工作：十六个任务、容量三，至少十三个要等。二十二次说明平均每个等待的任务被打回一到两次。

### 简历上写「state machine with validated transitions」，追问会是什么？

**一句话答案**：「非法转移怎么拦？两个人同时转移怎么办？」答：两层拦截加 compare-and-set。

**展开**：Python 层 `check()` 在发 SQL 前拒绝，数据库层触发器拒绝（`CheckViolation illegal job transition SUCCEEDED -> PENDING`）。并发由 `WHERE state = <from>` 解决，输的一方拿到 None。测试保证两层的边表一致。

**追问**：
- *为什么不只靠数据库？* 应用层先拒绝，错误信息更清楚，也省一次往返；数据库层是兜底，防的是绕过应用的写入。
