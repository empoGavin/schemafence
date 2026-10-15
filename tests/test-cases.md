# schemafence 测试用例

范围：离线路径（无 API key、无 PostgreSQL、无网络）。live/数据库/API 嵌入相关用例不在本表，
另见 `tests/report-2026-10-06.md` 的「遗留手动验证项」。

- 环境：Windows，Python 3.13.14，仓库根 = `<repo>`，`python demo.py` 与 `python agent_cli.py` 均指本机 Python。
- 复现器：`python tests/qa_probe.py`（零依赖，逐条打印 PASS/FAIL，任一失败退出码 1，临时产物写 `tests/tmp/` 且已 gitignore）。
- 结果列含义：`实际` 为命令/断言的可判定输出；`结果` 为修复后的最终状态，带「首跑」标记的用例首轮为 FAIL，修复后转 PASS（见报告 BUG-1/2/3）。

## 汇总

| 维度 | 用例数 | 首跑失败 | 修复后 |
|---|---|---|---|
| A. guard 闸门 | 25 | 4（A21–A24） | 25 PASS |
| B. 路由 | 8 | 0 | 8 PASS |
| C. 知识检索 | 12 | 1（C7） | 12 PASS |
| D. CLI 行为 | 19 | 4（D14–D17） | 19 PASS |
| E. 工具层 | 9 | 0（E8 首跑为用例自身缺陷） | 9 PASS |
| F. 评测基线 | 2 | 0 | 2 PASS |
| G. 产物完整性 | 5 | 1（G5，总数字段未回填） | 5 PASS |
| H. 规模语料生成器 | 4 | 0 | 4 PASS |
| I. 隔离子进程 | 2 | 0 | 2 PASS |
| J. SQL 语义检查 | 11 | 0 | 11 PASS |
| K. 规则包 | 10 | 0 | 10 PASS |
| L. 规则推断与事实归一 | 12 | 1（L0 首跑断言写错） | 12 PASS |
| M. 活库事实与索引归一 | 10 | 1（M1 首跑断言写错） | 10 PASS |
| **合计** | **129** | **12** | **129 PASS** |

C11、C12、D19 是后来补的，随 `--rebuild` 与孤儿 source 检测一起进来（见 §C 与 §D 末尾）。
I1、I2 随「harness 不该继承操作者 key 文件」的修复一起进来（见 §I）。
J0–J10 随 `checks_sql.py` 一起进来（见 §J）。
K0–K9 随规则包（`schemafence/rulepack.py`、`rules/*.json`、`python -m schemafence.review`）
一起进来（见 §K）。
L0–L11 随事实注册表（`schemafence/facts.py`）与 `--infer` 一起进来（见 §L）。L0 首跑是
**用例自身写错**：断言「整行相同」，而第四个写法确实多了一条 `ALTER` 动作，`change_count`
本就该不同；归一的是规则读的那个事实，不是整行。
M0–M9 随 `catalog` 事实模型（`snapshot.py` 的 `build_schema`/`apply_script`）与
`rules/index-hygiene.json` 一起进来（见 §M）。M1 首跑同样是**用例自身写错**：把
`CREATE UNIQUE INDEX` 也当成「主键/唯一约束的又一种写法」，可它不挂约束——`from_constraint`
本来就该是 `False`。规格该说的是「同一列同一序同一个索引」，「有没有约束」是另一件事。

## A. guard 闸门（七道闸门正反例 + 绕过）

前置：`python -c "from schemafence.guard import guard"`，或等价地在 `tests/qa_probe.py` 的 `guard_cases()` 中执行。

