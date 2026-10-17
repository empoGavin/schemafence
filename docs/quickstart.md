# 上手：先跑通不碰数据库的那一半，再接一台真库

schemafence 有两半：**审脚本/查查询的那半**不需要数据库，克隆下来就能跑；**读活库的那半**
需要一台真的 PostgreSQL。这份手册从零带到两台都能跑。

- **预计 30–45 分钟**（Linux 机器/容器已就绪的话），其中前 3 分钟就能看到第一份报告
- **不需要 PostgreSQL 基础**，命令都可直接复制粘贴
- 命令约定：`[VM]` 在 Linux 机器或 Cloud Studio 容器里执行，`[Win]` 在你本机执行

对应仓库里的两条代码路径：`demo.py`（离线，只用标准库）与 `python -m schemafence.review`
（审一份脚本或一条查询）。

---

## 0. 先看三条结论，能省你两个小时

**结论一：这个项目不需要 API key，也不需要 GPU。**
审脚本、查查询、七层护栏全是确定性代码，不是模型。所以离线那半零依赖、零费用、断网也能跑，
CPU 就够（没有本地模型要加载）。模型要到接上问答循环时才需要，那时候再配 key 也不迟。

**结论二：不要在容器里装 Docker。**
Cloud Studio 的工作空间本身就是一个容器，**里面没有 systemd**，`systemctl start docker`
会直接报错。网上教程让你 `sudo dockerd` 后台启动——能跑，但每次重启工作空间都要重来一遍。
所以 `scripts/setup_pg.sh` 走发行版原生的包管理与服务命令，不用 Docker。
`docker-compose.yml` 是给本地和别人的机器准备的。

**结论三：先在本机跑通离线那半，再上虚拟机或云。**
`python demo.py` 不需要数据库、不需要 key，本机 30 秒就能出结果（Windows 上就是在
`%USERPROFILE%\schemafence` 目录里敲这一条）。代码本身是好的再往下走，
后面出问题就一定是环境问题，排查范围立刻小一半。

---

## 1. 准备一个环境

两条路，选一条即可，都跑通同一批命令：

| | **A. Linux 机器** | **B. Cloud Studio 容器** |
|---|---|---|
| 适合 | 长期用、想留快照、想练手 | 手边没有 Linux、想几分钟试一次 |
| 本文档对应 | Oracle Linux 10 / RHEL 系（也适用 Ubuntu，差异见 2.4） | 腾讯云 Cloud Studio，Ubuntu 22.04 / 24.04 |
| 代价 | 要先有虚拟机 | 消耗机时额度，容器重启后数据库可能没了 |

### 1.1 环境要求

| | |
|---|---|
| Python | **3.10+**（3.12 实测）。离线那半只用标准库，装不装别的包都能跑 |
| PostgreSQL | 16.x（本文档的脚本装的就是它），只在 live 模式需要 |
| pgvector | 0.8.7（脚本默认源码编译；发行版包只有 0.6.x，也够 demo 用） |
| psycopg | live 模式唯一的第三方依赖，`requirements.txt` 里就这一行 |
| 网络 | 只在下两步需要：克隆仓库、装 PG。跑起来之后离线那半断网也行 |

### 1.2 走 A 路：确认 Linux 机器现状（8 项体检）

**在 Linux 机器里**依次执行这 8 条，把结果记下来：

```bash
# ① 发行版和版本
cat /etc/os-release | head -3

# ② 当前用户是谁、是不是 root
whoami; id -u

# ③ 能不能用 sudo
sudo -n true 2>/dev/null && echo "sudo 可用（免密）" || echo "sudo 需要密码，或当前不是 sudo 用户"

# ④ 机器的 IP
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

逐条对照：

| # | 应该看到 | 不对怎么办 |
|---|---|---|
| ① | `NAME="Oracle Linux Server"` + `VERSION_ID="10.x"` | 如果显示别的系统，本文档的包管理命令需要相应调整 |
| ② | 你自己的用户名（不是 `root`，`id -u` 不是 `0`），或就是 root | 两种都能继续，脚本两种情况都处理了 |
| ③ | `sudo 可用` | 如果提示没装 sudo：以 root 执行 `dnf -y install sudo`，再 `usermod -aG wheel <你的用户名>` |
| ④ | 形如 `inet 192.168.x.x/24` | 没 IP 说明网卡没起来，见 6.1 |
| ⑤ | `active` | 未运行 → `sudo dnf -y install openssh-server && sudo systemctl enable --now sshd` |
| ⑥ | 列出 `ol10_baseos_latest` / `ol10_appstream` 等 | 报错见 6.2 |
| ⑦ | `Python 3.12.x` | 更低版本也不影响离线模式（要求 3.10+） |
| ⑧ | `Enforcing` + `running` | 保持默认即可，不用关。脚本已处理 SELinux |

> **关于 SELinux**：Oracle Linux 默认 `Enforcing`（强制模式）。**不要为了图省事关掉它** ——
> 脚本已经处理了唯一会被它影响的地方（源码编译 pgvector 后的文件标签）。关掉反而会在
> 以后遇到 enforcing 的系统时重新踩一遍。
>
> **Python 3.12 上装包会被 PEP 668 拦住**（`error: externally-managed-environment`，
> **`sudo pip install` 一样被拦**，不是权限问题）。所以第 4 步用 venv，见 4.1。

装基础工具（`[VM]`）：

```bash
sudo dnf -y install git python3-pip python3-devel gcc make vim-enhanced unzip curl
sudo timedatectl set-timezone Asia/Shanghai
```

| 包 | 用途 |
|---|---|
| `git` | 从 GitHub 克隆代码 |
| `python3-pip` | 装 Python 包；也提供 `venv` 所需的 ensurepip |
| `python3-devel` `gcc` `make` | 编译器。**必装**：pgvector 默认走源码编译（为了拿到最新版 0.8.7） |
| `vim-enhanced` | 编辑配置文件（不习惯就用 `nano`，也在系统里） |
| `unzip` `curl` | 解压、下载、测试接口 |

从 Windows 连进这台机器（`[Win]`）：Git Bash 或 PowerShell 里 `ssh <你的用户名>@192.168.181.128`
（IP 用体检第 ④ 条看到的那个）。第一次连接问 `Are you sure you want to continue connecting`
时输 `yes`，再输密码。传文件用 WinSCP：协议 **SFTP** → 主机填 IP → 端口 22。

> **在 VMware 的 NAT 网络模式下，宿主机可以直接访问虚拟机的 IP**，不需要配端口转发。
> 连不上也没关系，**直接在 VMware 窗口里操作效果完全一样**，只是粘贴用鼠标右键。

### 1.3 走 B 路：建一个 Cloud Studio 工作空间

1. 打开 <https://cloudstudio.net> 或 <https://ide.cloud.tencent.com>，微信登录
2. 右上角头像 → 看 **资源包 / 我的额度**，确认还剩多少机时
3. 点 **创建应用 / 新建工作空间**：
   - 方式选 **从 Git 仓库导入**
   - 仓库地址填：`https://github.com/empoGavin/schemafence`
   - 运行环境 / 模板：选 **Ubuntu**（22.04 或 24.04 都可以，脚本两种都兼容）
   - 规格：**2 核 4G**（CPU，**不要选 GPU** —— 按机时抵扣因子，GPU 是纯烧额度）

