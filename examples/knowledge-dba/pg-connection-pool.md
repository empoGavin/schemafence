> 合成语料：虚构业务场景 + 公开 PostgreSQL 通用知识，不含任何公司内部信息。

# 连接池与连接数：为什么 max_connections 不是越大越好

## 一个连接到底占多少资源

PG 是进程模型，每个连接一个后端进程。基础开销通常 5–10MB，
再加上该连接可能用到的 `work_mem`、临时文件、以及它持有的锁和快照。

粗算一下：1000 个连接，每个平均 8MB 基础内存，再叠加排序哈希用的 work_mem，
轻松吃掉十几 GB。而且连接越多，锁竞争、快照清理压力、上下文切换都上升，
**吞吐反而下降**。所以调大 max_connections 通常是掩盖问题，不是解决问题。

## 池化放在哪一层

- 应用侧连接池（HikariCP、连接池中间件）：每个实例一个池，最常用
- pgbouncer / pgpool：独立进程，支持 session / transaction / statement 三种池模式
- 数据库侧：只保留少量真实连接

transaction 池模式最常用，但有几类功能会失效，必须提前知道：
会话级 `SET`、prepared statement（需配置 `max_prepared_statements`）、
advisory lock、`LISTEN/NOTIFY`、游标跨事务。

## 池大小怎么算

不要按 QPS 算，按**并发度**算。经验起点是「CPU 核数 × 2 + 有效磁盘数」，
再根据实测响应时间微调。让请求在池里排一小会儿队，比让它进数据库制造并发更健康。

## 连接被打满时的排查顺序

```sql
SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY 2 DESC;

SELECT application_name, state, count(*) FROM pg_stat_activity
 GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20;

SELECT pid, now() - state_change AS idle_for, query
  FROM pg_stat_activity
 WHERE state = 'idle in transaction'
 ORDER BY state_change LIMIT 10;
```

重点看三个信号：`idle in transaction` 有多少、哪个 application_name 占了大头、
是不是有长事务把连接占住不还。

## 一个合成案例：促销开始后的连接风暴

某零售订单系统（虚构）在大促开始 30 分钟内连接数从 200 涨到 800，应用报
`too many clients already`。定位过程：

1. 按 application_name 分组统计，发现全部来自订单服务的 4 个实例
2. 应用侧池配置是每实例 50，但这次扩容到 16 个实例，池总量从 200 变成 800
3. 同时有一条查询因为缺索引变慢，把连接持有时间拉长，排队迅速堆积

处置：池上限改为按总量分配（每实例 15）、对慢查询补索引、
加 `idle_in_transaction_session_timeout` 兜底，并把这个参数写进服务模板。

## 结论

连接数的正确姿势是：**数据库端只放必需的连接数，池化尽量靠近应用，
把并发瓶颈暴露在池的队列上，而不是让它打死数据库。**