| 编号 | 维度 | 前置条件 | 步骤（命令 / 输入） | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| A0 | guard/L1–L7 | 无 | `run_selftest()`（demo.py 内 13 例） | 13 例全 PASS，`all_passed=True` | `13 cases → all passed` | PASS |
| A1 | L1 单语句 | 无 | `guard("SELECT 1; DROP TABLE shop.users")` | `ok=False`, `layer=L1` | `ok=False layer=L1` | PASS |
| A2 | L1 注释绕过 | 无 | `guard("SELECT 1 -- \n; DROP TABLE shop.users")` | `ok=False`, `layer=L1` | `ok=False layer=L1` | PASS |
| A3 | L1 空语句 | 无 | `guard("   ")` | `ok=False`, `layer=L1` | `ok=False layer=L1` | PASS |
| A4 | L2 放行 | 无 | `guard("SELECT 1")` | `ok=True` | `ok=True` | PASS |
| A5 | L2 DDL | 无 | `guard("DROP TABLE shop.orders")` | `ok=False`, `layer=L2` | `ok=False layer=L2` | PASS |
| A6 | L2 大小写混淆 | 无 | `guard("  delete from shop.orders")` | `ok=False`, `layer=L2` | `ok=False layer=L2` | PASS |
| A7 | L2 会话写 | 无 | `guard("SET search_path TO evil")` | `ok=False`, `layer=L2` | `ok=False layer=L2` | PASS |
| A8 | L3 CTE 藏 DML | 无 | `guard("WITH d AS (DELETE FROM shop.orders RETURNING *) SELECT * FROM d")` | `ok=False`, `layer=L3` | `ok=False layer=L3` | PASS |
| A9 | L3 CTE 藏 UPDATE | 无 | `guard("WITH u AS (UPDATE shop.orders SET amount=0 RETURNING *) SELECT * FROM u")` | `ok=False`, `layer=L3` | `ok=False layer=L3` | PASS |
| A10 | L4 危险函数 | 无 | `guard("SELECT pg_sleep(60)")` | `ok=False`, `layer=L4` | `ok=False layer=L4` | PASS |
| A11 | L4 文件函数 | 无 | `guard("SELECT lo_import('/etc/passwd')")` | `ok=False`, `layer=L4` | `ok=False layer=L4` | PASS |
| A12 | L5 白名单放行 | 无 | `guard("SELECT * FROM shop.orders", table_whitelist=["orders"])` | `ok=True`（裸名归一化后命中） | `ok=True` | PASS |
| A13 | L5 白名单拦截 | 无 | `guard("SELECT * FROM secret.creds", table_whitelist=["orders"])` | `ok=False`, `layer=L5` | `ok=False layer=L5` | PASS |
| A14 | L6 强制 LIMIT | 无 | `guard("SELECT * FROM shop.orders")` | `ok=True`, `rewritten=True`, `sql` 含 `LIMIT 100` | `SELECT * FROM shop.orders\nLIMIT 100` | PASS |
| A15 | L6 已有 LIMIT | 无 | `guard("SELECT * FROM shop.orders LIMIT 5")` | `ok=True`, `rewritten=False` | `rewritten=False` | PASS |
| A16 | L7 审计（放行） | 无 | `guard("SELECT 1")` | `audit.decision="allowed"`，`session_sql` 三句 | `allowed` + 3 句 session SQL | PASS |
| A17 | L7 审计（拦截） | 无 | `guard("DROP TABLE t")` | `audit.decision="blocked"`, `audit.layer="L2"` | `blocked / L2` | PASS |
| A18 | 行注释不可藏关键字 | 无 | `guard("SELECT * FROM shop.orders -- ; DROP TABLE shop.users")` | `ok=True`（注释先剥离，合法查询不误杀） | `ok=True` | PASS |
| A19 | 块注释不可藏关键字 | 无 | `guard("SELECT /* DROP TABLE shop.users */ 1")` | `ok=True` | `ok=True` | PASS |
| A20 | 字符串字面量不误杀 | 无 | `guard("SELECT 'DROP TABLE t' AS s")` | `ok=True` | `ok=True` | PASS |
| A21 | 写意图 SELECT | 无 | `guard("SELECT * INTO shop.orders_bak FROM shop.orders")` | `ok=False`（CTAS 是写） | 首跑 `ok=True`（FAIL）→ 修复后 `layer=L3 forbidden keyword: INTO` | PASS |
| A22 | 序列写 | 无 | `guard("SELECT nextval('shop.orders_id_seq')")` | `ok=False` | 首跑 `ok=True`（FAIL）→ 修复后 `layer=L4 dangerous function: nextval` | PASS |
| A23 | 序列写 | 无 | `guard("SELECT setval('shop.orders_id_seq', 1)")` | `ok=False` | 首跑 `ok=True`（FAIL）→ 修复后 `layer=L4 dangerous function: setval` | PASS |
| A24 | 加锁读 | 无 | `guard("SELECT * FROM shop.orders FOR SHARE")` | `ok=False`（非只读） | 首跑 `ok=True`（FAIL）→ 修复后 `layer=L2 locking clause: FOR SHARE` | PASS |

## B. 路由（写请求 vs 疑问句）

前置：`route(q)` 返回 `(calls, reasons)`；`calls[0][0]` 为 `__refuse__` / `__clarify__` / 工具名。

| 编号 | 维度 | 前置条件 | 步骤 | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| B0 | 路由回归 | 无 | `run_route_selftest()` | 10 例全 PASS | `10 cases → all passed` | PASS |
| B1 | 写祈使 | 无 | `route("帮我删掉 orders 表 2024 年之前的数据")` | 首选 `__refuse__`，理由含 "asks for a write" | `__refuse__` + 理由 | PASS |
| B2 | 写词 + 疑问 | 无 | `route("truncate 大表会锁多久？")` | 首选 `search_docs`，不拒绝 | `search_docs` + "phrased as a question" | PASS |
| B3 | 写词 + 吗/？ | 无 | `route("把参数从配置文件里删掉和设成 0，效果一样吗？")` | 首选 `search_docs` | `search_docs` + 理由 | PASS |
| B4 | ACT_REQUEST | 无 | `route("请给我建表语句")` | 首选 `__refuse__` | `__refuse__` | PASS |
| B5 | 写词 + 疑问 | 无 | `route("建表时要注意什么")` | 首选 `search_docs` | `search_docs` | PASS |
| B6 | 模糊问题 | 无 | `route("我不确定")` | 首选 `__clarify__` | `__clarify__` | PASS |
| B7 | 理由文案 | 无 | `route("索引没被用上，怎么排查？")` | `search_docs` 且 reason 非空 | `search_docs` + "about practice" | PASS |

## C. 知识检索（离线召回 / 地板 / 覆盖 / 降级）

前置：语料 `examples/knowledge-dba`（14 篇 / 40 片）。

