# schemafence · Cloud Studio 实操手册

> 目标：在腾讯云 Cloud Studio 的免费机时上，把「数据库 AI Agent」跑起来，
> 并用它演示一次「AI 生成的 SQL 看起来对、其实错」的拦截过程。
>
> 阅读前提：仓库已建好（`github.com/empoGavin/schemafence`）。
> 本手册里所有命令都可以直接复制粘贴。

---

## 0. 先看三条结论，能省你两个小时

**结论一：不要在 Cloud Studio 里用 Docker。**
Cloud Studio 的工作空间本身就是一个容器，里面**没有 systemd**，`systemctl start docker` 会直接报错。
网上教程让你 `sudo dockerd` 后台启动——能跑，但每次重启工作空间都要重来一遍。
所以本仓库的安装脚本走 `apt` 路线，不用 Docker。`docker-compose.yml` 是给本地和别人的机器准备的，不是给你现在用的。

**结论二：不要开 GPU 规格。**
这个项目全程 CPU 就够（没有本地模型）。按机时抵扣因子：

| 规格 | 抵扣因子（机时/小时） | 适合 |
|------|---------------------|------|
| 1 核 2G | 0.1 | 够用，但编译 pgvector 会慢 |
| **2 核 4G** | **0.25** | **本手册推荐：够快，省额度** |
| 4 核 8G | 0.5 | 奢侈但可接受 |
| GPU T4 | 1.2 | ❌ 本项目完全用不到 |
| GPU A10 / L40 | 3.3 / 8 | ❌ 开了就是在烧额度 |

即使是首次绑定赠送的 20 机时，在 2 核 4G 上也能跑 **80 小时**，调试期基本够用。
另外平台通常还有每月免费额度（标准版口径约 1 万分钟），**具体以控制台「我的额度」页面为准，先看一眼再建空间**。

**结论三：这个项目不需要 API key。**
「约束层」是确定性的代码，不是模型。所以离线 demo 零依赖、零费用、断网也能跑。
模型要到接上 Agent 循环时才需要，那时候再配 key 也不迟。

---

## 1. 准备：先在本机确认 demo 能跑（2 分钟，零成本）

还没建空间之前，先确认代码本身是好的。在你本机的 `%USERPROFILE%\schemafence` 目录：

```bash
python demo.py
```

**预期输出**（节选）：

```
schemafence — the constraint layer between LLMs and databases
====================================================================
source : sample_schema.sql
tables : 7      columns : 33
mode   : offline (no database, no model, no API key)
...
  16 finding(s): 5 high / 9 medium / 2 low
...
[guard] seven-layer selftest
  13 cases → all passed
```

看到 `all passed` 就说明代码没问题，可以上云了。
顺便再试一条，看看「自然语言 → 选表 → 生成 SQL → 过护栏」的完整链路：

```bash
python demo.py --ask "最近一周的订单金额合计是多少？"
```

它会给出两个分数相同的候选表 `shop.orders` 和 `shop.orders_archive`
——**这正是本项目要解决的问题的现场演示**：模型选哪个都说得过去，但选错了就是错的，而且不报错。

---

## 2. 创建 Cloud Studio 工作空间（3 分钟）

1. 打开 <https://cloudstudio.net> 或 <https://ide.cloud.tencent.com>，微信登录。
2. 右上角头像 → 看 **资源包 / 我的额度**，确认还剩多少机时。
3. 点 **创建应用 / 新建工作空间**：
   - 方式选 **从 Git 仓库导入**
   - 仓库地址填：`https://github.com/empoGavin/schemafence`
   - 运行环境 / 模板：选 **Ubuntu**（22.04 或 24.04 都可以，脚本两种都兼容）
   - 规格：**2 核 4G**（CPU，不要选 GPU）
4. 创建后点 **编写代码 / 进入工作空间**，等 10 秒左右打开 Web IDE。
5. 打开底部 **终端**，确认环境：

```bash
python3 -V          # 需要 3.10 以上
git log --oneline -1
```