| 规格 | 抵扣因子（机时/小时） | 适合 |
|------|---------------------|------|
| 1 核 2G | 0.1 | 够用，但编译 pgvector 会慢 |
| **2 核 4G** | **0.25** | **推荐：够快，省额度** |
| 4 核 8G | 0.5 | 奢侈但可接受 |
| GPU T4 | 1.2 | ❌ 本项目完全用不到 |
| GPU A10 / L40 | 3.3 / 8 | ❌ 开了就是在烧额度 |

即使是首次绑定赠送的 20 机时，在 2 核 4G 上也能跑 **80 小时**，调试期基本够用。平台通常还有每月
免费额度，**具体以控制台「我的额度」页面为准，先看一眼再建空间**。

4. 创建后点 **编写代码 / 进入工作空间**，等 10 秒左右打开 Web IDE
5. 打开底部 **终端**，确认环境：

```bash
python3 -V          # 需要 3.10 以上
git log --oneline -1
```

> Cloud Studio 已经把你的登录凭据接进了 git，后面 `git push` 不需要再输密码。

### 1.4 两条路的差异，先知道这四处

| # | 项目 | Ubuntu（Cloud Studio） | **Oracle Linux 10 / RHEL 系** | 影响 |
|---|---|---|---|---|
| 1 | **`pg_hba.conf` 默认认证** | `scram-sha-256`（开箱可用密码） | **`ident`** ← 必须改成 `scram-sha-256` | 🔴 **致命**：不改则 live 模式 100% 连不上 |
| 2 | **数据目录初始化** | 装包时自动完成 | 必须手工 `postgresql-setup --initdb` | 🔴 不做则服务起不来 |
| 3 | **数据目录路径** | `/var/lib/postgresql/16/main` | **`/var/lib/pgsql/data`** | 🟡 所有涉及路径的命令都要换 |
| 4 | **服务名与启动方式** | `postgresql@16-main` / `pg_ctlcluster 16 main start` | **`postgresql`** / `systemctl enable --now postgresql` | 🟡 `pg_lsclusters` 在 OL 上不存在 |
| 5 | pgvector 来源 | `apt install postgresql-16-pgvector` | **源码编译 0.8.7**（AppStream 包只有 0.6.x） | 🟡 多一步编译，脚本自动做 |
| 6 | 包管理器 | `apt` / `apt-get` | `dnf` / `yum` | 🟢 |
| 7 | SELinux | 无（AppArmor） | **Enforcing** | 🟡 源码编译插件后要 `restorecon` |
| 8 | 防火墙 | ufw（默认不拦本地） | **firewalld**（running） | 🟢 本地连接不受影响 |
| 9 | Python | 3.12 | 3.12 | 🟢 都要用 venv（PEP 668） |
| 10 | 需要订阅注册吗 | 否 | **否**（Oracle Linux 公共源免注册；RHEL 才需要） | 🟢 |

一句话：Ubuntu 装完就能连，Oracle Linux 还要两步 —— `initdb` 和改 `pg_hba`。

---

## 2. 第一步：不碰数据库，先跑通（3 分钟）

**PG 的安装脚本就在代码仓库里，所以先拿代码，再执行脚本。**

### 2.1 拿到代码 `[VM]`

```bash
cd ~
git clone https://github.com/empoGavin/schemafence.git
cd schemafence && ls
```

预期看到：

```
LICENSE  README.md  demo.py  docker-compose.yml  docs  examples  requirements.txt  schemafence  scripts
```

> 公有仓库克隆**不需要**账号密码。如果提示要密码，说明仓库还是私有的 → GitHub 仓库
> Settings → 最下方 Danger Zone → Change visibility → 改成 Public。

不用 GitHub 也可以：用 WinSCP 把整个仓库目录拖到 `/home/<你的用户名>/` 下即可。
传之前建议先把 `.git` 临时改名成 `.git_bak`，传完再改回来 —— 跨系统拷贝 `.git` 偶尔会因权限报错。

### 2.2 跑第一条命令

```bash
cd ~/schemafence && python3 demo.py
```

**不需要 `pip install` 任何东西** —— 这个脚本只用 Python 标准库，是刻意做成零依赖的，
就是为了让「克隆下来就能跑」这件事成立。

你应该看到（节选）：

```
schemafence — a read-only diagnostic agent, a reviewer, and a fence
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
  13 cases → all passed
```

**判定标准**（数字对不上就是代码没传全）：

| 数字 | 应该是 |
|---|---|
| `tables` | 7 |
| `columns` | 33 |
| `finding(s)` | 16（5 high / 9 medium / 2 low） |
| selftest | `13 cases → all passed` |

### 2.3 看完整链路：提问 → 选表 → 生成 SQL → 护栏判决

```bash
python3 demo.py --ask "total order amount for the last week?"
```

重点看这一段：