| 编号 | 维度 | 前置条件 | 步骤 | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| C1 | 离线召回 | 已 ingest | `Toolbox(store).search_docs("死元组占比多少需要处理膨胀？", k=5)` | `ok=True` 且 `len(hits)>=1` | `hits=5` | PASS |
| C2 | 地板按模式 | 离线 embedder | 同上，读 `data.min_score` | 离线默认地板 = 0.0 | `0.0` | PASS |
| C3 | 地板按模式 | embedder.mode="api" | 构造 api 模式 embedder（mock `one()`）后 `search_docs` | 地板 = 0.45 | `0.45` | PASS |
| C4 | 高地板生效 | 无 | `Toolbox(doc_min_score=0.99).search_docs(...)` | `ok=True`，`hits` 空，note 含 "no matching note" | 空 hits + note | PASS |
| C5 | env 覆盖 | `SF_DOC_MIN_SCORE=0.99` | `Toolbox().search_docs(...)` 读 `min_score` | `min_score=0.99` | `0.99` | PASS |
| C6 | k 上限 | 无 | `search_docs("x", k=99)` | 回显 `k=20` | `20` | PASS |
| C7 | k 下限 | 无 | `search_docs("x", k=0)` | 回显 `k=1`（文档下限） | 首跑 `k=5`（FAIL）→ 修复后 `k=1` | PASS |
| C8 | 标题内联切片 | 无 | `chunk_markdown("# 判定指南\n\n## 治理标准\n死元组占比...")` | 至少一片 content 含标题词「治理标准」 | 命中 | PASS |
| C9 | 空 store 降级 | `JsonStore` 无 chunk | `Toolbox(empty_store).search_docs("x", k=3)` | `ok=True`，hits 空，note 提示语料无匹配 | 空 hits + note | PASS |
| C10 | 无 store 降级 | `store=None` | `Toolbox(store=None).search_docs("x")` | `ok=False`，error 含 "not loaded"，不抛异常 | `knowledge store not loaded — run --ingest first` | PASS |
| C11 | 孤儿 source 判定 | 手工构造含 `a.md`/`b.md` 的 store | `orphan_sources(store, [只有 a.md 的 chunks])` | 返回 `['b.md']`（库里有、本次语料没有的） | `['b.md']` | PASS |
| C12 | 孤儿判定不误报 | 同上 | `orphan_sources(store, 全部 chunks)` | 返回 `[]` | `[]` | PASS |

## D. CLI 行为（ingest / ask / eval / report / 坏参数）

前置：临时 store = `tests/tmp/dba.json`（先 `--ingest examples/knowledge-dba`）；`--trace tests/tmp/qa-trace.jsonl`。

| 编号 | 维度 | 前置条件 | 步骤（命令） | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| D1 | ingest | 无 | `agent_cli.py --ingest examples/knowledge-dba --store tests/tmp/dba.json` | 退出 0，生成 store 文件 | `rc=0`，文件存在 | PASS |
| D2 | ask 知识题 | 已 ingest | `--ask "表膨胀怎么处理？"` | 退出 0，输出含 `search_docs` 与 `From the knowledge base:` | 命中 | PASS |
| D3 | ask 写请求 | 已 ingest | `--ask "帮我删掉 orders 表"` | 退出 0，输出 `I cannot do that` | 命中 | PASS |
| D4 | ask 数字题 | 无 db | `--ask "有几条订单？"` | 退出 0，提示 `no database attached` | 命中 | PASS |
| D5 | eval 汇总 | 已 ingest | `--eval --eval-file eval/questions-dba.md` | `questions : 44`，`hit rate : 97.7%` | `44 / 97.7%` | PASS |
| D6 | report 指定路径 | 已 ingest | `--report tests/tmp/report-gen.md ...` | 退出 0，写指定文件，且不触碰 `eval/report-dba.md` | 生成；`git status eval/` 为空 | PASS |
| D7 | 缺参数 | 无 | `agent_cli.py`（无子命令） | 打印 help，退出 0 | `rc=0`，含 usage | PASS |
| D8 | ask 空 store | store 不存在 | `--ask x --store tests/tmp/does-not-exist.json` | 退出 2，提示 `the store is empty` | `rc=2` | PASS |
| D9 | eval 缺题库 | 无 | `--eval --eval-file tests/tmp/nope.md` | 退出 2，提示 `no questions found` | `rc=2` | PASS |
| D10 | 坏 float | 无 | `--ask x --min-score abc` | argparse 报错，退出 2 | `rc=2` | PASS |
| D11 | 坏枚举 | 无 | `--ask x --llm bogus` | argparse 报错，退出 2 | `rc=2` | PASS |
| D12 | LLM 无 key 降级 | 无 `SF_LLM_API_KEY` | `--ask ... --llm openai` | 提示回退规则路由，`driver: rules` | 命中 | PASS |
| D13 | LLM 死端点降级 | `SF_LLM_BASE_URL=http://127.0.0.1:9/v1`，超时 2s，重试 0 | `--ask ... --llm openai` | 退出 0，无栈回溯，输出 `became unreachable` + 证据报告 | 命中，约 2s | PASS |
| D14 | ingest 坏路径 | 无 | `--ingest tests/tmp/no-such-dir` | 退出非 0、无栈回溯、提示 `no such corpus` | 首跑 traceback（FAIL）→ 修复后 `rc=2` 一行错误 | PASS |
| D15 | 坏 search-path | 无 | `--search-path 'bad;drop'` | 退出非 0、无栈回溯、提示 search-path | 首跑 traceback（FAIL）→ 修复后 `rc=2` | PASS |
| D16 | api 无 key | 未设 `SF_EMBED_API_KEY` | `--mode api` | 退出非 0、无栈回溯、提示 `SF_EMBED_API_KEY` | 首跑 traceback（FAIL）→ 修复后 `rc=2` | PASS |
| D17 | report 坏语料目录 | 无 | `--report ... --corpus tests/tmp/no-such-dir` | 退出非 0、无栈回溯 | 首跑 traceback（FAIL）→ 修复后 `rc=2` | PASS |
| D18 | 多次 --ask | 已 ingest | `--ask "表膨胀怎么处理？" --ask "帮我删掉 orders 表"` | 两题都处理（`[ask]` 出现 2 次） | 命中 | PASS |
| D19 | `--rebuild` 在 JSON 后台 | 已 ingest | `--ingest ... --rebuild` | 退出 0，打印 "nothing to empty"（JSON 整体替换，无需清表） | `rc=0` + "nothing to empty — this backend is replaced whole" | PASS |

