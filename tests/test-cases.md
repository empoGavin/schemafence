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
| **合计** | **80** | **10** | **80 PASS** |

C11、C12、D19 是后来补的，随 `--rebuild` 与孤儿 source 检测一起进来（见 §C 与 §D 末尾）。

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

## 已知约定核对

| 约定 | 核对结论 |
|---|---|
| 相关度地板按 `embedder.mode` 判模式 | 成立。`tools.py` 用 `mode = getattr(embedder, "mode", "offline")`；离线 = 0.0、api = 0.45（C2/C3 验证）。 |
| 不要用 `--eval --report` 覆盖 `eval/report-dba.md` | 遵守。本次报告生成一律输出到 `tests/tmp/`（D6）。 |
| 路由：写词命中 ≠ 写意图 | 成立（B1–B5，demo.py [router] 10 例全绿）。 |
| demo.py 三块自检为回归门禁 | 成立，修复后 `python demo.py` 退出 0，13/10/7 全绿。 |
| 多来源标注 `甲 / 乙` 命中任一即算命中 | 成立。`_gold_sources()` 按 `/` 拆分；第 9 题 `pg-vacuum-tuning / pg-bloat-seq-scan` 在 rank 4 命中计为 hit（D5 汇总内）。 |
| 「最近的提交 874d7bd、12b9b98、8e36a49 本地未 push」 | **与代码实际不符**：`git branch -vv` 显示 `main` 跟踪 `origin/main` 且 "up to date"；`git log` 顶部为 `8e36a49`。这三条实际已同步到 `origin/main`，并非未 push。未据此改代码。 |
| 语料规模：knowledge 8 篇 / dba 14 篇 | 成立（ingest 报告 8 篇、14 篇）。 |

## 隐私检查

`tests/` 下新增文件与两份文档均不含本机用户名路径；文档示例统一用 `<repo>` / `tests/tmp/`。