```
  question : total order amount for the last week?
  #1  shop.orders                score 6
  #2  shop.orders_archive        score 6
  #3  shop.refunds               score 4
  note : 3 candidate tables scored close together — the model picks one, and nothing in the database stops it picking the wrong one
```

这就是整个项目要处理的那类问题：几张表得分挨得很近，模型必然会挑一张，而数据库不会阻止它挑错。
`shop.orders` 是活数据，`shop.orders_archive` 是归档表 —— 挑错了，报表数字就是错的，
**而且不会报任何错**。

再试一次中文提问，对比一下「没什么可拦的」长什么样：

```bash
python3 demo.py --ask "哪个表存了退款信息？"
```

预期 `#1  shop.refunds  score 12` —— 唯一答案、分数明显拉开。

### 2.4 审一份别人写的脚本

这是不需要数据库的第二件事，也是这个项目最常用的入口：

```bash
python -m schemafence.review --rules rules/table-hygiene.json \
       --rules rules/change-review.json --ddl examples/change_script_review.sql
```

退出码是这条命令在流水线里的意义：**0** 读过且干净、**1** 读过且至少一条规则命中、
**2** 审不了（规则包没过自己的用例，或者文件里什么都没认出来）。第三个码存在的意义是：
「0 finding(s)」永远不会打印在一个没人看懂的文件上。

### 2.5 故意让护栏拦一次

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

**一条正常查询后面偷偷接一句「删表」，护栏在第一层就拦住了。**

---

## 3. 第二步：装 PostgreSQL + pgvector（3–5 分钟）

### 3.1 一条命令装完 `[VM]`

```bash
cd ~/schemafence
bash scripts/setup_pg.sh
```

脚本自动判断你用的是 `apt` 还是 `dnf`，然后做完 9 件事。**一次性跑完，中途不需要你干预。**
（Debian 系装到的 PG 版本跟着发行版走：22.04 通常是 14，24.04 通常是 16。）

| # | 步骤 | RHEL 系实际执行的命令 | 作用 |
|---|---|---|---|
| 1 | 判断包管理器 | `command -v dnf` | 选对安装方式 |
| 2 | 装 PG | `dnf -y install postgresql-server postgresql-contrib` | Oracle Linux 10 装的是 **PostgreSQL 16**（AppStream 自带） |
| 3 | 装 pgvector | **Debian 系**先试 `apt install postgresql-<版本>-pgvector`；**RHEL 系**自动装编译工具（`gcc make git postgresql-server-devel`）→ `git clone v0.8.7` → `make install` → `restorecon` | ⚠️ **默认源码编译最新版 0.8.7**；发行版没有对应版本的包时**自动降级为源码编译**（约 1 分钟）。注意：OL10 的 `postgresql-devel` 不带 `pg_config`，必须用 `postgresql-server-devel`（脚本已处理）。发行版包只有 0.6.x，默认不用 |
| 4 | **初始化数据目录** | `postgresql-setup --initdb` | ⚠️ **RHEL 系独有**。Debian 系是装包时自动初始化的，RHEL 系必须显式执行，否则服务起不来 |
| 5 | 启动服务 | RHEL 系 `systemctl enable --now postgresql`；Debian 系 `pg_ctlcluster` / `service` | ⚠️ **RHEL 系服务名是 `postgresql`**（不是 Ubuntu 的 `postgresql@16-main`）；RHEL 系还要 `systemctl`，容器里没有 systemd 时用 `service` |
| 6 | **修认证方式** | 把 `pg_hba.conf` 里的 `ident` 改成 `scram-sha-256` | ⚠️ **RHEL 系独有**，见 3.3 |
| 7 | 设密码 | `ALTER USER postgres PASSWORD 'pgvec123'` | 让程序能用密码经 TCP 连进来 |
| 8 | 建库 | `createdb fence_demo` | 新建一个空库 |
| 9 | 灌示例 + 收统计 | `psql -f examples/sample_schema.sql` + `ANALYZE;` | 建 7 张表（含 6 类真实陷阱）+ 生成 `pg_stats` |

最后看到这段就是全部成功：

```
==> done
    postgres : 16   pgvector: 0.8.7   shop tables: 7
    database : fence_demo   user: postgres   password: pgvec123

    offline demo :  python3 demo.py
    live demo    :  python3 demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo
```

> **`shop tables: 7` 和 `pgvector: 0.8.7` 这两个值必须出现。** 整行没出来或版本不对见 6.4。
>
> **脚本是幂等的** —— 中途失败的话，修完问题直接再跑一遍 `bash scripts/setup_pg.sh` 即可，
> 不会有副作用。
>
> 想用发行版自带的旧版 pgvector（省掉编译那一步）：`PGVECTOR_FROM_DIST=1 bash scripts/setup_pg.sh`。
> Cloud Studio 上如果包没有且编译失败，先跑 `apt-cache search pgvector` 看到底有没有对应版本的包。

### 3.2 手工核对 PG 装好了 `[VM]`

```bash
postgres --version
psql --version && ls -l /var/lib/pgsql/data/PG_VERSION
```

预期（小版本号会变，**只要是 16.x 就合格**）：

```
postgres (PostgreSQL) 16.10
psql (PostgreSQL) 16.10
-rw-r--r--  1 postgres postgres 3 Oct  3 14:25 /var/lib/pgsql/data/PG_VERSION
```

> `/var/lib/pgsql/data` 是 Oracle Linux 上数据库文件的存放位置（Ubuntu 上是
> `/var/lib/postgresql/16/main`）。里面那个 `PG_VERSION` 是数据目录已初始化的标志 ——
> 没有它说明第 4 步没做。

```bash
sudo -u postgres psql -d fence_demo -c "\dt shop.*"
```

预期看到 **7 行**：`orders`、`orders_archive`、`refunds`、`blacklist`、`user_profile`、
`user_events`、`users`

> **如果看到一行警告 `could not change directory to "/home/xxx": Permission denied`** ——
> 这是无害的，原因是 `postgres` 用户读不了你的家目录。想让它消失就先 `cd /tmp` 再执行命令。

### 3.3 Oracle Linux 上唯一一处「不改就必然失败」

PostgreSQL 的客户端认证由 `pg_hba.conf` 控制，两个发行版的默认值不同：

