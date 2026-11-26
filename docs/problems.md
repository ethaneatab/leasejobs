# 日常问题与解决

这一页回答面试里那个问题：「你遇到过什么问题，怎么定位，怎么解决的？」

每个问题标了来源：

- **真实踩坑**：做这个仓库时真的遇到的 bug、红灯；
- **典型事故**：这类系统在生产上的常见事故，用 `LEASEJOBS_LAB_SABOTAGE` 开关在仓库里复现过。

代码位置用 `@learn` 锚点标注（`node scripts/learn_anchors.mjs` 会生成带行号的 GitHub 链接，learn 页面上的「看源码」用的就是它）。每个问题都有一段逐行高亮的讲解视频，素材在 `video/problems.src.json`。

## Q1. 预热线程卡死，日志里一个错都没有
<!-- id: q01-notify-deadlock -->

**来源：真实踩坑**（第 6 章，写缓存预热时）

**现象**

```
tests/test_result_cache.py::test_success_notify_warms_the_cache
E       AssertionError: timed out after 5s waiting for NOTIFY to warm the cache
```

`var/api.log` 和测试输出里没有任何异常。

**在哪里**：[`leasejobs/api.py`](../leasejobs/api.py) 的 `@learn:cache-warm`（`warm_cache_forever`）；修复前的版本在 [`docs/problems/snippets/notify_deadlock_before.py`](problems/snippets/notify_deadlock_before.py) 的 `@learn:legacy-notify-loop`。

**怎么定位**

1. 假设触发器没装上 → 查 `pg_trigger`，`jobs_notify_succeeded` 在 → 排除。
2. 假设通知没发 → 另开一个连接 `LISTEN leasejobs_succeeded`，手动把一行改成 SUCCEEDED，立刻收到 `['2']` → 排除。
3. 只剩预热线程。`conn.notifies()` 是生成器，迭代期间一直持有连接锁；循环体里用**同一个连接** `execute`，要拿同一把锁 → 线程自己等自己。没有异常，所以没有日志。

**怎么解决**

修复前：

```python
for n in lc.notifies(timeout=0.5, stop_after=1):
    row = lc.execute("SELECT ... WHERE id = %s", (int(n.payload),)).fetchone()
```

修复后：

```python
ids = [int(n.payload) for n in lc.notifies(timeout=0.5, stop_after=1)]
for jid in ids:
    row = lc.execute("SELECT ... WHERE id = %s", (jid,)).fetchone()
```

顺带修了第二个竞态：测试可能在线程 LISTEN 之前就跑完任务。现在 LISTEN 成功后 `listening.set()`，测试先 `app.state.cache_listening.wait(5)`。

**怎么证明修好了**：`test_success_notify_warms_the_cache` 修复前稳定超时，修复后连续多轮全套测试通过。

**面试怎么讲**：缓存预热测试一直超时，但没有任何报错（S）。我要找到它为什么静默失败（T）。我用排除法先证明触发器和通知都正常，把范围缩到线程自己，发现是在 psycopg 的通知生成器里复用了同一个连接，自己等自己的锁（A）。改成先收集再查询后测试转绿，我还补了一个「监听就绪」信号消除测试里的竞态（R）。

## Q2. 被限速的任务一起回来，又一起被拒
<!-- id: q02-defer-stampede -->

**来源：真实踩坑**（第 5 章，写限速测试时）

**现象**：测试是绿的，但打印出来的数字不对劲：

```
16 calls over 6.57s; worst excess over the bucket bound 0.00; 91 deferrals
```

十六个任务，推迟了九十一次。

**在哪里**：[`leasejobs/worker.py`](../leasejobs/worker.py) 的 `@learn:worker-token`（`acquire_token`）；修复前在 [`docs/problems/snippets/defer_stampede_before.py`](problems/snippets/defer_stampede_before.py) 的 `@learn:legacy-defer`。

**怎么定位**

1. 假设桶的计算错了，放进来的比该放的少 → 判据 `worst excess` 接近零，总时间 6.5 秒等于理论下限 → 排除。
2. 假设是被拒的任务同步回来 → 看事件表，同一个时刻有十几条 `rate_limited`，`wait_s` 几乎一样 → 确认。每次回来只有一个拿到令牌。

**怎么解决**

修复前：

```python
wait = g.wait_s * (1 + random.random() * 0.2)
```

修复后（按桶的真实服务能力错开）：

```python
waiting = self.conn.execute("SELECT count(*) AS n FROM jobs WHERE state = 'SCHEDULED' AND deferrals > 0 "
                            "AND payload->>'integration' = %s", (name,)).fetchone()["n"]
wait = g.wait_s + random.uniform(0, (waiting + 1) / g.refill_per_s)
```

