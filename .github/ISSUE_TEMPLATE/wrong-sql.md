---
name: plausible-but-wrong SQL
about: A statement that ran, returned a number, and was wrong — the failure this project is about
title: ''
labels: ''
assignees: ''
---

**Sanitize first.** Replace real schema, table and column names with made-up ones, drop any
credentials or connection strings, and do not paste anything from an employer's internal
documentation. A renamed reproduction is just as useful; an unsanitized one cannot be used.

### The statement

```sql

```

### The schema it ran against

The three or four tables and columns that matter — a sketch, not the whole model:

```sql

```

### What it returned, and what was actually true

- returned:
- expected:

### Which check should have caught it

The list is in the README. Tick whatever is closest, and say so if the answer is "none of them":

- [ ] 1 schema resolution: wrong table or column picked
- [ ] 2 join-key sanity: rows silently dropped by the join
- [ ] 3 type & precision: value changed on the way through
- [ ] 4 NULL semantics: three-valued logic
- [ ] S SQL-side semantics: multiplied measure, cancelled `LEFT JOIN`
- [ ] 5 result plausibility
- [ ] 6 guardrails
- [ ] none of them: a gap

### Anything else

Which backend and version, and any detail that made this hard to see coming.
