"""The seven-layer guardrail.

This is the part an LLM cannot talk its way past.  Every layer is a
deterministic function of the SQL text, which is the whole point:
advice lowers the chance of a mistake, a guard lowers its blast radius.

  L1  single statement only
  L2  read-only statement shape (SELECT / WITH / EXPLAIN / TABLE / VALUES)
  L3  forbidden keywords (DDL + DML)
  L4  dangerous functions (filesystem, sleep, session control)
  L5  table whitelist (optional, per deployment)
  L6  forced row limit
  L7  audit record + session hardening

Two of the seven cannot live in a string checker and are enforced on the
database side instead; ``guard()`` returns them in ``session_sql`` so the
caller applies them before running anything:

  * a role that only has SELECT (physical write protection)
  * statement_timeout (a runaway query cannot hold the instance hostage)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

ALLOWED_PREFIX = re.compile(r"^(SELECT|WITH|EXPLAIN|TABLE|VALUES)\b", re.IGNORECASE)
FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|GRANT|REVOKE|CREATE|COPY|MERGE"
    r"|CALL|DO|LOCK|VACUUM|REINDEX|CLUSTER|COMMENT|REFRESH|SECURITY|SET|RESET|DISCARD)\b",
    re.IGNORECASE,
)
DANGEROUS_FUNCTIONS = re.compile(
    r"\b(pg_sleep|pg_sleep_for|pg_sleep_until|pg_read_file|pg_read_binary_file"
    r"|pg_ls_dir|pg_stat_file|lo_import|lo_export|dblink|pg_terminate_backend"
    r"|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|set_config"
    r"|pg_advisory_lock|pg_advisory_xact_lock|current_setting)\b",
    re.IGNORECASE,
)
TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)", re.IGNORECASE
)
HAS_LIMIT = re.compile(r"\blimit\s+\d+", re.IGNORECASE)

SESSION_SQL = [
    "SET default_transaction_read_only = on",
    "SET statement_timeout = '10s'",
    "SET idle_in_transaction_session_timeout = '30s'",
]


@dataclass
class GuardResult:
    ok: bool
    sql: str
    layer: str = ""
    reason: str = ""
    rewritten: bool = False
    tables: list[str] = field(default_factory=list)
    session_sql: list[str] = field(default_factory=list)
    audit: dict = field(default_factory=dict)

    def __bool__(self) -> bool:  # allows `if guard(sql):`
        return self.ok


def _sanitize(sql: str) -> str:
    """Drop string literals and comments so they cannot hide keywords."""
    out: list[str] = []
    i, n = 0, len(sql)
    in_quote = False
    while i < n:
        ch = sql[i]
        if in_quote:
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    i += 2
                    continue
                in_quote = False
            i += 1
            continue
        if ch == "'":
            in_quote = True
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(ch)
        i += 1
    return " ".join("".join(out).split())


def guard(sql: str, *, max_rows: int = 100, table_whitelist=None,
          timeout_ms: int = 10_000) -> GuardResult:
    """Check (and if needed rewrite) a query before it is allowed near a database."""
    started = time.time()
    session_sql = list(SESSION_SQL)
    session_sql[1] = f"SET statement_timeout = '{timeout_ms}ms'"
    result = GuardResult(ok=False, sql=sql, session_sql=session_sql)

    def fail(layer: str, reason: str) -> GuardResult:
        result.layer, result.reason = layer, reason
        result.audit = {"decision": "blocked", "layer": layer, "reason": reason,
                        "ms": round((time.time() - started) * 1000, 2)}
        return result

    raw = sql.strip()
    if not raw:
        return fail("L1", "empty statement")

    # Analyse a comment- and literal-free copy; execute the original text.
    clean = _sanitize(raw)
    if not clean:
        return fail("L1", "statement is empty after removing comments")

    clean_body = clean.rstrip().rstrip(";").strip()
    if ";" in clean_body:
        return fail("L1", "multiple statements in one call are not allowed")

    if not ALLOWED_PREFIX.match(clean_body):
        head = clean_body.split()[0].upper() if clean_body.split() else "?"
        return fail("L2", f"only read-only statements are allowed (got {head})")

    hit = FORBIDDEN_KEYWORDS.search(clean_body)
    if hit:
        return fail("L3", f"forbidden keyword: {hit.group(1).upper()}")

    hit = DANGEROUS_FUNCTIONS.search(clean_body)
    if hit:
        return fail("L4", f"dangerous function: {hit.group(1)}")

    result.tables = sorted({m.group(1).lower() for m in TABLE_REF.finditer(clean_body)})
    if table_whitelist:
        allowed = {t.lower() for t in table_whitelist}
        outside = [t for t in result.tables
                   if t.lower() not in allowed and t.split(".")[-1].lower() not in allowed]
        if outside:
            return fail("L5", f"table outside the whitelist: {', '.join(outside)}")

    body = raw.rstrip().rstrip(";").strip()
    final_sql = body
    if not HAS_LIMIT.search(clean_body):
        final_sql = f"{body}\nLIMIT {max_rows}"
        result.rewritten = True
    result.sql = final_sql

    result.ok = True
    result.audit = {"decision": "allowed", "tables": result.tables,
                    "rewritten": result.rewritten, "max_rows": max_rows,
                    "ms": round((time.time() - started) * 1000, 2)}
    return result


GUARD_CASES: list[tuple[str, bool]] = [
    ("SELECT 1", True),
    ("SELECT id, amount FROM shop.orders WHERE status = 20", True),
    ("WITH recent AS (SELECT 1 AS x) SELECT * FROM recent", True),
    ("EXPLAIN SELECT * FROM shop.orders", True),
    ("SELECT * FROM shop.orders -- ; DROP TABLE shop.users", True),
    ("DROP TABLE shop.orders", False),
    ("DELETE FROM shop.orders WHERE 1 = 1", False),
    ("UPDATE shop.orders SET amount = 0", False),
    ("INSERT INTO shop.orders (id) VALUES (1)", False),
    ("TRUNCATE shop.orders", False),
    ("SELECT pg_sleep(60)", False),
    ("SELECT * FROM shop.orders; DROP TABLE shop.users", False),
]


def run_selftest() -> tuple[list[tuple[str, bool, bool]], bool]:
    """Return (rows, all_passed) where each row is (sql, expected, passed)."""
    rows = []
    all_passed = True
    for sql, expected in GUARD_CASES:
        got = guard(sql).ok
        passed = got is expected
        all_passed = all_passed and passed
        rows.append((sql, expected, passed))

    limited = guard("SELECT * FROM shop.orders")
    limit_ok = limited.ok and "LIMIT 100" in limited.sql
    all_passed = all_passed and limit_ok
    rows.append(("* forced LIMIT on a query without one", True, limit_ok))
    return rows, all_passed
