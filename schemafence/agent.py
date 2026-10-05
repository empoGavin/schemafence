"""The agent loop — tool selection, then evidence, then an answer.

Two drivers, one loop:

  rule-based   a deterministic router that picks tools from the question.
               No key, no network, no model.  It exists for two reasons:
               it makes the demo runnable on a plane, and it is the honest
               baseline — when the model does better, you can show by how much.

  model-driven any OpenAI-compatible chat endpoint with function calling.
               ``SF_LLM_API_KEY`` switches it on; the loop is hand-written on
               purpose.  When an interviewer asks "how does the agent decide
               which tool to call, and what happens when a tool fails?", the
               answer has to come from code you wrote, not from a framework's
               docstring.

The loop is the same in both cases: pick tools -> run them -> feed the
results back -> answer, at most ``max_rounds`` times.  Tools go through the
guard either way.  That is the whole argument of this project in one sentence:
the model may propose, the fence disposes.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .planner import plan
from .tools import TOOL_SPECS, Toolbox

MAX_ROUNDS = 6

AGENT_SYSTEM = """You are a database operations assistant working against a
PostgreSQL instance that you may only read from.

Hard rules:
  1. You may only produce read-only SQL: SELECT, WITH ... SELECT, or EXPLAIN.
  2. Never propose INSERT, UPDATE, DELETE, DDL of any kind, or COPY.
  3. Every query you write must carry an explicit LIMIT.
  4. Before querying, check that the table and column you are about to use
     exist and mean what you think they mean.  A query that runs and returns
     the wrong number is worse than an error.
  5. If the question is ambiguous about which table or which time range is
     meant, ask a clarifying question instead of guessing.
  6. Cite your evidence: which document or which statistic the answer came
     from.  An answer without evidence is a guess.

Prefer searching the knowledge base for questions about practice and
experience, and prefer querying the catalogue for questions about the
current state of the data.

Diagnosing slowness has a fixed order, and skipping steps produces wrong
answers:
  1. read the plan (explain_sql);
  2. check the table's stats (get_table_stats) — a large dead_tuples count
     next to live_tuples means bloat, and bloat makes a sequential scan
     read far more pages than the live rows justify;
  3. search the knowledge base (search_docs) for the matching method note.
     This step is NOT optional: strong priors make common topics feel
     familiar, but the runbook carries the operational warnings memory
     does not have (lock levels, repack trade-offs, in-house thresholds);
  4. judge what came back before you use it.  A passage is evidence only
     if it addresses the question actually asked — a note about
     subtransactions does not answer a question about idle transactions,
     even if both mention long transactions.  State plainly that the
     knowledge base has no matching note when that is the case; a
     near-miss dressed up as an answer is worse than an honest gap,
     because the citation makes it look verified.
  5. only then suggest fixes — cite the passage you found (or say none
     matched), and never suggest a new index before ruling out bloat,
     because an index on a bloated table helps nobody.