## E. 工具层（selftest / explain 参数化 / search_path）

| 编号 | 维度 | 前置条件 | 步骤 | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| E0 | 工具回归 | 无 | `run_tool_selftest()` | 7 例全 PASS | `7 cases → all passed` | PASS |
| E1 | explain 参数化 | 无 | mock 连接下 `explain_sql("SELECT 1")` | 执行串以 `EXPLAIN (COSTS ON, VERBOSE OFF)` 开头 | `EXPLAIN (COSTS ON, VERBOSE OFF) \| SELECT 1 \| LIMIT 100` | PASS |
| E2 | explain 过 guard | 无 | mock 连接下 `explain_sql("DROP TABLE t")` | `ok=False`，`layer=L3` | `forbidden keyword: DROP` | PASS |
| E3 | search_path 补 public | 无 | `Toolbox(search_path="shop")` 执行查询 | 执行 `SET search_path TO shop, public` | 命中 | PASS |
| E4 | public 不重复 | 无 | `Toolbox(search_path="shop, public")` | 仍为 `shop, public`（不重复追加） | 命中 | PASS |
| E5 | 未知工具 | 无 | `call("definitely_not_a_tool", {})` | `ok=False`,`decision="unknown-tool"` | 命中 | PASS |
| E6 | 限定名解析 | 无 | `get_table_stats(table="shop.orders")` | 参数拆成 `schema=shop, table=orders` | 命中 | PASS |
| E7 | 坏参数 | 无 | `get_table_stats(bogus=1)` | `decision="bad-arguments"` | 命中 | PASS |
| E8 | 无库报告 | 无 db | `Toolbox().run_sql("SELECT 1")` | `ok=False`,`blocked=False`,error 含 `no database` | 命中 | PASS |

## F. 评测基线

| 编号 | 维度 | 前置条件 | 步骤 | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| F1 | DBA 基线 | 已 ingest knowledge-dba | `--eval --eval-file eval/questions-dba.md` | 命中 ≥ 43/44（97.7%） | 43/44 = 97.7%，仅 1 题 MISS（`磁盘还有一半空间，为什么还要提前扩容？`） | PASS |
| F2 | knowledge 基线 | 已 ingest examples/knowledge | `--eval`（16 题） | 100% | 16/16 = 100.0% | PASS |

## G. 产物完整性（七层套件分两条命令写同一路径）

复现的是「终端全绿、留下的文件残缺」这类缺陷：`--suite guard` 不需要库、`--suite checks --db` 需要，
所以天然是两条命令，而两条命令默认共用 `bench/layers.json`。第二条曾把第一条覆盖掉，且**没有任何地方报错**。
本组用 `tests/tmp/layers-split.json` 复现两步序列，再读文件断言「文件里的行数」与「total 字段」自洽。

| 编号 | 维度 | 前置条件 | 步骤 | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| G1 | guard 单跑 | 无 | `test_layers.py --suite guard --out tests/tmp/layers-split.json` | 退出 0 | `rc=0` | PASS |
| G2 | checks 覆盖同路径 | G1 完成 | `test_layers.py --suite checks --out <同上>` | 退出 0 | `rc=0` | PASS |
| G3 | 不丢前一套件 | G1+G2 | 读文件的 `suites.guard` | 44 行（曾被覆盖成 0 行） | `guard rows=44` | PASS |
| G4 | 后一套件也在 | G1+G2 | 读文件的 `suites.checks` | 18 行 | `checks rows=18` | PASS |
| G5 | total 描述文件本身 | G1+G2 | 读文件的 `total` | 等于 44+18=62（曾为 `None`） | `total=62 rows=62` | PASS |

G5 做过变异验证：删掉 `merge_into_existing()` 里的 `"total": len(fresh)` 一行后，
用例报 `FAIL G5 total=None rows=62`，79/80——说明它咬的正是这个缺陷，不是恒真断言。

## H. 规模语料生成器（资源基准的输入）

`examples/knowledge-scale/` 是生成物、不入库（`.gitignore`），
所以**生成器本身才是要锁住的产物**。本组验两条 runbook 承诺：篇数符合要求、
生成的笔记能被正常切片（片数决定基准的预期）。

| 编号 | 维度 | 前置条件 | 步骤 | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| H1 | 篇数 | 无 | `make_scale_corpus.py --notes 60 --out tests/tmp/scale-corpus` | 退出 0，产出 60 个 `.md` | `rc=0 files=60` | PASS |
| H2 | 合规声明 | H1 完成 | 读任一生成文件 | 含「合成语料」「不含任何公司内部信息」 | 命中 | PASS |
| H3 | 重复运行不残留 | H1 完成 | 同目录改用 `--notes 20` 再跑 | 只剩 20 个文件（旧的更大的那批被清掉） | `files=20` | PASS |
| H4 | 可切片 | H1 完成 | 对生成文件跑 `chunk_markdown` | 片数 ≥ 篇数（不是每篇一个空片） | `chunks=40`（20 篇） | PASS |

