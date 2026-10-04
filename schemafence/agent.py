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
import time
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
"""

DESTRUCTIVE = ("删掉", "删除", "清空", "清除", "改一下", "改掉", "drop", "delete",
               "truncate", "update ", "insert", "写入", "导入数据", "建表", "授权")
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
        return f"{head}\n      → {self.summary}  [{self.ms:.0f} ms]"


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
    """Question -> [(tool, args)].  Every branch states its reason out loud."""
    low = question.lower()
    reasons: list[str] = []

    if any(word in low for word in DESTRUCTIVE):
        return [("__refuse__", {})], [
            "the question asks for a write — this agent may only read"]
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
        reasons.append("the question is about the state of the data, not about practice")
    if any(word in low for word in PLAN_WORDS):
        if planned_sql:
            calls.append(("explain_sql", {"sql": planned_sql}))
            reasons.append("the question is about speed — read the plan, do not guess")
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

class LLMClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, temperature: float = 0.1,
                 timeout: int = 120):
        self.base_url = (base_url or os.environ.get(
            "SF_LLM_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("SF_LLM_API_KEY", "")
        self.model = model or os.environ.get("SF_LLM_MODEL", "deepseek-chat")
        self.temperature = temperature
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        payload: dict = {"model": self.model, "messages": messages,
                         "temperature": self.temperature}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]


def _tool_message(call_id: str, payload: dict) -> dict:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > 4000:
        text = text[:4000] + "… (truncated)"
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def _run_llm(question: str, toolbox: Toolbox, llm: LLMClient,
             max_rounds: int) -> AgentRun:
    run = AgentRun(question=question, driver=f"model / {llm.model}")
    messages = [{"role": "system", "content": AGENT_SYSTEM},
                {"role": "user", "content": question}]

    for round_no in range(1, max_rounds + 1):
        run.rounds = round_no
        message = llm.chat(messages, tools=TOOL_SPECS)
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            run.answer = (message.get("content") or "").strip()
            return run
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = toolbox.call(name, args)
            run.steps.append(Step(name, args, result.data, result.ok,
                                  result.ms, result.brief()))
            payload = result.data if result.ok else {
                "error": result.data.get("error") or result.data.get("reason"),
                "hint": "fix the query and try once more, or explain why you cannot"}
            messages.append(_tool_message(call.get("id", name), payload))

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
        run = _run_llm(question, toolbox, llm, max_rounds)
    else:
        run = _run_offline(question, toolbox, schema)
    run.ms = round((time.perf_counter() - started) * 1000, 2)
    return run


_WORD = re.compile(r"[A-Za-z]+")


def looks_english(text: str) -> bool:
    return bool(_WORD.search(text)) and len(_WORD.findall(text)) >= 3
