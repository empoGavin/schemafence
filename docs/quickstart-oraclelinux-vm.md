# 本机 VMware 虚拟机部署指南 · Oracle Linux 10 版

> **目标**：在已经建好的 Oracle Linux 10 虚拟机上，装好 **PostgreSQL 16 + pgvector**，把 **schemafence** 跑起来（离线模式 + 连真实数据库的 live 模式），并逐条验证通过。
>
> **不需要任何 PostgreSQL 或 AI 基础。** 所有命令可直接复制粘贴。
>
> 预计耗时：**30–45 分钟**（虚拟机已就绪，省掉了下载 ISO 和装系统那一小时）。
>
> 上一版是按 Ubuntu 24.04 写的（`docs/quickstart-vmware.md`，已删除）。如果你看过那一版，**务必先读第 10 节的差异对照表** —— Oracle Linux 有 4 处和 Ubuntu 不一样，其中 1 处不改就必然失败。

---

## 30 秒速览（想直接照做的话）

```bash
# [VM] 四条命令从零到跑通
sudo dnf -y install git python3-pip
git clone https://github.com/empoGavin/schemafence.git ~/schemafence
cd ~/schemafence && bash scripts/setup_pg.sh
python3 demo.py

# [VM] 再三条，跑通连真实数据库的模式
cd ~/schemafence && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo
```

下面每一节都会解释这些命令在做什么、你应该看到什么、出错了去哪看。

---

## 目录

| 节 | 内容 | 预计耗时 |
|---|---|---|
| 0 | 三个名词，一句话讲清 | 读 2 分钟 |
| 1 | 确认虚拟机现状（8 项体检） | 5 分钟 |
| 2 | 从 Windows 连进虚拟机 | 5 分钟 |
| 3 | 系统准备（dnf） | 5 分钟 |
| 4 | 把代码放进虚拟机 | 2 分钟 |
| 5 | **装 PostgreSQL + pgvector** | 5 分钟 |
| 6 | 运行①：离线模式 | 3 分钟 |
| 7 | 运行②：连真实数据库 | 5 分钟 |
| 8 | **全流程验证清单（16 条）** | 10 分钟 |
| 9 | 拍快照 + 日常使用 | 5 分钟 |
| 10 | **Ubuntu 版 vs Oracle Linux 版差异对照** | 读 3 分钟 |
| 11 | 故障排查速查表 | 备用 |
| 附录 A | 资源调整 | — |
| 附录 B | 想用更新的 PG / pgvector（PGDG 路径） | — |
| 附录 C | 改密码、重建示例库 | — |

---

## 0. 三个名词，一句话讲清

- **虚拟机**：用 VMware 在你 Windows 里"虚拟"出来的一台完整电脑。**你已经建好了**，装的是 Oracle Linux 10。装崩了删掉重来，不影响你的 Windows。
- **PostgreSQL（简称 PG）**：一个免费的开源数据库。schemafence 需要一台真实数据库，来演示"读系统目录、读统计信息"这些真实场景。
- **pgvector**：PG 的一个**插件**。装上它，PG 就多了一种"向量"类型，能存也能查 AI 里的语义向量 —— 一台数据库同时干关系库和向量库两件事。

**命令约定**：

- 标着 `[VM]` 的命令 → 在**虚拟机里**执行
- 标着 `[Win]` 的命令 → 在 **Windows 上**执行（PowerShell 或 Git Bash）

虚拟机里敲命令的地方叫**终端**。如果是图形界面，左上角 Activities → 搜 Terminal；如果是纯命令行安装，你登录后看到的那行提示符就是终端。**粘贴用鼠标右键或 Ctrl+Shift+V**（Ctrl+V 在 Linux 终端里通常无效）。

---

## 1. 确认虚拟机现状（8 项体检）

先把虚拟机的实际情况摸清楚。**在虚拟机里**依次执行这 8 条，把结果记下来。

```bash
# ① 发行版和版本
cat /etc/os-release | head -3

# ② 当前用户是谁、是不是 root
whoami; id -u

# ③ 能不能用 sudo
sudo -n true 2>/dev/null && echo "sudo 可用（免密）" || echo "sudo 需要密码，或当前不是 sudo 用户"

# ④ 虚拟机的 IP
ip -4 addr show | grep -w inet

# ⑤ SSH 服务是否在跑
systemctl is-active sshd 2>/dev/null || echo "sshd 未运行"

# ⑥ 能否连外网（dnf 源是否可用）
sudo dnf repolist enabled 2>/dev/null | head -10

# ⑦ 自带 Python 版本
python3 --version

# ⑧ SELinux 和防火墙状态
getenforce 2>/dev/null
sudo firewall-cmd --state 2>/dev/null
```

**逐条对照**：

| # | 应该看到 | 不对怎么办 |
|---|---|---|
| ① | `NAME="Oracle Linux Server"` + `VERSION_ID="10.x"` | 如果显示别的系统，本文档的包管理命令需要相应调整 |
| ② | 你自己的用户名（不是 `root`，`id -u` 不是 `0`），或就是 root | 两种都能继续，脚本两种情况都处理了 |
| ③ | `sudo 可用` | 如果提示没装 sudo：以 root 执行 `dnf -y install sudo`，再 `usermod -aG wheel <你的用户名>` |
| ④ | 形如 `inet 192.168.x.x/24` | 没 IP 说明网卡没起来，见 11.1 |
| ⑤ | `active` | 未运行 → `sudo dnf -y install openssh-server && sudo systemctl enable --now sshd` |
| ⑥ | 列出 `ol10_baseos_latest` / `ol10_appstream` 等 | 报错见 11.2 |
| ⑦ | `Python 3.12.x` | 更低版本也不影响离线模式（要求 3.10+） |
| ⑧ | `Enforcing` + `running` | 保持默认即可，不用关。脚本已处理 SELinux |

> **关于 SELinux**：Oracle Linux 默认 `Enforcing`（强制模式）。**不要为了图省事关掉它** —— 本文档的脚本已经处理了唯一会被它影响的地方（源码编译 pgvector 后的文件标签）。关掉它反而会让你养成坏习惯，而且以后在客户现场遇到 enforcing 的系统又要重新踩一遍。