H3 的存在理由：基准会**照单全收目录里的文件**。若小规模重跑留着上一轮的大批文件，
测出来的规模就不是命令里写的那个，而且不会有任何报错。

## I. 隔离子进程（harness 只按自己的设置跑）

`agent_cli.py` 启动时会读 `~/.schemafence.env`（`load_env_file`）。那个文件是操作者的，
不是 harness 的：里面有 `SF_EMBED_API_KEY` 时嵌入器切到 API，`--eval` 就从 44 次哈希查表
变成 44 次 HTTP 往返——开发机上没有这个文件、D5 只要 1.7 s；装了 key 的 Linux VM 上
直接撞穿子进程上限，终端只剩一句 timeout，看不出是哪条跑出来的。所以 `child_env()`
给每个子进程一个空 home（`HOME` + `USERPROFILE`）并丢掉继承来的 `SF_*`，本组锁的就是它。

| 编号 | 维度 | 前置条件 | 步骤（命令 / 输入） | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| I1 | 空 home | 无 | `--ingest examples/knowledge --store tests/tmp/hermetic.json`，读输出里的 `env file` 行 | 找的是 `<repo>/tests/tmp/home/.schemafence.env` 且报告 not found | 命中 | PASS |
| I2 | shell 里的 key | 调用方 `SF_EMBED_API_KEY` / `SF_EMBED_BASE_URL` 已导出 | 同上 | `rc=0`，`embedding : offline / hashed-lexical` | `rc=0` | PASS |

反向验证：把带 `SF_EMBED_API_KEY` 的 `.schemafence.env` 放进一个假 home，用
`USERPROFILE=<假 home> HOME=<假 home>` 跑整个 harness——修复前 D1/D2/D5/D6/D16/D19
与 F1/F2 共 8 条 FAIL（端点指死端口时是 `rc=2`；指真端点时就是 VM 上的那次超时），
修复后 86/86（当时的用例总数；加 §J 与 §K 之后总数是 107）。

## J. SQL 语义检查（`checks.py` 与 `guard.py` 之间那一层）

`checks.py` 只吃 Schema、`guard.py` 只吃 SQL，各自都对，所以各自都看不见这两类错误：
查询自己合法、Schema 自己合法、回来的数却是错的。`check_sql(schema, sql)` 把两者叠起来看，
**只报告、不裁决**——放不放行仍然只由 `guard()` 决定。

| 编号 | 维度 | 前置条件 | 步骤（命令 / 输入） | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| J0 | 内置自检 | `SQL_SAMPLE_DDL` 可解析 | `run_sql_selftest()` | 12 例全过 | 12/12 | PASS |
| J1 | S1 命中 | 同上 | `SUM(o.paid_amount)` 跨 `JOIN order_items` | 1 条 S1/high，subject 同时点出度量与子表 | `SUM(o.paid_amount) x shop.order_items` | PASS |
| J2 | S3 命中 | 同上 | `LEFT JOIN refunds ... WHERE r.refund_amount > 0` | 1 条 S3/high，subject 带列名 | `shop.refunds.refund_amount tested in WHERE` | PASS |
| J3 | S3 反例 | 同上 | `... WHERE r.id IS NULL` | 无 finding（反连接惯用法） | `no finding` | PASS |
| J4 | S1 反例 | 同上 | `SUM(oi.line_amount)`（度量就在膨胀侧） | 无 finding | `no finding` | PASS |
| J5 | S1 反例 | 同上 | 同上 + `GROUP BY oi.order_id` | 无 finding，且有一条 grain note | `... aggregated at the grain of shop.order_items` | PASS |
| J6 | 唯一性反例 | 同上 | `JOIN subscription s ON o.id = s.order_id`（`UNIQUE`） | 无 finding | `no finding` | PASS |
| J7 | 弃权 | 同上 | 聚合与连接都在括号子查询里 | 无 finding，note 说明只读一层 | `the statement has a nested query ...` | PASS |
| J8 | 弃权 | 两张表同名列 | `SUM(amount)` 跨 `s.a` / `s.b` | 无 finding，note 报归属数 | `SUM(amount) belongs to 2 tables ...` | PASS |
| J9 | 边界 | 同上 | `DELETE FROM shop.orders WHERE id = 1` | 语义层 0 finding（它不裁决），`guard()` 仍拦 | `guard_ok=False findings=0` | PASS |
| J10 | demo 接线 | 无 | `python demo.py` | `rc=0`，输出含 `[sql-checks]` 与 `12 cases → all passed` | `rc=0` | PASS |

J4/J5/J6 是这一层最容易被写坏的三个反例：**膨胀侧自己的度量不算被膨胀**、
**已经按子表粒度聚合的不算被膨胀**、**连接键唯一的不算膨胀**。J8 与 J9 是两条纪律：
解析不出来宁可弃权（记 note），语义层永远不碰执行路径。

自检里另有三例钉子句边界（`WHERE` 后面跟 `GROUP BY` / `ORDER BY` / `LIMIT`）。
`_clause_body` 曾经把 `found[i + 1].start` 当成整数用，漏了调用括号，于是**只要
`WHERE` 不是最后一个子句就抛 `TypeError`** —— 真实查询里这是常态，而当时的九个用例
恰好都以 `WHERE` 结尾，所以 S1/S3 在大多数语句上其实是崩的。这三例就是那次修复的门闩。

## K. 规则包（一条审核规则是一个文件，外加它自己的用例）