```
# Ubuntu 24.04 的默认值（开箱就能用密码连）
host    all   all   127.0.0.1/32   scram-sha-256

# Oracle Linux / RHEL 系的默认值
host    all   all   127.0.0.1/32   ident      ← 问题在这里
```

`ident` 的机制是：数据库去问客户端所在机器的 **ident 服务（113 端口）**「你是哪个用户」，
然后按回答决定放行。本机上没有 ident 服务在跑，所以这一问永远得不到答案 →
**每次用密码走 TCP 连接都会失败**。而 `demo.py --db postgresql://...@localhost:5432/fence_demo`
**正是走 TCP**，所以不改这一行，live 模式必然连不上。

**安装脚本已经自动帮你改了**（并且留了备份 `pg_hba.conf.bak.<时间戳>`）。手工核对：

```bash
sudo grep -E "^(host|local)" /var/lib/pgsql/data/pg_hba.conf
```

合格输出（注意 host 行是 `scram-sha-256`，`local` 行保持 `peer` 不动）：

```
local   all             all                                     peer
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256
local   replication     all                                     peer
host    replication     all             127.0.0.1/32            scram-sha-256
host    replication     all             ::1/128                 scram-sha-256
```

**如果还是 `ident`**，手工改：

```bash
sudo cp /var/lib/pgsql/data/pg_hba.conf /var/lib/pgsql/data/pg_hba.conf.bak.manual
sudo sed -i -E 's/^([[:space:]]*host[[:space:]]+.*[[:space:]])ident([[:space:]]*)$/\1scram-sha-256\2/' /var/lib/pgsql/data/pg_hba.conf
sudo systemctl reload postgresql
sudo grep -E "^host" /var/lib/pgsql/data/pg_hba.conf
```

> `local` 那行保持 `peer` 不动：`local` 走的是 Unix socket（`sudo -u postgres psql` 用这条），
> `peer` 通过比对操作系统用户名放行，不需要改。
>
> 不要改成 `md5`：MD5 是过时的弱算法，PostgreSQL 14 起 `scram-sha-256` 就是默认且推荐的
> 密码算法。

---

## 4. 第三步：连接真实数据库（live 模式）

前面都是对着一份 DDL 文本做静态分析。现在要连上真正跑起来的 PG，从系统目录里读
**真实结构、真实行数估算、真实的 NULL 比例**。

> **这一节与第 2 节的离线模式互不依赖。** 这里才需要 `pip install`（只装 psycopg 驱动），
> 而离线模式始终不需要。本节跑不通也不影响第 2 节的结论。
>
> **两个前提都要满足**：① PG 正在运行；② `pg_hba.conf` 的 host 行是 `scram-sha-256`
> 而不是 `ident` —— 否则会报 `password authentication failed`。

先把两件事分清楚：

| | 离线（第 2 节） | live（本节） |
|---|---|---|
| 命令 | `python3 demo.py` | `python3 demo.py --db postgresql://...` |
| 数据来源 | 一份 DDL 文本文件 `examples/sample_schema.sql` | 真实数据库的 `pg_catalog` + `pg_stats` |
| 需要装 Python 包吗 | **不需要**（零依赖，只用标准库） | 需要 `pip install -r requirements.txt`（只有 psycopg 驱动） |
| 需要数据库在跑吗 | **不需要** | **需要**（PG16 已就绪） |
| 典型输出 | `16 finding(s): 5 high / 9 medium / 2 low` | `tables read from the catalogue : 7` |
| 可以跳过吗 | 不建议（它是「克隆下来 5 分钟能跑」的证明） | 可跳过，但本节的价值在于用**真实统计信息**复核同一批检查 |

### 4.1 建一个隔离的 Python 环境 `[VM]`

```bash
cd ~/schemafence
python3 -m venv .venv
source .venv/bin/activate
```

执行后提示符前面会多出 `(.venv)`：

```
(.venv) [gavin@schemafence-pg schemafence]$
```

Oracle Linux 10（以及所有 RHEL 9+ / Ubuntu 23.04+）遵循 **PEP 668**，系统 Python 目录里有一个
`/usr/lib/python3.x/EXTERNALLY-MANAGED` 标记文件，pip 看到它就拒绝安装——你会看到
`error: externally-managed-environment`。**这不是权限问题，`sudo pip install` 一样会被拦**：
系统自己的工具（如 dnf 的 Python 插件）依赖这些 PyPI 包，直接覆盖会在几个月后把系统的包管理器弄坏。

venv 不是唯一出路，但它不需要 root、不污染系统、换机器结果一致：

| 方案 | 命令 | 特点 |
|---|---|---|
| **venv（推荐）** | `python3 -m venv .venv && source .venv/bin/activate` | 不需要 root；每个项目一个独立抽屉；换机器/换云主机都一样 |
| 系统级 RPM 包 | `sudo dnf install python3-psycopg3` | 装到系统 Python 里、由 dnf 管理。但 OL10 上这个包在 **EPEL** 里，得先 `sudo dnf install oracle-epel-release-el10`，且版本比 PyPI 旧 |
| 强行覆盖系统 Python | `sudo pip install --break-system-packages -r requirements.txt` | 加了 `--break-system-packages` 才能绕过 PEP 668。容器里随便用，正经机器上不推荐（可能和 RPM 包冲突） |

### 4.2 装 live 模式唯一需要的依赖 `[VM]`

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

预期看到：

```
Successfully installed psycopg-3.2.x psycopg-binary-3.2.x python-dotenv-1.x.x
```

> `requirements.txt` 里只有 `psycopg[binary]`（连 PG 的驱动）和 `python-dotenv`。
> `[binary]` 意味着有预编译 wheel，**不需要编译、不需要 libpq-devel**。

### 4.3 跑起来

```bash
python demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo
```

（进了 venv 之后 `python` 或 `python3` 都可以。）

### 4.4 你该看到什么

在第 2.2 那堆输出之后，会多出这么一段：