### 1.1 体检结论表

| 项目 | 你的值 |
|---|---|
| 操作系统 | Oracle Linux 10.x |
| 用户名 | ________ |
| 虚拟机 IP | ________ |
| sudo | 可用 / 需要密码 / 用的是 root |
| SELinux | Enforcing |
| firewalld | running |

---

## 2. 从 Windows 连进虚拟机

### 2.1 确认 IP `[VM]`

```bash
hostname -I
```

输出形如 `192.168.181.128`（可能带空格分隔的多个地址，取第一个）。

### 2.2 从 Windows 连接 `[Win]`

打开 **Git Bash** 或 **PowerShell**：

```
ssh <你的用户名>@192.168.181.128
```

第一次连接会问：

```
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

输入 `yes` → 回车 → 输入密码 → 回车。

> **在 VMware 的 NAT 网络模式下，Windows 可以直接访问虚拟机的 IP**，不需要配端口转发。这是 VMware NAT 的特性（宿主机和虚拟机在同一个虚拟网段里）。

### 2.3 传文件（可选）

用你已经装好的 **WinSCP**：新建站点 → 协议 **SFTP** → 主机填虚拟机 IP → 端口 22 → 用户名/密码 → 登录。

### 2.4 连不上怎么办

见 **11.1**。实在连不上也没关系 —— **直接在 VMware 窗口里操作，效果完全一样**，只是粘贴方式变成鼠标右键。

---

## 3. 系统准备

> 以下命令**全部在虚拟机里执行** `[VM]`，可以整段复制粘贴。`sudo` 会要密码。

### 3.1 更新系统

```bash
sudo dnf -y upgrade
```

**Oracle Linux 不需要注册订阅**（这点和 RHEL 不同）—— 它的 `dnf` 源默认指向 Oracle 的公共仓库，装上就能用。耗时 2–8 分钟，取决于网速。

### 3.2 装基础工具

```bash
sudo dnf -y install git python3-pip python3-devel gcc make vim-enhanced unzip curl
```

| 包 | 用途 |
|---|---|
| `git` | 从 GitHub 克隆代码 |
| `python3-pip` | 装 Python 包；也提供 `venv` 所需的 ensurepip |
| `python3-devel` `gcc` `make` | 编译器。**必装**：pgvector 默认走源码编译（为了拿到最新版 0.8.7） |
| `vim-enhanced` | 编辑配置文件（不习惯就用 `nano`，也在系统里） |
| `unzip` `curl` | 解压、下载、测试接口 |

### 3.3 设时区为中国

```bash
sudo timedatectl set-timezone Asia/Shanghai
date
```

`date` 应输出类似 `Sat Oct  3 14:20:00 CST 2026`，带 `CST` 就对了。

### 3.4 验证

```bash
python3 --version && git --version && echo "基础环境就绪"
```

预期：

```
Python 3.12.9
git version 2.47.1
基础环境就绪
```

（版本号可能不同，Python 只要是 3.12.x 即可。）

---

## 4. 把代码放进虚拟机

**PG 的安装脚本就在代码仓库里，所以先拿代码，再执行脚本。**

### 路线 A（推荐）：从 GitHub 克隆

先在 Windows 上确认已经推送 `[Win]`：

```bash
cd C:\Users\75261\schemafence
git status
```

| 看到什么 | 怎么做 |
|---|---|
| `Your branch is up to date with 'origin/main'` | 已推送 ✅ 继续 |
| `Your branch is ahead of 'origin/main' by N commits` | 先 `git push`，按提示登录 GitHub |
| `?? docs/...` 有未跟踪文件 | 先 `git add . && git commit -m "docs"`，再 `git push` |

然后 `[VM]`：

```bash
cd ~
git clone https://github.com/empoGavin/schemafence.git
cd schemafence && ls
```

预期看到：

```
LICENSE  README.md  demo.py  docker-compose.yml  docs  examples  requirements.txt  schemafence  scripts
```

> 公有仓库克隆**不需要**账号密码。如果提示要密码，说明仓库还是私有的 → 去 GitHub 仓库 Settings → 最下方 Danger Zone → Change visibility → 改成 Public。

### 路线 B：不走 GitHub，直接从 Windows 传

用 WinSCP（见 2.3）把整个 `C:\Users\75261\schemafence` 文件夹拖到虚拟机的 `/home/<你的用户名>/` 下。

> 传之前建议在 Windows 上把 `.git` 文件夹临时改名为 `.git_bak`，传完再改回来 —— 跨系统拷贝 `.git` 偶尔会因权限报错。

### 4.1 验证代码完整 `[VM]`

```bash
cd ~/schemafence && python3 demo.py | grep -E "finding|cases"
```

预期：

```
  16 finding(s): 5 high / 9 medium / 2 low
  13 cases → all passed
```

看到这两行说明代码是好的。（这一步还**不需要数据库**。）

---

## 5. 装 PostgreSQL + pgvector

### 5.1 执行安装脚本 `[VM]`

```bash
cd ~/schemafence
bash scripts/setup_pg.sh
```

这个脚本会自动判断你用的是 `apt` 还是 `dnf`，然后做完 9 件事。**一次性跑完，中途不需要你干预。**

最后看到这段就是全部成功：

```
==> done
    postgres : 16   pgvector: 0.8.7   shop tables: 7
    database : fence_demo   user: postgres   password: pgvec123

    offline demo :  python3 demo.py
    live demo    :  python3 demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo
