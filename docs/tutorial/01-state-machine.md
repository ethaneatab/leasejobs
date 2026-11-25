# 第 1 章：最小能跑的任务服务，和一张写死的状态图

> 讲解视频：[p1-submit](../videos/p1-submit.mp4)

## 这一章解决什么问题

后台任务（background job）最容易坏在「状态」上。一个任务被标成成功以后，又被改回待执行；一个取消了的任务，又被 worker 拿去跑。
这类 bug 很难在日志里看出来，因为每一次 UPDATE 单独看都「成功」了。

所以第一步不急着做并发、租约、调度。先做三件小事：

1. 一个 REST API，能提交和查询任务；
2. 一个 worker，能把任务从 PENDING 跑到 SUCCEEDED；
3. 一张**显式的状态图**（state machine），不在图上的边，一律拒绝。

## 状态有哪些？

```
SCHEDULED ─▶ PENDING ─▶ LEASED ─▶ RUNNING ─▶ SUCCEEDED
                ▲          │  │       │ ├──▶ FAILED_RETRYABLE ─▶ SCHEDULED
                └──────────┘  │       │ ├──▶ FAILED_TERMINAL
                (租约过期)     │       │ ├──▶ TIMED_OUT
                              ▼       │ └──▶ CANCELLED
                          SCHEDULED   └──▶ PENDING（租约过期）
                        （没拿到限速令牌）
```

这一章只会用到 PENDING → LEASED → RUNNING → SUCCEEDED / FAILED_TERMINAL。
其它边是后面几章的：租约过期回收（第 3 章）、调度（第 4 章）、限速推迟（第 5 章）、重试和超时（第 7 章）。
但是边现在就全部定义好，因为「哪些边合法」是设计，不是实现细节。

## 为什么要拦两次？

假设：只在 Python 里检查就够了。

不够。总会有人在 psql 里手改一行，或者写一个新脚本绕过 `store.transition`。
所以同一张边表存了两份：

- `leasejobs/states.py` 的 `TRANSITIONS`，在发 SQL 之前就拒绝；
- `sql/001_core.sql` 的 `job_transitions` 表加一个 `BEFORE UPDATE` 触发器（trigger），Postgres 自己拒绝。

两份会不会写得不一样？`tests/test_states.py::test_python_and_sql_edge_lists_are_identical` 每次都比一遍。

真跑一次，在 psql 里把一个成功的任务改回 PENDING：

```
CheckViolation illegal job transition SUCCEEDED -> PENDING (job 1)
CONTEXT:  PL/pgSQL function jobs_guard_transition() line 6 at RAISE
```

## 状态怎么改，才不会被别人覆盖？

所有状态变更只走一个函数：`store.transition(conn, job_id, frm, to, ...)`。
它生成的 SQL 永远带着 `WHERE id = %s AND state = %s`。
也就是 compare-and-set：只有任务**还在** frm 的时候才改。
改不到（返回 0 行），说明别人先改了。函数返回 None，交给调用方决定。

同一个事务里，它还往 `job_events` 写一行 from → to。后面的租约时间线、learn 页面，全部从这张表读。

## worker 怎么拿任务？

这一章的 worker 很朴素：先 SELECT 几个候选，再对每个候选做一次 PENDING → LEASED 的 compare-and-set。
两个 worker 撞到同一行，只有一个能改成功，另一个换下一行。
它是对的，但是在多 worker 的时候会白白打架。第 2 章换成 `FOR UPDATE SKIP LOCKED`。

## 跑一次看看

```
$ curl -s -XPOST localhost:8910/jobs -H 'X-API-Key: dev-key-alice' \
    -H 'content-type: application/json' -d '{"type":"report","payload":{"rows":2000,"seed":42}}'
{"id":1,"owner":"alice","type":"report",...,"state":"PENDING","attempts":0,...}
```

一秒后查事件表：

```
submit   None    -> PENDING    api:alice
claim    PENDING -> LEASED     worker-1
start    LEASED  -> RUNNING    worker-1
finish   RUNNING -> SUCCEEDED  worker-1   {"outcome": "succeeded"}
```

worker 的结构化日志（structured log），一行一个 JSON：

```
{"ts":1795635501.735,"level":"info","component":"worker","pid":46349,"msg":"claimed","worker":"worker-1","job_id":1,"type":"report"}
```

## 鉴权和归属

每个请求带 `X-API-Key`。库里只存 key 的 sha256，`api_keys` 表把它映射到 owner。
任务属于提交它的 owner。别人的任务返回 404，而不是 403：不告诉对方「这个 id 存在」。

## 测试

- `tests/test_states.py`：九乘九种 from/to 组合，只有边表里的能过；终态没有出边；触发器真的拒绝。
- `tests/test_api.py`：没 key 是 401；坏 payload、未知类型、多余字段是 422；别人的任务 404；幂等键（idempotency key）返回同一个任务；取消终态任务是 409。
- `tests/test_worker_basic.py`：worker 跑完后，事件表正好是那四行。

## 怎么 checkout 到这一步跑起来

```bash
git checkout step-01
docker-compose -p leasejobs up -d --wait postgres
/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m leasejobs.migrate && .venv/bin/python -m leasejobs.seed
.venv/bin/python -m pytest          # 这一步是 94 passed
.venv/bin/python -m leasejobs.api &          # :8910
.venv/bin/python -m leasejobs.worker --id worker-1 &
```
