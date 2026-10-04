# 表膨胀（table bloat）的成因与治理

## 一句话结论

膨胀不是"数据变多了"，而是"空间回收不掉了"——死元组还占着页面，新数据只能继续往后写。

## 成因

PostgreSQL 的 MVCC 不原地更新：一条记录被 UPDATE 或 DELETE 之后，旧版本变成死元组（dead tuple），
只有 VACUUM 才能把它标记为可复用。以下几种情况会让死元组的产生速度长期高于回收速度：

- 长事务或未提交事务持有旧快照，VACUUM 无法清理比它更新的死元组
- 大批量 UPDATE / DELETE 之后没有触发 autovacuum（表太大、阈值没到）
- autovacuum 被长事务反复打断，永远跑不完一轮
- 频繁的 HOT-breaking 更新：更新了被索引的列，索引膨胀比表膨胀更严重

## 怎么判断要不要治理

看三个数，不要凭感觉：

1. `pg_stat_user_tables.n_dead_tup` 与 `n_live_tup` 的比例，超过 20% 就值得关注
2. `pg_class.reltuples` 与真实 `count(*)` 的偏离程度（估算严重失真说明统计信息也旧了）
3. 表大小与"理论上应该占用的空间"的比值：`pg_total_relation_size` 对照
   `pgstattuple` 插件给出的 `dead_tuple_percent`

## 治理手段

- **VACUUM（普通）**：在线，只回收可复用空间，不归还操作系统。首选。
- **VACUUM FULL**：整表重写，会持有 ACCESS EXCLUSIVE 锁，期间该表**完全不可读写**。
  大表上执行等于一次计划内停机，必须先确认窗口期。
- **pg_repack**：在线重建，代价是磁盘空间要翻倍，且需要主键或唯一键。
- **分区表**：把"删旧数据"从 DELETE 改成 DROP PARTITION，成本从"产生死元组"变成"元数据操作"，
  这是数据生命周期管理里最有效的一招。

## 常见误区

- 只调 `autovacuum_vacuum_scale_factor` 而不看长事务，属于治标
- 认为"每天凌晨 VACUUM FULL 一次"是好习惯——对大表这是每天一次停机
- 忽略索引膨胀：表 3GB、索引 12GB 的情况在真实系统里很常见