```

> **`shop tables: 7` 和 `pgvector: 0.8.7` 这两个值必须出现。** 如果 `pgvector` 显示的是别的、或者整行没出来，见 11.4。
>
> **脚本是幂等的** —— 中途失败的话，修完问题直接再跑一遍 `bash scripts/setup_pg.sh` 即可，不会有副作用。

### 5.2 脚本到底做了什么（Oracle Linux 特有步骤已标注）

| # | 步骤 | RHEL 系实际执行的命令 | 作用 |
|---|---|---|---|
| 1 | 判断包管理器 | `command -v dnf` | 选对安装方式 |
| 2 | 装 PG | `dnf -y install postgresql-server postgresql-contrib` | Oracle Linux 10 装的是 **PostgreSQL 16**（AppStream 自带） |
| 3 | 装 pgvector | 自动装编译工具（`gcc make git postgresql-server-devel`）→ `git clone v0.8.7` → `make install` → `restorecon` | ⚠️ **默认源码编译最新版 0.8.7**。注意：OL10 的 `postgresql-devel` 不带 `pg_config`，必须用 `postgresql-server-devel`（脚本已处理）。发行版包只有 0.6.x，默认不用；想省事用旧版就 `PGVECTOR_FROM_DIST=1 bash scripts/setup_pg.sh` |
| 4 | **初始化数据目录** | `postgresql-setup --initdb` | ⚠️ **RHEL 系独有**。Debian 系是装包时自动初始化的，RHEL 系必须显式执行，否则服务起不来 |
| 5 | 启动服务 | `systemctl enable --now postgresql` | ⚠️ **RHEL 系服务名是 `postgresql`**（不是 Ubuntu 的 `postgresql@16-main`）；debian 系还要 `pg_ctlcluster` |
| 6 | **修认证方式** | 把 `pg_hba.conf` 里的 `ident` 改成 `scram-sha-256` | ⚠️ **RHEL 系独有且关键**，详见下一节 |
| 7 | 设密码 | `ALTER USER postgres PASSWORD 'pgvec123'` | 让程序能用密码经 TCP 连进来 |
| 8 | 建库 | `createdb fence_demo` | 新建一个空库 |
| 9 | 灌示例 + 收统计 | `psql -f examples/sample_schema.sql` + `ANALYZE;` | 建 7 张表（含 6 类真实陷阱）+ 生成 `pg_stats` |

### 5.3 ⚠️ 最关键的一处差异：`ident` 必须改成 `scram-sha-256`

**这是 Oracle Linux 上唯一一处"不改就必然失败"的地方，值得单独讲清楚。**

PostgreSQL 的客户端认证由 `pg_hba.conf` 控制。两个发行版的默认值不同：

**Ubuntu 24.04 的默认值**（开箱就能用密码连）：

```
host    all   all   127.0.0.1/32   scram-sha-256
```

**Oracle Linux / RHEL 系的默认值**：

```
host    all   all   127.0.0.1/32   ident      ← 问题在这里
```

`ident` 的机制是：数据库去问客户端所在机器的 **ident 服务（113 端口）**"你是哪个用户"，然后按回答决定放行。本机上没有 ident 服务在跑，所以这一问永远得不到答案 → **每次用密码走 TCP 连接都会失败**。

而 `demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo` **正是走 TCP**。所以不改这一行，live 模式必然连不上。

**安装脚本已经自动帮你改了**（并且留了备份 `pg_hba.conf.bak.<时间戳>`）。手工核对一下：

```bash
sudo grep -E "^(host|local)" /var/lib/pgsql/data/pg_hba.conf
```

**合格输出**（注意 host 行是 `scram-sha-256`，`local` 行保持 `peer` 不动）：

```
local   all             all                                     peer
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256
local   replication     all                                     peer
host    replication     all             127.0.0.1/32            scram-sha-256
host    replication     all             ::1/128                 scram-sha-256
```

**如果还是 `ident`**，手工改（脚本因为某些原因没改到）：

```bash
sudo cp /var/lib/pgsql/data/pg_hba.conf /var/lib/pgsql/data/pg_hba.conf.bak.manual
sudo sed -i -E 's/^([[:space:]]*host[[:space:]]+.*[[:space:]])ident([[:space:]]*)$/\1scram-sha-256\2/' /var/lib/pgsql/data/pg_hba.conf
sudo systemctl reload postgresql
sudo grep -E "^host" /var/lib/pgsql/data/pg_hba.conf
```

> **为什么 `local` 那行保持 `peer` 不动**：`local` 走的是 Unix socket（`sudo -u postgres psql` 用这条），`peer` 通过比对操作系统用户名放行，是最安全的方式，不需要改。
> **为什么不改成 `md5`**：MD5 是过时的弱算法。PostgreSQL 14 起 `scram-sha-256` 就是默认且推荐的密码算法。你在客户现场也只应该用 scram。

### 5.4 手工核对 PG 装好了 `[VM]`

```bash
postgres --version
```

预期（小版本号会变，**只要是 16.x 就合格**）：

```
postgres (PostgreSQL) 16.10
```

```bash
psql --version && ls -l /var/lib/pgsql/data/PG_VERSION
```

预期：

```
psql (PostgreSQL) 16.10
-rw-r--r--  1 postgres postgres 3 Oct  3 14:25 /var/lib/pgsql/data/PG_VERSION
```

> `/var/lib/pgsql/data` 就是 Oracle Linux 上数据库文件的存放位置（Ubuntu 上是 `/var/lib/postgresql/16/main`）。里面那个 `PG_VERSION` 文件是数据目录已初始化的标志 —— 没有它说明第 4 步没做。

### 5.5 手工核对 7 张示例表都在 `[VM]`

```bash
sudo -u postgres psql -d fence_demo -c "\dt shop.*"
```

预期看到 **7 行**：`orders`、`orders_archive`、`refunds`、`blacklist`、`user_profile`、`user_events`、`users`

> **如果看到一行警告 `could not change directory to "/home/xxx": Permission denied`** —— 这是无害的。原因是 `postgres` 用户读不了你的家目录。想让它消失就先 `cd /tmp` 再执行命令。（安装脚本里已经这么做了。）

---

## 6. 运行①：离线模式（不需要数据库）

### 6.1 跑起来 `[VM]`

```bash
cd ~/schemafence
python3 demo.py
```

> **不需要 `pip install` 任何东西**。这个脚本只用 Python 标准库 —— 它是刻意做成零依赖的，就是为了让"克隆下来就能跑"这件事成立。

### 6.2 你该看到什么（逐条对照）

```
schemafence — the constraint layer between LLMs and databases
====================================================================
source : sample_schema.sql
tables : 7      columns : 33
mode   : offline (no database, no model, no API key)

[check 1] schema resolution — which entity gets picked   (1)
  HIGH   shop.orders  vs  shop.orders_archive
         why : Two tables differ only by name ...
[check 2] join-key sanity — rows silently dropped   (4)
[check 3] type & precision — values silently changed   (6)
[check 4] NULL semantics — three-valued logic traps   (1)
[check H] hygiene — comments and naming   (4)

