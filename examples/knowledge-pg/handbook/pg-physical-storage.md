> 语料来源：公开 PostgreSQL 官方知识整理（PostgreSQL 16）+ 虚构案例，不含任何公司内部信息。

# PostgreSQL 物理存储：8KB 数据页、TOAST、表空间与段文件

## 先把粒度说清：表不是文件，页才是单位

数据目录 `PGDATA` 下，每个库有一个以数据库 OID 命名的子目录（`base/` 下），每张表和每个索引各占一个独立文件。这个文件的名字通常等于表或索引的 filenode，记录在 `pg_class.relfilenode` 里。注意：**filenode 不保证等于 OID**，TRUNCATE、REINDEX、CLUSTER 和某些 ALTER TABLE 都会换掉 filenode 而保留 OID，所以不要假设两者相等。系统目录里 `pg_class.relfilenode` 甚至可能是 0，真正的值要用 `pg_relation_filenode()` 取。

除了主文件（main fork），每个表和索引还有一个空闲空间映射（FSM），文件名是 filenode 加 `_fsm`；表还额外有一份可见性映射（VM），后缀 `_vm`；unlogged 表及其索引还有第三份初始化分叉，后缀 `_init`。

## 8KB 数据页里有什么：五段结构

PostgreSQL 的页大小默认为 8KB，表里的行不允许跨页存放。一页由五部分组成：

- 页头（PageHeaderData），固定 24 字节。里面记录 `pd_lsn`（本页最后一次改动的 WAL 位置）、`pd_checksum`（启用数据校验和时使用）、`pd_flags`、`pd_lower`（空闲空间起点）、`pd_upper`（空闲空间终点）、`pd_special`（特殊空间起点）、`pd_pagesize_version`，以及 `pd_prune_xid`（页上最老的未清理 XMAX）。
- 行指针数组（ItemIdData），每个 4 字节，存放"偏移 + 长度"。
- 空闲空间，位于行指针与行数据之间。
- 行数据本身，从页尾向前分配。
- 特殊空间，普通表为空，索引用来存 B-tree 的左右兄弟指针一类信息。

一个实务含义藏在行指针里：行指针一旦分配就不会因为行被搬动而改变，所以它（即 CTID，页号 + 指针下标）可以在很长时间里稳定地指向一个"槽位"。这也是 VACUUM 能在页内压缩空闲空间而不破坏引用的原因。

每行的行头在多数平台上是 23 字节，后面跟可选的 NULL 位图、可选的对象 ID 和用户数据。

```sql
SELECT ctid, xmin, xmax FROM orders_2024 LIMIT 3;
```

`ctid` 就是这个"页号 + 槽位下标"，它是物理地址，不保证长期不变。

## TOAST：超长字段怎么塞进 8KB 的页

既然行不能跨页，超大的字段就直接放不下。PostgreSQL 的解法是 TOAST（The Oversized-Attribute Storage Technique）：把大字段压缩、切块，放到一张附属的 TOAST 表里，主表只留一个指针。表的 `pg_class.reltoastrelid` 指向它的 TOAST 表。

触发点是一个阈值：**只有一整行宽于 `TOAST_TUPLE_THRESHOLD`（约 2KB）时，TOAST 机制才介入**，然后把字段压缩或外置，直到行宽低于 `TOAST_TUPLE_TARGET`（默认也约 2KB）或再也压不动为止。外置的数据按不超过 `TOAST_MAX_CHUNK_SIZE`（约 2000 字节，正好让 4 块放进一页）切块，每块作为 TOAST 表的一行，列是 `chunk_id`、`chunk_seq`、`chunk_data`。

TOAST 策略有四种，可以用 `ALTER TABLE ... SET STORAGE` 按列指定：

- `PLAIN`：不压缩也不外置，只适合不可 TOAST 的类型。
- `EXTENDED`：先压缩、不够再外置，这是大多数可 TOAST 类型的默认策略。
- `EXTERNAL`：只外置、不压缩。
- `MAIN`：只压缩、不外置（只有实在放不下时才外置，属于兜底）。

压缩算法由列的 `COMPRESSION` 选项决定，未显式指定时看参数 `default_toast_compression`，默认是 `pglz`（编译时带 LZ4 时可选 `lz4`）。

这里有一个值得记住的取舍：**如果业务经常对大文本或 bytea 做子串截取，`EXTERNAL` 反而更快**，因为不压缩时，取子串只需要读取需要的分块，不必先解压整段；代价是占用更多存储。判断依据是你的访问模式——是"整段读"，还是"经常只取中间一段"。

## 表空间：换个目录放热数据

表空间的作用是把某些表/索引放到指定的物理目录（例如另一块盘或另一种文件系统）。实现方式是：`PGDATA/pg_tblspc` 下有一个以表空间 OID 命名的符号链接，指向 `CREATE TABLESPACE` 里指定的真实目录；真实目录里再按版本号分子目录，然后按数据库分。

```sql
SELECT spcname, pg_tablespace_location(oid) FROM pg_tablespace;
```

要注意的是，表空间本身不改变数据组织方式，它只是把文件放到别的地方，所以它带来的是 IO 隔离，而不是性能魔法。

## 1GB 段文件与 fsync / full_page_writes

当一张表或索引超过 1GB 时，会被切成 1GB 的段文件：第一段沿用 filenode，后续段叫 `filenode.1`、`filenode.2`……这样做的目的是绕开某些文件系统对单文件大小的限制。1GB 只是编译时的默认段大小。

可靠性方面有两个参数值得记住默认值：`fsync` 默认 `on`，保证提交过的改动真正落盘；`full_page_writes` 默认 `on`，在 checkpoint 之后对某页的第一次修改会把整页内容写进 WAL，用来对抗崩溃时"只写了一半"的页面。

关掉它们确实能提速，但代价是崩溃后可能得到无法恢复的损坏。官方给的可接受场景很窄：全新装载的库、跑完就丢弃的批处理库、可以随时重建的只读克隆。**"硬件质量好"不构成关闭 fsync 的理由**。

## 落到可观察：看大小、看分叉、看 TOAST 表

```sql
SELECT relname, relfilenode, reltoastrelid
  FROM pg_class WHERE relname = 'orders_2024';

SELECT pg_size_pretty(pg_relation_size('orders_2024'))     AS main,
       pg_size_pretty(pg_table_size('orders_2024'))        AS with_toast_and_fsm,
       pg_size_pretty(pg_total_relation_size('orders_2024')) AS with_indexes;
```

`pg_relation_size` 只算主文件；`pg_table_size` 含 TOAST、FSM 和 VM 分叉，但不含索引；`pg_total_relation_size` 再叠加所有索引。

## 核心判断：大字段的"慢"往往先怀疑 TOAST 的存储策略

遇到大文本表查询慢，先别急着加索引。常见根因是存储策略把可优化空间浪费掉了：字符类型默认走 `EXTENDED`（先压缩），于是每次取子串都要先解压整段；如果业务是"整段写入、频繁截取"，把它改成 `EXTERNAL` 常常立竿见影。判断顺序应该是：**先看 `reltoastrelid` 是否存在、这张表是不是真的在走 TOAST，再看列策略，最后才轮到索引和 SQL**。