`git log` 能看到 `Add README with project overview and roadmap` 就说明仓库已经拉下来了。

> Cloud Studio 已经把你的登录凭据接进了 git，后面 `git push` 不需要再输密码。

---

## 3. 跑离线 demo（30 秒，验证环境）

```bash
cd ~/schemafence 2>/dev/null || cd "$(find ~ -maxdepth 3 -type d -name schemafence | head -1)"
python3 demo.py
```

看到 16 个 finding + `13 cases → all passed` 就对了。
**到这一步，「5 分钟跑起来」已经兑现了** —— 一个陌生人克隆仓库、跑一条命令，就能看到完整报告。
（README 里承诺的就是这个，不需要数据库、不需要 key、不需要模型。）

---

## 4. 装 PostgreSQL + pgvector（3–5 分钟）

```bash
cd "$(find ~ -maxdepth 3 -type d -name schemafence | head -1)"
bash scripts/setup_pg.sh
```

脚本做五件事，每步都会打印进度：

1. `apt` 安装 PostgreSQL（自动识别版本，22.04 通常是 14，24.04 通常是 16）
2. 装 `postgresql-<版本>-pgvector`；**如果该发行版没有这个包，自动降级为源码编译**（约 1 分钟）
3. 启动集群 —— 用 `pg_ctlcluster` / `service`，**不用 systemctl**（这就是不用 Docker 的原因）
4. 设密码 `pgvec123`，建库 `fence_demo`，导入 `examples/sample_schema.sql`
5. 跑一次 `ANALYZE`，让 `pg_stats` 有数据（NULL 比例检查靠它）

**预期结尾**：

```
==> done
    database : fence_demo   user: postgres   password: pgvec123

    offline demo :  python demo.py
    live demo    :  python demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo
```

> 如果第 2 步两次都失败（包没有 + 编译报错），先跑 `apt-cache search pgvector` 看到底有没有对应版本的包，
> 把输出发我，我按你的实际版本改脚本。

---

## 5. 跑 live 模式（2 分钟，今天最值钱的一步）

先装唯一的依赖：

```bash
pip install -r requirements.txt
```

然后：

```bash
python3 demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo
```

**预期新增输出**：

```
[live] connected to a real database
  tables read from the catalogue : 7
  columns                        : 33

  row estimates (from pg_class.reltuples, no COUNT(*))
    shop.orders              ~5
    shop.users               ~4
    ...

  measured NULL fractions (pg_stats) — the join-loss evidence
    shop.orders.user_id        20.0% NULL
    shop.users.email           25.0% NULL

  same checks, live data → 16 finding(s) (5 high, 9 medium, 2 low)
```

这里有两处刻意的设计：

- **行数用 `pg_class.reltuples` 估算，不用 `COUNT(*)`** —— 体检工具不该把它检查的库压垮，这是 DBA 的习惯。
- **NULL 比例来自 `pg_stats`，是实测数据而不是猜的** —— `shop.orders.user_id` 有 20% 为 NULL，也就是"五笔订单里有一笔，一旦 JOIN 就消失"。

---

## 6. 验证最小向量检索（2 分钟）

```sql
sudo -u postgres psql -d fence_demo -f scripts/verify_pgvector.sql
```

这一步验证四件事：

1. `CREATE EXTENSION vector` 能装上
2. 余弦距离检索 `<=>` 能返回结果（`1 - 距离 = 相似度`）
3. HNSW 索引能建起来（小表上仍走顺序扫描，正常）
4. **只读账号 `agent_ro` 建好，只有 SELECT 权限**

再亲手确认一次"物理上写不进去"：

```bash
psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" \
     -c "DELETE FROM shop.orders WHERE 1=1"
# 预期：ERROR:  permission denied for table orders
```

这个报错来自七层护栏的第一层，也是最硬的一层：拒绝发生在数据库自己那里，不是代码里的一句判断。想留证据就截个图。

---