[summary]
  16 finding(s): 5 high / 9 medium / 2 low
  every one of them produces a query that runs successfully and returns
  a wrong number.  None of them raises an error.
[guard] seven-layer selftest
  ok   expect allow  SELECT ...
  ...
  13 cases → all passed
```

**判定标准**（数字对不上就是代码没传全）：

| 数字 | 应该是 |
|---|---|
| `tables` | 7 |
| `columns` | 33 |
| `finding(s)` | 16（5 high / 9 medium / 2 low） |
| selftest | `13 cases → all passed` |

### 6.3 看完整链路：提问 → 选表 → 生成 SQL → 护栏判决 `[VM]`

```bash
python3 demo.py --ask "total order amount for the last week?"
```

重点看这一段：

```
  #1  shop.orders                score 6
  #2  shop.orders_archive        score 6
  note : 2 candidate tables scored close together — the model picks one,
         and nothing in the database stops it picking the wrong one
```

**这就是整个项目要解决的问题**：两张表得分一模一样，模型必然会挑一张，而数据库不会阻止它挑错。`shop.orders` 是活数据，`shop.orders_archive` 是归档表 —— 挑错了，报表数字就是错的，**而且不会报任何错**。

再试中文提问：

```bash
python3 demo.py --ask "哪个表存了退款信息？"
```

预期 `#1  shop.refunds  score 12` —— 唯一答案、分数明显拉开，**没有歧义，也就没什么可拦的**。

**把这两次输出对比着看，你就明白"护栏"到底在拦什么了。**

### 6.4 故意让护栏拦一次 `[VM]`

```bash
python3 -c "
from schemafence.guard import guard
v = guard('SELECT * FROM shop.orders; DROP TABLE shop.users')
print('allowed:', v.ok)
print('layer  :', v.layer)
print('reason :', v.reason)
"
```

预期：

```
allowed: False
layer  : L1
reason : multiple statements in one call are not allowed
```

**一条正常查询后面偷偷接一句"删表"，护栏在第一层就拦住了。**

---

## 7. 运行②：连接真实数据库（live 模式）

前面都是对着一份 DDL 文本做静态分析。现在要连上真正跑起来的 PG，从系统目录里读**真实结构、真实行数估算、真实的 NULL 比例**。

### 7.1 建一个隔离的 Python 环境 `[VM]`

```bash
cd ~/schemafence
python3 -m venv .venv
source .venv/bin/activate
```

执行后提示符前面会多出 `(.venv)`：

```
(.venv) [gavin@schemafence-pg schemafence]$
```

> **为什么必须用 venv**：Oracle Linux 10（和所有 RHEL 9+）不允许直接往系统 Python 装包，会报 `error: externally-managed-environment`。venv 相当于给这个项目单独一个抽屉，装什么都不影响系统。

### 7.2 装 live 模式唯一需要的依赖 `[VM]`

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

预期看到：

```
Successfully installed psycopg-3.2.x psycopg-binary-3.2.x python-dotenv-1.x.x
```

> `requirements.txt` 里只有 `psycopg[binary]`（连 PG 的驱动）和 `python-dotenv`。`[binary]` 意味着有预编译 wheel，**不需要编译、不需要 libpq-devel**。离线模式一个都不需要。

### 7.3 跑 live 模式 `[VM]`

```bash
python demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo
```

> 进了 venv 之后用 `python` 或 `python3` 都可以。

### 7.4 你该看到什么（本次配置最关键的一段验证）

在 6.2 那堆输出之后，会多出这么一段：

```
[live] connected to a real database
  tables read from the catalogue : 7
  columns                        : 33

  row estimates (from pg_class.reltuples, no COUNT(*))
    shop.orders                  ~1000
    shop.orders_archive          ~1000
    ...

  measured NULL fractions (pg_stats) — the join-loss evidence
    shop.orders.user_id          12.4% NULL
    ...

  same checks, live data → N finding(s) (X high, Y medium, Z low)
```

**三处必须对得上**：

| 看到什么 | 说明什么 |
|---|---|
| `tables read from the catalogue : 7` | 真的读到了你刚建的 7 张表（不是读本地文件） |
| `row estimates` 有行数 | 读的是 `pg_class.reltuples` 这个**统计估算**，对表**没有执行 `COUNT(*)`** |
| `measured NULL fractions (pg_stats)` 有百分比 | 读到了 `ANALYZE` 生成的**列级统计信息** |

> **中间那一行值得多看一眼**：在大表上执行 `COUNT(*)` 会全表扫描、可能拖垮数据库。这个项目用 `reltuples`（统计信息里的行数估算）代替 —— 这是典型的 DBA 视角设计，也是"懂数据库的人和不懂的人做出来东西的差别"。
>
> **第三行就是"连接丢行"这个陷阱的物证**：`shop.orders.user_id` 有一部分是 NULL。一旦 SQL 按它去 join 用户表，这些订单会被**静默丢掉** —— 结果少了一批订单，但没有任何报错。

> ⚠️ 如果这里打印的是 `could not connect: ...` →
> - 报 `ident authentication failed` 或 `no pg_hba.conf entry` → **回到 5.3**，这是 Oracle Linux 上最常见的坑
> - 报 `ModuleNotFoundError: No module named 'psycopg'` → 见 11.6
> - 报 `connection refused` → 见 11.5

### 7.5 验证 pgvector 真的能用（向量检索） `[VM]`

```bash
sudo -u postgres psql -d fence_demo -f scripts/verify_pgvector.sql
```

这个脚本会做四件事：建一个 3 维向量表 → 插入 3 条 → 做一次余弦相似度检索 → 建 HNSW 索引 → 创建只读账号 `agent_ro`。

> **为什么用 3 维手写向量**：为了不依赖任何 embedding API（那需要联网 + API key）。今天要验证的只有一件事 —— pgvector 能装、能查、索引能建。真实维度（1024/1536 维）是下一步的事。

**按顺序应看到三段输出**：

**第 1 段 —— 相似度检索**（`订单金额` 排第一，它的向量离查询向量最近）：

