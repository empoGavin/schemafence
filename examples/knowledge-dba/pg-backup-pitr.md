> 合成语料：虚构业务场景 + 公开 PostgreSQL 通用知识，不含任何公司内部信息。

# 备份与 PITR：你到底能恢复到几点几分

## 三种备份形态

- **逻辑备份** `pg_dump` / `pg_dumpall`：灵活、可跨版本，但恢复慢，适合小库和部分表
- **物理全量** `pg_basebackup`：整个数据目录，恢复快，是 PITR 的基础
- **连续归档 WAL**：让恢复点从"昨天凌晨"精确到"某秒"

只有物理全量 + WAL 归档的组合才叫 PITR，其余都只能恢复到备份时刻。

## 归档配置与验证

```sql
ALTER SYSTEM SET archive_mode = on;
ALTER SYSTEM SET archive_command = 'test ! -f /archive/%f && cp %p /archive/%f';
ALTER SYSTEM SET archive_timeout = '60s';
```

归档开启后必须验证，不能只看配置：

```sql
SELECT archived_count, failed_count, last_archived_time, last_failed_time
  FROM pg_stat_archiver;
```

`failed_count` 持续增长就意味着恢复链已经断了，这在故障当天才会被发现就太晚了。

## RPO 与 RTO 怎么定

- **RPO**（能容忍丢多少数据）决定归档频率。`archive_timeout = 60s` 意味着最坏丢 1 分钟
- **RTO**（多久能恢复）必须靠演练实测，不能靠估算。备份恢复速度受磁盘、解压、
  WAL 重放速度共同影响，通常比预期慢一截

## 恢复流程要点

1. 还原基础备份到目标目录
2. 建 `recovery.signal`（PG12+），配置 `restore_command`
3. 指定 `recovery_target_time` 与 `recovery_target_action`
4. 恢复完成后**先以只读方式校验**数据，再切流量
5. 记录实际恢复耗时，写回演练报告

## 备份最容易骗人的地方

- 备份任务成功 ≠ 备份可恢复。命令返回 0 只说明文件写完了
- 未演练的备份等于没有备份。建议把恢复演练写进季度例行任务
- 恢复演练要覆盖"恢复到某个时间点"，只验证全量还原是不够的
- 归档目录和备份要异地或至少异机存放，同盘同机等于零份备份