**怎么证明修好了**：同一个测试修复前后各跑三次（`docs/results/ratelimit-deferrals.txt`）：推迟次数中位数 91 → 22，总耗时中位数 6.54 s → 6.96 s。这是取舍，两个数字都写着。

**面试怎么讲**：限速功能是对的，但推迟次数是任务数的五倍多（S）。我要减少无效的往返又不突破全局速率（T）。我从事件表看到被拒的任务拿到同样的等待提示、同时回来，于是按「排队数除以补充速率」把它们撒开（A）。推迟次数降了约四分之三，代价是总时间多零点四秒，两组实测都记在仓库里（R）。

## Q3. 没配限速的集成，让 worker 一直认领不到任务
<!-- id: q03-infinity-json -->

**来源：真实踩坑**（第 5 章，接入限速以后旧测试变红）

**现象**

```
E       AssertionError: timed out after 20.0s waiting for w1 running the job
FAILED tests/test_recovery.py::test_kill9_worker_job_is_reclaimed_with_history_intact
```

worker 子进程的日志最后几行：

```
psycopg.errors.InvalidTextRepresentation: invalid input syntax for type json
DETAIL:  Token "Infinity" is invalid.
CONTEXT:  JSON data, line 1: ...tegration": "ledger-api", "tokens_left": Infinity...
```

**在哪里**：[`leasejobs/ratelimit.py`](../leasejobs/ratelimit.py) 的 `@learn:bucket`；修复前在 [`docs/problems/snippets/infinity_json_before.py`](problems/snippets/infinity_json_before.py) 的 `@learn:legacy-infinity`。

**怎么定位**

1. 假设超时设短了 → 但这个测试之前稳定通过，改动只有限速 → 先不调超时，去看子进程输出（测试给每个 worker 写了日志文件）。
2. 日志里是 Postgres 拒绝 JSON。测试夹具每个测试都清空表，包括 `integrations`，于是集成「没有配置」，代码返回 `float("inf")` 表示不限速。
3. `json.dumps(float("inf"))` 写出 `Infinity`，Python 认，Postgres 不认。worker 主循环在认领后崩掉。

**怎么解决**

修复前：`return Grant(True, name, float("inf"), float("inf"), 0.0)`

修复后：`return Grant(True, name, None, None, 0.0)`，类型标成 `float | None`。另外 worker 主循环现在接住意外异常、记 `loop error`、一秒后继续，不再因为一行坏数据整个退出。

**怎么证明修好了**：两个恢复测试重新变绿；全套测试通过。

**面试怎么讲**：加完限速，两个和限速无关的恢复测试超时了（S）。我要找到真正的原因而不是调大超时（T）。我先去看子进程日志，发现「不限速」用了无穷大，被序列化成 Postgres 不接受的 JSON，worker 循环崩溃（A）。改成用空值表示，并让主循环对意外异常有韧性，测试恢复（R）。

## Q4. worker 被杀以后，任务永远停在 RUNNING
<!-- id: q04-zombie-lease -->

**来源：典型事故**（用 `LEASEJOBS_LAB_SABOTAGE=no_reclaim` 复现）

**现象**（`docs/results/failing-controls.txt`）

```
E       AssertionError: timed out after 15s waiting for another worker to reclaim and finish the job;
        saw {'state': 'RUNNING', 'lease_owner': 'w1', 'fencing_token': 1, 'expired_s_ago': Decimal('13.6')}
```

**在哪里**：[`leasejobs/lease.py`](../leasejobs/lease.py) 的 `@learn:reap`（`reap_expired`）；开关分支是 `@learn:lab-no-reclaim`。

**怎么定位**

1. 看任务行：持有者是已经被 `kill -9` 的 w1，`lease_expires_at` 早已过去 → 租约机制本身没问题，是「没人看过期」。
2. 看 `/stalled`：reason 是 `lease_expired_unreclaimed`，这个 reason 的含义就是「没有活着的 worker 在回收」。
3. 看活着的 w2 的日志：没有 `reclaimed expired lease` → 回收这一步没跑。

**怎么解决**

修复前（开关打开时）：

```python
if "no_reclaim" in sabotage():
    return []
```

修复后：每个 worker 认领前都跑 `reap_expired`：`... WHERE state IN ('LEASED','RUNNING') AND lease_expires_at < now() ... FOR UPDATE SKIP LOCKED`，带着看到的 token 做条件转移；最后一次尝试时过期的直接 FAILED_TERMINAL。