规则从 Python 函数里搬到一个 JSON 文件里。**一个规则的用例不是提示词，是验收**：
`load_rulepack(verify=True)` 会先跑这个规则自己带的 `fires` / `silent` 例子，任何一条
对不上就拒收整个文件。这样「新增规则只写文件、不改代码」的前提才立得住——能立刻用，
是因为文件与结论之间除了求值器没有别的东西；能信，是因为文件必须自己证明自己。

另外两条纪律：**规则选择器匹配到 0 行必须出声**（0 不是通过），**整份文件没被读懂必须
退出码 2**（不许打印 `0 finding(s)` 然后退 0，那读起来就是批准）。

| 编号 | 维度 | 前置条件 | 步骤（命令 / 输入） | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| K0 | 内置自检 | `rules/` 下三个包存在 | `run_rulepack_selftest()` | 15 例全过 | 15/15 | PASS |
| K1 | 接线 | 两个已发布的包 | `--rules table-hygiene --rules change-review --ddl examples/change_script_review.sql` | 两个包各自 `examples verified`，9 条 finding，`worst high`，rc=1 | rc=1 | PASS |
| K2 | 自证 | 同上 | `--rules table-hygiene --verify` | 14 例全过，rc=0 | `14 example(s) → all passed` | PASS |
| K3 | 词表导出 | 无 | `--vocabulary` | rc=0，JSON 含四个 target 与 `authoring_contract` | `targets=['change','column','index','table']` | PASS |
| K4 | 反例 | 一份每条规则都满足的脚本 | `--rules table-hygiene --ddl k-clean.sql` | 0 finding，rc=0，仍打印每个规则匹配了多少行 | rc=0 | PASS |
| K5 | 边界 | 一份没人读得懂的脚本（`\copy` / `VACUUM FULL`） | `--ddl k-blind.sql` | rc=2，打印 `NOTHING RECOGNISED`，**不打印 `0 finding(s)`** | rc=2 | PASS |
| K6 | 拒收 | 把 `silent` 例子换成会命中的脚本 | `--rules k-broken.json --ddl …` | rc=2，报 `failed its own examples`，规则不生效 | rc=2 | PASS |
| K7 | 动作级读取 | `examples/change_script_review.sql` | `parse_ddl` | 9 条语句 0 条 unread，9 个 action，含 `drop_column`/`drop_table`；两张只被 ALTER 的表 `created_by is None` | `stubs=['shop.orders','shop.orders_2023']` | PASS |
| K8 | 动作级事实 | 同上 | 取 `ALTER … ADD COLUMN channel_code TEXT NOT NULL` 那一列 | 该列可被规则读到，且带 `nullable=False`、`default=None`、`added_by` 有值 | `nullable=False added_by=yes` | PASS |
| K9 | 快照复用 | `examples/sample_schema.sql` | `--rules table-hygiene --ddl sample_schema.sql` | rc=1，且每一列规则都匹配到非空集合（无 `(nothing matched)`） | rc=1 | PASS |

K5 与 K6 是这一节存在的理由：**会撒谎的绿**有两种，一种是没读懂却报 0 条，一种是规则
自己都没被用例证明过就上了岗。两种都不许出现。K7/K8 说明为什么规则层排在动作读取器
之后——`ddl.py` 以前只认 `CREATE TABLE`，一份 ALTER 脚本会解析成 0 张表、0 条 finding、
退出码 0，正好是这个项目要来抓的失败发生在自己身上。

## L. 规则推断与事实归一（`--infer`）

要求收口成一句话：**「我给出 prompt 可以生成规则，但规则应可马上应用——我生成的变更脚本
审核规则，可以马上用规则去审核别人的脚本。」** 这句话把架构钉死了：**规则能立刻用
⟺ 规则只用「引擎已认识的原子事实」**。所以顺序只能是 事实层（引擎自带）→ 规则文件 →
求值器，中间不许有编译、训练或二次生成。推论是那句口径的准确形式：**加规则 = 加一个文件；
加事实 = 改代码**。

「一个需求可以写好几种」是这条约束的试金石。加主键有四写法：列级 `PRIMARY KEY`、表级
`PRIMARY KEY (id)`、命名约束 `CONSTRAINT pk PRIMARY KEY (id)`、以及事后
`ALTER TABLE … ADD PRIMARY KEY`。**规则不该知道写法**，所以四种写法必须在**事实层**塌成
同一个事实——塌不干净，规则要么漏、要么报假。L0 就是这条的钉子。

`--infer` 是「举例子 → 得到规则」的确定性形态：**把求值器反过来用**。枚举封闭词表，留下
对所有 `fires` 命中、对所有 `silent` 沉默的候选；一条都留不下时，报出最接近的几条**近失**，
并说清它们分不开的那几个例子就是「读者还没采集的事实」。全程零模型：它不是猜，是把
已经能表达的东西找出来。