```
[live] connected to a real database
  tables read from the catalogue : 7
  columns                        : 33

  row estimates (from pg_class.reltuples, no COUNT(*))
    shop.orders                  ~5
    shop.users                   ~4
    ...

  measured NULL fractions (pg_stats) — the join-loss evidence
    shop.orders.user_id          20.0% NULL
    shop.users.email             25.0% NULL
    ...

  same checks, live data → N finding(s) (X high, Y medium, Z low)
```

**三处必须对得上**：

| 看到什么 | 说明什么 |
|---|---|
| `tables read from the catalogue : 7` | 真的读到了你刚建的 7 张表（不是读本地文件） |
| `row estimates` 有行数 | 读的是 `pg_class.reltuples` 这个**统计估算**，对表**没有执行 `COUNT(*)`** |
| `measured NULL fractions (pg_stats)` 有百分比 | 读到了 `ANALYZE` 生成的**列级统计信息** |

> **中间那一行值得多看一眼**：在大表上执行 `COUNT(*)` 会全表扫描、可能拖垮数据库。
> 这个项目用 `reltuples`（统计信息里的行数估算）代替 —— 一个体检工具不该把它检查的库压垮。
>
> **第三行就是「连接丢行」这个陷阱的物证**：`shop.orders.user_id` 有 20% 为 NULL，
> 也就是「五笔订单里有一笔，一旦 JOIN 就消失」。一旦 SQL 按它去 join 用户表，这些订单会被
> **静默丢掉** —— 结果少了一批订单，但没有任何报错。

> ⚠️ 如果这里打印的是 `could not connect: ...` →
> - 报 `ident authentication failed` 或 `no pg_hba.conf entry` → **回到 3.3**，这是 Oracle Linux 上最常见的坑
> - 报 `ModuleNotFoundError: No module named 'psycopg'` → 见 6.6
> - 报 `connection refused` → 见 6.5

### 4.5 审一份脚本（接上活库）

规则可以要求「脚本里看不出来的事实」，最典型的一条是：**不能给表加重复索引**。
脚本知道自己要建的索引，但「这张表上是不是已经有了」只有库知道：

```bash
python -m schemafence.review --rules rules/index-hygiene.json --db "$PG_DSN" --ddl release.sql
```

不带 `--db` 直接跑，它会**拒绝执行**而不是报干净 —— 规则包声明了它需要 catalog 的事实，
没读到就不算应用过。这不是报错，是它该有的行为：**规则没能被求值 = 什么都没通过**。

---

## 5. 第四步：验证

### 5.1 pgvector 真的能用（向量检索） `[VM]`

```bash
sudo -u postgres psql -d fence_demo -f scripts/verify_pgvector.sql
```

这个脚本会做四件事：

1. `CREATE EXTENSION vector` 能装上
2. 余弦距离检索 `<=>` 能返回结果（`1 - 距离 = 相似度`）
3. HNSW 索引能建起来（小表上仍走顺序扫描，正常）
4. **只读账号 `agent_ro` 建好，只有 SELECT 权限**

> **用 3 维手写向量**是为了不依赖任何 embedding API（那需要联网 + API key）。
> 这一步要验的只有一件事 —— pgvector 能装、能查、索引能建。

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

> HNSW 是 pgvector 0.5.0 才引入的索引算法。能建起来，说明插件版本足够新
> （0.6.x 和 0.8.x 都支持）。

**第 3 段 —— 只读账号**：

```
 rolname  | rolcanlogin | rolcreatedb | rolsuper
----------+-------------+-------------+----------
 agent_ro | t           | f           | f
(1 row)
```

### 5.2 只读账号物理上写不进去（七层护栏的第一层） `[VM]`

**先退出 venv 或另开一个终端**（这条用系统自带的 `psql` 就行）：

```bash
psql "postgresql://agent_ro:ro_only@localhost:5432/fence_demo" \
     -c "DELETE FROM shop.orders WHERE 1=1"
```

**预期是报错 —— 报错就说明成功**：

```
ERROR:  permission denied for table orders
```

> 2.5 那次拦截是**代码里的规则** —— 规则可以改、可以写错、可以被绕过。
> 这一条是**数据库层面的物理权限** —— 就算上层代码被绕过、就算有人直接敲 SQL，
> 这个账号也删不掉任何东西。七层护栏里这一层最硬。

### 5.3 交互式体验一下 PG（可选） `[VM]`

```bash
sudo -u postgres psql -d fence_demo
```

进入后依次试这几条（**每条以分号结尾**）：

```sql
\dt shop.*                                      -- 列出 shop 下的所有表
SELECT count(*) FROM shop.orders;                -- 数一下订单有多少条
\d shop.orders                                   -- 看 orders 表的完整定义
\du                                              -- 列出所有数据库角色
SELECT extname, extversion FROM pg_extension;    -- 看装了哪些扩展（应有 vector）
\q                                               -- 退出
```

---

## 6. 验收清单与排查

### 6.1 全流程验证清单

按顺序做完，每条打勾，才算环境配置完整：

| # | 验证什么 | 在哪跑 | 命令 | 合格标准 |
|---|---|---|---|---|
| V1 | 机器/容器在运行 | VMware 界面或 Cloud Studio 控制台 | 看列表 | `Powered On` / 工作空间可进入 |
| V2 | 能从本机登录 | `[Win]` | `ssh <用户>@<IP>` | 进入提示符 |
| V3 | 系统版本 | `[VM]` | `cat /etc/os-release \| head -2` | `Oracle Linux Server` + `10.x`（或 Ubuntu） |
| V4 | 基础工具齐全 | `[VM]` | `python3 --version && git --version` | Python 3.10+ + git 版本号 |
| V5 | 时区正确 | `[VM]` | `date` | 含 `CST`，时间与北京时间一致 |
| V6 | PG 服务在跑 | `[VM]` | `systemctl is-active postgresql` | `active` |
| V7 | PG 版本正确 | `[VM]` | `postgres --version` | `PostgreSQL 16.x` |
| V8 | 数据目录已初始化 | `[VM]` | `cat /var/lib/pgsql/data/PG_VERSION` | `16` |
| V9 | **认证方式是密码** | `[VM]` | 见 3.3 的 grep 命令 | host 行是 `scram-sha-256`，**不是 `ident`** |
| V10 | 示例库建好了 | `[VM]` | 见 3.2 | 7 张表 |
| V11 | pgvector 装好了 | `[VM]` | 见下方汇总 | `pgvector = 0.8.7`（若走了发行版包则为 0.6.x） |
| V12 | 离线审计能跑 | `[VM]` | 见 2.2 | `16 finding(s): 5 high / 9 medium / 2 low` |
| V13 | 七层护栏自测 | `[VM]` | 见 2.2 | `13 cases → all passed` |
| V14 | 护栏能拦危险 SQL | `[VM]` | 见 2.5 | `allowed: False` / `layer: L1` |
| V15 | live 模式连上库 | `[VM]` | 见 4.3 | `tables read from the catalogue : 7` |
| V16 | 读到真实统计信息 | `[VM]` | 见 4.3 | 有 `row estimates` 和 `measured NULL fractions`，例如 `shop.orders.user_id 20.0% NULL` |