"""

DESTRUCTIVE = ("删掉", "删除", "清空", "清除", "改一下", "改掉", "drop", "delete",
               "truncate", "update ", "insert", "写入", "导入数据", "建表", "授权")
# A write keyword alone is not intent.  What separates "帮我删掉这张表" from
# "删除大表分区要注意什么" is the shape of the sentence, so the router reads
# both: an act request ("帮我"/"直接跑") means do it, a question form ("？"
# "怎么"/"是不是") means explain it.
ACT_REQUEST = ("帮我", "给我", "请帮", "麻烦帮", "麻烦你", "来删", "来改", "来建",
               "执行一下", "跑一下", "直接执行", "直接跑", "帮我做", "你来")
INTERROGATIVE = ("?", "？", "吗", "呢", "怎么", "如何", "为什么", "是否", "能不能",
                 "可不可以", "要不要", "需要注意", "注意什么", "区别", "一样",
                 "哪个", "哪里", "是什么", "什么意思")

# (question, expected to be refused) — the router is the one place where a
# keyword list is doing judgement, so it gets a selftest like the guard has.
ROUTE_CASES: tuple[tuple[str, bool], ...] = (
    ("帮我删掉 orders 表 2024 年之前的数据", True),
    ("帮我删掉 orders 表的数据，可以吗？", True),
    ("drop table orders", True),
    ("清空 t 表", True),
    ("delete from shop.orders where id = 1", True),
    ("把参数从配置文件里删掉和设成 0，效果一样吗？", False),
    ("怎么安全地删除大表的历史分区？", False),
    ("truncate 大表会锁多久？", False),
    ("授权给只读账号要注意什么？", False),
    ("怎么建表更合理？", False),
)


def run_route_selftest() -> tuple[list[tuple[str, bool, bool]], bool]:
    """Return (rows, all_passed) where each row is (question, expected, passed)."""
    rows = []
    all_passed = True
    for question, expected in ROUTE_CASES:
        refused = bool(route(question)[0]) and route(question)[0][0][0] == "__refuse__"
        passed = refused is expected
        all_passed = all_passed and passed
        rows.append((question, expected, passed))
    return rows, all_passed
VAGUE = ("我不确定", "不知道查什么", "不知道", "随便", "帮我看看", "看看有什么问题",
         "你看着办", "有啥问题")

DOC_WORDS = ("怎么", "如何", "为什么", "原理", "是什么", "处理", "方案", "经验",
             "区别", "注意", "风险", "坑", "治理", "优化", "建议", "流程",
             "how ", "why ", "what is", "best practice")
PLAN_WORDS = ("慢", "执行计划", "explain", "耗时", "性能", "卡", "超时", "plan")
STATS_WORDS = ("膨胀", "表大小", "多大", "多少行", "死元组", "死行", "统计信息",
               "索引情况", "占多少", "空间", "vacuum", "bloat")
QUERY_WORDS = ("多少", "几条", "列出", "查一下", "查出", "合计", "总额", "统计一下",
               "top", "最多", "最少", "count", "sum", "平均", "明细")


@dataclass
class Step:
    tool: str
    args: dict
    result: dict
    ok: bool
    ms: float
    summary: str

    def render(self, index: int) -> str:
        mark = "ok " if self.ok else "!! "
        head = f"  {mark}{index}. {self.tool}({_short(self.args)})"
        out = f"{head}\n      → {self.summary}  [{self.ms:.0f} ms]"
        # An EXPLAIN result IS the evidence — print every line, not just the
        # one-line summary, or the user watches the model reason about a plan
        # they never saw.
        plan = self.result.get("plan") if isinstance(self.result, dict) else None
        if plan:
            out += "\n" + "\n".join(f"      {line}" for line in plan)
        return out


@dataclass
class AgentRun:
    question: str
    driver: str
    steps: list[Step] = field(default_factory=list)
    answer: str = ""
    refused: bool = False
    clarify: bool = False
    rounds: int = 0
    ms: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _short(args: dict, limit: int = 52) -> str:
    if not args:
        return ""
    text = ", ".join(f"{k}={v}" for k, v in args.items())
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


# --------------------------------------------------------------------------- #
# deterministic router
# --------------------------------------------------------------------------- #

def route(question: str, schema=None) -> tuple[list[tuple[str, dict]], list[str]]:
    """Question -> [(tool, args)].  Every branch states its reason out loud.

    Note how the write check is split in two.  A keyword hit does *not* prove
    the intent is a write: "把参数从配置文件里删掉和设成 0 效果一样吗" is a
    practice question that happens to contain 删掉, and refusing it makes the
    assistant useless for a whole class of legitimate DBA questions.

    So the keyword is combined with the *shape of the sentence*: an imperative
    ("帮我删掉…", "drop table orders") is a request to act, a question ("怎么
    安全地删除…？") is a request to explain.  Only the first is refused here;
    the second is answered from the knowledge base with no statement executed.

    The reason for being generous on the question side: the two mistakes are
    not symmetric.  Letting a write request reach the tools costs one refusal
    with no side effect — the guard is what actually stops a statement.  Killing
    a real question costs the user his answer, and no later gate can restore it.
    """
    low = question.lower()
    reasons: list[str] = []

    named_write = [word for word in DESTRUCTIVE if word in low]
    begging = any(word in low for word in ACT_REQUEST)
    asking = any(word in low for word in INTERROGATIVE)

    if named_write and (begging or not asking):
        return [("__refuse__", {})], [
            "the question asks for a write — this agent may only read"]
    if named_write:
        return [("search_docs", {"query": question, "k": 5})], [
            f"the question mentions a write ({named_write[0]}) but is phrased as a "
            "question — answered from the knowledge base only, and no statement "
            "will be executed"]
    if any(word in question for word in VAGUE):
        return [("__clarify__", {})], [
            "the question does not name a table, a time range or a symptom — "
            "guessing here is how a plausible-but-wrong answer gets produced"]

    calls: list[tuple[str, dict]] = []
    planned_sql = None
    if schema is not None:
        planned_sql = plan(question, schema).sql

    if any(word in low for word in STATS_WORDS):
        calls.append(("get_table_stats", {}))
        reasons.append("the question is about the state of the data")
    if any(word in low for word in PLAN_WORDS):
        if planned_sql:
            calls.append(("explain_sql", {"sql": planned_sql}))
            reasons.append("the question is about speed, so read the plan first")
    if any(word in low for word in QUERY_WORDS):
        if planned_sql:
            calls.append(("run_sql", {"sql": planned_sql}))
            reasons.append("the question asks for numbers, so it needs rows")
    if any(word in low for word in DOC_WORDS):
        calls.append(("search_docs", {"query": question, "k": 5}))
        reasons.append("the question is about practice — check what we already know")

    if not calls:
        calls.append(("search_docs", {"query": question, "k": 5}))
        reasons.append("no signal in the question — fall back to the knowledge base")
    return calls, reasons


# --------------------------------------------------------------------------- #
# model-driven loop (OpenAI-compatible, still zero dependencies)
# --------------------------------------------------------------------------- #

class LLMUnavailable(RuntimeError):
    """The chat endpoint could not be reached, or gave up after retries.

    Raised instead of letting a socket error escape: an unhandled
    TimeoutError kills the whole process and takes the tool evidence
    gathered so far with it — the one thing worth keeping when the model
    goes away mid-diagnosis.
    """


class LLMClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, temperature: float = 0.1,
                 timeout: int | None = None, retries: int | None = None):
        self.base_url = (base_url or os.environ.get(
            "SF_LLM_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("SF_LLM_API_KEY", "")
        self.model = model or os.environ.get("SF_LLM_MODEL", "deepseek-chat")
        self.temperature = temperature
        # A thinking model spends a long time before the first byte arrives,
        # and a 120 s read timeout killed a whole run at round 1.  Default
        # higher, tunable per environment.
        self.timeout = timeout if timeout is not None else int(
            os.environ.get("SF_LLM_TIMEOUT", "300"))
        self.retries = retries if retries is not None else int(
            os.environ.get("SF_LLM_RETRIES", "1"))

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        payload: dict = {"model": self.model, "messages": messages,
                         "temperature": self.temperature}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        url = f"{self.base_url}/chat/completions"
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                # 429 and 5xx are worth another try; 4xx (auth, bad model
                # name, bad request) will fail identically forever.
                if exc.code == 429 or exc.code >= 500:
                    last = exc
                    if attempt < self.retries:
                        time.sleep(2 ** attempt)
                        continue
                raise LLMUnavailable(
                    f"HTTP {exc.code} from {url}: {detail}") from exc
            except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise LLMUnavailable(
                    f"{type(exc).__name__}: {exc} after {self.retries + 1} "
                    f"attempt(s) with a {self.timeout}s read timeout — raise "
                    f"SF_LLM_TIMEOUT if the model is simply slow") from exc
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                raise LLMUnavailable(f"malformed response from {url}: {exc}") from exc
        raise LLMUnavailable(f"unreachable: {last}")


def _tool_message(call_id: str, payload: dict) -> dict:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > 4000:
        text = text[:4000] + "… (truncated)"
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def _schema_catalogue(schema) -> str:
    """One line per table: qualified name, columns *with their exact types*.

    The model cannot guess which schema the tables live in — it wrote
    `FROM orders` and then tried `schema=public`, and both failed because
    the demo schema is `shop`.  Names alone were not enough either: with
    only a column list the model wrote `status = 'completed'` against a
    smallint column and lost the whole EXPLAIN.  Types cost a few dozen
    more tokens and remove a second failure class.
    """
    if schema is None or not getattr(schema, "tables", None):
        return ""
    lines = []
    for t in schema.tables:
        cols = ", ".join(f"{c.name} {c.type}" for c in t.columns)
        note = f"  -- {t.comment}" if t.comment else ""
        lines.append(f"  - {t.qualified} ({cols}){note}")
    return ("\n\nSchema catalogue — use these EXACT qualified names (unqualified\n"
            "table names fail because search_path does not include the schema)\n"
            "and write literals that match the listed column types:\n"
            + "\n".join(lines) + "\n")


def _run_llm(question: str, toolbox: Toolbox, llm: LLMClient,
             max_rounds: int, schema=None) -> AgentRun:
    run = AgentRun(question=question, driver=f"model / {llm.model}")
    messages = [{"role": "system", "content": AGENT_SYSTEM + _schema_catalogue(schema)},
                {"role": "user", "content": question}]
    # Session memory keyed by (tool, arguments).  Two deterministic facts:
    # a *failed* call reruns into the identical error, and a *successful*
    # call returns the identical result — in a read-only agent neither is
    # worth a second execution.  The last VM run showed Qwen3-8B calling
    # search_docs four times with the same arguments, each hit paying for
    # an embedding request, until the budget died with no answer written.
    # Successful repeats get the cached payload plus a use-it-and-move-on
    # note; failed repeats get the don't-retry message.  Either way the
    # call does not execute again, and a round made entirely of repeats
    # trips the loop-breaker below.
    seen: dict[str, dict] = {}

    def ask(messages: list[dict], tools: list[dict] | None = None) -> dict | None:
        """One model call, degrading instead of dying.

        The endpoint went away mid-diagnosis once already (a read timeout at
        round 1 killed the process and every tool result with it).  On
        failure the loop stops here and the evidence gathered so far is
        assembled mechanically — the run stays useful even when the model
        does not.
        """
        try:
            return llm.chat(messages, tools=tools)
        except LLMUnavailable as exc:
            run.answer = (f"The model endpoint became unreachable: {exc}\n"
                          "The run stopped there. Here is the evidence gathered "
                          "before it did:\n" + _offline_answer(run))
            return None

    for round_no in range(1, max_rounds + 1):
        run.rounds = round_no
        message = ask(messages, tools=TOOL_SPECS)
        if message is None:
            return run
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            content = re.sub(r"<think>.*?</think>", "",
                             message.get("content") or "", flags=re.S).strip()
            if content:
                run.answer = content
                return run
            # Qwen3-class thinking models sometimes return an empty content
            # with everything parked in reasoning_content.  Nudge once, with
            # tools withheld, so the only possible move is a plain-text
            # summary of the evidence already gathered.
            messages.append({"role": "user", "content":
                "Your last reply was empty. Answer the original question now, "
                "in plain text, citing the tool results you already have."})
            final = ask(messages)
            if final is None:
                return run
            run.answer = (re.sub(r"<think>.*?</think>", "",
                                 final.get("content") or "", flags=re.S).strip()
                          or _offline_answer(run))
            return run
        executed_any = False
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            key = name + "|" + json.dumps(args, ensure_ascii=False, sort_keys=True)
            if key in seen:
                prior = seen[key]
                if prior.get("ok"):
                    payload = dict(prior)
                    payload["cached"] = True
                    payload["note"] = ("you already ran this exact call; this is the "
                                       "cached result — do not call it again, use it "
                                       "and move on to the next step or answer")
                else:
                    payload = {"error": prior.get("error"),
                               "hint": "you already ran this exact call and it failed "
                                       "the same way — repeating it will not help. "
                                       "Change the approach, or answer now from the "
                                       "evidence you already have."}
                messages.append(_tool_message(call.get("id", name), payload))
                continue
            executed_any = True
            result = toolbox.call(name, args)
            run.steps.append(Step(name, args, result.data, result.ok,
                                  result.ms, result.brief()))
            if result.ok:
                seen[key] = result.data
            else:
                seen[key] = {"error": (result.data.get("error")
                                       or result.data.get("reason") or "failed")}
            payload = result.data if result.ok else {
                "error": result.data.get("error") or result.data.get("reason"),
                "hint": "fix the query and try once more, or explain why you cannot"}
            messages.append(_tool_message(call.get("id", name), payload))

        # Every call this round was a cached repeat — success or failure, the
        # model is looping on something already answered.  Take the tools
        # away so its only move is a plain-text answer; if it still returns
        # nothing, assemble the evidence mechanically.
        if calls and not executed_any:
            final = ask(messages)
            if final is None:
                return run
            run.answer = (re.sub(r"<think>.*?</think>", "",
                                 final.get("content") or "", flags=re.S).strip()
                          or _offline_answer(run))
            return run

    run.answer = ("I stopped after the tool-call budget ran out.  Here is what I "
                  "found before stopping:\n" + _offline_answer(run))
    return run


# --------------------------------------------------------------------------- #
# offline driver
# --------------------------------------------------------------------------- #

def _offline_answer(run: AgentRun) -> str:
    """Turn the tool output into an answer without a model.

    It is deliberately mechanical: quote the passages, show the rows, name the
    table.  A model writes better prose; it does not write better evidence.
    """
    lines: list[str] = []
    for step in run.steps:
        if step.tool == "search_docs" and step.ok:
            hits = step.result.get("hits", [])
            if not hits:
                lines.append("Knowledge base returned nothing — the note may not exist yet.")
                continue
            lines.append("From the knowledge base:")
            for hit in hits[:3]:
                snippet = " ".join(str(hit["snippet"]).split())
                lines.append(f"  · {hit['source']} · {hit['section']} "
                             f"(score {hit['score']:.3f}) — {snippet[:160]}")
        elif step.tool == "run_sql" and step.ok:
            rows = step.result.get("rows", [])
            columns = step.result.get("columns", [])
            lines.append(f"Query result ({step.result.get('rowcount', 0)} row(s),"
                         f" tables: {', '.join(step.result.get('tables') or ['?'])}):")
            if not rows:
                lines.append("  (no rows)")
            for row in rows[:5]:
                pairs = ", ".join(f"{c}={v}" for c, v in zip(columns, row))
                lines.append(f"  · {pairs}")
        elif step.tool == "explain_sql" and step.ok:
            lines.append("Execution plan:")
            lines.extend(f"  {line}" for line in step.result.get("plan", [])[:8])
        elif step.tool == "get_table_stats" and step.ok:
            lines.append("Catalogue statistics (worst dead-tuple ratio first):")
            for table in step.result.get("tables", [])[:5]:
                lines.append(
                    f"  · {table['table']}: {table['size']}, ~{table['est_rows']} rows, "
                    f"dead {table['dead_tuples']} ({table['dead_ratio'] * 100:.1f}%), "
                    f"last analyse {table['last_analyze']}")
        else:
            lines.append(f"({step.tool}: {step.summary})")
    lines.append("")
    lines.append("— assembled without a language model: the evidence above is the "
                 "tool output, verbatim.")
    return "\n".join(lines)


def _run_offline(question: str, toolbox: Toolbox, schema=None) -> AgentRun:
    run = AgentRun(question=question, driver="rules / no model")
    calls, reasons = route(question, schema)
    run.rounds = 1

    if calls and calls[0][0] == "__refuse__":
        run.refused = True
        run.answer = ("I cannot do that.  I only read: no INSERT, UPDATE, DELETE or "
                      "DDL leaves this process.  If you need the data changed, that "
                      "has to go through a change request against a write-capable role.")
        return run
    if calls and calls[0][0] == "__clarify__":
        run.clarify = True
        run.answer = ("Before I query anything, I need one of: which table, which time "
                      "range, or which symptom.  A query built on a guess returns a "
                      "number that looks right, which is worse than no answer.")
        return run

    for index, (name, args) in enumerate(calls, 1):
        result = toolbox.call(name, args)
        run.steps.append(Step(name, args, result.data, result.ok, result.ms,
                              result.brief()))
    run.reasons = reasons
    run.answer = _offline_answer(run)
    return run


def route_reasons(question: str, schema=None) -> list[str]:
    return route(question, schema)[1]


def run_agent(question: str, toolbox: Toolbox, schema=None,
              llm: LLMClient | None = None, max_rounds: int = MAX_ROUNDS) -> AgentRun:
    started = time.perf_counter()
    if llm is not None and llm.available:
        run = _run_llm(question, toolbox, llm, max_rounds, schema=schema)
    else:
        run = _run_offline(question, toolbox, schema)
    run.ms = round((time.perf_counter() - started) * 1000, 2)
    return run


_WORD = re.compile(r"[A-Za-z]+")


def looks_english(text: str) -> bool:
    return bool(_WORD.search(text)) and len(_WORD.findall(text)) >= 3