```
   label   | similarity
-----------+------------
 订单金额  |     0.9902
 退款原因  |     0.9364
(2 rows)
```

**第 2 段 —— HNSW 索引建好了**：

```
        indexname        |                  indexdef
-------------------------+--------------------------------------------
 idx_demo_vectors_hnsw   | CREATE INDEX idx_demo_vectors_hnsw ON ...
                         |  USING hnsw (embedding vector_cosine_ops) ...
(1 row)
```

> HNSW 是 pgvector 0.5.0 才引入的索引算法。能建起来，说明插件版本足够新（0.6.x 和 0.8.x 都支持，本手册装的是 0.8.7）。

**第 3 段 —— 只读账号**：

```
 rolname  | rolcanlogin | rolcreatedb | rolsuper
----------+-------------+-------------+----------
 agent_ro | t           | f           | f
(1 row)
```

### 7.6 验证"只读账号物理上写不进去"（七层护栏的第一层） `[VM]`

**先退出 venv 或另开一个终端**（这条用系统自带的 `psql` 就行）：

```bash
psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" \
     -c "DELETE FROM shop.orders WHERE 1=1"
```

**预期是报错 —— 报错就说明成功**：

```
ERROR:  permission denied for table orders
```

> **为什么这一条特别重要**：
> 6.4 那次拦截是**代码里的规则** —— 规则可以改、可以写错、可以被绕过。
> 这一条是**数据库层面的物理权限** —— 就算上层代码被绕过、就算有人直接敲 SQL，这个账号也删不掉任何东西。
> 这是七层护栏里最硬的一层：**不是"我不允许你写"，而是"你根本没有写的权限"**。

### 7.7 交互式体验一下 PG（可选） `[VM]`

```bash
sudo -u postgres psql -d fence_demo
```

进入后依次试这几条（**每条以分号结尾**，回车执行）：

```sql
\dt shop.*                                      -- 列出 shop 下的所有表
SELECT count(*) FROM shop.orders;                -- 数一下订单有多少条
\d shop.orders                                   -- 看 orders 表的完整定义
\du                                              -- 列出所有数据库角色
SELECT extname, extversion FROM pg_extension;    -- 看装了哪些扩展（应有 vector）
\q                                               -- 退出
```

---

## 8. 全流程验证清单（16 条）

**按顺序做完，每条打勾，才算环境配置完整。**

| # | 验证什么 | 在哪跑 | 命令 | 合格标准 |
|---|---|---|---|---|
| V1 | 虚拟机在运行 | VMware 界面 | 看左侧列表 | `schemafence-pg`（或你的名字），Powered On |
| V2 | 能从 Windows 登录 | `[Win]` | `ssh <用户>@<IP>` | 进入虚拟机提示符 |
| V3 | 系统是 Oracle Linux 10 | `[VM]` | `cat /etc/os-release \| head -2` | `Oracle Linux Server` + `10.x` |
| V4 | 基础工具齐全 | `[VM]` | `python3 --version && git --version` | Python 3.12.x + git 版本号 |
| V5 | 时区正确 | `[VM]` | `date` | 含 `CST`，时间与北京时间一致 |
| V6 | PG 服务在跑 | `[VM]` | `systemctl is-active postgresql` | `active` |
| V7 | PG 版本正确 | `[VM]` | `postgres --version` | `PostgreSQL 16.x` |
| V8 | 数据目录已初始化 | `[VM]` | `cat /var/lib/pgsql/data/PG_VERSION` | `16` |
| V9 | **认证方式是密码** | `[VM]` | 见 5.3 的 grep 命令 | host 行是 `scram-sha-256`，**不是 `ident`** |
| V10 | 示例库建好了 | `[VM]` | 见 5.5 | 7 张表 |
| V11 | pgvector 装好了 | `[VM]` | 见下方汇总 | `pgvector = 0.8.7`（若走了发行版包则为 0.6.x） |
| V12 | 离线审计能跑 | `[VM]` | 见 4.1 | `16 finding(s): 5 high / 9 medium / 2 low` |
| V13 | 七层护栏自测 | `[VM]` | 见 4.1 | `13 cases → all passed` |
| V14 | 护栏能拦危险 SQL | `[VM]` | 见 6.4 | `allowed: False` / `layer: L1` |
| V15 | live 模式连上库 | `[VM]` | 见 7.3 | `tables read from the catalogue : 7` |
| V16 | 读到真实统计信息 | `[VM]` | 见 7.3 | 有 `row estimates` 和 `measured NULL fractions` |

**外加两条"环境健康"检查（Oracle Linux 特有）**：

| # | 验证什么 | 命令 | 合格标准 |
|---|---|---|---|
| H1 | SELinux 没有拦 PG | `sudo ausearch -m AVC -ts recent 2>/dev/null \| grep -i postgres \| head` | **没有任何输出** |
| H2 | 只读账号写不进去 | 见 7.6 | 报 `permission denied for table orders` |

### V11 汇总命令：一次跑完 4 项只读检查

```bash
sudo -u postgres psql -d fence_demo -tAc "SELECT 'pg_version     = ' || current_setting('server_version')"
sudo -u postgres psql -d fence_demo -tAc "SELECT 'data_directory = ' || current_setting('data_directory')"
sudo -u postgres psql -d fence_demo -tAc "SELECT 'tables_in_shop = ' || count(*) FROM information_schema.tables WHERE table_schema='shop'"
sudo -u postgres psql -d fence_demo -tAc "SELECT 'pgvector       = ' || COALESCE((SELECT extversion FROM pg_extension WHERE extname='vector'),'NOT INSTALLED')"
sudo -u postgres psql -d fence_demo -tAc "SELECT 'vector_ops     = ' || count(*) FROM pg_operator WHERE oprname IN ('<=>','<->','<#>')"
```

**合格输出**：

```
pg_version     = 16.10
data_directory = /var/lib/pgsql/data
tables_in_shop = 7
pgvector       = 0.8.7
vector_ops     = 3
```

| 行 | 说明 |
|---|---|
| `pg_version` | 只要是 `16.x` 就合格 |
| `data_directory` | 必须是 `/var/lib/pgsql/data`（说明是 RHEL 系的标准布局） |
| `tables_in_shop` | 必须是 `7` |
| `pgvector` | 有版本号即合格。显示 `NOT INSTALLED` → 见 11.4 |
| `vector_ops` | `>= 3` 即合格（`<=>` 余弦、`<->` 欧氏、`<#>` 内积）。是 `0` 说明插件没加载成功 |