| 编号 | 维度 | 前置条件 | 步骤（命令 / 输入） | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| L0 | 事实归一 | 一个主键的四种写法 | `rows_for("table"/"column", parse_ddl(...))` | 主键相关字段完全一致（`primary_key_columns`/`has_primary_key`/`unique_key_count`），列上 `is_primary=True`、`nullable=False`，且 `unique_key_count=0`（主键不重复计成唯一键） | `identical=True column=True pk=['id']` | PASS |
| L1 | 词表闭合 | 一份含 14 种语句的脚本 | `{c.kind} <= set(CHANGE_KINDS)` | 读出来的每个 kind 都在词表里，`alter_table` 不再漏声明 | `14 kind(s), undeclared=[]` | PASS |
| L2 | 推断 | `examples/rule-request-primary-key.json` | `infer_rule(...)` | 有候选，且最简的那个正是 DBA 会说的那个事实（`has_primary_key present`，无 select） | 5 候选，首选如上 | PASS |
| L3 | 可装载 | 取第一个候选 | `load_rulepack(verify=True)` | 通过用例门禁，用例原样带过去 | 1 rule，examples 一致 | PASS |
| L4 | 立刻可用 | 同上 | `--infer … --write` 然后 `--rules <写出的包> --ddl 同事的脚本` | 写盘成功；下一条命令就能审出问题（rc=1，只命中没有主键的那张表） | `write rc=0 review rc=1` | PASS |
| L5 | 拒答 | 两个在现有事实上**完全相同**的脚本（`INHERITS` 读者不读） | `--infer l-impossible.json` | rc=2，打印「no rule over the current facts…」与近失，**不打印空的 `"rules": []`** | rc=2 | PASS |
| L6 | 缺口可见 | 无 | `--facts` | rc=0，列出三个 fact model，`sql` 未建并说明理由与去处 | rc=0 | PASS |
| L7 | 同源 | 无 | `--vocabulary` vs `PROVIDERS` | 词表由事实注册表生成，二者不能漂移 | 3 target / 3 model | PASS |
| L8 | 去重 | 两条 target/select/断言完全相同的规则 | `find_overlaps` | 报 `exact`，点名两条 id | exact=1 | PASS |
| L9 | 包含 | 一条更宽的 + 一条更窄的同断言规则 | 同上 | 报 `redundant`，说明窄的只在措辞上有用 | redundant=1 exact=0 | PASS |
| L10 | 冲突 | 同 select，一条要求 `present`、一条要求 `absent` | 同上 | 报 `conflicting` | conflicting=1 | PASS |
| L11 | 拒收 | 同一个包里一条规则写了两遍 | `--rules l-dup.json --verify` | rc=2，报 `written twice` | rc=2 | PASS |

L4 是这一节要证明的那句话：**描述 + 例子 → 规则文件 → 下一条命令就能审别人的脚本**，
中间没有编译、没有训练、没有第二次生成。L5 是它的反面，同样重要：分不开的时候，
工具不许给出一条永不触发的规则——它必须说「缺的是事实」，并把位置指向事实提供者。
L8–L11 是「自动去重」的落地：完全重复**拒收**（会让同一个 subject 被报两次），
包含与冲突**出声但不拒收**（可能是刻意的措辞或严重度差异），跨包同 id 也报，
因为 finding 只带 id，两条规则共用一个 id 在输出里分不开。

## M. 活库事实与索引归一（第五种写法，以及必须查活库的那类规则）

主键只是例子。真正的要求是：**我说「加一条主键规则」，服务就得认得所有加主键的写法**。
同一个问题换个事实（索引）更狠——一个索引有五种写法：列级 `UNIQUE`、表内 `UNIQUE (a)`、
命名约束 `CONSTRAINT uq UNIQUE (a)`、事后 `ALTER TABLE … ADD CONSTRAINT`、以及
`CREATE UNIQUE INDEX`。第五种写法不是语句，是**活库**：`pg_index` 是同一件事的另一个读者。

第二层要求更关键：**「不能给表加重复索引」这种规则，只查脚本是答不出来的**。脚本只知道
自己建的索引，表上早就有的那个只有在库里。所以这一节验两件事：事实层把五种写法读成同一个
事实；而需要活库的规则在只拿到脚本时**必须拒绝应用**，不许静静报 0 条。

规则侧因此多了一层：`applies_to` 从「说明书」变成「执行条件」。包里写
`["catalog"]`，喂它一份脚本就 `WAS NOT APPLIED`，并告诉你哪条命令能给到事实，退出码 2。
规则没能被求值，就等于什么都没通过——这跟「0 finding 不是通过」是同一条纪律。

「查知识库」在这里的位置也要说清：**语料给的是写法清单，不是运行时输入**。手册里每一种
写索引/写键的方式变成 §M 里的一个用例，读者必须把它们读成一个事实。检索是概率的，这一层
不允许是——所以文档只产出**测试**。

