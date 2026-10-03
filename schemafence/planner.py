"""An offline planner.

This is a deliberately dumb, keyword-based stand-in for a language model.
It exists so the demo runs with no API key at all — and so that the point
of the project stays obvious: whatever produced the SQL, the SQL still has
to pass the fence before it touches a database.

Swap in a real model with --llm openai; the output is the same shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ddl import Schema, Table

CONCEPT_HINTS = {
    "订单": ["order", "orders"],
    "退款": ["refund", "refunds"],
    "用户": ["user", "users"],
    "黑名单": ["blacklist"],
    "风控": ["blacklist"],
    "画像": ["profile"],
    "事件": ["event", "events"],
    "行为": ["event", "events"],
    "金额": ["amount"],
    "价格": ["price"],
    "余额": ["balance"],
    "状态": ["status"],
    "时间": ["time", "at"],
    "邮箱": ["email"],
    "手机": ["phone"],
    "昵称": ["nickname"],
}

STOPWORDS = {
    "the", "a", "an", "of", "is", "are", "was", "how", "many", "much", "and",
    "in", "on", "for", "to", "me", "my", "what", "which", "table", "data",
    "please", "give", "show", "list", "last", "this", "that", "with",
}

AGGREGATE_WORDS = (
    "总额", "合计", "总共", "一共", "多少", "数量", "统计", "占比", "平均",
    "sum", "total", "count", "how many", "average", "avg", "max", "min",
)


@dataclass
class Candidate:
    table: Table
    score: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class Plan:
    question: str
    candidates: list[Candidate] = field(default_factory=list)
    sql: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def chosen(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def _tokens(question: str) -> set[str]:
    low = question.lower()
    tokens = {m.group(0) for m in re.finditer(r"[a-z_][a-z0-9_]*", low)
              if m.group(0) not in STOPWORDS and len(m.group(0)) > 1}
    for zh, words in CONCEPT_HINTS.items():
        if zh in question:
            tokens.update(words)
    return tokens


def _name_match(token: str, name: str) -> bool:
    name = name.lower()
    if token == name or token in name.split("_"):
        return True
    return name.startswith(token) or token.startswith(name)


def plan(question: str, schema: Schema) -> Plan:
    result = Plan(question=question)
    tokens = _tokens(question)

    for table in schema.tables:
        score, reasons = 0, []
        for token in tokens:
            if _name_match(token, table.name):
                score += 4
                reasons.append(f"table name matches “{token}”")
            if table.comment and token in table.comment.lower():
                score += 2
                reasons.append(f"table comment matches “{token}”")
            for col in table.columns:
                if _name_match(token, col.name):
                    score += 2
                    reasons.append(f"column {col.name} matches “{token}”")
                elif col.comment and token in col.comment.lower():
                    score += 1
                    reasons.append(f"column comment {col.name} matches “{token}”")
        if score:
            result.candidates.append(Candidate(table=table, score=score, reasons=reasons[:4]))

    result.candidates.sort(key=lambda c: (-c.score, c.table.name))

    if not result.candidates:
        result.notes.append("no table matched — a real model would ask for clarification here")
        return result

    if len(result.candidates) > 1:
        result.notes.append(
            f"{len(result.candidates)} candidate tables scored close together — the model "
            "picks one, and nothing in the database stops it picking the wrong one")

    chosen = result.candidates[0].table
    money = next((c for c in chosen.columns if c.looks_like_money), None)
    aggregate = any(w in question.lower() for w in AGGREGATE_WORDS)
    if aggregate and money:
        result.sql = f"SELECT sum({money.name}) AS total_amount FROM {chosen.qualified}"
    elif aggregate:
        result.sql = f"SELECT count(*) AS row_count FROM {chosen.qualified}"
    else:
        result.sql = f"SELECT * FROM {chosen.qualified}"

    if any(chosen.name.lower().endswith(s) for s in
           ("_archive", "_archived", "_bak", "_history", "_old")):
        result.notes.append(
            f"the chosen table “{chosen.qualified}” looks like an archive — "
            "the answer may be stale even though the query succeeds")

    return result