---

## 9. 拍快照 + 日常使用

### 9.1 立刻拍一个快照（30 秒，强烈建议）

现在这个状态是"干净可用"的。在 VMware 菜单：

**VM → Snapshot → Take Snapshot...**

| 字段 | 填 |
|---|---|
| Name | `baseline-pg16-pgvector-demo` |
| Description | `Oracle Linux 10 + PG16 + pgvector + fence_demo 已就绪 2026-10-03` |

> **为什么这一步价值最高**：以后你实验搞乱了 —— 改错配置、删错表、升级失败、装了个包把系统搞崩 —— 只要 **VM → Snapshot → Revert to Snapshot**，选这个快照，**一分钟就回到现在这个状态**，什么都不用重装。
>
> **花 30 秒，省掉未来几小时。**

### 9.2 日常开关

| 想做什么 | 怎么做 |
|---|---|
| 暂时不用，下次接着用 | VMware 菜单 **VM → Power → Suspend** |
| 完全退出 | 虚拟机里 `sudo poweroff`；或 VMware 菜单 **VM → Power → Shut Down Guest** |
| 下次开机 | VMware 里选中虚拟机 → **Power On** |
| 只操作数据库，不打开 VMware 窗口 | VMware 保持运行（可最小化）→ 从 Windows `ssh <用户>@<IP>` |

### 9.3 服务会自动启动吗？

会。`systemctl enable --now postgresql` 已经做了开机自启。确认：

```bash
systemctl is-enabled postgresql
```

预期 `enabled`。

```bash
systemctl status postgresql --no-pager | head -5
```

应看到 `Active: active (running)`。

### 9.4 需要开防火墙吗？

**不需要。** PG 默认只监听 `localhost`（127.0.0.1），而 `demo.py` 也是在这台虚拟机内部连接的。

确认一下：

```bash
sudo ss -lntp | grep 5432
```

预期只看到 `127.0.0.1:5432`，**没有** `0.0.0.0:5432`：

```
LISTEN 0  244  127.0.0.1:5432  0.0.0.0:*  users:(("postgres",pid=1234,fd=6))
```

> **这个输出本身就是一条安全验证**：数据库没有对网络暴露。要改成允许外部连接是另一个话题（需要同时改 `listen_addresses` + `pg_hba.conf` + 开 firewalld 端口），本项目不需要。

---

## 10. Ubuntu 版 vs Oracle Linux 版差异对照

如果你看过上一版文档，这四项必须更新认知 —— 其中第 1 项不改就必然失败。

| # | 项目 | Ubuntu 24.04 | **Oracle Linux 10 / RHEL 系** | 影响 |
|---|---|---|---|---|
| 1 | **`pg_hba.conf` 默认认证** | `scram-sha-256`（开箱可用密码） | **`ident`** ← 必须改成 `scram-sha-256` | 🔴 **致命**：不改则 live 模式 100% 连不上 |
| 2 | **数据目录初始化** | 装包时自动完成 | 必须手工 `postgresql-setup --initdb` | 🔴 不做则服务起不来 |
| 3 | **数据目录路径** | `/var/lib/postgresql/16/main` | **`/var/lib/pgsql/data`** | 🟡 所有涉及路径的命令都要换 |
| 4 | **服务名** | `postgresql@16-main` / `pg_lsclusters` | **`postgresql`**（用 `systemctl status postgresql`） | 🟡 `pg_lsclusters` 在 OL 上不存在 |
| 5 | 服务启动方式 | `pg_ctlcluster 16 main start` | `systemctl enable --now postgresql` | 🟡 容器里两者都要 `service` |
| 6 | pgvector 来源 | `apt install postgresql-16-pgvector` | **源码编译 0.8.7**（AppStream 包只有 0.6.x） | 🟡 多一步编译，脚本自动做 |
| 7 | 包管理器 | `apt` / `apt-get` | `dnf` / `yum` | 🟢 |
| 8 | SELinux | 无（AppArmor） | **Enforcing** | 🟡 源码编译插件后要 `restorecon` |
| 9 | 防火墙 | ufw（默认不拦本地） | **firewalld**（running） | 🟢 本地连接不受影响 |
| 10 | Python | 3.12 | 3.12 | 🟢 |
| 11 | 往系统 Python 装包 | 被 PEP 668 禁止 | 被 PEP 668 禁止 | 🟢 都要用 venv |
| 12 | 需要订阅注册吗 | 否 | **否**（Oracle Linux 公共源免注册；RHEL 才需要） | 🟢 |

**一句话记住**：**Ubuntu 是"装完就能连"，Oracle Linux 是"装完还要两步（initdb + 改 pg_hba）"。**

---

## 11. 故障排查速查表

### 11.1 虚拟机没 IP / ssh 连不上

```bash
# ① 网卡状态
ip -4 addr show
nmcli device status
```

- `ens33` 显示 `disconnected` → `sudo nmcli device connect ens33`
- 有 IP 但 Windows 连不上 → 在 VMware 里确认 **VM → Settings → Network Adapter** 是 **NAT** 且勾了 **Connected** + **Connect at power on**
- 仍不通 → 查 sshd：`systemctl status sshd --no-pager | head -3`，不是 `active (running)` 就 `sudo systemctl enable --now sshd`
- Windows 侧：`ping <虚拟机IP>`。不通说明是网络层；通但 ssh 失败说明是 sshd 层
- **IP 变了是常态**（NAT 下 DHCP）。每次连之前先 `hostname -I`
- 公司网络有管控策略时，**直接用 VMware 窗口操作**，功能完全一样

### 11.2 `dnf` 很慢 / `repolist` 报错

Oracle Linux 的源指向 `yum.oracle.com`（公共、免注册）。如果慢：

```bash
# 看看启用了哪些源
sudo dnf repolist

# 清缓存重建（解决元数据过期导致的怪问题）
sudo dnf clean all && sudo dnf makecache
```

