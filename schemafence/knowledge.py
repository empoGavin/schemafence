"""The knowledge layer: chunk -> embed -> store -> retrieve.

An agent needs two kinds of memory:

  * what happened before — runbooks, incident notes, design decisions
  * what is true right now — the live catalogue and its statistics

This module is the first kind.  It has two interchangeable backends so the
demo never depends on a paid service:

  offline   deterministic hashed embedding + a JSON file     (no key, no deps)
  live      OpenAI-compatible embedding + pgvector           (key + database)

Both produce the same 1024-dimension vectors, so the retrieval code is
identical and only the storage differs.

Be honest about the offline vector: it is **lexical**, not semantic.  It
matches character bigrams and words, so it finds the right note when the
question shares vocabulary with it, and misses paraphrases.  That is exactly
why it makes a good baseline — measure it, then measure the API embedding,
and put both numbers in the README.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

DIM = 1024

CJK = re.compile(r"[\u4e00-\u9fff]")
CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
SENTENCE = re.compile(r"[^。！？；!?;]+[。！？；!?;]?")


# --------------------------------------------------------------------------- #
# sizing
# --------------------------------------------------------------------------- #

def estimate_tokens(text: str) -> int:
    """Dependency-free token estimate: one token per CJK char, ~1.3 per word.

    It is an estimate and it is used as one — for chunk *sizing* only.
    Never quote it as an exact token count.
    """
    return len(CJK.findall(text)) + int(len(WORD.findall(text)) * 1.3) + 1


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE.findall(text) if s.strip()]


# --------------------------------------------------------------------------- #
# offline embedding — the hashing trick
# --------------------------------------------------------------------------- #

def _stable_hash(token: str) -> int:
    """hash() is salted per process; the store must stay reproducible."""
    return int.from_bytes(
        hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big"
    )


def _features(text: str) -> list[tuple[str, float]]:
    """Latin words + CJK character bigrams.  Bigrams carry most of the signal
    for Chinese, where single characters are far too common to discriminate."""
    feats: list[tuple[str, float]] = [(m.group(0), 1.0) for m in WORD.finditer(text.lower())]
    for run in CJK_RUN.findall(text):
        if len(run) == 1:
            feats.append((run, 1.0))
        else:
            feats.extend((run[i:i + 2], 1.4) for i in range(len(run) - 1))
    return feats


def embed_offline(text: str, dim: int = DIM,
                  idf: dict[str, float] | None = None) -> list[float]:
    """Deterministic hashed bag-of-features, L2-normalised.

    Same text always gives the same vector, on any machine, with no network.

    ``idf`` is the corpus weighting computed at ingest time.  Without it every
    common word counts as much as 'bloat', which is why the first version of
    this ranked the wrong note first — the fix is three lines and a JSON file,
    not a bigger model.
    """
    raw: dict[str, float] = {}
    for token, weight in _features(text):
        if idf is not None:
            weight *= idf.get(token, 1.0)
        raw[token] = raw.get(token, 0.0) + weight

    vec = [0.0] * dim
    for token, weight in raw.items():
        h = _stable_hash(token)
        idx = h % dim
        sign = 1.0 if (h // dim) % 2 == 0 else -1.0
        vec[idx] += sign * math.sqrt(max(weight, 0.0))

    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def build_idf(texts: list[str]) -> dict[str, float]:
    """log(1 + N/df) over chunk-level features — the whole 'training' step."""
    df: dict[str, int] = {}
    for text in texts:
        for token in {tok for tok, _ in _features(text)}:
            df[token] = df.get(token, 0) + 1
    total = max(len(texts), 1)
    return {token: round(math.log(1.0 + total / count), 3)
            for token, count in df.items()}


def save_idf(idf: dict[str, float], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(idf, ensure_ascii=False), encoding="utf-8")


def load_idf(path: str | Path) -> dict[str, float]:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def cosine(a, b) -> float:
    """Both sides are normalised, so the dot product is the cosine."""
    return sum(x * y for x, y in zip(a, b))


# --------------------------------------------------------------------------- #
# live embedding — any OpenAI-compatible endpoint, via urllib (still no deps)
# --------------------------------------------------------------------------- #

DEFAULT_EMBED_MODEL = "text-embedding-v3"
DEFAULT_EMBED_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class Embedder:
    """Text -> vector.  ``mode='auto'`` picks the API only if a key exists."""

    def __init__(self, mode: str = "auto", dim: int = DIM,
                 model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, batch: int = 10,
                 dimensions: int | None = None,
                 idf: dict[str, float] | None = None):
        key = api_key if api_key is not None else os.environ.get("SF_EMBED_API_KEY", "")
        if mode == "auto":
            mode = "api" if key else "offline"
        if mode == "api" and not key:
            raise RuntimeError(
                "mode='api' needs SF_EMBED_API_KEY (see docs appendix A)")

        self.mode = mode
        self.dim = dim
        self.model = model or os.environ.get("SF_EMBED_MODEL", DEFAULT_EMBED_MODEL)
        self.base_url = (base_url or os.environ.get("SF_EMBED_BASE_URL",
                                                    DEFAULT_EMBED_BASE)).rstrip("/")
        self.api_key = key
        self.batch = batch
        self.dimensions = dimensions
        self.idf: dict[str, float] = idf or {}

    @property
    def label(self) -> str:
        if self.mode == "offline":
            suffix = " + corpus idf" if self.idf else ""
            return f"offline / hashed-lexical{suffix} / {self.dim}d"
        return f"api / {self.model} / {self.dim}d"

    def one(self, text: str) -> list[float]:
        return self.many([text])[0]

    def many(self, texts: list[str]) -> list[list[float]]:
        if self.mode == "offline":
            return [embed_offline(t, self.dim, self.idf or None) for t in texts]

        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch):
            out.extend(self._api_call(texts[start:start + self.batch]))
        return out

    def _api_call(self, texts: list[str]) -> list[list[float]]:
        payload: dict = {"model": self.model, "input": texts}
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        req = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            hints = {
                400: "the request was rejected — most often the `dimensions` "
                     "field is not supported by this model (e.g. BAAI/bge-m3 "
                     "on SiliconFlow, a fixed-1024 model).  Leave "
                     "SF_EMBED_DIMENSIONS unset; the provider's exact "
                     "complaint is in the body above",
                401: "the key was rejected.  A non-empty SF_EMBED_API_KEY was sent, so "
                     "check the VALUE: is it a DashScope key for THIS endpoint "
                     f"({self.base_url})?  A key from another provider, a truncated "
                     "copy, or stray quotes/whitespace all land here.  Try:  "
                     "curl -s $SF_EMBED_BASE_URL/embeddings -H \"Authorization: Bearer "
                     "$SF_EMBED_API_KEY\" -H 'Content-Type: application/json' "
                     "-d '{\"model\":\"...\",\"input\":[\"hi\"]}'  — the JSON body in "
                     "the message above names the exact code (InvalidApiKey etc.)",
                403: "the key is valid but not allowed to call this model "
                     "(not activated, or out of quota)",
                404: f"no such model or path: {self.model} @ {self.base_url}",
                429: "rate limited — retry with backoff",
            }
            raise RuntimeError(
                f"embeddings API returned HTTP {e.code} for {self.base_url}/embeddings\n"
                f"  body  : {detail}\n"
                f"  likely: {hints.get(e.code, 'see the body above')}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"cannot reach {self.base_url} ({e.reason}) — network, DNS, or "
                "wrong SF_EMBED_BASE_URL") from None
        data = sorted(body["data"], key=lambda d: d.get("index", 0))
        vecs = [d["embedding"] for d in data]
        if vecs and len(vecs[0]) != self.dim:
            raise RuntimeError(
                f"the endpoint returned {len(vecs[0])}-dim vectors but the store "
                f"expects {self.dim} (set SF_EMBED_DIM, or recreate the table)")
        return vecs


# --------------------------------------------------------------------------- #
# chunking — heading-path aware, because the section is part of the evidence
# --------------------------------------------------------------------------- #

@dataclass
class Chunk:
    source: str
    section: str
    content: str
    token_count: int
    vector: list[float] = field(default_factory=list)


def chunk_markdown(text: str, source: str, target_tokens: int = 512,
                   overlap_tokens: int = 64) -> list[Chunk]:
    """Split one document into overlapping chunks that remember where they came from.

    Three rules, and the last two are the ones people forget:

      1. a heading is a *soft* boundary — a section shorter than half a chunk
         is merged forward into the next one, so a chunk is never a fragment
         of a sentence's worth of context;
      2. a heading is also *kept in the text*, not just used as a label.  In a
         hand-written note the heading is where the most discriminative words
         live ("判断治理是否真的有效"), and dropping it means a question phrased
         the way the author titled the section retrieves nothing — which is
         exactly what the first eval run showed;
      3. the label is captured when the chunk *starts*, not when it is flushed,
         and overlap never crosses a heading.  The first version set the label
         only on the empty-buffer path, so every chunk after the first came out
         as "(untitled)" and the evidence lost the one field that tells the
         reader which section it came from.

    Front matter before the first heading (a disclaimer, a licence) is dropped:
    it is header plumbing, not evidence, and embedding it only adds noise.
    """
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []
    label = "(untitled)"
    chunks: list[Chunk] = []
    soft_min = max(80, target_tokens // 2)

    def section_path() -> str:
        return " / ".join(t for _, t in stack) if stack else "(untitled)"

    def flush(keep_overlap: bool = True) -> None:
        nonlocal buffer
        if not buffer:
            return
        content = " ".join(buffer).strip()
        if content:
            chunks.append(Chunk(source=source, section=label, content=content,
                                token_count=estimate_tokens(content)))
        if keep_overlap and len(buffer) > 1 and overlap_tokens > 0:
            keep: list[str] = []
            acc = 0
            for sentence in reversed(buffer[:-1]):
                if acc + estimate_tokens(sentence) > overlap_tokens:
                    break
                keep.insert(0, sentence)
                acc += estimate_tokens(sentence)
            buffer = keep
        else:
            buffer = []

    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            title = heading.group(2).strip()
            level = len(heading.group(1))
            if not stack:
                buffer = []                     # drop front matter, see docstring
            elif buffer and estimate_tokens(" ".join(buffer)) >= soft_min:
                flush(keep_overlap=False)       # a heading is a hard boundary for overlap
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            if not buffer:
                label = section_path()
            buffer.append(f"{title}。")         # keep the title searchable
            continue
        if not line.strip():
            continue
        for sentence in _sentences(line):
            buffer.append(sentence)
            if estimate_tokens(" ".join(buffer)) >= target_tokens:
                flush()
    flush()
    return chunks


def read_corpus(directory: str | Path, target_tokens: int = 512,
                overlap_tokens: int = 64) -> list[Chunk]:
    """Every .md / .txt under ``directory``, chunked."""
    root = Path(directory)
    if not root.exists():
        raise FileNotFoundError(f"no such corpus directory: {root}")
    files = sorted(p for p in root.rglob("*")
                   if p.suffix.lower() in {".md", ".txt"} and p.is_file())
    chunks: list[Chunk] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks.extend(chunk_markdown(text, source=path.stem,
                                     target_tokens=target_tokens,
                                     overlap_tokens=overlap_tokens))
    return chunks


# --------------------------------------------------------------------------- #
# stores
# --------------------------------------------------------------------------- #

@dataclass
class Hit:
    source: str
    section: str
    content: str
    score: float
    token_count: int = 0

    def render(self, width: int = 96) -> str:
        head = f"[{self.score:.3f}] {self.source} · {self.section}"
        snippet = " ".join(self.content.split())
        if len(snippet) > width:
            snippet = snippet[:width] + "…"
        return f"{head}\n    {snippet}"


class JsonStore:
    """The offline backend: a JSON file you can read with your own eyes."""

    def __init__(self, path: str | Path, dim: int = DIM):
        self.path = Path(path)
        self.dim = dim
        self.chunks: list[Chunk] = []
        self.idf: dict[str, float] = {}
        # set by the CLI so that query and chunk vectors are built the same way
        self.embedder: Embedder | None = None

    @classmethod
    def load(cls, path: str | Path, dim: int = DIM) -> "JsonStore":
        store = cls(path, dim=dim)
        p = Path(path)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            store.dim = raw.get("dim", dim)
            store.idf = raw.get("idf", {})
            store.chunks = [Chunk(**c) for c in raw.get("chunks", [])]
        return store

    def replace(self, chunks: list[Chunk]) -> None:
        self.chunks = list(chunks)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"dim": self.dim, "idf": self.idf,
             "chunks": [asdict(c) for c in self.chunks]},
            ensure_ascii=False), encoding="utf-8")

    def search(self, vector: list[float], k: int = 5) -> list[Hit]:
        scored = [(cosine(vector, c.vector), c) for c in self.chunks]
        scored.sort(key=lambda pair: -pair[0])
        return [Hit(source=c.source, section=c.section, content=c.content,
                    score=round(s, 4), token_count=c.token_count)
                for s, c in scored[:k]]

    def stats(self) -> dict:
        sources = {c.source for c in self.chunks}
        total = sum(c.token_count for c in self.chunks)
        return {"documents": len(sources), "chunks": len(self.chunks),
                "avg_tokens": round(total / len(self.chunks)) if self.chunks else 0,
                "dim": self.dim}


DOC_CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS doc_chunks (
  id          BIGSERIAL PRIMARY KEY,
  source      TEXT        NOT NULL,
  section     TEXT,
  content     TEXT        NOT NULL,
  token_count INT,
  embedding   VECTOR(%(dim)s),
  created_at  TIMESTAMPTZ DEFAULT now()
)
"""