**外加两条「环境健康」检查（Oracle Linux 特有）**：

| # | 验证什么 | 命令 | 合格标准 |
|---|---|---|---|
| H1 | SELinux 没有拦 PG | `sudo ausearch -m AVC -ts recent 2>/dev/null \| grep -i postgres \| head` | **没有任何输出** |
| H2 | 只读账号写不进去 | 见 5.2 | 报 `permission denied for table orders` |

**V11 汇总命令：一次跑完 5 项只读检查**

```bash
sudo -u postgres psql -d fence_demo -tAc "SELECT 'pg_version     = ' || current_setting('server_version')"
sudo -u postgres psql -d fence_demo -tAc "SELECT 'data_directory = ' || current_setting('data_directory')"
sudo -u postgres psql -d fence_demo -tAc "SELECT 'tables_in_shop = ' || count(*) FROM information_schema.tables WHERE table_schema='shop'"
sudo -u postgres psql -d fence_demo -tAc "SELECT 'pgvector       = ' || COALESCE((SELECT extversion FROM pg_extension WHERE extname='vector'),'NOT INSTALLED')"
sudo -u postgres psql -d fence_demo -tAc "SELECT 'vector_ops     = ' || count(*) FROM pg_operator WHERE oprname IN ('<=>','<->','<#>')"
```

合格输出：

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
| `pgvector` | 有版本号即合格。显示 `NOT INSTALLED` → 见 6.4 |
| `vector_ops` | `>= 3` 即合格（`<=>` 余弦、`<->` 欧氏、`<#>` 内积）。是 `0` 说明插件没加载成功 |

### 6.2 机器没 IP / ssh 连不上

```bash
# ① 网卡状态
ip -4 addr show
nmcli device status
```

- `ens33` 显示 `disconnected` → `sudo nmcli device connect ens33`
- 有 IP 但 Windows 连不上 → 在 VMware 里确认 **VM → Settings → Network Adapter** 是 **NAT**
  且勾了 **Connected** + **Connect at power on**
- 仍不通 → 查 sshd：`systemctl status sshd --no-pager | head -3`，不是 `active (running)`
  就 `sudo systemctl enable --now sshd`
- Windows 侧：`ping <虚拟机IP>`。不通说明是网络层；通但 ssh 失败说明是 sshd 层
- **IP 变了是常态**（NAT 下 DHCP）。每次连之前先 `hostname -I`
- 公司网络有管控策略时，**直接用 VMware 窗口操作**，功能完全一样

### 6.3 `dnf` / `apt` 很慢或报错

Oracle Linux 的源指向 `yum.oracle.com`（公共、免注册）：

```bash
sudo dnf repolist                              # 看看启用了哪些源
sudo dnf clean all && sudo dnf makecache       # 清缓存重建（解决元数据过期）
```

如果报无法解析域名 → 先确认网络：`ping -c 2 8.8.8.8`，再 `ping -c 2 yum.oracle.com`。
DNS 问题就检查 `/etc/resolv.conf`。国内可考虑换镜像站（把
`/etc/yum.repos.d/oracle-linux-ol10.repo` 里的 `baseurl` 域名替换即可），但**这一步不是必须的，
先在原速下试**。

### 6.4 pgvector 装不上 / `pgvector = NOT INSTALLED`

**先看包在不在**：

```bash
dnf list available pgvector postgresql*-pgvector 2>/dev/null
rpm -qa | grep -i vector
```

**情况 A：源码编译失败 / 网络连不上 GitHub。** 脚本默认源码编译 v0.8.7（为了拿到最新版）。
退回发行版包（Oracle Linux 10 AppStream 自带 0.6.x，功能够 demo 用）：

```bash
PGVECTOR_FROM_DIST=1 bash scripts/setup_pg.sh
```

或者手工 `sudo dnf -y install pgvector`。想要 0.8.x 又不想编译，两条路：

**A-1　从 PGDG 拿 0.8.x 发行版包**：

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

> ⚠️ **`restorecon` 那一行在 Oracle Linux 上不能省**。RHEL 系默认 SELinux `Enforcing`，
> `make install` 丢进去的 `.so` 文件标签可能不对，PG 会被拒绝加载，报的还是含糊的
> `could not load library`。

**情况 B：包装了，但 `CREATE EXTENSION` 报 `extension not found`。**
两个原因：① **大版本号没对上**（PG 16 就要 `pgvector_16`）；② **装完没重启服务**
（`.so` 要重启才加载）。

```bash
sudo -u postgres psql -tAc "SHOW server_version;"
sudo systemctl restart postgresql
sudo -u postgres psql -d fence_demo -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

**验证**：

```bash
sudo -u postgres psql -d fence_demo -tAc "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

### 6.5 live 模式报 `could not connect` / `connection refused`

```bash
systemctl is-active postgresql                      # ① 服务在跑吗
sudo ss -lntp | grep 5432                           # ② 端口在听吗
psql "postgresql://postgres:pgvec123@localhost:5432/fence_demo" -c "SELECT 1"   # ③ 手工连一次
```