**怎么证明修好了**：`test_kill9_worker_job_is_reclaimed_with_history_intact` 开关打开红、关掉绿；尝试历史是 `[(1, 'w1', 1, 'lease_expired'), (2, 'w2', 2, 'succeeded')]`。

**面试怎么讲**：worker 崩溃后任务会永远挂在它名下（S）。恢复不能依赖那个已经死了的进程（T）。我让每个活着的 worker 在认领前回收过期租约，条件转移防止两人同时回收，最后一次尝试过期就终止（A）。kill -9 测试在关掉回收时变红、打开时变绿，任务历史完整（R）。

## Q5. 被判死的 worker 醒过来，覆盖了新结果
<!-- id: q05-stale-overwrite -->

**来源：典型事故**（用 `LEASEJOBS_LAB_SABOTAGE=no_fencing` 复现）

**现象**

```
E       AssertionError: stale worker overwrote the result: {'by': 'w-a'}
E       assert {'by': 'w-a'} == {'by': 'w-b'}
```

**在哪里**：[`leasejobs/lease.py`](../leasejobs/lease.py) 的 `@learn:fence`；开关分支 `@learn:lab-no-fencing`；所有写入（`heartbeat`、`start`、`finish`）都用它。

**怎么定位**

1. 事件表里 w-a 的 `finish` 在 w-b 的 `claim`（token 2）之后，而且成功了 → 写入没有检查「我是不是现任」。
2. 看 `fence()`：开关打开时返回 `"TRUE"`，只剩状态条件；w-b 正在 RUNNING，状态条件对 w-a 也成立。

**怎么解决**

修复前：`return "TRUE", ()`

修复后：`return "lease_owner = %s AND fencing_token = %s", (self.worker_id, job["fencing_token"])`。被拒的写入记一条 `stale_write_rejected`，带 `my_token` 和 `current_token`。

**怎么证明修好了**：`test_stale_holder_cannot_heartbeat_or_finish`（进程内，精确）和 `test_sigstop_worker_revives_and_is_fenced_out`（真的 SIGSTOP/SIGCONT）都是开关打开红、关掉绿。

**面试怎么讲**：一个卡住的 worker 醒来后，会把旧结果写在新结果上面（S）。我需要让迟到的写入自己失败（T）。每次认领令牌号加一，所有写入都带「持有者是我且令牌号相同」的条件（A）。对照测试证明关掉令牌会覆盖、打开后旧写入改到零行并留下记录（R）。

## Q6. 外部系统收到了两次写入
<!-- id: q06-duplicate-downstream -->

**来源：典型事故**（`no_fencing` 下的 SIGSTOP 测试）

**现象**

```
E       AssertionError: accepted writes: [{'token': None, ..., 'attempt': 2, 'worker': 'w2', ...},
                                          {'token': None, ..., 'attempt': 1, 'worker': 'w1', ...}]
E       assert ['w2', 'w1'] == ['w2']
```

我们库里的结果是对的（w2），但外部集成收了两次。

**在哪里**：[`leasejobs/integration.py`](../leasejobs/integration.py) 的 `@learn:integration-fence`，以及 [`leasejobs/jobs.py`](../leasejobs/jobs.py) 的 `@learn:call-integration`（发 `X-Fencing-Token`）。

**怎么定位**

1. 为什么库里挡住了？w1 写库时任务已经是 SUCCEEDED，状态条件不成立。这是巧合，不是设计。
2. 外部系统没有任何条件 → 只在自己库里 fencing 是不够的。

**怎么解决**：令牌放进请求头；外部系统记每个 key 见过的最大令牌，小的回 409；worker 把 409 当成 `FencedOut`，放弃这次尝试。

**怎么证明修好了**：同一个测试打开令牌后：`acct-7` 的 accepted 只有 w2，rejected 是 `[1]`。

**面试怎么讲**：关掉令牌后，外部系统收到了重复写入（S）。fencing 要一直延伸到真正产生副作用的地方（T）。我把令牌带到下游，由下游拒绝旧令牌（A）。但我会讲清边界：它保证最后落地的值属于最新租约，不保证只发生一次，那需要下游按任务做幂等（R）。

## Q7. 一个不守规矩的任务，让超时形同虚设
<!-- id: q07-runaway-job -->

**来源：典型事故**（这个测试和修复是一起写的，没有修复前的红灯记录）

**现象**：任务在一个阻塞调用里不出来，也不调 `ctx.check()`。`timeout_s=0.3`，但 worker 被占住整整三秒，这期间它手上的其他任务都在等。