## 7. 提交代码（1 分钟）

```bash
git add -A
git commit -m "Add schema audit demo, seven-layer guard and Cloud Studio setup script"
git push
```

回到 GitHub 仓库页面，确认 `demo.py`、`schemafence/`、`scripts/`、`docs/` 都上去了。
顺手把 About 栏的 topics 填上（**`data-migration` 这个标签别漏**）：

```
llm  text-to-sql  nl2sql  postgresql  guardrails  ai-agent  pgvector  data-migration
```

**最后：回控制台停止工作空间。** 不停止会继续消耗额度。

---

## 8. 搭建验收清单

逐条打勾：

- [ ] 离线 demo 跑通（截图：16 findings + 13 cases passed）
- [ ] `SELECT version();` 有输出（证明集群起来了）
- [ ] live 模式输出 NULL 比例（截图：`shop.orders.user_id 20.0% NULL`）
- [ ] pgvector 相似度查询有结果
- [ ] `agent_ro` 删除数据被拒绝（截图那个 permission denied）
- [ ] 一页原理笔记：embedding 在干什么 / 余弦距离为什么比欧氏距离适合文本 / RAG 三段式 / 什么时候该微调而不是 RAG
- [ ] 代码已 push

---

## 9. 常见问题排查

| 现象 | 原因 | 处理 |
|------|------|------|
| `systemctl: command not found` | 容器里没有 systemd | 正常。用 `pg_ctlcluster 16 main start` 或 `sudo service postgresql start` |
| `psql: FATAL: role "root" does not exist` | 直接用当前用户连了库 | 必须 `sudo -u postgres psql` |
| `could not change directory to "/root"` | postgres 用户读不了当前目录 | 无害警告，忽略；或在 `/tmp` 下执行 |
| `apt install postgresql-XX-pgvector` 找不到 | 发行版版本太老 | 脚本会自动源码编译；也可先 `apt-cache search pgvector` |
| live 模式连不上库 | 密码没设 / 集群没起来 | `sudo pg_lsclusters` 看状态，`sudo -u postgres psql -c "SELECT 1"` 测连通 |
| `pg_stats` 查出来是空的 | 没做过 ANALYZE | `sudo -u postgres psql -d fence_demo -c "ANALYZE;"` |
| `vector` 类型不存在 | 扩展没建在 `fence_demo` 库上 | `sudo -u postgres psql -d fence_demo -c "CREATE EXTENSION vector;"` |
| 工作空间重启后数据库没了 | 容器文件系统不保证持久化 | 重跑 `bash scripts/setup_pg.sh` 即可（脚本是幂等的） |

---

## 10. 后面还能往上加什么

| 接下来 | 加什么 | 具体动作 |
|-----|-------------------|---------|
| 真实嵌入 | 把 3 维换成 1024 维 | 新建 `doc_chunks` 表（计划 2.3 节），用 `pgvector` 存 schema 快照和脱敏文档；对比 256/512/1024 三种切片粒度的 Top-5 命中率 |
| 工具调用 | 接真模型 | `--llm openai` 分支：`search_docs` / `run_sql` / `explain_sql` / `get_table_stats` 四个工具 + 主循环；把每一步打印出来 |
| 护栏上真实路径 | live 模式改用 `agent_ro` 连接 | 加失败自纠；跑几个刁钻问题（"帮我删掉这张表"必须被拒） |
| 收尾 | 开源 | 更新 README 的 Quick Start 与效果数据 |

**小结**：这一节搭好的是「约束层」——它不需要模型就能工作，这是工程上的分界点，也是后面所有功能的地基。

---

## 附：本地（Windows）怎么跑

离线 demo 在你本机就能跑，用来改代码最快：

```bash
cd %USERPROFILE%\schemafence
python demo.py
python demo.py --ask "哪个表存了退款信息？"
```

在 Windows 上跑 live 模式需要本机有 PostgreSQL，不建议为了这个装——**数据库放到 Cloud Studio 上跑，本地只改代码**。