| 编号 | 维度 | 前置条件 | 步骤（命令 / 输入） | 预期结果 | 实际结果 | 结果 |
|---|---|---|---|---|---|---|
| M1 | 事实归一 | 4 种约束写法 + 1 种 `CREATE UNIQUE INDEX` + 1 行活库返回 | `rows_for("index", parse_ddl(…))/build_schema(…)` | 4 种约束写法与活库行**每个字段相同**；`CREATE UNIQUE INDEX` 只差 `from_constraint`；`created_here` 脚本侧全 True、活库侧 False | `differs=['from_constraint']` | PASS |
| M2 | 端到端 | 活库快照（已有 `idx_orders_paid_at`）+ 脚本新建同名同列索引 | `apply_script(snapshot, parse_ddl(…))` → `review_schema([index-hygiene], …)` | 脚本那条被并进事实，2 条 finding，且 `{duplicate_of}` 在 detail 里被填成对方的索引名 | 1 added, 2 finding(s) | PASS |
| M3 | 反例 | 同列不同序 / 不同 method / 不同 partial 谓词 / 表达式键 | 读 `duplicate_count` | 前三种都不算重复；`(a)` 全量 vs `(a) WHERE a>0` 只报 partial 那条（全量的覆盖它，反之不成立） | `order=[0,0] partial={'i1':0,'i2':1}` | PASS |
| M4 | 执行条件 | `rules/index-hygiene.json`（`applies_to=["catalog"]`） | `--rules index-hygiene --ddl m-script.sql` | rc=2，打印 `WAS NOT APPLIED`，给出 `--db` 提示，**不打印 `no rule fired`** | rc=2 | PASS |
| M5 | 自证 | 同上 | `--rules index-hygiene --verify` | 5 例全过（含「同列不同序」这条近失），rc=0 | `5 example(s) → all passed` | PASS |
| M6 | 缺口可见 | 无 | `--facts` | `catalog` 列为已建；`index` target 标注 read from `ddl, schema, catalog` | rc=0 | PASS |
| M7 | 边界 | 空活库 + 一份建新表的脚本 | `apply_script(build_schema(), …)` | 索引不并入（0 条），新表名被记入 `new_tables` 供上报表 | `indexes=0 new_tables=['shop.new']` | PASS |
| M8 | 描述→规则 | `examples/rule-request-duplicate-index.json`（2 fires / 2 silent） | `--infer … --write`，再 `--rules <包> --ddl` | 首选 `duplicate_count lte 0`；`applies_to` 由请求带出为 `["catalog"]`；写盘 rc=0，拿脚本去用 rc=2（该拒就拒） | `first=[{duplicate_count lte 0}]` | PASS |
| M9 | 不撒谎 | 一个连不上的 DSN | `--db postgresql://nobody@127.0.0.1:1/none?connect_timeout=2` | rc=2，打印 `no rule was applied`，**不打印 `0 finding(s)` / `no rule fired`** | rc=2 | PASS |
| M10 | 词表导出 | 无 | `--vocabulary` | `applies_to` 含 `catalog`；`index` target 含 `duplicate_count`；`finding_text` 说明 `{field}` | `applies_to=['ddl','schema','catalog']` | PASS |

M3 是这一节的牙齿。**«同列不同列序» 恰恰是最常见的误报来源**：`(a, b)` 和 `(b, a)` 是两个
不同的索引，只有前者能服务 `a` 的等值查询。同样，`lower(email)` 与 `email` 不是同一个键——
所以表达式键**从不参与比较**，它照常出行，只是永远不被算成「别人的重复」。少报是漏一条
finding，误报是把两个不同的东西说成一个，后者会直接毁掉审阅者对工具的信任。

M8 与 §L 的 L4 是同一个论断的第二次出现：**描述 + 例子 → 规则文件 → 立刻能审**。区别只在
这次规则要的是活库事实，于是 `applies_to` 一路从请求文件带进包里，`--ddl` 会拒绝它——
自动生成出来的规则同样要接受「执行条件」的约束，而不是绕过去。

M9 是这一层最容易写成假绿的地方：连不上库的时候，最像样的输出应该是退出码 2 加一句
「什么都没读、什么都没应用」，而不是一行 `0 finding(s)`。

**这一节没验的部分**：`INDEXES_SQL` 本身（`pg_index` + `unnest(indkey::int2[]) WITH ORDINALITY`
+ `indnkeyatts`）**还没有在真库上跑过**——本机没有 PostgreSQL，VM 上是下一步。被验证的是
决定性的那一半：活库行与脚本行经同一个 `build_schema`/`rows_for` 出口，字段逐个相同（M1），
所以规则分不出自己在读哪一边。真库要验的是另一半：那条查询在 PG16 上返回的行，形状与
M1 里手写的那一行一致。

## 已知约定核对

| 约定 | 核对结论 |
|---|---|
| 相关度地板按 `embedder.mode` 判模式 | 成立。`tools.py` 用 `mode = getattr(embedder, "mode", "offline")`；离线 = 0.0、api = 0.45（C2/C3 验证）。 |
| 不要用 `--eval --report` 覆盖 `eval/report-dba.md` | 遵守。本次报告生成一律输出到 `tests/tmp/`（D6）。 |
| 路由：写词命中 ≠ 写意图 | 成立（B1–B5，demo.py [router] 10 例全绿）。 |
| demo.py 四块自检为回归门禁 | 成立，修复后 `python demo.py` 退出 0，13/10/7/12 全绿。 |
| 多来源标注 `甲 / 乙` 命中任一即算命中 | 成立。`_gold_sources()` 按 `/` 拆分；第 9 题 `pg-vacuum-tuning / pg-bloat-seq-scan` 在 rank 4 命中计为 hit（D5 汇总内）。 |
| 「最近的提交 874d7bd、12b9b98、8e36a49 本地未 push」 | **与代码实际不符**：`git branch -vv` 显示 `main` 跟踪 `origin/main` 且 "up to date"；`git log` 顶部为 `8e36a49`。这三条实际已同步到 `origin/main`，并非未 push。未据此改代码。 |
| 语料规模：knowledge 8 篇 / dba 14 篇 | 成立（ingest 报告 8 篇、14 篇）。 |
| 已知极限：一条规则只能看一行，那「两行是否重复」怎么办 | 成立，做法是把比较放进事实提供者（`facts.py` 的 `_duplicates`），导出成 `duplicate_count`；规则读到一个数，仍然是纯声明（M3）。 |
| 规则包能不能要求活库 | 能，且是**强制**的：`applies_to` 与本次事实来源不匹配就不应用、退出码 2（M4/M8），不是文档约定。 |

## 隐私检查

`tests/` 下新增文件与两份文档均不含本机用户名路径；文档示例统一用 `<repo>` / `tests/tmp/`。