**在哪里**：[`leasejobs/worker.py`](../leasejobs/worker.py) 的 `@learn:hard-timeout`（`run_with_deadline`）；修复前的写法在 [`docs/problems/snippets/run_inline_before.py`](problems/snippets/run_inline_before.py)。

**怎么定位**：超时是协作式的（`ctx.check()` 抛 `AttemptTimeout`），所以它的正确性取决于每一个 job 的写法。只要有一个 job 不检查，约定就破了。

**怎么解决**：job 在单独线程里跑，主线程 `join(截止时间 + 0.5 秒)`；还活着就标记 lost、按超时处理、继续干活。被放弃的线程以后的写入带旧令牌，会被拒。

**怎么证明修好了**：`test_a_job_that_ignores_its_deadline_is_abandoned`：`block` 任务阻塞三秒，断言 worker 不到三秒就放弃，状态 TIMED_OUT，`last_error` 含 `abandoned`。

**面试怎么讲**：协作式超时挡不住不配合的任务（S）。截止时间必须由执行者强制（T）。我把任务放进单独线程，到点就放弃它，并依靠令牌让它迟到的写入失效（A）。测试用一个故意阻塞的任务证明 worker 按时脱身；Python 线程杀不掉，所以要硬隔离得用子进程，这一点写在局限里（R）。

## Q8. 调度器停了四十多分钟，恢复后一口气补了四十三个任务
<!-- id: q08-misfire-flood -->

**来源：典型事故**（单元测试里构造）

**现象**：每分钟一次的 schedule，调度器停了四十二分半。按「错过的都补」的写法，恢复后会一次性插入四十三个任务，和正常流量抢 worker。

**在哪里**：[`leasejobs/schedules.py`](../leasejobs/schedules.py) 的 `@learn:misfire`（`due_slots`）；朴素写法在 [`docs/problems/snippets/misfire_before.py`](problems/snippets/misfire_before.py)。

**怎么定位**：这不是 bug，是缺一条策略。问题是：对这个任务来说，「错过的那四十几次」还有没有意义？

**怎么解决**：每个 schedule 选 `misfire`：`skip`（默认，只补最近一次）或 `catch_up`（每个窗口都补，最多 `max_catchup` 个，保留最近的）。另有唯一索引 `(schedule_id, scheduled_for)`，崩溃重放也不会补出两份。

**怎么证明修好了**：`tests/test_misfire.py`：skip 补 `[m(42)]`；catch_up 上限 5 补 `m(38)..m(42)`。`tests/test_scheduler.py::test_refiring_the_same_slot_creates_no_second_job` 测重放。

**面试怎么讲**：调度器停机后的补跑可能冲垮队列（S）。需要让每类任务自己决定补不补、补多少（T）。我做了 skip 和有上限的 catch_up 两种策略，并用唯一索引保证重放幂等（A）。单元测试覆盖了两种策略和上限，数据库测试覆盖了崩溃重放（R）。

## Q9. 重试测试里，任务一直停在 PENDING
<!-- id: q09-backoff-invisible -->

**来源：真实踩坑**（第 7 章，测试自己的 bug）

**现象**

```
E       AssertionError: job 1 not terminal: PENDING
{"msg":"attempt failed, retry scheduled","attempt":1,"backoff_s":0.924,...}
```

四个重试相关的测试同时这样红。

**在哪里**：[`tests/test_failures.py`](../tests/test_failures.py) 的 `@learn:backoff-driver`（`drive`）；修复前在 [`docs/problems/snippets/backoff_test_before.py`](problems/snippets/backoff_test_before.py)。

**怎么定位**

1. 任务确实从 SCHEDULED 变成了 PENDING → 调度器释放成功了。
2. 但 worker 认领条件是 `run_at <= now()`，`now()` 是数据库的真时间；`run_at` 还在退避后的未来 → worker 看不见。
3. 根因：测试用「未来一小时的假时钟」推调度器，只骗了系统的一半。

**怎么解决**

修复前：`s.tick(now=time.time() + 3600)`

修复后：

```python
conn.execute("UPDATE jobs SET run_at = now() WHERE id = %s AND state = 'SCHEDULED'", (jid,))
s.resync()
s.tick()
```

**怎么证明修好了**：`tests/test_failures.py` 十个测试全绿。这个红灯也顺便证明了认领条件确实在检查 `run_at`：退避中的任务不会被提前偷跑。

**面试怎么讲**：重试测试一直停在待执行（S）。我要让测试跳过退避又不改变系统行为（T）。我发现假时钟只作用在调度器，worker 仍用数据库时间，于是改成直接把任务的运行时间拉到现在（A）。测试转绿，而且这个过程反过来验证了认领条件本身是对的（R）。