| psql 报什么 | 原因 | 修复 |
|---|---|---|
| `FATAL: Ident authentication failed` 或 `no pg_hba.conf entry` | **还是 `ident`** | 回到 3.3 |
| `FATAL: password authentication failed` | 密码没设上 | `sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'pgvec123';"` |
| `connection refused` | 服务没起 / 端口不对 | 回 6.7 |
| `database "fence_demo" does not exist` | 库没建 | `sudo -u postgres createdb fence_demo` |

### 6.6 live 模式报 `ModuleNotFoundError: No module named 'psycopg'`

说明没进 venv，或依赖没装。**按顺序**：

```bash
cd ~/schemafence
source .venv/bin/activate
pip install -r requirements.txt
python demo.py --db postgresql://postgres:pgvec123@localhost:5432/fence_demo
```

> ⚠️ `source .venv/bin/activate` 和 `python demo.py` **必须在同一个终端会话里**。
> 关掉终端重新 ssh 进来后 `(.venv)` 会消失，必须重新 `source` 一次。
> 判断有没有进 venv：**看提示符前面有没有 `(.venv)`**。

### 6.7 PG 服务起不来

```bash
systemctl status postgresql --no-pager
journalctl -u postgresql -n 50 --no-pager
```

按报错对症：

| 报错 | 原因 | 修复 |
|---|---|---|
| `directory "/var/lib/pgsql/data" is missing or empty` | **忘了 initdb**（Oracle Linux 上最常见） | `cd /tmp && sudo postgresql-setup --initdb --unit postgresql` |
| `could not create lock file ... Permission denied` | 数据目录属主不对 | `sudo chown -R postgres:postgres /var/lib/pgsql/data` |
| `could not load library ... vector.so: Permission denied` | **SELinux 拦了源码编译的插件** | `sudo restorecon -Rv /usr/lib64/pgsql /usr/share/pgsql && sudo systemctl restart postgresql` |
| `make: pg_config: 没有那个文件或目录`（编译 pgvector 时） | OL10 的 `postgresql-devel` **不带** `pg_config` | `sudo dnf -y install postgresql-server-devel`，然后重跑 `bash scripts/setup_pg.sh` |
| `错误：无法打开当前目录：传输端点尚未连接`（dnf 安装时） | 当前目录在一个**失效的挂载点**上（如 VMware 共享文件夹） | `cd /tmp` 换到本地目录再执行 |
| `[warn] could not locate pg_hba.conf` | 数据目录 `/var/lib/pgsql/data` 是 700 权限，普通用户既测不到文件存在、也读不了内容（改写会被静默跳过，`ident` 原样留着） | 脚本用 `sudo test` / `sudo grep` 读它；仍看不到就手工核对：`sudo grep -E "^host.*ident" /var/lib/pgsql/data/pg_hba.conf` |
| 重跑时反复打印 `initialising /var/lib/pgsql/data` | 同一原因：700 目录下的 `-f` 测试失败，误判为未初始化 | 脚本用 `sudo test` 判定；手工确认：`sudo test -f /var/lib/pgsql/data/PG_VERSION && echo 已初始化` |
| `psql: 权限不够`（读 sample_schema.sql 时） | postgres 用户进不了你的家目录 | 临时绕过：`sudo -u postgres psql -q -d fence_demo < ~/schemafence/examples/sample_schema.sql` |
| `port 5432 already in use` | 有别的实例在跑 | `sudo ss -lntp \| grep 5432` 找出进程 |

### 6.8 `bash scripts/setup_pg.sh` 中途失败

脚本每一步都会打印 `==> 步骤名`，看它停在哪：

| 停在哪 | 原因 | 怎么办 |
|---|---|---|
| `==> installing postgresql` | 网络/源问题 | 见 6.3 |
| `==> installing pgvector` | 包不存在且编译失败 | 见 6.4 |
| `==> starting the cluster` | 服务起不来 | 见 6.7 |
| `==> configuring password authentication` | 找不到 pg_hba.conf | 手工按 3.3 改 |
| `==> loading the demo schema` | SQL 失败 | 先 `sudo -u postgres psql -d fence_demo -c "\dt shop.*"` 看建了几张表 |

修完问题后**直接再跑一遍**（脚本幂等）：

```bash
cd ~/schemafence && bash scripts/setup_pg.sh
```

### 6.9 其他

**每条 `sudo -u postgres psql` 都打印 `could not change directory to "/home/xxx"`** ——
无害警告，`postgres` 用户读不了你的家目录。想让它消失就先 `cd /tmp`，或用登录 shell：
`sudo -iu postgres psql -d fence_demo`。

**SELinux 相关排查**：

```bash
getenforce                                  # 应该是 Enforcing
sudo ausearch -m AVC -ts recent | tail -30   # 看最近的拒绝记录
sudo semanage boolean -l | grep -i postgres  # 看相关布尔值
```

> `semanage` 不在的话先 `sudo dnf -y install policycoreutils-python-utils`。
> **本文档的流程不需要关 SELinux。** 真遇到了被拦的情况，优先用 `restorecon` 修标签，
> 而不是 `setenforce 0` —— 关掉只是把问题藏起来。

**Cloud Studio 上工作空间重启后数据库没了** —— 容器文件系统不保证持久化，重跑
`bash scripts/setup_pg.sh` 即可（脚本幂等）。**「灌进去」和「存住了」是两件事**，
要长期留住得把 PGDATA 放到持久化卷上。

**容器里（Cloud Studio 等没有 systemd 的环境）的报错对照**：

| 现象 | 原因 | 处理 |
|------|------|------|
| `systemctl: command not found` | 容器里没有 systemd | 正常。用 `pg_ctlcluster 16 main start` 或 `sudo service postgresql start` |
| `psql: FATAL: role "root" does not exist` | 直接用当前用户连了库 | 必须 `sudo -u postgres psql` |
| `could not change directory to "/root"` | postgres 用户读不了当前目录 | 无害警告，忽略；或在 `/tmp` 下执行 |
| `apt install postgresql-XX-pgvector` 找不到 | 发行版版本太老 | 脚本会自动降级为源码编译；也可先 `apt-cache search pgvector` |
| live 模式连不上库 | 密码没设 / 集群没起来 | `sudo pg_lsclusters` 看状态，`sudo -u postgres psql -c "SELECT 1"` 测连通 |
| `pg_stats` 查出来是空的 | 没做过 ANALYZE | `sudo -u postgres psql -d fence_demo -c "ANALYZE;"` |
| `vector` 类型不存在 | 扩展没建在 `fence_demo` 库上 | `sudo -u postgres psql -d fence_demo -c "CREATE EXTENSION vector;"` |
| 工作空间重启后数据库没了 | 容器文件系统不保证持久化 | 重跑 `bash scripts/setup_pg.sh`（幂等） |

