> 合成语料：虚构业务场景 + 公开 PostgreSQL 通用知识，不含任何公司内部信息。

# 最小权限模型：角色分层、默认权限与定期复核

## 三类角色，各管一件事

- **登录角色**：能连库的身份，对应一个人或一个应用实例
- **组角色**：只承载权限，不用于登录，权限通过它下发
- **对象属主**：表、视图、函数的属主，决定谁能对它们授权

推荐做法是"登录角色只属于组角色，权限一律挂组角色"，
这样人员变动时只需要调整成员关系，不用改一堆对象授权。

## 应用账号该给到什么程度

- 只给业务 schema 的 `SELECT/INSERT/UPDATE/DELETE`，不给 `DDL`
- 不给 `SUPERUSER`，不给 `CREATEDB`、`CREATEROLE`
- 连接串加密存储，禁止出现在代码仓库和日志里
- 报表类只读账号单独建，绑定只读副本

DBA 日常操作用独立的管理账号，不要复用应用账号，
否则审计日志里分不清"是人做的还是程序做的"。

## 默认权限：让新建表自动可访问

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA biz
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
```

不设默认权限的话，每次上线新建表都会遇到"应用报权限不足"，
然后临时手工 GRANT，久而久之权限就乱了。注意默认权限是**按属主**生效的，
建表角色变了规则就不生效，这个坑在多人协作环境里很常见。

## search_path 与 public schema

PG15 起 `public` schema 不再默认对所有人开放 `CREATE`，
这是安全改进，但老脚本可能因此报错，升级时要检查。

自定义 `search_path` 时顺序很重要，把业务 schema 放在最前；
不要依赖默认的 `"$user", public`，尤其在有临时对象或函数重载的场景下。

## 审计与定期复核

```sql
-- 谁有超级用户权限
SELECT rolname FROM pg_roles WHERE rolsuper;

-- 过期或长期未登录的账号
SELECT rolname, rolcanlogin, rolvaliduntil FROM pg_roles WHERE rolcanlogin;
```

复核建议按季度做，产出两张表：**账号清单**（负责人、用途、有效期）和
**权限矩阵**（角色 × schema × 权限类型）。发现异常权限时，
先记录再回收，不要直接删角色——影响面要先评估。