如果报无法解析域名 → 先确认网络：`ping -c 2 8.8.8.8`，再 `ping -c 2 yum.oracle.com`。DNS 问题就检查 `/etc/resolv.conf`。

国内可考虑用 Oracle Linux 的镜像站（如清华、阿里），把 `/etc/yum.repos.d/oracle-linux-ol10.repo` 里的 `baseurl` 域名替换即可 —— 但**这一步不是必须的，先在原速下试**，`dnf makecache` 之后包本身下载通常不慢。

### 11.3 PG 服务起不来

```bash
systemctl status postgresql --no-pager
journalctl -u postgresql -n 50 --no-pager
```

**按报错对症**：

| 报错 | 原因 | 修复 |
|---|---|---|
| `directory "/var/lib/pgsql/data" is missing or empty` | **忘了 initdb**（Oracle Linux 上最常见） | `cd /tmp && sudo postgresql-setup --initdb --unit postgresql` |
| `could not create lock file ... Permission denied` | 数据目录属主不对 | `sudo chown -R postgres:postgres /var/lib/pgsql/data` |
| `could not load library ... vector.so: Permission denied` | **SELinux 拦了源码编译的插件** | `sudo restorecon -Rv /usr/lib64/pgsql /usr/share/pgsql && sudo systemctl restart postgresql` |
| `make: pg_config: 没有那个文件或目录`（编译 pgvector 时） | OL10 的 `postgresql-devel` **不带** `pg_config` | `sudo dnf -y install postgresql-server-devel`，然后重跑 `bash scripts/setup_pg.sh` |
| `port 5432 already in use` | 有别的实例在跑 | `sudo ss -lntp \| grep 5432` 找出进程 |

### 11.4 pgvector 装不上 / `pgvector = NOT INSTALLED`

**先看包在不在**：

```bash
dnf list available pgvector postgresql*-pgvector 2>/dev/null
rpm -qa | grep -i vector
```

**情况 A：源码编译失败 / 网络连不上 GitHub**

脚本的默认路径就是源码编译 v0.8.7（为了拿到最新版）。如果编译或 clone 失败，可以退回发行版包（Oracle Linux 10 AppStream 自带 0.6.x，功能够 demo 用）：

```bash
PGVECTOR_FROM_DIST=1 bash scripts/setup_pg.sh
```

或者手工 `sudo dnf -y install pgvector`。如果连 0.6.x 都想要更新的包，两条路：

**A-1　从 PGDG 拿 0.8.x 发行版包**（不想源码编译时用）：

```bash
sudo dnf -y install https://download.postgresql.org/pub/repos/yum/reporpms/EL-10-x86_64/pgdg-redhat-repo-latest.noarch.rpm
sudo dnf -y install pgvector_16
sudo systemctl restart postgresql
sudo -u postgres psql -d fence_demo -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

**A-2　源码编译**（脚本的默认路径，手工版）：

```bash
sudo dnf -y install gcc make git postgresql-server-devel
pg_config --version   # 能输出 PostgreSQL 16.x 再继续
rm -rf /tmp/pgvector
git clone --branch v0.8.7 --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector
make -C /tmp/pgvector && sudo make -C /tmp/pgvector install
# SELinux：给新装的文件打正确的标签，否则 PG 拒绝加载
sudo restorecon -Rv /usr/lib64/pgsql /usr/share/pgsql
sudo systemctl restart postgresql
sudo -u postgres psql -d fence_demo -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

> ⚠️ **`restorecon` 那一行在 Oracle Linux 上不能省**。RHEL 系默认 SELinux `Enforcing`，`make install` 丢进去的 `.so` 文件标签可能不对，PG 会被拒绝加载，报的还是含糊的"could not load library"。

**情况 B：包装了，但 `CREATE EXTENSION` 报 "extension not found"**

两个原因：① **大版本号没对上**（PG 16 就要 `pgvector_16`，不能装 `pgvector_17`）；② **装完没重启服务**（`.so` 要重启才加载）。

```bash
sudo -u postgres psql -tAc "SHOW server_version;"
sudo systemctl restart postgresql
sudo -u postgres psql -d fence_demo -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

**验证**：

```bash
sudo -u postgres psql -d fence_demo -tAc "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

### 11.5 live 模式报 `could not connect` / `connection refused`

```bash
# ① 服务在跑吗
systemctl is-active postgresql

# ② 端口在听吗
sudo ss -lntp | grep 5432

# ③ 手工连一次，看真实报错
psql "postgresql://postgres:pgvec123@localhost:5432/fence_demo" -c "SELECT 1"
```

| psql 报什么 | 原因 | 修复 |
|---|---|---|
| `FATAL: Ident authentication failed` 或 `no pg_hba.conf entry` | **还是 `ident`** | 回到 **5.3** |
| `FATAL: password authentication failed` | 密码没设上 | `sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'pgvec123';"` |
| `connection refused` | 服务没起 / 端口不对 | 回 11.3 |
| `database "fence_demo" does not exist` | 库没建 | `sudo -u postgres createdb fence_demo` |

### 11.6 live 模式报 `ModuleNotFoundError: No module named 'psycopg'`

说明没进 venv，或依赖没装。**按顺序**：

```bash
cd ~/schemafence
source .venv/bin/activate
pip install -r requirements.txt
python demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo
```

> ⚠️ **关键点**：`source .venv/bin/activate` 和 `python demo.py` **必须在同一个终端会话里**。关掉终端重新 ssh 进来后 `(.venv)` 会消失，必须重新 `source` 一次。
>
> 判断有没有进 venv：**看提示符前面有没有 `(.venv)`**。

### 11.7 `bash scripts/setup_pg.sh` 中途失败

脚本每一步都会打印 `==> 步骤名`，看它停在哪：

| 停在哪 | 原因 | 怎么办 |
|---|---|---|
| `==> installing postgresql` | 网络/源问题 | 见 11.2 |
| `==> installing pgvector` | 包不存在且编译失败 | 见 11.4 |
| `==> starting the cluster` | 服务起不来 | 见 11.3 |
| `==> configuring password authentication` | 找不到 pg_hba.conf | 手工按 5.3 改 |
| `==> loading the demo schema` | SQL 失败 | 先 `sudo -u postgres psql -d fence_demo -c "\dt shop.*"` 看建了几张表 |