**集群到底起来没有**：`sudo -u postgres psql -c "SELECT version();"` 有输出就算起来了。

---

## 7. 收尾：快照、额度、日常开关

### 7.1 走 A 路：立刻拍一个快照

现在这个状态是「干净可用」的。在 VMware 菜单 **VM → Snapshot → Take Snapshot...**：

| 字段 | 填 |
|---|---|
| Name | `baseline-pg16-pgvector-demo` |
| Description | `Oracle Linux 10 + PG16 + pgvector + fence_demo 已就绪` |

> 以后实验搞乱了 —— 改错配置、删错表、升级失败 —— 只要 **VM → Snapshot → Revert to Snapshot**
> 选这个快照，**一分钟就回到现在这个状态**，什么都不用重装。

日常开关：

| 想做什么 | 怎么做 |
|---|---|
| 暂时不用，下次接着用 | VMware 菜单 **VM → Power → Suspend** |
| 完全退出 | 虚拟机里 `sudo poweroff`；或 **VM → Power → Shut Down Guest** |
| 下次开机 | VMware 里选中虚拟机 → **Power On** |
| 只操作数据库，不打开 VMware 窗口 | VMware 保持运行（可最小化）→ 从 Windows `ssh <用户>@<IP>` |

`systemctl enable --now postgresql` 已经做了开机自启，确认一下：

```bash
systemctl is-enabled postgresql          # 预期 enabled
systemctl status postgresql --no-pager | head -5   # 应看到 Active: active (running)
```

**需要开防火墙吗？不需要。** PG 默认只监听 `localhost`（127.0.0.1），而 `demo.py` 也是在这台机器
内部连接的：

```bash
sudo ss -lntp | grep 5432
```

预期只看到 `127.0.0.1:5432`，**没有** `0.0.0.0:5432`：

```
LISTEN 0  244  127.0.0.1:5432  0.0.0.0:*  users:(("postgres",pid=1234,fd=6))
```

> **这个输出本身就是一条安全验证**：数据库没有对网络暴露。

### 7.2 走 B 路：记得停工作空间

**回控制台停止工作空间**，不停止会继续消耗额度。

### 7.3 想从 Windows 用客户端连过去（可选，一般不需要）

默认**不建议**开。如果确实想用 DBeaver 之类的客户端连过去看看，三处都要改：

```bash
# ① 让 PG 监听所有地址
sudo sed -i "s/^#\?listen_addresses.*/listen_addresses = '*'/" /var/lib/pgsql/data/postgresql.conf

# ② 允许你的网段（把 192.168.181.0/24 换成实际网段）
echo "host  all  all  192.168.181.0/24  scram-sha-256" | sudo tee -a /var/lib/pgsql/data/pg_hba.conf

# ③ 开防火墙
sudo firewall-cmd --permanent --add-service=postgresql
sudo firewall-cmd --reload

# ④ 重启
sudo systemctl restart postgresql
```

> ⚠️ `listen_addresses = '*'` **暴露在网络上**是运维大忌。只在实验机器上用，做完改回 `localhost`。

---

## 附录 A：资源调整

PG 的资源占用很低（示例库不到 10MB，2 核 4G 跑得很宽裕）。要调整：

| 情况 | 怎么做 |
|---|---|
| 内存不够 | **关机**后 **VM → Settings → Memory** → 调到 8192 MB（主机 16GB，最多给 8GB） |
| 核数不够 | **VM → Settings → Processors** → 调到 4 |
| 磁盘不够 | **VM → Settings → Hard Disk → Expand** 扩到 60GB，然后进虚拟机执行：<br>`sudo growpart /dev/sda 3 && sudo xfs_growfs /`（Oracle Linux 默认**文件系统是 XFS**，用 `xfs_growfs`，不是 Ubuntu 的 `resize2fs`） |

> 调整内存/核数**必须先关机**（挂起状态改不了）。

**每步耗时参考**：`dnf upgrade` 2–8 分钟 · 装 PG + pgvector 2–5 分钟 · 克隆代码 <1 分钟 ·
venv + psycopg 1–2 分钟。**合计约 30–45 分钟。**

## 附录 B：想用更新的 PG / pgvector（PGDG 路径）

Oracle Linux 10 自带的 PG 是 16，pgvector 源码装到了 0.8.7。想再跟上游主要是换 PG 主版本：

- **pgvector 0.8.x** 新增了迭代式索引扫描（`hnsw.iterative_scan`），在「带 WHERE 过滤的向量检索」
  上明显更好 —— schemafence 的知识层检索正好属于这类场景
- **PG 17 / 18** 有增量排序、`pg_stat_io` 等改进

用 PGDG 仓库替换成新版（PGDG 对 EL10 官方支持）：

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

> ⚠️ **走这条路的话，`scripts/setup_pg.sh` 后面的步骤要手工补**：脚本是针对发行版自带的
> `postgresql` 服务写的。PGDG 安装的版本服务名是 `postgresql-17`、数据目录是
> `/var/lib/pgsql/17/data`、`pg_config` 在 `/usr/pgsql-17/bin`。
>
> **建议**：先用发行版自带的路径把整条链路跑通，确认理解了每一步在干什么之后，再考虑换版本。
> 不要一上来就同时引入「没跑通过」和「第三方仓库」两个变量 —— 出了问题分不清是谁的锅。

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

*适用范围：Oracle Linux 10 / RHEL 系、Ubuntu 22.04+（含 Cloud Studio 容器），核对于 2026-10-16。
示例输出取自 `examples/sample_schema.sql`（5 笔订单 / 4 个用户）。*
