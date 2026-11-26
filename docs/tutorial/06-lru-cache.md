# 第 6 章：结果缓存：自己写一个 O(1) 的 LRU

> 讲解视频：[p7-cache](../videos/p7-cache.mp4)

## 这一章解决什么问题

任务跑完以后，调用方会反复来查结果。轮询一个报表任务，一分钟可能查几十次。每次都打到 Postgres，是在用最贵的资源回答最便宜的问题。

结果有一个很好的性质：**SUCCEEDED 是终态，结果以后不会再变**。所以缓存它不需要失效逻辑，只需要一个容量上限，满了就扔掉最久没用的那个。这就是 LRU（least recently used）。

## 为什么不用 OrderedDict？

`functools.lru_cache` 和 `OrderedDict.move_to_end` 都能做。但它们把最值得看的那部分藏起来了。
`leasejobs/cache.py` 用最原始的两样东西拼出来：

- 一个字典：key → 链表节点。查找 O(1)；
- 一个双向链表：节点按最近使用排好，头部最新，尾部最旧。

两个哨兵节点（sentinel）`_head` 和 `_tail` 永远在两端。这样插入和摘除都不用判断「前面是不是空」。

## get 和 put 各做了什么？

`get(key)`：查字典。没有就记一次 miss。有就把节点从原位置摘下来（`_unlink`），挂到头部（`_push_front`），记一次 hit。

`put(key, value)`：已有的 key，改值，挪到头部。新 key，挂到头部；如果超过容量，摘掉 `_tail.prev`，也就是最旧的那个，再从字典里删掉。

每一步都是固定几次指针操作，跟缓存里有多少东西无关。

## 怎么证明它是 O(1)？

不靠嘴说，测一下。`tests/test_lru.py::test_cost_per_operation_does_not_grow_with_size` 在同一台机器上，分别对一千个和十万个条目的缓存做六万次 get 加 put，各跑五次取中位数：

```
LRU get+put: 724 ns/op at 1e3 entries, 770 ns/op at 1e5 entries
```

条目多了一百倍，每次操作基本不变。断言用的是**相对**比较：大的不超过小的三倍。不写死某个纳秒数，因为那只在这台机器上成立。

## 怎么证明它是对的？

一个参照物：OrderedDict 版本的 LRU，几行代码，大家都信它。
hypothesis 的状态机测试（`RuleBasedStateMachine`）随机生成 put、get、delete 的序列，同一串操作同时做在两边。每一步之后检查：get 返回一样；从新到旧的 key 顺序一样。
跑两百个例子，每个六十步。

## 缓存什么时候被填进去？

两条路：

1. **读穿**（read-through）：`GET /jobs/{id}/result` 先查缓存；没有就查库，是 SUCCEEDED 才放进缓存，响应头 `X-Cache: MISS`；下一次就是 `HIT`。
2. **预热**（warm-up）：`sql/006_result_notify.sql` 的触发器在任务变成 SUCCEEDED 时 `pg_notify('leasejobs_succeeded', id)`。API 进程里有一个线程 LISTEN 这个频道，收到就把结果放进缓存。第一次来查的人也直接命中。

缓存命中也要检查归属：缓存里存着 owner，不是你的照样 404。`test_cached_result_is_still_owner_checked` 专门测这一条。

## 一个真踩到的坑：预热线程卡死，什么报错都没有

第一版预热线程是这样写的：在 `for n in conn.notifies(...)` 的循环体里，直接用同一个连接查结果。
测试 `test_success_notify_warms_the_cache` 等五秒超时：

```
E       AssertionError: timed out after 5s waiting for NOTIFY to warm the cache
```

日志里一个错误都没有。

怎么定位的？

- 假设一：触发器没装上。查 `pg_trigger`，两个触发器都在。排除。
- 假设二：通知没发出来。写一个小脚本，另开一个连接 LISTEN，手动把一行改成 SUCCEEDED，立刻收到了 `['2']`。排除。
- 剩下的只有预热线程自己。`notifies()` 是一个生成器，迭代期间它一直拿着连接的锁；循环体里再用同一个连接 `execute`，要拿同一把锁。线程自己等自己，永远等下去。

改法：先把这一批通知的 id 收进列表，生成器结束、锁放掉以后，再查库。

还有第二个小问题：测试在预热线程刚启动、还没执行 LISTEN 的时候就把任务跑完了，通知发给了空气。所以加了一个 `cache_listening` 事件，测试先等它。

## 怎么 checkout 到这一步跑起来

```bash
git checkout step-06
.venv/bin/python -m leasejobs.migrate          # 006_result_notify.sql
.venv/bin/python -m pytest -s tests/test_lru.py tests/test_result_cache.py
curl -si localhost:8910/jobs/1/result -H 'X-API-Key: dev-key-alice' | grep -i x-cache
```