修完问题后**直接再跑一遍**（脚本幂等）：

```bash
cd ~/schemafence && bash scripts/setup_pg.sh
```

### 11.8 每条 `sudo -u postgres psql` 都打印 `could not change directory to "/home/xxx"`

**无害警告**，因为 `postgres` 用户读不了你的家目录。想让它消失就先切换目录：

```bash
cd /tmp
sudo -u postgres psql -d fence_demo -c "SELECT 1"
```

或者用登录 shell：`sudo -iu postgres psql -d fence_demo`。

### 11.9 SELinux 相关排查

```bash
getenforce                                  # 应该是 Enforcing
sudo ausearch -m AVC -ts recent | tail -30   # 看最近的拒绝记录
sudo semanage boolean -l | grep -i postgres  # 看相关布尔值
```

> `semanage` 不在的话先 `sudo dnf -y install policycoreutils-python-utils`。

**本文档的流程不需要关 SELinux。** 如果确实遇到了被拦的情况，优先用 `restorecon` 修标签，而不是 `setenforce 0` —— 关掉只是把问题藏起来，以后换台机器还会遇到。

### 11.10 想从 Windows 用客户端连虚拟机的 PG（可选）

默认**不需要**也**建议不要**开。如果确实想（比如用你机器上的 PLSQL Developer 或 DBeaver 连过去看看），三处都要改：

```bash
# ① 让 PG 监听所有地址
sudo sed -i "s/^#\?listen_addresses.*/listen_addresses = '*'/" /var/lib/pgsql/data/postgresql.conf

# ② 允许你的网段（把 192.168.181.0/24 换成你虚拟机实际网段）
echo "host  all  all  192.168.181.0/24  scram-sha-256" | sudo tee -a /var/lib/pgsql/data/pg_hba.conf

# ③ 开防火墙
sudo firewall-cmd --permanent --add-service=postgresql
sudo firewall-cmd --reload

# ④ 重启
sudo systemctl restart postgresql
```

> ⚠️ 注意：`listen_addresses = '*'` **暴露在网络上**是运维大忌。只在你的实验虚拟机里用，做完了改回 `localhost`。

---

## 附录 A：资源调整

虚拟机里 PG 的资源占用很低（示例库不到 10MB，2 核 4G 跑得很宽裕）。要调整：

| 情况 | 怎么做 |
|---|---|
| 内存不够 | **关机**后 **VM → Settings → Memory** → 调到 8192 MB（主机 16GB，最多给 8GB） |
| 核数不够 | **VM → Settings → Processors** → 调到 4 |
| 磁盘不够 | **VM → Settings → Hard Disk → Expand** 扩到 60GB，然后进虚拟机执行：<br>`sudo growpart /dev/sda 3 && sudo xfs_growfs /`（注意：Oracle Linux 默认**文件系统是 XFS**，用 `xfs_growfs`，不是 Ubuntu 的 `resize2fs`） |

> 调整内存/核数**必须先关机**（挂起状态改不了）。

**每步耗时参考**：`dnf upgrade` 2–8 分钟 · 装 PG + pgvector 2–5 分钟 · 克隆代码 <1 分钟 · venv + psycopg 1–2 分钟。**合计约 30–45 分钟。**

---

## 附录 B：想用更新的 PG / pgvector（PGDG 路径）

Oracle Linux 10 自带的 PG 是 16；pgvector 我们已经源码装到了最新的 0.8.7。想再跟上游主要是换 PG 主版本：

- **pgvector 0.8.x** 新增了迭代式索引扫描（`hnsw.iterative_scan`），在"带 WHERE 过滤的向量检索"上明显更好 —— 这是 schemafence Day 4–7 会碰到的场景。
- **PG 17 / 18** 有增量排序、`pg_stat_io` 等改进。

**用 PGDG 仓库替换成新版**（PGDG 对 EL10 官方支持）：

```bash
# 1) 加入 PGDG 仓库
sudo dnf -y install https://download.postgresql.org/pub/repos/yum/reporpms/EL-10-x86_64/pgdg-redhat-repo-latest.noarch.rpm

# 2) 屏蔽发行版自带的 PG，避免版本混装
sudo dnf -y module disable postgresql

# 3) 装 PG 17 + 对应 pgvector
sudo dnf -y install postgresql17-server postgresql17-contrib pgvector_17

# 4) 初始化（注意路径带版本号）
sudo /usr/pgsql-17/bin/postgresql-17-setup initdb

# 5) 启动（服务名也带版本号）
sudo systemctl enable --now postgresql-17
```

> ⚠️ **走这条路的话，`scripts/setup_pg.sh` 后面的步骤要手工补**：脚本是针对发行版自带的 `postgresql` 服务写的。PGDG 安装的版本服务名是 `postgresql-17`、数据目录是 `/var/lib/pgsql/17/data`、`pg_config` 在 `/usr/pgsql-17/bin`。
>
> **建议**：先用发行版自带的路径把整条链路跑通（也就是本文档的流程），确认理解了每一步在干什么之后，再考虑换版本。**不要一上来就同时引入"没跑通过"和"第三方仓库"两个变量** —— 出了问题分不清是谁的锅。

---

## 附录 C：改密码、重建示例库

**改 PG 密码**（同时要改 `demo.py` 命令里的密码）：

```bash
sudo -u postgres psql -c "ALTER USER postgres PASSWORD '新密码';"
```

**重建示例库**（把数据改乱了想恢复）：

```bash
sudo -u postgres psql -c "DROP DATABASE IF EXISTS fence_demo;"
sudo -u postgres createdb fence_demo
sudo -u postgres psql -q -d fence_demo -f ~/schemafence/examples/sample_schema.sql
sudo -u postgres psql -q -d fence_demo -c "CREATE EXTENSION IF NOT EXISTS vector;"
sudo -u postgres psql -q -d fence_demo -c "ANALYZE;"
```

**更快的方式**：直接 **VM → Snapshot → Revert to Snapshot** 回到 baseline 快照，10 秒搞定。

---

*文档版本：2026-10-03 · 适配 Oracle Linux 10.x · schemafence 第 3 天版本 · 主机 VMware Workstation 17.6.2 / Windows / 16GB RAM*