HNSW_DDL = """
CREATE INDEX IF NOT EXISTS idx_chunks_hnsw ON doc_chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
"""


def _vec_literal(vector: list[float]) -> str:
    """pgvector accepts the text form '[a,b,c]'.  Sending it as a string keeps
    the code independent of the pgvector-python adapters."""
    return "[" + ",".join(f"{v:.6g}" for v in vector) + "]"


class PgStore:
    """The live backend: pgvector, so 20 years of PG knowledge is the point.

    Every statement here is the *same* statement the offline store runs —
    only the ANN index makes it fast.  ``use_index=False`` is kept around so
    the difference can be measured rather than asserted.
    """

    def __init__(self, dsn: str, embedder: Embedder, dim: int | None = None):
        import psycopg  # lazy: offline mode has no third-party deps
        self.dsn = dsn
        self.embedder = embedder
        self.dim = dim or embedder.dim
        self.conn = psycopg.connect(dsn)
        self.conn.autocommit = True

    def close(self) -> None:
        self.conn.close()

    def ensure_schema(self, with_index: bool = True) -> None:
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(DOC_CHUNKS_DDL % {"dim": self.dim})
            if with_index:
                cur.execute(HNSW_DDL)

    def replace(self, chunks: list[Chunk]) -> int:
        sources = sorted({c.source for c in chunks})
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM doc_chunks WHERE source = ANY(%s)", (sources,))
            for c in chunks:
                cur.execute(
                    "INSERT INTO doc_chunks (source, section, content, token_count,"
                    " embedding) VALUES (%s, %s, %s, %s, %s::vector)",
                    (c.source, c.section, c.content, c.token_count,
                     _vec_literal(c.vector)))
        return len(chunks)

    def search(self, vector: list[float], k: int = 5,
               use_index: bool = True) -> list[Hit]:
        sql = ("SELECT source, section, content, token_count,"
               "       1 - (embedding <=> %s::vector) AS score"
               "  FROM doc_chunks"
               " ORDER BY embedding <=> %s::vector"
               " LIMIT %s")
        literal = _vec_literal(vector)
        if use_index:
            with self.conn.cursor() as cur:
                cur.execute(sql, (literal, literal, k))
                rows = cur.fetchall()
        else:
            # SET LOCAL only means anything inside a transaction, and the
            # comparison is the point: same query, index off.
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.execute("SET LOCAL enable_indexscan = off")
                    cur.execute("SET LOCAL enable_bitmapscan = off")
                    cur.execute(sql, (literal, literal, k))
                    rows = cur.fetchall()
        return [Hit(source=s, section=sec, content=c, token_count=t,
                    score=round(float(score), 4))
                for s, sec, c, t, score in rows]

    def stats(self) -> dict:
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) AS chunks, count(DISTINCT source) AS docs"
                        "  FROM doc_chunks")
            chunks, docs = cur.fetchone()
            cur.execute("SELECT coalesce(round(avg(token_count)), 0) FROM doc_chunks")
            avg = cur.fetchone()[0]
            cur.execute("SELECT pg_size_pretty(pg_total_relation_size('doc_chunks'))")
            size = cur.fetchone()[0]
        return {"documents": docs, "chunks": chunks, "avg_tokens": avg,
                "dim": self.dim, "table_size": size}


# --------------------------------------------------------------------------- #
# convenience
# --------------------------------------------------------------------------- #

def ingest_directory(directory, store, embedder: Embedder | None = None,
                     target_tokens: int = 512, overlap_tokens: int = 64) -> list[Chunk]:
    """Read -> chunk -> weight -> embed -> hand to the store.

    The IDF is computed here, from this corpus, and then travels with the
    store — the query side has to use the same weights the chunk side used,
    or the two vectors stop being comparable.
    """
    embedder = embedder or Embedder()
    chunks = read_corpus(directory, target_tokens=target_tokens,
                         overlap_tokens=overlap_tokens)
    if chunks:
        texts = [c.content for c in chunks]
        if embedder.mode == "offline":
            embedder.idf = build_idf(texts)
        vectors = embedder.many(texts)
        for chunk, vector in zip(chunks, vectors):
            chunk.vector = vector
    if hasattr(store, "idf"):
        store.idf = embedder.idf
    store.replace(chunks)
    return chunks
